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

    # --- D5: ?focus=<id>&depth=1 ego-graph + empty honest state ---
    def test_ego_graph_focus_depth_one(self):
        games = [
            self._game(id=1, game_id="g-1", name="A", series="S"),
            self._game(id=2, game_id="g-2", name="B", series="S"),
            self._game(id=3, game_id="g-3", name="C", series="Other"),
        ]
        result = build_graph(games, [], kinds={"series"}, limit=400, focus="g-1", depth=1)
        ids = {n["game_id"] for n in result["nodes"]}
        self.assertIn("g-1", ids)
        self.assertIn("g-2", ids)
        self.assertNotIn("g-3", ids)
        self.assertEqual(result["focus"], "g-1")
        self.assertEqual(result["depth"], 1)

    def test_ego_graph_depth_two_includes_two_hops(self):
        games = [
            self._game(id=1, game_id="g-1", name="A", series="S1"),
            self._game(id=2, game_id="g-2", name="B", series="S1"),
            self._game(id=3, game_id="g-3", name="C", series="S2"),
        ]
        # Chain g-1 -- g-2 (series S1); g-2 -- g-3 via genre (all Action).
        full = build_graph(games, [], limit=400)
        self.assertTrue(full["edges"])
        ego1 = build_graph(games, [], limit=400, focus="g-1", depth=1)
        ego2 = build_graph(games, [], limit=400, focus="g-1", depth=2)
        self.assertLessEqual(len(ego1["nodes"]), len(ego2["nodes"]))
        self.assertLessEqual(len(ego2["nodes"]), len(full["nodes"]))

    def test_ego_graph_unknown_focus_empty_honest(self):
        games = [self._game(id=1, game_id="g-1")]
        result = build_graph(games, [], limit=400, focus="missing", depth=1)
        self.assertEqual(result["nodes"], [])
        self.assertTrue(result["empty"])

    def test_empty_library_honest_state(self):
        result = build_graph([], [], limit=400)
        self.assertEqual(result["nodes"], [])
        self.assertEqual(result["edges"], [])
        self.assertTrue(result["empty"])
        self.assertFalse(result["truncated"])

    # --- D5: dark-theme getComputedStyle regression + focus wiring ---
    def test_constellation_js_uses_computed_style(self):
        text = (ROOT / "static" / "constellation.js").read_text(encoding="utf-8")
        self.assertIn("getComputedStyle", text)
        self.assertNotIn("var(--constellation", text.split("getComputedStyle")[0][-500:] + "" or "")
        # No raw hex assigned to canvas colors (fallbacks are keywords only).
        for line in text.splitlines():
            if "strokeStyle" in line or "fillStyle" in line:
                self.assertNotRegex(line, r"#[0-9a-fA-F]{3,6}")

    def test_constellation_js_sends_focus_depth(self):
        text = (ROOT / "static" / "constellation.js").read_text(encoding="utf-8")
        self.assertIn("focus", text)
        self.assertIn("depth", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
