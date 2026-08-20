import sys
import os
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api_errors import GameNotFound
from handlers.wine import WineHandlers
from pkg.parity.parity_faugus import find_faugus_data_dirs, scan_faugus_games
from pkg.parity.parity_wine import get_prefix_for_game, list_proton_versions, list_wine_prefixes

def _reset_openbox_modules():
    for name in ("openbox", "webapp_state", "web_app", "handlers.faugus"):
        sys.modules.pop(name, None)



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



class DummyWineHandler(WineHandlers):
    def __init__(self, authorized=True):
        self._authorized = authorized
        self.status = None
        self.payload = None

    def authorized(self):
        return self._authorized

    def send_json(self, status, payload):
        self.status = status
        self.payload = payload

    def send_error(self, status, msg=""):
        self.status = status
        self.payload = {"error": msg}


class TestWineHandlers(unittest.TestCase):
    def test_wine_prefixes_when_wine_available(self):
        h = DummyWineHandler()
        with mock.patch("handlers.wine.HAS_WINE", True), \
             mock.patch("handlers.wine.list_wine_prefixes", return_value=["/tmp/prefix1"]):
            h._api_get_api_wine_prefixes(mock.Mock())
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload, {"prefixes": ["/tmp/prefix1"], "available": True})

    def test_wine_prefixes_when_wine_unavailable(self):
        h = DummyWineHandler()
        with mock.patch("handlers.wine.HAS_WINE", False):
            h._api_get_api_wine_prefixes(mock.Mock())
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload, {"prefixes": [], "available": False})

    def test_wine_protons_when_wine_available(self):
        h = DummyWineHandler()
        sample_protons = [{"name": "Proton 8.0", "path": "/opt/proton", "source": "steam"}]
        with mock.patch("handlers.wine.HAS_WINE", True), \
             mock.patch("handlers.wine.list_proton_versions", return_value=sample_protons):
            h._api_get_api_wine_protons(mock.Mock())
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload, {"protons": sample_protons, "available": True})

    def test_wine_protons_when_wine_unavailable(self):
        h = DummyWineHandler()
        with mock.patch("handlers.wine.HAS_WINE", False):
            h._api_get_api_wine_protons(mock.Mock())
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload, {"protons": [], "available": False})

    def test_wine_prefix_for_game_not_found(self):
        h = DummyWineHandler()
        parsed = mock.Mock(query="id=nonexistent-game")
        with mock.patch("handlers.wine.load_state_view", return_value={"games": []}):
            with self.assertRaises(GameNotFound):
                h._api_get_api_wine_prefix_for_game(parsed)

    def test_wine_prefix_for_game_when_wine_unavailable(self):
        h = DummyWineHandler()
        parsed = mock.Mock(query="id=0")
        fake_state = {"games": [{"name": "Test Game"}]}
        with mock.patch("handlers.wine.load_state_view", return_value=fake_state), \
             mock.patch("handlers.wine.HAS_WINE", False):
            h._api_get_api_wine_prefix_for_game(parsed)
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload, {"prefix": "", "available": False})

    def test_wine_prefix_for_game_when_wine_available(self):
        h = DummyWineHandler()
        parsed = mock.Mock(query="id=0")
        fake_state = {"games": [{"name": "Test Game", "wine_prefix": "/tmp/pfx"}]}
        with mock.patch("handlers.wine.load_state_view", return_value=fake_state), \
             mock.patch("handlers.wine.HAS_WINE", True), \
             mock.patch("handlers.wine.get_prefix_for_game", return_value="/tmp/pfx"):
            h._api_get_api_wine_prefix_for_game(parsed)
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload, {"prefix": "/tmp/pfx", "available": True})

    def test_wine_prefix_for_game_empty_prefix(self):
        h = DummyWineHandler()
        parsed = mock.Mock(query="id=0")
        fake_state = {"games": [{"name": "Test Game"}]}
        with mock.patch("handlers.wine.load_state_view", return_value=fake_state), \
             mock.patch("handlers.wine.HAS_WINE", True), \
             mock.patch("handlers.wine.get_prefix_for_game", return_value=""):
            h._api_get_api_wine_prefix_for_game(parsed)
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload, {"prefix": "", "available": False})

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


    def test_faugus_import_handler_deduplicates(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            prev = os.environ.get("OPENBOX_DATA_DIR")
            os.environ["OPENBOX_DATA_DIR"] = tmp
            try:
                _reset_openbox_modules()
                from openbox import save_state, load_state
                from web_app import Handler

                save_state({"games": [], "profiles": {}, "history": [], "settings": {}, "playlists": []})
                handler = object.__new__(Handler)
                responses = []
                handler.send_json = lambda status, payload: responses.append((status, payload))
                fake_scanned = [{
                    "name": "Faugus Title",
                    "faugus_id": "faugus-123",
                    "path": "/bin/true",
                    "prefix": "/tmp/pfx",
                    "source_identity": "faugus:faugus-123",
                }]
                with mock.patch("handlers.faugus.scan_faugus_games", return_value=fake_scanned):
                    handler._api_post_api_faugus_import({})
                    self.assertEqual(responses[0][0], 200)
                    self.assertEqual(responses[0][1]["added"], 1)
                    self.assertEqual(responses[0][1]["found"], 1)

                    # Second import with same candidate should deduplicate (0 added, 1 found)
                    handler._api_post_api_faugus_import({})
                    self.assertEqual(responses[1][0], 200)
                    self.assertEqual(responses[1][1]["added"], 0)
                    self.assertEqual(responses[1][1]["found"], 1)
                state = load_state()
                self.assertEqual(len(state["games"]), 1)
                self.assertEqual(state["games"][0]["name"], "Faugus Title")
            finally:
                if prev is None:
                    os.environ.pop("OPENBOX_DATA_DIR", None)
                else:
                    os.environ["OPENBOX_DATA_DIR"] = prev
                _reset_openbox_modules()

if __name__ == "__main__":
    unittest.main()
