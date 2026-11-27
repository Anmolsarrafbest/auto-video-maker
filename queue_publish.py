"""Prepare, review, and publish distinct Shorts using the local machine."""

import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4

import shorts
from storyboard import DURATIONS

PROGRESS = shorts.ROOT / "pilot-progress.json"
PREFIX = "queue-"


def jobs():
    return sorted(
        (job for job in shorts.completed_jobs() if job.name.startswith(PREFIX)),
        key=lambda job: ((job / "metadata.json").stat().st_mtime_ns, job.name),
    )


def review_signature(job):
    return {
        name: hashlib.sha256((job / name).read_bytes()).hexdigest()
        for name in ("short.mp4", "metadata.json")
    }


def inspect_draft(video, metadata):
    """Measure the encoded audio and report review warnings without blocking release."""
    import imageio_ffmpeg

    warnings = []
    for beat in metadata.get("visual_timeline", []):
        duration = beat["end_time"] - beat["start_time"]
        if duration > 5:
            warnings.append(f'Visual beat {beat["beat_id"]} holds for {duration:.2f}s (limit 5.0s).')
    probe = [os.getenv("SHORTS_FFMPEG") or imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner",
             "-i", str(video), "-map", "0:a:0", "-vn", "-af", "ebur128=framelog=verbose", "-f", "null", "-"]
    try:
        report = subprocess.run(probe, capture_output=True, text=True, errors="replace",
                                check=True, timeout=45).stderr
    except (OSError, subprocess.SubprocessError):
        warnings.append("FFmpeg could not measure runtime and loudness.")
        return {"duration_seconds": None, "integrated_lufs": None, "warnings": warnings}
    duration_match = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", report)
    actual = None
    if duration_match:
        hours, minutes, seconds = map(float, duration_match.groups())
        actual = round(hours * 3600 + minutes * 60 + seconds, 3)
        target = metadata.get("target_duration_seconds")
        if target and abs(actual - target) / target > .25:
            warnings.append(f"Exported runtime {actual:g}s differs from the {target:g}s target by more than 25%.")
    else:
        warnings.append("FFmpeg did not report the exported runtime.")
    loudness = re.findall(r"^\s*I:\s*(-?[\d.]+)\s+LUFS", report, re.MULTILINE)
    integrated = float(loudness[-1]) if loudness else None
    if integrated is None:
        warnings.append("FFmpeg did not report integrated loudness.")
    elif not -16 <= integrated <= -12:
        warnings.append(f"Integrated loudness is {integrated:g} LUFS (review range -16 to -12 LUFS).")
    return {"duration_seconds": actual, "integrated_lufs": integrated, "warnings": warnings}


def qa_gate(job):
    """Evaluate hard QA failures that must block unattended publishing."""
    video = job / "short.mp4"
    meta_path = job / "metadata.json"
    if not video.is_file() or video.stat().st_size == 0:
        return {"passed": False, "errors": ["Rendered video file is missing or empty."], "warnings": []}
    if not meta_path.is_file():
        return {"passed": False, "errors": ["Draft metadata.json is missing."], "warnings": []}
    try:
        meta = shorts.read_json(meta_path)
    except (OSError, ValueError):
        return {"passed": False, "errors": ["Draft metadata.json is unreadable."], "warnings": []}
    qa = meta.get("qa") if isinstance(meta.get("qa"), dict) else inspect_draft(video, meta)
    warnings = list(qa.get("warnings") or [])
    errors = []
    duration = qa.get("duration_seconds")
    if duration is None or duration <= 0:
        errors.append("FFmpeg did not measure a valid video duration.")
    if qa.get("integrated_lufs") is None:
        errors.append("FFmpeg did not measure integrated audio loudness.")
    for warning in warnings:
        if warning.startswith("Visual beat "):
            errors.append(warning)
    return {"passed": not errors, "errors": errors, "warnings": warnings}


def approval_matches(job):
    review = job / "review.json"
    if not review.is_file():
        return False
    meta = shorts.read_json(job / "metadata.json")
    production = meta.get("production", {})
    if meta.get("language") == "hi" and production.get("voice") != "Recorded Hindi narration":
        return False
    return production.get("production_revision") == 2 and shorts.read_json(review) == review_signature(job)


