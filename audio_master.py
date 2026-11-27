"""Build and master a CPU-only soundtrack from local media or a MIDI score."""

from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import wave

from local_secrets import secret

CACHE = Path(__file__).resolve().parent / "assets-cache"
PROGRAMS = {
    "ambient synth": (89, 81, 38), "felt piano": (0, 46, 32), "syncopated marimba": (12, 11, 32),
    "slow instrumental swell": (48, 10, 33), "orbital groove": (89, 4, 38),
}


def midi_music(destination, theme, score, topic_id, seconds):
    soundfont = CACHE / "soundfont.sf2"
    if not soundfont.is_file():
        return False
    import mido

    beat, chords, motif, percussion, _ = score
    rng = random.Random(topic_id)
    root_shift = rng.choice((-2, 0, 2))
    midi = mido.MidiFile()
    track = midi.add_track()
    track.append(mido.MetaMessage("set_tempo", tempo=round(beat * 1_000_000)))
    for channel, program in enumerate(PROGRAMS[theme]):
        track.append(mido.Message("program_change", channel=channel, program=program))
    ticks = midi.ticks_per_beat
    events = []
    groove_gain = .65 + .35 * percussion

    def add_note(position, channel, note, velocity, duration):
        start = round(position * ticks)
        vel = max(1, min(127, round(velocity)))
        events.extend(((start, channel, note, vel),
                       (start + round(duration * ticks), channel, note, 0)))

    for index in range(int(seconds / beat) + 1):
        bar_idx = (index // 4) % len(chords)
        chord = chords[bar_idx]
        beat_in_bar = index % 4
        melody_idx = (1 + motif[beat_in_bar]) % len(chord)
        melody_note = chord[melody_idx] + 12
        if bar_idx == len(chords) - 1 and beat_in_bar == 3:
            melody_note = chord[min(len(chord) - 1, melody_idx + 1)] + 12
        notes = [(1, melody_note, 68 + rng.randint(-6, 6), .65)]
        if beat_in_bar in (1, 3) and rng.random() < .75:
            passing = chord[(melody_idx + 1) % len(chord)] + 12
            add_note(index + .5, 1, passing + root_shift, 50 + rng.randint(-5, 5), .38)
        if beat_in_bar == 0:
            notes += [(0, note, 48 + rng.randint(-4, 4), 3.8) for note in chord]
        for channel, note, velocity, duration in notes:
            add_note(index, channel, note + root_shift, velocity, duration)
        backbeat = index % 2
        drum = (37 if theme in {"felt piano", "slow instrumental swell"} else 38) if backbeat else 36
        add_note(index, 9, drum, (49 if backbeat else 75) * groove_gain + rng.randint(-4, 4), .16)
        if index % 8 == 0:
            add_note(index, 9, 46, 34 * groove_gain, .35)
        for offset, velocity in ((0, 43), (.5, 29)):
            add_note(index + offset, 9, 42, (velocity + rng.randint(-5, 5)) * groove_gain, .10)
        if beat_in_bar in (0, 1, 3):
            offset = 0 if beat_in_bar == 0 else .5
            bass = chord[0] + root_shift - (12 if chord[0] > 47 else 0)
            add_note(index + offset, 2, bass + (7 if beat_in_bar == 3 else 0), 62 + rng.randint(-4, 4), .42)
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
        synth.program_select(9, bank, 128, 0)
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


def transition_cues(visual_timeline, outro):
    """Place low-peak accents while leaving room around the opening and outro."""
    cues = []
    for segment in visual_timeline:
        time = segment["start_time"]
        if time < .45 or time >= outro - .45:
            continue
        callout = not segment["part"] and (segment.get("callout_stat") or segment.get("visual_type") == "callout_card")
        if callout:
            cue = (time, "pop", .16, -27, True)
        elif segment.get("visual_type") == "archival" and not segment["part"] and (CACHE / "sfx" / "shutter.wav").is_file():
            cue = (time, "shutter", .18, -30, False)
        else:
            cue = (time, "whoosh", .24, -31, False)
        if cues and time - cues[-1][0] < .45:
            if callout and not cues[-1][-1] and (len(cues) == 1 or time - cues[-2][0] >= .45):
                cues[-1] = cue
            continue
        cues.append(cue)
    return [cue[:4] for cue in cues]


def master(ffmpeg, narration, seconds, topic, theme, score, cues, *, visual_timeline=()):
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
        space = "aecho=0.8:0.88:55:0.22," if music == "SoundFont MIDI" else ""
        fade_in, fade_out = (.04, .04) if topic.get("loop") else (.25, .6)
        graph += [
            f"[1:a]aformat=sample_rates=48000:channel_layouts=stereo,atrim=0:{seconds},"
            f"highpass=f=65,{space}volume=0.47,afade=t=in:d={fade_in},afade=t=out:st={max(0, seconds - fade_out)}:d={fade_out}[bed]",
            "[bed][key]sidechaincompress=threshold=0.025:ratio=5:attack=25:release=330[duck]",
        ]
        mix.append("[duck]")
        next_input += 1
    effect = topic.get("episode_plan", {}).get("sound", "sweep")
    cue_count = 0
    reveal, outro = cues
    accents = transition_cues(visual_timeline, outro)
    opening_pop = visual_timeline and visual_timeline[0].get("callout_stat")
    cue_plan = [(0, "pop", .16, -27) if opening_pop else (0, "whoosh", .30, -21)]
    if not topic.get("loop"):
        cue_plan.append((outro, "chime", .65, -16))
    if not any(abs(time - reveal) < .45 for time, *_ in accents):
        cue_plan.append((reveal, {"sweep": "whoosh", "pop": "pop", "chime": "chime"}[effect], .65, -19))
    peaks = {}
    placed_accents = []
    for (time, filename, duration, target), transition in sorted(
        [(cue, False) for cue in cue_plan] + [(cue, True) for cue in accents]
    ):
        path = CACHE / "sfx" / f"{filename}.wav"
        if path.is_file():
            key = (path, duration)
            if key not in peaks:
                analysis = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path), "-t", str(duration),
                                           "-af", "volumedetect", "-f", "null", "-"],
                                          capture_output=True, text=True, check=True)
                peak = re.search(r"max_volume:\s*(-?[\d.]+) dB", analysis.stderr)
                peaks[key] = float(peak[1]) if peak else 0
            gain = min(9, target - peaks[key])
            cmd += ["-i", str(path)]
            label = f"[cue{next_input}]"
            graph.append(f"[{next_input}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                         f"atrim=0:{duration},volume={gain:.2f}dB,"
                         f"afade=t=out:st={duration - .08}:d=0.08,"
                         f"adelay={round(time * 1000)}:all=1{label}")
            mix.append(label)
            next_input += 1
            cue_count += 1
            if transition:
                placed_accents.append({"time": round(time, 3), "effect": filename})
    graph.append("".join(mix) + f"amix=inputs={len(mix)}:duration=first:normalize=0,"
                 "loudnorm=I=-14:TP=-1.5:LRA=11[out]")
    destination = narration.with_name("master.wav")
    subprocess.run(cmd + ["-filter_complex", ";".join(graph), "-map", "[out]", "-ar", "48000",
                          "-ac", "2", "-t", str(seconds), str(destination)], check=True)
    return destination, {"music": music, "sfx_cues": cue_count, "transition_sfx": placed_accents,
                         "track": source.name if music == "local track" else None}
