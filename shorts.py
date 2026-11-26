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

import archives
import footage
import visuals
from production import ATTRIBUTION, VOICE_CREDITS, VOICE_SNAPSHOT, treatment, voice_credit
from render import encode, narration, spoken_plan
from storyboard import CLIPS, DURATIONS, episode, flow_prompt
from local_secrets import secret
from youtube import has_token, read_json, read_token, save_json, sign_in, upload

OUTPUT = ROOT / "output"
STATE = ROOT / "state"
VOICE_DIR = ROOT / "voices"
TOKEN = ROOT / "secrets" / "youtube-token.json"
PAUSED_LANGUAGES = {"hi"}
CLIENT = ROOT / "secrets" / "youtube-client.json"
VOICE_BASE = f"https://huggingface.co/rhasspy/piper-voices/resolve/{VOICE_SNAPSHOT}/en"
TOPIC_TAGS = {
    "Space": ("astronomy", "space facts", "#SpaceFacts"),
    "Ocean": ("marine biology", "ocean facts", "#OceanFacts"),
    "Physics": ("physics", "everyday science", "#Physics"),
    "History": ("history of science", "science biographies", "#ScienceHistory"),
}
GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent"
)


def render_treatment(topic):
    direction = treatment(topic)
    direction.update(layout=episode(topic)[1], production_revision=2)
    if topic["id"] in CLIPS and footage.cache_file(topic["id"]).is_file():
        direction["clip_source"] = CLIPS[topic["id"]]["source"]
    if "archive" in topic and archives.cache_file(topic).is_file():
        direction["archive_source"] = topic["archive"]["source"]
    return direction


def configure_console():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def youtube_metadata(topic, trends=None, *, archive_used=False):
    category, related, hashtag = TOPIC_TAGS.get(
        topic["category"], ("science facts", "science education", "#ScienceFacts")
    )
    aliases = {cleaned(alias) for alias in topic["aliases"]}
    direction = treatment(topic)
    language = direction.get("language", "en")
    source_label = "स्रोत" if language == "hi" else "Source"
    archive = topic.get("archive") if archive_used else None
    image_credit = f'\n\nArchival image: {archive["credit"]} {archive["source"]}' if archive else ""
    tags = {"#Shorts": None, "#ScienceShorts": None, hashtag: None}
    for terms in (trends or {}).values():
        for term in terms:
            if term in aliases:
                tags.setdefault("#" + term.title().replace(" ", ""), None)
    return {
        "title": topic["beats"][0] if topic.get("format", "quiz") in {"quiz", "myth"} else topic["title"],
        "description": (
            f'{topic["beats"][1]}\n\n{source_label}: {topic["source"]}{image_credit}\n\n'
            f'{voice_credit(direction["voice"])}\n\n{" ".join(tags)}'
        ),
        "tags": list(dict.fromkeys([*topic["aliases"], category, related, "science shorts"])),
        "language": language,
    }


