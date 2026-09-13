"""Route-level tests for Record That clips and reel jobs."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api_errors import BadRequest  # noqa: E402
from handlers import clips  # noqa: E402


class Handler:
    def __init__(self):
        self.responses = []

    def send_json(self, status, payload):
        self.responses.append((status, payload))


class ClipRouteTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.media = self.root / "media" / "clips" / "g1"
        self.media.mkdir(parents=True)
        self.clip_path = self.media / "clip.mp4"
        self.clip_path.write_bytes(b"clip")
        self.state = {
            "games": [{"game_id": "g1", "name": "Game One", "clips": []}],
            "settings": {"obs_replay_enabled": True},
        }
        self.approval = mock.patch.object(clips, "approved_media_path", side_effect=self._approve)
        self.approval.start()
        self.addCleanup(self.approval.stop)

    def _approve(self, path, *, must_exist=False):
        candidate = Path(path).expanduser().resolve()
        if self.root not in candidate.parents:
            raise ValueError("outside")
        if must_exist and not candidate.is_file():
            raise FileNotFoundError(str(candidate))
        return candidate

    def tearDown(self):
        self.tempdir.cleanup()

    def _transact(self, callback):
        return self.state, callback(self.state)

    def test_capture_prefers_obs_and_persists_gallery_record(self):
        handler = Handler()
        with mock.patch.object(clips, "load_state_view", return_value=self.state), mock.patch.object(
            clips, "replay_enabled", return_value=True
        ), mock.patch.object(
            clips, "save_replay_buffer", return_value={"responseData": {"savedFilename": "/tmp/replay.mp4"}}
        ), mock.patch.object(
            clips, "_copy_clip", return_value=str(self.clip_path)
        ), mock.patch.object(
            clips, "transact_state", side_effect=self._transact
        ), mock.patch.object(clips, "bump_media_epoch"):
            clips.capture_clip(handler, {"game_id": "g1", "launch_id": "launch-1"})
        status, payload = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertFalse(payload["fallback"])
        self.assertTrue(payload["obs"])
        self.assertEqual(payload["clip"]["source"], "obs_replay")
        self.assertEqual(self.state["games"][0]["video_recording"], str(self.clip_path))

    def test_capture_falls_back_to_screenshot_and_reports_failure_when_unavailable(self):
        handler = Handler()
        with mock.patch.object(clips, "load_state_view", return_value=self.state), mock.patch.object(
            clips, "replay_enabled", return_value=False
        ), mock.patch.object(
            clips, "_copy_clip", return_value=None
        ), mock.patch.object(
            clips, "_capture_screenshot_clip", return_value=str(self.clip_path)
        ), mock.patch.object(
            clips, "transact_state", side_effect=self._transact
        ), mock.patch.object(clips, "bump_media_epoch"):
            clips.capture_clip(handler, {"game_id": "g1"})
        self.assertTrue(handler.responses[-1][1]["fallback"])
        self.assertFalse(handler.responses[-1][1]["obs"])
        self.assertEqual(self.state["games"][0]["clips"][-1]["source"], "screenshot")

        with mock.patch.object(clips, "load_state_view", return_value=self.state), mock.patch.object(
            clips, "replay_enabled", return_value=False
        ), mock.patch.object(clips, "_copy_clip", return_value=None), mock.patch.object(
            clips, "_capture_screenshot_clip", return_value=None
        ):
            with self.assertRaises(BadRequest) as raised:
                clips.capture_clip(Handler(), {"game_id": "g1"})
        self.assertEqual(raised.exception.code, "CLIP_UNAVAILABLE")

    def test_list_and_reel_routes_return_bounded_public_data(self):
        self.state["games"][0]["clips"] = [{
            "path": str(self.clip_path),
            "clip_id": "clip-1",
            "created_at": "2026-09-12T00:00:00+00:00",
            "fallback": False,
        }]
        handler = Handler()
        with mock.patch.object(clips, "load_state_view", return_value=self.state):
            clips.clips_list(handler, SimpleNamespace(query="game_id=g1"))
        self.assertEqual(handler.responses[-1][1]["total"], 1)
        self.assertEqual(handler.responses[-1][1]["clips"][0]["clip_id"], "clip-1")

        with mock.patch.object(clips, "load_state_view", return_value=self.state), mock.patch.object(
            clips, "build_reel_manifest", return_value={"format": 1, "inputs": []}
        ) as manifest:
            clips.reel_manifest(handler, SimpleNamespace(query="game_id=g1"))
        manifest.assert_called_once()
        self.assertEqual(handler.responses[-1][1]["format"], 1)

    def test_create_reel_queues_a_job_and_worker_returns_result(self):
        handler = Handler()
        job = {"state": "queued", "job_id": "job-1"}
        with mock.patch.object(clips, "load_state_view", return_value=self.state), mock.patch.object(
            clips.JOB_MANAGER, "submit", return_value=job
        ) as submit, mock.patch.object(
            clips, "create_reel", return_value={"path": "/tmp/reel.html", "fallback": True}
        ) as render:
            clips.create_reel_job(handler, {"game_id": "g1", "year": 2026})
            worker = submit.call_args.args[1]
            self.assertEqual(worker()["game_id"], "g1")
        self.assertEqual(handler.responses[-1], (202, {"state": "queued", "job_id": "job-1", "game_id": "g1"}))
        render.assert_called_once()

    def test_clip_paths_outside_the_media_root_are_not_public(self):
        with mock.patch.object(clips, "approved_media_path", side_effect=ValueError("outside")):
            self.assertIsNone(clips._public_clip({"path": "/etc/passwd"}))
        self.assertIsNone(clips._public_clip({"path": ""}))
        self.assertEqual(clips._saved_filename({"nested": {"savedFilename": "/tmp/a.mp4"}}), "/tmp/a.mp4")

    def test_routes_and_runtime_manifest_include_record_that(self):
        from routes import GET_TABLE, POST_TABLE, PUBLIC_GET_PATHS
        from routes.registry import all_routes

        self.assertEqual(GET_TABLE["/api/v2/clips"], "handlers.clips.clips_list")
        self.assertEqual(POST_TABLE["/api/v2/clips/capture"], "handlers.clips.capture_clip")
        self.assertIn("/static/clips.js", PUBLIC_GET_PATHS)
        self.assertTrue(any(route.path == "/api/v2/reels/create" for route in all_routes()))
        self.assertIn("handlers/clips.py", (ROOT / "runtime_modules.txt").read_text())


if __name__ == "__main__":
    unittest.main()
