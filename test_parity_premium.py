#!/usr/bin/env python3
"""Tests for LaunchBox Premium parity helpers."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from parity_premium import (
    custom_field_defs,
    enhanced_ra_profile,
    import_loose_arcade,
    import_xbox360_folder,
    pick_best_rom,
    rank_rom_group,
    resolve_vita_title,
    rom_quality_score,
)


class PremiumFeatureTests(unittest.TestCase):
    def test_custom_field_defs(self):
        defs = custom_field_defs({"custom_field_defs": [{"name": "Cabinet", "options": ["Upright", "Cocktail"]}]})
        self.assertEqual(defs[0]["name"], "Cabinet")
        self.assertEqual(len(defs[0]["options"]), 2)

    def test_rom_ranking_prefers_clean_usa_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bad = root / "Game (Beta).zip"
            good = root / "Game (USA).zip"
            bad.write_bytes(b"x" * 32)
            good.write_bytes(b"y" * 64)
            self.assertEqual(pick_best_rom([str(bad), str(good)]), str(good))
            self.assertEqual(rank_rom_group([str(bad), str(good)])[0], str(good))

    def test_rom_quality_score_missing_path(self):
        # A missing ROM must score worst, not raise.
        missing = Path("/nonexistent/game.zip")
        self.assertGreaterEqual(rom_quality_score(missing), 1000)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            present = root / "Game.zip"
            present.write_bytes(b"x")
            self.assertLess(rom_quality_score(present), rom_quality_score(missing))

    def test_vita_title_resolution(self):
        title = resolve_vita_title({"title": "PCSE01234", "title_id": "PCSE01234", "metadata": {"Title": "Persona 4 Golden"}})
        self.assertEqual(title, "Persona 4 Golden")

    def test_loose_arcade_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "dragon.zip").write_bytes(b"arcade")
            games = import_loose_arcade(root)
            self.assertEqual(len(games), 1)
            self.assertEqual(games[0]["platform"], "Arcade")

    def test_xbox360_default_xex(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Halo 3"
            root.mkdir()
            (root / "default.xex").write_bytes(b"xex")
            games = import_xbox360_folder(root.parent)
            self.assertEqual(games[0]["name"], "Halo 3")

    def test_enhanced_ra_profile(self):
        progress = enhanced_ra_profile({"earned": 3, "total": 10, "earned_hardcore": 1}, {"username": "test", "api_key": "key"})
        self.assertIn("commitment_label", progress)


if __name__ == "__main__":
    unittest.main()
