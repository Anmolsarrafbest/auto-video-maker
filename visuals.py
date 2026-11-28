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
PEXELS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AutoVideoMaker/1.1; +https://github.com/Anmolsarrafbest/auto-video-maker)",
}
COMMONS_INFO = {"action": "query", "prop": "imageinfo", "format": "json", "formatversion": 2,
                "iiprop": "url|mime|size|extmetadata",
                "iiextmetadatafilter": "Artist|LicenseShortName|LicenseUrl|Restrictions|ImageDescription|DateTimeOriginal"}
SHOT_ROLES = ("hook", "explanation", "payoff")
HUMAN_OFFTOPIC_TERMS = {
    "face", "faces", "portrait", "portraits", "eye", "eyes", "iris", "pupil",
    "person", "people", "woman", "women", "man", "men", "girl", "boy",
    "selfie", "model", "human", "humans", "smile", "smiling", "headshot",
}
HUMAN_TOPIC_TERMS = {
    "human", "humans", "person", "people", "brain", "eye", "eyes", "athlete",
    "astronaut", "scientist", "inventor", "pilot", "doctor", "history", "soldier",
    "artist", "historian", "writer", "wright", "katherine", "johnson",
}
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
    response = stock_get(COMMONS_API, {**COMMONS_INFO, "titles": title},
                         headers=COMMONS_HEADERS, provider="wikimedia")
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


def pixabay_cache_path(query, media_type, orientation=""):
    key_parts = [normalize_query(query), media_type]
    if orientation:
        key_parts.append(orientation)
    digest = hashlib.sha256(json.dumps(key_parts).encode()).hexdigest()
    return PIXABAY_CACHE / f"{media_type}-{digest}.json"


def pixabay_get(endpoint, params, media_type):
    path = pixabay_cache_path(params["q"], media_type, params.get("orientation", ""))
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


def stock_get(url, params, headers=None, *, provider=None):
    public_params = {name: value for name, value in params.items() if name != "key"}
    key = hashlib.sha256(json.dumps([url, public_params], sort_keys=True).encode()).hexdigest()
    path = CACHE / "search" / f"{key}.json"
    if path.is_file() and time.time() - path.stat().st_mtime < 86400:
        return json.loads(path.read_text(encoding="utf-8"))
    if provider:
        response, _ = rate_limiter.request_with_backoff(
            "GET", url, provider_id=provider, params=params, headers=headers, timeout=(10, 30))
        response.raise_for_status()
        value = response.json()
    else:
        value = get(url, params=params, headers=headers)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return value


def coverr_candidates(query):
    key = secret("COVERR_API_KEY").strip()
    if not key:
        return
    data = stock_get("https://api.coverr.co/videos",
                     {"query": query, "page_size": 8, "urls": "true", "filter": "is_premium:false,-is_ai_generated:true"},
                     headers={"x-api-key": key}, provider="coverr")
    for item in data.get("hits", []):
        url = item.get("urls", {}).get("mp4", "")
        if item.get("is_premium") or item.get("is_ai_generated") or not url or "{token}" in url:
            continue
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "storage.coverr.co":
            continue
        yield {"id": str(item["id"]), "kind": "video", "provider": "coverr", "title": item["title"],
               "keywords": " ".join(item.get("tags") or []), "url": url,
               "source": "https://coverr.co/videos/" + quote(str(item.get("slug") or item["id"]), safe=""),
               "credit": "Footage from Coverr", "width": item.get("max_width", 0),
               "height": item.get("max_height", 0)}


