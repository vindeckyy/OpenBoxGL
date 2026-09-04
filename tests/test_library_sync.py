#!/usr/bin/env python3
"""Tests for full library sync via mounted folder (cloud_sync 1.9.0)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pkg.parity  # noqa: F401,E402  # register flat-import finder

from cloud_sync import publish_library, pull_library, LIBRARY_SYNC_FILE  # noqa: E402
from cloud_sync import CloudSyncError  # noqa: E402


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
            "tombstones": {"id:deleted": {"deleted_at": "2026-09-01T00:00:00", "device_id": "dev-b"}},
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
        # Single-library wipe is 100% of local games, so the ADR 0038
        # mass-delete gate requires explicit confirm.
        result = pull_library(state, self.dir, confirm=True)
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


class HandlerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["OPENBOX_DATA_DIR"] = str(self._tmp.name)
        import openbox
        openbox.STATE_STORE.save({"games": [{"game_id": "1", "name": "Quake"}], "settings": {"cloud_folder": str(self._tmp.name)}, "profiles": {}})

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop("OPENBOX_DATA_DIR", None)

    def test_publish_handler(self):
        from handlers.health import HealthHandlers

        class MockHandler(HealthHandlers):
            def __init__(self):
                self._sent = None
            def send_json(self, code, body):
                self._sent = (code, body)

        handler = MockHandler()
        handler._api_post_api_v2_library_sync_publish({"device_id": "test-dev"})
        self.assertEqual(handler._sent[0], 200)
        self.assertIn("published_games", handler._sent[1])

    def test_pull_handler(self):
        from handlers.health import HealthHandlers

        class MockHandler(HealthHandlers):
            def __init__(self):
                self._sent = None
            def send_json(self, code, body):
                self._sent = (code, body)

        handler = MockHandler()
        handler._api_post_api_v2_library_sync_pull({"device_id": "test-dev"})
        self.assertEqual(handler._sent[0], 200)
        self.assertIn("synced_at", handler._sent[1])

    def test_publish_handler_no_folder(self):
        import openbox
        openbox.STATE_STORE.save({"games": [], "settings": {}, "profiles": {}})
        from handlers.health import HealthHandlers

        class MockHandler(HealthHandlers):
            def __init__(self):
                self._sent = None
            def send_json(self, code, body):
                self._sent = (code, body)

        handler = MockHandler()
        handler._api_post_api_v2_library_sync_publish({})
        self.assertEqual(handler._sent[0], 400)

    def test_pull_handler_no_folder(self):
        import openbox
        openbox.STATE_STORE.save({"games": [], "settings": {}, "profiles": {}})
        from handlers.health import HealthHandlers

        class MockHandler(HealthHandlers):
            def __init__(self):
                self._sent = None
            def send_json(self, code, body):
                self._sent = (code, body)

        handler = MockHandler()
        handler._api_post_api_v2_library_sync_pull({})
        self.assertEqual(handler._sent[0], 400)


class SyncV2ConflictTest(unittest.TestCase):
    """Library sync v2 (ADR 0038): conflicts[] reporting, LWW unchanged."""

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

    def test_concurrent_edit_remote_wins_reports_conflict(self):
        self._write_remote({
            "id:1": {"game": {"game_id": "1", "name": "Remote"}, "updated_at": "2026-09-01T13:00:00"},
        })
        state = {"games": [{"game_id": "1", "name": "Local", "_sync_updated_at": "2026-09-01T12:00:00"}]}
        result = pull_library(state, self.dir)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["games"][0]["name"], "Remote")
        conflicts = result["conflicts"]
        self.assertEqual(len(conflicts), 1)
        entry = conflicts[0]
        self.assertEqual(entry["game_key"], "id:1")
        self.assertEqual(entry["local_updated_at"], "2026-09-01T12:00:00")
        self.assertEqual(entry["remote_updated_at"], "2026-09-01T13:00:00")
        self.assertEqual(entry["winner"], "remote")
        self.assertIn("name", entry["fields_differ"])

    def test_concurrent_edit_local_wins_reports_conflict(self):
        self._write_remote({
            "id:1": {"game": {"game_id": "1", "name": "Remote"}, "updated_at": "2026-09-01T12:00:00"},
        })
        state = {"games": [{"game_id": "1", "name": "Local", "_sync_updated_at": "2026-09-01T13:00:00"}]}
        result = pull_library(state, self.dir)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["games"][0]["name"], "Local")
        conflicts = result["conflicts"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["winner"], "local")
        self.assertIn("name", conflicts[0]["fields_differ"])


class SyncV2GuardTest(unittest.TestCase):
    """Library sync v2 (ADR 0038): contention, mass-delete gate, GC, shelf rows."""

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

    def test_flock_contention_readable_error(self):
        import fcntl as _fcntl
        from unittest import mock
        state = {"games": []}
        with mock.patch.object(_fcntl, "flock", side_effect=BlockingIOError(11, "Resource temporarily unavailable")):
            with self.assertRaises(CloudSyncError) as ctx:
                publish_library(state, self.dir, device_id="dev-a")
        self.assertIn("busy", str(ctx.exception).lower())

    def test_pull_mass_delete_needs_confirm_no_mutation(self):
        tombstones = {
            f"id:{i}": {"deleted_at": "2026-09-01T00:00:00", "device_id": "dev-remote"}
            for i in range(2)
        }
        self._write_remote({}, tombstones=tombstones)
        games = [{"game_id": str(i), "name": f"Game {i}"} for i in range(10)]
        state = {"games": list(games)}
        result = pull_library(state, self.dir)
        self.assertTrue(result["needs_confirm"])
        self.assertEqual(result["deleted"], 2)
        self.assertEqual(len(result["games"]), 10)
        self.assertEqual(len(state["games"]), 10)

    def test_pull_mass_delete_with_confirm_applies(self):
        tombstones = {
            f"id:{i}": {"deleted_at": "2026-09-01T00:00:00", "device_id": "dev-remote"}
            for i in range(2)
        }
        self._write_remote({}, tombstones=tombstones)
        state = {"games": [{"game_id": str(i), "name": f"Game {i}"} for i in range(10)]}
        result = pull_library(state, self.dir, confirm=True)
        self.assertFalse(result["needs_confirm"])
        self.assertEqual(result["deleted"], 2)
        self.assertEqual(len(result["games"]), 8)

    def test_pull_small_delete_applies_without_confirm(self):
        tombstones = {"id:0": {"deleted_at": "2026-09-01T00:00:00", "device_id": "dev-remote"}}
        self._write_remote({}, tombstones=tombstones)
        state = {"games": [{"game_id": str(i), "name": f"Game {i}"} for i in range(20)]}
        result = pull_library(state, self.dir)
        self.assertFalse(result["needs_confirm"])
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(len(result["games"]), 19)

    def test_tombstone_gc_90_days_reported(self):
        target = self.dir / LIBRARY_SYNC_FILE
        target.write_text(json.dumps({
            "format": 2,
            "tombstones": {
                "id:old": {"deleted_at": "2026-01-01T00:00:00", "device_id": "dev-b"},
                "id:new": {"deleted_at": "2026-09-01T00:00:00", "device_id": "dev-b"},
            },
        }))
        state = {"games": []}
        result = publish_library(state, self.dir, device_id="dev-a", now="2026-09-04T00:00:00")
        payload = json.loads(target.read_text())
        self.assertNotIn("id:old", payload["tombstones"])
        self.assertIn("id:new", payload["tombstones"])
        self.assertEqual(result["tombstones_gc"], 1)

    def test_gc_tombstones_skips_invalid_records(self):
        from cloud_sync import _gc_tombstones, _timestamp
        kept, gc_count = _gc_tombstones(
            {
                "id:ok": {"deleted_at": "2026-09-01T00:00:00", "device_id": "dev-b"},
                "id:bad": "junk",
                "id:empty": {},
            },
            _timestamp("2026-09-04T00:00:00"),
        )
        self.assertEqual(set(kept), {"id:ok"})
        self.assertEqual(gc_count, 0)

    def test_manual_entry_merge_path_usable(self):
        self._write_remote({
            "id:m1": {"game": {"game_id": "m1", "name": "Chess", "manual_entry": True, "path": ""},
                      "updated_at": "2026-09-01T00:00:00"},
        })
        state = {"games": []}
        result = pull_library(state, self.dir)
        self.assertEqual(result["added"], 1)
        merged = result["games"][0]
        self.assertTrue(merged.get("manual_entry"))
        self.assertTrue(merged.get("path_usable"))

    def test_publish_manifest_media_synced_false(self):
        state = {"games": [{"game_id": "1", "name": "Quake"}]}
        publish_library(state, self.dir, device_id="dev-a")
        payload = json.loads((self.dir / LIBRARY_SYNC_FILE).read_text())
        self.assertIs(payload["media_synced"], False)


class SyncV2HandlerTest(unittest.TestCase):
    """Handler-level: mass-delete confirm gate returns SYNC_NEEDS_CONFIRM."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["OPENBOX_DATA_DIR"] = str(self._tmp.name)
        import openbox
        openbox.STATE_STORE.save({"games": [{"game_id": "1", "name": "Quake"}], "settings": {"cloud_folder": str(self._tmp.name)}, "profiles": {}})
        from cloud_sync import game_key as _game_key
        stored = openbox.load_state()["games"][0]
        tkey = _game_key(stored)
        target = Path(str(self._tmp.name)) / LIBRARY_SYNC_FILE
        target.write_text(json.dumps({
            "format": 2,
            "generated_at": "2026-09-01T00:00:00",
            "device_id": "dev-remote",
            "games": {},
            "tombstones": {tkey: {"deleted_at": "2026-09-01T00:00:00", "device_id": "dev-remote"}},
        }))

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop("OPENBOX_DATA_DIR", None)

    def _handler(self):
        from handlers.health import HealthHandlers

        class MockHandler(HealthHandlers):
            def __init__(self):
                self._sent = None
            def send_json(self, code, body):
                self._sent = (code, body)

        return MockHandler()

    def test_pull_handler_needs_confirm_409(self):
        import openbox
        handler = self._handler()
        handler._api_post_api_v2_library_sync_pull({"device_id": "test-dev"})
        code, body = handler._sent
        self.assertEqual(code, 409)
        self.assertTrue(body["needs_confirm"])
        self.assertEqual(body["code"], "SYNC_NEEDS_CONFIRM")
        current = openbox.load_state()
        self.assertEqual(len(current["games"]), 1)

    def test_pull_handler_confirm_applies(self):
        import openbox
        handler = self._handler()
        handler._api_post_api_v2_library_sync_pull({"device_id": "test-dev", "confirm": True})
        code, body = handler._sent
        self.assertEqual(code, 200)
        self.assertEqual(body["deleted"], 1)
        current = openbox.load_state()
        self.assertEqual(len(current["games"]), 0)


if __name__ == "__main__":
    unittest.main()
