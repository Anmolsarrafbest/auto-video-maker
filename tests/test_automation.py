from datetime import datetime, timedelta, timezone
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


if __name__ == "__main__":
    unittest.main()
