"""Episode plans shared by the renderer and the exported Flow prompt."""

import math
import re

from production import treatment

MIN_DURATION_SEC = 12
MAX_DURATION_SEC = 85
DURATIONS = (10, 12, 15, 18, 20, 25)
DISCOVERY_DURATIONS = range(MIN_DURATION_SEC, MAX_DURATION_SEC + 1)
VISUAL_TYPES = ("archival", "stock_video", "diagram", "callout_card", "3d_render")
DIAGRAM_TEMPLATES = ("timeline", "comparison", "process")
MAX_VISUAL_SECONDS = 4.5
MAX_BEAT_SECONDS = 5.5
CLIPS = {
    "wright-first-flight": {
        "file": "wright-onboard-italy-1909.ogv",
        "url": "https://upload.wikimedia.org/wikipedia/commons/e/ee/Wilbur_Wright_onboard_flight%2C_Italy_1909.ogv",
        "source": "https://commons.wikimedia.org/wiki/File:Wilbur_Wright_onboard_flight,_Italy_1909.ogv",
        "credit": "Wilbur Wright onboard flight in Italy, 1909 · Wikimedia Commons · Public Domain Mark 1.0.",
        "label": "LATER ARCHIVAL FOOTAGE · ITALY, 1909",
        "context": "Open with the first-flight photograph, then show the later film only when the narration reaches Italy.",
        "start_on_last_line": True,
        "offset": 8,
    },
    "jupiter-spin": {
        "file": "cassini-jupiter-map.mov",
        "url": "https://assets.science.nasa.gov/content/dam/science/psd/solar/2023/09/iss_001227a_med.mov",
        "source": "https://science.nasa.gov/resource/cassinis-first-color-movie-of-jupiter/",
        "credit": "Cassini camera map sequence (PIA02863): NASA/JPL/University of Arizona.",
        "label": "CASSINI · CLOUD MAP TIME-LAPSE, 2000",
        "context": "Mapped spacecraft observations of cloud motion across 24 rotations, not a real-time view.",
    },
    "moon-distance": {
        "file": "moon-turntable.mp4",
        "url": "https://svs.gsfc.nasa.gov/vis/a000000/a005300/a005319/moon_turntable_720p30.mp4",
        "source": "https://svs.gsfc.nasa.gov/5319/",
        "credit": "Lunar rotation visualization: NASA Scientific Visualization Studio.",
        "label": "NASA SVS · LUNAR ROTATION",
        "context": "A simulation of rotation. Use only as a lunar close-up.",
    },
    "mars-sunset": {
        "file": "mars-curiosity-sunset.gif",
        "url": "https://assets.science.nasa.gov/content/dam/science/psd/photojournal/pia/pia19/pia19401/PIA19401.gif",
        "source": "https://science.nasa.gov/photojournal/sunset-sequence-in-mars-gale-crater-animation/",
        "credit": "Curiosity Mastcam sunset sequence (PIA19401): NASA/JPL-Caltech/MSSS/Texas A&M Univ.",
        "label": "CURIOSITY MASTCAM · APRIL 15, 2015",
        "context": "Four rover photographs taken about seven minutes apart in total. Present as a time-lapse.",
    },
}
EPISODES = {
    "moon-distance": (20, "space film", "Establish the lunar surface while the narrator introduces the Apollo reflectors",
                      "Explain the round-trip laser measurement with a clearly labeled diagram",
                      "Show the widening gap with a fixed reference line. Mark the diagram as not to scale."),
    "mars-sunset": (12, "space film", "Open low over the Martian horizon",
                    "Push toward the setting Sun as the blue halo emerges",
                    "Hold the blue light close to the Sun and keep the surrounding terrain rusty."),
    "katherine-johnson": (25, "documentary", "Hold on the credited NASA portrait, with room for the opening caption",
                          "Cut to an orbit and a calculation-check graphic",
                          "Keep the 1971 portrait date distinct from the 1962 flight. Avoid fabricated historical footage."),
    "rainbow-split": (18, "experiment", "Follow one beam into a transparent raindrop",
                      "Switch to a close cross-section while the ray bends and reflects",
                      "Separate the colors on exit. Keep the geometry readable and show only one internal reflection."),
}


def anchor_tokens(text):
    return [re.sub(r"[^\w]", "", word.casefold()) for word in text.split()
            if re.search(r"\w", word)]


def find_anchor(words, anchor, cursor):
    return next((index for index in range(cursor, len(words) - len(anchor) + 1)
                 if words[index:index + len(anchor)] == anchor), None)


