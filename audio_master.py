"""Build and master a CPU-only soundtrack from local media or a MIDI score."""

from pathlib import Path
import random
import shutil
import subprocess
import sys
import wave

from local_secrets import secret

CACHE = Path(__file__).resolve().parent / "assets-cache"
PROGRAMS = {
    "ambient synth": (89, 81), "felt piano": (0, 46), "syncopated marimba": (12, 11),
    "slow instrumental swell": (48, 10), "orbital groove": (38, 4),
}


def midi_music(destination, theme, score, topic_id, seconds):
    soundfont = CACHE / "soundfont.sf2"
    if not soundfont.is_file():
        return False
    import mido

    beat, chords, motif, _, _ = score
    root_shift = random.Random(topic_id).choice((-2, 0, 2))
    midi = mido.MidiFile()
    track = midi.add_track()
    track.append(mido.MetaMessage("set_tempo", tempo=round(beat * 1_000_000)))
    for channel, program in enumerate(PROGRAMS[theme]):
        track.append(mido.Message("program_change", channel=channel, program=program))
    ticks = midi.ticks_per_beat
    events = []
    for index in range(int(seconds / beat) + 1):
        chord = chords[(index // 4) % len(chords)]
        notes = [(1, chord[1 + motif[index % 4]] + 12, 66, .70)]
        if index % 4 == 0:
            notes += [(0, note, 48, 3.8) for note in chord]
        for channel, note, velocity, duration in notes:
            note += root_shift
            events.extend(((index * ticks, channel, note, velocity),
                           (index * ticks + round(duration * ticks), channel, note, 0)))
    previous = 0
    for position, channel, note, velocity in sorted(events):
        track.append(mido.Message("note_on", channel=channel, note=note, velocity=velocity,
                                  time=position - previous))
        previous = position
    native = secret("FLUIDSYNTH_EXE") or shutil.which("fluidsynth") or next(
        (str(path) for path in (CACHE / "fluidsynth").rglob("fluidsynth.exe")), "")
    if native:
        midi_path = destination.with_suffix(".mid")
        midi.save(midi_path)
        subprocess.run([native, "-ni", "-F", str(destination), "-r", "44100",
                        str(soundfont), str(midi_path)], check=True, capture_output=True)
        return True

    import fluidsynth

    synth = fluidsynth.Synth(samplerate=44100)
    try:
        bank = synth.sfload(str(soundfont))
        if bank < 0:
            raise ValueError("Unable to load the configured SoundFont.")
        for channel, program in enumerate(PROGRAMS[theme]):
            synth.program_select(channel, bank, 0, program)
        with wave.open(str(destination), "wb") as output:
            output.setparams((2, 2, 44100, 0, "NONE", "not compressed"))
            for message in midi:
                count = round(message.time * 44100)
                if count:
                    output.writeframes(synth.get_samples(count).tobytes())
                if message.type == "note_on":
                    if message.velocity:
                        synth.noteon(message.channel, message.note, message.velocity)
                    else:
                        synth.noteoff(message.channel, message.note)
    finally:
        synth.delete()
    return True


def master(ffmpeg, narration, seconds, topic, theme, score, cues):
    folder = CACHE / "music" / theme
    tracks = sorted(path for path in folder.glob("*") if path.suffix.lower() in {".mp3", ".wav"})
    if not tracks:
        tracks = sorted(path for path in (CACHE / "music").rglob("*")
                        if path.is_file() and path.suffix.lower() in {".mp3", ".wav"})
    source = random.Random(topic["id"]).choice(tracks) if tracks else None
    music = "local track" if source else "narration only"
    if not source:
        target = narration.with_name("score.wav")
        try:
            if midi_music(target, theme, score, topic["id"], seconds):
                source, music = target, "SoundFont MIDI"
        except (ImportError, ValueError, OSError, subprocess.CalledProcessError) as error:
            print(f"SoundFont unavailable ({type(error).__name__}); continuing with narration.", file=sys.stderr)
    cmd = [ffmpeg, "-v", "error", "-y", "-i", str(narration)]
    graph = ["[0:a]aformat=sample_rates=48000:channel_layouts=stereo[voice]"]
    mix = ["[voice]"]
    next_input = 1
    if source:
        cmd += ["-stream_loop", "-1", "-i", str(source)]
        graph[0] = graph[0].replace("[voice]", ",asplit=2[voice][key]")
        graph += [
            f"[1:a]aformat=sample_rates=48000:channel_layouts=stereo,atrim=0:{seconds},"
            f"volume=0.47,afade=t=in:d=0.25,afade=t=out:st={max(0, seconds - .6)}:d=0.6[bed]",
            "[bed][key]sidechaincompress=threshold=0.025:ratio=5:attack=25:release=330[duck]",
        ]
        mix.append("[duck]")
        next_input += 1
    effect = topic.get("episode_plan", {}).get("sound", "sweep")
    cue_count = 0
    for time, filename in zip(cues, ({"sweep": "whoosh", "pop": "pop", "chime": "chime"}[effect], "chime")):
        path = CACHE / "sfx" / f"{filename}.wav"
        if path.is_file():
            cmd += ["-i", str(path)]
            label = f"[cue{next_input}]"
            graph.append(f"[{next_input}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                         f"atrim=0:1,volume=0.55,adelay={round(time * 1000)}:all=1{label}")
            mix.append(label)
            next_input += 1
            cue_count += 1
    graph.append("".join(mix) + f"amix=inputs={len(mix)}:duration=first:normalize=0,"
                 "loudnorm=I=-14:TP=-1.5:LRA=11[out]")
    destination = narration.with_name("master.wav")
    subprocess.run(cmd + ["-filter_complex", ";".join(graph), "-map", "[out]", "-ar", "48000",
                          "-ac", "2", "-t", str(seconds), str(destination)], check=True)
    return destination, {"music": music, "sfx_cues": cue_count,
                         "track": source.name if music == "local track" else None}
