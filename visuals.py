"""Find and cache reviewed visual inserts from zero-cost providers."""

import argparse
import hashlib
from html import unescape
from itertools import islice
from importlib.util import find_spec
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import quote, urlparse
from urllib.parse import unquote

if __name__ == "__main__":
    import shorts

from PIL import Image, ImageOps
import requests

from local_secrets import flag, secret
import rate_limiter

CACHE = Path(__file__).resolve().parent / "assets-cache" / "visuals"
PIXABAY_CACHE = Path(__file__).resolve().parent / "assets-cache" / "pixabay_search_cache"
LIMIT = 40 * 1024 * 1024
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
COMMONS_HEADERS = {"User-Agent": "AutoVideoMaker/1.1 (https://github.com/Anmolsarrafbest/auto-video-maker)"}
COMMONS_INFO = {"action": "query", "prop": "imageinfo", "format": "json", "formatversion": 2,
                "iiprop": "url|mime|size|extmetadata",
                "iiextmetadatafilter": "Artist|LicenseShortName|LicenseUrl|Restrictions|ImageDescription|DateTimeOriginal"}
SHOT_ROLES = ("hook", "explanation", "payoff")
QUERY_STOP = set(("a an and are as at by did does for from has how in into is of on or that the "
                  "their this through to was what when where which why with its it end keep held "
                  "more most very just like side each both than then over also only still here there "
                  "some such make makes made while frame scene slow slowly wide tight across around "
                  "open show cut ease push pan zoom hold introduce use highlight close image clip shot "
                  "footage visual credited photograph graphic animation simple render reveal camera "
                  "background detail view comparison lasted seconds years look").split())


def validate_reference(value):
    """Accept editorial citations as metadata without requesting untrusted URLs."""
    url = urlparse(value)
    host = url.hostname or ""
    if (url.scheme != "https" or url.username or url.password or url.port
            or not url.path.strip("/") or not re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z]{2,63}", host)
            or host.endswith((".local", ".internal", ".localhost", ".test", ".invalid"))):
        raise ValueError("Give a direct public HTTPS source that supports the claim.")
    return url


def cache_commons(topic_id, source, *, event_year=""):
    url = validate_reference(source)
    title = unquote(url.path.removeprefix("/wiki/"))
    if url.hostname != "commons.wikimedia.org" or not title.startswith("File:"):
        raise ValueError("For automatic media import, use a Wikimedia Commons File: page.")
    response = get(COMMONS_API, headers=COMMONS_HEADERS, params={**COMMONS_INFO, "titles": title})
    try:
        page = response["query"]["pages"][0]
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError("Commons did not return a file and license.") from error
    candidate = commons_candidate(page)
    if event_year and not matches_event_year(candidate, event_year):
        raise ValueError("The supplied archive did not identify the requested event year.")
    return cache(topic_id, candidate)


def commons_candidate(page):
    try:
        info = page["imageinfo"][0]
        fields = info["extmetadata"]
        license_name = fields["LicenseShortName"]["value"]
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError("Commons did not return a file and license.") from error
    if license_name not in {"Public domain", "CC0", "CC BY 4.0", "CC BY 3.0", "CC BY 2.0"}:
        raise ValueError("This Commons license requires manual media review.")
    if fields.get("Restrictions", {}).get("value"):
        raise ValueError("This Commons file has additional restrictions.")
    mime = info["mime"]
    if not mime.startswith("video/") and mime not in {"image/jpeg", "image/png", "image/webp"}:
        raise ValueError("Use a Commons photograph or video.")
    asset = urlparse(info["url"])
    if asset.scheme != "https" or asset.hostname != "upload.wikimedia.org":
        raise ValueError("Unexpected Commons asset host.")
    artist = unescape(re.sub(r"<[^>]*>", " ", fields.get("Artist", {}).get("value", ""))).strip()
    description = unescape(re.sub(r"<[^>]*>", " ", fields.get("ImageDescription", {}).get("value", "")))[:1000]
    license_url = fields.get("LicenseUrl", {}).get("value", "")
    if license_name.startswith("CC BY ") and (not artist or not license_url):
        raise ValueError("This Commons file lacks complete attribution.")
    credit = " · ".join(item for item in (artist, license_name, license_url, "cropped for this video") if item)
    title = page["title"]
    return {"id": str(page["pageid"]), "title": Path(title.removeprefix("File:")).stem.replace("_", " "),
            "keywords": description,
            "date": fields.get("DateTimeOriginal", {}).get("value", ""),
            "kind": "video" if mime.startswith("video/") else "image",
            "width": info.get("width", 0), "height": info.get("height", 0),
            "url": asset._replace(query="", fragment="").geturl(),
            "source": "https://commons.wikimedia.org/wiki/" + quote(title.replace(" ", "_"), safe=":"),
            "credit": " ".join(credit.split()), "automatic": True}


