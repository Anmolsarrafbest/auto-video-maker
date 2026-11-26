"""Episode plans shared by the renderer and the exported Flow prompt."""

from production import treatment

DURATIONS = (10, 12, 15, 18, 20, 25)
CLIPS = {
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


def episode(topic):
    if "discovery" in topic:
        return (15, "documentary", *topic["discovery"]["shots"])
    style = topic.get("format", "quiz")
    default = {"story": (20, "documentary"), "demo": (18, "experiment"),
               "myth": (12, "experiment"), "quiz": (12, "magazine")}[style]
    return EPISODES.get(topic["id"], (*default, f'Open on {topic["labels"][0].lower()}',
                        f'Change framing to illustrate {topic["labels"][1].lower()}',
                        "End on a clear visual payoff that directly supports the narrated fact."))


def flow_prompt(topic, timings, seconds, plan, clip_used):
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
    return (
        f'{seconds}-second vertical 9:16 {layout} episode: {topic["title"]}\n'
        f'Factual reference: {topic["source"]}\n{asset}\n'
        f'0.00–{reveal:.2f}s | Establishing shot. {opening}.\n'
        f'{reveal:.2f}–{end_shot:.2f}s | Explanation. {explanation}.\n'
        f'{end_shot:.2f}–{seconds:.2f}s | Payoff. {payoff}\n\n'
        f'Exact spoken track ({topic.get("language", "en")}):\n{narration}\n\n'
        f'Delivery: {direction["delivery"]}, with natural emphasis on the fact. '
        'Preserve the timing of pauses. Keep the lower center clear for captions. '
        'Use hard cuts or motivated reframing. Keep motion slow enough to read on a phone. '
        'Avoid invented numbers, logos, fake archive material, and generated on-screen text. '
        'Use an instrumental arrangement under the voice and let it rise in the gaps. '
        'Add a readable “Follow and subscribe” overlay in the final two seconds during editing. '
        'If a single generation is too short, render the shots separately and join them in this order.\n'
    )
