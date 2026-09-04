#!/usr/bin/env python3
"""Tests for full library sync via mounted folder (cloud_sync 1.9.0)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pkg.parity  # noqa: F401,E402  # register flat-import finder

from cloud_sync import publish_library, pull_library, LIBRARY_SYNC_FILE  # noqa: E402


class PublishLibraryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_publish_writes_library_file(self):
        state = {"games": [{"game_id": "1", "name": "Quake", "platform": "PC"}]}
        result = publish_library(state, self.dir, device_id="dev-a")
        self.assertEqual(result["published_games"], 1)
        target = self.dir / LIBRARY_SYNC_FILE
        self.assertTrue(target.is_file())
        payload = json.loads(target.read_text())
        self.assertEqual(payload["format"], 2)
        self.assertEqual(payload["device_id"], "dev-a")
        self.assertIn("id:1", payload["games"])

    def test_publish_preserves_remote_tombstones(self):
        target = self.dir / LIBRARY_SYNC_FILE
        target.write_text(json.dumps({
            "format": 2,
            "tombstones": {"id:deleted": {"deleted_at": "2026-01-01T00:00:00", "device_id": "dev-b"}},
        }))
        state = {"games": [{"game_id": "1", "name": "Quake"}]}
        publish_library(state, self.dir, device_id="dev-a")
        payload = json.loads(target.read_text())
        self.assertIn("id:deleted", payload["tombstones"])

    def test_publish_clears_tombstone_for_readded_game(self):
        target = self.dir / LIBRARY_SYNC_FILE
        target.write_text(json.dumps({
            "format": 2,
            "tombstones": {"id:1": {"deleted_at": "2026-01-01T00:00:00", "device_id": "dev-b"}},
        }))
        state = {"games": [{"game_id": "1", "name": "Quake"}]}
        publish_library(state, self.dir, device_id="dev-a")
        payload = json.loads(target.read_text())
        self.assertNotIn("id:1", payload["tombstones"])


class PullLibraryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_remote(self, games_map, tombstones=None):
        target = self.dir / LIBRARY_SYNC_FILE
        payload = {
            "format": 2,
            "generated_at": "2026-09-01T00:00:00",
            "device_id": "dev-remote",
            "games": games_map,
            "tombstones": tombstones or {},
        }
        target.write_text(json.dumps(payload))

    def test_pull_adds_new_games(self):
        self._write_remote({
            "id:1": {"game": {"game_id": "1", "name": "Quake"}, "updated_at": "2026-09-01T00:00:00"},
        })
        state = {"games": []}
        result = pull_library(state, self.dir)
        self.assertEqual(result["added"], 1)
        self.assertEqual(len(result["games"]), 1)
        self.assertEqual(result["games"][0]["name"], "Quake")

    def test_pull_updates_newer_remote(self):
        self._write_remote({
            "id:1": {"game": {"game_id": "1", "name": "Quake Updated"}, "updated_at": "2026-09-01T12:00:00"},
        })
        state = {"games": [{"game_id": "1", "name": "Quake Old", "_sync_updated_at": "2026-01-01T00:00:00"}]}
        result = pull_library(state, self.dir)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["games"][0]["name"], "Quake Updated")

    def test_pull_skips_older_remote(self):
        self._write_remote({
            "id:1": {"game": {"game_id": "1", "name": "Quake Old"}, "updated_at": "2026-01-01T00:00:00"},
        })
        state = {"games": [{"game_id": "1", "name": "Quake New", "_sync_updated_at": "2026-09-01T00:00:00"}]}
        result = pull_library(state, self.dir)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["games"][0]["name"], "Quake New")

    def test_pull_applies_tombstones(self):
        self._write_remote(
            {},
            tombstones={"id:1": {"deleted_at": "2026-09-01T00:00:00", "device_id": "dev-remote"}},
        )
        state = {"games": [{"game_id": "1", "name": "Quake"}]}
        result = pull_library(state, self.dir)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(len(result["games"]), 0)

    def test_pull_no_remote_file_returns_unchanged(self):
        state = {"games": [{"game_id": "1", "name": "Quake"}]}
        result = pull_library(state, self.dir)
        self.assertEqual(result["added"], 0)
        self.assertEqual(len(result["games"]), 1)

    def test_pull_malformed_remote_raises(self):
        target = self.dir / LIBRARY_SYNC_FILE
        target.write_text("not json at all")
        state = {"games": []}
        from cloud_sync import CloudRemoteInvalid
        with self.assertRaises(CloudRemoteInvalid):
            pull_library(state, self.dir)


class RouteRegistrationTest(unittest.TestCase):
    def test_routes_in_post_table(self):
        from routes import POST_TABLE
        self.assertIn("/api/v2/library/sync/publish", POST_TABLE)
        self.assertIn("/api/v2/library/sync/pull", POST_TABLE)


if __name__ == "__main__":
    unittest.main()
