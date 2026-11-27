"""Prepare approved Reels and publish them locally through Meta's official API."""

import argparse
from datetime import datetime, timezone
from getpass import getpass
import re
import sys
import time

import queue_publish
import shorts
from local_secrets import secret, store_secret

CONFIG = shorts.ROOT / "secrets" / "instagram.json"
GRAPH = "https://graph.facebook.com/v27.0"
UPLOAD = "https://rupload.facebook.com/ig-api-upload/v27.0"


def request(session, method, url, **kwargs):
    response = session.request(method, url, timeout=45, allow_redirects=False, **kwargs)
    if not 200 <= response.status_code < 300:
        code = None
        subcode = None
        detail = ""
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            debug = payload.get("debug_info") if isinstance(payload.get("debug_info"), dict) else {}
            code = error.get("code")
            subcode = error.get("error_subcode")
            raw_detail = error.get("error_user_msg") or error.get("message") or debug.get("message") or payload.get("message") or ""
            detail = " ".join(str(raw_detail).split())[:240]
        if code == 190:
            raise ValueError("Instagram Page token expired or is invalid (Meta code 190). Run instagram_publish.py connect again.")
        parts = [f"HTTP {response.status_code}", f"API code {code}"]
        if subcode is not None:
            parts.append(f"subcode {subcode}")
        suffix = f": {detail}" if detail else ""
        raise ValueError(f"Meta request failed ({', '.join(parts)}{suffix}).")
    return response.json()


def account(session, account_id):
    return request(session, "GET", f"{GRAPH}/{account_id}", params={"fields": "id,username"})


def caption(job):
    meta = shorts.read_json(job / "metadata.json")
    description = meta["description"]
    lines = description.splitlines()
    tags = []
    if lines and lines[-1].startswith("#"):
        tags = lines.pop().split()
    discovery = meta.get("discovery", {})
    matched = discovery.get("trend", "")
    trend_tag = "#" + re.sub(r"[^\w]", "", matched.title()) if matched else ""
    candidates = [trend_tag, *discovery.get("instagram_tags", ()), *tags]
    selected = {}
    for tag in candidates:
        if re.fullmatch(r"#[\w]{2,45}", tag) and tag.casefold() != "#shorts":
            selected.setdefault(tag.casefold(), tag)
    suffix = " ".join(list(selected.values())[:5])
    result = meta["title"] + "\n\n" + "\n".join(lines).rstrip()
    if suffix:
        result += "\n\n" + suffix
    if len(result) > 2200:
        raise ValueError("Shorten the draft's description to fit the Instagram caption.")
    return result


def publish(session, config, job, record, day):
    path = job / "instagram.json"
    if record["account_id"] != config["account_id"] or record["review"] != queue_publish.review_signature(job):
        raise ValueError("Instagram approval no longer matches the account or the reviewed draft.")
    if record.get("publish_started"):
        raise ValueError("An earlier Instagram publish may have succeeded. Check the profile before retrying.")
    if not record.get("container"):
        result = request(session, "POST", f"{GRAPH}/{config['account_id']}/media",
                         data={"media_type": "REELS", "upload_type": "resumable", "caption": caption(job)})
        if not re.fullmatch(r"\d+", result["id"]):
            raise ValueError("Meta returned an invalid container ID.")
        record["container"] = result["id"]
        shorts.save_json(path, record)
    container = record["container"]
    if not record.get("media_sent"):
        video = job / "short.mp4"
        file_size = str(video.stat().st_size)
        last_error = None
        for attempt in range(3):
            try:
                with video.open("rb") as handle:
                    result = request(session, "POST", f"{UPLOAD}/{container}", data=handle, headers={
                        "Authorization": f"OAuth {config['token']}", "offset": "0",
                        "file_size": file_size, "Content-Type": "application/octet-stream",
                    })
                break
            except (ValueError, shorts.requests.RequestException) as error:
                last_error = error
                if attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))
        if result.get("success") is not True:
            raise ValueError("Meta did not confirm the media transfer.")
        record["media_sent"] = True
        shorts.save_json(path, record)
    for _ in range(8):
        status = request(session, "GET", f"{GRAPH}/{container}", params={"fields": "status_code"})["status_code"]
        if status == "FINISHED":
            break
        if status != "IN_PROGRESS":
            raise ValueError(f"Instagram container status is {status}. Review it before trying again.")
        time.sleep(5)
    else:
        print("Instagram is still processing. Run publish-next again later to resume this reel.")
        return
    record.update(publish_started=True, publish_day=day)
    shorts.save_json(path, record)
    result = request(session, "POST", f"{GRAPH}/{config['account_id']}/media_publish",
                     data={"creation_id": container})
    record["media_id"] = result["id"]
    instagram_url = ""
    try:
        permalink = request(session, "GET", f"{GRAPH}/{result['id']}", params={"fields": "permalink"}).get("permalink", "")
        if isinstance(permalink, str) and permalink.startswith("https://"):
            instagram_url = permalink
            record["permalink"] = permalink
    except (ValueError, shorts.requests.RequestException):
        pass
    if not instagram_url and config.get("username"):
        instagram_url = f"https://www.instagram.com/{config['username']}/"
    shorts.save_json(path, record)
    shorts.record_published_history(job, instagram_url=instagram_url)
    print(f"Instagram Reel published. Media ID {result['id']}.")


