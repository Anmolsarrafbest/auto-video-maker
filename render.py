"""Draw original science illustrations and encode narrated portrait video."""

import math
from array import array
from bisect import bisect_right
from functools import lru_cache
from importlib.util import find_spec
import os
import random
import re
import subprocess
import wave

from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageOps

from hindi_captions import ass_filter
from local_voices import windows_pcm
from production import HINDI, INDIAN_ENGLISH, treatment
from storyboard import CLIPS, episode
import footage

WIDTH, HEIGHT, FPS = 720, 1280, 30
END_CARD = "Follow and subscribe"
ANSWER_PAUSE = 1.5
PALETTES = {
    "observatory": ((9, 14, 29), (10, 12, 21), "#405273", "#0D1527", 30),
    "archive": ((27, 19, 20), (25, 17, 12), "#705040", "#251B1B", 8),
    "lab": ((8, 26, 26), (9, 20, 15), "#376761", "#102824", 13),
    "ocean": ((6, 20, 34), (6, 28, 29), "#37657B", "#0B2537", 42),
    "copper": ((37, 20, 16), (66, 36, 23), "#C68458", "#44271C", 10),
    "paper": ((52, 36, 32), (89, 67, 49), "#C9A880", "#49362B", 4),
}

# Each arrangement has its own chord progression, phrasing and instruments.
SCORES = {
    "orbital groove": (.46, ((38, 57, 60, 65, 69), (46, 57, 62, 65, 69),
                             (41, 57, 60, 64, 69), (43, 55, 62, 65, 70)), (0, 2, 3, 1), .8, 1),
    "ambient synth": (.72, ((45, 57, 60, 64, 71), (41, 53, 60, 64, 67),
                           (48, 60, 64, 67, 74), (43, 55, 62, 69, 71)), (0, 2, 1, 3), .35, .75),
    "felt piano": (.81, ((48, 60, 64, 67, 71), (45, 57, 60, 64, 69),
                        (50, 62, 65, 69, 72), (43, 55, 59, 65, 69)), (3, 1, 2, 0), .08, 1.30),
    "syncopated marimba": (.52, ((50, 62, 65, 69, 72), (43, 55, 59, 62, 67),
                                 (48, 60, 64, 67, 72), (45, 57, 61, 64, 69)), (1, 3, 0, 2), 1, 1.10),
    "slow instrumental swell": (.91, ((40, 52, 55, 59, 66), (47, 59, 62, 66, 69),
                                     (45, 57, 60, 64, 71), (43, 55, 59, 62, 67)), (0, 1, 3, 2), .18, .45),
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
    return (reveal, seconds - 2)


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
        sun_y = y - 69 + min(time * 7, 80)
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


def frames(topic, beats, timings, seconds, archive_path=None, clip=(), diagram=()):
    accent = ImageColor.getrgb(topic["color"])
    theme = treatment(topic)["theme"]
    base, tint, border, caption_fill, radius = PALETTES[theme]
    background = Image.new("RGB", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(background)
    for y in range(HEIGHT):
        glow = max(0, 1 - abs(y - 545) / 650)
        draw.line((0, y, WIDTH, y), fill=tuple(component + int(change * glow)
                                              for component, change in zip(base, tint)))
    if theme == "lab":
        for x in range(77, 640, 33):
            draw.line((x, 336, x, 743), fill="#23463F")
        for y in range(345, 744, 33):
            draw.line((64, y, 630, y), fill="#23463F")
    elif theme == "archive":
        for y in range(350, 750, 32):
            draw.line((70, y, 627, y), fill="#513932")
            draw.line((70, y - 4, 87, y - 4), fill=border)
    elif theme == "ocean":
        for index in range(9):
            path = [(x, 349 + index * 47 + 10 * math.sin(x / 62 + index)) for x in range(66, 631, 7)]
            draw.line(path, fill="#1D4657", width=2)
    else:
        rng = random.Random(topic["id"])
        for _ in range(64):
            x, y = rng.randrange(62, 641), rng.randrange(336, 752)
            circle(draw, x, y, rng.choice((1, 1, 2)), "#465677")
    hindi = topic.get("language") == "hi"
    if not hindi:
        draw.text((58, 140), topic["category"].upper(), font=font(25, True), fill=accent)
    plan = spoken_plan(topic, beats)
    heading = beats[0] if plan[0][0] == "hook" else topic["title"]
    headline = font(53, True, serif=theme in {"archive", "paper", "copper"})
    if wrap(heading, headline, 576).count("\n") > 1:
        headline = font(42, True, serif=theme == "archive")
    if not hindi:
        draw.multiline_text((55, 184), wrap(heading, headline, 576), font=headline,
                            fill="#F7F3EA", spacing=2)
    photograph = None
    if archive_path:
        with Image.open(archive_path) as source:
            photograph = ImageOps.contain(source.convert("RGB"), (535, 342))
    layout = episode(topic)[1]
    starts = [start for start, _ in timings]
    captions = []
    for role, text, _ in plan:
        face = font(39, True)
        if wrap(text, face, 526).count("\n") > 3:
            face = font(34, True)
        captions.append((wrap(text, face, 526) if role != "hook" else "", face))
    label_font = font(23, True)
    reveal, outro = motion_cues(topic, beats, timings, seconds)
    recap_start = timings[-1][1] + .15
    recap_steps = ("Light bends as it enters", "It reflects once inside",
                   "The colors exit at different angles")
    motion = topic.get("episode_plan", {}).get("motion", "underline")
    for index in range(seconds * FPS):
        time = index / FPS
        recap = time >= recap_start
        replay = bool(diagram) and outro - recap_start >= 2.4 and recap
        replay_progress = min(1, max(0, (time - recap_start) / max(.1, outro - recap_start)))
        part = max(0, bisect_right(starts, time) - 1)
        beat = int(plan[part][0] == "answer")
        img = background.copy()
        draw = ImageDraw.Draw(img)
        archive_shot = time < seconds * topic.get("archive_fraction", .73)
        if clip and archive_shot:
            archive_duration = seconds * topic.get("archive_fraction", .73)
            # Spread sparse camera observations across the shot in chronological order.
            frame = int(time * len(clip) / archive_duration) if topic["id"] in {"mars-sunset", "jupiter-spin"} else index
            img.paste(clip[min(len(clip) - 1, frame)], (60, 370))
            draw.text((348, 714), CLIPS[topic["id"]]["label"], font=font(20, True), fill=accent, anchor="mm")
        elif photograph is not None and archive_shot:
            img.paste(photograph, ((696 - photograph.width) // 2, 341 + (342 - photograph.height) // 2))
            draw.text((348, 714), topic["archive"]["label"], font=font(20, True), fill=accent, anchor="mm")
        elif diagram and beat:
            diagram_frame = min(len(diagram) - 1, max(0, round((time - reveal) * FPS)))
            if replay:
                diagram_frame = round(replay_progress * (len(diagram) - 1))
            shot = diagram[diagram_frame]
            drift = min(1, max(0, (time - reveal) / max(1, seconds - reveal)))
            width = WIDTH + round(32 * drift)
            shot = shot.resize((width, round(456 * width / WIDTH)), Image.Resampling.BILINEAR)
            img.paste(shot, ((WIDTH - width) // 2, 300 - (shot.height - 456) // 2))
        else:
            if layout in {"space film", "experiment"}:
                stage = Image.new("RGB", (WIDTH, HEIGHT), base)
                illustration(ImageDraw.Draw(stage), topic["scene"], accent, time, beat)
                if topic["scene"] == "distance":
                    moon = lunar_sphere(time * 4)
                    offset = 30 * (1 - math.cos(time * .4))
                    stage.paste(moon, (round(477 + offset) - 42, 500), moon)
                push_start = min(recap_start, seconds - 3)
                progress = min(1, max(0, (time - push_start) / max(1.2, outro - push_start)))
                ease = progress * progress * (3 - 2 * progress)
                wide = (62, 364 if beat else 315, 645, 715 if beat else 750)
                bounds = tuple(round(a + (b - a) * ease) for a, b in zip(wide, (96, 384, 625, 689)))
                shot = stage.crop(bounds)
                shot = ImageOps.fit(shot, (720, 490))
                img.paste(shot, (0, 313 if layout == "space film" else 330))
            else:
                stage = Image.new("RGB", (WIDTH, HEIGHT), base)
                illustration(ImageDraw.Draw(stage), topic["scene"], accent, time, beat)
                shot = ImageOps.fit(stage.crop((54, 319, 646, 752)), (WIDTH, 490))
                img.paste(shot, (0, 306))
        if beat and not recap and not hindi and not (archive_shot and (clip or photograph is not None)):
            label = topic["labels"][1]
            draw.text((348, 710), label, font=label_font if label_font.getlength(label) < 550 else font(20, True),
                      fill=accent, anchor="mm")
            if topic["scene"] == "distance":
                draw.text((348, 755), "ILLUSTRATION · NOT TO SCALE", font=font(17, True), fill=accent, anchor="mm")
        caption, face = captions[part]
        if recap:
            face = font(35, True)
            takeaway = topic["labels"][1]
            if replay:
                step = bisect_right((1.4, 2.2), diagram_frame / FPS)
                takeaway = recap_steps[step]
            caption = wrap(takeaway, face, 526)
        if caption:
            if layout in {"space film", "experiment"} and not hindi:
                draw.rectangle((53, 862, 60, 1039), fill=accent)
            else:
                draw.rounded_rectangle((54, 845, 642, 1063), radius=radius, fill=caption_fill, outline=border, width=2)
            if not hindi:
                draw.multiline_text((348, 950), caption, font=face,
                                    fill="#F7F3EA", anchor="mm", align="center", spacing=7)
        if layout == "magazine":
            draw.rounded_rectangle((57, 1054, 639, 1060), radius=3, fill=border)
            draw.rectangle((57, 1054, 57 + 582 * (index + 1) / (seconds * FPS), 1060), fill=accent)
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
        if time >= outro:
            progress = min(1, (time - outro) / .35)
            y = 1090 + round(18 * (1 - progress) ** 3)
            draw.rounded_rectangle((145, y, 575, y + 60), radius=30, fill=base, outline=accent, width=2)
            draw.text((360, y + 30), END_CARD, font=font(27, True), fill="#F7F3EA", anchor="mm")
        yield img


def mix_music(audio, rate, timings, topic, cues):
    """Compose an original background score around the episode's delivery."""
    samples = array("h")
    samples.frombytes(audio)
    score = treatment(topic)["soundtrack"]
    beat_length, progression, motif, percussion, lead = SCORES[score]
    random_noise = random.Random(topic.get("id", score))
    key = random_noise.choice((-2, -1, 0, 2, 3))
    inversion = random_noise.randrange(len(motif))
    motif = motif[inversion:] + motif[:inversion]
    beat_length *= random_noise.uniform(.91, 1.09)
    bar_length = beat_length * 4
    chords = [tuple(440 * 2 ** ((note + key - 69) / 12) for note in chord) for chord in progression]
    sound = topic.get("episode_plan", {}).get("sound", "sweep")
    pitch, glide, decay = {"pop": (155, 80, 25), "sweep": (220, 180, 14),
                           "chime": (520, 35, 10)}[sound]
    effects_plan = ((cues[0], pitch, glide, decay, 1350), (cues[1], 420, 180, 14, 1000))
    for index, speech in enumerate(samples):
        time = index / rate
        bar = time % bar_length
        root, *tones = chords[int(time / bar_length) % len(chords)]
        chord_fade = min(1, bar * 10, (bar_length - bar) * 10)
        pad = 1100 * chord_fade * sum(
            math.sin(math.tau * note * time) + .2 * math.sin(math.tau * note * 2.003 * time)
            for note in tones
        ) / len(tones)
        pulse = bar % beat_length
        bass = 1600 * min(1, pulse * 75) * math.exp(-pulse * 3) * math.sin(math.tau * root * pulse)
        kick = 900 * math.exp(-pulse * 25) * math.sin(math.tau * (45 * pulse + .7 * (1 - math.exp(-35 * pulse))))
        brush = 590 * random_noise.uniform(-1, 1) * math.exp(-(bar % (beat_length / 2)) * 40)
        melody_time = pulse
        note = tones[motif[min(3, int(bar / beat_length))]] * 2
        angle = math.tau * note * melody_time
        color = math.sin(angle)
        if score == "syncopated marimba":
            melody_time = (bar + beat_length / 3) % beat_length
            angle = math.tau * note * melody_time
            color = math.sin(angle) + .28 * math.sin(3.97 * angle)
        elif score == "felt piano":
            color += .18 * math.sin(2 * angle) + .09 * math.sin(3.01 * angle)
        elif score == "slow instrumental swell":
            color *= .5 + .5 * math.sin(bar * math.tau / bar_length - math.pi / 2)
        keys = 640 * lead * min(1, melody_time * 60) * math.exp(-melody_time * 6) * color
        distance = min(max(start - time, time - end, 0) for start, end in timings)
        duck = .53 + .47 * min(1, distance / .14)
        fade = min(1, (time + .03) * 8, (len(samples) - index) / (.38 * rate))
        if score == "felt piano":
            hammer = math.sin(angle) + .22 * math.sin(2.01 * angle) + .10 * math.sin(4.03 * angle)
            arpeggio = tones[(int(bar * 2 / beat_length) + inversion) % len(tones)]
            piano = 1450 * hammer * min(1, pulse * 110) * math.exp(-pulse * 4.6)
            resonance = 460 * math.sin(math.tau * arpeggio * time) * chord_fade
            music = piano + resonance + .24 * pad
        elif score == "ambient synth":
            swell = .55 + .45 * math.sin(math.tau * (bar / bar_length - .25))
            drone = 1200 * math.sin(math.tau * root * time) * chord_fade
            music = pad * swell + drone + .3 * keys
        elif score == "slow instrumental swell":
            wash = .48 + .52 * math.sin(time * .8) ** 2
            music = pad * wash + .48 * bass + .25 * brush + .8 * keys
        else:
            music = .30 * pad + bass + percussion * (kick + brush) + 1.2 * keys
        if score == "orbital groove":
            offbeat = (bar + beat_length / 2) % beat_length
            pluck = math.sin(math.tau * note * offbeat) * math.exp(-offbeat * 8)
            music += 650 * pluck * min(1, offbeat * 100) + .35 * pad
        effects = 0
        for onset, pitch, glide, decay, gain in effects_plan:
            elapsed = time - onset
            if 0 <= elapsed < .32:
                sweep = pitch * elapsed + glide * elapsed ** 2
                effects += gain * min(1, elapsed * 180) * math.exp(-elapsed * decay) * (
                    math.sin(math.tau * sweep) + .24 * math.sin(math.tau * sweep * 2)
                )
        mixed = round(speech * .76 + fade * (1.55 * duck * music + effects))
        samples[index] = max(-32768, min(32767, mixed))
    return samples.tobytes()


def narration(beats, seconds, voice_path, destination, *, topic=None):
    topic = topic or {}
    plan = spoken_plan(topic, beats)
    gaps = [gap for _, _, gap in plan]
    direction = treatment(topic)
    speed, expression, rhythm = DELIVERY[direction["delivery"]]
    def fit_narration():
        if direction["voice"] not in {HINDI, INDIAN_ENGLISH}:
            from piper import PiperVoice, SynthesisConfig

            if not voice_path.is_file():
                raise ValueError("Piper voice is missing. Run python shorts.py setup-voice first.")
            voice = PiperVoice.load(voice_path)

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
            config = SynthesisConfig(length_scale=length, noise_scale=expression + (.07 if role == "hook" else 0), noise_w_scale=rhythm,
                                     volume=.90 if role == "pause" else 1)
            chunks = voice.synthesize(line, syn_config=config)
            return voice.config.sample_rate, b"".join(chunk.audio_int16_bytes for chunk in chunks)

        for pace in ((1,) if direction["voice"] == HINDI else (1, .92, .84, .76)):
            rates, clips = zip(*(synthesize(i, role, line, pace)
                                 for i, (role, line, _) in enumerate(plan, 1)))
            rate = rates[0]
            if any(value != rate for value in rates):
                raise ValueError("Narration chunks have inconsistent sample rates.")
            if all(clips) and sum(len(clip) / (2 * rate) for clip in clips) + sum(gaps) <= seconds - .18:
                return rate, clips
        raise ValueError("Narration does not fit the selected duration. Try a longer duration.")

    rate, clips = fit_narration()
    audio = bytearray(2 * rate * seconds)
    timings = []
    end = 0
    for gap, clip in zip(gaps, clips):
        start = end + round(gap * rate)
        end = start + len(clip) // 2
        audio[2 * start:2 * start + len(clip)] = clip
        timings.append((start / rate, end / rate))
    with wave.open(str(destination), "wb") as wav:
        wav.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        wav.writeframes(mix_music(audio, rate, timings, topic, motion_cues(topic, beats, timings, seconds)))
    return timings


def encode(topic, beats, timings, seconds, wav_path, destination, poster=None, *, archive_path=None, use_clip=False):
    import imageio_ffmpeg

    ffmpeg = os.getenv("SHORTS_FFMPEG") or imageio_ffmpeg.get_ffmpeg_exe()
    clip = footage.decode(ffmpeg, topic["id"], FPS) if use_clip else ()
    if use_clip and not clip:
        raise ValueError("The reviewed footage did not decode. Check the cached file.")
    diagram = ()
    if topic["id"] == "rainbow-split" and find_spec("manim") is not None:
        from manim_diagrams import render_rainbow

        diagram = footage.decode_frames(ffmpeg, render_rainbow(wav_path.parent, FPS), FPS)
        if not diagram:
            raise ValueError("The Manim diagram did not decode.")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pixel_format", "rgb24", "-video_size", f"{WIDTH}x{HEIGHT}",
           "-framerate", str(FPS), "-i", "-", "-i", str(wav_path), "-map", "0:v", "-map", "1:a"]
    hindi = topic.get("language") == "hi"
    if hindi:
        cmd.extend(["-vf", ass_filter(wav_path.parent, topic, spoken_plan(topic, beats), timings, seconds)])
    cmd.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", "-t", str(seconds), str(destination)]
    )
    with subprocess.Popen(cmd, stdin=subprocess.PIPE, cwd=wav_path.parent) as process:
        try:
            for index, image in enumerate(frames(topic, beats, timings, seconds, archive_path, clip, diagram)):
                if poster and index == FPS and not hindi:
                    image.save(poster)
                process.stdin.write(image.tobytes())
        except BrokenPipeError:
            pass
        finally:
            process.stdin.close()
        if process.wait():
            raise ValueError("FFmpeg could not encode the Short.")
    if poster and hindi:
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", "1",
                        "-i", str(destination), "-frames:v", "1", str(poster)], check=True)
    return "manim" if diagram else "pillow"
