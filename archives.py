"""Download only source-checked archival images listed in the topic catalogue."""

import io
from pathlib import Path

from PIL import Image, ImageOps
import requests

CACHE = Path(__file__).resolve().parent / "assets-cache"
MAX_BYTES = 12 * 1024 * 1024


def cache_file(topic):
    return CACHE / f'{topic["id"]}.jpg'


def fetch(topic):
    record = topic["archive"]
    path = cache_file(topic)
    if path.is_file():
        return path
    with requests.get(record["image"], stream=True, timeout=(12, 45),
                      headers={"User-Agent": "AutoVideoMaker/1.0 (educational archival images)"}) as response:
        response.raise_for_status()
        buffer = io.BytesIO()
        for chunk in response.iter_content(64 * 1024):
            if buffer.tell() + len(chunk) > MAX_BYTES:
                raise ValueError(f"Archival image exceeds the download limit: {topic['id']}")
            buffer.write(chunk)
    buffer.seek(0)
    CACHE.mkdir(exist_ok=True)
    temporary = path.with_suffix(".part")
    with Image.open(buffer) as original:
        if original.width * original.height > 30_000_000:
            raise ValueError(f"Archival image is too large to decode: {topic['id']}")
        image = ImageOps.exif_transpose(original).convert("RGB")
        image.thumbnail((1400, 1400))
        image.save(temporary, format="JPEG", quality=92)
    temporary.replace(path)
    return path
