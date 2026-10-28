"""Tests for storefront catalog helpers."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pkg.parity  # noqa: F401,E402  # register flat-import finder

import parity_storefront as storefront_module
from parity_storefront import catalog_entries_to_games, catalog_heroic, catalog_steam, storefront_catalog
from pkg.platform_compat import steam_candidate_roots, url_opener_command


class StorefrontTests(unittest.TestCase):
    def test_catalog_steam_marks_installed(self):
        with TemporaryDirectory() as directory:
            home = Path(directory)
            steam_root = steam_candidate_roots(home)[0]
            steamapps = steam_root / "steamapps"
            steamapps.mkdir(parents=True)
            (steamapps / "appmanifest_42.acf").write_text('"AppState"\n{\n"appid" "42"\n"name" "Real Game"\n"installdir" "RealGame"\n}', encoding="utf-8")
            (steamapps / "common" / "RealGame").mkdir(parents=True)
            userdata = steam_root / "userdata/1/config"
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

    def test_uninstalled_steam_entries_use_the_platform_opener(self):
        with TemporaryDirectory() as directory:
            home = Path(directory)
            steam_root = steam_candidate_roots(home)[0]
            (steam_root / "steamapps").mkdir(parents=True)
            userdata = steam_root / "userdata/1/config"
            userdata.mkdir(parents=True)
            (userdata / "localconfig.vdf").write_text('"Apps"\n{\n"99"\n{\n}\n}', encoding="utf-8")
            catalog = catalog_steam(home)
        opener = url_opener_command()
        self.assertEqual(catalog[0]["path"], opener)
        self.assertTrue(catalog[0]["launch"].startswith(opener))

    def test_uninstalled_heroic_entries_use_the_platform_opener(self):
        with TemporaryDirectory() as directory:
            home = Path(directory)
            base = home / "heroic"
            cache = base / "store_cache"
            cache.mkdir(parents=True)
            (cache / "legendaryLibrary.json").write_text(
                json.dumps({"abc": {"title": "Celeste", "store": "Epic"}}), encoding="utf-8"
            )
            with mock.patch.object(storefront_module, "heroic_bases", return_value=[base]), \
                 mock.patch.object(storefront_module, "import_heroic", return_value=[]):
                catalog = catalog_heroic(home)
        opener = url_opener_command()
        self.assertEqual(catalog[0]["source"], "Epic")
        self.assertFalse(catalog[0]["installed"])
        self.assertEqual(catalog[0]["path"], opener)
        self.assertTrue(catalog[0]["launch"].startswith(opener))

    def test_a_host_without_a_url_opener_says_so(self):
        with TemporaryDirectory() as directory:
            with mock.patch.object(storefront_module, "url_opener_command", return_value=None):
                with self.assertRaises(FileNotFoundError):
                    catalog_steam(Path(directory))
                with self.assertRaises(FileNotFoundError):
                    catalog_heroic(Path(directory))


if __name__ == "__main__":
    unittest.main()