def pinned_comment(topic):
    prompts = {
        "venus-spin": "Which surprised you more about Venus: its slow spin or its shorter year?",
        "moon-distance": "How would you measure the distance to the Moon from Earth?",
        "rainbow-split": "What would you like to see next: a double rainbow or how a prism separates light?",
        "eclipse-red": "What color did the Moon look during the last lunar eclipse you saw?",
        "saturn-rings": "Which part of Saturn would you explore first: the rings or one of its moons?",
        "mercury-day": "Which would feel stranger on Mercury: a sunrise or a year?",
    }
    categories = {
        "Space": "Which space fact should we animate next?",
        "History": "Which moment from science history should we cover next?",
        "Physics": "What everyday example would you use to explain this?",
        "Ocean": "Which ocean mystery should we explain next?",
    }
    return topic.get("pinned_comment") or prompts.get(
        topic["id"], categories.get(topic["category"], "What would you like us to explain next?")
    )


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
    key = secret("GEMINI_FREE_API_KEY").strip()
    if not key:
        print("GEMINI_FREE_API_KEY is unset. Using the sourced narration.", file=sys.stderr)
        return topic["beats"]
    quiz = topic.get("format", "quiz") in {"quiz", "myth"}
    opener = ("Start with a 4 to 9 word question ending in a question mark." if quiz else
              "Start with a brief factual or visual hook matching the supplied opening.")
    response = requests.post(
        GEMINI_ENDPOINT,
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
    for voice in VOICE_CREDITS:
        language, speaker, quality = voice.split("-")
        for filename in (f"{voice}.onnx", f"{voice}.onnx.json", "MODEL_CARD"):
            local_name = f"{voice}.model-card.md" if filename == "MODEL_CARD" else filename
            path = VOICE_DIR / local_name
            if path.is_file():
                continue
            partial = path.with_suffix(path.suffix + ".part")
            with requests.get(f"{VOICE_BASE}/{language}/{speaker}/{quality}/{filename}",
                              timeout=(15, 90), stream=True) as response:
                response.raise_for_status()
                with partial.open("wb") as handle:
                    for chunk in response.iter_content(1024 * 1024):
                        handle.write(chunk)
            if partial.stat().st_size < (1_000_000 if filename.endswith(".onnx") else 50):
                raise ValueError(f"The downloaded Piper file appears incomplete: {filename}.")
            partial.replace(path)
    print(f"Piper voices ready in {VOICE_DIR}")


def completed_jobs():
    return sorted(path.parent for path in OUTPUT.glob("*/metadata.json")
                  if (path.parent / "short.mp4").is_file())


def plan_shots(provider, brief):
    instruction = ("Act as a documentary editor. Given the locked brief below, return JSON "
                   "with archive_fraction (number 0.60 to 0.85, the cut from archive to diagram) "
                   "and direction (one concrete composition and transition suggestion, up to 400 characters). "
                   "Use only the supplied subjects and sources. Preserve narration and dates.\n" + brief)
    if provider == "ollama":
        response = requests.post("http://127.0.0.1:11434/api/generate", json={
            "model": os.getenv("SHORTS_LOCAL_MODEL", "gemma3:4b"), "stream": False,
            "prompt": instruction, "format": "json"}, timeout=90)
        response.raise_for_status()
        raw = response.json()["response"]
    else:
        key = secret("GEMINI_FREE_API_KEY").strip()
        if not key:
            raise ValueError("Missing free-tier key")
        response = requests.post(
            GEMINI_ENDPOINT,
            headers={"x-goog-api-key": key}, json={"contents": [{"parts": [{"text": instruction}]}],
            "generationConfig": {"responseMimeType": "application/json"}}, timeout=30)
        response.raise_for_status()
        raw = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    result = json.loads(raw)
    fraction, note = result["archive_fraction"], result["direction"]
    if not isinstance(fraction, (int, float)) or not .60 <= fraction <= .85 or not isinstance(note, str) or not 1 <= len(note.strip()) <= 400:
        raise ValueError("Invalid shot plan")
    return fraction, note.strip()


def gemini_episode(topic, seconds, assets):
    key = secret("GEMINI_FREE_API_KEY").strip()
    if not key:
        raise ValueError("GEMINI_FREE_API_KEY is empty in this PowerShell session")
    fields = {
        "hook": {"type": "STRING"},
        "explanation": {"type": "STRING"},
        "opening": {"type": "STRING", "enum": ["statement", "question"]},
        "archive_fraction": {"type": "NUMBER"},
        "motion": {"type": "STRING", "enum": ["brackets", "underline", "pulse"]},
        "sound": {"type": "STRING", "enum": ["pop", "sweep", "chime"]},
        "direction": {"type": "STRING"},
    }
    context = {key: topic[key] for key in ("title", "beats", "source")}
    context["available_archive"] = assets
    instruction = (
        f"Plan one {seconds}-second documentary Short in English. Treat the supplied beats as facts. "
        "Preserve their meaning and numbers. Avoid adding any unverified facts, names or assets. "
        "Use one natural sentence or one short question as the hook, then a concise explanation "
        "that adds information without repeating the hook. Prefer a statement for a visual demonstration. "
        f"Keep the combined script under {int(seconds * 2.1)} words. "
        "Select a motion graphic and sound for the explanation reveal. "
        "Set archive_fraction between 0.60 and 0.85 and write a specific shot prompt under 400 characters. "
        "Leave the final two seconds available for the end overlay.\n" + json.dumps(context)
    )
    response = requests.post(
        GEMINI_ENDPOINT, headers={"x-goog-api-key": key}, timeout=30,
        json={"contents": [{"parts": [{"text": instruction}]}],
              "generationConfig": {"responseMimeType": "application/json",
                                   "responseSchema": {"type": "OBJECT", "properties": fields,
                                                      "required": list(fields)}}},
    )
    response.raise_for_status()
    parts = response.json()["candidates"][0]["content"]["parts"]
    plan = json.loads("".join(part["text"] for part in parts if not part.get("thought")))
    for name, field in fields.items():
        value = plan[name]
        if field["type"] == "STRING":
            if not isinstance(value, str) or not value.strip() or len(value) > 400:
                raise ValueError("Invalid plan text")
            if "enum" in field and value not in field["enum"]:
                raise ValueError("Invalid plan option")
    fraction = plan["archive_fraction"]
    if not isinstance(fraction, (int, float)) or not .60 <= fraction <= .85:
        raise ValueError("Invalid archive timing")
    if (plan["opening"] == "question") != plan["hook"].strip().endswith("?"):
        raise ValueError("Opening does not match its format")
    return plan


def render_job(topic, provenance, seconds, job, use_gemini=False, *, trends=None, planner=None):
    if topic.get("language") in PAUSED_LANGUAGES:
        raise ValueError("Hindi production is paused. Choose an English topic for now.")
    if job.exists():
        raise ValueError(f"The output directory already exists: {job}")
    beats = topic["beats"]
    direction = render_treatment(topic)
    seconds = seconds or episode(topic)[0]
    episode_plan = None
    if planner == "gemini":
        assets = {name: direction[name] for name in ("clip_source", "archive_source") if name in direction}
        try:
            episode_plan = gemini_episode(topic, seconds, assets)
            beats = [episode_plan["hook"], episode_plan["explanation"]]
            topic = dict(topic, episode_plan=episode_plan, archive_fraction=episode_plan["archive_fraction"])
        except (ValueError, KeyError, TypeError, IndexError, requests.RequestException) as error:
            response = getattr(error, "response", None)
            reason = f"HTTP {response.status_code}" if response is not None else type(error).__name__
            if not secret("GEMINI_FREE_API_KEY").strip():
                reason = "GEMINI_FREE_API_KEY is empty"
            print(f"Gemini episode plan unavailable ({reason}). Using the built-in plan.", file=sys.stderr)
    elif use_gemini and topic.get("language") != "hi":
        try:
            beats = draft_beats(topic, seconds)
        except requests.RequestException:
            print("Gemini could not be reached. Using sourced narration.", file=sys.stderr)
    archive_path = None
    if "archive_source" in direction:
        archive_path = archives.cache_file(topic)
    elif "archive" in topic:
        print(f"Archival photo unavailable for {topic['id']}. Using its original diagram.", file=sys.stderr)
    visual = visuals.selected(topic["id"]) if archive_path is None and "clip_source" not in direction else None
    if visual:
        direction["visual_source"] = visual["source"]

    def write_audio(script):
        return narration(script, seconds, VOICE_DIR / f'{direction["voice"]}.onnx', wav, topic=topic)

    with tempfile.TemporaryDirectory(dir=STATE, prefix="render-") as directory:
        scratch = Path(directory) / "job"
        scratch.mkdir()
        wav = scratch.parent / "narration.wav"
        try:
            timings = write_audio(beats)
        except ValueError:
            if beats == topic["beats"] and not episode_plan:
                raise
            print("The Gemini narration did not fit. Using sourced narration.", file=sys.stderr)
            beats = topic["beats"]
            if episode_plan:
                topic = {key: value for key, value in topic.items()
                         if key not in {"episode_plan", "archive_fraction"}}
                episode_plan = None
            timings = write_audio(beats)
        brief = flow_prompt(topic, timings, seconds, spoken_plan(topic, beats), "clip_source" in direction)
        if archive_path:
            brief += f'Available photograph: {topic["archive"]["credit"]}\n'
        if episode_plan:
            direction["planner"] = "gemini"
            direction["episode_plan"] = episode_plan
            brief += f'Editor suggestion: {episode_plan["direction"]}\n'
            brief += f'Reveal graphic: {episode_plan["motion"]}. Reveal sound: {episode_plan["sound"]}.\n'
            print("Using one Gemini plan for narration, shots, motion and sound.")
        if planner == "ollama":
            try:
                fraction, note = plan_shots(planner, brief)
                topic = dict(topic, archive_fraction=fraction)
                direction.update(planner=planner, archive_fraction=fraction)
                brief += f"Editor suggestion: {note}\n"
            except (ValueError, KeyError, TypeError, IndexError, requests.RequestException) as error:
                response = getattr(error, "response", None)
                if response is not None:
                    reason = f"HTTP {response.status_code}"
                elif isinstance(error, requests.RequestException):
                    reason = type(error).__name__
                elif planner == "gemini" and not secret("GEMINI_FREE_API_KEY").strip():
                    reason = "GEMINI_FREE_API_KEY is empty in this PowerShell session"
                else:
                    reason = "the model returned an invalid or incomplete shot plan"
                print(f"{planner} shot planner unavailable ({reason}). Using the built-in shot plan.",
                      file=sys.stderr)
        if archive_path or "clip_source" in direction:
            brief += f'Use the credited archive until {seconds * topic.get("archive_fraction", .73):.2f}s, then cut to the explanation.\n'
        direction.update(encode(
            topic, beats, timings, seconds, wav, scratch / "short.mp4", scratch / "preview.png",
            archive_path=archive_path, use_clip="clip_source" in direction, visual=visual,
        ))
        style = topic.get("format", "quiz")
        pause = next(gap for role, _, gap in spoken_plan(topic, beats) if role == "answer")
        metadata = youtube_metadata(topic, trends, archive_used=archive_path is not None)
        if "clip_source" in direction:
            body, separator, tags = metadata["description"].rpartition("\n\n")
            clip = CLIPS[topic["id"]]
            metadata["description"] = f'{body}\n\n{clip["credit"]} {clip["source"]}{separator}{tags}'
        if visual:
            body, separator, tags = metadata["description"].rpartition("\n\n")
            metadata["description"] = f'{body}\n\nVisual: {visual["credit"]} {visual["source"]}{separator}{tags}'
        save_json(scratch / "metadata.json", {
            "topic_id": topic["id"], "selection": provenance, "duration_seconds": seconds,
            **metadata,
            "narration": [line for _, line, _ in spoken_plan(topic, beats)],
            "format": style, "source": topic["source"],
            "production": direction,
            "archive": topic.get("archive") if archive_path is not None else None,
            "answer_pause_seconds": pause, "pinned_comment": pinned_comment(topic),
            "needs_review": bool(episode_plan or (visual and visual.get("automatic"))) or beats != topic["beats"],
            "made_for_kids": False, "synthetic_media": bool(visual and visual["synthetic"]),
        })
        (scratch / "flow-prompt.txt").write_text(
            brief,
            encoding="utf-8",
        )
        (scratch / "research-prompt.txt").write_text(
            f'Research this short for a general audience: {topic["title"]}\n'
            f'Start with this source: {topic["source"]}\n'
            'Check the narration against primary sources such as mission archives, original papers, '
            'or museum collections. Give two concrete details that explain how we know, with direct links. '
            'Flag uncertain claims and suggest one relevant archival shot with its credit and usage terms. '
            'Draft a short natural opening, a question-led alternative, and a concise explanation. '
            'Keep the final two seconds clear for the follow and subscribe overlay.\n'
            f'Current narration: {" ".join(beats)}\n',
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
    if meta.get("language") == "hi" and meta.get("production", {}).get("voice") != "Recorded Hindi narration":
        raise ValueError("Re-record and render this Hindi draft before publishing it.")
    if meta["needs_review"] and not allow_reviewed:
        raise ValueError("Review the Gemini draft, then use upload --reviewed to confirm it.")
    if not has_token(TOKEN):
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
                meta["description"] = defaults["description"].replace(
                    voice_credit(treatment(topic)["voice"]), ATTRIBUTION
                )
            elif "#shorts" not in meta["description"].lower():
                meta["description"] += "\n\n" + defaults["description"].split("\n\n")[-1]
            meta.update(tags=defaults["tags"], language=defaults["language"])
            save_json(job / "metadata.json", meta)
    video_id = upload(job, TOKEN, visibility, date)
    print(f"Uploaded as {visibility}: https://youtu.be/{video_id}")


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup-voice", help="Download the free Piper voice set")
    sub.add_parser("setup-captions", help="Download the free CPU Whisper tiny model for offline use")
    assets = sub.add_parser("setup-assets", help="Download the verified history images")
    assets.add_argument("--topic", action="append", help="Only fetch the selected archival topic")
    clip_setup = sub.add_parser("setup-footage", help="Download reviewed NASA footage")
    clip_setup.add_argument("--topic", choices=CLIPS)
    connect = sub.add_parser("connect-youtube", help="Authenticate the intended YouTube channel")
    connect.add_argument("--client", type=Path, default=CLIENT,
                         help="Path to the downloaded Google OAuth Desktop client JSON")
    for command in ("generate", "daily"):
        cmd = sub.add_parser(command, help="Render a local draft" if command == "generate" else "Run one or two daily slots")
        cmd.add_argument("--seconds", type=int, choices=DURATIONS, help="Override the episode length")
        cmd.add_argument("--region", default="IN", help="Google Trends region code")
        cmd.add_argument("--offline", action="store_true", help="Use the topic library without fetching trends")
    generate = sub.choices["generate"]
    generate.add_argument("--shot-planner", choices=("ollama", "gemini"),
                          help="Use Gemini for one complete episode plan or Ollama for shot direction")
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
    if args.command == "setup-captions":
        from huggingface_hub import snapshot_download
        from hindi_captions import MODEL_DIR

        snapshot_download("Systran/faster-whisper-tiny", local_dir=MODEL_DIR,
                          allow_patterns=["*.json", "model.bin", "vocabulary.txt"])
        print("Whisper tiny cached for offline captions.")
        return
    if args.command == "setup-voice":
        setup_voice()
        return
    if args.command == "setup-footage":
        for topic_id in ([args.topic] if args.topic else CLIPS):
            print(footage.setup(topic_id))
        return
    if args.command == "setup-assets":
        available = {topic["id"]: topic for topic in read_json(ROOT / "topics.json") if "archive" in topic}
        requested = args.topic or available
        failed = []
        for topic_id in requested:
            if topic_id not in available:
                raise ValueError(f"No verified archival image is listed for {topic_id}.")
            topic = available[topic_id]
            try:
                print(archives.fetch(topic))
            except (requests.RequestException, OSError, ValueError) as error:
                failed.append(f"{topic_id}: {error}")
        if failed:
            raise ValueError("Some archive hosts did not supply their image. The renderer will use "
                             "the original diagrams for those topics.\n" + "\n".join(failed))
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

    topics = [topic for topic in read_json(ROOT / "topics.json") if topic.get("language") not in PAUSED_LANGUAGES]
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
        render_job(topic, reason, args.seconds, job, args.gemini, trends=current_trends(), planner=args.shot_planner)
        return

    if args.upload and (not has_token(TOKEN) or not args.channel_id or
                        args.channel_id != read_token(TOKEN)["channel"]["id"]):
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
    configure_console()
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
