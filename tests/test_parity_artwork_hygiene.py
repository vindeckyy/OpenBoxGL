#!/usr/bin/env python3
"""F5: Artwork Doctor report, selection, and undo journal tests."""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity.parity_artwork_hygiene import (  # noqa: E402
    build_report,
    image_dimensions,
    list_undo_batches,
    load_undo_manifest,
    select_fixable,
    snapshot_for_replacement,
    undo_batch,
    write_undo_manifest,
)

# 1x1 and 600x900 PNGs are generated from raw IHDR bytes in the helpers below.
def _png(width, height, seed=0):
    import struct
    import zlib

    def chunk(kind, payload):
        return (
            struct.pack(">I", len(payload)) + kind + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixel = bytes((seed % 251, (seed * 7) % 251, (seed * 13) % 251))
    raw = b"".join(b"\x00" + pixel * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


class ArtworkHygieneTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def test_image_dimensions_reads_png(self):
        path = self.root / "cover.png"
        path.write_bytes(_png(600, 900))
        self.assertEqual(image_dimensions(path), (600, 900))
        broken = self.root / "broken.png"
        broken.write_bytes(b"not an image")
        self.assertIsNone(image_dimensions(broken))

    def test_report_finds_missing_low_res_wrong_aspect_and_duplicates(self):
        good = self.root / "good.png"
        good.write_bytes(_png(600, 900))
        small = self.root / "small.png"
        small.write_bytes(_png(64, 64))
        wide = self.root / "wide.png"
        wide.write_bytes(_png(1200, 300))
        shared = self.root / "shared.png"
        shared.write_bytes(_png(600, 900, seed=5))
        shared_twin = self.root / "shared-twin.png"
        shared_twin.write_bytes(_png(600, 900, seed=5))
        games = [
            {"game_id": "g1", "name": "One", "cover": str(good)},
            {"game_id": "g2", "name": "Two", "cover": str(small)},
            {"game_id": "g3", "name": "Three", "cover": str(wide)},
            {"game_id": "g4", "name": "Four"},
            {"game_id": "g5", "name": "Five", "cover": str(shared)},
            {"game_id": "g6", "name": "Six", "cover": str(shared_twin)},
            {"game_id": "g7", "name": "Seven", "cover": str(self.root / "missing.png")},
        ]
        report = build_report(games)
        counts = report["counts"]
        self.assertEqual(counts["missing_cover"], 1)
        self.assertEqual(counts["missing_file"], 1)
        self.assertGreaterEqual(counts["low_res"], 1)
        self.assertGreaterEqual(counts["wrong_aspect"], 1)
        self.assertEqual(counts["duplicate"], 2)
        self.assertEqual(len(report["duplicates"]), 1)
        self.assertEqual(len(report["duplicates"][0]["games"]), 2)
        self.assertEqual(report["provider_attribution"], "SteamGridDB")
        self.assertEqual(report["scanned"], 7)

    def test_select_fixable_dedupes_and_filters(self):
        report = {
            "issues": [
                {"issue": "missing_cover", "game_id": "g1", "name": "One", "field": "cover"},
                {"issue": "low_res", "game_id": "g1", "name": "One", "field": "cover", "width": 10, "height": 10},
                {"issue": "wrong_aspect", "game_id": "g2", "name": "Two", "field": "background"},
                {"issue": "duplicate", "game_id": "g3", "name": "Three", "field": "cover"},
                {"issue": "missing_file", "game_id": "g4", "name": "Four", "field": "cover"},
            ]
        }
        selected = select_fixable(report, fields=["cover"])
        keys = sorted((item["game_id"], item["field"]) for item in selected)
        self.assertEqual(keys, [("g1", "cover"), ("g3", "cover")])
        first = next(item for item in selected if item["game_id"] == "g1")
        self.assertEqual(first["issues"], ["low_res", "missing_cover"])
        only_g2 = select_fixable(report, fields=["background"], game_ids=["g2"])
        self.assertEqual(len(only_g2), 1)

    def test_snapshot_and_undo_restores_replaced_files(self):
        cache = self.root / "cache"
        artwork = self.root / "art" / "cover.png"
        artwork.parent.mkdir(parents=True)
        artwork.write_bytes(_png(600, 900))
        record = snapshot_for_replacement(cache, "batch1", "game1", "cover", str(artwork))
        self.assertFalse(record["created"])
        self.assertTrue(Path(record["backup"]).is_file())
        artwork.write_bytes(_png(64, 64))
        write_undo_manifest(cache, "batch1", [record])
        restored = undo_batch(cache, "batch1")
        self.assertEqual(restored[0]["action"], "restored")
        self.assertEqual(image_dimensions(artwork), (600, 900))
        self.assertEqual(list_undo_batches(cache)[0]["count"], 1)

    def test_snapshot_and_undo_removes_new_files(self):
        cache = self.root / "cache"
        record = snapshot_for_replacement(cache, "batch2", "game2", "cover", "")
        self.assertTrue(record["created"])
        new_file = self.root / "downloaded.png"
        new_file.write_bytes(_png(600, 900))
        write_undo_manifest(cache, "batch2", [record])
        restored = undo_batch(cache, "batch2")
        self.assertEqual(restored[0]["action"], "removed")

    def test_missing_or_mismatched_manifest_is_rejected(self):
        cache = self.root / "cache"
        with self.assertRaises(ValueError):
            load_undo_manifest(cache, "nope")
        with self.assertRaises(ValueError):
            undo_batch(cache, "nope")

    def test_report_only_reads_games_and_is_json_serializable(self):
        import json

        report = build_report([{"game_id": "g1", "name": "One"}])
        json.dumps(report)
        self.assertNotIn("cover", report["issues"][0].get("path", ""))


if __name__ == "__main__":
    unittest.main()
