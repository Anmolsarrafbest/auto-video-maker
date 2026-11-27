"""Draw original science illustrations and encode narrated portrait video."""

import io
import math
from bisect import bisect_right
from functools import lru_cache
from importlib.util import find_spec
import os
import random
import re
import shutil
import subprocess
import wave

from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont, ImageOps

from hindi_captions import ass_filter, build_word_timeline, topic_badge
from audio_master import master
from local_secrets import secret
from local_voices import windows_pcm
from production import HINDI, INDIAN_ENGLISH, SCORES, treatment
from storyboard import CLIPS, episode, resolve_visual_timeline
import footage

WIDTH, HEIGHT = 720, 1280
OUTPUT_SIZE = (1080, 1920)
FPS = int(secret("SHORTS_FPS") or "30")
if FPS not in (30, 60):
    raise ValueError("SHORTS_FPS must be 30 or 60.")
END_CARD = "Follow and subscribe"
OUTRO_SECONDS = 2
ANSWER_PAUSE = 1.5
PALETTES = {
    "observatory": ((9, 14, 29), (10, 12, 21), "#405273", "#0D1527", 30),
    "archive": ((27, 19, 20), (25, 17, 12), "#705040", "#251B1B", 8),
    "lab": ((8, 26, 26), (9, 20, 15), "#376761", "#102824", 13),
    "ocean": ((6, 20, 34), (6, 28, 29), "#37657B", "#0B2537", 42),
    "copper": ((37, 20, 16), (66, 36, 23), "#C68458", "#44271C", 10),
    "paper": ((52, 36, 32), (89, 67, 49), "#C9A880", "#49362B", 4),
}

DELIVERY = {
    "documentary": (1.05, .50, .36),
    "precise": (.94, .42, .23),
    "curious": (.99, .57, .42),
    "gentle": (1.08, .46, .32),
}


def think_line(topic):
    prompts = ("Think about it.", "Take a guess.", "What’s your hunch?", "Any guesses?")
    return random.Random(topic.get("id", "") + ":prompt").choice(prompts)


def spoken_plan(topic, beats):
    """Share the audio and caption schedule for a topic's editorial format."""
    style = topic.get("format", "quiz")
    # A stable choice keeps the audio, captions and export in sync.
    natural = topic.get("id") == "jupiter-spin" or (
        style in {"quiz", "myth"} and random.Random(topic.get("id", "") + ":hook").random() < .4
    )
    episode_plan = topic.get("episode_plan")
    plan = [] if natural and not episode_plan else [("hook", beats[0], .14)]
    question = episode_plan["opening"] == "question" if episode_plan else style == "quiz" and not natural
    if question:
        plan.append(("pause", think_line(topic), .18))
    parts = re.split(r"(?<=[a-z0-9]\.)(?=\s+[A-Z])\s+|(?<=।)\s+", beats[1])
    # Keep brief replies such as “No.” attached to the explanation.
    if len(parts) > 1 and len(parts[0].split()) < 4:
        parts[:2] = [" ".join(parts[:2])]
    reveal_gap = ANSWER_PAUSE if question else .25
    return plan + [("answer", text, reveal_gap if i == 0 else .1) for i, text in enumerate(parts)]


def font(size, bold=False, serif=False):
    names = ("arialbd.ttf", "DejaVuSans-Bold.ttf") if bold else ("arial.ttf", "DejaVuSans.ttf")
    if serif:
        names = ("timesbd.ttf", "DejaVuSerif-Bold.ttf") + names
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


def motion_cues(topic, beats, timings, seconds):
    reveal = next(start for (role, _, _), (start, _) in zip(spoken_plan(topic, beats), timings)
                  if role == "answer")
    return (reveal, seconds if topic.get("loop") else seconds - OUTRO_SECONDS)


def wrap(text, face, width):
    lines = [""]
    for word in text.split():
        line = (lines[-1] + " " + word).strip()
        if lines[-1] and face.getlength(line) > width:
            lines.append(word)
        else:
            lines[-1] = line
    return "\n".join(lines)


def circle(draw, x, y, radius, fill=None, outline=None, width=1):
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill, outline, width)


def ease_cubic(value):
    value = max(0, min(1, value))
    return 4 * value ** 3 if value < .5 else 1 - (-2 * value + 2) ** 3 / 2


@lru_cache(maxsize=8)
def _radial_canvas(size, color):
    import numpy as np

    width, height = size
    rgb = ImageColor.getrgb(color) if isinstance(color, str) else color
    y = np.linspace(-1.0, 1.0, height, dtype=np.float32)[:, None]
    x = np.linspace(-1.0, 1.0, width, dtype=np.float32)[None, :]
    glow = np.clip(1.0 - np.hypot(x * 0.9, (y + 0.08) * 1.1), 0.0, 1.0) ** 1.6
    lift = np.array([18.0, 22.0, 30.0], dtype=np.float32)
    pixels = np.clip(np.array(rgb, dtype=np.float32) + glow[..., None] * lift, 0, 255).astype(np.uint8)
    return Image.fromarray(pixels, mode="RGB")


@lru_cache(maxsize=8)
def _rounded_mask(size, radius=32):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    return mask


@lru_cache(maxsize=8)
def _frame_shadow(size):
    inset = 30
    shadow = Image.new("L", (size[0] + 2 * inset, size[1] + 2 * inset))
    shadow.paste(_rounded_mask(size).point(lambda value: value * .6), (inset, inset))
    return shadow.filter(ImageFilter.GaussianBlur(10)), inset


class SupersampledShapes:
    """Scale the existing illustration coordinates onto an antialiased art plane."""

    scale = 3
    top = 280

    def __init__(self, color):
        self.image = _radial_canvas((WIDTH * self.scale, 520 * self.scale), color).copy()
        self.draw = ImageDraw.Draw(self.image)

    def points(self, coords):
        if isinstance(coords[0], (tuple, list)):
            return [tuple(self.points(point)) for point in coords]
        return tuple(round((value - (self.top if i % 2 else 0)) * self.scale)
                     for i, value in enumerate(coords))

    def ellipse(self, box, fill=None, outline=None, width=1):
        self.draw.ellipse(self.points(box), fill, outline, width * self.scale)

    def arc(self, box, start, end, *, fill, width=1):
        self.draw.arc(self.points(box), start, end, fill, width * self.scale)

    def polygon(self, points, *, fill):
        self.draw.polygon(self.points(points), fill)

    def line(self, points, *, fill, width=1):
        self.draw.line(self.points(points), fill, width * self.scale, joint="curve")


