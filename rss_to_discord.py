#!/usr/bin/env python3
"""
RSS to Discord Webhook Relay
Monitors one or more RSS feeds (e.g., Keep.md, RSS.app Twitter/X feeds)
and relays new updates to a Discord channel via webhook.
"""

import argparse
import datetime
import email.utils
import html
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("rss_to_discord")

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

XML_NAMESPACES = {
    "atom": "http://www.w3.org/2005/Atom",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "media": "http://search.yahoo.com/mrss/",
}


def load_config(config_path: str) -> dict:
    config = {
        "feeds": [],
        "webhook_url": "",
        "check_interval_seconds": 300,
        "state_file": os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "posted_ids.json"),
        "initial_action": "post-latest",
        "discord": {
            "username": "Twitter Feed",
            "avatar_url": "https://abs.twimg.com/favicons/twitter.3.ico",
            "embed_color": 1942002,  # Twitter Blue (#1DA1F2)
        },
    }

    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                user_config = json.load(f)
                config.update(user_config)
                if "discord" in user_config and isinstance(user_config["discord"], dict):
                    config["discord"] = {**config.get("discord", {}), **user_config["discord"]}
        except Exception as e:
            logger.warning(f"Failed to read config file at {config_path}: {e}. Using defaults.")

    # Normalize feeds list
    if not config["feeds"]:
        if config.get("rss_url"):
            config["feeds"] = [{"name": "default", "url": config["rss_url"]}]

    # Normalize state file path
    if not os.path.isabs(config["state_file"]):
        base_dir = os.path.dirname(os.path.abspath(config_path if os.path.exists(config_path) else __file__))
        config["state_file"] = os.path.abspath(os.path.join(base_dir, config["state_file"]))

    # Allow environment variable overrides
    if os.environ.get("DISCORD_WEBHOOK_URL"):
        config["webhook_url"] = os.environ["DISCORD_WEBHOOK_URL"]
    if os.environ.get("CHECK_INTERVAL"):
        try:
            config["check_interval_seconds"] = int(os.environ["CHECK_INTERVAL"])
        except ValueError:
            pass

    return config


