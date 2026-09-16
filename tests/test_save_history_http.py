"""Route-level tests for save history list/verify/restore/prune (F4)."""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import handlers.data as data_handlers  # noqa: E402
from api_errors import BadRequest, GameNotFound  # noqa: E402
from handlers.data import DataHandlers  # noqa: E402
from saves import backup_saves  # noqa: E402


class Handler(DataHandlers):
    def __init__(self):
        self.responses = []

    def send_json(self, status, payload):
        self.responses.append((status, payload))


class FakeJobs:
    def __init__(self):
        self.jobs = []

    def submit(self, key, worker):
        self.jobs.append((key, worker))
        return {"job_id": f"job-{len(self.jobs)}"}


class SaveHistoryRouteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.data_path = self.root / "library.json"
        self.backup_root = self.root / "save-backups"
        self.save_dir = self.root / "saves"
        self.save_dir.mkdir()
        self.save_file = self.save_dir / "slot1.sav"
        self.save_file.write_text("one")
        self.game = {
            "id": 0,
            "game_id": "g-1",
            "name": "Real Game",
            "path": "/games/real",
            "save_paths": [str(self.save_dir)],
        }
        self.state = {"games": [self.game], "settings": {"save_backup_limit": 10}}
        self.jobs = FakeJobs()
        patches = [
            mock.patch.object(data_handlers, "DATA", self.data_path),
            mock.patch.object(data_handlers, "load_state", side_effect=lambda: self.state),
            mock.patch.object(data_handlers, "load_state_view", side_effect=lambda: self.state),
            mock.patch.object(data_handlers, "JOB_MANAGER", self.jobs),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _backup(self, label="manual"):
        archive = backup_saves(self.game, self.backup_root, label=label)
        time.sleep(0.005)
        return archive

    def test_history_route_lists_versions(self):
        self._backup("manual")
        self._backup("on-close")
        handler = Handler()
        handler._api_get_api_v2_saves_history(SimpleNamespace(query="id=0"))
        status, payload = handler.responses[0]
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 2)
        self.assertEqual([item["source"] for item in payload["versions"]], ["auto", "manual"])
        self.assertIn("retention_plan", payload)

    def test_history_route_unknown_game(self):
        handler = Handler()
        with self.assertRaises(GameNotFound):
            handler._api_get_api_v2_saves_history(SimpleNamespace(query="id=42"))

    def test_verify_route_reads_the_archive_and_rejects_tampering(self):
        archive = self._backup("manual")
        handler = Handler()
        handler._api_post_api_v2_saves_history_verify({"id": 0, "backup": archive.name})
        status, payload = handler.responses[0]
        self.assertEqual(status, 200)
        self.assertTrue(payload["verified"])
        self.assertGreaterEqual(payload["members"], 2)

        archive.write_bytes(b"not a zip")
        with self.assertRaises(BadRequest) as raised:
            Handler()._api_post_api_v2_saves_history_verify({"id": 0, "backup": archive.name})
        self.assertEqual(raised.exception.code, "SAVE_BACKUP_INVALID")

        with self.assertRaises(BadRequest):
            Handler()._api_post_api_v2_saves_history_verify({"id": 0})
        with self.assertRaises(GameNotFound):
            Handler()._api_post_api_v2_saves_history_verify({"id": 0, "backup": "missing.zip"})

    def test_restore_route_requires_a_backup_name(self):
        handler = Handler()
        with self.assertRaises(BadRequest):
            handler._api_post_api_v2_saves_history_restore({"id": 0})

    def test_prune_route_uses_the_configured_limit_by_default(self):
        for _ in range(3):
            self._backup("manual")
        self.state["settings"]["save_backup_limit"] = 1
        handler = Handler()
        handler._api_post_api_v2_saves_history_prune({"id": 0})
        self.assertEqual(handler.responses[0][1], {"removed": 2, "kept": 1})

    def test_restore_route_queues_a_job_that_restores(self):
        archive = self._backup("manual")
        self.save_file.write_text("two")
        handler = Handler()
        handler._api_post_api_v2_saves_history_restore({"id": 0, "backup": archive.name})
        status, payload = handler.responses[0]
        self.assertEqual(status, 202)
        self.assertEqual(payload["state"], "queued")
        self.assertTrue(self.jobs.jobs)
        key, worker = self.jobs.jobs[-1]
        self.assertIn("saves-restore", key)
        worker(None)
        self.assertEqual(self.save_file.read_text(), "one")

    def test_prune_route_applies_retention(self):
        for _ in range(3):
            self._backup("manual")
        handler = Handler()
        handler._api_post_api_v2_saves_history_prune({"id": 0, "keep": 1})
        status, payload = handler.responses[0]
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"removed": 2, "kept": 1})

        with self.assertRaises(BadRequest):
            Handler()._api_post_api_v2_saves_history_prune({"id": 0, "keep": -1})
        with self.assertRaises(BadRequest):
            Handler()._api_post_api_v2_saves_history_prune({"id": 0, "keep": "many"})

    def test_routes_and_manifest_include_save_history(self):
        from routes import GET_TABLE, POST_TABLE
        from routes.registry import all_routes

        self.assertEqual(GET_TABLE["/api/v2/saves/history"], "_api_get_api_v2_saves_history")
        self.assertEqual(POST_TABLE["/api/v2/saves/history/verify"], "_api_post_api_v2_saves_history_verify")
        self.assertEqual(POST_TABLE["/api/v2/saves/history/restore"], "_api_post_api_v2_saves_history_restore")
        self.assertEqual(POST_TABLE["/api/v2/saves/history/prune"], "_api_post_api_v2_saves_history_prune")
        self.assertTrue(any(route.path == "/api/v2/saves/history" for route in all_routes()))
        self.assertTrue(any(route.path == "/api/v2/saves/history/test-restore" for route in all_routes()))
        manifest = (ROOT / "runtime_modules.txt").read_text()
        self.assertIn("pkg/parity/parity_save_history.py", manifest)

    def test_test_restore_drill_verifies_without_touching_live_saves(self):
        archive = self._backup("manual")
        self.save_file.write_text("changed since backup")
        handler = Handler()
        handler._api_post_api_v2_saves_history_test_restore({"id": 0, "backup": archive.name})
        status, payload = handler.responses[0]
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertGreaterEqual(payload["files"], 1)
        self.assertEqual(payload["files"], payload["members"])
        self.assertEqual(len(payload["sha256"]), 64)
        self.assertEqual(payload["game"], "Real Game")
        # The live save was not restored or rewritten by the drill.
        self.assertEqual(self.save_file.read_text(), "changed since backup")

    def test_test_restore_rejects_bad_and_missing_archives(self):
        archive = self._backup("manual")
        archive.write_bytes(b"not a zip")
        with self.assertRaises(BadRequest) as raised:
            Handler()._api_post_api_v2_saves_history_test_restore({"id": 0, "backup": archive.name})
        self.assertEqual(raised.exception.code, "SAVE_BACKUP_INVALID")
        with self.assertRaises(BadRequest):
            Handler()._api_post_api_v2_saves_history_test_restore({"id": 0})
        with self.assertRaises(GameNotFound):
            Handler()._api_post_api_v2_saves_history_test_restore({"id": 0, "backup": "missing.zip"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
