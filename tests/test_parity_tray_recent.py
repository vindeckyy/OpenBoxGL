#!/usr/bin/env python3
"""Tray recent-game publication."""
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pkg.parity  # noqa: E402,F401
from pkg.parity.parity_tray_recent import recent_games, write_recent_games  # noqa: E402


class RecentGamesTests(unittest.TestCase):
    def test_history_order_is_most_recent_first_and_unique(self):
        state = {
            "games": [{"game_id": "g1", "name": "Alpha"}, {"game_id": "g2", "name": "Beta"}],
            "history": [
                {"game_id": "g1", "game": "Alpha"},
                {"game_id": "g2", "game": "Beta"},
                {"game_id": "g1", "game": "Alpha"},
            ],
        }
        self.assertEqual(recent_games(state), [("Alpha", "g1"), ("Beta", "g2")])

    def test_last_played_fills_missing_history(self):
        state = {
            "games": [
                {"game_id": "g1", "name": "Old", "last_played": "2026-01-01T00:00:00"},
                {"game_id": "g2", "name": "New", "last_played": "2026-02-01T00:00:00"},
            ],
            "history": [],
        }
        self.assertEqual(recent_games(state), [("New", "g2"), ("Old", "g1")])

    def test_names_are_single_line(self):
        state = {"games": [], "history": [{"game_id": "g1", "game": "A\tB\nC"}]}
        self.assertEqual(recent_games(state), [("A B C", "g1")])

    def test_file_is_owner_only_and_tab_separated(self):
        with tempfile.TemporaryDirectory() as directory:
            state = {"games": [], "history": [{"game_id": "g1", "game": "Alpha"}]}
            path = write_recent_games(directory, state)
            self.assertEqual(path.read_text(encoding="utf-8"), "Alpha\tg1\n")
            if sys.platform != "win32":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_recent_limit_is_eight(self):
        state = {
            "games": [],
            "history": [{"game_id": f"g{index}", "game": f"Game {index}"} for index in range(10)],
        }
        self.assertEqual(len(recent_games(state)), 8)


if __name__ == "__main__":
    unittest.main()