def load_posted_ids(state_file: str) -> set:
    if os.path.exists(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return set(data)
                elif isinstance(data, dict) and "posted_ids" in data:
                    return set(data["posted_ids"])
        except Exception as e:
            logger.warning(f"Failed to read state file at {state_file}: {e}")
    return set()


def save_posted_ids(state_file: str, posted_ids: set, max_history: int = 1000):
    os.makedirs(os.path.dirname(os.path.abspath(state_file)), exist_ok=True)
    temp_file = f"{state_file}.tmp"
    history_list = list(posted_ids)[-max_history:]
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(history_list, f, indent=2)
        os.replace(temp_file, state_file)
    except Exception as e:
        logger.error(f"Failed to save state file at {state_file}: {e}")


def clean_html(raw_html: str) -> str:
    if not raw_html:
        return ""

    # Remove script and style tags completely
    text = re.sub(r"<script[^>]*>.*?</script>", "", raw_html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)

    # Check for embedded blockquote tweet structure (e.g. from rss.app)
    # <blockquote class="twitter-tweet"><p ...>TWEET_TEXT</p>— Author <a ...>Date</a></blockquote>
    tweet_p_match = re.search(r"<p[^>]*>(.*?)</p>", text, flags=re.DOTALL | re.IGNORECASE)
    if tweet_p_match and "twitter-tweet" in text:
        text = tweet_p_match.group(1)

    # Convert breaks and paragraphs to newlines
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>\s*<p[^>]*>", "\n\n", text, flags=re.IGNORECASE)

    # Remove all remaining HTML tags
    text = re.sub(r"<[^>]+>", "", text)

    # Unescape HTML entities (&amp;, &lt;, etc.)
    text = html.unescape(text)

    # Strip excess blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_images(item_el: ET.Element, content_html: str, desc_html: str) -> list:
    images = []
    seen = set()

    # 1. Media RSS namespace (<media:content> or <media:thumbnail>)
    for m in item_el.findall("{http://search.yahoo.com/mrss/}content"):
        url = m.attrib.get("url", "").strip()
        if url and url not in seen:
            seen.add(url)
            images.append(url)

    for m in item_el.findall("{http://search.yahoo.com/mrss/}thumbnail"):
        url = m.attrib.get("url", "").strip()
        if url and url not in seen:
            seen.add(url)
            images.append(url)

    # 2. HTML <img> tags in content or description
    combined = (content_html or "") + " " + (desc_html or "")
    img_matches = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', combined, flags=re.IGNORECASE)
    for url in img_matches:
        url = url.strip()
        if url and url not in seen:
            seen.add(url)
            images.append(url)

    return images


def parse_rfc2822_date(date_str: str) -> str:
    if not date_str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        dt = email.utils.parsedate_to_datetime(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.isoformat()
    except Exception:
        return datetime.datetime.now(datetime.timezone.utc).isoformat()


def fetch_rss(url: str, user_agent: str = "Mozilla/5.0 (compatible; RSS-to-Discord/1.1)") -> str:
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def parse_feed(xml_text: str) -> tuple:
    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("Invalid RSS: <channel> element not found")

    image_url = ""
    img_el = channel.find("image/url")
    if img_el is not None and img_el.text:
        image_url = img_el.text.strip()

    channel_info = {
        "title": (channel.findtext("title") or "RSS Feed").strip(),
        "link": (channel.findtext("link") or "").strip(),
        "description": (channel.findtext("description") or "").strip(),
        "avatar_url": image_url,
    }

    items = []
    for item_el in channel.findall("item"):
        title = item_el.findtext("title") or ""
        link = item_el.findtext("link") or ""
        guid = item_el.findtext("guid") or link or title
        creator = item_el.findtext("{http://purl.org/dc/elements/1.1/}creator") or ""
        pub_date = item_el.findtext("pubDate") or ""
        desc_raw = item_el.findtext("description") or ""
        content_raw = item_el.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or ""

        images = extract_images(item_el, content_raw, desc_raw)
        clean_text_content = clean_html(desc_raw) or clean_html(content_raw) or title.strip()

        items.append({
            "guid": guid.strip(),
            "title": title.strip(),
            "link": link.strip(),
            "creator": creator.strip(),
            "pub_date": pub_date.strip(),
            "timestamp": parse_rfc2822_date(pub_date),
            "text": clean_text_content,
            "images": images,
        })

    return channel_info, items


def build_discord_payload(item: dict, channel_info: dict, discord_cfg: dict) -> dict:
    link = item["link"] or ""
    title = item["title"] or ""
    text = item["text"] or ""
    creator = item["creator"] or ""
    images = item.get("images", [])

    embed_color = discord_cfg.get("embed_color", 1942002)

    display_title = title
    if len(display_title) > 256:
        display_title = display_title[:253] + "..."

    description_text = text
    if len(description_text) > 4000:
        description_text = description_text[:3997] + "..."

    author_name = channel_info.get("title", "X (Twitter)")
    author_url = channel_info.get("link", "https://x.com")
    avatar_url = channel_info.get("avatar_url") or discord_cfg.get("avatar_url", "https://abs.twimg.com/favicons/twitter.3.ico")

    footer_text = "X (Twitter)"
    if creator:
        channel_title_lower = author_name.lower()
        creator_clean = creator.lstrip("@").lower()
        if creator_clean and creator_clean not in channel_title_lower:
            footer_text = f"Reposted from {creator} • via RSS"
        else:
            footer_text = f"Posted by {creator} • via RSS"

    main_embed = {
        "author": {
            "name": author_name,
            "url": author_url,
            "icon_url": avatar_url,
        },
        "title": display_title if display_title else "New Post",
        "url": link if link else None,
        "description": description_text if description_text else None,
        "color": embed_color,
        "timestamp": item.get("timestamp"),
        "footer": {
            "text": footer_text,
        },
    }

    # Clean None values
    main_embed = {k: v for k, v in main_embed.items() if v is not None}

    if images:
        main_embed["image"] = {"url": images[0]}

    embeds = [main_embed]

    # Additional images: Discord groups embeds sharing the exact same URL into a 2x2 multi-image gallery
    if link and len(images) > 1:
        for extra_img in images[1:4]:
            embeds.append({
                "url": link,
                "image": {"url": extra_img},
            })

    username = author_name if len(author_name) <= 80 else author_name[:77] + "..."

    payload = {
        "username": username,
        "avatar_url": avatar_url,
        "embeds": embeds,
    }

    return payload


def send_to_discord(webhook_url: str, payload: dict, max_retries: int = 3) -> bool:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "RSS-to-Discord/1.1",
        },
        method="POST",
    )

    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status in (200, 204):
                    return True
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = 5
                try:
                    resp_body = json.loads(e.read().decode("utf-8"))
                    retry_after = float(resp_body.get("retry_after", 5))
                except Exception:
                    pass
                logger.warning(f"Rate limited by Discord. Waiting {retry_after:.1f}s before retry...")
                time.sleep(retry_after)
            elif 500 <= e.code < 600:
                logger.warning(f"Discord server error HTTP {e.code}. Attempt {attempt}/{max_retries}. Retrying in 2s...")
                time.sleep(2)
            else:
                try:
                    err_text = e.read().decode("utf-8", errors="replace")
                except Exception:
                    err_text = ""
                logger.error(f"Failed to send to Discord webhook (HTTP {e.code}): {err_text}")
                return False
        except Exception as e:
            logger.warning(f"Network error sending to Discord: {e}. Attempt {attempt}/{max_retries}. Retrying...")
            time.sleep(2)

    logger.error("Failed to deliver payload to Discord after max retries.")
    return False


