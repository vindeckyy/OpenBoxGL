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


class ArtworkHygieneCoverageTests(unittest.TestCase):
    """Cover the image parsers' edge branches and the undo journal variants."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def test_parsers_reject_truncated_or_wrong_blobs(self):
        import struct

        from pkg.parity.parity_artwork_hygiene import (
            _bmp_size,
            _gif_size,
            _jpeg_size,
            _png_size,
            _webp_size,
        )

        self.assertIsNone(_png_size(b"short"))
        self.assertIsNone(_png_size(b"not a png header" + b"\x00" * 32))
        self.assertIsNone(_gif_size(b"GIF"))
        self.assertIsNone(_bmp_size(b"BM"))
        self.assertIsNone(_jpeg_size(b"nope"))
        self.assertIsNone(_jpeg_size(b"\xff\xd8\xff"))
        self.assertIsNone(_webp_size(b"RIFF----WEBP"))
        # Valid minimal headers decode.
        gif = b"GIF89a" + struct.pack("<HH", 320, 200) + b"\x00" * 4
        self.assertEqual(_gif_size(gif), (320, 200))
        bmp = b"BM" + b"\x00" * 16 + struct.pack("<ii", 100, 50)
        self.assertEqual(_bmp_size(bmp), (100, 50))
        # Minimal JPEG: SOI, APP0, then SOF0 carrying 400x300.
        jpeg = (
            b"\xff\xd8"
            + b"\xff\xe0" + struct.pack(">H", 16) + b"J" * 14
            + b"\xff\xc0" + struct.pack(">H", 8) + b"\x08"
            + struct.pack(">HH", 300, 400) + b"\x01\x11\x00"
        )
        self.assertEqual(_jpeg_size(jpeg), (400, 300))
        # WebP VP8X and VP8L variants.
        vp8x = bytearray(30)
        vp8x[0:4] = b"RIFF"
        vp8x[8:12] = b"WEBP"
        vp8x[12:16] = b"VP8X"
        vp8x[24:27] = (639).to_bytes(3, "little")
        vp8x[27:30] = (359).to_bytes(3, "little")
        self.assertEqual(_webp_size(bytes(vp8x)), (640, 360))
        vp8l = bytearray(30)
        vp8l[0:4] = b"RIFF"
        vp8l[8:12] = b"WEBP"
        vp8l[12:16] = b"VP8L"
        bits = (99 & 0x3FFF) | ((49 & 0x3FFF) << 14)
        vp8l[21:25] = bits.to_bytes(4, "little")
        self.assertEqual(_webp_size(bytes(vp8l)), (100, 50))
        vp8 = bytearray(40)
        vp8[0:4] = b"RIFF"
        vp8[8:12] = b"WEBP"
        vp8[12:16] = b"VP8 "
        self.assertIsNone(_webp_size(bytes(vp8)))  # no frame tag present

    def test_image_dimensions_jpeg_branch_and_missing_file(self):
        import struct

        from pkg.parity.parity_artwork_hygiene import _jpeg_size

        # image_dimensions hands the post-64-byte tail to the JPEG parser, so a
        # file whose tail starts with a fresh SOI exercises that branch.
        tail = (
            b"\xff\xd8"
            + b"\xff\xc0" + struct.pack(">H", 8) + b"\x08"
            + struct.pack(">HH", 10, 20) + b"\x01\x11\x00"
        )
        path = self.root / "photo.jpg"
        path.write_bytes(b"\xff\xd8" + b"\x00" * 62 + tail)
        self.assertEqual(_jpeg_size(tail), (20, 10))
        self.assertEqual(image_dimensions(path), (20, 10))
        self.assertIsNone(image_dimensions(self.root / "nope.jpg"))

    def test_file_digest_and_aspect_helpers(self):
        from pkg.parity.parity_artwork_hygiene import _aspect_issue, _file_digest

        target = self.root / "blob.bin"
        target.write_bytes(b"data")
        digest = _file_digest(target)
        self.assertEqual(len(digest), 40)
        self.assertIsNone(_file_digest(self.root / "missing.bin"))
        # Within tolerance -> no issue; unknown kind -> no issue.
        self.assertIsNone(_aspect_issue("cover", 600, 800))
        self.assertIsNone(_aspect_issue("icon", 10, 10))
        flagged = _aspect_issue("cover", 1600, 100)
        self.assertIsNotNone(flagged)

    def test_build_report_skips_nondict_games_and_remote_urls(self):
        report = build_report(
            [
                "not-a-dict",
                {"game_id": "g1", "name": "Remote", "cover": "https://example.com/c.png"},
                {"game_id": "", "name": "Empty", "cover": ""},
            ]
        )
        self.assertEqual(report["scanned"], 2)
        kinds = {issue["issue"] for issue in report["issues"]}
        self.assertEqual(kinds, {"missing_cover"})

    def test_select_fixable_skips_unfixable_and_filters(self):
        report = {
            "issues": [
                {"issue": "missing_file", "game_id": "g1", "field": "cover", "name": "A"},
                {"issue": "low_res", "game_id": "", "field": "cover", "name": "B"},
                {"issue": "low_res", "game_id": "g2", "field": "cover", "name": "C"},
                {"issue": "wrong_aspect", "game_id": "g3", "field": "background", "name": "D"},
            ]
        }
        picked = select_fixable(report)
        self.assertEqual([item["game_id"] for item in picked], ["g2"])
        picked = select_fixable(report, fields=["background"], game_ids=["g3", "g9"])
        self.assertEqual([item["game_id"] for item in picked], ["g3"])
        self.assertEqual(select_fixable(report, game_ids=["g9"]), [])

    def test_snapshot_copy_failure_marks_record_created(self):
        import shutil
        from unittest import mock

        from pkg.parity.parity_artwork_hygiene import snapshot_for_replacement

        current = self.root / "cover.png"
        current.write_bytes(b"bytes")
        with mock.patch.object(shutil, "copy2", side_effect=OSError("disk full")):
            record = snapshot_for_replacement(self.root / "cache", "b1", "g1", "cover", current)
        self.assertTrue(record["created"])
        self.assertEqual(record["backup"], "")

    def test_load_undo_manifest_rejects_bad_payloads(self):
        from pkg.parity.parity_artwork_hygiene import UNDO_DIRECTORY

        cache = self.root / "cache"
        root = cache / UNDO_DIRECTORY
        root.mkdir(parents=True)
        (root / "bad.json").write_text('{"records": "nope"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            load_undo_manifest(cache, "bad")
        (root / "mismatch.json").write_text(
            '{"batch_id": "other", "records": []}', encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            load_undo_manifest(cache, "mismatch")

    def test_undo_batch_handles_record_variants(self):
        from pkg.parity.parity_artwork_hygiene import UNDO_DIRECTORY

        cache = self.root / "cache"
        root = cache / UNDO_DIRECTORY
        root.mkdir(parents=True)
        backup = root / "backup.bin"
        backup.write_bytes(b"orig")
        previous = self.root / "cover.png"
        import json

        manifest = {
            "batch_id": "b9",
            "records": [
                "not-a-dict",
                {"game_id": "g1", "field": "cover", "previous": str(previous),
                 "backup": str(backup), "created": False},
                {"game_id": "g2", "field": "cover", "previous": "",
                 "backup": "", "created": False},
                {"game_id": "g3", "field": "cover", "previous": str(previous),
                 "backup": str(self.root / "gone.bin"), "created": False},
            ],
        }
        (root / "b9.json").write_text(json.dumps(manifest), encoding="utf-8")
        restored = undo_batch(cache, "b9")
        actions = [entry["action"] for entry in restored]
        self.assertEqual(actions, ["restored", "skipped", "skipped"])
        self.assertEqual(previous.read_bytes(), b"orig")

    def test_undo_batch_marks_failed_restores(self):
        import json
        import shutil
        from unittest import mock

        from pkg.parity.parity_artwork_hygiene import UNDO_DIRECTORY

        cache = self.root / "cache"
        root = cache / UNDO_DIRECTORY
        root.mkdir(parents=True)
        backup = root / "backup.bin"
        backup.write_bytes(b"orig")
        manifest = {
            "batch_id": "b10",
            "records": [
                {"game_id": "g1", "field": "cover",
                 "previous": str(self.root / "cover.png"),
                 "backup": str(backup), "created": False},
            ],
        }
        (root / "b10.json").write_text(json.dumps(manifest), encoding="utf-8")
        with mock.patch.object(shutil, "copy2", side_effect=OSError("ro")):
            restored = undo_batch(cache, "b10")
        self.assertEqual(restored[0]["action"], "failed")

    def test_list_undo_batches_skips_corrupt_and_respects_limit(self):
        import json
        import time

        from pkg.parity.parity_artwork_hygiene import UNDO_DIRECTORY

        cache = self.root / "cache"
        self.assertEqual(list_undo_batches(cache), [])
        root = cache / UNDO_DIRECTORY
        root.mkdir(parents=True)
        (root / "corrupt.json").write_text("{nope", encoding="utf-8")
        for batch_id in ("b1", "b2"):
            (root / f"{batch_id}.json").write_text(
                json.dumps({"batch_id": batch_id, "created_at": "", "provider": "p", "records": []}),
                encoding="utf-8",
            )
            time.sleep(0.01)
        batches = list_undo_batches(cache)
        self.assertEqual(len(batches), 2)
        limited = list_undo_batches(cache, limit=1)
        self.assertEqual(len(limited), 1)


if __name__ == "__main__":
    unittest.main()
