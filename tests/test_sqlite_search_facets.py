#!/usr/bin/env python3
"""P2-8: SQLite search and facets run at the SQL layer with JSON parity."""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity.parity_filter_presets import explorer_facets  # noqa: E402
from pkg.state.sqlite_readmodel import SqliteReadModel  # noqa: E402

GAMES = [
    {"game_id": "g1", "name": "Super Mario 64", "platform": "Nintendo 64", "genre": "Platform", "developer": "Nintendo", "publisher": "Nintendo", "progress": ""},
    {"game_id": "g2", "name": "mario kart 8", "platform": "", "genre": "Racing, Arcade", "developer": "Nintendo", "publisher": "", "progress": "Playing"},
    {"game_id": "g3", "name": "MARIO Party", "platform": "GameCube", "genre": "Party", "developer": "Hudson", "publisher": "Nintendo", "hidden": True},
    {"game_id": "g4", "name": "Pokémon Édition", "platform": "GBA", "genre": "", "developer": "", "publisher": "", "esrb": "E"},
    {"game_id": "g5", "name": "100% Orange Juice", "platform": "PC", "genre": "Board", "developer": "Orange_Juice", "publisher": "", "progress": "Playing"},
]

QUERIES = ["mario", "MARIO", "rio", "ma", "É", "é", "%", "_", "orange juice", "100", "xyz", "nintendo"]


class SqliteSearchParityTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.model = SqliteReadModel(Path(self.tempdir.name) / "readmodel.db")
        self.model._enabled = True
        self.model.rebuild({"games": GAMES})
        self.model._signature = (1, 2, 3)

    def tearDown(self):
        self.model.close()
        self.tempdir.cleanup()

    def test_search_matches_python_casefold_substring(self):
        for query in QUERIES:
            expected = [
                game["game_id"] for game in GAMES
                if query.casefold() in str(game.get("name") or "").casefold()
            ][:50]
            actual = [game["game_id"] for game in self.model.search(query)]
            self.assertEqual(actual, expected, f"query {query!r} diverged")

    def test_search_respects_limit_and_library_order(self):
        results = self.model.search("mario", limit=2)
        self.assertEqual([game["game_id"] for game in results], ["g1", "g2"])

    def test_query_parity_check(self):
        self.assertTrue(self.model.query_parity_check(GAMES))

    def test_explorer_facets_matches_json_contract(self):
        for field in ("platform", "developer", "publisher", "progress", "esrb"):
            self.assertEqual(
                self.model.explorer_facets(field),
                explorer_facets(GAMES, field),
                field,
            )

    def test_genre_facets_delegate_to_json_path(self):
        self.assertIsNone(self.model.explorer_facets("genre"))

    def test_unrebuilt_model_falls_back_to_python_scan(self):
        model = SqliteReadModel(Path(self.tempdir.name) / "unrebuilt.db")
        model._enabled = True
        model._connect()
        # Simulate an old database with rows but no folded column populated.
        conn = model._conn
        conn.execute(
            "INSERT INTO games (game_id, name, title, raw_json) VALUES (?,?,?,?)",
            ("g1", "Legacy Name", "Legacy Name", '{"game_id": "g1", "name": "Legacy Name"}'),
        )
        conn.commit()
        self.assertEqual([game["game_id"] for game in model.search("legacy")], ["g1"])
        model.close()


if __name__ == "__main__":
    unittest.main()
