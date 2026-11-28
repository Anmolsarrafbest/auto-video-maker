import base64
import io
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
import wave

from PIL import Image

import rate_limiter
import render
import shorts
import storyboard
import visuals
import youtube


def flux_response(color):
    data = io.BytesIO()
    Image.new("RGB", (108, 192), color).save(data, "PNG")
    return Mock(status_code=200, json=Mock(return_value={
        "success": True, "result": {"image": base64.b64encode(data.getvalue()).decode("ascii")}
    }))


class FluxMotionTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(dir=shorts.ROOT)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.enterContext(patch.object(visuals, "CACHE", self.root / "manifest"))
        self.enterContext(patch.object(visuals, "FLUX_CACHE", self.root / "cf_3d"))
        self.credentials = self.enterContext(patch.object(visuals, "cloudflare_credentials"))
        self.credentials.return_value = ("a" * 32, "test-token")
        self.topic = {"id": "discovered-flux-fixture", "title": "Inside the coil", "category": "Physics"}
        self.beat = {"beat_id": 0, "anchor_text": "Look closely now", "visual_type": "stock_video",
                     "search_query": "coil magnetic field"}

    def test_flux_formats_request_and_caches_decoded_image(self):
        with patch.object(visuals.rate_limiter, "request_with_backoff",
                          return_value=(flux_response("red"), "cloudflare_flux")) as post:
            asset = visuals.cache_flux(self.topic, self.beat)
            self.credentials.return_value = None
            self.assertEqual(visuals.cache_flux(self.topic, self.beat, allow_request=False), asset)
            self.assertIsNone(visuals.cache_flux(self.topic, dict(self.beat, search_query="other coil")))
        post.assert_called_once()
        args, options = post.call_args
        self.assertEqual(args, ("POST", f"https://api.cloudflare.com/client/v4/accounts/{'a' * 32}/ai/run/{visuals.FLUX_MODEL}"))
        self.assertEqual(options["headers"], {"Authorization": "Bearer test-token"})
        self.assertEqual(options["json"]["steps"], 4)
        self.assertIn("Dark matte studio 3D macro render of Inside the coil", options["json"]["prompt"])
        self.assertEqual(options["max_retries"], 0)
        self.assertFalse(options["allow_redirects"])
        self.assertTrue(asset["synthetic"])
        with Image.open(visuals.local_asset(asset)["path"]) as image:
            self.assertEqual(image.size, (1080, 1920))
            self.assertGreater(image.getpixel((500, 800))[0], 240)

    def test_daily_guard_keeps_utc_accounting_if_local_day_changes(self):
        with (
            patch.object(rate_limiter, "STATE_DIR", self.root),
            patch.object(rate_limiter, "QUOTA_FILE", self.root / "quota.json"),
            patch.object(rate_limiter, "QUOTA_LOCK", self.root / "quota.lock"),
            patch.object(rate_limiter, "_today", side_effect=["2026-11-27", "2026-11-28"]),
            patch.dict(rate_limiter.PROVIDER_LIMITS["cloudflare_flux"], {"daily_cap": 1}),
        ):
            rate_limiter.reserve_slot("cloudflare_flux")
            with self.assertRaisesRegex(ValueError, "Daily quota cap"):
                rate_limiter.reserve_slot("cloudflare_flux")

    def test_resolver_limits_requests_and_prioritizes_explicit_3d(self):
        beats = [dict(self.beat, beat_id=i, search_query=f"coil design {i}") for i in range(6)]
        beats[-1]["visual_type"] = "3d_render"
        self.topic["discovery"] = {"beats": beats}
        with (
            patch.object(visuals, "secret", return_value=""),
            patch.object(visuals, "ranked_matches", return_value=[]),
            patch.object(visuals, "cache_icon", return_value=None),
            patch.object(visuals.rate_limiter, "request_with_backoff",
                         return_value=(flux_response("red"), "cloudflare_flux")) as post,
        ):
            visuals.cache_storyboard(self.topic)
        self.assertEqual(post.call_count, 4)
        manifest = visuals.read_manifest(self.topic["id"])
        self.assertEqual({item["beat_id"] for item in manifest["beats"] if item["asset"]}, {0, 1, 2, 5})

    def test_missing_flux_credentials_fall_back_to_stock_on_explicit_beat(self):
        beat = dict(self.beat, visual_type="3d_render")
        self.topic["discovery"] = {"beats": [beat]}
        self.credentials.return_value = None
        stock = {"file": "coil.mp4", "kind": "video", "source": "https://example.org/coil"}
        with (
            patch.object(visuals, "secret", return_value=""),
            patch.object(visuals, "ranked_matches", return_value=[stock]),
            patch.object(visuals, "cache", return_value=stock),
            patch.object(visuals.rate_limiter, "request_with_backoff") as post,
        ):
            visuals.cache_storyboard(self.topic)
        post.assert_not_called()
        self.assertEqual(visuals.read_manifest(self.topic["id"])["beats"][0]["asset"], stock)

    def test_stat_validation_caps_popups(self):
        narration = ["Look closely now.", "See the field moving. Watch the circuit."]
        beats = [
            dict(self.beat, beat_id=i, anchor_text=anchor, stat_callout={"value": "4%", "label": "Example"})
            for i, anchor in enumerate(("Look closely now", "See the field", "Watch the circuit"))
        ]
        clean = storyboard.validate_visual_beats(beats, narration)
        self.assertEqual(sum("stat_callout" in beat for beat in clean), 2)
        graph = render.motion_overlay_filter(
            self.root, [dict(beat, start_time=i * 2, end_time=i * 2 + 1.8) for i, beat in enumerate(clean)],
            8, "#91dfda", "null")
        self.assertEqual(graph.count("drawtext="), 4)
        self.assertIn("eval=frame", graph)
        self.assertEqual((self.root / "stat-1-value.txt").read_text(encoding="utf-8"), "4%")

    def test_ffmpeg_xfade_outputs_all_three_transitions(self):
        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        size = (64, 96)
        red = Image.new("RGB", size, "red")
        blue = Image.new("RGB", size, "blue")
        with patch.object(render, "OUTPUT_SIZE", size):
            for slot in (1, 2, 3):
                frames = render.xfade_frames(ffmpeg, red, [blue] * 8, 8, slot, self.root)
                self.assertEqual(len(frames), 8, f"Transition slot {slot}")
                self.assertGreater(frames[0].getpixel((32, 48))[0], 245)
                self.assertGreater(frames[-1].getpixel((32, 48))[2], 245)

    def test_mocked_flux_asset_reaches_real_mp4_and_upload_metadata(self):
        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        self.topic.update(format="story", opening="statement", scene="discovery", color="#91DFDA",
                          beats=["Look closely now.", "See the field moving."],
                          source="https://example.org/coil", aliases=[], labels=["Inside the coil", "Magnetic field"])
        beats = [
            self.beat,
            dict(self.beat, beat_id=1, anchor_text="See the field", visual_type="3d_render",
                 search_query="coil windings", stat_callout={"value": "40%", "label": "Example figure"}),
        ]
        self.topic["discovery"] = {"beats": beats}
        with (
            patch.object(visuals, "secret", return_value=""),
            patch.object(visuals, "ranked_matches", return_value=[]),
            patch.object(visuals, "cache_icon", return_value=None),
            patch.object(visuals.rate_limiter, "request_with_backoff",
                         side_effect=[(flux_response("blue"), ""), (flux_response("red"), "")]),
        ):
            visuals.cache_storyboard(self.topic)
        assets, _ = visuals.selected_storyboard(self.topic)

        def narration(_beats, seconds, _voice, destination, *, topic=None):
            with wave.open(str(destination), "wb") as wav:
                wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                wav.writeframes(b"\x00\x00" * (24000 * 5))
            return [(0, .8), (1, 2.1)]

        def check_motion(*args):
            paths = render_flux_motion(*args)
            self.assertTrue(paths, "FLUX Ken Burns graph returned no frames")
            return paths

        job = self.root / "job"
        render_flux_motion = render.render_flux_motion
        with (
            patch.object(shorts, "MIN_DURATION_SEC", 4),
            patch.object(shorts, "narration", side_effect=narration),
            patch("hindi_captions.recognized_words", return_value=[]),
            patch.object(render, "FPS", 30),
            patch.object(render, "render_flux_motion", side_effect=check_motion) as motion,
            patch.object(render, "xfade_frames", wraps=render.xfade_frames) as xfade,
        ):
            shorts.render_job(self.topic, "Offline FLUX fixture", None, job)
        self.assertEqual({call.args[1] for call in motion.call_args_list}, {assets[0]["path"], assets[1]["path"]})
        self.assertGreaterEqual(xfade.call_count, 1)
        for expression in (render.flux_motion_filter(7, "in"), render.flux_motion_filter(7, "out"),
                           render.flux_motion_filter(7, "drift")):
            self.assertIn("s=1080x1920", expression)
            self.assertIn("zoompan=", expression)
        meta = shorts.read_json(job / "metadata.json")
        self.assertTrue(meta["containsSyntheticMedia"])
        self.assertTrue(youtube.build_upload_body(meta, "private")["status"]["containsSyntheticMedia"])
        self.assertIn("FLUX.1 Schnell", meta["description"])
        video = job / "short.mp4"
        subprocess.run([ffmpeg, "-v", "error", "-xerror", "-i", str(video), "-f", "null", "-"],
                       capture_output=True, check=True)

        def frame(time):
            png = subprocess.check_output(
                [ffmpeg, "-v", "error", "-ss", str(time), "-i", str(video), "-frames:v", "1",
                 "-f", "image2pipe", "-vcodec", "png", "-"])
            return Image.open(io.BytesIO(png))

        opening, middle = frame(.4), frame(1.6)
        self.assertEqual(opening.size, (1080, 1920))
        self.assertGreater(opening.getpixel((500, 700))[0], 180)
        self.assertGreater(middle.getpixel((500, 700))[2], 180)
        self.assertGreater(middle.getpixel((300, 1917))[1], opening.getpixel((300, 1917))[1] + 60)
