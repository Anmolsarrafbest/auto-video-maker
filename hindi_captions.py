"""Burn properly shaped Devanagari text with FFmpeg/libass."""

import os
from pathlib import Path
import shutil


def timestamp(seconds):
    centiseconds = round(seconds * 100)
    minutes, remainder = divmod(centiseconds, 6000)
    hours, minutes = divmod(minutes, 60)
    whole, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02}:{whole:02}.{fraction:02}"


def ass_filter(scratch, topic, plan, timings, seconds):
    font_name = "Noto Sans Devanagari"
    fonts = ""
    if os.name == "nt":
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
Style: Header,{font_name},28,&H00FFE3CB,&H00FFFFFF,&H001B1511,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,7,58,80,130,1
Style: Title,{font_name},47,&H00F2F5F7,&H00FFFFFF,&H001B1511,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,7,55,75,175,1
Style: Label,{font_name},25,&H00FFE3CB,&H00FFFFFF,&H001B1511,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,2,75,95,540,1
Style: Caption,{font_name},38,&H00F2F5F7,&H00FFFFFF,&H001B1511,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,2,95,115,321,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    def line(start, end, style, text):
        safe = text.replace("\\", "／").replace("{", "(").replace("}", ")").replace("\n", " ")
        return f"Dialogue: 0,{timestamp(start)},{timestamp(end)},{style},,0,0,0,,{safe}\n"

    heading = {"Space": "अंतरिक्ष", "History": "इतिहास", "Physics": "भौतिकी", "Ocean": "समुद्र"}
    script += line(0, seconds, "Header", heading.get(topic["category"], "विज्ञान"))
    script += line(0, seconds, "Title", plan[0][1])
    first_answer = next(start for (start, _), (role, _, _) in zip(timings, plan) if role == "answer")
    script += line(first_answer, seconds, "Label", topic["labels"][1])
    for i, (role, text, _) in enumerate(plan):
        if role != "hook":
            end = timings[i + 1][0] if i + 1 < len(timings) else seconds
            script += line(timings[i][0], end, "Caption", text)
    (scratch / "hindi.ass").write_text(script, encoding="utf-8")
    return "ass=hindi.ass" + fonts
