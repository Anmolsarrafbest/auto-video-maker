"""Research sources and pacing without a paid LLM or media scraping."""

import argparse
from datetime import datetime, timedelta, timezone
import os
import sys
from urllib.parse import quote

import shorts
import youtube
from render import spoken_plan
from storyboard import CLIPS, episode
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials


def matches_nearby(text, words):
    tokens = shorts.cleaned(text).split()
    window = len(words) + 3
    return any(words <= set(tokens[start:start + window]) for start in range(len(tokens)))


def nasa(query, limit):
    words = set(shorts.cleaned(query).split())
    if not words:
        raise ValueError("Enter a NASA topic to search for.")
    topics = shorts.read_json(shorts.ROOT / "topics.json")
    reviewed = [
        (topic, CLIPS[topic["id"]]) for topic in topics if topic["id"] in CLIPS
        and words <= set(shorts.cleaned(f'{topic["title"]} {CLIPS[topic["id"]]["label"]} '
                                       f'{CLIPS[topic["id"]]["credit"]} {CLIPS[topic["id"]]["context"]}').split())
    ]
    for topic, clip in reviewed:
        print(f'Reviewed insert for {topic["id"]}: {clip["source"]}')
    response = shorts.requests.get("https://images-api.nasa.gov/search", timeout=20,
                                   params={"q": query, "media_type": "video", "page_size": max(limit, 20)})
    response.raise_for_status()
    entries = [item["data"][0] for item in response.json()["collection"]["items"] if item.get("data")]
    entries = [entry for entry in entries if matches_nearby(entry["title"], words)
               or matches_nearby(entry.get("description") or "", words)]
    entries.sort(key=lambda entry: -len(words & set(shorts.cleaned(entry["title"]).split())))
    print("NASA video candidates. Check the credit, reuse terms, and relevance on each archive page.")
    if not entries:
        print("No closely matching library videos. Try a broader search.")
    for entry in entries[:limit]:
        address = "https://images.nasa.gov/details/" + quote(entry["nasa_id"], safe="")
        title = " ".join(entry["title"].split())
        print(f"{title} | {entry.get('center', 'NASA')} | {address}")


def open_youtube():
    key = os.getenv("YOUTUBE_API_KEY")
    if key:
        session = shorts.requests.Session()
        session.params["key"] = key
        return session
    if shorts.TOKEN.is_file():
        token = shorts.read_json(shorts.TOKEN)
        return AuthorizedSession(Credentials.from_authorized_user_info(token["credentials"]))
    raise ValueError("Set YOUTUBE_API_KEY locally or connect the YouTube channel first.")


def watchlist(query, region, days, limit):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open_youtube() as session:
        result = session.get(f"{youtube.API}/search", timeout=20, params={
            "part": "snippet", "type": "video", "videoDuration": "short", "regionCode": region,
            "q": query, "publishedAfter": since, "order": "relevance", "maxResults": limit,
        })
        youtube.require(result, (200,), "search YouTube")
        ids = [item["id"]["videoId"] for item in result.json().get("items", [])]
        if not ids:
            print("No recent short-duration videos matched this search.")
            return
        result = session.get(f"{youtube.API}/videos", timeout=20, params={
            "id": ",".join(ids), "part": "snippet,statistics,contentDetails",
        })
        youtube.require(result, (200,), "read YouTube video details")
        items = {item["id"]: item for item in result.json().get("items", [])}
    print("YouTube short-duration results. The duration filter also matches some regular videos.")
    for pk in ids:
        item = items.get(pk)
        if not item:
            continue
        snippet = item["snippet"]
        title = " ".join(snippet["title"].split())
        views = item.get("statistics", {}).get("viewCount", "unknown")
        print(f"{title} | {snippet['channelTitle']} | {snippet['publishedAt'][:10]} | "
              f"{views} public views | https://youtu.be/{pk}")


def brief(topic, wpm):
    plan = spoken_plan(topic, topic["beats"])
    seconds, layout, *_ = episode(topic)
    words = sum(len(line.split()) for _, line, _ in plan)
    estimate = words * 60 / wpm + sum(gap for _, _, gap in plan)
    print(topic["title"])
    print(f"Source to check: {topic['source']}")
    print(f"{seconds}s {layout} plan | {words} spoken words | ~{estimate:.1f}s including pauses at {wpm} WPM")
    print(f"Hook: {plan[0][1]}")
    print(f"Claim to verify: {topic['beats'][1]}")
    if estimate > seconds - 1:
        print("Give the edit more breathing room. Trim the script or choose a longer cut.")
    if topic["id"] in CLIPS:
        print(f"Reviewed archive insert: {CLIPS[topic['id']]['source']}")


def main():
    shorts.configure_console()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    archive = sub.add_parser("nasa")
    archive.add_argument("query")
    archive.add_argument("--limit", type=int, choices=range(1, 21), default=6)
    videos = sub.add_parser("youtube")
    videos.add_argument("query")
    videos.add_argument("--region", choices=("IN", "US"), default="IN")
    videos.add_argument("--days", type=int, choices=(7, 14, 30, 90), default=30)
    videos.add_argument("--limit", type=int, choices=range(1, 21), default=6)
    script = sub.add_parser("brief")
    script.add_argument("topic")
    script.add_argument("--wpm", type=int, choices=range(80, 241), default=155, metavar="80–240")
    args = parser.parse_args()
    try:
        if args.command == "nasa":
            nasa(args.query, args.limit)
        elif args.command == "youtube":
            watchlist(args.query, args.region, args.days, args.limit)
        else:
            topic = next((t for t in shorts.read_json(shorts.ROOT / "topics.json") if t["id"] == args.topic), None)
            if topic is None:
                raise ValueError("Unknown topic. Use an ID from topics.json.")
            brief(topic, args.wpm)
    except shorts.RefreshError:
        sys.exit("The local YouTube sign-in has expired. Reconnect or use a YouTube API key.")
    except shorts.requests.RequestException:
        sys.exit("The research provider could not be reached. Retry after checking the connection.")
    except (ValueError, KeyError, OSError) as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()
