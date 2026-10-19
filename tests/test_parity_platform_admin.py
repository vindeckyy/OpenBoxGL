#!/usr/bin/env python3
"""Platform rename/delete behavior, including bounded media migration."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pkg.parity  # noqa: E402,F401
from pkg.parity import parity_platform_admin as admin  # noqa: E402


def state():
    return {
        "games": [
            {"game_id": "g1", "name": "Alpha", "platform": "NES", "cover": ""},
            {"game_id": "g2", "name": "Beta", "platform": "SNES", "cover": ""},
        ],
        "settings": {
            "platform_categories": {"NES": "Console"},
            "platform_documents": {"NES": [{"name": "Manual"}]},
            "cover_model_by_platform": {"NES": "jewel"},
            "model_back_by_platform": {"NES": "straight"},
            "model_case_by_platform": {"NES": "solid"},
            "model_rotation_lock_by_platform": {"NES": "horizontal"},
            "emulator_platform_settings": {"NES": {"retroarch_core": "fceux_libretro.so"}},
            "filter_presets": [{"name": "NES preset", "rules": {"platform": "NES"}}],
        },
        "profiles": {"NES": "fceux {path}"},
        "emulator_settings": {
            "mame": {"managed": True, "platforms": {"NES": {"video_mode": "auto"}}},
            "retroarch": {"managed": False, "platforms": {"SNES": {"run_ahead": 1}}},
        },
        "playlists": [
            {"name": "NES only", "type": "filter", "rules": {"platform": "NES"}},
            {"name": "Manual", "type": "manual", "members": ["g1"]},
        ],
        "trash": [],
    }


class BulkMediaMigrationTests(unittest.TestCase):
    def test_bulk_platform_change_moves_only_selected_games(self):
        from handlers.library import _bulk_update_with_media

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            first = media_root / "NES" / "g1" / "cover.png"
            second = media_root / "NES" / "g2" / "cover.png"
            for path in (first, second):
                path.parent.mkdir(parents=True)
                path.write_bytes(path.stem.encode())
            value = {
                "games": [
                    {"game_id": "g1", "name": "Alpha", "platform": "NES", "cover": str(first)},
                    {"game_id": "g2", "name": "Beta", "platform": "NES", "cover": str(second)},
                    {"game_id": "g3", "name": "Gamma", "platform": "SNES", "cover": ""},
                ],
            }
            with patch.object(admin, "approved_media_path", side_effect=lambda path, must_exist=False: Path(path)):
                changed, moved, errors = _bulk_update_with_media(
                    value, ["g1"], {"platform": "Famicom"}, True, media_root=media_root,
                )
            self.assertEqual(changed, 1)
            self.assertEqual(moved, 1)
            self.assertEqual(errors, [])
            self.assertEqual(value["games"][0]["platform"], "Famicom")
            self.assertEqual(value["games"][0]["cover"], str(media_root / "Famicom" / "g1" / "cover.png"))
            self.assertTrue((media_root / "Famicom" / "g1" / "cover.png").is_file())
            self.assertEqual(value["games"][1]["platform"], "NES")
            self.assertEqual(value["games"][1]["cover"], str(second))
            self.assertTrue(second.is_file())


class RenameTests(unittest.TestCase):
    def test_rename_moves_metadata_and_keeps_other_platforms(self):
        value = state()
        result = admin.rename_platform(value, "NES", "Famicom")
        self.assertEqual(result["games"], 1)
        self.assertEqual(value["games"][0]["platform"], "Famicom")
        self.assertEqual(value["games"][1]["platform"], "SNES")
        self.assertIn("Famicom", value["settings"]["platform_categories"])
        self.assertNotIn("NES", value["settings"]["platform_categories"])
        self.assertEqual(value["profiles"], {"Famicom": "fceux {path}"})
        self.assertEqual(value["settings"]["cover_model_by_platform"], {"Famicom": "jewel"})
        self.assertEqual(value["settings"]["model_back_by_platform"], {"Famicom": "straight"})
        self.assertEqual(value["settings"]["model_case_by_platform"], {"Famicom": "solid"})
        self.assertEqual(value["settings"]["model_rotation_lock_by_platform"], {"Famicom": "horizontal"})
        self.assertEqual(value["settings"]["emulator_platform_settings"], {"Famicom": {"retroarch_core": "fceux_libretro.so"}})
        self.assertEqual(value["playlists"][0]["rules"]["platform"], "Famicom")
        self.assertEqual(value["playlists"][1]["members"], ["g1"])
        self.assertEqual(value["settings"]["filter_presets"][0]["rules"]["platform"], "Famicom")
        self.assertEqual(value["emulator_settings"]["mame"]["platforms"], {"Famicom": {"video_mode": "auto"}})
        self.assertEqual(value["emulator_settings"]["retroarch"]["platforms"], {"SNES": {"run_ahead": 1}})

    def test_rename_rejects_existing_target(self):
        with self.assertRaisesRegex(ValueError, "already exists"):
            admin.rename_platform(state(), "NES", "SNES")

    def test_rename_rejects_path_separators(self):
        with self.assertRaisesRegex(ValueError, "separator"):
            admin.rename_platform(state(), "NES", "../../etc")

    def test_rename_rejects_empty_missing_and_same_name(self):
        with self.assertRaisesRegex(ValueError, "required"):
            admin.rename_platform(state(), "", "Nintendo")
        with self.assertRaisesRegex(ValueError, "not found"):
            admin.rename_platform(state(), "Missing", "Nintendo")
        self.assertEqual(admin.rename_platform(state(), "NES", "NES")["games"], 0)

    def test_default_media_root_is_created_below_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(admin, "DATA", root / "data"):
                result = admin.rename_platform(state(), "NES", "Famicom", migrate_media=True)
            self.assertEqual(result["media_moved"], 0)
            self.assertTrue((root / "media").is_dir())

    def test_move_file_handles_identical_conflicting_and_cross_device(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "one.png"
            source.write_bytes(b"same")
            dest = root / "two.png"
            dest.write_bytes(b"same")
            admin._move_file(source, dest)
            self.assertFalse(source.exists())

            source.write_bytes(b"new")
            with self.assertRaisesRegex(ValueError, "already exists"):
                admin._move_file(source, dest)
            self.assertTrue(source.exists())

            cross = root / "three.png"
            with patch.object(Path, "replace", side_effect=OSError("cross-device")):
                admin._move_file(source, cross)
            self.assertFalse(source.exists())
            self.assertEqual(cross.read_bytes(), b"new")

    def test_media_migration_skips_unapproved_and_outside_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "elsewhere" / "shot.png"
            outside.parent.mkdir(parents=True)
            outside.write_bytes(b"shot")
            value = state()
            value["games"][0]["cover"] = "bad.png"
            value["games"][0]["screenshots"] = [str(outside)]

            def approved(path, must_exist=False):
                if path == "bad.png":
                    raise OSError("gone")
                return outside

            with patch.object(admin, "approved_media_path", side_effect=approved):
                moved, skipped, touched = admin._migrate_paths(value, "NES", "Famicom", root / "media")
            self.assertEqual((moved, skipped, touched), (0, 2, []))

    def test_media_migration_rolls_back_prior_moves(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "media" / "NES"
            media.mkdir(parents=True)
            first = media / "cover.png"
            second = media / "background.png"
            first.write_bytes(b"cover")
            second.write_bytes(b"background")
            value = state()
            value["games"][0]["cover"] = str(first)
            value["games"][0]["background"] = str(second)
            original = admin._move_file
            calls = []

            def flaky(source, dest):
                calls.append(source)
                if len(calls) == 2:
                    raise OSError("disk full")
                return original(source, dest)

            with patch.object(admin, "approved_media_path", side_effect=lambda path, must_exist=False: Path(path)), patch.object(admin, "_move_file", side_effect=flaky):
                with self.assertRaises(OSError):
                    admin._migrate_paths(value, "NES", "Famicom", root / "media")
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())

    def test_media_migration_moves_only_platform_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "media" / "NES" / "cover.png"
            outside = root / "media" / "SNES" / "cover.png"
            source.parent.mkdir(parents=True)
            outside.parent.mkdir(parents=True)
            source.write_bytes(b"nes")
            outside.write_bytes(b"snes")
            value = state()
            value["games"][0]["cover"] = str(source)
            original = admin.approved_media_path
            admin.approved_media_path = lambda path, must_exist=False: Path(path)
            try:
                result = admin.rename_platform(value, "NES", "Famicom", migrate_media=True, media_root=root / "media")
            finally:
                admin.approved_media_path = original
            self.assertEqual(result["media_moved"], 1)
            self.assertFalse(source.exists())
            self.assertTrue((root / "media" / "Famicom" / "cover.png").is_file())
            self.assertTrue(outside.is_file())
            self.assertEqual(value["games"][0]["cover"], str(root / "media" / "Famicom" / "cover.png"))


class DeleteTests(unittest.TestCase):
    def test_delete_moves_games_to_trash(self):
        value = state()
        result = admin.delete_platform(value, "NES")
        self.assertEqual(result["games"], 1)
        self.assertEqual([game["game_id"] for game in value["games"]], ["g2"])
        self.assertEqual(value["trash"][0]["game_id"], "g1")
        self.assertNotIn("NES", value["settings"]["platform_categories"])
        self.assertNotIn("NES", value["profiles"])
        self.assertEqual(value["settings"]["model_back_by_platform"], {})
        self.assertEqual(value["settings"]["model_case_by_platform"], {})
        self.assertEqual(value["settings"]["model_rotation_lock_by_platform"], {})
        self.assertEqual(value["emulator_settings"]["mame"]["platforms"], {})
        self.assertEqual(value["emulator_settings"]["retroarch"]["platforms"], {"SNES": {"run_ahead": 1}})

    def test_delete_requires_a_platform(self):
        with self.assertRaisesRegex(ValueError, "not found"):
            admin.delete_platform(state(), "Missing")

    def test_delete_media_is_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "NES" / "cover.png"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"x")
            sibling = root / "SNES" / "cover.png"
            sibling.parent.mkdir(parents=True)
            sibling.write_bytes(b"y")
            result = admin.delete_platform(state(), "NES", delete_media=True, media_root=root)
            self.assertEqual(result["media_deleted"], 1)
            self.assertFalse(target.exists())
            self.assertTrue(sibling.is_file())


if __name__ == "__main__":
    unittest.main()
