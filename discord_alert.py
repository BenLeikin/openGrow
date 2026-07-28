#!/usr/bin/env python3
"""Discord alert transport via an incoming webhook (openGardener).

Posts alerts to a Discord channel as a colour-coded embed. Callers say
send(title, message, level=...) and don't care how it's delivered.

The webhook URL is a secret -- anyone holding it can post to your channel -- so
it is read at runtime, never hardcoded:
  1. "discord_webhook" in config.json (next to this module), or
  2. a .discord_webhook file next to this module (gitignore it)

  import discord_alert
  discord_alert.send("openGardener", "All sensors healthy.", level="good")

Levels map to embed colour: good (green), watch (yellow), problem/error (red),
info (blurple). Returns True on success, False otherwise, and no-ops (False)
when no webhook is configured, so callers never have to guard.
"""
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("config.json")
WEBHOOK_FILE = Path(__file__).with_name(".discord_webhook")

COLORS = {
    "good": 0x57F287, "success": 0x57F287,
    "watch": 0xFEE75C, "warning": 0xFEE75C,
    "problem": 0xED4245, "error": 0xED4245,
    "info": 0x5865F2,
}


def _cfg():
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def webhook_url():
    url = _cfg().get("discord_webhook")
    if not url:
        try:
            if WEBHOOK_FILE.exists():
                url = WEBHOOK_FILE.read_text().strip()
        except Exception:
            url = None
    if not url:
        return None
    url = url.strip()
    # discordapp.com is the legacy host; normalise to the current one
    return url.replace("discordapp.com", "discord.com")


def enabled():
    return bool(webhook_url())


def send(title, message, level="info", username="openGardener", fields=None):
    """Post an embed to the configured Discord webhook. `fields` is an optional
    list of {"name","value","inline"} dicts for structured detail."""
    url = webhook_url()
    if not url:
        return False
    embed = {
        "title": str(title)[:256],
        "description": str(message)[:4000],
        "color": COLORS.get(level, COLORS["info"]),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "openGardener"},
    }
    if fields:
        embed["fields"] = [
            {"name": str(f.get("name", ""))[:256],
             "value": str(f.get("value", ""))[:1024],
             "inline": bool(f.get("inline", False))}
            for f in fields[:25]
        ]
    payload = {"username": username, "embeds": [embed]}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 # Discord's Cloudflare 403s the default urllib agent.
                 "User-Agent": "openGardener/1.0 (+https://opengardener.pilg0re.net)"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300  # Discord returns 204 on success
    except Exception as e:
        print(f"discord send failed: {e}")
        return False


if __name__ == "__main__":
    import sys
    msg = sys.argv[1] if len(sys.argv) > 1 else "Test alert from openGardener."
    if not enabled():
        print('No Discord webhook configured. Add "discord_webhook" to config.json')
        print("or create a .discord_webhook file next to this script, then retry.")
    else:
        ok = send("openGardener", msg, level="info")
        print("sent" if ok else "failed")