def get(url, **kwargs):
    response = requests.get(url, timeout=(10, 30), **kwargs)
    response.raise_for_status()
    return response.json()


def pexels_enabled():
    return flag("ENABLE_PEXELS", default=False)


def normalize_query(query):
    return " ".join(query.strip().casefold().split())


def pixabay_cache_path(query, media_type):
    digest = hashlib.sha256(json.dumps([normalize_query(query), media_type]).encode()).hexdigest()
    return PIXABAY_CACHE / f"{media_type}-{digest}.json"


def pixabay_get(endpoint, params, media_type):
    path = pixabay_cache_path(params["q"], media_type)
    if path.is_file() and time.time() - path.stat().st_mtime < 86400:
        return json.loads(path.read_text(encoding="utf-8"))
    response, _ = rate_limiter.request_with_backoff(
        "GET",
        "https://pixabay.com/api/" + endpoint,
        provider_id="pixabay",
        params=params,
        timeout=(10, 30),
    )
    response.raise_for_status()
    value = response.json()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.replace(path)
    return value


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
    if provider == "wikimedia":
        data = stock_get(COMMONS_API, {**COMMONS_INFO, "generator": "search", "gsrnamespace": 6,
                                     "gsrsearch": query, "gsrlimit": 8}, headers=COMMONS_HEADERS)
        for page in data.get("query", {}).get("pages", []):
            try:
                yield dict(commons_candidate(page), provider="wikimedia")
            except ValueError:
                continue
        return
    if provider == "nasa":
        data = stock_get("https://images-api.nasa.gov/search",
                   params={"q": query, "media_type": "image,video", "page_size": 8})
        for item in data["collection"]["items"]:
            info = item["data"][0]
            yield {"id": info["nasa_id"], "kind": info["media_type"], "title": info["title"],
                   "provider": "nasa",
                   "source": "https://images.nasa.gov/details/" + quote(info["nasa_id"], safe=""),
                   "credit": info.get("photographer") or info.get("center") or "NASA",
                   "collection": item["href"], "keywords": " ".join(info.get("keywords") or ())}
        return
    if provider == "pexels":
        if not pexels_enabled():
            return
        key = secret("PEXELS_API_KEY")
        if not key:
            raise ValueError("Set the PEXELS_API_KEY in .env.")
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
                yield {"id": str(item["id"]), "kind": kind, "title": title, "provider": "pexels",
                       "source": item["url"], "credit": f"{credit} / Pexels", "url": url,
                       "width": width, "height": height}
        return
    key = secret("PIXABAY_API_KEY")
    if not key:
        raise ValueError("Set the PIXABAY_API_KEY in .env.")
    for kind, endpoint in (("video", "videos/"), ("image", "")):
        params = {"key": key, "q": query[:100], "per_page": 8, "safesearch": "true"}
        if kind == "image":
            params.update(image_type="photo", min_width=640)
        try:
            hits = pixabay_get(endpoint, params, kind)["hits"]
        except (requests.RequestException, OSError, ValueError, KeyError):
            print(f"pixabay: {kind} search unavailable.", file=sys.stderr)
            continue
        for item in hits:
            if kind == "video":
                files = [file for file in item["videos"].values()
                         if file.get("url") and min(file["width"], file["height"]) >= 540
                         and 0 < file["size"] <= LIMIT]
                if not files:
                    continue
                file = min(files, key=lambda entry: (min(entry["width"], entry["height"]) < 720,
                                                     abs(min(entry["width"], entry["height"]) - 1080)))
                url, width, height = file["url"], file["width"], file["height"]
            else:
                url = item["largeImageURL"]
                width, height = item["imageWidth"], item["imageHeight"]
            yield {"id": str(item["id"]), "kind": kind, "title": item["tags"], "provider": "pixabay",
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
            with requests.get(url, stream=True, timeout=(10, 60),
                              allow_redirects=not record.get("headers"),
                              headers={**COMMONS_HEADERS, **record.get("headers", {})}) as response:
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
    key = secret("POLLINATIONS_API_KEY").strip()
    model = secret("POLLINATIONS_ZERO_COST_MODEL").strip()
    if not key or not model:
        raise ValueError("Pollinations requires an API key and an explicitly configured zero-cost model.")
    models = get("https://gen.pollinations.ai/image/models")
    if not isinstance(models, list):
        raise ValueError("Could not verify the Pollinations model catalog.")
    match = next((entry for entry in models if isinstance(entry, dict)
                  and model in (entry.get("name"), entry.get("id"))), {})
    pricing = match.get("pricing")
    if (match.get("paid_only") or not isinstance(pricing, dict) or "completionImageTokens" not in pricing
            or any(str(rate) not in {"0", "0.0"} for field, rate in pricing.items() if field != "currency")):
        raise ValueError("The Pollinations catalog did not confirm zero-cost image generation.")
    prompt = f"Vertical 9:16 editorial science illustration, no text or logos: {query}"
    return {"kind": "image", "source": "https://pollinations.ai/", "synthetic": True,
            "credit": "AI illustration generated with Pollinations",
            "headers": {"Authorization": f"Bearer {key}"},
            "url": (f"https://gen.pollinations.ai/image/{quote(prompt, safe='')}"
                    f"?width=1080&height=1920&model={quote(model, safe='')}")}


def matches_event_year(match, year):
    dates = re.findall(r"\b[12]\d{3}\b", re.sub(r"<[^>]+>", "", match.get("date", "")))
    return year in dates if dates else bool(re.search(rf"\b{re.escape(year)}\b", match["title"]))


def ranked_matches(query, providers, *, strict=False, event_year=""):
    terms = set(keywords(query))
    if event_year:
        terms.discard(event_year)
    ranked = []
    for provider in providers:
        try:
            matches = list(candidates(query, provider))
        except (requests.RequestException, OSError, ValueError, KeyError, TypeError, IndexError):
            print(f"{provider}: search unavailable.", file=sys.stderr)
            continue
        for match in matches:
            words = set(keywords(match["title"] + " " + match.get("keywords", "")))
            overlap = len(terms & words)
            if overlap < min(len(terms), 3 if event_year else 2 if strict else 1):
                continue
            if event_year and not matches_event_year(match, event_year):
                continue
            width, height = match.get("width", 0), match.get("height", 0)
            portrait_hd = width >= 720 and height >= 1280 and width < height
            shape = 2 if portrait_hd else 1 if not width else 0
            closeness = -abs(width / height - 9 / 16) if height else -1
            video = match["kind"] == "video"
            vertical_video = video and portrait_hd
            near_target = video and 0 < width < height and closeness >= -.025
            ranked.append(((near_target, vertical_video, overlap, video, shape, closeness, min(width, 2160)), match))
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
    return local_asset(read_manifest(topic_id))


def read_manifest(topic_id):
    manifest = CACHE / f"{topic_id}.json"
    if manifest.is_file():
        return json.loads(manifest.read_text(encoding="utf-8"))
    return {}


def local_asset(record):
    if not record or "file" not in record:
        return None
    path = CACHE / Path(record["file"]).name
    return dict(record, path=path) if path.is_file() else None


def keywords(text):
    return list(dict.fromkeys(word for word in re.findall(r"[a-z0-9]+", text.casefold())
                              if len(word) > 1 and word not in QUERY_STOP))


def shot_search_fields(value, title, shots):
    queries = value.get("shot_queries")
    icon = value.get("icon_query")

    def valid_phrase(phrase, low, high):
        return (isinstance(phrase, str) and len(phrase) <= 70
                and re.fullmatch(r"[A-Za-z0-9'-]+(?: [A-Za-z0-9'-]+)*", phrase)
                and low <= len(phrase.split()) <= high and bool(keywords(phrase)))

    if queries is not None and (not isinstance(queries, list) or len(queries) != len(SHOT_ROLES)
                                or not all(valid_phrase(query, 2, 5) for query in queries)):
        raise ValueError("Supply three shot_queries of two to five words.")
    if icon is not None and not valid_phrase(icon, 1, 2):
        raise ValueError("Supply an icon_query of one or two words.")
    subject = keywords(title)
    if queries is None:
        queries = [" ".join(list(dict.fromkeys(subject[:3] + keywords(shot) + subject[3:]))[:5])
                   for shot in shots]
    return {"shot_queries": queries, "icon_query": icon or " ".join(subject[:2])}


ICON_SETS = {"tabler": "Tabler Icons (MIT)", "mdi": "Material Design Icons (Apache 2.0)",
             "lucide": "Lucide (ISC)", "lucide-lab": "Lucide Lab (ISC)",
             "iconoven": "IconOven by Appic Marketing (MIT)"}


def cache_icon(query):
    if not query:
        return None
    data = stock_get("https://api.iconify.design/search",
                     {"query": query, "prefixes": ",".join(ICON_SETS), "limit": 32})
    terms = set(keywords(query))
    for icon in data.get("icons", []):
        if not re.fullmatch(r"[a-z-]+:[a-z0-9-]+", icon):
            continue
        prefix, name = icon.split(":")
        if prefix not in ICON_SETS or not terms.intersection(keywords(name)):
            continue
        filename = f"icon-{prefix}-{name}.svg"
        path = CACHE / filename
        if not path.is_file():
            try:
                with requests.get(f"https://api.iconify.design/{prefix}/{name}.svg", stream=True,
                                  timeout=(5, 12)) as response:
                    response.raise_for_status()
                    svg = response.raw.read(65537, decode_content=True)
            except requests.HTTPError as error:
                if error.response.status_code == 404:
                    continue
                raise
            if (len(svg) > 65536 or not svg.lstrip().startswith(b"<svg")
                    or re.search(rb"<!|href\s*=|url\s*\(|<(?:script|style|foreignObject|image)\b", svg, re.I)):
                raise ValueError("Unexpected icon response.")
            temporary = path.with_suffix(".part")
            temporary.write_bytes(svg)
            temporary.replace(path)
        return {"file": filename, "kind": "icon", "credit": ICON_SETS[prefix] + " · tinted for this video",
                "source": f"https://icon-sets.iconify.design/{prefix}/{name}/",
                "synthetic": False, "automatic": True}
    return None


def beat_cache_records(manifest, specs):
    if not specs:
        legacy = manifest.get("shots", [manifest] if "file" in manifest else [])
        return {index: record for index, record in enumerate(legacy) if record}
    cached = {item["beat_id"]: item for item in manifest.get("beats", [])}
    return {spec["beat_id"]: cached[spec["beat_id"]]["asset"] for spec in specs
            if spec["beat_id"] in cached
            and all(cached[spec["beat_id"]].get(key) == spec[key] for key in ("search_query", "visual_type"))
            and cached[spec["beat_id"]].get("asset")}


def selected_storyboard(topic):
    manifest = read_manifest(topic["id"])
    records = beat_cache_records(manifest, topic["discovery"].get("beats"))
    return {key: asset for key, record in records.items() if (asset := local_asset(record))}, local_asset(manifest.get("icon"))


def select_relevant_candidates(topic, beats_by_id, candidate_pools, *, requests_sent=None):
    """Score top media candidates across all beats in one batched Gemini Flash Lite call."""
    if not candidate_pools:
        return {}
    key = secret("GEMINI_FREE_API_KEY").strip()
    if not key:
        return {beat_id: 0 for beat_id in candidate_pools}
    batched = []
    for beat_id, pool in candidate_pools.items():
        beat = beats_by_id[beat_id]
        batched.append({
            "beat_id": beat_id,
            "anchor_text": beat.get("anchor_text", ""),
            "visual_type": beat.get("visual_type", "stock_video"),
            "search_query": beat.get("search_query", ""),
            "candidates": [
                {
                    "index": idx,
                    "kind": item.get("kind", "image"),
                    "provider": item.get("provider", ""),
                    "title": (item.get("title") or "")[:120],
                    "tags_or_description": (item.get("keywords") or "")[:180],
                    "size": f'{item.get("width", 0)}x{item.get("height", 0)}',
                }
                for idx, item in enumerate(pool[:3])
            ],
        })
    context = {
        "topic_title": topic["title"],
        "category": topic["category"],
        "real_event": bool(topic.get("discovery", {}).get("real_event")),
        "event_year": topic.get("discovery", {}).get("event_year", ""),
        "narration": " ".join(topic["beats"]),
        "beats": batched,
    }
    instruction = (
        "Select the most visually relevant media candidate for each beat in one pass. "
        "For each beat, compare the anchor_text and narration context against the candidate titles/tags. "
        "Pick index 0, 1, or 2 ONLY if that candidate genuinely depicts the subject or action of the beat "
        "(prefer video for stock_video beats when equally relevant). "
        "Return -1 if none of the candidates clearly match the beat, if they depict the wrong species/object/era, "
        "or if they look like unrelated text scans/diagrams. "
        "Return JSON mapping every supplied beat_id to its choice (-1, 0, 1, or 2).\n" + json.dumps(context)
    )
    payload = {
        "contents": [{"parts": [{"text": instruction}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "selections": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "beat_id": {"type": "INTEGER"},
                                "choice": {"type": "INTEGER"},
                            },
                            "required": ["beat_id", "choice"],
                        },
                    }
                },
                "required": ["selections"],
            },
        },
    }
    if requests_sent is not None:
        requests_sent.append(payload)
    try:
        response, _ = rate_limiter.call_gemini(key, payload, model="gemini-3.1-flash-lite", timeout=35)
        if not response.ok:
            print(f"Visual relevance gate unavailable (HTTP {response.status_code}); using top ranked matches.",
                  file=sys.stderr)
            return {beat_id: 0 for beat_id in candidate_pools}
        parts = response.json()["candidates"][0]["content"]["parts"]
        parsed = json.loads("".join(part["text"] for part in parts if not part.get("thought")))
        choices = {}
        for entry in parsed.get("selections", []):
            bid = entry.get("beat_id")
            choice = entry.get("choice")
            if bid in candidate_pools and isinstance(choice, int) and -1 <= choice < len(candidate_pools[bid]):
                choices[bid] = choice
        return {beat_id: choices.get(beat_id, 0) for beat_id in candidate_pools}
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as error:
        print(f"Visual relevance gate fallback ({type(error).__name__}); using top ranked matches.", file=sys.stderr)
        return {beat_id: 0 for beat_id in candidate_pools}


