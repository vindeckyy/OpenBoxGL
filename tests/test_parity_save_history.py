"""Pure tests for per-game save history: listing, verify, restore, retention (F4)."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity.parity_save_history import (  # noqa: E402
    SOURCE_AUTO,
    SOURCE_MANUAL,
    SOURCE_PRE_LAUNCH,
    SOURCE_SAFETY,
    apply_retention,
    classify_label,
    history_response,
    latest_version,
    parse_archive_name,
    retention_plan,
    save_history,
    verify_version,
    version_entry,
)
from saves import backup_saves, restore_saves  # noqa: E402


class SaveHistoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.backup_root = self.root / "backups"
        self.save_dir = self.root / "saves"
        self.save_dir.mkdir()
        self.save_file = self.save_dir / "slot1.sav"
        self.save_file.write_text("one")
        self.game = {"game_id": "g-1", "name": "Real Game", "path": "/games/real", "save_paths": [str(self.save_dir)]}

    def _backup(self, label="manual"):
        archive = backup_saves(self.game, self.backup_root, label=label)
        time.sleep(0.005)
        return archive

    def test_classify_labels(self):
        self.assertEqual(classify_label(""), SOURCE_MANUAL)
        self.assertEqual(classify_label("manual"), SOURCE_MANUAL)
        self.assertEqual(classify_label("on-close"), SOURCE_AUTO)
        self.assertEqual(classify_label("auto"), SOURCE_AUTO)
        self.assertEqual(classify_label("pre-launch"), SOURCE_PRE_LAUNCH)
        self.assertEqual(classify_label("before-restore"), SOURCE_SAFETY)
        self.assertEqual(classify_label("something-else"), "other")

    def test_parse_archive_name(self):
        parsed = parse_archive_name("20260916-101112-123456-manual.zip")
        self.assertEqual(parsed["label"], "manual")
        self.assertEqual(parsed["source"], SOURCE_MANUAL)
        self.assertEqual(parsed["created_at"].isoformat(), "2026-09-16T10:11:12.123456+00:00")
        self.assertIsNone(parse_archive_name("not-an-archive.zip"))
        self.assertIsNone(parse_archive_name("20260916-101112-123456-manual.txt"))

    def test_listing_reports_age_size_and_source(self):
        self._backup("manual")
        self._backup("on-close")
        versions = save_history(self.game, self.backup_root)
        self.assertEqual([item["source"] for item in versions], [SOURCE_AUTO, SOURCE_MANUAL])
        for item in versions:
            self.assertGreater(item["size"], 0)
            self.assertGreaterEqual(item["age_seconds"], 0)
            self.assertTrue(item["created_at"])
        self.assertEqual(versions[0]["label"], "on-close")

    def test_version_entry_falls_back_for_foreign_names(self):
        directory = self.root / "backups"
        directory.mkdir()
        foreign = directory / "weird-name.zip"
        foreign.write_bytes(b"data")
        entry = version_entry(foreign)
        self.assertEqual(entry["name"], "weird-name.zip")
        self.assertEqual(entry["source"], "other")
        self.assertEqual(entry["size"], 4)

    def test_verify_reads_the_archive_back(self):
        archive = self._backup("manual")
        result = verify_version(self.game, self.backup_root, archive.name)
        self.assertTrue(result["verified"])
        self.assertGreaterEqual(result["members"], 2)
        self.assertGreater(result["bytes"], 0)
        self.assertEqual(len(result["sha256"]), 64)
        self.assertEqual(result["game"], "Real Game")

        corrupt = self._backup("manual")
        corrupt.write_bytes(b"not a zip")
        with self.assertRaises(ValueError):
            verify_version(self.game, self.backup_root, corrupt.name)

        with self.assertRaises(FileNotFoundError):
            verify_version(self.game, self.backup_root, "missing.zip")

    def test_restore_reverts_the_save(self):
        archive = self._backup("manual")
        self.save_file.write_text("two")
        restored = restore_saves(self.game, self.backup_root, archive.name)
        self.assertEqual(restored.name, archive.name)
        self.assertEqual(self.save_file.read_text(), "one")
        # The safety copy taken before restore is visible in the history.
        sources = {item["source"] for item in save_history(self.game, self.backup_root)}
        self.assertIn(SOURCE_SAFETY, sources)

    def test_retention_plan_and_apply(self):
        for _ in range(4):
            self._backup("manual")
        versions = save_history(self.game, self.backup_root)
        self.assertEqual(len(versions), 4)
        plan = retention_plan(versions, 2)
        self.assertEqual(len(plan), 2)
        self.assertEqual(plan, [item["name"] for item in versions[2:]][::-1])
        result = apply_retention(self.game, self.backup_root, 2)
        self.assertEqual(result["removed"], 2)
        self.assertEqual(result["kept"], 2)
        self.assertEqual(len(save_history(self.game, self.backup_root)), 2)
        self.assertEqual(retention_plan(versions, 0), [])

    def test_history_response_summarizes(self):
        self._backup("manual")
        self._backup("before-restore")
        payload = history_response(self.game, self.backup_root, keep=1)
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["sources"][SOURCE_SAFETY], 1)
        self.assertGreater(payload["total_bytes"], 0)
        self.assertEqual(payload["latest"]["name"], payload["versions"][0]["name"])
        self.assertEqual(len(payload["retention_plan"]), 1)


class SaveHistoryDefensiveTests(unittest.TestCase):
    def test_parse_archive_name_rejects_impossible_stamp(self):
        self.assertIsNone(parse_archive_name("20261340-999999-999999-manual.zip"))

    def test_version_entry_handles_stat_failures(self):
        class Unreadable:
            name = "weird-name.zip"
            stem = "weird-name"

            def stat(self):
                raise OSError("gone")

        entry = version_entry(Unreadable())
        self.assertEqual(entry["name"], "weird-name.zip")
        self.assertEqual(entry["size"], 0)
        self.assertEqual(entry["source"], "other")

    def test_latest_and_retention_helpers_tolerate_bad_input(self):
        self.assertIsNone(latest_version([]))
        self.assertIsNone(latest_version("junk"))
        self.assertEqual(retention_plan([{"name": "a.zip"}], "many"), [])
        self.assertEqual(retention_plan("junk", 1), [])
        self.assertEqual(retention_plan([], 1), [])

    def test_apply_retention_skips_missing_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = {"name": "G", "path": "/g", "save_paths": []}
            with patch("pkg.parity.parity_save_history.save_history", return_value=[{"name": "missing.zip"}]):
                result = apply_retention(game, root, 0)
            self.assertEqual(result, {"removed": 0, "kept": 1})

    def test_verify_skips_directory_members_and_checks_member_bound(self):
        import zipfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup_root = root / "backups"
            game = {"name": "G", "path": "/g", "save_paths": []}
            from saves import game_backup_dir

            directory_path = game_backup_dir(game, backup_root)
            directory_path.mkdir(parents=True)
            archive = directory_path / "20260916-101112-123456-manual.zip"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("manifest.json", json.dumps({"game": "G", "roots": []}))
                package.writestr("roots/", b"")
                package.writestr("roots/0/slot.sav", b"data")
            result = verify_version(game, backup_root, archive.name)
            self.assertTrue(result["verified"])
            self.assertEqual(result["members"], 2)

            with patch("pkg.parity.parity_save_history.MAX_VERIFY_MEMBER_BYTES", 1):
                with self.assertRaises(ValueError):
                    verify_version(game, backup_root, archive.name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
