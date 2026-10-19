"""Tests for storefront catalog helpers."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pkg.parity  # noqa: F401,E402  # register flat-import finder

from parity_storefront import catalog_entries_to_games, catalog_steam, storefront_catalog


class StorefrontTests(unittest.TestCase):
    def test_catalog_steam_marks_installed(self):
        with TemporaryDirectory() as directory:
            home = Path(directory)
            steamapps = home / ".local/share/Steam/steamapps"
            steamapps.mkdir(parents=True)
            (steamapps / "appmanifest_42.acf").write_text('"AppState"\n{\n"appid" "42"\n"name" "Real Game"\n"installdir" "RealGame"\n}', encoding="utf-8")
            (steamapps / "common" / "RealGame").mkdir(parents=True)
            userdata = home / ".local/share/Steam/userdata/1/config"
            userdata.mkdir(parents=True)
            (userdata / "localconfig.vdf").write_text('"Apps"\n{\n"42"\n{\n}\n"99"\n{\n}\n}', encoding="utf-8")
            catalog = catalog_steam(home)
            by_id = {item["id"]: item for item in catalog}
            self.assertTrue(by_id["42"]["installed"])
            self.assertEqual(by_id["42"]["install_uri"], "steam://store/42")
            self.assertFalse(by_id["99"]["installed"])
            self.assertEqual(by_id["99"]["install_uri"], "steam://install/99")
            # A manifest whose install directory vanished is Missing, not
            # Installed, even though Steam still lists the app as owned.
            (steamapps / "common" / "RealGame").rmdir()
            catalog = {item["id"]: item for item in catalog_steam(home)}
            self.assertFalse(catalog["42"]["installed"])
            self.assertEqual(catalog["42"]["install_uri"], "steam://install/42")
            self.assertEqual(catalog["42"]["name"], "Real Game")

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
