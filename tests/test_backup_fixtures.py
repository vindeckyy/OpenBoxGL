#!/usr/bin/env python3
"""Backup fixture corpus (plan W4-05, ADR 0093).

``v1-from-1.19.1.zip`` was written by the 1.19.1 writer, so it is a real 1.19 archive.
``v2-from-2.0.zip`` is the hash-manifested default. ``compat-1.19-from-2.0.zip`` is the 2.0
export at format 1 and must keep the 1.19 layout. Each archive must restore on this build,
and the report must say which members were hash-checked.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pkg.parity import parity_backup as backup  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "backup"
V1 = "v1-from-1.19.1.zip"
V2 = "v2-from-2.0.zip"
COMPAT = "compat-1.19-from-2.0.zip"


def read_manifest(name):
    with zipfile.ZipFile(FIXTURES / name) as package:
        return json.loads(package.read("manifest.json")), sorted(package.namelist())


class BackupFixtureCorpusTests(unittest.TestCase):
    def restore(self, name, items=None):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "data"
            result = backup.restore_backup(FIXTURES / name, target, items=items, force=True)
            library = json.loads((target / "library.json").read_text(encoding="utf-8"))
            return result, library

    def test_every_fixture_restores_the_same_two_games(self):
        for name in (V1, V2, COMPAT):
            with self.subTest(archive=name):
                result, library = self.restore(name)
                self.assertEqual(sorted(result["items"]), ["library", "settings"])
                self.assertEqual([game["game_id"] for game in library["games"]], ["game-a", "game-b"])

    def test_v2_archive_hash_checks_every_member_and_the_1_19_archive_checks_none(self):
        v2_result, _ = self.restore(V2)
        v1_result, _ = self.restore(V1)
        v2_members = [row for row in v2_result["files"] if row["item"] != "other" and row["name"] != "manifest.json"]
        self.assertTrue(v2_members)
        self.assertTrue(all(row["verified"] for row in v2_members))
        self.assertFalse(any(row["verified"] for row in v1_result["files"]))

    def test_1_19_compatible_export_keeps_the_1_19_layout(self):
        v1_manifest, v1_members = read_manifest(V1)
        compat_manifest, compat_members = read_manifest(COMPAT)
        self.assertEqual(v1_members, compat_members)
        self.assertEqual(set(v1_manifest), set(compat_manifest))
        self.assertNotIn("format", compat_manifest)


if __name__ == "__main__":
    unittest.main()