def validate_diagram(data):
    if not isinstance(data, dict) or data.get("template") not in DIAGRAM_TEMPLATES:
        raise ValueError("Choose timeline, comparison, or process for the diagram template.")
    template = data["template"]
    items = data.get("items")
    if not isinstance(items, list) or not 2 <= len(items) <= 4:
        raise ValueError("Diagram templates need two to four items.")
    title = data.get("title", "")
    unit = data.get("unit", "")
    if not isinstance(title, str) or len(title) > 40 or "\n" in title or "\r" in title:
        raise ValueError("Use a one-line diagram title of at most 40 characters.")
    if not isinstance(unit, str) or len(unit) > 12 or "\n" in unit or "\r" in unit:
        raise ValueError("Use a short unit for comparison values.")
    normalized = []
    for item in items:
        label = item.get("label") if isinstance(item, dict) else None
        if not isinstance(label, str) or not 0 < len(label.strip()) <= 32 or "\n" in label or "\r" in label:
            raise ValueError("Use one short label for each diagram item.")
        entry = {"label": label.strip()}
        if template == "comparison":
            value = item.get("value")
            if type(value) not in (int, float) or not 0 <= value <= 1e15 or not math.isfinite(value):
                raise ValueError("Comparison values must be finite nonnegative numbers.")
            entry["value"] = value
        normalized.append(entry)
    if template == "comparison" and not any(item["value"] for item in normalized):
        raise ValueError("A comparison needs at least one positive value.")
    return {"template": template, "title": title.strip(), "unit": unit.strip(), "items": normalized}


def validate_visual_beats(beats, narration, seconds=None):
    minimum = 5 if (seconds is not None and seconds >= 30) else 2
    if not isinstance(beats, list) or not minimum <= len(beats) <= 12:
        raise ValueError(f"Supply {minimum} to 12 visual beats.")
    words = anchor_tokens(" ".join(narration))
    cursor = 0
    cleaned = []
    stat_count = 0
    for beat in beats:
        if not isinstance(beat, dict):
            raise ValueError("Each visual beat must be an object.")
        beat_id = beat.get("beat_id")
        expected_ids = (cleaned[-1]["beat_id"] + 1,) if cleaned else (0, 1)
        if type(beat_id) is not int or beat_id not in expected_ids:
            raise ValueError("Visual beat IDs must be consecutive and start at zero or one.")
        anchor, query, stat = (beat.get("anchor_text"), beat.get("search_query"), beat.get("callout_stat", ""))
        if not isinstance(anchor, str) or len(anchor) > 100 or not 2 <= len(anchor_tokens(anchor)) <= 5:
            raise ValueError(f"Beat {beat_id} needs a 2–5 word narration anchor.")
        found = find_anchor(words, anchor_tokens(anchor), cursor)
        if found is None:
            raise ValueError(f"Beat {beat_id} anchor must appear verbatim and in order in the narration.")
        cursor = found + 1
        kind = beat.get("visual_type")
        if kind not in VISUAL_TYPES:
            raise ValueError(f"Beat {beat_id} has an unsupported visual type.")
        if (not isinstance(query, str) or len(query) > 70
                or not re.fullmatch(r"[\w'-]+(?: [\w'-]+){1,3}", query.strip())):
            raise ValueError(f"Beat {beat_id} needs a 2–4 word search query.")
        if not isinstance(stat, str) or len(stat) > 38 or any(mark in stat for mark in "\r\n"):
            raise ValueError(f"Beat {beat_id} callout must fit on one short line.")
        record = {"beat_id": beat_id, "anchor_text": anchor.strip(), "visual_type": kind,
                  "search_query": query.strip(), **({"callout_stat": stat.strip()} if stat.strip() else {})}
        if "stat_callout" in beat:
            callout = beat["stat_callout"]
            if not isinstance(callout, dict) or any(
                not isinstance(callout.get(key), str) or not 1 <= len(callout[key].strip()) <= limit
                or any(ord(char) < 32 for char in callout[key])
                for key, limit in (("value", 24), ("label", 32))
            ):
                raise ValueError(f"Beat {beat_id} needs a short stat callout value and label.")
            if stat_count < 2:
                record["stat_callout"] = {key: callout[key].strip() for key in ("value", "label")}
                stat_count += 1
        fallback = beat.get("fallback_video_query", "")
        if fallback != "":
            if (not isinstance(fallback, str) or len(fallback) > 48
                    or not re.fullmatch(r"[\w'-]+(?: [\w'-]+)?", fallback.strip())):
                raise ValueError(f"Beat {beat_id} needs a 1–2 word fallback video query.")
            record["fallback_video_query"] = fallback.strip()
        if "diagram" in beat:
            if kind != "diagram":
                raise ValueError("Diagram data belongs on a diagram beat.")
            record["diagram"] = validate_diagram(beat["diagram"])
        cleaned.append(record)
    return cleaned


