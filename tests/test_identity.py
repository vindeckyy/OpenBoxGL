import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pkg.parity.parity_identity import (
    normalize_identity,
    normalize_path_identity,
    normalize_rom_name,
    detect_duplicate_identities,
    backfill_source_identity,
    source_family,
    source_identities,
)
from state_store import _stable_game_id

class TestIdentity(unittest.TestCase):
    def test_native_storefront_ids_are_identities(self):
        self.assertEqual(normalize_identity({"gog_id": "123"}), "gog:123")
        self.assertEqual(normalize_identity({"ea_id": "456"}), "ea:456")
        self.assertEqual(normalize_identity({"ubisoft_id": "789"}), "ubisoft:789")
        self.assertIn("gog:123", source_identities({"gog_id": "123"}))
        self.assertIn("ea:456", source_identities({"ea_id": "456"}))
        self.assertIn("ubisoft:789", source_identities({"ubisoft_id": "789"}))
        self.assertEqual(source_family({"gog_id": "123"}), "gog")
        self.assertEqual(source_family({"ea_id": "456"}), "ea")
        self.assertEqual(source_family({"ubisoft_id": "789"}), "ubisoft")

    def test_empty_native_ids_do_not_change_stable_id(self):
        plain = {"path": "/games/Game.exe", "platform": "PC"}
        self.assertEqual(_stable_game_id(plain), _stable_game_id({**plain, "gog_id": "", "ea_id": "", "ubisoft_id": ""}))

    def test_ea_games_with_same_path_get_distinct_ids(self):
        base = {"path": "C:/Windows/explorer.exe", "platform": "PC"}
        self.assertNotEqual(_stable_game_id({**base, "ea_id": "111"}), _stable_game_id({**base, "ea_id": "222"}))

    def test_normalize_steam(self):
        self.assertEqual(normalize_identity({"steam_app_id": 730}), "steam:730")

    def test_normalize_heroic(self):
        self.assertEqual(normalize_identity({"heroic_app_id": "abc123", "source": "Epic"}), "heroic:Epic:abc123")

    def test_normalize_lutris(self):
        self.assertEqual(normalize_identity({"lutris_id": "my-game"}), "lutris:my-game")

    def test_normalize_gameyfin(self):
        self.assertEqual(normalize_identity({"gameyfin_id": "xyz"}), "gameyfin:xyz")

    def test_normalize_durable_source_identity(self):
        self.assertEqual(
            normalize_identity({"source_identity": "faugus:demo", "path": ""}),
            "faugus:demo",
        )

    def test_normalize_faugus(self):
        self.assertEqual(normalize_identity({"faugus_id": "demo"}), "faugus:demo")

    def test_normalize_generic_file(self):
        path = os.path.expanduser("~/games/doom.exe")
        ident = normalize_identity({"path": path})
        self.assertTrue(ident.startswith("path:"))
        self.assertTrue("doom.exe" in ident)

    def test_normalize_rom_name(self):
        self.assertEqual(normalize_rom_name("Super Mario World (USA) (Rev 1).sfc"), "super mario world")
        self.assertEqual(normalize_rom_name("Sonic the Hedgehog [!].md"), "sonic the hedgehog.md")
        self.assertEqual(normalize_rom_name("Game.zip"), "game")

    def test_normalize_arcade_identity(self):
        self.assertEqual(normalize_identity({"rom_name": "Game.zip", "source": "mame"}), "mame:game")
        self.assertEqual(normalize_identity({"rom_name": "Game.zip"}), "arcade:game")

    def test_path_normalization(self):
        p1 = normalize_path_identity("/a/b/../c/game.exe")
        p2 = normalize_path_identity("/a/c/game.exe")
        self.assertEqual(p1, p2)
        
        # Test case insensitivity on platforms where os.path.normcase changes case
        if os.path.normcase("A") == "a":
            p3 = normalize_path_identity("/A/C/Game.exe")
            self.assertEqual(p1, p3)

    def test_duplicate_detection(self):
        games = [
            {"id": "1", "steam_app_id": 730},
            {"id": "2", "steam_app_id": 730},
            {"id": "3", "lutris_id": "foo"}
        ]
        dupes = detect_duplicate_identities(games)
        self.assertEqual(len(dupes), 1)
        self.assertEqual(dupes[0]["identity"], "steam:730")
        self.assertEqual(dupes[0]["games"], ["1", "2"])

    def test_duplicate_detection_prefers_stable_game_id(self):
        games = [
            {"game_id": "stable-1", "source_identity": "faugus:demo"},
            {"game_id": "stable-2", "source_identity": "faugus:demo"},
        ]
        dupes = detect_duplicate_identities(games)
        self.assertEqual(dupes[0]["games"], ["stable-1", "stable-2"])

    def test_backfill(self):
        games = [
            {"id": "1", "steam_app_id": 730},
            {"id": "2", "lutris_id": "foo", "source_identity": "lutris:foo"}
        ]
        count = backfill_source_identity(games)
        self.assertEqual(count, 1)
        self.assertEqual(games[0]["source_identity"], "steam:730")
        self.assertEqual(games[1]["source_identity"], "lutris:foo")

    def test_storefront_collision(self):
        games = [
            {"id": "1", "steam_app_id": "123"},
            {"id": "2", "heroic_app_id": "123", "source": "Epic"}
        ]
        dupes = detect_duplicate_identities(games)
        self.assertEqual(len(dupes), 0)

    def test_none_for_invalid(self):
        self.assertIsNone(normalize_identity({}))
        self.assertIsNone(normalize_identity({"name": "Empty"}))

if __name__ == "__main__":
    unittest.main()