def is_item_seen(item: dict, posted_ids: set) -> bool:
    guid = item.get("guid")
    link = item.get("link")
    return (guid and guid in posted_ids) or (link and link in posted_ids)


def mark_item_seen(item: dict, posted_ids: set):
    if item.get("guid"):
        posted_ids.add(item["guid"])
    if item.get("link"):
        posted_ids.add(item["link"])


def sync_single_feed(feed_cfg: dict, global_cfg: dict, posted_ids: set, dry_run: bool = False, initial_action_override: str = None) -> int:
    feed_url = feed_cfg.get("url") or feed_cfg.get("rss_url")
    feed_name = feed_cfg.get("name", feed_url)
    webhook_url = feed_cfg.get("webhook_url") or global_cfg.get("webhook_url")
    state_file = global_cfg["state_file"]
    initial_action = initial_action_override or feed_cfg.get("initial_action") or global_cfg.get("initial_action", "post-latest")

    if not webhook_url and not dry_run:
        logger.error(f"[{feed_name}] No Discord webhook URL configured.")
        return 0

    logger.info(f"[{feed_name}] Fetching RSS feed from {feed_url}...")
    try:
        xml_text = fetch_rss(feed_url)
    except Exception as e:
        logger.error(f"[{feed_name}] Error fetching RSS feed: {e}")
        return 0

    try:
        channel_info, items = parse_feed(xml_text)
    except Exception as e:
        logger.error(f"[{feed_name}] Error parsing RSS XML: {e}")
        return 0

    logger.info(f"[{feed_name}] Feed '{channel_info['title']}' loaded with {len(items)} items.")

    # Check if this feed has any items already seen
    feed_seen_count = sum(1 for item in items if is_item_seen(item, posted_ids))
    is_feed_first_run = (feed_seen_count == 0)

    if is_feed_first_run and items:
        logger.info(f"[{feed_name}] First run for this feed. Applying initial action: '{initial_action}'")
        if initial_action == "mark-seen":
            for item in items:
                mark_item_seen(item, posted_ids)
            if not dry_run:
                save_posted_ids(state_file, posted_ids)
            logger.info(f"[{feed_name}] Marked all {len(items)} existing items as seen.")
            return 0
        elif initial_action == "post-latest":
            for item in items[1:]:
                mark_item_seen(item, posted_ids)
            items_to_post = [items[0]]
        elif initial_action == "post-all":
            items_to_post = list(reversed(items))
        else:
            items_to_post = [items[0]]
    else:
        # Items to post: unposted items in chronological order (oldest first)
        items_to_post = [item for item in reversed(items) if not is_item_seen(item, posted_ids)]

    if not items_to_post:
        logger.info(f"[{feed_name}] No new items to post.")
        return 0

    logger.info(f"[{feed_name}] Found {len(items_to_post)} new item(s) to post.")
    posted_count = 0

    discord_cfg = {**global_cfg.get("discord", {}), **feed_cfg.get("discord", {})}

    for item in items_to_post:
        payload = build_discord_payload(item, channel_info, discord_cfg)

        if dry_run:
            logger.info(f"[{feed_name}] [DRY-RUN] Would post: {item['title']} ({item['link']})")
            logger.info(f"[{feed_name}] [DRY-RUN] Payload: {json.dumps(payload, indent=2)}")
            mark_item_seen(item, posted_ids)
            posted_count += 1
        else:
            logger.info(f"[{feed_name}] Posting to Discord: {item['title']} ({item['link']})")
            success = send_to_discord(webhook_url, payload)
            if success:
                mark_item_seen(item, posted_ids)
                save_posted_ids(state_file, posted_ids)
                posted_count += 1
                time.sleep(1)
            else:
                logger.error(f"[{feed_name}] Stopping sync due to delivery failure on item: {item['guid']}")
                break

    if not dry_run and posted_count > 0:
        save_posted_ids(state_file, posted_ids)

    logger.info(f"[{feed_name}] Sync complete. Posted {posted_count} item(s).")
    return posted_count


