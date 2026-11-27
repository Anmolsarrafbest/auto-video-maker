"""Send zero-cost ntfy.sh, Discord, and Telegram run notifications when configured."""

import argparse
import json
import os
from pathlib import Path
import re
import sys

import requests

from local_secrets import secret

DEFAULT_SUMMARY = Path(__file__).resolve().parent / "state" / "run_summary.json"


def _ascii_header(value):
    cleaned = re.sub(r"[\r\n]+", " ", str(value or "")).strip()
    return cleaned.encode("ascii", "ignore").decode("ascii")


def default_run_url(run_url=""):
    """Resolve the GitHub Actions run URL from argument or workflow environment."""
    if run_url:
        return run_url.strip()
    env_url = os.getenv("GITHUB_RUN_URL", "").strip()
    if env_url:
        return env_url
    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    run_id = os.getenv("GITHUB_RUN_ID", "").strip()
    if repo and run_id:
        server = os.getenv("GITHUB_SERVER_URL", "https://github.com").strip().rstrip("/")
        return f"{server}/{repo}/actions/runs/{run_id}"
    return ""


def load_summary(path):
    """Read an optional run summary JSON file without failing if missing."""
    if not path or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_summary_input(raw_summary):
    if raw_summary is None:
        return load_summary(DEFAULT_SUMMARY), ""
    candidate = Path(raw_summary)
    if candidate.is_file():
        return load_summary(candidate), ""
    if str(raw_summary).strip().lower().endswith(".json"):
        return {}, ""
    return {}, str(raw_summary).strip()


def format_summary(summary, *, status="unknown", token_warnings=(), run_url="", summary_text=""):
    """Build a concise run summary string for mobile push, Discord, and Telegram."""
    attempted = int(summary.get("attempted", 0) or 0)
    published = int(summary.get("published", 0) or 0)
    rendered = int(summary.get("completed_renders", 0) or 0)
    lines = [f"Auto Video Maker ({status.upper()})"]
    if summary:
        lines.append(f"Attempted: {attempted} | Rendered: {rendered} | Published: {published}")
    elif summary_text:
        lines.append(summary_text)
    else:
        lines.append("Run completed without a draft summary file.")

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
    resolved_url = default_run_url(run_url)
    if resolved_url:
        lines.append(f"Run: {resolved_url}")
    return "\n".join(lines)


def build_ntfy_payload(summary, *, status="unknown", token_warnings=(), run_url="", summary_text=""):
    """Build ntfy.sh title, priority, tags, action button, and body for a run."""
    items = summary.get("items") or []
    published = int(summary.get("published", 0) or 0)
    rendered = int(summary.get("completed_renders", 0) or 0)
    combined_warnings = list(dict.fromkeys([
        *(summary.get("token_warnings") or ()),
        *(w for w in token_warnings if w),
    ]))
    has_errors = any(item.get("error") or item.get("qa_skipped") for item in items)
    is_failure = status.strip().lower() != "success" or has_errors
    urgent = is_failure or bool(combined_warnings)

    resolved_run_url = default_run_url(run_url)
    first_upload_url = next(
        (
            url for item in items
            for url in (item.get("youtube_url", ""), item.get("instagram_url", ""))
            if isinstance(url, str) and url.startswith("https://")
        ),
        "",
    )

    if urgent:
        priority = "5"
        tags = "rotating_light,warning"
        title = "Auto Video Maker: Run Failed" if is_failure else "Action Needed: Token Expiring"
        action_url = resolved_run_url or first_upload_url
        action_label = "Open Run Logs" if resolved_run_url else "Open Video"
    else:
        priority = "3"
        tags = "white_check_mark,clapper"
        if summary.get("platform") == "draft" and published == 0:
            title = f"Auto Video Maker: {rendered} draft(s) rendered"
        else:
            title = f"Auto Video Maker: {published} video(s) uploaded"
        action_url = first_upload_url or resolved_run_url
        action_label = "Open Video" if first_upload_url else "Open Run Logs"

    actions = f"view, {action_label}, {action_url}, clear=true" if action_url else ""
    body = format_summary(
        summary,
        status=status,
        token_warnings=combined_warnings,
        run_url=resolved_run_url,
        summary_text=summary_text,
    )
    return {
        "title": title,
        "priority": priority,
        "tags": tags,
        "actions": actions,
        "body": body,
    }


def send_ntfy(topic, body, *, title="", priority="3", tags="", actions=""):
    """Post a push notification to https://ntfy.sh/<topic>."""
    clean_topic = topic.strip().lstrip("/")
    headers = {"Priority": _ascii_header(priority or "3")}
    if title:
        headers["Title"] = _ascii_header(title)
    if tags:
        headers["Tags"] = _ascii_header(tags)
    if actions:
        headers["Actions"] = _ascii_header(actions)
    response = requests.post(
        f"https://ntfy.sh/{clean_topic}",
        data=body.encode("utf-8"),
        headers=headers,
        timeout=15,
    )
    response.raise_for_status()


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


def send_notifications(message, *, discord_url=None, telegram_token=None, telegram_chat_id=None,
                       ntfy_topic=None, ntfy_payload=None):
    """Dispatch notifications to configured targets; exit silently if none are set."""
    topic = (secret("NTFY_TOPIC") if ntfy_topic is None else ntfy_topic).strip()
    webhook = (secret("DISCORD_WEBHOOK_URL") if discord_url is None else discord_url).strip()
    bot_token = (secret("TELEGRAM_BOT_TOKEN") if telegram_token is None else telegram_token).strip()
    chat_id = (secret("TELEGRAM_CHAT_ID") if telegram_chat_id is None else telegram_chat_id).strip()
    sent = []
    if topic:
        payload = ntfy_payload or {"body": message, "title": "Auto Video Maker", "priority": "3", "tags": "", "actions": ""}
        try:
            send_ntfy(
                topic,
                payload.get("body", message),
                title=payload.get("title", ""),
                priority=payload.get("priority", "3"),
                tags=payload.get("tags", ""),
                actions=payload.get("actions", ""),
            )
            sent.append("ntfy")
        except requests.RequestException as error:
            print(f"ntfy notification failed ({type(error).__name__}).", file=sys.stderr)
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


def notify_run(*, status="unknown", summary=None, token_warnings=(), run_url="", summary_text=""):
    """Collect token warnings, format run summary, and dispatch configured notifications."""
    collected_warnings = list(token_warnings)
    if not collected_warnings:
        try:
            import instagram_publish

            warning = instagram_publish.check_token_expiry()
            if warning:
                collected_warnings.append(warning)
        except Exception:
            pass
    summary_data = summary if isinstance(summary, dict) else {}
    ntfy_payload = build_ntfy_payload(
        summary_data,
        status=status,
        token_warnings=collected_warnings,
        run_url=run_url,
        summary_text=summary_text,
    )
    return send_notifications(ntfy_payload["body"], ntfy_payload=ntfy_payload)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", default="unknown")
    parser.add_argument("--summary", default=None, help="Path to summary JSON or literal summary text")
    parser.add_argument("--url", "--run-url", dest="run_url", default="", help="Video or GitHub Actions run URL")
    args = parser.parse_args()

    summary, summary_text = _resolve_summary_input(args.summary)
    sent = notify_run(
        status=args.status,
        summary=summary,
        run_url=args.run_url,
        summary_text=summary_text,
    )
    if sent:
        print(f"Sent run summary via {', '.join(sent)}.")


if __name__ == "__main__":
    main()
