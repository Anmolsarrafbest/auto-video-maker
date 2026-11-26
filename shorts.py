"""Generate local Shorts drafts and optionally upload them to one YouTube channel."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
if (ROOT / ".deps").is_dir():
    sys.path.insert(0, str(ROOT / ".deps"))

import requests
from filelock import FileLock, Timeout
from google.auth.exceptions import RefreshError

from render import ANSWER_PAUSE, THINK_LINE, VOICE, encode, narration, spoken_plan
from youtube import read_json, save_json, sign_in, upload

OUTPUT = ROOT / "output"
STATE = ROOT / "state"
VOICE_DIR = ROOT / "voices"
TOKEN = ROOT / "secrets" / "youtube-token.json"
CLIENT = ROOT / "secrets" / "youtube-client.json"
VOICE_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/hifi_tts_92/medium"
ATTRIBUTION = (
    "Narration synthesized with Piper en_US-hifi_tts_92-medium, trained on the voice of "
    "Cori Samuel (Hi-Fi TTS reader 92), CC BY 4.0. The audio is synthesized. "
    "https://openslr.org/109/ | https://creativecommons.org/licenses/by/4.0/"
)
TOPIC_TAGS = {
    "Space": ("astronomy", "space facts", "#SpaceFacts"),
    "Ocean": ("marine biology", "ocean facts", "#OceanFacts"),
    "Physics": ("physics", "everyday science", "#Physics"),
    "History": ("history of science", "science biographies", "#ScienceHistory"),
}


def youtube_metadata(topic, trends=None):
    category, related, hashtag = TOPIC_TAGS.get(
        topic["category"], ("science facts", "science education", "#ScienceFacts")
    )
    aliases = {cleaned(alias) for alias in topic["aliases"]}
    tags = {"#Shorts": None, "#ScienceShorts": None, hashtag: None}
    for terms in (trends or {}).values():
        for term in terms:
            if term in aliases:
                tags.setdefault("#" + term.title().replace(" ", ""), None)
    return {
        "title": topic["beats"][0] if topic.get("format", "quiz") in {"quiz", "myth"} else topic["title"],
        "description": (
            f'{topic["beats"][1]}\n\nSource: {topic["source"]}\n\n'
            f'{ATTRIBUTION}\n\n{" ".join(tags)}'
        ),
        "tags": list(dict.fromkeys([*topic["aliases"], category, related, "science shorts"])),
        "language": "en",
    }


def cleaned(text):
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def fetch_trends(region):
    try:
        response = requests.get("https://trends.google.com/trending/rss", params={"geo": region},
                                timeout=12)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        return [cleaned(title.text or "") for title in root.findall("./channel/item/title")][:50]
    except (requests.RequestException, ET.ParseError):
        print("Google Trends was unavailable. Using the sourced topic library.", file=sys.stderr)
        return []


def choose(topics, used, regional_trends):
    available = [topic for topic in topics if topic["id"] not in used]
    if not available:
        return None, "The topic library is exhausted. Add new sourced topics to topics.json."
    by_alias = {cleaned(alias): topic for topic in available for alias in topic["aliases"]}
    for trend in regional_trends:
        if trend in by_alias:
            return by_alias[trend], "Matched Google Trends"
    return available[0], "Sourced evergreen topic"


def draft_beats(topic, seconds):
    """Opt-in API call on a free, unbilled Gemini project for manually reviewed drafts."""
    key = os.environ.get("GEMINI_FREE_API_KEY")
    if not key:
        print("GEMINI_FREE_API_KEY is unset. Using the sourced narration.", file=sys.stderr)
        return topic["beats"]
    quiz = topic.get("format", "quiz") in {"quiz", "myth"}
    opener = ("Start with a 4 to 9 word question ending in a question mark." if quiz else
              "Start with a brief factual or visual hook matching the supplied opening.")
    response = requests.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent",
        headers={"x-goog-api-key": key},
        json={
            "contents": [{"parts": [{"text": (
                f"Rewrite the two supplied narration beats for a {seconds}-second English science Short. "
                "Keep the facts, numbers, ordering, and meaning. Add no external information. "
                f"{opener} Continue in the "
                "second beat in at most 23 words. Use plain English and no politics or religion. Here are "
                "the verified beats: " + json.dumps(topic["beats"])
            )}]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "responseSchema": {"type": "ARRAY", "items": {"type": "STRING"}}},
        }, timeout=22,
    )
    if response.ok:
        try:
            beats = json.loads(response.json()["candidates"][0]["content"]["parts"][0]["text"])
            if isinstance(beats, list) and len(beats) == 2 and all(
                isinstance(line, str) and 4 <= len(line.split()) <= 23
                and len(line) <= 140 and "\n" not in line for line in beats
            ) and (not quiz or (beats[0].endswith("?") and len(beats[0].split()) <= 9)):
                return beats
        except (ValueError, TypeError, IndexError, KeyError):
            pass
    print(f"Gemini draft unavailable (HTTP {response.status_code}). Using sourced narration.",
          file=sys.stderr)
    return topic["beats"]


def setup_voice():
    VOICE_DIR.mkdir(exist_ok=True)
    for filename in (f"{VOICE}.onnx", f"{VOICE}.onnx.json", "MODEL_CARD"):
        path = VOICE_DIR / filename
        if path.is_file():
            continue
        partial = path.with_suffix(path.suffix + ".part")
        with requests.get(f"{VOICE_BASE}/{filename}", timeout=(15, 90), stream=True) as response:
            response.raise_for_status()
            with partial.open("wb") as handle:
                for chunk in response.iter_content(1024 * 1024):
                    handle.write(chunk)
        if partial.stat().st_size < (1_000_000 if filename.endswith(".onnx") else 50):
            raise ValueError(f"The downloaded Piper file appears incomplete: {filename}.")
        partial.replace(path)
    print(f"Piper voice ready in {VOICE_DIR}")


def completed_jobs():
    return sorted(path.parent for path in OUTPUT.glob("*/metadata.json")
                  if (path.parent / "short.mp4").is_file())


def render_job(topic, provenance, seconds, job, use_gemini=False, *, trends=None):
    if job.exists():
        raise ValueError(f"The output directory already exists: {job}")
    beats = topic["beats"]
    if use_gemini:
        try:
            beats = draft_beats(topic, seconds)
        except requests.RequestException:
            print("Gemini could not be reached. Using sourced narration.", file=sys.stderr)
    def write_audio(script):
        return narration(script, seconds, VOICE_DIR / f"{VOICE}.onnx", wav, topic=topic)

    with tempfile.TemporaryDirectory(dir=STATE, prefix="render-") as directory:
        scratch = Path(directory) / "job"
        scratch.mkdir()
        wav = scratch.parent / "narration.wav"
        try:
            timings = write_audio(beats)
        except ValueError:
            if beats == topic["beats"]:
                raise
            print("The Gemini narration did not fit. Using sourced narration.", file=sys.stderr)
            beats = topic["beats"]
            timings = write_audio(beats)
        encode(topic, beats, timings, seconds, wav, scratch / "short.mp4", scratch / "preview.png")
        style = topic.get("format", "quiz")
        pause = ANSWER_PAUSE if style == "quiz" else .25
        transition = (
            f'Then say "{THINK_LINE}" and wait {pause:g} seconds before revealing the answer.\n'
            if style == "quiz" else "Give the explanation immediately after a short natural beat.\n"
        )
        save_json(scratch / "metadata.json", {
            "topic_id": topic["id"], "selection": provenance, "duration_seconds": seconds,
            **youtube_metadata(topic, trends),
            "narration": [line for _, line, _ in spoken_plan(topic, beats)],
            "format": style, "source": topic["source"],
            "answer_pause_seconds": pause, "needs_review": beats != topic["beats"],
            "made_for_kids": False, "synthetic_media": False,
        })
        (scratch / "flow-prompt.txt").write_text(
            f'Create a vertical 9:16 {seconds}-second illustrated {style} Short.\n'
            f'Subject: {topic["title"]}.\n'
            f'Open immediately with this spoken line: {beats[0]}\n'
            f'{transition}'
            'Keep the opening visuals on the subject and save the explanation for the reveal.\n'
            f'Then answer it in voice-over: {beats[1]}\n'
            f'Illustrate the answer: {topic["labels"][0].lower()}, then {topic["labels"][1].lower()}.\n'
            'Use a mellow instrumental groove with warm chords, a bass line, and light percussion. '
            'Keep it under the voice and bring it forward during the pause.\n'
            'Use illustrated motion graphics, clear visual transitions, and no logos or onscreen text.\n',
            encoding="utf-8",
        )
        scratch.rename(job)
    print(f"{provenance}: {topic['title']}\n{job / 'short.mp4'}")


def today(timezone):
    return datetime.now(ZoneInfo(timezone)).date().isoformat()


def publish(job, visibility, date, limit, allow_reviewed=False):
    meta = read_json(job / "metadata.json")
    upload_path = job / "upload.json"
    current = read_json(upload_path) if upload_path.exists() else {}
    if current.get("video_id"):
        print(f"Already uploaded: https://youtu.be/{current['video_id']}")
        return
    if meta["needs_review"] and not allow_reviewed:
        raise ValueError("Review the Gemini draft, then use upload --reviewed to confirm it.")
    if not TOKEN.exists():
        raise ValueError("YouTube is disconnected. Follow the OAuth setup in README.md, then run "
                         "python shorts.py connect-youtube --client \"PATH_TO_DOWNLOADED_JSON\".")
    occupied = sum(read_json(path)["attempt_date"] == date
                   for path in OUTPUT.glob("*/upload.json") if path != upload_path)
    if occupied >= limit:
        raise ValueError(f"The daily upload limit of {limit} has been reached.")
    if not current and "tags" not in meta:
        library = read_json(ROOT / "topics.json")
        topic = next((item for item in library if item["id"] == meta["topic_id"]), None)
        if topic:
            defaults = youtube_metadata(topic)
            # Preserve any title or description the owner has already edited.
            if meta["title"] == topic["title"]:
                meta["title"] = defaults["title"]
            old_description = f'{topic["title"]}.\n\nSource: {topic["source"]}\n\n{ATTRIBUTION}'
            if meta["description"] == old_description:
                meta["description"] = defaults["description"]
            elif "#shorts" not in meta["description"].lower():
                meta["description"] += "\n\n" + defaults["description"].split("\n\n")[-1]
            meta.update(tags=defaults["tags"], language=defaults["language"])
            save_json(job / "metadata.json", meta)
    video_id = upload(job, TOKEN, visibility, date)
    print(f"Uploaded as {visibility}: https://youtu.be/{video_id}")


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup-voice", help="Download the free Piper voice")
    connect = sub.add_parser("connect-youtube", help="Authenticate the intended YouTube channel")
    connect.add_argument("--client", type=Path, default=CLIENT,
                         help="Path to the downloaded Google OAuth Desktop client JSON")
    for command in ("generate", "daily"):
        cmd = sub.add_parser(command, help="Render a local draft" if command == "generate" else "Run one or two daily slots")
        cmd.add_argument("--seconds", type=int, choices=(12, 15, 20), default=12)
        cmd.add_argument("--region", default="IN", help="Google Trends region code")
        cmd.add_argument("--offline", action="store_true", help="Use the topic library without fetching trends")
    generate = sub.choices["generate"]
    generate.add_argument("--topic", help="Select an ID from topics.json")
    generate.add_argument("--gemini", action="store_true", help="Use an explicit free-tier key for a reviewed draft")
    daily = sub.choices["daily"]
    daily.add_argument("--count", type=int, choices=(1, 2), default=1)
    daily.add_argument("--timezone", default="Asia/Kolkata")
    daily.add_argument("--upload", choices=("private", "unlisted", "public"))
    daily.add_argument("--channel-id", help="Required for scheduled uploads")
    manual = sub.add_parser("upload", help="Upload an existing local job")
    manual.add_argument("job", help="The job folder name inside output/")
    manual.add_argument("--visibility", choices=("private", "unlisted", "public"), default="private")
    manual.add_argument("--reviewed", action="store_true", help="Confirm that you reviewed the Gemini draft")
    manual.add_argument("--daily-limit", type=int, choices=(1, 2), default=2)
    manual.add_argument("--timezone", default="Asia/Kolkata")
    return parser.parse_args(sys.argv[1:] or ["--help"])


def run(args):
    if args.command == "setup-voice":
        setup_voice()
        return
    if args.command == "connect-youtube":
        if not args.client.is_file():
            raise ValueError(
                f"YouTube setup needs a Google OAuth Desktop client JSON. File not found: {args.client}\n"
                "Create and download it from https://console.cloud.google.com/auth/clients "
                "(see README.md for setup).\n"
                'Then run: python shorts.py connect-youtube --client "PATH_TO_DOWNLOADED_JSON"'
            )
        client = read_json(args.client)
        installed = client.get("installed") if isinstance(client, dict) else None
        if not isinstance(installed, dict) or not installed.get("client_id"):
            raise ValueError("Choose the OAuth client JSON downloaded from a Google Desktop app client.")
        channel = sign_in(args.client, TOKEN)
        print(f"Connected channel: {channel['title']} ({channel['id']})")
        return
    if args.command == "upload":
        if not re.fullmatch(r"[a-z0-9-]+", args.job):
            raise ValueError("Pass the folder name inside output/ as the job.")
        job = OUTPUT / args.job
        if job not in completed_jobs():
            raise ValueError(f"Job not found: {job}")
        publish(job, args.visibility, today(args.timezone), args.daily_limit, args.reviewed)
        return

    topics = read_json(ROOT / "topics.json")
    if not re.fullmatch(r"[A-Z]{2}", args.region):
        raise ValueError("Use a two-letter region code, such as IN or US.")
    used = {read_json(path / "metadata.json")["topic_id"] for path in completed_jobs()}
    trends = None

    def current_trends():
        nonlocal trends
        if trends is None:
            regions = dict.fromkeys((args.region, "US", "IN"))
            trends = {} if args.offline else {region: fetch_trends(region) for region in regions}
        return trends

    if args.command == "generate":
        if args.topic:
            topic = next((entry for entry in topics if entry["id"] == args.topic), None)
            if not topic:
                raise ValueError("Unknown topic. Choose an ID in topics.json.")
            reason = "Selected topic"
        else:
            topic, reason = choose(topics, used, current_trends().get(args.region, ()))
        if not topic:
            print(reason)
            return
        job = OUTPUT / f'{topic["id"]}-{uuid.uuid4().hex[:8]}'
        render_job(topic, reason, args.seconds, job, args.gemini, trends=current_trends())
        return

    if args.upload and (not TOKEN.exists() or not args.channel_id or
                        args.channel_id != read_json(TOKEN)["channel"]["id"]):
        raise ValueError("To schedule uploads, connect YouTube and pass its exact --channel-id.")
    date = today(args.timezone)
    for slot in range(1, args.count + 1):
        job = OUTPUT / f"{date}-{slot}"
        if not (job / "metadata.json").is_file():
            topic, reason = choose(topics, used, current_trends().get(args.region, ()))
            if not topic:
                print(reason)
                return
            render_job(topic, reason, args.seconds, job, trends=current_trends())
            used.add(topic["id"])
        if args.upload:
            publish(job, args.upload, date, args.count)


if __name__ == "__main__":
    args = cli()
    STATE.mkdir(exist_ok=True)
    OUTPUT.mkdir(exist_ok=True)
    try:
        with FileLock(STATE / "worker.lock", timeout=0):
            run(args)
    except Timeout:
        sys.exit("Another Shorts command is running. Try again when it finishes.")
    except requests.RequestException:
        sys.exit("The network request failed. Check your connection and retry the same job.")
    except RefreshError:
        sys.exit("YouTube authorization could not be refreshed. Run connect-youtube again.")
    except (OSError, ValueError, KeyError) as error:
        sys.exit(str(error))
