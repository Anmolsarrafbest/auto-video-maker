import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import shorts
import visuals
from PIL import Image
from audio_master import transition_cues
from hindi_captions import ass_filter, build_word_timeline
from render import frames, place_visual
from queue_publish import inspect_draft
from spark_job import parse_idea
from storyboard import MAX_VISUAL_SECONDS, resolve_visual_timeline, validate_diagram, validate_visual_beats


def visual_beat(index, anchor, query="blue light", kind="stock_video"):
    return {"beat_id": index, "anchor_text": anchor, "search_query": query, "visual_type": kind}


class TimedBeatsTests(unittest.TestCase):
    def test_estimated_repeated_anchors_and_bounded_segments(self):
        text = "Blue light bends. Blue light scatters."
        plan = [("hook", "Watch closely.", .14), ("answer", text, .25)]
        timings = [(.14, 1), (2, 14)]
        specs = validate_visual_beats([visual_beat(0, "Watch closely"),
                                       visual_beat(1, "Blue light"),
                                       visual_beat(2, "Blue light")],
                                      ["Watch closely.", text], 18)
        with patch("hindi_captions.recognized_words", return_value=[]) as recognize:
            words, timing_source = build_word_timeline(Path("unused.wav"), "en", plan, timings)
        recognize.assert_called_once()
        self.assertEqual(timing_source, "estimated")
        timeline = resolve_visual_timeline({"discovery": {"beats": specs}}, words, plan, timings, 18)
        self.assertEqual([segment["anchor_time"] for segment in timeline if not segment["part"]], [.14, 2, 8])
        self.assertEqual(timeline[0]["start_time"], 0)
        self.assertEqual(timeline[-1]["end_time"], 18)
        for segment in timeline:
            self.assertLessEqual(segment["end_time"] - segment["start_time"], MAX_VISUAL_SECONDS + 1e-8)
        self.assertTrue(any(segment["part"] for segment in timeline))

    def test_detected_word_start_drives_beat(self):
        plan = [("hook", "Watch closely.", .14), ("answer", "Blue light scatters.", .25)]
        timings = [(0, 1), (1.2, 4)]
        detected = [("Blue", 1.7, 1.9), ("light", 1.96, 2.2), ("scatters", 2.4, 3)]
        with patch("hindi_captions.recognized_words", return_value=detected):
            words, timing_source = build_word_timeline(Path("unused.wav"), "en", plan, timings)
        self.assertEqual(timing_source, "whisper")
        topic = {"discovery": {"beats": [visual_beat(0, "Watch closely"), visual_beat(1, "Blue light")]}}
        timeline = resolve_visual_timeline(topic, words, plan, timings, 6)
        self.assertEqual(timeline[1]["start_time"], 1.7)
        self.assertEqual(words[1][0][1], timeline[1]["start_time"])

    def test_bad_anchor_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "verbatim"):
            validate_visual_beats([visual_beat(0, "Blue light"), visual_beat(1, "Green fog")],
                                  ["Blue light scatters."], 18)

    def test_legacy_spark_and_modern_payload(self):
        old = shorts.read_json(shorts.ROOT / "spark-example.json")
        topic, seconds = parse_idea(json.dumps(old))
        self.assertEqual(seconds, 18)
        self.assertEqual(len(topic["discovery"]["shots"]), 3)
        new = {
            **old, "duration_seconds": 32,
            "beats": [visual_beat(index, anchor) for index, anchor in enumerate((
                "Look at", "Wright Flyer", "That first flight", "Orville Wright", "covered one hundred",
            ))],
        }
        topic, seconds = parse_idea(json.dumps(new))
        self.assertEqual(seconds, 32)
        self.assertEqual(len(topic["discovery"]["beats"]), 5)
        self.assertNotIn("shots", topic["discovery"])

    def test_gemini_request_uses_beat_schema(self):
        body = {
            "title": "Why blue light scatters", "category": "Physics",
            "hook": "Look at blue light.", "explanation": "Blue light scatters in air.",
            "opening_label": "Blue light", "payoff_label": "Scattering",
            "source": "https://example.org/science/scattering", "trend": "", "instagram_tags": "",
            "beats": [visual_beat(0, "Look at"), visual_beat(1, "light scatters")],
            "icon_query": "light", "real_event": False, "event_media_url": "", "event_year": "",
        }
        response = Mock(ok=True)
        response.json.return_value = {"candidates": [{"content": {"parts": [{"text": json.dumps(body)}]}}]}
        with patch.object(shorts, "secret", return_value="private-test-key"), \
                patch.object(shorts.requests, "post", return_value=response):
            requests = []
            topic = shorts.discover_topic(18, {}, [], requests_sent=requests)
        schema = requests[0]["generationConfig"]["responseSchema"]
        self.assertIn("beats", schema["required"])
        self.assertNotIn("shot_queries", schema["properties"])
        self.assertEqual(topic["discovery"]["beats"], body["beats"])
        self.assertNotIn("private-test-key", json.dumps(requests))

    def test_discovery_normalizes_beat_queries_callouts_and_stray_diagrams(self):
        body = {
            "title": "Why blue light scatters", "category": "Physics",
            "hook": "Look at blue light.", "explanation": "Blue light scatters in air molecules.",
            "opening_label": "Blue light", "payoff_label": "Scattering",
            "source": "https://example.org/science/scattering", "trend": "", "instagram_tags": "",
            "beats": [
                {
                    "beat_id": 0, "anchor_text": "Look at", "visual_type": "stock_video",
                    "search_query": "sunlight",
                    "callout_stat": "Shortest visible wavelengths scatter much more strongly",
                    "diagram": {"template": "process", "title": "Stray", "items": [{"label": "A"}, {"label": "B"}]},
                },
                {
                    "beat_id": 1, "anchor_text": "light scatters", "visual_type": "callout_card",
                    "search_query": "clear blue daytime sky, Rayleigh scattering molecules",
                },
            ],
            "icon_query": "light", "real_event": False, "event_media_url": "", "event_year": "",
        }
        response = Mock(ok=True)
        response.json.return_value = {"candidates": [{"content": {"parts": [{"text": json.dumps(body)}]}}]}
        with patch.object(shorts, "secret", return_value="private-test-key"), \
                patch.object(shorts.requests, "post", return_value=response) as post:
            topic = shorts.discover_topic(18, {}, [])
        post.assert_called_once()
        beats = topic["discovery"]["beats"]
        self.assertEqual(beats[0]["search_query"], "sunlight blue")
        self.assertLessEqual(len(beats[0]["callout_stat"]), 38)
        self.assertNotIn("diagram", beats[0])
        self.assertEqual(beats[1]["search_query"], "clear blue daytime sky")

    def test_cache_separates_beat_assets_and_skips_callout_search(self):
        specs = [visual_beat(1, "Blue light", "blue light"), visual_beat(2, "light scatters", kind="callout_card")]
        topic = {"id": "fixture", "title": "Blue light", "category": "Science",
                 "discovery": {"beats": specs}}
        with TemporaryDirectory(dir=shorts.ROOT) as directory:
            root = Path(directory)
            (root / "fixture.jpg").touch()
            record = {"file": "fixture.jpg", "kind": "image", "source": "https://example.org/archive"}
            with patch.object(visuals, "CACHE", root), patch.object(visuals, "secret", return_value=""), \
                    patch.object(visuals, "ranked_matches", return_value=[record]) as rank, \
                    patch.object(visuals, "cache", return_value=record), patch.object(visuals, "cache_icon", return_value=None):
                visuals.cache_storyboard(topic)
                selected, _ = visuals.selected_storyboard(topic)
                self.assertEqual(set(selected), {1})
                self.assertEqual(selected[1]["path"], root / "fixture.jpg")
                rank.assert_called_once()
                specs[0]["search_query"] = "different subject"
                self.assertFalse(visuals.selected_storyboard(topic)[0])
                shorts.save_json(root / "fixture.json", {"shots": [record, None, None]})
                topic["discovery"] = {"shots": ["a", "b", "c"]}
                self.assertEqual(set(visuals.selected_storyboard(topic)[0]), {0})

    def test_missing_media_uses_distinct_card_or_reframe(self):
        topic = {
            "id": "visual-fixture", "title": "Blue light", "color": "#8DE8F7", "category": "Physics",
            "format": "story", "opening": "statement", "scene": "discovery",
            "discovery": {"beats": []}, "discovery_style": "field-notes", "labels": ["Blue light", "Scattering"],
        }
        beats = ["Watch closely.", "The blue light bends. It also scatters."]
        timings = [(0, 1), (1, 3), (3, 5)]
        timeline = [
            {**visual_beat(0, "Watch closely", "blue light"), "part": 0, "start_time": 0, "end_time": 2},
            {**visual_beat(1, "Blue light", "blue light"), "part": 0, "start_time": 2, "end_time": 4},
            {**visual_beat(2, "It also scatters", "light scatters"), "part": 0, "start_time": 4, "end_time": 6},
        ]
        photo = {"kind": "image", "images": [Image.new("RGB", (100, 100))], "portrait": False}
        with patch("render.FPS", 1):
            images = list(frames(topic, beats, timings, 6, visual_timeline=timeline,
                                 storyboard={0: photo}, load_visual=lambda _: photo))
        self.assertEqual([item["rendered_as"] for item in timeline], ["image", "reframe", "callout_card"])
        self.assertNotEqual(images[1].tobytes(), images[5].tobytes())
        self.assertNotEqual(images[0].getpixel((900, 1)), images[-1].getpixel((900, 1)))

    def test_word_highlight_ends_at_detected_end(self):
        plan = [("answer", "Blue light.", 0)]
        timings = [(1, 2)]
        detected = [("Blue", 1.05, 1.3), ("light", 1.55, 1.9)]
        with patch("hindi_captions.recognized_words", return_value=detected):
            lines, _ = build_word_timeline(Path("unused.wav"), "en", plan, timings)
        with TemporaryDirectory(dir=shorts.ROOT) as directory:
            scratch = Path(directory)
            ass_filter(scratch, {}, plan, timings, 3, lines)
            events = [line for line in (scratch / "captions.ass").read_text(encoding="utf-8").splitlines()
                      if line.startswith("Dialogue:")]
        self.assertEqual(len(events), 3)
        self.assertIn("0:00:01.05,0:00:01.30", events[0])
        self.assertIn(r"{\1c&H74F8FF&}Blue{\rCaption} light.", events[0])
        self.assertIn("0:00:01.30,0:00:01.55", events[1])
        self.assertNotIn(r"\1c", events[1])
        self.assertIn(r"Blue {\1c&H74F8FF&}light.{\rCaption}", events[2])

    def test_transition_sounds_are_spaced_and_prioritize_callouts(self):
        timeline = [{"start_time": 0, "part": 0},
                    {"start_time": 2, "part": 1},
                    {"start_time": 2.25, "part": 0, "callout_stat": "120 feet"},
                    {"start_time": 3, "part": 1},
                    {"start_time": 5.9, "part": 0}]
        self.assertEqual(transition_cues(timeline, 6),
                         [(2.25, "pop", .16, -27), (3, "whoosh", .24, -31)])

    def test_landscape_has_margin_and_shadow(self):
        shot = Image.new("RGB", (1080, 600), (240, 240, 240))
        canvas = place_visual(shot, (0, 400), 1)
        self.assertEqual(canvas.getpixel((540, 650)), (240, 240, 240))
        self.assertLess(canvas.getpixel((10, 650))[0], 170)
        self.assertLess(canvas.getpixel((540, 995))[0], canvas.getpixel((10, 995))[0])

    def test_validated_diagram_config_survives_beat_normalization(self):
        config = {"template": "comparison", "title": "Flight times", "unit": "seconds",
                  "items": [{"label": "First flight", "value": 12}, {"label": "Fourth flight", "value": 59}]}
        beats = [visual_beat(0, "First flight"),
                 dict(visual_beat(1, "Fourth flight", kind="diagram"), diagram=config)]
        normalized = validate_visual_beats(beats, ["First flight. Fourth flight."], 18)
        self.assertEqual(normalized[1]["diagram"], config)
        with self.assertRaisesRegex(ValueError, "finite nonnegative"):
            validate_diagram(dict(config, items=[{"label": "a", "value": -1}, {"label": "b", "value": 1}]))
        with self.assertRaisesRegex(ValueError, "belongs"):
            validate_visual_beats([beats[0], dict(beats[1], visual_type="stock_video")],
                                  ["First flight. Fourth flight."], 18)

    def test_review_qa_reads_measured_media_and_flags_thresholds(self):
        meta = {"target_duration_seconds": 30,
                "visual_timeline": [{"beat_id": 2, "start_time": 1, "end_time": 6.2}]}
        report = "  Duration: 00:00:20.10, start: 0\n Summary:\n     I: -18.4 LUFS\n"
        with patch("queue_publish.subprocess.run", return_value=Mock(stderr=report)):
            qa = inspect_draft(Path("fixture.mp4"), meta)
        self.assertEqual(qa["duration_seconds"], 20.1)
        self.assertEqual(qa["integrated_lufs"], -18.4)
        self.assertEqual(len(qa["warnings"]), 3)

    def test_review_qa_accepts_exact_limits(self):
        meta = {"target_duration_seconds": 20,
                "visual_timeline": [{"beat_id": 4, "start_time": 2, "end_time": 7}]}
        report = "  Duration: 00:00:15.00, start: 0\n Summary:\n     I: -16.0 LUFS\n"
        with patch("queue_publish.subprocess.run", return_value=Mock(stderr=report)):
            self.assertEqual(inspect_draft(Path("fixture.mp4"), meta)["warnings"], [])


if __name__ == "__main__":
    unittest.main()