def camera_shot(image, size, time, duration, zoom=.12, *, reframe=0):
    motion = ease_cubic(time / max(.1, duration))
    aspect = size[0] / size[1]
    width = min(image.width, image.height * aspect) / (1 + .16 * bool(reframe) + zoom * motion)
    height = width / aspect
    x = (image.width - width) * (.65 - .3 * motion if reframe % 2 else .35 + .3 * motion)
    y = (image.height - height) * (.58 if reframe % 2 else .42)
    return image.transform(size, Image.Transform.EXTENT, (x, y, x + width, y + height),
                           Image.Resampling.BICUBIC)


@lru_cache(maxsize=4)
def visual_shade(size):
    mask = Image.new("L", (1, size[1]))
    mask.putdata([round(72 * max(0, (row / max(1, size[1] - 1) - .55) / .45) ** 2)
                  for row in range(size[1])])
    return mask.resize(size)


def native_coordinates(coords):
    scale = OUTPUT_SIZE[0] / WIDTH
    if isinstance(coords[0], (tuple, list)):
        return [native_coordinates(point) for point in coords]
    return tuple(round(value * scale) for value in coords)


def native_font(size, bold=False):
    return font(round(size * OUTPUT_SIZE[0] / WIDTH), bold)


class NativeOverlay:
    """Draw UI coordinates directly on the output canvas."""

    def __init__(self, image):
        self.draw = ImageDraw.Draw(image)

    def text(self, point, text, **kwargs):
        self.draw.text(native_coordinates(point), text, **kwargs)

    def multiline_text(self, point, text, **kwargs):
        self.draw.multiline_text(native_coordinates(point), text, **kwargs)

    def rectangle(self, coords, **kwargs):
        self.draw.rectangle(native_coordinates(coords), **kwargs)

    def polygon(self, points, *, fill):
        self.draw.polygon(native_coordinates(points), fill=fill)

    def rounded_rectangle(self, coords, *, radius, width=1, **kwargs):
        scale = OUTPUT_SIZE[0] / WIDTH
        self.draw.rounded_rectangle(native_coordinates(coords), radius=round(radius * scale),
                                    width=round(width * scale), **kwargs)

    def line(self, coords, *, fill, width=1):
        self.draw.line(native_coordinates(coords), fill=fill, width=round(width * OUTPUT_SIZE[0] / WIDTH))

    def ellipse(self, coords, *, outline, width=1):
        self.draw.ellipse(native_coordinates(coords), outline=outline,
                          width=round(width * OUTPUT_SIZE[0] / WIDTH))


def place_visual(shot, position, time, *, portrait=False):
    opening_scale = ease_cubic(time / .075) if time < .075 else 1 - ease_cubic((time - .075) / .225)
    punch = .09 * opening_scale
    if portrait:
        img = camera_shot(shot, OUTPUT_SIZE, 1, 1, zoom=punch) if punch else shot
        img.paste((8, 12, 23), (0, 0, *OUTPUT_SIZE), visual_shade(OUTPUT_SIZE))
        return img
    small = ImageOps.fit(shot, (270, 480), Image.Resampling.BILINEAR)
    blur = small.filter(ImageFilter.GaussianBlur(18))
    backdrop = Image.blend(blur, Image.new("RGB", blur.size, (8, 12, 23)), .55)
    img = backdrop.resize(OUTPUT_SIZE, Image.Resampling.BILINEAR)
    if punch:
        shot = camera_shot(shot, shot.size, 1, 1, zoom=punch)
    framed_size = (round(shot.width * .92), round(shot.height * .92))
    x = position[0] + (shot.width - framed_size[0]) // 2
    y = position[1] + (shot.height - framed_size[1]) // 2
    shot = shot.resize(framed_size, Image.Resampling.LANCZOS)
    shadow, inset = _frame_shadow(framed_size)
    img.paste((0, 0, 0), (x - inset, y - inset + 10, x - inset + shadow.width, y - inset + 10 + shadow.height), shadow)
    img.paste(shot, (x, y), _rounded_mask(framed_size))
    return img


@lru_cache(maxsize=72)
def lunar_sphere(phase):
    """Project a lit, rotating procedural sphere. This is a diagram, not a lunar map."""
    image = Image.new("RGBA", (84, 84))
    pixels = image.load()
    for row in range(84):
        ny = (row - 41.5) / 40
        for column in range(84):
            nx = (column - 41.5) / 40
            radius_squared = nx * nx + ny * ny
            if radius_squared <= 1:
                nz = math.sqrt(1 - radius_squared)
                longitude = math.atan2(nx, nz) + phase * .08
                texture = 10 * math.sin(longitude * 8 + ny * 9) + 5 * math.cos(longitude * 19 - ny * 16)
                illumination = max(0, -.56 * nx - .28 * ny + .76 * nz)
                gray = round(52 + 156 * illumination + texture)
                pixels[column, row] = (gray, gray, min(255, gray + 11), 255)
    return image


