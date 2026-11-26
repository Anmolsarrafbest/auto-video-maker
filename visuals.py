"""Find and cache reviewed visual inserts from zero-cost providers."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import quote, urlparse

if __name__ == "__main__":
    import shorts

from PIL import Image, ImageOps
import requests

from local_secrets import secret

CACHE = Path(__file__).resolve().parent / "assets-cache" / "visuals"
LIMIT = 40 * 1024 * 1024


def get(url, **kwargs):
    response = requests.get(url, timeout=(10, 30), **kwargs)
    response.raise_for_status()
    return response.json()


def stock_get(url, params, headers=None):
    public_params = {name: value for name, value in params.items() if name != "key"}
    key = hashlib.sha256(json.dumps([url, public_params], sort_keys=True).encode()).hexdigest()
    path = CACHE / "search" / f"{key}.json"
    if path.is_file() and time.time() - path.stat().st_mtime < 86400:
        return json.loads(path.read_text(encoding="utf-8"))
    value = get(url, params=params, headers=headers)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return value


def candidates(query, provider):
    if provider == "nasa":
        data = get("https://images-api.nasa.gov/search",
                   params={"q": query, "media_type": "image,video", "page_size": 8})
        for item in data["collection"]["items"]:
            info = item["data"][0]
            yield {"id": info["nasa_id"], "kind": info["media_type"], "title": info["title"],
                   "source": "https://images.nasa.gov/details/" + quote(info["nasa_id"], safe=""),
                   "credit": info.get("photographer") or info.get("center") or "NASA",
                   "collection": item["href"], "keywords": " ".join(info.get("keywords") or ())}
        return
    key = secret("PEXELS_API_KEY" if provider == "pexels" else "PIXABAY_API_KEY")
    if not key:
        raise ValueError(f"Set the {provider.upper()}_API_KEY in .env.")
    if provider == "pexels":
        for kind, endpoint, field in (("video", "videos/search", "videos"), ("image", "search", "photos")):
            data = stock_get("https://api.pexels.com/v1/" + endpoint,
                             {"query": query, "orientation": "portrait", "per_page": 5},
                             headers={"Authorization": key})
            for item in data[field]:
                if kind == "video":
                    files = [file for file in item["video_files"] if file.get("width") and file.get("height")
                             and file["height"] > file["width"] and file.get("file_type") == "video/mp4"]
                    if not files:
                        continue
                    file = min(files, key=lambda entry: (entry["width"] < 1080, abs(entry["width"] - 1080)))
                    url, credit = file["link"], item["user"]["name"]
                    width, height = file["width"], file["height"]
                else:
                    url, credit = item["src"]["large2x"], item["photographer"]
                    width, height = item["width"], item["height"]
                title = item.get("alt") or urlparse(item["url"]).path.strip("/").replace("-", " ")
                yield {"id": str(item["id"]), "kind": kind, "title": title,
                       "source": item["url"], "credit": f"{credit} / Pexels", "url": url,
                       "width": width, "height": height}
        return
    for kind, endpoint in (("video", "videos/"), ("image", "")):
        params = {"key": key, "q": query, "per_page": 5, "safesearch": "true"}
        if kind == "image":
            params.update(orientation="vertical", min_width=720)
        for item in stock_get("https://pixabay.com/api/" + endpoint, params)["hits"]:
            if kind == "video":
                files = [file for file in item["videos"].values() if 0 < file["width"] < file["height"]]
                if not files:
                    continue
                file = min(files, key=lambda entry: (entry["width"] < 1080, abs(entry["width"] - 1080)))
                url, width, height = file["url"], file["width"], file["height"]
            else:
                url = item["largeImageURL"]
                width, height = item["imageWidth"], item["imageHeight"]
            yield {"id": str(item["id"]), "kind": kind, "title": item["tags"],
                   "source": item["pageURL"], "credit": f"{item['user']} / Pixabay", "url": url,
                   "width": width, "height": height}


def cache(topic_id, record):
    kind = record["kind"]
    if "collection" in record:
        collection = record["collection"]
        if urlparse(collection).hostname != "images-assets.nasa.gov":
            raise ValueError("Unexpected NASA asset collection host.")
        assets = get(collection)
        extension = (".mp4",) if kind == "video" else (".jpg", ".jpeg", ".png")
        available = [urlparse(url)._replace(scheme="https").geturl() for url in assets
                     if urlparse(url).scheme in {"http", "https"}
                     and urlparse(url).hostname == "images-assets.nasa.gov"
                     and urlparse(url).path.lower().endswith(extension)]
        quality = ("~medium", "~large", "~orig") if kind == "video" else ("~orig", "~large", "~medium")
        record["url"] = next((url for tag in quality for url in available if tag in url),
                             available[0] if available else "")
    url = record["url"]
    if urlparse(url).scheme != "https":
        raise ValueError("No supported HTTPS media source was found.")
    CACHE.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(url.encode()).hexdigest()[:16]
    filename = f"{topic_id}-{digest}" + (".mp4" if kind == "video" else ".jpg")
    path = CACHE / filename
    if not path.is_file():
        temporary = path.with_suffix(".part")
        try:
            with requests.get(url, stream=True, timeout=(10, 60)) as response:
                response.raise_for_status()
                with temporary.open("wb") as output:
                    count = 0
                    for chunk in response.iter_content(128 * 1024):
                        count += len(chunk)
                        if count > LIMIT:
                            raise ValueError("This asset exceeds the 40 MB cache limit.")
                        output.write(chunk)
            if kind == "image":
                with Image.open(temporary) as original:
                    if original.width * original.height > 32_000_000:
                        raise ValueError("This image exceeds the decode limit.")
                    image = ImageOps.exif_transpose(original).convert("RGB")
                    if record.get("synthetic"):
                        image = ImageOps.fit(image, (1080, 1920), Image.Resampling.LANCZOS)
                    else:
                        image.thumbnail((1920, 1920), Image.Resampling.LANCZOS)
                    image.save(temporary, "JPEG", quality=94)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return {"file": filename, "kind": kind, "source": record["source"], "credit": record["credit"],
            "synthetic": bool(record.get("synthetic")), "automatic": bool(record.get("automatic"))}


def illustration_request(query):
    prompt = f"Vertical 9:16 editorial science illustration, no text or logos: {query}"
    return {"kind": "image", "source": "https://pollinations.ai/", "synthetic": True,
            "credit": "AI illustration generated with Pollinations",
            "url": f"https://gen.pollinations.ai/image/{quote(prompt, safe='')}?width=1080&height=1920"}


def ranked_matches(query, providers):
    terms = set(re.findall(r"\w+", query.casefold())) - {"with", "the", "and", "from", "for", "a", "of", "on"}
    ranked = []
    for provider in providers:
        try:
            matches = list(candidates(query, provider))
        except (requests.RequestException, OSError, ValueError):
            print(f"{provider}: search unavailable.", file=sys.stderr)
            continue
        for match in matches:
            words = set(re.findall(r"\w+", (match["title"] + " " + match.get("keywords", "")).casefold()))
            overlap = len(terms & words)
            if terms and not overlap:
                continue
            width, height = match.get("width", 0), match.get("height", 0)
            portrait_hd = width >= 720 and height >= 1280 and width < height
            shape = 2 if portrait_hd else 1 if not width else 0
            closeness = -abs(width / height - 9 / 16) if height else -1
            ranked.append(((shape, overlap, closeness, match["kind"] == "video", min(width, 2160)), match))
    return [match for _, match in sorted(ranked, key=lambda item: item[0], reverse=True)]


def cache_best(topic, query, providers, pollinations_fallback):
    for match in ranked_matches(query, providers)[:8]:
        try:
            return cache(topic, dict(match, automatic=True))
        except (requests.RequestException, OSError, ValueError):
            print(f"Skipped unavailable asset {match['id']}.", file=sys.stderr)
    if pollinations_fallback:
        return cache(topic, dict(illustration_request(query), automatic=True))
    raise ValueError("No matching visual could be cached. Try another search or provider.")


def selected(topic_id):
    manifest = CACHE / f"{topic_id}.json"
    if not manifest.is_file():
        return None
    record = json.loads(manifest.read_text(encoding="utf-8"))
    path = CACHE / Path(record["file"]).name
    return dict(record, path=path) if path.is_file() else None


def main():
    import shorts

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("topic", choices=[topic["id"] for topic in shorts.read_json(shorts.ROOT / "topics.json")])
    parser.add_argument("query")
    parser.add_argument("--provider", choices=("nasa", "pexels", "pixabay", "pollinations"))
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--use", metavar="ID", help="Cache the reviewed match, or use 'generate' for Pollinations")
    selection.add_argument("--auto", action="store_true", help="Rank matches and cache the best available visual")
    parser.add_argument("--pollinations-fallback", action="store_true", help="Try a keyless illustration after --auto")
    args = parser.parse_args()
    if args.pollinations_fallback and not args.auto:
        parser.error("--pollinations-fallback requires --auto.")
    provider = args.provider or "nasa"
    try:
        if args.auto:
            if args.provider == "pollinations":
                parser.error("Use --pollinations-fallback with --auto, or request --use generate.")
            providers = [args.provider] if args.provider else [
                name for name in ("pexels", "pixabay", "nasa")
                if name == "nasa" or secret(name.upper() + "_API_KEY")
            ]
            selected_asset = cache_best(args.topic, args.query, providers, args.pollinations_fallback)
        elif provider == "pollinations":
            if args.use != "generate":
                parser.error("Use --use generate to request one keyless AI illustration.")
            selected_asset = cache(args.topic, illustration_request(args.query))
        else:
            matches = list(candidates(args.query, provider))
            if not args.use:
                for match in matches:
                    print(f"{match['id']}  {match['kind']}  {match['title']}  {match['source']}")
                return
            record = next((match for match in matches if match["id"] == args.use), None)
            if not record:
                raise ValueError("That ID was not found in the provider results.")
            selected_asset = cache(args.topic, record)
        shorts.save_json(CACHE / f"{args.topic}.json", selected_asset)
        print(f"Visual cached for {args.topic}. Review the match: {selected_asset['source']}")
    except requests.HTTPError as error:
        parser.exit(1, f"Visual provider returned HTTP {error.response.status_code}. Cached assets remain available.\n")
    except (requests.RequestException, OSError, ValueError) as error:
        reason = str(error) if isinstance(error, ValueError) else type(error).__name__
        parser.exit(1, f"Visual setup failed ({reason}). Cached assets remain available.\n")


if __name__ == "__main__":
    main()
