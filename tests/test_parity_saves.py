#!/usr/bin/env python3
"""Save-scan provenance tests (``save_sources``) for parity_saves."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity import parity_saves  # noqa: E402


class ScanSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()

    def test_scan_records_pcsx2_memcard_source(self):
        memcards = self.home / ".config/PCSX2/memcards"
        memcards.mkdir(parents=True)
        (memcards / "game-a.ps2").write_text("card", encoding="utf-8")
        game = {"game_id": "g1", "name": "Game A", "platform": "PlayStation 2", "save_paths": []}
        found = parity_saves.scan_all_saves([game], home=self.home)
        self.assertEqual(found, {0: [str(memcards)]})
        self.assertEqual(
            game["save_sources"][str(memcards)],
            {"emulator_id": "pcsx2", "core": "", "slot": ""},
        )
        # Provenance does not rewrite the configured save_paths shape.
        self.assertEqual(game["save_paths"], [])

    def test_scan_records_steam_and_retroarch_core(self):
        steam = self.home / ".local/share/Steam/userdata/1/42/remote"
        steam.mkdir(parents=True)
        (steam / "slot.srm").write_text("steam", encoding="utf-8")
        core_dir = self.home / ".config/retroarch/saves/snes9x"
        core_dir.mkdir(parents=True)
        retro_save = core_dir / "Fixture Game.srm"
        retro_save.write_text("retro", encoding="utf-8")
        game = {"game_id": "g2", "name": "Fixture Game", "platform": "SNES", "steam_app_id": "42"}
        found = parity_saves.scan_all_saves([game], home=self.home)
        self.assertEqual(sorted(found[0]), sorted([str(steam), str(retro_save)]))
        self.assertEqual(game["save_sources"][str(steam)]["emulator_id"], "steam")
        self.assertEqual(
            game["save_sources"][str(retro_save)],
            {"emulator_id": "retroarch", "core": "snes9x", "slot": ""},
        )

    def test_scan_extra_candidates_carry_dolphin_metadata(self):
        title = self.home / ".local/share/dolphin-emu/Wii/title"
        title.mkdir(parents=True)
        game = {"game_id": "g3", "name": "Wii Game", "platform": "Wii"}
        found = parity_saves.scan_all_saves([game], home=self.home)
        self.assertEqual(found, {0: [str(title)]})
        self.assertEqual(
            game["save_sources"][str(title)],
            {"emulator_id": "dolphin", "core": "", "slot": ""},
        )

    def test_scan_skips_games_without_existing_saves(self):
        game = {"game_id": "g4", "name": "No Saves", "platform": "SNES"}
        found = parity_saves.scan_all_saves([game], home=self.home)
        self.assertEqual(found, {})
        self.assertNotIn("save_sources", game)

    def test_extra_save_candidates_keep_historical_shape(self):
        title = self.home / ".local/share/dolphin-emu/Wii/title"
        title.mkdir(parents=True)
        candidates = parity_saves.extra_save_candidates(
            {"platform": "WiiWare"}, home=self.home
        )
        self.assertEqual(len(candidates), 1)
        entry = candidates[0]
        self.assertEqual(entry["path"], str(title))
        self.assertEqual(entry["label"], "Dolphin Wii/WiiWare")
        self.assertTrue(entry["shared"])
        self.assertEqual(entry["emulator_id"], "dolphin")

    def test_scan_preserves_existing_save_paths(self):
        memcards = self.home / ".config/PCSX2/memcards"
        memcards.mkdir(parents=True)
        (memcards / "game.ps2").write_text("card", encoding="utf-8")
        custom = str(self.home / "custom.sav")
        game = {
            "game_id": "g5",
            "name": "PS2 Game",
            "platform": "PlayStation 2",
            "save_paths": [custom],
        }
        parity_saves.scan_all_saves([game], home=self.home)
        self.assertEqual(game["save_paths"], [custom])

    def test_games_with_saves_returns_indices(self):
        memcards = self.home / ".config/PCSX2/memcards"
        memcards.mkdir(parents=True)
        (memcards / "game.ps2").write_text("card", encoding="utf-8")
        games = [
            {"game_id": "g6", "name": "One", "platform": "PlayStation 2"},
            {"game_id": "g7", "name": "Two", "platform": "SNES"},
        ]
        self.assertEqual(parity_saves.games_with_saves(games, home=self.home), [0])
        self.assertIn("save_sources", games[0])
        self.assertNotIn("save_sources", games[1])


if __name__ == "__main__":
    unittest.main()
