"""Cache and decode the explicitly reviewed footage catalogue."""

from pathlib import Path
import subprocess
import requests
from PIL import Image
from storyboard import CLIPS

CACHE = Path(__file__).resolve().parent / "assets-cache"


def cache_file(topic_id):
    return CACHE / CLIPS[topic_id]["file"]


def setup(topic_id):
    path = cache_file(topic_id)
    CACHE.mkdir(exist_ok=True)
    if not path.is_file():
        temporary = path.with_suffix(".part")
        with requests.get(CLIPS[topic_id]["url"], stream=True, timeout=(15, 45)) as response:
            response.raise_for_status()
            count = 0
            with temporary.open("wb") as output:
                for chunk in response.iter_content(262144):
                    count += len(chunk)
                    if count > 30 * 1024 * 1024:
                        raise ValueError("The reviewed footage exceeded its download limit.")
                    output.write(chunk)
        temporary.replace(path)
    return path


def decode(ffmpeg, topic_id, fps):
    return decode_frames(ffmpeg, cache_file(topic_id), fps)


def decode_frames(ffmpeg, path, fps):
    width, height = 576, 324
    data = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path), "-t", "4",
         "-vf", f"fps={fps},scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
         "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    ).stdout
    stride = width * height * 3
    return [Image.frombytes("RGB", (width, height), data[i:i + stride])
            for i in range(0, len(data) - stride + 1, stride)]