def visual_beat_text(beats):
    for beat in beats:
        yield beat["search_query"]
        yield beat.get("fallback_video_query", "")
        yield beat.get("callout_stat", "")
        yield from beat.get("stat_callout", {}).values()
        if diagram := beat.get("diagram"):
            yield diagram["title"]
            yield diagram["unit"]
            yield from (item["label"] for item in diagram["items"])


def split_sparse_beats(beats, narration_lines, estimated_seconds, *, max_beat_seconds=MAX_BEAT_SECONDS,
                       subject_keywords=()):
    """Split beats whose estimated span exceeds max_beat_seconds into distinct sub-beats."""
    if not beats or estimated_seconds <= max_beat_seconds:
        return beats
    raw_words = [w for line in narration_lines for w in str(line).split() if re.search(r"\w", w)]
    norm_tokens = [re.sub(r"[^\w]", "", w.casefold()) for w in raw_words]
    if len(norm_tokens) < 4:
        return beats
    positions = []
    cursor = 0
    for beat in beats:
        anchor = anchor_tokens(beat.get("anchor_text", ""))
        found = find_anchor(norm_tokens, anchor, cursor) if anchor else None
        if found is None:
            return beats
        positions.append(found)
        cursor = found + 1

    subject_clean = [re.sub(r"[^\w'-]", "", kw) for kw in subject_keywords if re.search(r"\w", str(kw))]
    stop = {"a", "an", "and", "are", "as", "at", "by", "for", "from", "has", "have", "in", "into",
            "is", "it", "its", "of", "on", "or", "that", "the", "their", "this", "to", "was", "with"}
    result = []
    total_tokens = len(raw_words)
    for idx, beat in enumerate(beats):
        result.append(dict(beat))
        start_tok = positions[idx] if idx > 0 else 0
        end_tok = positions[idx + 1] if idx + 1 < len(positions) else total_tokens
        span_seconds = estimated_seconds * max(0, end_tok - start_tok) / max(1, total_tokens)
        if span_seconds <= max_beat_seconds or end_tok - start_tok < 4:
            continue
        chunks = math.ceil(span_seconds / max_beat_seconds)
        step = (end_tok - start_tok) / chunks
        for sub in range(1, chunks):
            if len(result) >= 12:
                break
            mid = round(start_tok + sub * step)
            if mid <= start_tok or mid + 2 > end_tok:
                continue
            anchor_slice = [re.sub(r"[^\w'-]", "", w) for w in raw_words[mid:mid + 3]]
            anchor_slice = [w for w in anchor_slice if re.search(r"\w", w)]
            if len(anchor_slice) < 2:
                continue
            local_words = [
                re.sub(r"[^\w'-]", "", w).casefold()
                for w in raw_words[mid:min(end_tok, mid + 8)]
            ]
            content = [w for w in local_words if len(w) > 2 and w not in stop]
            query_words = list(dict.fromkeys([*subject_clean[:1], *content[:2], *subject_clean[1:2]]))[:4]
            if len(query_words) < 2:
                query_words = (query_words + anchor_slice)[:2]
            result.append({
                "beat_id": len(result),
                "anchor_text": " ".join(anchor_slice),
                "visual_type": "stock_video",
                "search_query": " ".join(query_words[:4]),
            })
    for new_id, beat in enumerate(result):
        beat["beat_id"] = new_id
    return result