def sync_all_feeds(config: dict, dry_run: bool = False, initial_action_override: str = None) -> int:
    state_file = config["state_file"]
    posted_ids = load_posted_ids(state_file)
    feeds = config.get("feeds", [])

    total_posted = 0
    for feed in feeds:
        try:
            total_posted += sync_single_feed(
                feed,
                config,
                posted_ids,
                dry_run=dry_run,
                initial_action_override=initial_action_override,
            )
        except Exception as e:
            logger.error(f"Unexpected error processing feed {feed}: {e}", exc_info=True)

    return total_posted


def send_test_message(config: dict):
    webhook_url = config.get("webhook_url")
    if not webhook_url:
        logger.error("No webhook_url provided.")
        return

    feeds_count = len(config.get("feeds", []))
    feeds_summary = "\n".join(f"• **{f.get('name', 'Feed')}**: `{f.get('url')}`" for f in config.get("feeds", []))

    test_payload = {
        "username": "RSS Webhook Tester",
        "avatar_url": config.get("discord", {}).get("avatar_url", "https://abs.twimg.com/favicons/twitter.3.ico"),
        "embeds": [
            {
                "title": "RSS to Discord Relay Connected!",
                "description": f"Monitoring **{feeds_count}** active feed(s):\n\n{feeds_summary}",
                "color": 65280,  # Green
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "footer": {"text": "RSS to Discord Relay Test"},
            }
        ],
    }
    logger.info("Sending test message to Discord webhook...")
    success = send_to_discord(webhook_url, test_payload)
    if success:
        logger.info("Test message sent successfully! Check your Discord channel.")
    else:
        logger.error("Failed to send test message.")


def main():
    parser = argparse.ArgumentParser(description="Send RSS feed updates to a Discord webhook.")
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG_PATH, help="Path to config.json file")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--daemon", action="store_true", help="Run continuously in background/daemon mode")
    parser.add_argument("--interval", type=int, help="Check interval in seconds (default: from config or 300)")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and print updates without sending to Discord")
    parser.add_argument("--test", action="store_true", help="Send a test notification to Discord webhook and exit")
    parser.add_argument(
        "--initial-action",
        choices=["post-latest", "mark-seen", "post-all"],
        help="Initial run behavior: 'post-latest' (post single newest item), 'mark-seen' (silently mark all existing as seen), 'post-all' (post all items)",
    )
    args = parser.parse_args()

    config = load_config(args.config)

    if args.test:
        send_test_message(config)
        return

    interval = args.interval or config.get("check_interval_seconds", 300)

    if args.daemon:
        logger.info(f"Starting RSS-to-Discord daemon for {len(config.get('feeds', []))} feed(s). Checking every {interval}s ({interval/60:.1f}m)...")
        try:
            while True:
                sync_all_feeds(config, dry_run=args.dry_run, initial_action_override=args.initial_action)
                time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("Daemon stopped by user.")
    else:
        sync_all_feeds(config, dry_run=args.dry_run, initial_action_override=args.initial_action)


if __name__ == "__main__":
    main()
