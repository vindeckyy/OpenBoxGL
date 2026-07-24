"""Tests for LaunchBox parity helper modules."""

import json
import tempfile
import unittest
from pathlib import Path

from parity_discovery import discovery_lists, item_rating
from parity_import import generate_m3u, group_multi_disc, recommend_emulators
from parity_media import load_media_queue, media_types_from_settings, normalize_video_fields, save_media_queue, sort_images_by_region
from parity_saves import enforce_backup_limit


class ParityFeatureTests(unittest.TestCase):
    def test_group_multi_disc(self):
        paths = [
            Path("/tmp/Final Fantasy (Disc 1).bin"),
            Path("/tmp/Final Fantasy (Disc 2).bin"),
            Path("/tmp/Single Game.iso"),
        ]
        groups = group_multi_disc(paths)
        self.assertEqual(len(groups), 2)
        self.assertEqual(len(groups[0]), 2)

    def test_generate_m3u(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            disc1 = root / "Game (Disc 1).bin"
            disc2 = root / "Game (Disc 2).bin"
            disc1.write_bytes(b"a")
            disc2.write_bytes(b"b")
            m3u = generate_m3u([disc1, disc2], root / "Game.m3u")
            self.assertTrue(m3u.is_file())
            self.assertIn("Disc 1", m3u.read_text())
            self.assertIn("Disc 2", m3u.read_text())

    def test_recommend_emulators(self):
        items = recommend_emulators("GameCube")
        self.assertTrue(any(item["name"] == "Dolphin" for item in items))

    def test_sort_images_by_region(self):
        images = [
            {"region": "Japan", "filename": "jp.png"},
            {"region": "North America", "filename": "na.png"},
        ]
        ordered = sort_images_by_region(images)
        self.assertEqual(ordered[0]["region"], "North America")

    def test_media_queue_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            queue = Path(folder) / "queue.json"
            save_media_queue(queue, [{"name": "Test"}])
            self.assertEqual(load_media_queue(queue), [{"name": "Test"}])

    def test_media_types_from_settings(self):
        types = media_types_from_settings({"auto_import_media_types": ["cover"]})
        self.assertEqual(types, {"cover"})

    def test_normalize_video_fields(self):
        game = {"video": "/tmp/theme.mp4"}
        normalize_video_fields(game)
        self.assertEqual(game["video_snap"], "/tmp/theme.mp4")

    def test_enforce_backup_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            game = {"name": "Demo", "path": "/tmp/demo.bin"}
            backup_dir = root / "backups"
            from saves import game_backup_dir
            target = game_backup_dir(game, backup_dir)
            target.mkdir(parents=True, exist_ok=True)
            for index in range(4):
                (target / f"20250101-120000-{index}.zip").write_bytes(b"x")
            removed = enforce_backup_limit(game, backup_dir, 2)
            self.assertEqual(removed, 2)
            self.assertEqual(len(list(target.glob("*.zip"))), 2)

    def test_discovery_lists(self):
        games = [
            {"name": "A", "added_at": "2026-01-02", "play_count": 0, "rating": 5},
            {"name": "B", "added_at": "2026-01-01", "play_count": 1, "rating": 2},
        ]
        lists = discovery_lists(games, limit=1)
        self.assertEqual(lists["recently_added"][0], 0)
        self.assertEqual(lists["never_played"][0], 0)
        self.assertEqual(item_rating(games[0]), 5.0)


if __name__ == "__main__":
    unittest.main()