def resolve_visual_timeline(topic, word_lines, plan, timings, seconds):
    """Map script anchors and legacy slots onto bounded visual segments."""
    discovery = topic.get("discovery", {})
    specs = discovery.get("beats")
    entries = []
    if specs:
        tokens = [(token, start) for line in word_lines for word, start, _ in line
                  for token in anchor_tokens(word)]
        words = [token for token, _ in tokens]
        cursor = 0
        for spec in specs:
            match = find_anchor(words, anchor_tokens(spec["anchor_text"]), cursor)
            if match is None:
                raise ValueError(f'Visual beat {spec["beat_id"]} no longer matches the spoken narration.')
            cursor = match + 1
            entries.append((tokens[match][1], spec))
    elif discovery:
        answers = [(start, text) for (role, text, _), (start, _) in zip(plan, timings) if role == "answer"]
        reveal, first_answer = answers[0]
        payoff, final_answer = answers[-1]
        if len(answers) == 1:
            words = word_lines[-1]
            payoff = words[len(words) // 2][1] if len(words) > 1 else (reveal + seconds) / 2
        entries = [(0, {"beat_id": 0, "anchor_text": plan[0][1], "callout_stat": topic["labels"][0]}),
                   (reveal, {"beat_id": 1, "anchor_text": first_answer}),
                   (payoff, {"beat_id": 2, "anchor_text": final_answer, "callout_stat": topic["labels"][1]})]
        # Legacy plans have one middle asset. Give additional explanation
        # sentences their own distinct callout without renumbering cached slots.
        entries[2:2] = [(start, {"beat_id": index + 3, "anchor_text": text})
                        for index, (start, text) in enumerate(answers[1:-1])]
    if not entries:
        return []
    timeline = []
    for index, (anchor_time, spec) in enumerate(entries):
        start = anchor_time if index else 0
        end = entries[index + 1][0] if index + 1 < len(entries) else seconds
        if end <= start:
            continue
        count = max(1, math.ceil((end - start) / MAX_VISUAL_SECONDS))
        for part in range(count):
            timeline.append({**spec, "anchor_time": anchor_time, "part": part,
                             "start_time": start + (end - start) * part / count,
                             "end_time": start + (end - start) * (part + 1) / count})
    return timeline


def episode(topic):
    if "discovery" in topic:
        shots = topic["discovery"].get("shots", ("Establish the subject", "Develop the story", "Show the payoff"))
        return (15, "documentary", *shots)
    style = topic.get("format", "quiz")
    default = {"story": (20, "documentary"), "demo": (18, "experiment"),
               "myth": (12, "experiment"), "quiz": (12, "magazine")}[style]
    return EPISODES.get(topic["id"], (*default, f'Open on {topic["labels"][0].lower()}',
                        f'Change framing to illustrate {topic["labels"][1].lower()}',
                        "End on a clear visual payoff that directly supports the narrated fact."))


def flow_prompt(topic, timings, seconds, plan, clip_used, *, visual_timeline=()):
    _, layout, opening, explanation, payoff = episode(topic)
    reveal = (next(start for (start, _), (role, _, _) in zip(timings, plan) if role == "answer")
              if plan[0][0] == "hook" else min(2.0, seconds / 4))
    end_shot = min(seconds - .25, max(reveal + .5, seconds - 3))
    direction = treatment(topic)
    narration = "\n".join(
        f"{start:.2f}–{end:.2f}s [{role}]: {line}"
        for (start, end), (role, line, _) in zip(timings, plan)
    )
    asset = ""
    if clip_used:
        clip = CLIPS[topic["id"]]
        asset = f'Approved archive insert: {clip["source"]}. {clip["context"]}\n'
    visual_directions = (
        "\n".join(f'{beat["start_time"]:.2f}–{beat["end_time"]:.2f}s | '
                  f'{beat.get("visual_type", "visual")} | {beat.get("search_query") or beat["anchor_text"]}'
                  for beat in visual_timeline)
        if visual_timeline else
        f'0.00–{reveal:.2f}s | Establishing shot. {opening}.\n'
        f'{reveal:.2f}–{end_shot:.2f}s | Explanation. {explanation}.\n'
        f'{end_shot:.2f}–{seconds:.2f}s | Payoff. {payoff}'
    )
    ending_note = (
        'End cleanly for a seamless loop back to the opening shot without an outro card. '
        if topic.get("loop") else
        'Add a readable “Follow and subscribe” overlay in the final two seconds during editing. '
    )
    return (
        f'{seconds}-second vertical 9:16 {layout} episode: {topic["title"]}\n'
        f'Factual reference: {topic["source"]}\n{asset}\n'
        f'{visual_directions}\n\n'
        f'Exact spoken track ({topic.get("language", "en")}):\n{narration}\n\n'
        f'Delivery: {direction["delivery"]}, with natural emphasis on the fact. '
        'Preserve the timing of pauses. Keep the lower center clear for captions. '
        'Use hard cuts or motivated reframing. Keep motion slow enough to read on a phone. '
        'Avoid invented numbers, logos, fake archive material, and generated on-screen text. '
        'Use an instrumental arrangement under the voice and let it rise in the gaps. '
        + ending_note +
        'If a single generation is too short, render the shots separately and join them in this order.\n'
    )
