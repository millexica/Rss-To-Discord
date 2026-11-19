# RSS to Discord Webhook Relay

Automatically fetches new posts from multiple RSS feeds and relays them as formatted embeds to a Discord App via webhook.

## Configuration

Copy `config.example.json` to `config.json` and fill in your details:

```bash
cp config.example.json config.json
```

Settings in [`config.json`](./config.json):

```json
{
  "feeds": [
    {
      "name": "bvectorx3",
      "url": "https://rss.app/feeds/3kGLdnMw61WEIXiq.xml",
      "initial_action": "post-latest"
    },
    {
      "name": "WW1x2",
      "url": "https://rss.app/feeds/C6OtuhAt7sf1BTrD.xml",
      "initial_action": "post-latest"
    }
  ],
  "webhook_url": "https://discord.com/api/webhooks/...",
  "check_interval_seconds": 300,
  "state_file": "data/posted_ids.json",
  "initial_action": "post-latest",
  "discord": {
    "username": "X / Twitter Updates",
    "avatar_url": "https://abs.twimg.com/favicons/twitter.3.ico",
    "embed_color": 1942002
  }
}
```

### Feed Options
Each feed in `"feeds"` can have:
- `"name"`: The label used in logs.
- `"url"`: The RSS feed URL.
- `"webhook_url"`: The webhook URL to route this feed to another channel.
- `"initial_action"` 
  - `"post-latest"` (default): Posts only the most recent item, marks older items as seen to avoid flooding.
  - `"mark-seen"`: Marks all current items as seen silently without posting anything.
  - `"post-all"`: Posts all items currently in the feed.

