"""Offline Windows speech for Indian English editions."""

import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import wave

WINDOWS_SCRIPT = Path(__file__).with_name("windows-voice.ps1")


def windows_pcm(text, voice, speed, scratch):
    with TemporaryDirectory(dir=scratch, prefix="speech-") as folder:
        request, result = Path(folder) / "speech.json", Path(folder) / "speech.wav"
        request.write_text(json.dumps({"text": text, "voice": voice, "speed": speed}), encoding="utf-8")
        try:
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(WINDOWS_SCRIPT), "-RequestPath", str(request), "-OutputPath", str(result)],
                capture_output=True, check=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ValueError("Indian English requires the Windows English (India) speech pack with Heera.") from error
        with wave.open(str(result), "rb") as audio:
            if audio.getnchannels() != 1 or audio.getsampwidth() != 2:
                raise ValueError("The Windows voice returned an unsupported audio format.")
            return audio.getframerate(), audio.readframes(audio.getnframes())
