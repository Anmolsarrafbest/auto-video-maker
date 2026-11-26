"""Offline Windows and Kokoro speech backends."""

from functools import lru_cache
import json
from pathlib import Path
import subprocess
import tempfile
from tempfile import TemporaryDirectory
import wave

WINDOWS_SCRIPT = Path(__file__).with_name("windows-voice.ps1")
KOKORO_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1"
KOKORO_FILES = {"kokoro-v1.0.int8.onnx": 114119327, "voices-v1.0.bin": 28214398}


def setup_kokoro(folder):
    import requests

    folder.mkdir(parents=True, exist_ok=True)
    for filename, expected in KOKORO_FILES.items():
        path = folder / filename
        if path.is_file() and path.stat().st_size == expected:
            continue
        partial = path.with_suffix(path.suffix + ".part")
        try:
            with requests.get(f"{KOKORO_RELEASE}/{filename}?download=1", stream=True, timeout=(15, 90)) as response:
                response.raise_for_status()
                size = 0
                with partial.open("wb") as output:
                    for chunk in response.iter_content(1024 * 1024):
                        size += len(chunk)
                        if size > expected:
                            raise ValueError(f"Unexpected size for {filename}.")
                        output.write(chunk)
            if size != expected:
                raise ValueError(f"Incomplete model download: {filename}.")
            partial.replace(path)
        finally:
            partial.unlink(missing_ok=True)


@lru_cache(maxsize=1)
def load_kokoro(folder):
    if any(not (folder / filename).is_file() for filename in KOKORO_FILES):
        raise ValueError("Run python shorts.py setup-kokoro to cache the voice assets.")
    try:
        from kokoro_onnx import Kokoro
        from onnxruntime import InferenceSession, SessionOptions
    except ImportError as error:
        raise ValueError("Install the voice engine with python -m pip install kokoro-onnx.") from error
    options = SessionOptions()
    options.intra_op_num_threads = 4
    session = InferenceSession(str(folder / "kokoro-v1.0.int8.onnx"), sess_options=options,
                               providers=["CPUExecutionProvider"])
    return Kokoro.from_session(session, str(folder / "voices-v1.0.bin"))


def kokoro_pcm(engine, text, voice, speed, scratch):
    import numpy as np

    # On Windows phonemizer creates a private copy of eSpeak's DLL. Use the
    # writable model cache so the copy can be loaded from restricted sessions.
    previous = tempfile.tempdir
    try:
        tempfile.tempdir = str(scratch)
        phonemes = engine.tokenizer.phonemize(text, "en-gb" if voice.startswith("b") else "en-us")
    finally:
        tempfile.tempdir = previous
    tokens = engine.tokenizer.tokenize(phonemes)
    if not tokens:
        raise ValueError("Kokoro could not pronounce this line.")
    if voice not in engine.voices:
        raise ValueError(f"Unknown Kokoro voice preset: {voice}.")
    styles = engine.voices[voice]
    shape = styles[min(len(tokens), len(styles) - 1)]
    inputs = {item.name: item.type for item in engine.sess.get_inputs()}
    token_input = "input_ids" if "input_ids" in inputs else "tokens"
    speed_type = np.int32 if inputs["speed"] == "tensor(int32)" else np.float32
    samples = engine.sess.run(None, {
        token_input: np.asarray([[0, *tokens, 0]], dtype=np.int64),
        "style": np.asarray(shape, dtype=np.float32),
        "speed": np.asarray([speed], dtype=speed_type),
    })[0].ravel()
    return 24000, (np.clip(samples, -1, 1) * 30000).astype("<i2").tobytes()


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
