"""Draw original science illustrations and encode narrated portrait video."""

import math
from array import array
import os
import random
import subprocess
import wave

from PIL import Image, ImageColor, ImageDraw, ImageFont

WIDTH, HEIGHT, FPS = 720, 1280, 24
VOICE = "en_US-hifi_tts_92-medium"
THINK_LINE = "Think about it."
ANSWER_PAUSE = 1.5


def font(size, bold=False):
    names = ("arialbd.ttf", "DejaVuSans-Bold.ttf") if bold else ("arial.ttf", "DejaVuSans.ttf")
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
    background = Image.new("RGB", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(background)
    for y in range(HEIGHT):
        glow = max(0, 1 - abs(y - 545) / 650)
        draw.line((0, y, WIDTH, y), fill=(9 + int(10 * glow), 14 + int(12 * glow), 29 + int(21 * glow)))
    rng = random.Random(topic["id"])
    for _ in range(64):
        x, y = rng.randrange(62, 641), rng.randrange(336, 752)
        circle(draw, x, y, rng.choice((1, 1, 2)), "#465677")
    draw.rounded_rectangle((53, 323, 643, 756), radius=30, outline="#405273", width=2)
    draw.text((58, 140), topic["category"].upper(), font=font(25, True), fill=accent)
    headline = font(53, True)
    if wrap(beats[0], headline, 576).count("\n") > 1:
        headline = font(42, True)
    draw.multiline_text((55, 184), wrap(beats[0], headline, 576), font=headline,
                        fill="#F7F3EA", spacing=2)
    caption_font = font(39, True)
    if wrap(beats[1], caption_font, 526).count("\n") > 3:
        caption_font = font(34, True)
    label_font = font(23, True)
    for index in range(seconds * FPS):
        time = index / FPS
        beat = int(time >= timings[2][0])
        img = background.copy()
        draw = ImageDraw.Draw(img)
        illustration(draw, topic["scene"], accent, time, beat)
        if beat:
            label = topic["labels"][1]
            draw.text((348, 710), label, font=label_font if label_font.getlength(label) < 550 else font(20, True),
                      fill=accent, anchor="mm")
        caption = beats[1] if beat else THINK_LINE if time >= timings[1][0] else ""
        if caption:
            draw.rounded_rectangle((54, 811, 642, 1029), radius=24, fill="#0D1527", outline="#31415E", width=2)
            draw.multiline_text((348, 916), wrap(caption, caption_font, 526), font=caption_font,
                                fill="#F7F3EA", anchor="mm", align="center", spacing=7)
        draw.rounded_rectangle((57, 1054, 639, 1060), radius=3, fill="#405273")
        draw.rectangle((57, 1054, 57 + 582 * (index + 1) / (seconds * FPS), 1060), fill=accent)
        yield img


def mix_music(audio, rate, timings):
    """Build an original groove from sustained chords, bass and brushed percussion."""
    samples = array("h")
    samples.frombytes(audio)
    bar_length = 2.4
    # Am7, Fmaj7, Cmaj7, Gadd9. Add a fresh chord every four beats.
    chords = ((110, 220, 261.63, 329.63, 392),
              (87.31, 174.61, 220, 261.63, 329.63),
              (130.81, 261.63, 329.63, 392, 493.88),
              (98, 196, 246.94, 293.66, 440))
    random_noise = random.Random(42)
    for index, speech in enumerate(samples):
        time = index / rate
        bar = time % bar_length
        root, *tones = chords[int(time / bar_length) % len(chords)]
        chord_fade = min(1, bar * 10, (bar_length - bar) * 10)
        pad = 1100 * chord_fade * sum(
            math.sin(math.tau * note * time) + .2 * math.sin(math.tau * note * 2.003 * time)
            for note in tones
        ) / len(tones)
        pulse = bar % .6
        bass = 1850 * min(1, pulse * 75) * math.exp(-pulse * 3) * math.sin(math.tau * root * pulse)
        kick = 900 * math.exp(-pulse * 25) * math.sin(math.tau * (45 * pulse + .7 * (1 - math.exp(-35 * pulse))))
        brush = 590 * random_noise.uniform(-1, 1) * math.exp(-(bar % .3) * 40)
        melody_time = bar % .6
        note = tones[(0, 2, 1, 3)[min(3, int(bar / .6))]] * 2
        keys = 700 * min(1, melody_time * 60) * math.exp(-melody_time * 6) * math.sin(math.tau * note * melody_time)
        distance = min(max(start - time, time - end, 0) for start, end in timings)
        duck = .53 + .47 * min(1, distance / .14)
        fade = min(1, (time + .03) * 8, (len(samples) - index) / (.38 * rate))
        samples[index] = round(speech * .76 + 1.45 * fade * duck * (pad + bass + kick + brush + keys))
    return samples.tobytes()


def narration(beats, seconds, voice_path, destination):
    from piper import PiperVoice, SynthesisConfig

    if not voice_path.is_file():
        raise ValueError("Piper voice is missing. Run python shorts.py setup-voice first.")
    voice = PiperVoice.load(voice_path)
    rate = voice.config.sample_rate
    lines = (beats[0], THINK_LINE, beats[1])
    gaps = (.14, .18, ANSWER_PAUSE)
    for pace in (1, .92, .84, .76):
        clips = [b"".join(chunk.audio_int16_bytes for chunk in voice.synthesize(
            line, syn_config=SynthesisConfig(length_scale=pace))) for line in lines]
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
        wav.writeframes(mix_music(audio, rate, timings))
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