def publish_batch(jobs, platform="both"):
    day = shorts.today("Asia/Kolkata")
    failures = []
    if platform in {"both", "youtube"}:
        try:
            if not shorts.has_token(shorts.TOKEN):
                raise ValueError("Connect your YouTube channel first.")
            occupied = sum(shorts.read_json(path).get("attempt_date") == day
                           for path in shorts.OUTPUT.rglob("upload.json"))
            for job in jobs:
                print(f"Publishing {job.name} on YouTube.", flush=True)
                shorts.publish(job, "public", day, occupied + len(jobs), allow_reviewed=True)
        except (ValueError, shorts.RefreshError, shorts.requests.RequestException) as error:
            detail = str(error) if isinstance(error, ValueError) else type(error).__name__
            failures.append(f"YouTube: {detail}")
    if platform in {"both", "instagram"}:
        try:
            publish_reels(jobs, day)
        except (ValueError, shorts.requests.RequestException) as error:
            detail = str(error) if isinstance(error, ValueError) else type(error).__name__
            failures.append(f"Instagram: {detail}")
    if failures:
        raise ValueError("\n".join(failures))


def resolve_config(session, token):
    if CONFIG.is_file():
        config = shorts.read_json(CONFIG)
        profile = account(session, config["account_id"])
        if profile["id"] != config["account_id"] or profile["username"].casefold() != config["username"].casefold():
            raise ValueError("The Instagram token resolved to a different account.")
        return {"account_id": config["account_id"], "username": config["username"], "token": token}
    account_id = secret("SPARK_INSTAGRAM_ACCOUNT_ID").strip()
    username = secret("SPARK_INSTAGRAM_USERNAME").strip()
    if account_id:
        if not re.fullmatch(r"\d+", account_id):
            raise ValueError("Set a numeric Instagram professional account ID.")
        profile = account(session, account_id)
        if username and profile["username"].casefold() != username.casefold():
            raise ValueError("The configured Instagram profile and Page token must match.")
        return {"account_id": profile["id"], "username": profile["username"], "token": token}
    page = request(session, "GET", f"{GRAPH}/me", params={"fields": "instagram_business_account{id,username}"})
    linked = page.get("instagram_business_account") or {}
    if not linked.get("id") or not linked.get("username"):
        raise ValueError("Connect your Instagram Creator account or set SPARK_INSTAGRAM_ACCOUNT_ID.")
    return {"account_id": str(linked["id"]), "username": linked["username"], "token": token}


def publish_reels(jobs, day):
    token = (secret("INSTAGRAM_PAGE_ACCESS_TOKEN") or (shorts.read_json(CONFIG).get("token") if CONFIG.is_file() else "")).strip()
    if not token:
        raise ValueError("The Instagram Page token is missing.")
    for job in jobs:
        caption(job)
    with shorts.requests.Session() as session:
        session.headers["Authorization"] = f"Bearer {token}"
        config = resolve_config(session, token)
        failures = []
        for index, job in enumerate(jobs):
            print(f"Publishing {job.name} on Instagram.", flush=True)
            path = job / "instagram.json"
            record = shorts.read_json(path) if path.is_file() else {
                "account_id": config["account_id"], "review": queue_publish.review_signature(job)
            }
            if record.get("media_id"):
                print(f"Instagram already published. Media ID {record['media_id']}.")
                continue
            try:
                for _ in range(3):
                    publish(session, config, job, record, day)
                    if record.get("media_id"):
                        break
                else:
                    print(f"Instagram is still processing {job.name}. Resume using publish-both.")
            except (ValueError, shorts.requests.RequestException) as error:
                detail = str(error) if isinstance(error, ValueError) else type(error).__name__
                print(f"Instagram failed for {job.name}: {detail}", file=sys.stderr)
                failures.append(f"{job.name}: {detail}")
            if index + 1 < len(jobs):
                time.sleep(4)
        if failures:
            raise ValueError("; ".join(failures))