def illustration(draw, scene, accent, time, beat):
    x, y = 348, 542
    muted = "#33496C"
    if scene == "orbit":
        draw.ellipse((121, y - 105, 575, y + 105), outline=muted, width=3)
        circle(draw, x, y, 56, "#FFB65C")
        circle(draw, x - 12, y - 14, 24, "#FFD98C")
        theta = time * .82 - 1
        px, py = x + 226 * math.cos(theta), y + 105 * math.sin(theta)
        circle(draw, px, py, 32, "#D99164")
        draw.arc((px - 32, py - 32, px + 32, py + 32), 110, 250, fill=accent, width=5)
    elif scene in {"eclipse", "distance"}:
        offset = (30 * (1 - math.cos(time * .4))) if scene == "distance" else 36 * (1 - beat)
        if scene == "eclipse":
            draw.polygon([(255, y - 65), (546, y - 27), (546, y + 27), (255, y + 65)], fill="#291D37")
            for dy in (-64, 64):
                draw.line((130, y + dy, 244, y + dy, 531, y + dy * .18), fill="#B36560", width=3)
            circle(draw, 125, y, 42, "#FFBD69")
        else:
            draw.line((248, y, 477 + offset, y), fill=muted, width=4)
        circle(draw, 255 if scene == "eclipse" else 198, y, 66, "#3976B3", "#85CEF1", 3)
        earth_x = 255 if scene == "eclipse" else 198
        draw.arc((earth_x - 48, y - 54, earth_x + 46, y + 53), 105, 284, fill="#63CEBA", width=10)
        circle(draw, 477 + offset, y, 40, accent)
        circle(draw, 466 + offset, y - 9, 11, "#8A709C" if scene == "distance" else "#B9574D")
    elif scene == "white-dwarf":
        radius = 89 - 15 * min(1, time / 7)
        for glow in range(5, 0, -1):
            circle(draw, x, y, radius + glow * 10, (30 + (5 - glow) * 13, 27 + (5 - glow) * 14, 58 + (5 - glow) * 17))
        circle(draw, x, y, radius, "#E9E8FF", accent, 4)
        if beat:
            for angle in (0, math.pi / 2, math.pi, math.pi * 1.5):
                dx, dy = math.cos(angle), math.sin(angle)
                ax, ay = x + dx * (radius + 20), y + dy * (radius + 20)
                draw.line((x + dx * 151, y + dy * 151, ax, ay), fill=accent, width=5)
                draw.polygon([(ax, ay), (ax + dx * 15 - dy * 8, ay + dy * 15 + dx * 8),
                              (ax + dx * 15 + dy * 8, ay + dy * 15 - dx * 8)], fill=accent)
    elif scene == "trajectory":
        draw.ellipse((143, y - 116, 553, y + 116), outline="#587294", width=3)
        circle(draw, x, y, 75, "#295887", "#8ABCCD", 3)
        draw.arc((x - 53, y - 53, x + 50, y + 51), 24, 157, fill="#6FC0A6", width=12)
        angle = time * .7
        px, py = x + 205 * math.cos(angle), y + 116 * math.sin(angle)
        draw.polygon([(px, py - 13), (px + 13, py + 10), (px - 13, py + 10)], fill=accent)
        if beat:
            for dx in (-178, 172):
                draw.line((x + dx, y + 85, x + dx + 10, y + 94, x + dx + 24, y + 74),
                          fill="#75E2C1", width=4)
    elif scene == "flight":
        draw.line((107, 638, 595, 638), fill="#9A8068", width=5)
        for step in range(118, 594, 32):
            draw.line((step, 638, step, 650), fill="#9A8068", width=3)
        px = 150 + min(1, time / 6) * 370
        py = 588 - 105 * math.sin(math.pi * min(1, time / 6))
        for offset in (-21, 13):
            draw.line((px - 56, py + offset, px + 57, py + offset), fill=accent, width=8)
        for offset in (-41, 36):
            draw.line((px + offset, py - 22, px + offset, py + 15), fill="#D59E73", width=4)
        draw.line((px - 22, py + 17, px + 45, py + 17, px + 64, py + 5), fill="#F2E0C0", width=4)
    elif scene == "cepheid":
        for center, cycle, size in ((211, 1.8, 39), (480, 3.5, 56)):
            phase = math.sin(time * math.tau / cycle)
            circle(draw, center, y - 38, size + phase * 13, "#403865")
            circle(draw, center, y - 38, size * .61 + phase * 10, accent if center == 480 else "#A391D2")
            points = [(center - 84 + i, y + 105 - 23 * math.sin((time + i / 58) * math.tau / cycle))
                      for i in range(169)]
            draw.line(points, fill=accent, width=3)
    elif scene == "raman":
        draw.line((100, y, x - 35, y), fill=accent, width=6)
        for dx, dy in ((-23, 12), (0, -18), (25, 11)):
            circle(draw, x + dx, y + dy, 16, "#28697D", "#D6FFF7", 2)
        for ray in (-1, 1):
            draw.line((x + 39, y, 584, y + ray * 95),
                      fill="#F8B475" if beat and ray == 1 else accent, width=4)
            phase = (time * 1.4) % 1
            circle(draw, x + 39 + phase * 197, y + ray * 95 * phase, 5, "#F8F3D5")
    elif scene == "coral":
        draw.ellipse((128, 628, 569, 672), fill="#233654")
        for i, (dx, height) in enumerate(((-150, 72), (-92, 169), (-42, 121), (20, 203), (76, 156), (134, 95))):
            color = ("#D47D7C", "#FF9B8D", "#E26480")[i % 3]
            tail = (x + dx * .35, 647)
            tip = (x + dx + math.sin(time * 1.5 + i) * 5, 647 - height)
            mid = ((tail[0] + tip[0]) / 2, (tail[1] + tip[1]) / 2)
            for point, width in ((tip, 15), ((mid[0] + 37, mid[1] - 51), 10),
                                 ((mid[0] - 41, mid[1] - 34), 9)):
                draw.line((tail, mid, point), fill=color, width=width)
                circle(draw, *point, width / 2, accent if beat else color)
    elif scene == "hearts":
        for i, px in enumerate((195, 348, 501)):
            pulse = 1 + .09 * math.sin(time * 7 - i)
            color = accent if beat else muted
            points = []
            for j in range(65):
                t = math.tau * j / 64
                points.append((px + pulse * 3.5 * 16 * math.sin(t) ** 3,
                               y - pulse * 3.5 * (13 * math.cos(t) - 5 * math.cos(2 * t)
                                                  - 2 * math.cos(3 * t) - math.cos(4 * t))))
            draw.polygon(points, fill=color)
            draw.line((px, y + 69, px, y + 113), fill=color, width=3)
    elif scene == "double":
        colors = ("#F68082", "#FFC17D", "#F9E98D", "#75DFC5", "#73ABFF", "#B199FF")
        for index, color in enumerate(colors):
            for radius, tone in ((219 - index * 9, color), (301 - index * 9, colors[-index - 1])):
                draw.arc((x - radius, y + 112 - radius, x + radius, y + 112 + radius),
                         195, min(347, 274 + int(time * 26)), fill=tone, width=7)
    elif scene == "scatter":
        circle(draw, 137, y, 42, "#FFCB79")
        draw.line((179, y, x, y), fill="#FFD8A1", width=5)
        for i in range(10):
            angle = (i - 4.5) * .42
            for distance in range(25, 177, 18):
                radius = distance + (time * 28 + i * 3) % 18
                circle(draw, x + math.cos(angle) * radius, y + math.sin(angle) * radius,
                       3, accent if beat or i % 3 else "#F59480")
        circle(draw, x, y, 14, "#C0E2FF", muted, 2)
    elif scene == "rings":
        for i in range(5):
            radius = 198 - i * 15
            draw.ellipse((x - radius, y - 60 + i * 5, x + radius, y + 60 - i * 5),
                         outline=("#816D69", "#D0A888", "#EDD6A8")[i % 3], width=4)
            theta = (i + 2) * time * .26
            circle(draw, x + radius * math.cos(theta), y + (60 - i * 5) * math.sin(theta), 4, "#FFF0BB")
        circle(draw, x, y, 88, "#D6A06D", accent, 3)
        for i in range(5):
            radius = 198 - i * 15
            draw.arc((x - radius, y - 60 + i * 5, x + radius, y + 60 - i * 5),
                     0, 180, fill=("#816D69", "#D0A888", "#EDD6A8")[i % 3], width=4)
    elif scene in {"tilt", "sideways"}:
        sideways = scene == "sideways"
        circle(draw, x, y, 101, "#294B63" if sideways else "#966348", accent, 3)
        dx, dy = (126, -18) if sideways else (7, -128)
        draw.line((x - dx, y - dy, x + dx, y + dy), fill=accent, width=4)
        for index in range(4):
            offset = index * 17 - 26 + 5 * math.sin(time * 2 + index)
            if sideways:
                draw.arc((x - 28 + offset, y - 89, x + 28 + offset, y + 89), 95, 270, fill="#6EC9D6", width=3)
            else:
                draw.arc((x - 89, y - 38 + offset, x + 89, y + 38 + offset), 10, 170, fill="#E5B48B", width=4)
    elif scene == "sunset":
        sun_y = y - 69 + 80 * ease_cubic(time / 11)
        for layer in range(10, 0, -1):
            tone = (40 + (10 - layer) * 6, 54 + (10 - layer) * 8, 83 + (10 - layer) * 12)
            circle(draw, x, sun_y, 30 + layer * 9, tone)
        circle(draw, x, sun_y, 28, "#DCEAFF")
        draw.polygon([(75, 637), (170, 584), (226, 599), (317, 570), (419, 601),
                      (501, 582), (622, 633), (622, 673), (75, 673)], fill="#9B5B4E")
        draw.polygon([(75, 656), (206, 623), (343, 659), (483, 622), (622, 644),
                      (622, 677), (75, 677)], fill="#4D3748")
    else:
        circle(draw, x, y, 118, "#1B3153", "#80AFE7", 3)
        draw.line((103, y - 94, x - 104, y - 52, x + 102, y + 51), fill="#FFE5AC", width=6)
        for i, color in enumerate(("#FF8188", "#FFC878", "#F6E890", "#76E2C5", "#75AAFF", "#BF99FF")):
            end_x = 135 + int(10 * math.sin(time * 2))
            draw.line((x + 102, y + 51, x - 17, y + 116, end_x, y + 104 + i * 14),
                      fill=color, width=3)


