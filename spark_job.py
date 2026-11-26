"""Accept a Spark idea and run the existing CPU render and publishing pipeline."""

import argparse
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

import shorts

SOURCE_DOMAINS = ("nasa.gov", "noaa.gov", "usgs.gov", "si.edu")
CATEGORIES = {"Space", "Physics", "Ocean", "History"}
EXCLUDED = re.compile(r"\b(politics|political|election|religion|religious|worship|campaign rally)\b", re.I)


def parse_idea(raw):
    if len(raw.encode("utf-8")) > 8192:
        raise ValueError("Keep the Spark idea under 8 KB.")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Send one JSON object per idea.")

    def field(name, limit):
        item = value.get(name)
        if not isinstance(item, str) or not 0 < len(item.strip()) <= limit or "\n" in item or "\r" in item:
            raise ValueError(f"Provide {name} as one line of at most {limit} characters.")
        return item.strip()

    request_id = field("request_id", 60)
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", request_id):
        raise ValueError("Use a lowercase, hyphenated request_id.")
    title, category = field("title", 95), field("category", 12)
    hook, explanation = field("hook", 150), field("explanation", 320)
    source = field("source", 400)
    seconds = value.get("duration_seconds")
    if type(seconds) is not int or not 15 <= seconds <= 25:
        raise ValueError("duration_seconds must be a whole number from 15 to 25.")
    if category not in CATEGORIES or sum(len(line.split()) for line in (hook, explanation)) > int(seconds * 2.3):
        raise ValueError("Use a supported science category and narration that fits the runtime.")
    url = urlsplit(source)
    host = url.hostname or ""
    if (url.scheme != "https" or url.username or url.password or url.port or not url.path.strip("/")
            or not any(host == domain or host.endswith("." + domain) for domain in SOURCE_DOMAINS)):
        raise ValueError("Use a direct HTTPS article from NASA, NOAA, USGS, or Smithsonian.")
    shots = value.get("shots")
    if not isinstance(shots, list) or len(shots) != 3 or any(
        not isinstance(shot, str) or not 0 < len(shot.strip()) <= 360 or "\n" in shot or "\r" in shot
        for shot in shots
    ):
        raise ValueError("Supply three one-line shot descriptions.")
    labels = [field("opening_label", 38), field("payoff_label", 38)]
    if EXCLUDED.search(" ".join([title, hook, explanation, *shots, *labels])):
        raise ValueError("Political and religious material is outside the editorial scope.")
    topic = {"id": f"spark-{request_id}", "title": title, "category": category, "format": "story",
             "opening": "statement", "scene": "discovery", "color": "#91DFDA", "beats": [hook, explanation],
             "source": source, "aliases": [], "labels": labels,
             "discovery": {"trend": "", "shots": [shot.strip() for shot in shots]}}
    return topic, seconds


def claim_publication(topic_id):
    # The ref is created atomically before sending anything to either platform.
    repo, commit, token = (os.environ[name] for name in ("GITHUB_REPOSITORY", "GITHUB_SHA", "GH_TOKEN"))
    response = shorts.requests.post(
        f"https://api.github.com/repos/{repo}/git/refs",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        json={"ref": f"refs/tags/{topic_id}", "sha": commit}, timeout=20,
    )
    if response.status_code != 201:
        raise ValueError(f"Could not reserve the publish ID (HTTP {response.status_code}). "
                         "Check existing uploads before retrying with another ID.")


def publish(job, topic, destinations):
    import instagram_publish as ig
    import queue_publish

    youtube_enabled = destinations in {"youtube", "both"}
    instagram_enabled = destinations in {"instagram", "both"}
    token = shorts.read_token(shorts.TOKEN) if youtube_enabled else None
    if youtube_enabled and token["channel"]["id"] != os.environ.get("SPARK_YOUTUBE_CHANNEL_ID"):
        raise ValueError("The configured YouTube channel and OAuth account must match.")
    day = shorts.today("Asia/Kolkata")
    with shorts.requests.Session() as session:
        if instagram_enabled:
            account_id = os.environ["SPARK_INSTAGRAM_ACCOUNT_ID"]
            if not re.fullmatch(r"\d+", account_id):
                raise ValueError("Set a numeric Instagram professional account ID.")
            access_token = shorts.secret("INSTAGRAM_PAGE_ACCESS_TOKEN")
            session.headers["Authorization"] = f"Bearer {access_token}"
            profile = ig.account(session, account_id)
            if profile["username"].casefold() != os.environ["SPARK_INSTAGRAM_USERNAME"].casefold():
                raise ValueError("The configured Instagram profile and Page token must match.")
            config = {"account_id": account_id, "token": access_token}
            approval = {"account_id": account_id, "review": queue_publish.review_signature(job)}
            ig.caption(job)
        claim_publication(topic["id"])
        if youtube_enabled:
            shorts.publish(job, "public", day, 1, allow_reviewed=True)
        if instagram_enabled:
            for _ in range(3):
                ig.publish(session, config, job, approval, day)
                if approval.get("media_id"):
                    return
            raise ValueError("Instagram is still processing. Check its saved container before trying again.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("validate", "draft", "youtube", "instagram", "both"), default="draft")
    parser.add_argument("--input", type=Path)
    args = parser.parse_args()
    raw = args.input.read_text(encoding="utf-8-sig") if args.input else os.environ.get("SPARK_IDEA_JSON", "")
    try:
        topic, seconds = parse_idea(raw)
        if args.mode == "validate":
            print(f"Valid Spark job: {topic['id']} ({seconds}s)")
            return
        with shorts.requests.get(topic["source"], timeout=20, stream=True, allow_redirects=False) as response:
            if response.status_code != 200:
                raise ValueError(f"The source link needs checking (HTTP {response.status_code}).")
        shorts.STATE.mkdir(exist_ok=True)
        shorts.OUTPUT.mkdir(exist_ok=True)
        job = shorts.OUTPUT / topic["id"]
        with shorts.FileLock(shorts.STATE / "worker.lock", timeout=0):
            shorts.render_job(topic, "Spark idea", seconds, job)
            if args.mode != "draft":
                publish(job, topic, args.mode)
    except (ValueError, KeyError, OSError, shorts.requests.RequestException, shorts.Timeout) as error:
        message = str(error) if isinstance(error, ValueError) else type(error).__name__
        sys.exit(f"Spark job stopped: {message}")


if __name__ == "__main__":
    main()
