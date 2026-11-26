"""Offline speech backends for the Indian English and Hindi editions."""

import ctypes as ct
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


class HindiSpeech:
    """Read PCM from the eSpeak NG synchronous synthesis callback."""

    def __enter__(self):
        try:
            from espeakng_loader import get_data_path, get_library_path
        except ImportError as error:
            raise ValueError("Install requirements.txt to enable offline Hindi narration.") from error

        self.engine = ct.CDLL(get_library_path())
        self.engine.espeak_Initialize.argtypes = (ct.c_int, ct.c_int, ct.c_char_p, ct.c_int)
        self.engine.espeak_SetVoiceByName.argtypes = (ct.c_char_p,)
        self.engine.espeak_SetParameter.argtypes = (ct.c_int, ct.c_int, ct.c_int)
        self.engine.espeak_Synth.argtypes = (ct.c_char_p, ct.c_size_t, ct.c_uint, ct.c_int,
                                           ct.c_uint, ct.c_uint, ct.c_void_p, ct.c_void_p)
        self.rate = self.engine.espeak_Initialize(2, 0, str(Path(get_data_path()).parent).encode(), 0x8000)
        if self.rate <= 0:
            raise ValueError("The offline Hindi speech engine could not start.")
        if self.engine.espeak_SetVoiceByName(b"hi") != 0:
            self.engine.espeak_Terminate()
            raise ValueError("Hindi speech data is missing.")
        return self

    def __exit__(self, *exc):
        self.engine.espeak_Terminate()

    def synthesize(self, text, speed):
        pieces = []

        @ct.CFUNCTYPE(ct.c_int, ct.POINTER(ct.c_short), ct.c_int, ct.c_void_p)
        def receive(pcm, count, _events):
            if pcm and count:
                pieces.append(ct.string_at(pcm, count * ct.sizeof(ct.c_short)))
            return 0

        self.engine.espeak_SetSynthCallback(receive)
        self.engine.espeak_SetParameter(1, round(155 * speed), 0)
        data = text.encode("utf-8")
        if self.engine.espeak_Synth(data, len(data) + 1, 0, 1, 0, 1, None, None) != 0:
            raise ValueError("The offline Hindi speech engine could not narrate this line.")
        return self.rate, b"".join(pieces)
