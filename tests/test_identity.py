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
)

class TestIdentity(unittest.TestCase):
    def test_normalize_steam(self):
        self.assertEqual(normalize_identity({"steam_app_id": 730}), "steam:730")

    def test_normalize_heroic(self):
        self.assertEqual(normalize_identity({"heroic_app_id": "abc123", "source": "Epic"}), "heroic:Epic:abc123")

    def test_normalize_lutris(self):
        self.assertEqual(normalize_identity({"lutris_id": "my-game"}), "lutris:my-game")

    def test_normalize_gameyfin(self):
        self.assertEqual(normalize_identity({"gameyfin_id": "xyz"}), "gameyfin:xyz")

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