def prepare(args, topics):
    if args.language in shorts.PAUSED_LANGUAGES:
        raise ValueError("Hindi production is paused. Choose an English topic for now.")
    if PROGRESS.is_file():
        remote_topics = set(shorts.read_json(PROGRESS))
    else:
        remote_topics = set()
    used = remote_topics | {
        shorts.read_json(job / "metadata.json")["topic_id"] for job in shorts.completed_jobs()
        if job.name.startswith(PREFIX) or (job / "upload.json").is_file()
    }
    available = [topic for topic in topics if topic["id"] not in used
                 and topic.get("language") not in shorts.PAUSED_LANGUAGES
                 and (args.language is None or topic.get("language", "en") == args.language)]
    if args.topic:
        selected = [topic for topic_id in dict.fromkeys(args.topic)
                    for topic in available if topic["id"] == topic_id]
        missing = set(args.topic) - {topic["id"] for topic in selected}
        if missing:
            raise ValueError(f"Unknown or already drafted: {', '.join(sorted(missing))}")
    else:
        styles = ("story", "demo", "myth", "quiz")
        by_style = [[topic for topic in available if topic.get("format", "quiz") == style]
                    for style in styles]
        selected = [group[turn] for turn in range(len(available))
                    for group in by_style if turn < len(group)][:args.count]
    if not selected:
        print("No unused sourced topics are available.")
        return
    trends = {} if args.offline else {region: shorts.fetch_trends(region) for region in ("IN", "US")}
    batch = shorts.new_output_batch()
    for topic in selected:
        if getattr(args, "auto_visuals", False) and not args.offline:
            import visuals
            if ("archive" not in topic and topic["id"] not in shorts.CLIPS
                    and visuals.selected(topic["id"]) is None):
                providers = [name for name in ("pexels", "pixabay", "nasa")
                             if name == "nasa" or shorts.secret(name.upper() + "_API_KEY")]
                try:
                    cached = visuals.cache_best(topic["id"], topic["title"], providers, False)
                    shorts.save_json(visuals.CACHE / f"{topic['id']}.json", cached)
                except (shorts.requests.RequestException, OSError, ValueError) as error:
                    print(f"Auto visual skipped for {topic['id']} ({type(error).__name__}).", file=sys.stderr)
        job = batch / f"{PREFIX}{topic['id']}"
        shorts.render_job(topic, "Review queue", args.seconds, job, trends=trends)
        print(f"Awaiting review: {job.name}")


def refresh(args, topics):
    catalogue = {topic["id"]: topic for topic in topics}
    pending = []
    for job in jobs():
        if (job / "upload.json").is_file():
            continue
        meta = shorts.read_json(job / "metadata.json")
        topic = catalogue.get(meta["topic_id"])
        if (topic and topic.get("language") not in shorts.PAUSED_LANGUAGES
                and meta.get("production") != shorts.render_treatment(topic)):
            pending.append((job, meta, topic))
    if not pending:
        print("No drafts need the new production treatments.")
        return
    trends = {} if args.offline else {region: shorts.fetch_trends(region) for region in ("IN", "US")}
    archive = shorts.STATE / "previous-renders"
    archive.mkdir(exist_ok=True)
    for job, meta, topic in pending:
        backup = archive / job.name
        if backup.exists():
            backup = archive / f"{job.name}-{uuid4().hex[:8]}"
        with TemporaryDirectory(dir=shorts.STATE, prefix="refresh-") as directory:
            staged = Path(directory) / job.name
            shorts.render_job(topic, "Refreshed review draft", None, staged, trends=trends)
            job.rename(backup)
            try:
                staged.rename(job)
            except OSError:
                backup.rename(job)
                raise
        print(f"Updated {job.name}. The earlier version is at {backup}.")


def approve(job, topics):
    if (job / "upload.json").is_file():
        raise ValueError("This video already has an upload record.")
    meta = shorts.read_json(job / "metadata.json")
    if meta.get("production", {}).get("production_revision") != 2:
        raise ValueError("Run python queue_publish.py refresh before approving this older draft.")
    if meta.get("language") == "hi" and meta["production"]["voice"] != "Recorded Hindi narration":
        raise ValueError("Re-record and render this Hindi draft before approving it.")
    topic = next((item for item in topics if item["id"] == meta["topic_id"]), None)
    if topic is None or topic["source"] not in meta["description"]:
        raise ValueError("The draft description must include its original fact source.")
    if meta["production"].get("archive_source"):
        archive = topic.get("archive")
        if (archive is None or archive != meta.get("archive") or
                archive["credit"] not in meta["description"] or archive["source"] not in meta["description"]):
            raise ValueError("Restore the archival photograph credit and source before approving the Short.")
    clip_source = meta["production"].get("clip_source")
    if clip_source and clip_source not in meta["description"]:
        raise ValueError("Restore the clip source in the description before approving the Short.")
    default = shorts.youtube_metadata(topic)
    allowed = {word.lower() for word in default["description"].splitlines()[-1].split()}
    allowed.update("#" + shorts.cleaned(alias).replace(" ", "") for alias in topic["aliases"])
    used = set(re.findall(r"(?<!\w)#[\w]+", meta["title"] + " " + meta["description"].lower()))
    if any(tag.lower() not in allowed for tag in used):
        raise ValueError("The draft contains a hashtag outside this topic's approved list.")
    signature = review_signature(job)
    for other in shorts.completed_jobs():
        if other != job and ((other / "review.json").is_file() or (other / "upload.json").is_file()):
            if review_signature(other)["short.mp4"] == signature["short.mp4"]:
                raise ValueError(f"The same video file already appears in {other.name}.")
    if "qa" not in meta:
        meta["qa"] = inspect_draft(job / "short.mp4", meta)
        shorts.save_json(job / "metadata.json", meta)
        signature = review_signature(job)
    for warning in meta["qa"]["warnings"]:
        print(f"Review warning: {warning}")
    shorts.save_json(job / "review.json", signature)
    print(f"Approved: {job.name}")