def discovery_canvas(style, base, accent, time, label, phase, icon=None):
    img = Image.new("RGB", OUTPUT_SIZE, base)
    draw = NativeOverlay(img)
    ink = tuple(round(a * .22 + b * .78) for a, b in zip(accent, base))
    enter = ease_cubic(phase / .55)
    y = 530 + round(30 * (1 - enter))
    if style == "field-notes":
        for row in range(230, 810, 34):
            draw.line((90, row, 630, row), fill=ink, width=1)
        draw.line((95, 210, 95, 806), fill=accent, width=3)
        draw.rectangle((116, y - 94, 624, y + 98), fill=base)
    elif style == "waveform":
        for band in range(5):
            points = [(x, 345 + band * 90 + math.sin(x / 105 + time * 1.4 + band) * 32)
                      for x in range(0, 721, 12)]
            draw.line(points, fill=ink, width=3)
        draw.rectangle((70, y - 84, 650, y + 84), fill=base)
    elif style == "cut-paper":
        offset = round(18 * math.sin(time * .9))
        draw.polygon(((0, 290), (720, 220 + offset), (720, 413), (0, 500)), fill=ink)
        draw.polygon(((0, 633), (720, 545), (720, 740 + offset), (0, 810)), fill=ink)
        draw.rectangle((58, y - 98, 662, y + 98), fill=base)
    elif style == "spotlight":
        for radius in range(304, 110, -22):
            level = (304 - radius) / 194
            fill = tuple(round(a * level + b * (1 - level)) for a, b in zip(ink, base))
            draw.ellipse((360 - radius, y - radius, 360 + radius, y + radius), outline=fill, width=18)
        draw.rectangle((90, y - 82, 630, y + 82), fill=base)
    else:
        for column in range(70, 651, 48):
            for row in range(225, 807, 48):
                size = 2 + 2 * (1 + math.sin(time * 2 - column / 90 - row / 120))
                draw.ellipse((column - size, row - size, column + size, row + size), outline=ink, width=3)
        draw.rounded_rectangle((64, y - 105, 656, y + 105), radius=24, fill=base, outline=ink, width=3)
    face = native_font(round(54 + 8 * math.sin(math.pi * enter)), True)
    if icon is not None:
        x, top = native_coordinates((360, 225 + round(22 * (1 - enter))))
        img.paste(accent, (x - icon.width // 2, top, x - icon.width // 2 + icon.width, top + icon.height), icon)
    draw.multiline_text((360, y), wrap(label, face, 720), font=face, anchor="mm",
                        align="center", fill=accent)
    return img


def frames(topic, beats, timings, seconds, archive_path=None, clip=(), diagram=(),
           *, visual_timeline=(), storyboard=None, load_visual=None, load_diagram=None, icon=None, credit_icon=None):
    accent = ImageColor.getrgb(topic["color"])
    theme = treatment(topic)["theme"]
    base, _, border, caption_fill, radius = PALETTES[theme]
    hindi = topic.get("language") == "hi"
    plan = spoken_plan(topic, beats)
    badge = topic_badge(topic)
    badge_font = native_font(25, True)
    badge_width = badge_font.getlength(badge) / 1.5
    photograph = None
    if archive_path:
        with Image.open(archive_path) as source:
            photograph = ImageOps.contain(source.convert("RGB"), (1080, 1080), Image.Resampling.LANCZOS)
    layout = episode(topic)[1]
    starts = [start for start, _ in timings]
    takeaway_font = native_font(35, True)
    label_font = native_font(23, True)
    reveal, outro = motion_cues(topic, beats, timings, seconds)
    answer_gap = next(gap for role, _, gap in plan if role == "answer")
    pause_start = reveal - answer_gap
    recap_start = timings[-1][1] + .15
    recap_steps = ("Light bends as it enters", "It reflects once inside",
                   "The colors exit at different angles")
    motion = topic.get("episode_plan", {}).get("motion", "underline")
    archive_duration = seconds * topic.get("archive_fraction", .73)
    contextual_clip = bool(clip and CLIPS.get(topic["id"], {}).get("start_on_last_line"))
    clip_start = timings[-1][0] if contextual_clip else 0
    coordinate_scale = OUTPUT_SIZE[0] / WIDTH
    discovery_style = topic.get("discovery_style") if visual_timeline else None
    storyboard = storyboard or {}
    shot_starts = [segment["start_time"] for segment in visual_timeline]
    card_styles = ("field-notes", "waveform", "cut-paper", "spotlight", "grid")
    starting_style = card_styles.index(discovery_style) if discovery_style in card_styles else 0
    previous_photos, labels = [], []
    previous_photo = None
    for segment in visual_timeline:
        key = segment["beat_id"]
        previous_photos.append(previous_photo)
        if storyboard.get(key, {}).get("kind") == "image":
            previous_photo = key
        labels.append(segment.get("callout_stat") or segment.get("search_query")
                      or " ".join(segment["anchor_text"].split()[:5]))

    def native_size(width, height):
        return round(width * coordinate_scale), round(height * coordinate_scale)

    def discovery_shot(slot, time):
        segment = visual_timeline[slot]
        elapsed = time - segment["start_time"]
        duration = segment["end_time"] - segment["start_time"]
        key, subpart = segment["beat_id"], segment["part"]
        diagram_frames = ()
        if segment.get("visual_type") == "diagram":
            diagram_frames = load_diagram(key) if segment.get("diagram") and load_diagram else ()
            if diagram_frames:
                segment["diagram_template"] = segment["diagram"]["template"]
            diagram_frames = diagram_frames or diagram
        use_diagram = bool(diagram_frames)
        record = load_visual(key) if key in storyboard and not use_diagram else None
        classification = record["kind"] if record else ""
        if use_diagram:
            sequence, portrait, classification = diagram_frames, False, "diagram"
        elif not record:
            reused = previous_photos[slot]
            same_label = slot and labels[slot].casefold() == labels[slot - 1].casefold()
            if reused is not None and same_label:
                record = load_visual(reused)
                classification = "reframe" if record else ""
            if not record:
                if icon and credit_icon:
                    credit_icon()
                style = card_styles[(starting_style + slot) % len(card_styles)]
                label = labels[slot]
                if same_label and not segment.get("callout_stat"):
                    label = " ".join(segment["anchor_text"].split()[:5])
                segment["rendered_as"] = "callout_card"
                return discovery_canvas(style, base, accent, time, label, elapsed, icon), None
            sequence, portrait = record["images"], record["portrait"]
        else:
            sequence, portrait = record["images"], record["portrait"]
        segment["rendered_as"] = classification
        prior_id = previous_photos[slot]
        prior_file = storyboard.get(prior_id, {}).get("path")
        repeated_photo = bool(classification == "image" and prior_file and prior_file == record["path"])
        framing = subpart or int(classification == "reframe" or repeated_photo)
        if framing and not use_diagram:
            segment["reframed"] = True
        position = min(1, max(0, elapsed / max(.1, duration))) * (len(sequence) - 1)
        index = int(position)
        source = sequence[index]
        if index + 1 < len(sequence) and position > index:
            source = Image.blend(source, sequence[index + 1], position - index)
        size = OUTPUT_SIZE if portrait else native_size(WIDTH, 405 if use_diagram else 490)
        shot = camera_shot(source, size, elapsed, duration, zoom=0 if use_diagram else .12,
                           reframe=0 if use_diagram else framing)
        return (place_visual(shot, native_size(0, 294), time, portrait=portrait),
                record.get("context_label") if record else None)

    total_frames = max(1, round(seconds * FPS))
    for index in range(total_frames):
        time = index / FPS
        recap = time >= recap_start
        replay = topic["id"] == "rainbow-split" and bool(diagram) and outro - recap_start >= 2.4 and recap
        replay_progress = min(1, max(0, (time - recap_start) / max(.1, outro - recap_start)))
        part = max(0, bisect_right(starts, time) - 1)
        beat = int(plan[part][0] == "answer")
        show_clip = bool(clip) and (time >= clip_start if contextual_clip else time < archive_duration)
        archive_shot = show_clip or (photograph is not None and time < archive_duration)
        opacity = (ease_cubic((time - clip_start) / .45) if contextual_clip and show_clip
                   else ease_cubic((archive_duration - time) / .65) if archive_shot else 0)
        active_segment = None
        visual_context = None
        if opacity < 1 and discovery_style:
            slot = bisect_right(shot_starts, time) - 1
            active_segment = visual_timeline[slot]
            img, visual_context = discovery_shot(slot, time)
            transition = min(.35, (active_segment["end_time"] - active_segment["start_time"]) / 2)
            blend = ease_cubic((time - shot_starts[slot]) / transition)
            if slot and blend < 1:
                outgoing, outgoing_context = discovery_shot(slot - 1, time)
                img = Image.blend(outgoing, img, blend)
                if blend < .5:
                    active_segment, visual_context = visual_timeline[slot - 1], outgoing_context
        elif opacity < 1 and diagram and beat:
            diagram_frame = min(len(diagram) - 1, max(0, round((time - reveal) * FPS)))
            if replay:
                diagram_frame = round(replay_progress * (len(diagram) - 1))
            shot = camera_shot(diagram[diagram_frame], native_size(WIDTH, 456),
                               time - reveal, max(1, seconds - reveal), zoom=.07)
            img = place_visual(shot, native_size(0, 300), time)
        elif opacity < 1:
            if layout in {"space film", "experiment"}:
                stage = SupersampledShapes(base)
                illustration(stage, topic["scene"], accent, time, beat)
                if topic["scene"] == "distance":
                    moon = lunar_sphere(time * 4)
                    offset = 30 * (1 - math.cos(time * .4))
                    moon = moon.resize((252, 252), Image.Resampling.LANCZOS)
                    stage.image.paste(moon, stage.points((477 + offset - 42, 500)), moon)
                push_start = min(recap_start, seconds - 3)
                progress = min(1, max(0, (time - push_start) / max(1.2, outro - push_start)))
                ease = ease_cubic(progress)
                wide = (62, 364 if beat else 315, 645, 715 if beat else 750)
                bounds = tuple(a + (b - a) * ease for a, b in zip(wide, (96, 384, 625, 689)))
                shot = ImageOps.fit(stage.image.crop(stage.points(bounds)),
                                    native_size(WIDTH, 490), Image.Resampling.LANCZOS)
                img = place_visual(shot, native_size(0, 313 if layout == "space film" else 330), time)
            else:
                stage = SupersampledShapes(base)
                illustration(stage, topic["scene"], accent, time, beat)
                shot = ImageOps.fit(stage.image.crop(stage.points((54, 319, 646, 752))),
                                    native_size(WIDTH, 490), Image.Resampling.LANCZOS)
                img = place_visual(shot, native_size(0, 306), time)
        if archive_shot:
            if show_clip:
                clip_duration = seconds - clip_start if contextual_clip else archive_duration
                frame = min(len(clip) - 1, int((time - clip_start) * len(clip) / clip_duration))
                source = clip[frame]
            else:
                source = photograph
            portrait = source.height > source.width
            size = OUTPUT_SIZE if portrait else native_size(WIDTH, 405)
            shot = camera_shot(source, size, time, archive_duration, zoom=.08)
            archive_canvas = place_visual(shot, native_size(0, 310), time, portrait=portrait)
            img = archive_canvas if opacity == 1 else Image.blend(img, archive_canvas, opacity)
        draw = NativeOverlay(img)
        if not hindi:
            draw.rounded_rectangle((52, 124, 86 + badge_width, 173), radius=20, fill="#101A2B", outline=accent)
            draw.text((69, 149), badge, font=badge_font, fill=accent, anchor="lm")
        if visual_context:
            draw.text((360, 748), visual_context, font=label_font,
                      fill="#F7F3EA", anchor="mm", stroke_width=2, stroke_fill="#121827")
        if active_segment and active_segment["rendered_as"] != "callout_card" and active_segment.get("callout_stat"):
            draw.text((360, 232), active_segment["callout_stat"], font=takeaway_font,
                      fill="#F7F3EA", anchor="mm", stroke_width=2, stroke_fill="#121827")
        if contextual_clip and show_clip and opacity > .5:
            draw.text((360, 748), CLIPS[topic["id"]]["label"], font=label_font,
                      fill="#F7F3EA", anchor="mm", stroke_width=2, stroke_fill="#121827")
        if beat and not discovery_style and not recap and not hindi and not (archive_shot and (clip or photograph is not None)):
            label = topic["labels"][1]
            draw.text((348, 710), label, font=label_font if label_font.getlength(label) < 825 else native_font(20, True),
                      fill=accent, anchor="mm", stroke_width=2, stroke_fill="#121827")
            if topic["scene"] == "distance":
                draw.text((348, 755), "ILLUSTRATION · NOT TO SCALE", font=native_font(17, True), fill=accent, anchor="mm")
        caption, face = "", takeaway_font
        if recap:
            takeaway = topic["labels"][1]
            if replay:
                step = bisect_right((1.4, 2.2), diagram_frame / FPS)
                takeaway = recap_steps[step]
            caption = wrap(takeaway, face, 789)
        if caption:
            if layout in {"space film", "experiment"} and not hindi:
                draw.rectangle((53, 820, 60, 905), fill=accent)
            else:
                draw.rounded_rectangle((54, 810, 642, 916), radius=radius, fill=caption_fill, outline=border, width=2)
            if not hindi:
                draw.multiline_text((348, 865), caption, font=face,
                                    fill="#F7F3EA", anchor="mm", align="center", spacing=7)
        if answer_gap == ANSWER_PAUSE and pause_start <= time < reveal:
            remaining = (reveal - time) / answer_gap
            draw.rounded_rectangle((172, 922, 548, 929), radius=3, fill="#243348")
            draw.rectangle((172, 922, 172 + 376 * remaining, 929), fill=accent)
        if reveal <= time < reveal + .9 and not hindi:
            progress = min(1, (time - reveal) / .4)
            span = round(185 * (1 - (1 - progress) ** 3))
            if motion == "pulse":
                radius = 4 + round(19 * progress)
                draw.ellipse((360 - radius, 790 - radius, 360 + radius, 790 + radius),
                             outline=accent, width=3)
            elif motion == "brackets":
                for side in (-1, 1):
                    x = 360 + side * (245 - span / 5)
                    draw.line(((x + side * 24, 776), (x, 776), (x, 803),
                               (x + side * 24, 803)), fill=accent, width=3)
            else:
                draw.line((360 - span, 791, 360 + span, 791), fill=accent, width=3)
        if not topic.get("loop") and time >= outro:
            progress = min(1, (time - outro) / .35)
            y = 930 + round(8 * (1 - progress) ** 3)
            draw.rounded_rectangle((145, y, 575, y + 60), radius=30, fill=base, outline=accent, width=2)
            draw.text((360, y + 30), END_CARD, font=native_font(27, True), fill="#F7F3EA", anchor="mm")
        draw.rectangle((0, 0, WIDTH, 3), fill=base)
        draw.rectangle((0, 0, WIDTH * (index + 1) / total_frames, 3), fill=accent)
        yield img


def _trim_pcm_silence(pcm, rate, threshold=320, pad_ms=45):
    import numpy as np

    samples = np.frombuffer(pcm, dtype="<i2")
    active = np.flatnonzero(np.abs(samples) > threshold)
    if active.size == 0:
        return pcm
    pad = round(rate * pad_ms / 1000)
    start = max(0, int(active[0]) - pad)
    end = min(len(samples), int(active[-1]) + 1 + pad)
    return samples[start:end].tobytes()


def narration(beats, seconds, voice_path, destination, *, topic=None):
    topic = topic or {}
    plan = spoken_plan(topic, beats)
    gaps = [gap for _, _, gap in plan]
    direction = treatment(topic)
    speed, expression, rhythm = DELIVERY[direction["delivery"]]
    kokoro_voice = direction.get("kokoro_voice")

    def fit_narration():
        if kokoro_voice:
            from local_voices import kokoro_pcm, load_kokoro

            kokoro_dir = voice_path.parent / "kokoro"
            voice = load_kokoro(kokoro_dir)
        elif direction["voice"] not in {HINDI, INDIAN_ENGLISH}:
            from piper import PiperVoice, SynthesisConfig

            if not voice_path.is_file():
                raise ValueError("Piper voice is missing. Run python shorts.py setup-voice first.")
            voice = PiperVoice.load(voice_path, use_cuda=False)

        def synthesize(index, role, line, pace):
            length = pace * speed * (1.06 if role == "pause" else 1)
            if direction["voice"] == HINDI:
                recording = voice_path.parent.parent / "voiceovers" / topic["id"] / f"{index:02}.wav"
                if not recording.is_file():
                    raise ValueError(f"Record the spoken line as {recording} before rendering Hindi.")
                with wave.open(str(recording), "rb") as wav:
                    if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                        raise ValueError("Use mono, 16-bit PCM WAV recordings.")
                    return wav.getframerate(), wav.readframes(wav.getnframes())
            if direction["voice"] == INDIAN_ENGLISH:
                return windows_pcm(line, INDIAN_ENGLISH, 1 / length, destination.parent)
            if role == "hook":
                length *= .96
            pad_ms = 15 if (topic.get("loop") and index == len(plan)) else 45
            if kokoro_voice:
                rate, pcm = kokoro_pcm(voice, line, kokoro_voice, min(1.45, 1 / length), kokoro_dir)
                return rate, _trim_pcm_silence(pcm, rate, pad_ms=pad_ms)
            config = SynthesisConfig(length_scale=length, noise_scale=expression + (.07 if role == "hook" else 0), noise_w_scale=rhythm,
                                     volume=.90 if role == "pause" else 1)
            chunks = voice.synthesize(line, syn_config=config)
            rate = voice.config.sample_rate
            pcm = b"".join(chunk.audio_int16_bytes for chunk in chunks)
            return rate, _trim_pcm_silence(pcm, rate, pad_ms=pad_ms)

        for pace in ((1,) if direction["voice"] == HINDI else (1, .92, .84, .76)):
            rates, clips = zip(*(synthesize(i, role, line, pace)
                                 for i, (role, line, _) in enumerate(plan, 1)))
            rate = rates[0]
            if any(value != rate for value in rates):
                raise ValueError("Narration chunks have inconsistent sample rates.")
            if all(clips) and sum(len(clip) / (2 * rate) for clip in clips) + sum(gaps) <= seconds - (.04 if topic.get("loop") else .18):
                return rate, clips
        raise ValueError("Narration does not fit the selected duration. Try a longer duration.")

    rate, clips = fit_narration()
    audio = bytearray(2 * round(rate * seconds))
    timings = []
    end = 0
    for gap, clip in zip(gaps, clips):
        start = end + round(gap * rate)
        end = start + len(clip) // 2
        audio[2 * start:2 * start + len(clip)] = clip
        timings.append((start / rate, end / rate))
    with wave.open(str(destination), "wb") as wav:
        wav.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        wav.writeframes(audio)
    if direction["voice"] != HINDI:
        # Preserve pitch and reserve the end card. Cap the slowdown so the
        # caller can shorten the export when the script still ends early.
        reserve = .04 if topic.get("loop") else OUTRO_SECONDS + .05
        stretch = min(1.18, (seconds - reserve) / timings[-1][1])
        if stretch < 1 or (not topic.get("loop") and stretch >= 1.04):
            import imageio_ffmpeg

            adjusted = destination.with_suffix(".paced.wav")
            ffmpeg = os.getenv("SHORTS_FFMPEG") or imageio_ffmpeg.get_ffmpeg_exe()
            subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(destination),
                            "-af", f"atempo={1 / stretch:.8f},apad", "-t", str(seconds),
                            str(adjusted)], check=True)
            adjusted.replace(destination)
            timings = [(start * stretch, end * stretch) for start, end in timings]
    return timings


