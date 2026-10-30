#!/usr/bin/env python3
"""Media manifest (plan W3-07, W3-08): downloads are recorded, checks run on demand, repair is explicit.

The manifest is the record of what the app wrote. A file whose size and mtime still match is
trusted without hashing; a file whose size or mtime moved is hashed, so a rewrite that keeps the
size is still caught.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pkg.state import library_v7, media_manifest  # noqa: E402
from state_store import default_state  # noqa: E402


class MediaManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.media = Path(self.tmp.name) / "media"
        (self.media / "covers").mkdir(parents=True)
        self.cover = self.media / "covers" / "alpha.png"
        self.cover.write_bytes(b"cover-one")

    def test_a_recorded_download_gets_its_size_hash_and_source(self):
        self.assertEqual(media_manifest.record(self.media, self.cover, "steam-trailer"), "covers/alpha.png")
        entry = media_manifest.load(self.media)["covers/alpha.png"]
        self.assertEqual(entry["size"], len(b"cover-one"))
        self.assertEqual(entry["source"], "steam-trailer")
        self.assertEqual(len(entry["sha256"]), 64)

    def test_a_file_outside_the_media_tree_is_not_recorded(self):
        outside = Path(self.tmp.name) / "outside.png"
        outside.write_bytes(b"x")
        self.assertIsNone(media_manifest.record(self.media, outside, "capture"))
        self.assertEqual(media_manifest.load(self.media), {})

    def test_verify_reports_missing_and_untracked_files(self):
        media_manifest.record(self.media, self.cover, "emumovies-cover")
        self.cover.unlink()
        (self.media / "covers" / "beta.png").write_bytes(b"beta")
        report = media_manifest.verify(self.media)
        self.assertEqual(report["missing"], ["covers/alpha.png"])
        self.assertEqual(report["untracked"], ["covers/beta.png"])
        self.assertEqual(report["changed"], [])

    def test_a_rewrite_with_the_same_size_is_changed_and_an_mtime_only_move_is_not(self):
        media_manifest.record(self.media, self.cover, "capture")
        later = self.cover.stat().st_mtime + 10
        self.cover.write_bytes(b"cover-two")
        os.utime(self.cover, (later, later))
        self.assertEqual(media_manifest.verify(self.media)["changed"], ["covers/alpha.png"])
        media_manifest.rebuild(self.media)
        os.utime(self.cover, (later + 5, later + 5))
        self.assertEqual(media_manifest.verify(self.media)["changed"], [])

    def test_rebuild_repairs_changed_and_untracked_entries_and_keeps_sources(self):
        media_manifest.record(self.media, self.cover, "capture")
        self.cover.write_bytes(b"cover-two")
        os.utime(self.cover, (1, 1))
        (self.media / "covers" / "beta.png").write_bytes(b"beta")
        self.assertEqual(media_manifest.rebuild(self.media), {"added": 1, "refreshed": 1, "dropped": 0})
        entries = media_manifest.load(self.media)
        self.assertEqual(entries["covers/alpha.png"]["source"], "capture")
        self.assertEqual(entries["covers/beta.png"]["source"], media_manifest.UNTRACKED_SOURCE)
        self.assertEqual(media_manifest.verify(self.media)["changed"], [])

    def test_rebuild_drops_entries_whose_file_is_gone(self):
        media_manifest.record(self.media, self.cover, "capture")
        self.cover.unlink()
        self.assertEqual(media_manifest.rebuild(self.media)["dropped"], 1)
        self.assertEqual(media_manifest.load(self.media), {})

    def test_the_integrity_report_lists_media_problems_by_category(self):
        data = Path(self.tmp.name) / "data"
        (data / "media").mkdir(parents=True)
        (data / "library.json").write_text(json.dumps(default_state()), encoding="utf-8")
        media = data / "media"
        (media / "keep.png").write_bytes(b"keep")
        media_manifest.record(media, media / "keep.png", "capture")
        (media / "stray.png").write_bytes(b"stray")
        report = library_v7.check_library(data)
        self.assertEqual(report["categories"]["media_untracked"]["count"], 1)
        self.assertNotIn("media_missing", report["categories"])


if __name__ == "__main__":
    unittest.main()