def internet_archive_candidates(query):
    escaped = re.sub(r'[^\w\s-]', ' ', query)
    result = stock_get("https://archive.org/advancedsearch.php",
                       {"q": f'mediatype:movies AND ({escaped}) AND (licenseurl:*publicdomain* OR rights:"public domain")',
                        "fl[]": ["identifier", "title"], "rows": 5, "output": "json"},
                       headers=COMMONS_HEADERS, provider="internet_archive")
    for item in result.get("response", {}).get("docs", []):
        identifier = item.get("identifier", "")
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", identifier) or identifier in {".", ".."}:
            continue
        try:
            metadata = stock_get(f"https://archive.org/metadata/{identifier}", {},
                                 headers=COMMONS_HEADERS, provider="internet_archive")
        except (requests.RequestException, OSError, ValueError):
            continue
        info = metadata.get("metadata", {})
        license_url = urlparse(str(info.get("licenseurl", "")))
        public_domain = (license_url.hostname in ("creativecommons.org", "www.creativecommons.org")
                         and license_url.path.startswith(("/publicdomain/mark/", "/publicdomain/zero/")))
        if not public_domain and str(info.get("rights", "")).strip().casefold().rstrip(".") != "public domain":
            continue
        files = [file for file in metadata.get("files", []) if file.get("name", "").lower().endswith(".mp4")
                 and str(file.get("size", "")).isdigit() and 0 < int(file["size"]) <= LIMIT
                 and not file.get("private")]
        if files:
            file = max(files, key=lambda entry: int(entry["size"]))
            yield {"id": identifier, "kind": "video", "provider": "internet_archive",
                   "title": str(item.get("title") or identifier),
                   "keywords": str(info.get("subject", "")),
                   "source": f"https://archive.org/details/{identifier}", "credit": "Internet Archive · Public domain",
                   "url": f'https://archive.org/download/{identifier}/{quote(file["name"], safe="")}'}


def loc_candidates(query, media_type=None):
    """Search public-domain Library of Congress collections for archival video or images."""
    target_format = "video" if media_type == "video" else "image"
    data = stock_get(
        "https://www.loc.gov/search/",
        {"q": query, "fo": "json", "c": 8, "fa": f"online-format:{target_format}"},
        headers=COMMONS_HEADERS,
        provider="loc",
    )
    for item in data.get("results", []):
        item_url = str(item.get("id") or item.get("url") or "")
        if not item_url.startswith("https://"):
            continue
        resources = [str(u) for u in (item.get("image_url") or []) if isinstance(u, str) and u.startswith("https://")]
        for res in item.get("resources") or []:
            if isinstance(res, dict):
                for field in ("video", "url", "image"):
                    val = res.get(field)
                    if isinstance(val, str) and val.startswith("https://"):
                        resources.append(val)
        exts = (".mp4", ".mov", ".webm") if target_format == "video" else (".jpg", ".jpeg", ".png")
        asset_url = next((u for u in resources if urlparse(u).path.lower().endswith(exts)), "")
        if not asset_url:
            continue
        yield {
            "id": hashlib.sha256(asset_url.encode()).hexdigest()[:12],
            "kind": target_format,
            "provider": "loc",
            "title": str(item.get("title") or "Library of Congress archive"),
            "keywords": " ".join(str(s) for s in (item.get("subject") or [])),
            "source": item_url,
            "credit": "Library of Congress · Public domain",
            "url": asset_url,
        }


def candidates(query, provider, *, media_type=None):
    if media_type is None:
        yield from candidates(query, provider, media_type="video")
        yield from candidates(query, provider, media_type="image")
        return
    if provider == "loc":
        yield from loc_candidates(query, media_type=media_type)
        return
    if provider in {"coverr", "internet_archive"}:
        if media_type != "image":
            yield from (coverr_candidates(query) if provider == "coverr" else internet_archive_candidates(query))
        return
    if provider == "wikimedia":
        search = f"{query} filetype:video" if media_type == "video" else query
        data = stock_get(COMMONS_API, {**COMMONS_INFO, "generator": "search", "gsrnamespace": 6,
                                     "gsrsearch": search, "gsrlimit": 8}, headers=COMMONS_HEADERS, provider="wikimedia")
        for page in data.get("query", {}).get("pages", []):
            try:
                item = commons_candidate(page)
                if not media_type or item["kind"] == media_type:
                    yield dict(item, provider="wikimedia")
            except ValueError:
                continue
        return
    if provider == "nasa":
        data = stock_get("https://images-api.nasa.gov/search",
                    params={"q": query, "media_type": media_type, "page_size": 8}, provider="nasa")
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
            if media_type and media_type != kind:
                continue
            data = stock_get("https://api.pexels.com/v1/" + endpoint,
                              {"query": query, "per_page": 8},
                              headers={**PEXELS_HEADERS, "Authorization": key}, provider="pexels")
            for item in data[field]:
                if kind == "video":
                    files = [file for file in item["video_files"] if file.get("width") and file.get("height")
                             and file.get("file_type") == "video/mp4"]
                    if not files:
                        continue
                    file = min(files, key=lambda entry: (entry["width"] >= entry["height"],
                                                         abs(min(entry["width"], entry["height"]) - 1080)))
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
        if media_type and media_type != kind:
            continue
        base_params = {"key": key, "q": query[:100], "per_page": 8, "safesearch": "true"}
        if kind == "image":
            param_sets = [
                {**base_params, "image_type": "photo", "min_width": 640, "orientation": "vertical"},
                {**base_params, "image_type": "photo", "min_width": 640},
            ]
        else:
            param_sets = [base_params]
        seen_hits = set()
        hits = []
        for idx, params in enumerate(param_sets):
            if idx > 0 and len(hits) >= 4:
                break
            try:
                batch_hits = pixabay_get(endpoint, params, kind)["hits"]
            except (requests.RequestException, OSError, ValueError, KeyError):
                print(f"pixabay: {kind} search unavailable.", file=sys.stderr)
                continue
            for item in batch_hits:
                if item["id"] not in seen_hits:
                    seen_hits.add(item["id"])
                    hits.append(item)
        for item in hits:
            if kind == "video":
                files = [file for file in item["videos"].values()
                         if file.get("url") and min(file["width"], file["height"]) >= 540
                         and 0 < file["size"] <= LIMIT]
                if not files:
                    continue
                file = min(files, key=lambda entry: (
                    entry["width"] >= entry["height"],
                    min(entry["width"], entry["height"]) < 720,
                    abs(min(entry["width"], entry["height"]) - 1080),
                ))
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
        assets = stock_get(urlparse(collection)._replace(scheme="https").geturl(), {}, provider="nasa")
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
                              headers={**(PEXELS_HEADERS if record.get("provider") == "pexels" else COMMONS_HEADERS),
                                       **record.get("headers", {})}) as response:
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
            "synthetic": bool(record.get("synthetic")), "automatic": bool(record.get("automatic")),
            "match_text": " ".join((record.get("title", ""), record.get("keywords", "")))}


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