def publish(args):
    if not shorts.has_token(shorts.TOKEN):
        raise ValueError("Connect the YouTube channel before publishing approved drafts.")
    if shorts.read_token(shorts.TOKEN)["channel"]["id"] != args.channel_id:
        raise ValueError("The selected channel does not match the connected account.")
    date = shorts.today("Asia/Kolkata")
    pilot_uploads = shorts.read_json(PROGRESS).values() if PROGRESS.is_file() else ()
    pilot_today = sum(entry["date"] == date for entry in pilot_uploads)
    limit = args.count - pilot_today
    pending = [job for job in jobs() if approval_matches(job)]
    for job in pending:
        record = job / "upload.json"
        if record.is_file() and shorts.read_json(record).get("video_id"):
            continue
        used_today = sum(shorts.read_json(path)["attempt_date"] == date
                         for path in shorts.OUTPUT.rglob("upload.json") if path != record)
        if used_today >= limit:
            break
        shorts.publish(job, args.visibility, date, limit, allow_reviewed=True)
    print("Queue run finished. Use status to see remaining drafts.")


def status():
    for job in jobs():
        record = job / "upload.json"
        video_id = shorts.read_json(record).get("video_id") if record.is_file() else None
        stage = "uploaded" if video_id else ("approved" if approval_matches(job) else "awaiting review")
        meta = shorts.read_json(job / "metadata.json")
        direction = meta.get("production")
        treatment_name = (f" | {direction['voice']}, {direction['soundtrack']}"
                          if direction else " | older render")
        metrics = job / "metrics.json"
        result = shorts.read_json(metrics) if metrics.is_file() else None
        outcome = (f" | {result['age_hours']}h: {result['stayed_to_watch']:.1f}% stayed, "
                   f"{result['average_viewed']:.1f}% viewed, {result['engaged_views']} engaged views") if result else ""
        url = f" | https://youtu.be/{video_id}" if video_id else ""
        print(f"{stage:15} {job.name} [{meta['format']}] {meta['title']}{treatment_name}{url}{outcome}")


def record_metrics(args, job):
    path = job / "upload.json"
    if not path.is_file() or not shorts.read_json(path).get("video_id"):
        raise ValueError("Record metrics only for an uploaded video.")
    if (args.age_hours <= 0 or not 0 <= args.stayed <= 100 or
            not 0 <= args.average < float("inf") or args.engaged < 0):
        raise ValueError("Enter valid video age, percentages, and engaged views.")
    shorts.save_json(job / "metrics.json", {"age_hours": args.age_hours, "stayed_to_watch": args.stayed,
                                            "average_viewed": args.average, "engaged_views": args.engaged})
    print(f"Saved Studio metrics for {job.name}.")


def main():
    shorts.configure_console()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    draft = sub.add_parser("prepare")
    draft.add_argument("--count", type=int, choices=range(1, 21), default=4)
    draft.add_argument("--seconds", type=int, choices=DURATIONS, help="Override the episode length")
    draft.add_argument("--topic", action="append")
    draft.add_argument("--language", choices=("en", "en-IN", "hi"))
    draft.add_argument("--offline", action="store_true")
    draft.add_argument("--auto-visuals", action="store_true",
                       help="Auto-cache stock/NASA visuals for topics without cached media")
    sub.add_parser("refresh", help="Update older drafts and archive their previous versions").add_argument(
        "--offline", action="store_true"
    )
    sub.add_parser("status")
    sub.add_parser("approve").add_argument("job")
    release = sub.add_parser("publish")
    release.add_argument("--count", type=int, choices=(1, 2), default=1)
    release.add_argument("--visibility", choices=("private", "unlisted", "public"), default="private")
    release.add_argument("--channel-id", required=True)
    metrics = sub.add_parser("metrics")
    metrics.add_argument("job")
    metrics.add_argument("--age-hours", type=int, required=True)
    metrics.add_argument("--stayed", type=float, required=True)
    metrics.add_argument("--average", type=float, required=True)
    metrics.add_argument("--engaged", type=int, required=True)
    args = parser.parse_args()
    shorts.STATE.mkdir(exist_ok=True)
    shorts.OUTPUT.mkdir(exist_ok=True)
    try:
        with shorts.FileLock(shorts.STATE / "worker.lock", timeout=0):
            topics = shorts.read_json(shorts.ROOT / "topics.json")
            if args.command == "prepare":
                prepare(args, topics)
            elif args.command == "refresh":
                refresh(args, topics)
            elif args.command == "status":
                status()
            elif args.command == "publish":
                publish(args)
            else:
                job = next((item for item in jobs() if item.name == args.job), None)
                if job is None:
                    raise ValueError("Choose an existing queue job from the status command.")
                if args.command == "approve":
                    approve(job, topics)
                else:
                    record_metrics(args, job)
    except shorts.Timeout:
        sys.exit("Another Shorts command is running. Try again when it finishes.")
    except shorts.RefreshError:
        sys.exit("YouTube authorization expired. Run python shorts.py connect-youtube again.")
    except shorts.requests.RequestException:
        sys.exit("The network request failed. Retry the same queue job.")
    except (KeyError, OSError, ValueError) as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()
