#!/usr/bin/env python3
"""P1-16/P1-17: health endpoint robustness.

JSON null paths must not turn the whole report into a 400, repeated health
calls must not re-stat every path, and a concurrently rotated backup file must
not 500 the backup list.
"""

import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from handlers import library as library_handlers  # noqa: E402
from handlers.health import HealthHandlers  # noqa: E402
from handlers.library import LibraryHandlers  # noqa: E402


class HealthNullFieldsTests(unittest.TestCase):
    def test_null_media_fields_still_report_200(self):
        handler = object.__new__(LibraryHandlers)
        handler.send_json = mock.Mock()
        state = {
            "games": [
                {
                    "game_id": "game-null",
                    "name": "Null Fields",
                    "path": None,
                    "cover": None,
                    "platform": None,
                    "applications": None,
                    "versions": [None],
                    "documents": [{"path": None}],
                    "save_paths": None,
                },
                {
                    "game_id": "game-null-save",
                    "name": "Null Save Paths",
                    "path": None,
                    "cover": None,
                    "platform": None,
                    "save_paths": [None, ""],
                },
            ],
            "profiles": {},
        }
        with mock.patch("handlers.library.load_state", return_value=state):
            handler.health()
        handler.send_json.assert_called_once()
        status, payload = handler.send_json.call_args[0]
        self.assertEqual(status, 200)
        self.assertEqual(payload["games"], 2)
        issue_types = {issue["type"] for issue in payload["issues"]}
        self.assertIn("Missing game", issue_types)
        self.assertIn("Missing box front", issue_types)


class HealthStatCacheTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "cover.png"
        self.path.write_bytes(b"cover")
        library_handlers._HEALTH_STAT_CACHE.clear()
        self.addCleanup(library_handlers._HEALTH_STAT_CACHE.clear)

    def _counting_stat(self, calls):
        real_stat = os.stat

        def counting_stat(*args, **kwargs):
            calls["count"] += 1
            return real_stat(*args, **kwargs)

        return counting_stat

    def test_repeated_stats_hit_the_ttl_cache(self):
        calls = {"count": 0}
        with mock.patch("os.stat", self._counting_stat(calls)):
            self.assertTrue(library_handlers._health_path_exists(self.path, is_file=True))
            first = calls["count"]
            self.assertTrue(library_handlers._health_path_exists(self.path, is_file=True))
            self.assertEqual(calls["count"], first)

    def test_expired_ttl_re_stats(self):
        calls = {"count": 0}
        with mock.patch("os.stat", self._counting_stat(calls)), \
             mock.patch.object(library_handlers, "_HEALTH_STAT_TTL", 0.0):
            self.assertTrue(library_handlers._health_path_exists(self.path, is_file=True))
            first = calls["count"]
            self.assertTrue(library_handlers._health_path_exists(self.path, is_file=True))
            self.assertGreater(calls["count"], first)

    def test_empty_and_null_paths_report_absent(self):
        library_handlers._HEALTH_STAT_CACHE.clear()
        self.assertFalse(library_handlers._health_path_exists(None))
        self.assertFalse(library_handlers._health_path_exists(""))
        self.assertFalse(library_handlers._health_path_exists("\0"))

    def test_cache_is_bounded(self):
        calls = {"count": 0}
        with mock.patch("os.stat", self._counting_stat(calls)), \
             mock.patch.object(library_handlers, "_HEALTH_STAT_CACHE_MAX", 0):
            self.assertTrue(library_handlers._health_path_exists(self.path, is_file=True))
        self.assertEqual(len(library_handlers._HEALTH_STAT_CACHE), 1)


class BackupListRobustnessTests(unittest.TestCase):
    def _handler(self):
        handler = object.__new__(HealthHandlers)
        captured = {}
        handler.send_json = lambda status, payload, **kwargs: captured.update(
            {"status": status, "payload": payload}
        )
        return handler, captured

    def test_rotated_backup_is_skipped(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        folder = Path(tempdir.name) / "backups"
        folder.mkdir()
        good = folder / "OpenBoxBackup-20260101.zip"
        with zipfile.ZipFile(good, "w") as package:
            package.writestr("manifest.json", json.dumps({"created": "2026-01-01", "items": ["library"]}))
        rotating = folder / "OpenBoxBackup-20260102.zip"
        with zipfile.ZipFile(rotating, "w") as package:
            package.writestr("manifest.json", "{}")

        handler, captured = self._handler()
        real_stat = Path.stat

        def fake_stat(path_self, *args, **kwargs):
            if path_self.name == rotating.name:
                raise OSError("file rotated during listing")
            return real_stat(path_self, *args, **kwargs)

        data_path = Path(tempdir.name) / "library.json"
        with mock.patch.object(Path, "stat", fake_stat), \
             mock.patch("handlers.health.DATA", data_path):
            handler._api_get_api_backups(None)

        self.assertEqual(captured["status"], 200)
        names = [entry["name"] for entry in captured["payload"]["backups"]]
        self.assertEqual(names, [good.name])
        self.assertEqual(captured["payload"]["backups"][0]["items"], ["library"])

    def test_unreadable_folder_returns_empty_list(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        handler, captured = self._handler()
        with mock.patch.object(Path, "glob", side_effect=OSError("folder removed")), \
             mock.patch("handlers.health.DATA", Path(tempdir.name) / "library.json"):
            handler._api_get_api_backups(None)
        self.assertEqual(captured["status"], 200)
        self.assertEqual(captured["payload"]["backups"], [])

    def test_corrupt_archive_is_marked_invalid(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        folder = Path(tempdir.name) / "backups"
        folder.mkdir()
        corrupt = folder / "OpenBoxBackup-20260103.zip"
        corrupt.write_bytes(b"not a zip archive")
        handler, captured = self._handler()
        with mock.patch("handlers.health.DATA", Path(tempdir.name) / "library.json"):
            handler._api_get_api_backups(None)
        self.assertEqual(captured["status"], 200)
        self.assertEqual(len(captured["payload"]["backups"]), 1)
        self.assertTrue(captured["payload"]["backups"][0]["invalid"])

    def test_backups_sorted_newest_first(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        folder = Path(tempdir.name) / "backups"
        folder.mkdir()
        older = folder / "OpenBoxBackup-20260101.zip"
        newer = folder / "OpenBoxBackup-20260102.zip"
        for path in (older, newer):
            with zipfile.ZipFile(path, "w") as package:
                package.writestr("manifest.json", "{}")
        os.utime(older, (1_700_000_000, 1_700_000_000))
        os.utime(newer, (1_800_000_000, 1_800_000_000))
        handler, captured = self._handler()
        with mock.patch("handlers.health.DATA", Path(tempdir.name) / "library.json"):
            handler._api_get_api_backups(None)
        names = [entry["name"] for entry in captured["payload"]["backups"]]
        self.assertEqual(names, [newer.name, older.name])


if __name__ == "__main__":
    unittest.main()
