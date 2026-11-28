"""Authenticate a single YouTube channel and resume uploads by job."""

from datetime import datetime, timezone
import hashlib
import json
import re
from urllib.parse import urlsplit

from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

API = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_token(path):
    from local_secrets import secret

    value = secret("YOUTUBE_TOKEN_JSON")
    return json.loads(value) if value else read_json(path)


def has_token(path):
    from local_secrets import secret

    return bool(secret("YOUTUBE_TOKEN_JSON")) or path.is_file()


def channel(session):
    response = session.get(f"{API}/channels", params={"mine": "true", "part": "snippet"}, timeout=20)
    require(response, (200,), "read the channel")
    items = response.json()["items"]
    if len(items) != 1:
        raise ValueError("Sign in with the Google account and channel you want to publish to.")
    return {"id": items[0]["id"], "title": items[0]["snippet"]["title"]}


def sign_in(client_path, token_path):
    flow = InstalledAppFlow.from_client_secrets_file(str(client_path), scopes=SCOPES)
    credentials = flow.run_local_server(port=0, prompt="consent", timeout_seconds=180)
    with AuthorizedSession(credentials) as session:
        selected = channel(session)
    from local_secrets import store_secret

    store_secret("YOUTUBE_TOKEN_JSON", json.dumps(
        {"channel": selected, "credentials": json.loads(credentials.to_json())}
    ))
    token_path.unlink(missing_ok=True)
    return selected


def require(response, allowed, action):
    if response.status_code not in allowed:
        try:
            details = response.json()["error"]
            reason = details.get("message", "") if isinstance(details, dict) else ""
        except (ValueError, KeyError):
            reason = ""
        raise ValueError(f"YouTube could not {action} (HTTP {response.status_code}). {reason}".strip())


def build_upload_body(meta, visibility, *, publish_at=None, now=None, check_future=True):
    """Build the YouTube Data API videos.insert snippet and status payload."""
    raw_title = " ".join(str(meta["title"]).split())
    if len(raw_title) > 100:
        raw_title = raw_title[:100].rsplit(" ", 1)[0].strip() or raw_title[:100]
    content = {
        "snippet": {"title": raw_title, "description": meta["description"], "categoryId": "28"},
        "status": {
            "privacyStatus": visibility,
            "selfDeclaredMadeForKids": bool(meta.get("made_for_kids", False)),
            "containsSyntheticMedia": bool(meta.get("synthetic_media", False)),
        },
    }
    if publish_at is not None:
        if visibility != "private" or publish_at.utcoffset() is None:
            raise ValueError("Scheduled uploads require private visibility and a time zone.")
        current = now or datetime.now(timezone.utc)
        if check_future and publish_at <= current:
            raise ValueError("Choose a future public release time.")
        content["status"]["publishAt"] = publish_at.isoformat()
    if meta.get("tags"):
        content["snippet"]["tags"] = meta["tags"]
    if meta.get("language"):
        content["snippet"]["defaultLanguage"] = meta["language"]
        content["snippet"]["defaultAudioLanguage"] = meta["language"]
    return content


def upload(job, token_path, visibility, execution_date, *, publish_at=None):
    path = job / "upload.json"
    record = read_json(path) if path.exists() else {}
    if record.get("video_id"):
        return record["video_id"]

    token = read_token(token_path)
    credentials = Credentials.from_authorized_user_info(token["credentials"], scopes=SCOPES)
    media = (job / "short.mp4").read_bytes()
    digest = hashlib.sha256(media).hexdigest()
    meta = read_json(job / "metadata.json")
    content = build_upload_body(meta, visibility, publish_at=publish_at, check_future=not record)
    expected_channel = token["channel"]["id"]
    if record and (record["sha256"], record["channel_id"], record["visibility"], record["content"]) != (
        digest, expected_channel, visibility, content
    ):
        raise ValueError("Resume with the original file, channel, visibility, and metadata.")

    with AuthorizedSession(credentials) as session:
        if channel(session)["id"] != expected_channel:
            raise ValueError("The current OAuth credentials resolve to a different YouTube channel.")
        if not record:
            response = session.post(
                UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
                headers={"X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(len(media))},
                json=content, timeout=30,
            )
            require(response, (200, 201), "start an upload")
            location = response.headers.get("Location", "")
            uri = urlsplit(location)
            if uri.scheme != "https" or uri.netloc not in {"www.googleapis.com", "youtube.googleapis.com"}:
                raise ValueError("YouTube returned an unexpected upload session URL.")
            record = {"session": location, "sha256": digest, "channel_id": expected_channel,
                      "visibility": visibility, "content": content}
        record["attempt_date"] = execution_date
        save_json(path, record)
        response = session.put(record["session"], headers={"Content-Range": f"bytes */{len(media)}"},
                               data=b"", timeout=30, allow_redirects=False)
        if response.status_code in (404, 410):
            raise ValueError("The upload session expired. Check YouTube Studio before clearing its local record.")
        require(response, (200, 201, 308), "check the upload session")
        if response.status_code == 308:
            received = response.headers.get("Range", "")
            match = re.fullmatch(r"bytes=0-(\d+)", received)
            if received and not match:
                raise ValueError("YouTube returned an unreadable upload offset.")
            start = int(match[1]) + 1 if match else 0
            if start >= len(media):
                raise ValueError("YouTube is still finalizing the upload. Retry this job later.")
            response = session.put(
                record["session"],
                headers={"Content-Range": f"bytes {start}-{len(media) - 1}/{len(media)}",
                         "Content-Type": "video/mp4"},
                data=media[start:], timeout=120, allow_redirects=False,
            )
            require(response, (200, 201), "finish the upload")
        record["video_id"] = response.json()["id"]
        save_json(path, record)
        return record["video_id"]
