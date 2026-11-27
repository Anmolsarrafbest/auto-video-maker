import os
from pathlib import Path
import struct
import time
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

import shorts

import local_voices
import rate_limiter
import render
import visuals


class SourcingTests(unittest.TestCase):
    def test_piper_synthesizes_once_and_keeps_natural_runtime(self):
        topic = {"format": "story"}
        voice = Mock()
        voice.config.sample_rate = 1000
        pcm = struct.pack("<h", 2000) * 1800
        voice.synthesize.return_value = [SimpleNamespace(audio_int16_bytes=pcm)]
        piper = SimpleNamespace(PiperVoice=Mock(), SynthesisConfig=SimpleNamespace)
        piper.PiperVoice.load.return_value = voice
        direction = {"voice": "Piper fixture", "delivery": "precise"}
        with TemporaryDirectory(dir=shorts.ROOT) as directory:
            model = Path(directory) / "voice.onnx"
            model.touch()
            wav = model.with_suffix(".wav")
            with patch.dict("sys.modules", {"piper": piper}), patch.object(render, "secret", return_value=""), \
                    patch.object(render, "treatment", return_value=direction), patch.object(render.subprocess, "run") as ffmpeg:
                timings = render.narration(["Watch the clouds closely.", "This part explains the hook. This part explains the rest."],
                                           2, model, wav, topic=topic)
            self.assertEqual(voice.synthesize.call_count, 3)
            self.assertTrue(all(call.kwargs["syn_config"].length_scale == 1.03
                                for call in voice.synthesize.call_args_list))
            self.assertAlmostEqual(timings[-1][0] - timings[-2][1], .20)
            self.assertGreater(timings[-1][1], 2)
            with wave.open(str(wav), "rb") as audio:
                self.assertGreaterEqual(audio.getnframes() / audio.getframerate(), timings[-1][1] + 2)
            ffmpeg.assert_not_called()

    def test_kokoro_defaults_to_one_and_accepts_tuning(self):
        direction = {"voice": "Kokoro fixture", "kokoro_voice": "fixture", "delivery": "curious"}
        with TemporaryDirectory(dir=shorts.ROOT) as directory:
            path = Path(directory) / "voice.wav"
            for raw, expected in (("", 1.0), ("0.98", .98)):
                with patch.object(render, "secret", return_value=raw), patch.object(render, "treatment", return_value=direction), \
                        patch.object(local_voices, "load_kokoro"), \
                        patch.object(local_voices, "kokoro_pcm", return_value=(1000, b"\x00\x04" * 400)) as pcm:
                    render.narration(["Watch this.", "There are two phases."], 10, path, path, topic={"format": "story"})
                self.assertTrue(all(call.args[3] == expected for call in pcm.call_args_list))

    def test_coverr_and_archive_parse_with_twenty_four_hour_cache(self):
        coverr = {"hits": [
            {"id": "free", "title": "Ocean waves", "urls": {"mp4": "https://storage.coverr.co/videos/free"}},
            {"id": "premium", "title": "Ocean waves", "is_premium": True,
             "urls": {"mp4": "https://storage.coverr.co/videos/premium"}},
        ]}
        archive_search = {"response": {"docs": [{"identifier": "ocean-item", "title": "Ocean waves"}]}}
        archive_item = {"metadata": {"licenseurl": "https://creativecommons.org/publicdomain/mark/1.0/"},
                        "files": [{"name": "Ocean waves.mp4", "size": "1260"}, {"name": "still.jpg", "size": "800"}]}
        results = {"https://api.coverr.co/videos": coverr, "https://archive.org/advancedsearch.php": archive_search,
                   "https://archive.org/metadata/ocean-item": archive_item}

        def request(_, url, **kwargs):
            return Mock(json=Mock(return_value=results[url])), kwargs["provider_id"]

        with TemporaryDirectory(dir=shorts.ROOT) as directory, patch.object(visuals, "CACHE", Path(directory)), \
                patch.object(visuals, "secret", return_value="test-key"), \
                patch.object(rate_limiter, "request_with_backoff", side_effect=request) as http:
            for _ in range(2):
                self.assertEqual([item["id"] for item in visuals.coverr_candidates("ocean waves")], ["free"])
                clips = list(visuals.internet_archive_candidates("ocean waves"))
                self.assertEqual(clips[0]["url"], "https://archive.org/download/ocean-item/Ocean%20waves.mp4")
            self.assertEqual(http.call_count, 3)
            self.assertEqual(http.call_args_list[0].kwargs["headers"], {"x-api-key": "test-key"})
            old = time.time() - 86401
            for cache in Path(directory).rglob("*.json"):
                os.utime(cache, (old, old))
            list(visuals.coverr_candidates("ocean waves"))
            list(visuals.internet_archive_candidates("ocean waves"))
            self.assertEqual(http.call_count, 6)
        archive_item["metadata"]["licenseurl"] = "https://example.com/publicdomain/mark/1.0/"
        with patch.object(visuals, "stock_get", side_effect=[archive_search, archive_item]):
            self.assertEqual(list(visuals.internet_archive_candidates("ocean waves")), [])

    def test_pexels_user_agent_applies_to_search_and_download(self):
        pexels = {"videos": [{"id": 1, "url": "https://www.pexels.com/video/ocean-waves/",
                              "user": {"name": "Camera operator"}, "video_files": [
                                  {"width": 1920, "height": 1080, "file_type": "video/mp4",
                                   "link": "https://videos.pexels.com/ocean.mp4"}]}]}
        with patch.object(visuals, "pexels_enabled", return_value=True), patch.object(visuals, "secret", return_value="key"), \
                patch.object(visuals, "stock_get", return_value=pexels) as search:
            record, = visuals.candidates("ocean waves", "pexels", media_type="video")
        self.assertEqual(search.call_args.kwargs["headers"]["User-Agent"], visuals.PEXELS_HEADERS["User-Agent"])
        response = Mock(iter_content=Mock(return_value=[b"fixture clip"]))
        with TemporaryDirectory(dir=shorts.ROOT) as directory, patch.object(visuals, "CACHE", Path(directory)), \
                patch.object(visuals.requests, "get") as download:
            download.return_value.__enter__.return_value = response
            visuals.cache("fixture", record)
        self.assertEqual(download.call_args.kwargs["headers"], visuals.PEXELS_HEADERS)

    def test_video_ranks_above_more_relevant_photo(self):
        photo = dict(kind="image", title="Ocean waves beach", width=1080, height=1920)
        video = dict(kind="video", title="Ocean waves", width=1920, height=1080)
        with patch.object(visuals, "candidates", return_value=[photo, video]):
            self.assertEqual(visuals.ranked_matches("ocean waves beach", ["pixabay"], strict=True), [video, photo])

    def test_broad_video_search_runs_before_photo(self):
        beat = {"beat_id": 0, "visual_type": "stock_video", "search_query": "Atlantic swell patterns",
                "fallback_video_query": "ocean waves"}
        topic = {"id": "fixture", "title": "Ocean waves", "category": "Nature", "discovery": {"beats": [beat]}}
        clip = {"kind": "video", "file": "wave.mp4", "source": "https://example.org/wave"}
        with TemporaryDirectory(dir=shorts.ROOT) as directory, patch.object(visuals, "CACHE", Path(directory)), \
                patch.object(visuals, "secret", return_value=""), patch.object(visuals, "pexels_enabled", return_value=False), \
                patch.object(visuals, "ranked_matches", side_effect=[[], [clip]]) as rank, \
                patch.object(visuals, "select_relevant_candidates", return_value={0: 0}), patch.object(visuals, "cache", return_value=clip):
            visuals.cache_storyboard(topic)
            self.assertEqual([(call.args[0], call.kwargs["media_type"]) for call in rank.call_args_list],
                             [("Atlantic swell patterns", "video"), ("ocean waves", "video")])
            self.assertEqual(visuals.read_manifest("fixture")["beats"][0]["asset"], clip)

    def test_clip_windows_do_not_overlap_and_need_relevance(self):
        video = {"kind": "video", "path": Path("waves.mp4"), "match_text": "ocean waves shoreline"}
        still = {"kind": "image", "path": Path("photo.jpg")}
        beats = [{"beat_id": index, "search_query": query}
                 for index, query in enumerate(("ocean waves", "rip currents", "ocean shoreline", "ocean waves"))]
        beats[1]["fallback_video_query"] = "ocean waves"
        planned = render.plan_video_windows({0: video, 1: still}, beats, lambda _: 12)
        self.assertEqual([(planned[i]["window_start"], planned[i]["window_end"]) for i in range(3)],
                         [(0, 4), (4, 8), (8, 12)])
        self.assertEqual(planned[1]["fallback_asset"], still)
        self.assertNotIn(3, planned)
        short = render.plan_video_windows({0: video}, beats, lambda _: 10)
        self.assertEqual(set(short), {0})

    def test_archive_providers_request_video_only(self):
        response = {"query": {"pages": []}, "collection": {"items": []}}
        with patch.object(visuals, "stock_get", return_value=response) as search:
            list(visuals.candidates("lunar surface", "wikimedia", media_type="video"))
            list(visuals.candidates("lunar surface", "nasa", media_type="video"))
        self.assertEqual(search.call_args_list[0].args[1]["gsrsearch"], "lunar surface filetype:video")
        self.assertEqual(search.call_args_list[1].kwargs["params"]["media_type"], "video")

    def test_coverr_hourly_limit_survives_midnight(self):
        with TemporaryDirectory(dir=shorts.ROOT) as directory:
            root = Path(directory)
            with patch.object(rate_limiter, "STATE_DIR", root), patch.object(rate_limiter, "QUOTA_FILE", root / "quota.json"), \
                    patch.object(rate_limiter, "QUOTA_LOCK", root / "quota.lock"), \
                    patch.object(rate_limiter.time, "time", return_value=7200), \
                    patch.object(rate_limiter, "_today", return_value="2026-01-02"):
                rate_limiter._save_state({"date": "2026-01-01", "counts": {}, "last_called": {},
                                         "coverr_hour": [7199] * 50})
                with self.assertRaisesRegex(ValueError, "Hourly quota cap"):
                    rate_limiter.reserve_slot("coverr")


if __name__ == "__main__":
    unittest.main()
