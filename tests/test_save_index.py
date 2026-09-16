#!/usr/bin/env python3
"""P2-5: RetroArch save discovery walks each tree once and caches by stem."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity import parity_saves  # noqa: E402
from saves import RETRO_INDEX_CACHE, discover_save_paths  # noqa: E402


class SaveIndexTests(unittest.TestCase):
    def setUp(self):
        RETRO_INDEX_CACHE.clear()

    def _home(self, tempdir):
        root = Path(tempdir) / ".config" / "retroarch" / "saves"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def test_scan_all_saves_finds_by_stem_and_reuses_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._home(directory)
            (root / "Super Mario 64.srm").write_text("save")
            (root / "Super Mario World.srm").write_text("save")
            (root / "state.sav").write_text("state")
            games = [
                {"game_id": "g1", "name": "Super Mario 64", "platform": "Nintendo 64", "path": "/roms/sm64.z64"},
                {"game_id": "g2", "name": "Unrelated", "platform": "PC", "path": "/roms/other"},
            ]
            with mock.patch("saves.os.walk", wraps=os.walk) as walk_spy:
                first = parity_saves.scan_all_saves(games, home=directory)
                walks_after_first = walk_spy.call_count
                second = parity_saves.scan_all_saves(games, home=directory)
                walks_after_second = walk_spy.call_count
            self.assertEqual(first, second)
            self.assertEqual(set(first), {0})
            self.assertTrue(first[0][0].endswith("Super Mario 64.srm"))
            self.assertGreater(walks_after_first, 0)
            self.assertEqual(walks_after_second, walks_after_first, "warm scan must not re-walk the tree")

    def test_discover_save_paths_uses_shared_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._home(directory)
            save = root / "Chrono Trigger.srm"
            save.write_text("save")
            game = {"game_id": "g1", "name": "Chrono Trigger", "platform": "SNES", "path": "/roms/ct.sfc"}
            with mock.patch("saves.os.walk", wraps=os.walk) as walk_spy:
                first = discover_save_paths(game, home=directory)
                walks_after_first = walk_spy.call_count
                second = discover_save_paths(game, home=directory)
            self.assertIn(str(save), [item["path"] for item in first])
            self.assertIn(str(save), [item["path"] for item in second])
            self.assertEqual(walk_spy.call_count, walks_after_first)

    def test_games_with_saves_reuses_long_lived_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._home(directory)
            (root / "Doom.srm").write_text("save")
            games = [{"game_id": "g1", "name": "Doom", "platform": "SNES", "path": "/roms/doom.sfc"}]
            with mock.patch("saves.os.walk", wraps=os.walk) as walk_spy:
                first = parity_saves.games_with_saves(games, home=directory)
                walks_after_first = walk_spy.call_count
                second = parity_saves.games_with_saves(games, home=directory)
            self.assertEqual(first, [0])
            self.assertEqual(second, [0])
            self.assertEqual(walk_spy.call_count, walks_after_first)
            self.assertGreaterEqual(parity_saves.SAVE_SCAN_TTL, 60)


if __name__ == "__main__":
    unittest.main()
