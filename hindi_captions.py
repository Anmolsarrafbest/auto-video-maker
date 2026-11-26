"""Build animated word captions for FFmpeg/libass."""

from difflib import SequenceMatcher
import os
from pathlib import Path
import re
import shutil
import sys
import wave


MODEL_DIR = Path(__file__).resolve().parent / "assets-cache" / "whisper-tiny"


def topic_badge(topic):
    headings = {"Space": "SPACE SCIENCE", "History": "HISTORY STORIES",
                "Physics": "EVERYDAY PHYSICS", "Ocean": "OCEAN SCIENCE"}
    if topic.get("language") == "hi":
        headings = {"Space": "अंतरिक्ष विज्ञान", "History": "विज्ञान का इतिहास",
                    "Physics": "रोज़ की भौतिकी", "Ocean": "समुद्री विज्ञान"}
        return headings.get(topic["category"], "विज्ञान की बातें")
    return headings.get(topic["category"], topic["category"].upper())


def recognized_words(wav, language):
    if not (MODEL_DIR / "model.bin").is_file():
        return []
    try:
        from faster_whisper import WhisperModel
        import numpy as np

        model = WhisperModel(str(MODEL_DIR), device="cpu", compute_type="int8",
                             cpu_threads=min(4, os.cpu_count() or 1), local_files_only=True)
        with wave.open(str(wav), "rb") as audio:
            rate = audio.getframerate()
            samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2")
        # Narration WAVs are mono PCM; pass samples directly to avoid PyAV-version
        # differences between Manim and faster-whisper on Windows.
        positions = np.arange(round(len(samples) * 16000 / rate)) * rate / 16000
        pcm = (np.interp(positions, np.arange(len(samples)), samples) / 32768).astype(np.float32)
        segments, _ = model.transcribe(pcm, language=language.split("-")[0], beam_size=1,
                                       word_timestamps=True, condition_on_previous_text=False)
        return [(word.word.strip(), word.start, word.end)
                for segment in segments for word in (segment.words or ())]
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        print(f"Whisper alignment unavailable ({type(error).__name__}). Using narration timings.",
              file=sys.stderr)
        return []


def align_words(text, start, end, detected):
    words = text.split()
    hits = [(word, a, b) for word, a, b in detected if start <= (a + b) / 2 <= end]
    normalize = lambda word: re.sub(r"[^\w]", "", word.casefold())
    matcher = SequenceMatcher(None, [normalize(word) for word in words],
                              [normalize(item[0]) for item in hits], autojunk=False)
    starts = [None] * len(words)
    for match in matcher.get_matching_blocks():
        for offset in range(match.size):
            starts[match.a + offset] = hits[match.b + offset][1]
    anchors = [(-1, start)] + [(i, t) for i, t in enumerate(starts) if t is not None] + [(len(words), end)]
    for (left, a), (right, b) in zip(anchors, anchors[1:]):
        for index in range(left + 1, right):
            fraction = (index - left) / (right - left) if left >= 0 else index / max(1, right)
            starts[index] = a + (b - a) * fraction
    starts = [min(end, max(start, value)) for value in starts]
    return words, starts


def timestamp(seconds):
    centiseconds = round(seconds * 100)
    minutes, remainder = divmod(centiseconds, 6000)
    hours, minutes = divmod(minutes, 60)
    whole, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02}:{whole:02}.{fraction:02}"


def _word_groups(words, max_words=3, max_chars=20):
    """Partition words into compact mobile-friendly caption chunks."""
    groups = []
    start = 0
    while start < len(words):
        end = start + 1
        chars = len(words[start])
        while end < len(words) and end - start < max_words and chars + 1 + len(words[end]) <= max_chars:
            chars += 1 + len(words[end])
            end += 1
        groups.extend([(start, end)] * (end - start))
        start = end
    return groups


def ass_filter(scratch, topic, plan, timings, seconds, wav):
    hindi = topic.get("language") == "hi"
    font_name = "Noto Sans Devanagari" if hindi else ("Arial" if os.name == "nt" else "DejaVu Sans")
    caption_size = 44 if hindi else 48
    fonts = ""
    if hindi and os.name == "nt":
        installed = Path(os.environ["WINDIR"]) / "Fonts" / "Nirmala.ttc"
        if not installed.is_file():
            raise ValueError("Hindi rendering needs Windows' Nirmala UI font.")
        font_name = "Nirmala UI"
        font_dir = scratch / "hindi-font"
        font_dir.mkdir(exist_ok=True)
        shutil.copyfile(installed, font_dir / "Nirmala.ttc")
        fonts = ":fontsdir=hindi-font"
    script = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 720
PlayResY: 1280
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Header,{font_name},25,&H00FFE3CB,&H00FFFFFF,&H001B1511,&H00000000,-1,0,0,0,100,100,0,0,1,2,0,7,58,80,130,1
Style: Label,{font_name},25,&H00FFE3CB,&H00FFFFFF,&H001B1511,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,2,75,95,540,1
Style: Caption,{font_name},{caption_size},&H00F2F5F7,&H00FFFFFF,&H001B1511,&H00000000,-1,0,0,0,100,100,0,0,1,3.4,1.5,2,68,88,365,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    def escape(text):
        return text.replace("\\", "／").replace("{", "(").replace("}", ")").replace("\n", " ")

    def line(start, end, style, text):
        return f"Dialogue: 0,{timestamp(start)},{timestamp(end)},{style},,0,0,0,,{text}\n"

    if hindi:
        script += line(0, seconds, "Header", escape(topic_badge(topic)))
        first_answer = next(start for (start, _), (role, _, _) in zip(timings, plan) if role == "answer")
        script += line(first_answer, seconds, "Label", escape(topic["labels"][1]))
    detected = recognized_words(wav, topic.get("language", "en"))
    for (_, text, _), (start, end) in zip(plan, timings):
        words, starts = align_words(text, start, end, detected)
        ranges = _word_groups(words)
        for index, begin in enumerate(starts):
            finish = starts[index + 1] if index + 1 < len(starts) else end
            if timestamp(finish) == timestamp(begin):
                continue
            group_start, group_end = ranges[index]
            emphasis = r"{\c&H74F8FF&\fscx114\fscy114\t(0,100,\fscx100\fscy100)}"
            phrase = " ".join(emphasis + escape(word) + r"{\r}" if position == index else escape(word)
                              for position, word in enumerate(words) if group_start <= position < group_end)
            script += line(begin, finish, "Caption", phrase)
    (scratch / "captions.ass").write_text(script, encoding="utf-8")
    return "ass=captions.ass" + fonts, "whisper" if detected else "estimated"