def _parse_expiry_date(raw):
    text = raw.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def check_token_expiry(token=None, *, warn_days=7, now=None):
    """Return a warning string if the Instagram token expires within warn_days, else None."""
    current = now or datetime.now(timezone.utc)
    expires_at = None
    configured = secret("INSTAGRAM_TOKEN_EXPIRES_AT").strip()
    if configured:
        try:
            expires_at = _parse_expiry_date(configured)
        except ValueError:
            return f"INSTAGRAM_TOKEN_EXPIRES_AT has an invalid date format: {configured!r}."
    else:
        resolved_token = (token if token is not None else secret("INSTAGRAM_PAGE_ACCESS_TOKEN")).strip()
        if not resolved_token and CONFIG.is_file():
            resolved_token = str(shorts.read_json(CONFIG).get("token", "")).strip()
        if not resolved_token:
            return None
        try:
            response = shorts.requests.get(
                f"{GRAPH}/debug_token",
                params={"input_token": resolved_token, "access_token": resolved_token},
                timeout=15,
            )
            if response.ok:
                data = response.json().get("data") or {}
                for field in ("expires_at", "data_access_expires_at"):
                    timestamp = data.get(field)
                    if isinstance(timestamp, (int, float)) and timestamp > 0:
                        expires_at = datetime.fromtimestamp(timestamp, tz=timezone.utc)
                        break
        except (ValueError, OSError, shorts.requests.RequestException):
            return None
    if expires_at is None:
        return None
    seconds_left = (expires_at - current).total_seconds()
    expiry_label = expires_at.date().isoformat()
    if seconds_left <= 0:
        return f"Instagram token expired on {expiry_label}. Refresh INSTAGRAM_PAGE_ACCESS_TOKEN."
    days_left = max(0, int(seconds_left // 86400))
    if seconds_left <= warn_days * 86400:
        return (
            f"Instagram token expires in {days_left} day(s) ({expiry_label}). "
            "Refresh INSTAGRAM_PAGE_ACCESS_TOKEN."
        )
    return None


def main():
    shorts.configure_console()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    connect = sub.add_parser("connect")
    connect.add_argument("--account-id", required=True)
    connect.add_argument("--username", required=True)
    for command in ("preview", "approve"):
        sub.add_parser(command).add_argument("job")
    sub.add_parser("publish-next")
    expiry = sub.add_parser("check-token", help="Warn if the Instagram token is within 7 days of expiry")
    expiry.add_argument("--warn-days", type=int, default=7)
    args = parser.parse_args()
    if args.command == "check-token":
        warning = check_token_expiry(warn_days=args.warn_days)
        if warning:
            print(f"WARNING: {warning}", file=sys.stderr)
        else:
            print("Instagram token expiry check OK.")
        return
    shorts.STATE.mkdir(exist_ok=True)
    try:
        with shorts.FileLock(shorts.STATE / "worker.lock", timeout=0), shorts.requests.Session() as session:
            if args.command == "preview":
                job = next((job for job in queue_publish.jobs() if job.name == args.job), None)
                if job is None:
                    raise ValueError("Choose a draft from queue_publish.py status.")
                print(job / "short.mp4")
                print(caption(job))
                return
            if args.command == "connect":
                if not re.fullmatch(r"\d+", args.account_id):
                    raise ValueError("Use the numeric Instagram professional account ID.")
                token = getpass("Facebook Page access token: ").strip()
                session.headers["Authorization"] = f"Bearer {token}"
                profile = account(session, args.account_id)
                if (profile["id"] != args.account_id or
                        profile["username"].lower() != args.username.removeprefix("@").lower()):
                    raise ValueError("The access token resolved to a different Instagram profile.")
                store_secret("INSTAGRAM_PAGE_ACCESS_TOKEN", token)
                shorts.save_json(CONFIG, {"account_id": profile["id"], "username": profile["username"]})
                print(f"Connected @{profile['username']}.")
                return
            if not CONFIG.is_file():
                raise ValueError("Connect your professional Instagram profile first. See README.md.")
            config = shorts.read_json(CONFIG)
            entries = [(job, shorts.read_json(job / "instagram.json"))
                       for job in queue_publish.jobs() if (job / "instagram.json").is_file()]
            if args.command == "approve":
                job = next((job for job in queue_publish.jobs() if job.name == args.job), None)
                if job is None or not queue_publish.approval_matches(job):
                    raise ValueError("Review the video in the local queue before approving for Instagram.")
                record = dict(entries).get(job)
                approval = {"account_id": config["account_id"], "review": queue_publish.review_signature(job)}
                if record and "container" in record:
                    raise ValueError("This draft already has an Instagram upload record.")
                shorts.save_json(job / "instagram.json", approval)
                print(f"Approved {job.name} for @{config['username']}.")
                return
            pending = [(job, record) for job, record in entries if record["account_id"] == config["account_id"]]
            day = shorts.today("Asia/Kolkata")
            if any(record.get("publish_day") == day for _, record in pending):
                print("Today's Instagram publication slot has already been used.")
                return
            job, record = next(((job, record) for job, record in pending if not record.get("media_id")), (None, None))
            if job is None:
                print("There are no approved Reels waiting for Instagram.")
                return
            config["token"] = secret("INSTAGRAM_PAGE_ACCESS_TOKEN") or config.get("token")
            if not config["token"]:
                raise ValueError("Instagram Page token is missing. Run connect again.")
            session.headers["Authorization"] = f"Bearer {config['token']}"
            profile = account(session, config["account_id"])
            if profile["id"] != config["account_id"] or profile["username"].lower() != config["username"].lower():
                raise ValueError("Meta returned a different profile. Reconnect the intended account.")
            publish(session, config, job, record, day)
    except shorts.Timeout:
        sys.exit("Another publishing command is running.")
    except shorts.requests.RequestException:
        sys.exit("The Meta network request failed. Check the profile before retrying a publish.")
    except (ValueError, KeyError, OSError) as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()
