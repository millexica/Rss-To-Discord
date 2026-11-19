#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_SYSTEMD_DIR="${HOME}/.config/systemd/user"

echo "=== Installing RSS to Discord systemd user service ==="
mkdir -p "${USER_SYSTEMD_DIR}"

cp "${SCRIPT_DIR}/systemd/rss-to-discord.service" "${USER_SYSTEMD_DIR}/"
cp "${SCRIPT_DIR}/systemd/rss-to-discord.timer" "${USER_SYSTEMD_DIR}/"

echo "Reloading systemd user daemon..."
systemctl --user daemon-reload

echo "Enabling and starting rss-to-discord.timer..."
systemctl --user enable --now rss-to-discord.timer

echo ""
echo "Status of rss-to-discord.timer:"
systemctl --user status rss-to-discord.timer --no-pager

echo ""
echo "Setup complete! The timer is active and will check the feed every 5 minutes."
echo "To view logs anytime: journalctl --user -u rss-to-discord.service -f"