def cache_storyboard(topic, *, requests_sent=None):
    """Resolve missing shots from zero-quota archives first, then cached/deduped Pixabay + batched Gemini gate."""
    topic_id = topic["id"]
    discovery = topic["discovery"]
    specs = discovery.get("beats")
    if specs:
        fields = {"icon_query": discovery.get("icon_query", "")}
    elif "shot_queries" in discovery and "icon_query" in discovery:
        fields = {name: discovery[name] for name in ("shot_queries", "icon_query")}
    else:
        fields = shot_search_fields(discovery, topic["title"], discovery["shots"])
    requests_by_beat = specs or [
        {"beat_id": index, "search_query": query, "visual_type": "stock_video"}
        for index, query in enumerate(fields["shot_queries"])
    ]
    supplied = discovery.get("media")
    event_year = discovery.get("event_year", "")
    event = bool(discovery.get("real_event") or supplied or "histor" in topic["category"].casefold())
    old = read_manifest(topic_id)
    if event and (old.get("event_year") != event_year or not old.get("real_event")):
        old = {}
    shots = {key: record for key, record in beat_cache_records(old, specs).items() if local_asset(record)}
    opening_id = next((beat["beat_id"] for beat in requests_by_beat
                       if beat["visual_type"] == "archival"), requests_by_beat[0]["beat_id"])
    if supplied and not shots.get(opening_id, {}).get("supplied"):
        try:
            shots[opening_id] = dict(cache_commons(topic_id, supplied["url"], event_year=event_year), supplied=True)
        except (requests.RequestException, OSError, ValueError):
            print("The supplied Commons asset could not be imported.", file=sys.stderr)
    stock = []
    if secret("PIXABAY_API_KEY"):
        stock.append("pixabay")
    if pexels_enabled() and secret("PEXELS_API_KEY"):
        stock.append("pexels")
    archives = ["wikimedia"]
    if re.search(r"\b(space|planet|moon|orbit|nasa|astronom\w*|mars|earth|solar|star\w*)\b",
                 topic["category"] + " " + topic["title"], re.I):
        archives.insert(0, "nasa")
    has_manim = find_spec("manim") is not None
    query_cache = {}

    def lookup(search_text, providers, required_year, visual_type):
        key = (normalize_query(search_text), tuple(providers), required_year, visual_type)
        if key not in query_cache:
            found = ranked_matches(search_text, providers, strict=True, event_year=required_year)
            if visual_type == "diagram":
                found = [match for match in found if match["kind"] == "image"]
            elif specs and visual_type == "stock_video" and not required_year:
                found.sort(key=lambda match: match["kind"] != "video")
            query_cache[key] = found
        return query_cache[key]

    beats_by_id = {beat["beat_id"]: beat for beat in requests_by_beat}
    candidate_pools = {}
    candidate_meta = {}
    for beat in requests_by_beat:
        index, query, visual_type = (beat[key] for key in ("beat_id", "search_query", "visual_type"))
        if index in shots or not query or visual_type == "callout_card":
            continue
        if visual_type == "diagram" and beat.get("diagram") and has_manim:
            continue
        archive_search = query if not event_year or event_year in keywords(query) else f"{query} {event_year}"
        matches = lookup(archive_search, archives, event_year, visual_type)
        is_stock = False
        if not matches and stock:
            matches = lookup(query, stock, "", visual_type)
            is_stock = bool(matches)
        if matches:
            candidate_pools[index] = matches[:3]
            candidate_meta[index] = is_stock

    selections = select_relevant_candidates(topic, beats_by_id, candidate_pools, requests_sent=requests_sent)
    used = {record["source"] for record in shots.values()}
    used_files = {record["file"] for record in shots.values()}
    for beat in requests_by_beat:
        index, query, visual_type = (beat[key] for key in ("beat_id", "search_query", "visual_type"))
        if index in shots or not query or visual_type == "callout_card":
            continue
        if visual_type == "diagram" and beat.get("diagram") and has_manim:
            continue
        choice = selections.get(index, -1)
        if index in candidate_pools and choice >= 0:
            pool = candidate_pools[index]
            ordered = [pool[choice]] + [item for i, item in enumerate(pool) if i != choice]
            for match in ordered:
                if match["source"] in used:
                    continue
                try:
                    asset = cache(topic_id, dict(match, automatic=True))
                except (requests.RequestException, OSError, ValueError):
                    continue
                if asset["file"] in used_files:
                    continue
                if event and candidate_meta.get(index):
                    asset["context_label"] = "ILLUSTRATIVE STOCK"
                shots[index] = asset
                used.add(asset["source"])
                used_files.add(asset["file"])
                break
        if index not in shots and choice != -1 and secret("POLLINATIONS_ZERO_COST_MODEL").strip():
            try:
                asset = cache(topic_id, dict(illustration_request(query), automatic=True))
                if asset["file"] not in used_files:
                    if event:
                        asset["context_label"] = "ILLUSTRATION"
                    shots[index] = asset
                    used_files.add(asset["file"])
            except (requests.RequestException, OSError, ValueError, TypeError):
                print("Zero-cost illustration unavailable.", file=sys.stderr)
    icon = old.get("icon")
    if not local_asset(icon) and len(shots) < len(requests_by_beat):
        try:
            icon = cache_icon(fields["icon_query"])
        except (requests.RequestException, OSError, ValueError, TypeError):
            print("Iconify search unavailable.", file=sys.stderr)
    CACHE.mkdir(parents=True, exist_ok=True)
    manifest = CACHE / f"{topic_id}.json"
    temporary = manifest.with_suffix(".tmp")
    media = ({"beats": [{**beat, "asset": shots.get(beat["beat_id"])} for beat in specs]} if specs
             else {"shots": [shots.get(index) for index in range(len(requests_by_beat))]})
    temporary.write_text(json.dumps({**media, "icon": icon, "real_event": event, "event_year": event_year,
                                     **fields}, indent=2), encoding="utf-8")
    temporary.replace(manifest)