def is_off_topic(match, subject_keywords=(), *, title="", category=""):
    """Reject candidates lacking subject_keywords overlap or showing human faces on non-human topics."""
    candidate_words = set(keywords(f"{match.get('title', '')} {match.get('keywords', '')}"))
    subject_terms = set(keywords(" ".join(subject_keywords)))
    topic_context = set(keywords(f"{title} {category}")) | subject_terms
    if (candidate_words & HUMAN_OFFTOPIC_TERMS) and not (topic_context & HUMAN_TOPIC_TERMS):
        if not subject_terms or not (candidate_words & subject_terms):
            return True
    if subject_terms and not (candidate_words & subject_terms):
        return True
    return False


def ranked_matches(query, providers, *, strict=False, event_year="", media_type=None,
                   subject_keywords=(), title="", category=""):
    terms = set(keywords(query))
    if event_year:
        terms.discard(event_year)
    subject_terms = set(keywords(" ".join(subject_keywords)))
    ranked = []
    for provider in providers:
        try:
            matches = list(candidates(query, provider, media_type=media_type))
        except (requests.RequestException, OSError, ValueError, KeyError, TypeError, IndexError):
            print(f"{provider}: search unavailable.", file=sys.stderr)
            continue
        for match in matches:
            if media_type and match["kind"] != media_type:
                continue
            if is_off_topic(match, subject_keywords, title=title, category=category):
                continue
            words = set(keywords(match["title"] + " " + match.get("keywords", "")))
            overlap = len(terms & words)
            subject_overlap = len(subject_terms & words) if subject_terms else 0
            if overlap < min(len(terms), 3 if event_year else 2 if strict else 1):
                continue
            if event_year and not matches_event_year(match, event_year):
                continue
            width, height = match.get("width", 0), match.get("height", 0)
            portrait_hd = width >= 720 and height >= 1280 and width < height
            portrait = width > 0 and height > width
            shape = 3 if portrait_hd else 2 if portrait else 1 if not width else 0
            closeness = -abs(width / height - 9 / 16) if height else -1
            video = match["kind"] == "video"
            vertical_video = video and portrait
            near_target = video and 0 < width < height and closeness >= -.025
            ranked.append(((video, near_target, vertical_video, subject_overlap, overlap, shape, closeness, min(width, 2160)), match))
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
    if isinstance(icon, str):
        icon = " ".join(icon.split()) or None

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
            and all(cached[spec["beat_id"]].get(key) == spec.get(key)
                    for key in ("search_query", "visual_type", "fallback_video_query"))
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


