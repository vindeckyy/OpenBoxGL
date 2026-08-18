import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pkg.parity.parity_wine import list_wine_prefixes, list_proton_versions, get_prefix_for_game
from pkg.parity.parity_faugus import find_faugus_data_dirs, scan_faugus_games


class TestWine(unittest.TestCase):
    def test_list_prefixes_returns_list(self):
        prefixes = list_wine_prefixes(search_roots=[])
        self.assertIsInstance(prefixes, list)

    def test_list_protons_returns_list(self):
        protons = list_proton_versions(search_roots=[])
        self.assertIsInstance(protons, list)
        # Should at least find PATH entries if wine exists or empty otherwise
        for entry in protons:
            self.assertIn("name", entry)
            self.assertIn("path", entry)
            self.assertIn("source", entry)

    def test_get_prefix_for_game_empty(self):
        self.assertEqual(get_prefix_for_game({}), "")

    def test_get_prefix_for_game_wine_prefix(self):
        game = {"wine_prefix": "/tmp"}
        # /tmp exists but no drive_c, still returns string
        self.assertEqual(get_prefix_for_game(game), "/tmp")

    def test_get_prefix_from_launch(self):
        game = {"launch": "WINEPREFIX='/tmp/foo' wine game.exe"}
        self.assertEqual(get_prefix_for_game(game), "/tmp/foo")

    def test_get_prefix_from_env_command(self):
        game = {"launch": "env WINEPREFIX=\"/tmp/foo bar\" wine game.exe"}
        self.assertEqual(get_prefix_for_game(game), "/tmp/foo bar")


class TestFaugus(unittest.TestCase):
    def test_find_dirs_returns_list(self):
        dirs = find_faugus_data_dirs()
        self.assertIsInstance(dirs, list)

    def test_scan_empty(self):
        games = scan_faugus_games(data_dirs=["/tmp/nonexistent_faugus_test_dir"])
        self.assertEqual(games, [])

    def test_scan_with_temp_json(self):
        import tempfile
        import json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "test-game.json"
            p.write_text(json.dumps({"name": "Test Game", "game_id": "test-game", "prefix": "/tmp/prefix", "exe": "/tmp/game.exe"}))
            games = scan_faugus_games(data_dirs=[tmp])
            self.assertEqual(len(games), 1)
            self.assertEqual(games[0]["faugus_id"], "test-game")
            self.assertEqual(games[0]["source_identity"], "faugus:test-game")

    def test_scan_prefixes_only_when_drive_c_exists(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid = root / "valid-game"
            (valid / "drive_c").mkdir(parents=True)
            (root / "not-a-prefix").mkdir()
            games = scan_faugus_games(data_dirs=[tmp])
            self.assertEqual([game["faugus_id"] for game in games], ["valid-game"])
            self.assertEqual(games[0]["prefix"], str(valid))

    def test_scan_prefix_root_itself(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "single-game"
            (prefix / "drive_c").mkdir(parents=True)
            games = scan_faugus_games(data_dirs=[str(prefix)])
            self.assertEqual([game["faugus_id"] for game in games], ["single-game"])


if __name__ == "__main__":
    unittest.main()