def main():
    import shorts

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("topic", choices=[topic["id"] for topic in shorts.read_json(shorts.ROOT / "topics.json")])
    parser.add_argument("query")
    parser.add_argument("--provider", choices=("nasa", "wikimedia", "pexels", "pixabay", "pollinations"))
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--use", metavar="ID", help="Cache the reviewed match, or use 'generate' for Pollinations")
    selection.add_argument("--auto", action="store_true", help="Rank matches and cache the best available visual")
    parser.add_argument("--pollinations-fallback", action="store_true", help="Try a configured zero-cost illustration after --auto")
    args = parser.parse_args()
    if args.pollinations_fallback and not args.auto:
        parser.error("--pollinations-fallback requires --auto.")
    provider = args.provider or "nasa"
    try:
        if args.auto:
            if args.provider == "pollinations":
                parser.error("Use --pollinations-fallback with --auto, or request --use generate.")
            if args.provider == "pexels" and not pexels_enabled():
                raise ValueError("Pexels is disabled (ENABLE_PEXELS=false).")
            providers = [args.provider] if args.provider else [
                name for name in ("nasa", "wikimedia", "pixabay", "pexels")
                if name in {"nasa", "wikimedia"}
                or (name == "pixabay" and secret("PIXABAY_API_KEY"))
                or (name == "pexels" and pexels_enabled() and secret("PEXELS_API_KEY"))
            ]
            selected_asset = cache_best(args.topic, args.query, providers, args.pollinations_fallback)
        elif provider == "pollinations":
            if args.use != "generate":
                parser.error("Use --use generate to request one configured zero-cost AI illustration.")
            selected_asset = cache(args.topic, illustration_request(args.query))
        else:
            if provider == "pexels" and not pexels_enabled():
                raise ValueError("Pexels is disabled (ENABLE_PEXELS=false).")
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