def load_broll_candidates(query="", subject_keywords=()):
    """Load unused local evergreen B-roll entries from broll_manifest.json if present."""
    manifest_path = next(
        (p for p in (Path(__file__).resolve().parent / "broll_manifest.json", CACHE / "broll_manifest.json")
         if p.is_file()),
        None,
    )
    if manifest_path is None:
        return []
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    entries = raw.get("clips", []) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
    terms = set(keywords(query)) | set(keywords(" ".join(subject_keywords)))
    scored = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("file"):
            continue
        file_path = CACHE / Path(entry["file"]).name
        if not file_path.is_file():
            continue
        tags = set(keywords(f"{entry.get('title', '')} {' '.join(entry.get('tags', ()))}"))
        if subject_keywords and not (set(keywords(" ".join(subject_keywords))) & tags):
            continue
        overlap = len(terms & tags) if terms else 0
        scored.append((overlap, {
            "file": file_path.name,
            "kind": entry.get("kind", "video"),
            "source": entry.get("source") or f"broll:{file_path.name}",
            "credit": entry.get("credit") or "Evergreen B-roll archive",
            "synthetic": False,
            "automatic": True,
            "match_text": f"{entry.get('title', '')} {' '.join(entry.get('tags', ()))}",
        }))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [item for _, item in scored]


