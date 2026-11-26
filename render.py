"""Draw original science illustrations and encode narrated portrait video."""

import math
from array import array
from bisect import bisect_right
import os
import random
import re
import subprocess
import wave

from PIL import Image, ImageColor, ImageDraw, ImageFont

from production import treatment

WIDTH, HEIGHT, FPS = 720, 1280, 24
THINK_LINE = "Think about it."
ANSWER_PAUSE = 1.5
PALETTES = {
    "observatory": ((9, 14, 29), (10, 12, 21), "#405273", "#0D1527", 30),
    "archive": ((27, 19, 20), (25, 17, 12), "#705040", "#251B1B", 8),
    "lab": ((8, 26, 26), (9, 20, 15), "#376761", "#102824", 13),
    "ocean": ((6, 20, 34), (6, 28, 29), "#37657B", "#0B2537", 42),
}

# Each arrangement has its own chord progression, phrasing and instruments.
SCORES = {
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


def spoken_plan(topic, beats):
    """Share the audio and caption schedule for a topic's editorial format."""
    style = topic.get("format", "quiz")
    plan = [("hook", beats[0], .14)]
    if style == "quiz":
        plan.append(("pause", THINK_LINE, .18))
    parts = re.split(r"(?<=[a-z0-9]\.)(?=\s+[A-Z])\s+", beats[1])
    # Keep brief replies such as “No.” attached to the explanation.
    if len(parts) > 1 and len(parts[0].split()) < 4:
        parts[:2] = [" ".join(parts[:2])]
    reveal_gap = ANSWER_PAUSE if style == "quiz" else .25
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


def frames(topic, beats, timings, seconds):
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
    draw.rounded_rectangle((53, 323, 643, 756), radius=radius, outline=border, width=2)
    draw.text((58, 140), topic["category"].upper(), font=font(25, True), fill=accent)
    headline = font(53, True, serif=theme == "archive")
    if wrap(beats[0], headline, 576).count("\n") > 1:
        headline = font(42, True, serif=theme == "archive")
    draw.multiline_text((55, 184), wrap(beats[0], headline, 576), font=headline,
                        fill="#F7F3EA", spacing=2)
    plan = spoken_plan(topic, beats)
    starts = [start for start, _ in timings]
    captions = []
    for role, text, _ in plan:
        face = font(39, True)
        if wrap(text, face, 526).count("\n") > 3:
            face = font(34, True)
        captions.append((wrap(text, face, 526) if role != "hook" else "", face))
    label_font = font(23, True)
    for index in range(seconds * FPS):
        time = index / FPS
        part = max(0, bisect_right(starts, time) - 1)
        beat = int(plan[part][0] == "answer")
        img = background.copy()
        draw = ImageDraw.Draw(img)
        illustration(draw, topic["scene"], accent, time, beat)
        if beat:
            label = topic["labels"][1]
            draw.text((348, 710), label, font=label_font if label_font.getlength(label) < 550 else font(20, True),
                      fill=accent, anchor="mm")
        caption, face = captions[part]
        if caption:
            draw.rounded_rectangle((54, 811, 642, 1029), radius=radius, fill=caption_fill, outline=border, width=2)
            draw.multiline_text((348, 916), caption, font=face,
                                fill="#F7F3EA", anchor="mm", align="center", spacing=7)
        draw.rounded_rectangle((57, 1054, 639, 1060), radius=3, fill=border)
        draw.rectangle((57, 1054, 57 + 582 * (index + 1) / (seconds * FPS), 1060), fill=accent)
        yield img


def mix_music(audio, rate, timings, topic):
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
        music = pad + bass + percussion * (kick + brush) + keys
        mixed = round(speech * .76 + 1.45 * fade * duck * music)
        samples[index] = max(-32768, min(32767, mixed))
    return samples.tobytes()


def narration(beats, seconds, voice_path, destination, *, topic=None):
    from piper import PiperVoice, SynthesisConfig

    if not voice_path.is_file():
        raise ValueError("Piper voice is missing. Run python shorts.py setup-voice first.")
    voice = PiperVoice.load(voice_path)
    rate = voice.config.sample_rate
    topic = topic or {}
    plan = spoken_plan(topic, beats)
    gaps = [gap for _, _, gap in plan]
    speed, expression, rhythm = DELIVERY[treatment(topic)["delivery"]]
    for pace in (1, .92, .84, .76):
        clips = []
        for role, line, _ in plan:
            config = SynthesisConfig(
                length_scale=pace * speed * (1.06 if role == "pause" else 1),
                noise_scale=expression, noise_w_scale=rhythm,
                volume=.90 if role == "pause" else 1,
            )
            clips.append(b"".join(chunk.audio_int16_bytes for chunk in voice.synthesize(line, syn_config=config)))
        if all(clips) and sum(len(clip) / (2 * rate) for clip in clips) + sum(gaps) <= seconds - .18:
            break
    else:
        raise ValueError("Narration does not fit the selected duration. Try 15 seconds.")

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
        wav.writeframes(mix_music(audio, rate, timings, topic))
    return timings


def encode(topic, beats, timings, seconds, wav_path, destination, poster=None):
    import imageio_ffmpeg

    ffmpeg = os.getenv("SHORTS_FFMPEG") or imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pixel_format", "rgb24", "-video_size", f"{WIDTH}x{HEIGHT}",
           "-framerate", str(FPS), "-i", "-", "-i", str(wav_path), "-map", "0:v", "-map", "1:a",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", "-t", str(seconds), str(destination)]
    with subprocess.Popen(cmd, stdin=subprocess.PIPE) as process:
        try:
            for index, image in enumerate(frames(topic, beats, timings, seconds)):
                if poster and index == FPS:
                    image.save(poster)
                process.stdin.write(image.tobytes())
        except BrokenPipeError:
            pass
        finally:
            process.stdin.close()
        if process.wait():
            raise ValueError("FFmpeg could not encode the Short.")
