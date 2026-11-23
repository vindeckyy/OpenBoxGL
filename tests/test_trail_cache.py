"""Derived trail.json cache invalidation, stale-while-rebuild, and self-heal tests."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

from pkg.parity import parity_trail as pt  # noqa: E402


class TrailCacheTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.ra_dir = self.data_dir / "cache" / "retroachievements"
        self.ra_dir.mkdir(parents=True)
        (self.ra_dir / "g-1.json").write_text(
            json.dumps({
                "earned": 1,
                "earned_hardcore": 1,
                "total": 2,
                "achievements": [
                    {"id": 1, "title": "First", "points": 10, "earned": True, "date_earned": "2026-10-01 12:00:00"},
                    {"id": 2, "title": "Second", "points": 5, "earned": False},
                ],
            }),
            encoding="utf-8",
        )
        self.state = {
            "games": [{"game_id": "g-1", "name": "Contra", "platform": "NES", "ra_game_id": "100"}],
            "trash": [],
        }

    def tearDown(self):
        self._tmp.cleanup()

    def test_signature_hit_corrupt_self_heal_and_stale_while_rebuild(self):
        # Also drop systems.json (a list) inside ra_dir to prove non-game caches are skipped
        (self.ra_dir / "systems.json").write_text(json.dumps([{"ID": 7, "Name": "NES"}]), encoding="utf-8")

        first = pt.get_trail(self.state, self.data_dir, "sig-1")
        self.assertFalse(first["stale"])
        self.assertEqual(first["total"], 1)

        # Signature hit serves cached trail.json without invoking compute_trail_index
        with mock.patch.object(pt, "compute_trail_index", side_effect=AssertionError("should hit cache")):
            hit = pt.get_trail(self.state, self.data_dir, "sig-1")
        self.assertEqual(hit["items"][0]["game_id"], "g-1")

        # Corrupt cache/trail.json self-heals
        cache_path = self.data_dir / "cache" / "trail.json"
        cache_path.write_text("{broken json", encoding="utf-8")
        healed = pt.get_trail(self.state, self.data_dir, "sig-1")
        self.assertEqual(healed["total"], 1)
        self.assertTrue(cache_path.is_file())

        # When file_count > INLINE_REBUILD_THRESHOLD and signature mismatches, serve stale + schedule callback
        called = []
        with mock.patch.object(pt, "INLINE_REBUILD_THRESHOLD", 0):
            stale_res = pt.get_trail(self.state, self.data_dir, "sig-2", rebuild_cb=lambda: called.append(True))
        self.assertTrue(stale_res["stale"])
        self.assertEqual(called, [True])


if __name__ == "__main__":
    unittest.main()
