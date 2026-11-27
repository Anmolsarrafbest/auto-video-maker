"""Accept a Spark idea and run the existing CPU render and publishing pipeline."""

import argparse
import json
import os
from pathlib import Path
import re
import sys

import shorts
import visuals
from storyboard import validate_visual_beats, visual_beat_text



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
    title, category = field("title", 95), field("category", 32)
    modern = "beats" in value
    hook, explanation = field("hook", 150), field("explanation", 1400 if modern else 320)
    source = field("source", 400)
    seconds = value.get("duration_seconds")
    max_seconds = 45 if modern else 25
    if type(seconds) is not int or not 15 <= seconds <= max_seconds:
        raise ValueError(f"duration_seconds must be a whole number from 15 to {max_seconds}.")
    if not re.fullmatch(r"[A-Za-z][A-Za-z &'-]{0,31}", category) or sum(len(line.split()) for line in (hook, explanation)) > int(seconds * 2.85):
        raise ValueError("Use a short topic category and narration that fits the runtime.")
    visuals.validate_reference(source)
    if modern:
        visual_beats = validate_visual_beats(value["beats"], [hook, explanation], seconds)
        icon = visuals.shot_search_fields({"icon_query": value.get("icon_query")}, title, [])["icon_query"]
        visual_plan = {"beats": visual_beats, "icon_query": icon}
        visual_text = [icon, *visual_beat_text(visual_beats)]
    else:
        shots = value.get("shots")
        if not isinstance(shots, list) or len(shots) != 3 or any(
            not isinstance(shot, str) or not 0 < len(shot.strip()) <= 360 or "\n" in shot or "\r" in shot
            for shot in shots
        ):
            raise ValueError("Supply three one-line shot descriptions.")
        search_fields = visuals.shot_search_fields(value, title, shots)
        visual_plan = {"shots": [shot.strip() for shot in shots], **search_fields}
        visual_text = [*shots, *search_fields["shot_queries"], search_fields["icon_query"]]
    labels = [field("opening_label", 38), field("payoff_label", 38)]
    real_event = value.get("real_event", False)
    if not isinstance(real_event, bool):
        raise ValueError("Provide real_event as true or false.")
    event_year = value.get("event_year", "")
    if event_year != "" and (not isinstance(event_year, str) or not re.fullmatch(r"[12]\d{3}", event_year)):
        raise ValueError("Provide event_year as a four-digit string.")
    if shorts.EXCLUDED_TOPICS.search(" ".join([category, title, hook, explanation, *labels, *visual_text])):
        raise ValueError("Political and religious material is outside the editorial scope.")
    topic = {"id": f"spark-{request_id}", "title": title, "category": category, "format": "story",
             "opening": "statement", "scene": "discovery", "color": "#91DFDA", "beats": [hook, explanation],
             "source": source, "aliases": [], "labels": labels,
             "discovery": {"trend": "", "real_event": real_event or bool(event_year),
                           "event_year": event_year, **visual_plan}}
    if value.get("media_url"):
        media_url = field("media_url", 450)
        visuals.validate_reference(media_url)
        topic["discovery"]["media"] = {"url": media_url, "relevance": field("media_relevance", 220)}
    return shorts.discovery_treatment(topic), seconds


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


def read_payload(path):
    if path:
        return path.read_text(encoding="utf-8-sig")
    if "SPARK_IDEA_JSON" in os.environ:
        return os.environ["SPARK_IDEA_JSON"]
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        return event["inputs"]["idea_json"]
    return ""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("validate", "draft", "youtube", "instagram", "both"), default="draft")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--offline", action="store_true", help="Render a draft using local assets only")
    parser.add_argument("--no-auto-visuals", action="store_true", help="Use cached visuals only")
    args = parser.parse_args()
    if args.offline and args.mode not in {"validate", "draft"}:
        parser.error("--offline supports validate and draft modes.")
    try:
        topic, seconds = parse_idea(read_payload(args.input))
        if args.mode == "validate":
            print(f"Valid Spark job: {topic['id']} ({seconds}s)")
            return
        shorts.STATE.mkdir(exist_ok=True)
        shorts.OUTPUT.mkdir(exist_ok=True)
        with shorts.FileLock(shorts.STATE / "worker.lock", timeout=0):
            if any(job.name == topic["id"] for job in shorts.completed_jobs()):
                raise ValueError(f"A draft already exists for {topic['id']}.")
            job = shorts.new_output_batch() / topic["id"]
            if not (args.offline or args.no_auto_visuals):
                visuals.cache_storyboard(topic)
            shorts.render_job(topic, "Spark idea", seconds, job)
            if args.mode != "draft":
                publish(job, topic, args.mode)
    except (ValueError, KeyError, OSError, shorts.requests.RequestException, shorts.Timeout) as error:
        message = str(error) if isinstance(error, ValueError) else type(error).__name__
        sys.exit(f"Spark job stopped: {message}")


if __name__ == "__main__":
    main()
