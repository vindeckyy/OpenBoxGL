"""Tests for pkg.parity.parity_constellation."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity.parity_constellation import build_graph  # noqa: E402


class ParityConstellationTests(unittest.TestCase):
    def _game(self, **kwargs):
        defaults = {
            "id": 1,
            "game_id": "g-1",
            "name": "Game",
            "platform": "SNES",
            "series": "",
            "developer": "",
            "publisher": "",
            "genre": "Action",
            "playtime_seconds": 0,
            "play_count": 0,
            "favorite": False,
            "cover": "",
            "has_cover": False,
            "year": 1996,
        }
        defaults.update(kwargs)
        return defaults

    def test_empty_library(self):
        result = build_graph([], [], limit=400)
        self.assertEqual(result["nodes"], [])
        self.assertEqual(result["edges"], [])
        self.assertEqual(result["kinds"], ["co_played", "developer", "genre", "platform_family", "publisher", "series"])

    def test_series_edge(self):
        games = [
            self._game(id=1, game_id="g-1", name="Mario 1", series="Mario"),
            self._game(id=2, game_id="g-2", name="Mario 2", series="Mario"),
            self._game(id=3, game_id="g-3", name="Other", series="Other"),
        ]
        result = build_graph(games, [], kinds={"series"}, limit=400)
        self.assertEqual(len(result["edges"]), 1)
        self.assertEqual(result["edges"][0]["kind"], "series")

    def test_developer_beats_genre(self):
        games = [
            self._game(id=1, game_id="g-1", name="A", developer="id", genre="Platform"),
            self._game(id=2, game_id="g-2", name="B", developer="id", genre="RPG"),
        ]
        result = build_graph(games, [], limit=400)
        self.assertEqual(result["edges"][0]["kind"], "developer")

    def test_platform_family_edge(self):
        games = [
            self._game(id=1, game_id="g-1", name="A", platform="SNES"),
            self._game(id=2, game_id="g-2", name="B", platform="NES"),
        ]
        result = build_graph(games, [], kinds={"platform_family"}, limit=400)
        self.assertEqual(len(result["edges"]), 1)

    def test_co_play_window(self):
        games = [
            self._game(id=1, game_id="g-1"),
            self._game(id=2, game_id="g-2"),
            self._game(id=3, game_id="g-3"),
        ]
        history = [
            {"game_id": "g-1", "started": "2026-01-01T12:00:00Z"},
            {"game_id": "g-2", "started": "2026-01-02T12:00:00Z"},  # within 7 days
            {"game_id": "g-3", "started": "2026-01-09T12:00:00Z"},  # 8 days out
        ]
        result = build_graph(games, history, kinds={"co_played"}, limit=400)
        pairs = {(e["s"], e["t"]) for e in result["edges"]}
        self.assertIn((0, 1), pairs)
        self.assertNotIn((0, 2), pairs)

    def test_limit_truncated(self):
        games = [self._game(id=i, game_id=f"g-{i}", playtime_seconds=i * 60) for i in range(1, 10)]
        result = build_graph(games, [], limit=5)
        self.assertEqual(len(result["nodes"]), 5)
        self.assertTrue(result["truncated"])

    def test_unknown_kinds_ignored(self):
        games = [self._game(id=1, game_id="g-1"), self._game(id=2, game_id="g-2", genre="Action")]
        result = build_graph(games, [], kinds={"bad_kind", "genre"}, limit=400)
        self.assertEqual(result["kinds"], ["genre"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
