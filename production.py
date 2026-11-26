"""Editorial choices and voice credits for each Short."""

from local_secrets import secret

VOICE = "en_US-hifi_tts_92-medium"
VOICE_SNAPSHOT = "5ec43b186ef915a195d3ddbcfd9916e76c503c3f"
ALBA = "en_GB-alba-medium"
JOE = "en_US-joe-medium"
INDIAN_ENGLISH = "Microsoft Heera"
HINDI = "Recorded Hindi narration"
KOKORO = "Kokoro ONNX"
ATTRIBUTION = (
    "Narration synthesized with Piper en_US-hifi_tts_92-medium, trained on the voice of "
    "Cori Samuel (Hi-Fi TTS reader 92), CC BY 4.0. The audio is synthesized. "
    "https://openslr.org/109/ | https://creativecommons.org/licenses/by/4.0/"
)
VOICE_CREDITS = {
    VOICE: ATTRIBUTION,
    ALBA: (
        "Narration synthesized with Piper Alba. Alba speech corpus by Cassia Valentini-Botinhao "
        "and Junichi Yamagishi, University of Edinburgh (2019), https://doi.org/10.7488/ds/2506. "
        "Base recordings: Hi-Fi TTS reader 92, Cori Samuel, https://openslr.org/109/. "
        "Both datasets are CC BY 4.0: https://creativecommons.org/licenses/by/4.0/."
    ),
    JOE: (
        "Narration synthesized with Piper Joe. Joe and rdh training recordings are CC0. "
        "https://github.com/OHF-Voice/voice-datasets"
    ),
}

# The visual treatment, soundtrack and delivery serve the topic, while a
# catalogue entry may select another narrator if it suits the episode better.
TREATMENTS = {
    "archive": (ALBA, "documentary", "felt piano"),
    "lab": (JOE, "precise", "syncopated marimba"),
    "observatory": (VOICE, "curious", "ambient synth"),
    "ocean": (ALBA, "gentle", "slow instrumental swell"),
    "copper": (VOICE, "precise", "orbital groove"),
    "paper": (ALBA, "documentary", "felt piano"),
}

# Each arrangement has its own chord progression, phrasing and instruments.
SCORES = {
    "orbital groove": (.46, ((38, 57, 60, 65, 69), (46, 57, 62, 65, 69),
                             (41, 57, 60, 64, 69), (43, 55, 62, 65, 70)), (0, 2, 3, 1), .8, 1),
    "ambient synth": (.72, ((45, 57, 60, 64, 71), (41, 53, 60, 64, 67),
                           (48, 60, 64, 67, 74), (43, 55, 62, 69, 71)), (0, 2, 1, 3), .35, .75),
    "felt piano": (.81, ((48, 60, 64, 67, 71), (45, 57, 60, 64, 69),
                        (50, 62, 65, 69, 72), (43, 55, 59, 65, 69)), (3, 1, 2, 0), .08, 1.30),
    "syncopated marimba": (.52, ((50, 62, 65, 69, 72), (43, 55, 59, 62, 67),
                                 (48, 60, 64, 67, 72), (45, 57, 61, 64, 69)), (1, 3, 0, 2), 1, 1.10),
    "slow instrumental swell": (.91, ((40, 52, 55, 59, 66), (47, 59, 62, 66, 69),
                                     (45, 57, 60, 64, 71), (43, 55, 59, 62, 67)), (0, 1, 3, 2), .18, .45),
}

TOPIC_THEMES = {"jupiter-spin": "copper", "katherine-johnson": "paper"}
TOPIC_VOICES = {
    "wright-first-flight": JOE,
    "leavitt-stars": VOICE,
    "saturn-rings": JOE,
    "mars-sunset": ALBA,
}


def treatment(topic):
    theme = {
        "History": "archive",
        "Physics": "lab",
        "Space": "observatory",
        "Ocean": "ocean",
    }.get(topic.get("category"), "observatory")
    if topic.get("format") == "demo":
        theme = "lab"
    theme = TOPIC_THEMES.get(topic.get("id"), theme)
    voice, delivery, soundtrack = TREATMENTS[theme]
    voice = TOPIC_VOICES.get(topic.get("id"), voice)
    language = topic.get("language", "en")
    if language == "en-IN":
        voice = INDIAN_ENGLISH
    elif language == "hi":
        voice = HINDI
    result = {
        "theme": theme,
        "voice": voice,
        "delivery": delivery,
        "soundtrack": soundtrack,
    }
    if language != "en":
        result["language"] = language
    kokoro_voice = secret("SHORTS_KOKORO_VOICE").strip()
    if language == "en" and kokoro_voice:
        if kokoro_voice == "auto":
            kokoro_voice = {VOICE: "af_heart", ALBA: "bf_emma", JOE: "am_michael"}[voice]
        result.update(voice=KOKORO, kokoro_voice=kokoro_voice)
    return result


def voice_credit(voice):
    extra = {
        INDIAN_ENGLISH: "Narration synthesized locally with Microsoft Heera, Windows English (India).",
        HINDI: "हिंदी वाचन इस वीडियो के लिए रिकॉर्ड किया गया है।",
        KOKORO: "Narration synthesized locally with Kokoro-82M. https://huggingface.co/hexgrad/Kokoro-82M",
    }
    return extra[voice] if voice in extra else VOICE_CREDITS[voice]