def encode(topic, beats, timings, seconds, wav_path, destination, poster=None, *,
           archive_path=None, use_clip=False, visual=None, storyboard=None, icon=None):
    import imageio_ffmpeg

    if (secret("USE_LOCAL_GPU") or "false").strip().lower() not in {"false", "0", "no"}:
        raise ValueError("This renderer is CPU-only. Set USE_LOCAL_GPU=false.")
    ffmpeg = os.getenv("SHORTS_FFMPEG") or imageio_ffmpeg.get_ffmpeg_exe()
    visual_credits = []
    credited = set()
    storyboard = storyboard or {}

    def credit(record, shot):
        key = (record["source"], shot)
        if key in credited:
            return
        credited.add(key)
        visual_credits.append({"shot": shot, **{key: record.get(key, False)
                                                for key in ("credit", "source", "synthetic", "automatic")}})

    failed_assets = set()

    @lru_cache(maxsize=2)
    def load_visual(beat_id):
        if beat_id in failed_assets:
            return None
        record = storyboard[beat_id]
        try:
            if record["kind"] == "video":
                portrait = footage.is_portrait(ffmpeg, record["path"])
                images = footage.decode_frames(ffmpeg, record["path"], 12, portrait=portrait)
                if not images:
                    raise ValueError("Empty clip")
            else:
                with Image.open(record["path"]) as original:
                    images = [original.convert("RGB")]
                portrait = images[0].height > images[0].width
        except (OSError, ValueError):
            failed_assets.add(beat_id)
            print(f"Could not decode visual beat {beat_id}; using motion graphics.")
            return None
        credit(record, f"beat-{beat_id}")
        return dict(record, images=images, portrait=portrait)

    icon_mask = None
    if icon:
        try:
            from resvg_py import svg_to_bytes

            with Image.open(io.BytesIO(svg_to_bytes(svg_path=str(icon["path"]), width=220))) as raster:
                icon_mask = raster.convert("RGBA").getchannel("A")
                icon_mask.thumbnail((220, 220), Image.Resampling.LANCZOS)
        except (ImportError, OSError, ValueError, RuntimeError):
            print("Could not rasterize the cached topic icon; using motion graphics.")
    clip = footage.decode(ffmpeg, topic["id"], FPS) if use_clip else ()
    if visual:
        if visual["kind"] == "video":
            clip = footage.decode_frames(ffmpeg, visual["path"], FPS,
                                         portrait=footage.is_portrait(ffmpeg, visual["path"]))
            if not clip:
                raise ValueError("The selected stock clip could not be decoded.")
        else:
            archive_path = visual["path"]
        credit(visual, "archive")
    if use_clip and not clip:
        raise ValueError("The reviewed footage did not decode. Check the cached file.")
    diagram, render_template = (), None
    if find_spec("manim") is not None:
        try:
            from manim_diagrams import DIAGRAMS, render_diagram, render_template

            diagram_id = topic["id"].removeprefix("discovered-").removeprefix("spark-")
            if diagram_id in DIAGRAMS:
                diagram = footage.decode_frames(ffmpeg, render_diagram(diagram_id, wav_path.parent, FPS), FPS)
        except (ImportError, OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
            print(f"Manim unavailable for the curated insert ({type(error).__name__}); using graphics.")
    plan = spoken_plan(topic, beats)
    word_lines, caption_timing = build_word_timeline(wav_path, topic.get("language", "en"), plan, timings)
    visual_timeline = resolve_visual_timeline(topic, word_lines, plan, timings, seconds)
    specs = {item["beat_id"]: item["diagram"] for item in visual_timeline if item.get("diagram")}
    failed_diagrams = set()

    @lru_cache(maxsize=2)
    def load_diagram(beat_id):
        if render_template is None or beat_id in failed_diagrams:
            return ()
        try:
            path = wav_path.parent / f"diagram-beat-{beat_id}.mp4"
            if not path.is_file():
                render_template(specs[beat_id], wav_path.parent, beat_id, 15)
            decoded = footage.decode_frames(ffmpeg, path, 15)
            if not decoded:
                raise ValueError("No diagram frames decoded.")
            return decoded
        except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
            failed_diagrams.add(beat_id)
            print(f"Diagram beat {beat_id} unavailable ({type(error).__name__}); using its fallback.")
            return ()
    caption_filter = ass_filter(wav_path.parent, topic, plan, timings, seconds, word_lines)
    shutil.copyfile(wav_path.parent / "captions.ass", destination.with_name("captions.ass"))
    theme = treatment(topic)["soundtrack"]
    mastered, audio = master(ffmpeg, wav_path, seconds, topic, theme, SCORES[theme],
                             motion_cues(topic, beats, timings, seconds), visual_timeline=visual_timeline)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pixel_format", "rgb24", "-video_size", f"{OUTPUT_SIZE[0]}x{OUTPUT_SIZE[1]}",
           "-framerate", str(FPS), "-i", "-", "-i", str(mastered), "-map", "0:v", "-map", "1:a",
           "-vf", caption_filter, "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
           "-pix_fmt", "yuv420p", "-threads:v", "4", "-c:a", "aac", "-b:a", "192k", "-ac", "2",
           "-movflags", "+faststart", "-t", str(seconds), str(destination)]
    with subprocess.Popen(cmd, stdin=subprocess.PIPE, cwd=wav_path.parent) as process:
        try:
            for image in frames(topic, beats, timings, seconds, archive_path, clip, diagram,
                                visual_timeline=visual_timeline, storyboard=storyboard, load_visual=load_visual,
                                load_diagram=load_diagram,
                                icon=icon_mask, credit_icon=lambda: credit(icon, "fallback")):
                process.stdin.write(image.tobytes())
        except BrokenPipeError:
            pass
        finally:
            process.stdin.close()
        if process.wait():
            raise ValueError("FFmpeg could not encode the Short.")
    if poster:
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", "1",
                        "-i", str(destination), "-frames:v", "1", str(poster)], check=True)
    used_manim = bool(diagram or any(item.get("diagram_template") for item in visual_timeline))
    return {"diagram_renderer": "manim" if used_manim else "pillow", "caption_timing": caption_timing,
            "resolution": list(OUTPUT_SIZE), "fps": FPS, "audio": audio, "visual_credits": visual_credits,
            "visual_timeline": visual_timeline}
