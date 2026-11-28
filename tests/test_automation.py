from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import instagram_publish
import notify
import queue_publish
import shorts
import youtube


class AutomationTests(unittest.TestCase):
    def test_published_history_dedup_and_merge(self):
        with TemporaryDirectory(dir=shorts.ROOT) as directory:
            root = Path(directory)
            history_path = root / "published_history.json"
            job = root / "discovered-moon-123"
            job.mkdir()
            shorts.save_json(job / "metadata.json", {
                "topic_id": "discovered-moon",
                "title": "Why the Moon drifts away",
                "qa": {"warnings": ["Exported runtime 14s differs from target."]},
            })
            shorts.record_published_history(job, youtube_url="https://youtu.be/abc123", path=history_path)
            shorts.record_published_history(job, instagram_url="https://www.instagram.com/reel/xyz/", path=history_path)

            entries = shorts.load_published_history(history_path)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["title"], "Why the Moon drifts away")
            self.assertEqual(entries[0]["topic_id"], "discovered-moon")
            self.assertEqual(entries[0]["youtube_url"], "https://youtu.be/abc123")
            self.assertEqual(entries[0]["instagram_url"], "https://www.instagram.com/reel/xyz/")
            self.assertEqual(entries[0]["qa_warnings"], ["Exported runtime 14s differs from target."])

            with patch.object(shorts, "completed_jobs", return_value=[]):
                avoid = shorts.discovery_avoid_titles(
                    [{"title": "Why the Moon drifts away"}, {"title": "Saturn rings"}],
                    history_path=history_path,
                )
            self.assertEqual(avoid, ["Why the Moon drifts away", "Saturn rings"])

    def test_youtube_contains_synthetic_media_mapping(self):
        base = {"title": "Test", "description": "Desc", "made_for_kids": False}
        self.assertTrue(youtube.build_upload_body({**base, "synthetic_media": True}, "public")["status"]["containsSyntheticMedia"])
        self.assertFalse(youtube.build_upload_body({**base, "synthetic_media": False}, "public")["status"]["containsSyntheticMedia"])
        self.assertFalse(youtube.build_upload_body(base, "public")["status"]["containsSyntheticMedia"])

    def test_qa_gate_blocks_hard_failures_only(self):
        with TemporaryDirectory(dir=shorts.ROOT) as directory:
            root = Path(directory)
            soft_job = root / "soft-job"
            soft_job.mkdir()
            (soft_job / "short.mp4").write_bytes(b"mp4-bytes")
            shorts.save_json(soft_job / "metadata.json", {
                "qa": {
                    "duration_seconds": 15.0,
                    "integrated_lufs": -14.2,
                    "warnings": ["Exported runtime 15s differs from the 24s target by more than 25%."],
                }
            })
            soft = queue_publish.qa_gate(soft_job)
            self.assertTrue(soft["passed"])
            self.assertEqual(len(soft["warnings"]), 1)

            hard_job = root / "hard-job"
            hard_job.mkdir()
            (hard_job / "short.mp4").write_bytes(b"mp4-bytes")
            shorts.save_json(hard_job / "metadata.json", {
                "qa": {
                    "duration_seconds": 15.0,
                    "integrated_lufs": None,
                    "warnings": ["Visual beat 0 holds for 6.10s (limit 5.0s)."],
                }
            })
            hard = queue_publish.qa_gate(hard_job)
            self.assertFalse(hard["passed"])
            self.assertGreaterEqual(len(hard["errors"]), 2)

    def test_instagram_token_expiry_warnings(self):
        now = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
        with patch.object(instagram_publish, "secret", return_value="2026-12-02"):
            warn = instagram_publish.check_token_expiry(now=now)
        self.assertIn("expires in", warn)

        with patch.object(instagram_publish, "secret", return_value="2027-01-15"):
            self.assertIsNone(instagram_publish.check_token_expiry(now=now))

        future_ts = int((now + timedelta(days=3)).timestamp())
        mock_resp = Mock(ok=True)
        mock_resp.json.return_value = {"data": {"expires_at": future_ts}}
        with patch.object(instagram_publish, "secret", side_effect=lambda name: "" if name == "INSTAGRAM_TOKEN_EXPIRES_AT" else "tok"), \
                patch.object(instagram_publish.shorts.requests, "get", return_value=mock_resp):
            warn = instagram_publish.check_token_expiry(now=now)
        self.assertIn("expires in 3 day(s)", warn)

    def test_notify_formatter_and_silent_noop(self):
        msg = notify.format_summary(
            {
                "attempted": 2,
                "completed_renders": 2,
                "published": 1,
                "items": [
                    {
                        "title": "Wombat Cubes",
                        "youtube_url": "https://youtu.be/111",
                        "instagram_url": "https://instagram.com/reel/222",
                        "qa_warnings": ["Loudness -16.5 LUFS"],
                    },
                    {
                        "title": "Broken Short",
                        "qa_skipped": True,
                        "error": "Rendered video file is missing or empty.",
                    },
                ],
            },
            status="success",
            token_warnings=["Instagram token expires in 4 day(s)."],
            run_url="https://github.com/example/runs/1",
        )
        self.assertIn("Auto Video Maker (SUCCESS)", msg)
        self.assertIn("https://youtu.be/111", msg)
        self.assertIn("QA skipped:", msg)
        self.assertIn("Token warning: Instagram token expires in 4 day(s).", msg)

        with patch.object(notify, "secret", return_value=""), patch.object(notify.requests, "post") as post:
            self.assertEqual(notify.send_notifications(msg), [])
        post.assert_not_called()

        ok_resp = Mock()
        ok_resp.raise_for_status = Mock()
        with patch.object(notify.requests, "post", return_value=ok_resp) as post:
            sent = notify.send_notifications(msg, discord_url="https://discord.example/hook", telegram_token="bot1", telegram_chat_id="99")
        self.assertEqual(sent, ["discord", "telegram"])
        self.assertEqual(post.call_count, 2)

    def test_ntfy_priority_actions_and_unset_skip(self):
        with patch.object(notify, "secret", return_value=""), patch.object(notify.requests, "post") as post:
            sent = notify.notify_run(
                status="success",
                summary={"attempted": 1, "completed_renders": 1, "published": 1, "items": [
                    {"title": "Wombat Cubes", "youtube_url": "https://youtu.be/abc"}
                ]},
                run_url="https://github.com/org/repo/actions/runs/42",
            )
        self.assertEqual(sent, [])
        post.assert_not_called()

        ok_resp = Mock()
        ok_resp.raise_for_status = Mock()
        with patch.object(notify, "secret", side_effect=lambda name: "my-secret-topic" if name == "NTFY_TOPIC" else ""), \
                patch.object(notify.requests, "post", return_value=ok_resp) as post:
            sent = notify.notify_run(
                status="success",
                summary={"attempted": 2, "completed_renders": 2, "published": 2, "items": [
                    {"title": "Wombat Cubes", "youtube_url": "https://youtu.be/abc"}
                ]},
                run_url="https://github.com/org/repo/actions/runs/42",
            )
        self.assertEqual(sent, ["ntfy"])
        url, kwargs = post.call_args.args[0], post.call_args.kwargs
        self.assertEqual(url, "https://ntfy.sh/my-secret-topic")
        self.assertEqual(kwargs["headers"]["Priority"], "3")
        self.assertEqual(kwargs["headers"]["Title"], "Auto Video Maker: 2 video(s) uploaded")
        self.assertEqual(kwargs["headers"]["Tags"], "white_check_mark,clapper")
        self.assertEqual(kwargs["headers"]["Actions"], "view, Open Video, https://youtu.be/abc, clear=true")

        with patch.object(notify, "secret", side_effect=lambda name: "my-secret-topic" if name == "NTFY_TOPIC" else ""), \
                patch.object(notify.requests, "post", return_value=ok_resp) as post:
            notify.notify_run(
                status="failure",
                summary={"attempted": 1, "completed_renders": 0, "published": 0, "items": [
                    {"title": "Broken", "qa_skipped": True, "error": "Missing audio"}
                ]},
                run_url="https://github.com/org/repo/actions/runs/99",
            )
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Priority"], "5")
        self.assertEqual(kwargs["headers"]["Title"], "Auto Video Maker: Run Failed")
        self.assertEqual(kwargs["headers"]["Tags"], "rotating_light,warning")
        self.assertEqual(kwargs["headers"]["Actions"], "view, Open Run Logs, https://github.com/org/repo/actions/runs/99, clear=true")

        summary, summary_text = notify._resolve_summary_input("Uploaded 1 draft cleanly")
        self.assertEqual(summary, {})
        self.assertEqual(summary_text, "Uploaded 1 draft cleanly")

    def test_discovery_accepts_long_natural_narration_without_17s_25s_cap(self):
        explanation = " ".join(f"Sentence number {i} explains how deep ocean vents support life." for i in range(1, 13))
        body = {
            "title": "Deep ocean vents", "category": "Ocean",
            "hook": "Look at volcanic chimneys on the dark sea floor.",
            "explanation": explanation,
            "opening_label": "Ocean vents", "payoff_label": "Chemical life",
            "source": "https://example.org/science/vents", "trend": "", "instagram_tags": "",
            "beats": [
                {"beat_id": 0, "anchor_text": "Look at volcanic", "visual_type": "stock_video", "search_query": "ocean vent"},
                {"beat_id": 1, "anchor_text": "Sentence number 1", "visual_type": "stock_video", "search_query": "deep sea"},
                {"beat_id": 2, "anchor_text": "Sentence number 4", "visual_type": "diagram", "search_query": "vent diagram"},
                {"beat_id": 3, "anchor_text": "Sentence number 8", "visual_type": "stock_video", "search_query": "tube worms"},
            ],
            "icon_query": "ocean", "real_event": False, "event_media_url": "", "event_year": "",
        }
        response = Mock(ok=True)
        response.json.return_value = {"candidates": [{"content": {"parts": [{"text": json.dumps(body)}]}}]}
        for target in (None, 17):
            with patch.object(shorts, "secret", return_value="key"), \
                    patch.object(shorts.requests, "post", return_value=response) as post:
                topic = shorts.discover_topic(target, {}, [])
            post.assert_called_once()
            self.assertEqual(topic["beats"][1], explanation)

    def test_discovery_condenses_and_gracefully_trims_over_85s_scripts(self):
        long_sentences = [
            f"Fact block {i} reveals how ancient builders mixed volcanic ash with warm seawater carefully."
            for i in range(1, 18)
        ]
        body = {
            "title": "Ancient Roman Concrete", "category": "History",
            "hook": "Touch harbor concrete poured two thousand years ago.",
            "explanation": " ".join(long_sentences),
            "opening_label": "Roman concrete", "payoff_label": "Self healing",
            "source": "https://example.org/history/concrete", "trend": "", "instagram_tags": "",
            "beats": [
                {"beat_id": 0, "anchor_text": "Touch harbor concrete", "visual_type": "archival", "search_query": "roman harbor"},
                {"beat_id": 1, "anchor_text": "Fact block 1", "visual_type": "stock_video", "search_query": "volcanic ash"},
                {"beat_id": 2, "anchor_text": "Fact block 17", "visual_type": "stock_video", "search_query": "ancient stone"},
            ],
            "icon_query": "column", "real_event": False, "event_media_url": "", "event_year": "",
        }
        response = Mock(ok=True)
        response.json.return_value = {"candidates": [{"content": {"parts": [{"text": json.dumps(body)}]}}]}
        requests_sent = []
        with patch.object(shorts, "secret", return_value="key"), \
                patch.object(shorts.requests, "post", return_value=response) as post:
            topic = shorts.discover_topic(None, {}, [], requests_sent=requests_sent)
        self.assertEqual(post.call_count, 3)
        self.assertIn("Condense", requests_sent[1]["contents"][0]["parts"][0]["text"])
        self.assertTrue(topic.get("script_warnings"))
        self.assertLessEqual(sum(len(line.split()) for line in topic["beats"]), 190)
        self.assertTrue(topic["beats"][1].endswith("."))
        self.assertEqual([b["beat_id"] for b in topic["discovery"]["beats"]], [0, 1])

    def test_audio_driven_render_job_and_post_tts_85s_trim(self):
        with TemporaryDirectory(dir=shorts.ROOT) as directory:
            root = Path(directory)
            voices_dir = root / "voices"
            voices_dir.mkdir()
            (voices_dir / "en_US-lessac-medium.onnx").write_bytes(b"stub")
            topic = {
                "id": "discovered-audio-test", "title": "Dynamic duration", "category": "Physics",
                "format": "story", "opening": "statement", "scene": "discovery", "color": "#91DFDA",
                "beats": [
                    "Watch this pendulum.",
                    "First sentence moves slowly across the room. Second sentence extends far beyond eighty five seconds.",
                ],
                "source": "https://example.org/pendulum", "aliases": [],
                "labels": ["Pendulum", "Gravity"],
                "discovery": {
                    "trend": "", "instagram_tags": [], "icon_query": "clock", "real_event": False, "event_year": "",
                    "beats": [
                        {"beat_id": 0, "anchor_text": "Watch this pendulum", "visual_type": "callout_card", "search_query": "pendulum swing"},
                        {"beat_id": 1, "anchor_text": "First sentence moves", "visual_type": "stock_video", "search_query": "metal pendulum"},
                        {"beat_id": 2, "anchor_text": "Second sentence extends", "visual_type": "stock_video", "search_query": "clock pendulum"},
                    ],
                },
            }
            def create_fake_encode(t, beats, timings, secs, wav, mp4, *a, **kw):
                mp4.write_bytes(b"mp4")
                (wav.parent / "captions.ass").write_text("", encoding="utf-8")
                return {
                    "visual_credits": [], "visual_timeline": [], "caption_timing": "estimated", "diagram_renderer": "pillow",
                }

            with patch.object(shorts, "VOICE_DIR", voices_dir), \
                    patch.object(shorts, "encode", side_effect=create_fake_encode), \
                    patch.object(shorts, "narration", return_value=[(0.14, 2.0), (2.25, 36.4)]), \
                    patch("queue_publish.inspect_draft", return_value={"duration_seconds": 39.0, "integrated_lufs": -14.0, "warnings": []}), \
                    patch("visuals.selected_storyboard", return_value=({}, None)):
                job1 = root / "job1"
                shorts.render_job(topic, "Discovery", None, job1)
                meta1 = shorts.read_json(job1 / "metadata.json")
                self.assertEqual(meta1["duration_seconds"], 39)
                self.assertIsNone(meta1["target_duration_seconds"])

            long_tts = [(0.14, 2.0), (2.25, 58.0), (58.2, 90.5)]
            short_tts = [(0.14, 2.0), (2.25, 58.0)]
            with patch.object(shorts, "VOICE_DIR", voices_dir), \
                    patch.object(shorts, "encode", side_effect=create_fake_encode), \
                    patch.object(shorts, "narration", side_effect=[long_tts, short_tts]), \
                    patch("queue_publish.inspect_draft", return_value={"duration_seconds": 60.0, "integrated_lufs": -14.0, "warnings": []}), \
                    patch("visuals.selected_storyboard", return_value=({}, None)):
                job2 = root / "job2"
                shorts.render_job(topic, "Discovery", None, job2)
                meta2 = shorts.read_json(job2 / "metadata.json")
                self.assertEqual(meta2["duration_seconds"], 60)
                self.assertTrue(any("85s" in w for w in meta2["qa"]["warnings"]))
                self.assertEqual(len(meta2["discovery"]["beats"]), 2)

    def test_qa_gate_allows_audio_driven_48s_and_blocks_over_85s(self):
        with TemporaryDirectory(dir=shorts.ROOT) as directory:
            root = Path(directory)
            ok_job = root / "discovered-dynamic"
            ok_job.mkdir()
            (ok_job / "short.mp4").write_bytes(b"mp4")
            meta = {"topic_id": "discovered-dynamic", "target_duration_seconds": None, "visual_timeline": []}
            with patch("queue_publish.subprocess.run", return_value=Mock(stderr="  Duration: 00:00:48.00, start: 0\n Summary:\n     I: -14.0 LUFS\n")):
                qa = queue_publish.inspect_draft(ok_job / "short.mp4", meta)
            self.assertEqual(qa["warnings"], [])
            shorts.save_json(ok_job / "metadata.json", {**meta, "qa": qa})
            self.assertTrue(queue_publish.qa_gate(ok_job)["passed"])

            over_job = root / "discovered-too-long"
            over_job.mkdir()
            (over_job / "short.mp4").write_bytes(b"mp4")
            shorts.save_json(over_job / "metadata.json", {
                "qa": {"duration_seconds": 87.0, "integrated_lufs": -14.0, "warnings": []}
            })
            self.assertFalse(queue_publish.qa_gate(over_job)["passed"])

    def test_discover_batch_runs_audio_driven_without_preset_seconds(self):
        seen_seconds = []

        def fake_discover(seconds, *a, **kw):
            seen_seconds.append(seconds)
            return {
                "id": f"discovered-slot-{len(seen_seconds)}", "title": f"Topic {len(seen_seconds)}",
                "category": "Physics", "format": "story", "opening": "statement", "scene": "discovery",
                "color": "#91DFDA", "beats": ["Hook line.", "Answer line."], "source": "https://example.org/p",
                "aliases": [], "labels": ["Hook", "Answer"],
                "discovery": {"trend": "", "instagram_tags": [], "beats": [], "icon_query": "atom", "real_event": False, "event_year": ""},
            }

        with TemporaryDirectory(dir=shorts.ROOT) as directory, \
                patch.object(shorts, "new_output_batch", return_value=Path(directory)), \
                patch.object(shorts, "discover_topic", side_effect=fake_discover), \
                patch.object(shorts, "discovery_treatment", side_effect=lambda t, s: t), \
                patch.object(shorts, "render_job") as render_mock, \
                patch.object(shorts.visuals, "cache_storyboard"), \
                patch.object(shorts, "completed_jobs", return_value=[]), \
                patch.object(shorts, "load_published_history", return_value=[]), \
                patch("queue_publish.qa_gate", return_value={"passed": True, "errors": [], "warnings": []}), \
                patch("sys.argv", ["shorts.py", "discover-batch", "--count", "2", "--platform", "draft", "--delay-seconds", "0"]):
            parsed = shorts.cli()
            shorts.run_discover_batch(parsed, [], lambda: {})
        self.assertEqual(seen_seconds, [None, None])
        self.assertEqual([call.args[2] for call in render_mock.call_args_list], [None, None])


if __name__ == "__main__":
    unittest.main()
