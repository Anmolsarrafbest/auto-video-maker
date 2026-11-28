"""Cache and decode the explicitly reviewed footage catalogue."""

from pathlib import Path
import re
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
        with requests.get(CLIPS[topic_id]["url"], stream=True, timeout=(15, 45),
                          headers={"User-Agent": "AutoVideoMaker/1.1 (https://github.com/Anmolsarrafbest/auto-video-maker)"}) as response:
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
    return decode_frames(ffmpeg, cache_file(topic_id), fps, offset=CLIPS[topic_id].get("offset", 0))


def is_portrait(ffmpeg, path):
    info = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    size = re.search(r"Video: [^\r\n]*?\b(\d{2,5})x(\d{2,5})\b", info.stderr)
    return bool(size and int(size[1]) < int(size[2]))


def video_duration(ffmpeg, path):
    info = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    match = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", info.stderr)
    if not match:
        return 0
    hours, minutes, seconds = map(float, match.groups())
    return 3600 * hours + 60 * minutes + seconds


def decode_frames(ffmpeg, path, fps, *, portrait=False, full_bleed=False, offset=0, duration=4):
    if full_bleed:
        width, height = 1080, 1920
        scale_filter = (
            f"fps={fps},scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920"
        )
    else:
        width, height = (720, 1280) if portrait else (1080, 608)
        scale_filter = (
            f"fps={fps},scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
        )
    stride = width * height * 3
    frames = []
    with subprocess.Popen(
        [ffmpeg, "-v", "error", "-ss", str(offset), "-i", str(path), "-t", str(duration),
         "-vf", scale_filter,
         "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE,
    ) as process:
        while data := process.stdout.read(stride):
            if len(data) != stride:
                break
            frames.append(Image.frombytes("RGB", (width, height), data))
        if process.wait():
            raise ValueError(f"Could not decode visual media: {Path(path).name}")
    return frames
