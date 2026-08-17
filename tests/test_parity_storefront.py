"""Tests for storefront catalog helpers."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from parity_storefront import catalog_entries_to_games, catalog_steam, storefront_catalog


class StorefrontTests(unittest.TestCase):
    def test_catalog_steam_marks_installed(self):
        with TemporaryDirectory() as directory:
            home = Path(directory)
            steamapps = home / ".local/share/Steam/steamapps"
            steamapps.mkdir(parents=True)
            (steamapps / "appmanifest_42.acf").write_text(
                '"AppState"\n{\n"appid" "42"\n"name" "Real Game"\n"installdir" "RealGame"\n}'
            )
            userdata = home / ".local/share/Steam/userdata/1/config"
            userdata.mkdir(parents=True)
            (userdata / "localconfig.vdf").write_text('"Apps"\n{\n"42"\n{\n}\n"99"\n{\n}\n}')
            catalog = catalog_steam(home)
            by_id = {item["id"]: item for item in catalog}
            self.assertTrue(by_id["42"]["installed"])
            self.assertFalse(by_id["99"]["installed"])

    def test_catalog_entries_to_games_uninstalled_only(self):
        entries = [
            {"name": "Installed", "source": "Steam", "installed": True, "path": "/usr/bin/steam", "launch": "steam", "steam_app_id": "1"},
            {"name": "Missing", "source": "Steam", "installed": False, "path": "/usr/bin/xdg-open", "launch": "xdg-open steam://store/2", "steam_app_id": "2"},
        ]
        games = catalog_entries_to_games(entries, uninstalled_only=True)
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["steam_app_id"], "2")

    def test_storefront_catalog_lutris(self):
        class Result:
            stdout = '[{"id":7,"name":"EA Game","installed":false,"service":"ea app","runner":"wine"}]'

        with TemporaryDirectory() as directory:
            catalog = storefront_catalog(
                "lutris",
                home=Path(directory),
                run=lambda *args, **kwargs: Result(),
                which=lambda name: "/usr/bin/lutris" if name == "lutris" else None,
            )
            self.assertEqual(catalog[0]["source"], "EA")
            self.assertFalse(catalog[0]["installed"])


if __name__ == "__main__":
    unittest.main()