def cache_storyboard(topic, *, requests_sent=None):
    """Build a multi-asset pool (videos + distinct photos + B-roll) with strict reuse caps."""
    topic_id = topic["id"]
    discovery = topic["discovery"]
    specs = discovery.get("beats")
    subject_keywords = tuple(discovery.get("subject_keywords") or keywords(topic["title"])[:3])
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
    if secret("COVERR_API_KEY"):
        stock.append("coverr")
    archives = ["wikimedia", "internet_archive", "loc"]
    if re.search(r"\b(space|planet|moon|orbit|nasa|astronom\w*|mars|earth|solar|star\w*)\b",
                 topic["category"] + " " + topic["title"], re.I):
        archives.insert(0, "nasa")
    has_manim = find_spec("manim") is not None
    query_cache = {}

    def lookup(search_text, providers, required_year, media_type):
        key = (normalize_query(search_text), tuple(providers), required_year, media_type)
        if key not in query_cache:
            query_cache[key] = ranked_matches(
                search_text, providers, strict=True, event_year=required_year, media_type=media_type,
                subject_keywords=subject_keywords, title=topic["title"], category=topic["category"],
            )
        return query_cache[key]

    def search(query, media_type, *, broad=False):
        if event_year and not broad:
            archive_query = query if event_year in keywords(query) else f"{query} {event_year}"
            actual = lookup(archive_query, archives, event_year, media_type)
            if actual:
                illustrative = lookup(query, stock, "", media_type) if media_type == "video" else []
                return actual + [dict(match, illustrative=True) for match in illustrative]
        providers = [*archives, *stock]
        matches = lookup(query, providers, "", media_type)
        return [dict(match, illustrative=True) for match in matches] if event else matches

    beats_by_id = {beat["beat_id"]: beat for beat in requests_by_beat}
    candidate_pools = {}
    for beat in requests_by_beat:
        index, query, visual_type = (beat[key] for key in ("beat_id", "search_query", "visual_type"))
        if shots.get(index, {}).get("kind") == "video" or not query or visual_type == "callout_card":
            continue
        if visual_type == "diagram" and beat.get("diagram") and has_manim:
            continue
        matches = search(query, "video")
        fallback = beat.get("fallback_video_query")
        if not matches and fallback and normalize_query(fallback) != normalize_query(query):
            matches = search(fallback, "video", broad=True)
        if matches:
            candidate_pools[index] = matches[:5]

    selections = select_relevant_candidates(topic, beats_by_id, candidate_pools, requests_sent=requests_sent)
    used_sources = set()
    used_files = set()
    video_counts = {}
    last_video_pos = {}
    for pos, beat in enumerate(requests_by_beat):
        rec = shots.get(beat["beat_id"])
        if not rec:
            continue
        used_sources.add(rec["source"])
        used_files.add(rec["file"])
        if rec.get("kind") == "video":
            video_counts[rec["source"]] = video_counts.get(rec["source"], 0) + 1
            last_video_pos[rec["source"]] = pos

    def can_use_asset(source, kind, beat_pos, *, allow_second_video=False):
        if kind == "video":
            count = video_counts.get(source, 0)
            if count == 0:
                return True
            if not allow_second_video or count >= 2:
                return False
            prev = last_video_pos.get(source)
            return prev is None or beat_pos - prev >= 3
        return source not in used_sources

    def store_match(index, matches, beat_pos, *, allow_second_video=False):
        for match in islice(matches, 8):
            source = match.get("source", "")
            kind = match.get("kind", "image")
            if not can_use_asset(source, kind, beat_pos, allow_second_video=allow_second_video):
                continue
            try:
                asset = cache(topic_id, dict(match, automatic=True))
            except (requests.RequestException, OSError, ValueError):
                continue
            if kind != "video" and asset["file"] in used_files:
                continue
            if kind == "video" and not allow_second_video and asset["file"] in used_files:
                continue
            if match.get("illustrative"):
                asset["context_label"] = "ILLUSTRATIVE STOCK"
            shots[index] = asset
            used_sources.add(asset["source"])
            used_files.add(asset["file"])
            if asset["kind"] == "video":
                video_counts[asset["source"]] = video_counts.get(asset["source"], 0) + 1
                last_video_pos[asset["source"]] = beat_pos
            return True
        return False

    # Pass 1: assign unique unused videos (plus fallback broad video queries) first.
    for beat_pos, beat in enumerate(requests_by_beat):
        index, query, visual_type = (beat[key] for key in ("beat_id", "search_query", "visual_type"))
        if index in shots or not query or visual_type == "callout_card":
            continue
        if visual_type == "diagram" and beat.get("diagram") and has_manim:
            continue
        choice = selections.get(index, -1)
        if index in candidate_pools and choice >= 0:
            pool = candidate_pools[index]
            ordered = [pool[choice]] + [item for i, item in enumerate(pool) if i != choice]
            store_match(index, ordered, beat_pos)
            fallback = beat.get("fallback_video_query")
            if shots.get(index, {}).get("kind") != "video" and fallback and normalize_query(fallback) != normalize_query(query):
                store_match(index, search(fallback, "video", broad=True), beat_pos)

    # Pass 2: fill remaining beats with distinct subject-relevant photos (max 1 use per image).
    extra_topic_photos = None
    subject_photo_query = " ".join(subject_keywords[:2] or keywords(topic["title"])[:2])
    for beat_pos, beat in enumerate(requests_by_beat):
        index, query, visual_type = (beat[key] for key in ("beat_id", "search_query", "visual_type"))
        if index in shots or not query or visual_type == "callout_card":
            continue
        if visual_type == "diagram" and beat.get("diagram") and has_manim:
            continue
        if not store_match(index, search(query, "image"), beat_pos):
            if subject_photo_query and normalize_query(subject_photo_query) != normalize_query(query):
                if extra_topic_photos is None:
                    extra_topic_photos = search(subject_photo_query, "image", broad=True)
                store_match(index, extra_topic_photos, beat_pos)

    # Pass 3: broll_manifest.json or non-consecutive 2nd video reuse (max 2 total, >=2 beats apart) or AI fallback.
    for beat_pos, beat in enumerate(requests_by_beat):
        index, query, visual_type = (beat[key] for key in ("beat_id", "search_query", "visual_type"))
        if index in shots or not query or visual_type == "callout_card":
            continue
        if visual_type == "diagram" and beat.get("diagram") and has_manim:
            continue
        for broll in load_broll_candidates(query, subject_keywords):
            if can_use_asset(broll["source"], broll["kind"], beat_pos) and broll["file"] not in used_files:
                shots[index] = broll
                used_sources.add(broll["source"])
                used_files.add(broll["file"])
                break
        if index not in shots and index in candidate_pools and selections.get(index, -1) >= 0:
            store_match(index, candidate_pools[index], beat_pos, allow_second_video=True)
        if index not in shots and secret("POLLINATIONS_ZERO_COST_MODEL").strip():
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
    parser.add_argument("--provider", choices=("nasa", "wikimedia", "internet_archive", "coverr",
                                               "pexels", "pixabay", "pollinations"))
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
                name for name in ("nasa", "wikimedia", "internet_archive", "pixabay", "pexels", "coverr")
                if name in {"nasa", "wikimedia", "internet_archive"}
                or (name == "pixabay" and secret("PIXABAY_API_KEY"))
                or (name == "pexels" and pexels_enabled() and secret("PEXELS_API_KEY"))
                or (name == "coverr" and secret("COVERR_API_KEY"))
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
