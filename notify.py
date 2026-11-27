"""Send zero-cost Discord and Telegram run summaries when configured."""

import argparse
import json
import os
from pathlib import Path
import sys

import requests

from local_secrets import secret

DEFAULT_SUMMARY = Path(__file__).resolve().parent / "state" / "run_summary.json"


def load_summary(path):
    """Read an optional run summary JSON file without failing if missing."""
    if not path or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def format_summary(summary, *, status="unknown", token_warnings=(), run_url=""):
    """Build a concise run summary string for Discord and Telegram."""
    attempted = int(summary.get("attempted", 0) or 0)
    published = int(summary.get("published", 0) or 0)
    rendered = int(summary.get("completed_renders", 0) or 0)
    lines = [
        f"Auto Video Maker ({status.upper()})",
        f"Attempted: {attempted} | Rendered: {rendered} | Published: {published}",
    ]
    for item in summary.get("items", []):
        label = item.get("title") or item.get("topic_id") or f"Slot {item.get('index', '?')}"
        details = []
        if item.get("youtube_url"):
            details.append(f"YT: {item['youtube_url']}")
        if item.get("instagram_url"):
            details.append(f"IG: {item['instagram_url']}")
        if item.get("qa_skipped"):
            details.append(f"QA skipped: {item.get('error', 'failed QA gate')}")
        elif item.get("error"):
            details.append(f"Error: {item['error']}")
        warnings = item.get("qa_warnings") or []
        if warnings:
            details.append(f"QA warnings: {'; '.join(warnings)}")
        lines.append(f"- {label}" + (f" ({' | '.join(details)})" if details else ""))

    combined_warnings = list(dict.fromkeys([
        *(summary.get("token_warnings") or ()),
        *(w for w in token_warnings if w),
    ]))
    for warning in combined_warnings:
        lines.append(f"Token warning: {warning}")
    if run_url:
        lines.append(f"Run: {run_url}")
    return "\n".join(lines)


def send_discord(webhook_url, message):
    """Post a message to a Discord incoming webhook."""
    response = requests.post(webhook_url, json={"content": message[:1950]}, timeout=15)
    response.raise_for_status()


def send_telegram(bot_token, chat_id, message):
    """Send a message via the Telegram Bot sendMessage API."""
    response = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={"chat_id": chat_id, "text": message[:4000], "disable_web_page_preview": True},
        timeout=15,
    )
    response.raise_for_status()


def send_notifications(message, *, discord_url=None, telegram_token=None, telegram_chat_id=None):
    """Dispatch notifications to configured targets; exit silently if none are set."""
    webhook = (secret("DISCORD_WEBHOOK_URL") if discord_url is None else discord_url).strip()
    bot_token = (secret("TELEGRAM_BOT_TOKEN") if telegram_token is None else telegram_token).strip()
    chat_id = (secret("TELEGRAM_CHAT_ID") if telegram_chat_id is None else telegram_chat_id).strip()
    sent = []
    if webhook:
        try:
            send_discord(webhook, message)
            sent.append("discord")
        except requests.RequestException as error:
            print(f"Discord notification failed ({type(error).__name__}).", file=sys.stderr)
    if bot_token and chat_id:
        try:
            send_telegram(bot_token, chat_id, message)
            sent.append("telegram")
        except requests.RequestException as error:
            print(f"Telegram notification failed ({type(error).__name__}).", file=sys.stderr)
    return sent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", default="unknown")
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--run-url", default=os.getenv("GITHUB_RUN_URL", ""))
    args = parser.parse_args()

    token_warnings = []
    try:
        import instagram_publish

        warning = instagram_publish.check_token_expiry()
        if warning:
            token_warnings.append(warning)
    except Exception:
        pass

    summary = load_summary(args.summary)
    message = format_summary(
        summary,
        status=args.status,
        token_warnings=token_warnings,
        run_url=args.run_url,
    )
    sent = send_notifications(message)
    if sent:
        print(f"Sent run summary via {', '.join(sent)}.")


if __name__ == "__main__":
    main()
