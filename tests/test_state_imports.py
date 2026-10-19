"""Tests for pkg.state.imports — extracted import/merge/sync domain logic."""

import sys
import threading
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest


class TestStateImports(unittest.TestCase):
    def test_module_importable(self):
        import pkg.state.imports
        self.assertIsNotNone(pkg.state.imports)

    def test_watch_stop_exported(self):
        import threading
        from pkg.state.imports import WATCH_STOP
        self.assertIsInstance(WATCH_STOP, threading.Event)

    def test_game_identity_exported(self):
        from pkg.state.imports import game_identity
        self.assertTrue(callable(game_identity))

    def test_game_identity_steam(self):
        from pkg.state.imports import game_identity
        game = {"steam_app_id": "12345"}
        self.assertEqual(game_identity(game), ("steam", "12345"))

    def test_game_identity_lutris(self):
        from pkg.state.imports import game_identity
        game = {"lutris_id": "my-game"}
        self.assertEqual(game_identity(game), ("lutris", "my-game"))

    def test_game_identity_path(self):
        from pkg.state.imports import game_identity
        game = {"path": "/some/rom.bin"}
        result = game_identity(game)
        self.assertEqual(result[0], "path")
        self.assertIn("rom.bin", result[1])

    def test_game_identity_native_storefronts(self):
        from pkg.state.imports import _launcher_label, game_identity
        self.assertEqual(game_identity({"gog_id": "123"}), ("gog", "123"))
        self.assertEqual(game_identity({"ea_id": "456"}), ("ea", "456"))
        self.assertEqual(game_identity({"ubisoft_id": "789"}), ("ubisoft", "789"))
        self.assertEqual(_launcher_label({"gog_id": "123"}), "GOG")
        self.assertEqual(_launcher_label({"ea_id": "456"}), "EA app")
        self.assertEqual(_launcher_label({"ubisoft_id": "789"}), "Ubisoft Connect")

    def test_consolidate_existing_games_exported(self):
        from pkg.state.imports import consolidate_existing_games
        self.assertTrue(callable(consolidate_existing_games))

    def test_consolidate_existing_games_dedup(self):
        from pkg.state.imports import consolidate_existing_games
        games = [
            {"name": "Game A", "steam_app_id": "100", "path": "/a"},
            {"name": "Game A", "steam_app_id": "100", "path": "/a"},
        ]
        removed = consolidate_existing_games(games)
        self.assertEqual(len(games), 1)
        self.assertEqual(len(removed), 1)

    def test_import_folder_path_exported(self):
        from pkg.state.imports import import_folder_path
        self.assertTrue(callable(import_folder_path))

    def test_merge_imported_games_exported(self):
        from pkg.state.imports import merge_imported_games
        self.assertTrue(callable(merge_imported_games))

    def test_auto_import_worker_exported(self):
        from pkg.state.imports import auto_import_worker
        self.assertTrue(callable(auto_import_worker))

    def test_auto_import_worker_passes_emulator_id(self):
        from pkg.state.imports import auto_import_worker

        state = {
            "settings": {
                "emulator_scan_configs": [
                    {"folder": "/tmp/roms", "emulator_id": "org.DolphinEmu.dolphin-emu", "auto_update": True},
                ],
            },
        }
        with mock.patch("pkg.state.imports.load_state", return_value=state), \
             mock.patch("pkg.state.imports.scan_emulator_folder", return_value=[]) as scan, \
             mock.patch("pkg.state.imports.merge_imported_games", return_value=(0, 0)), \
             mock.patch("pkg.state.imports.WATCH_STOP") as watch_stop:
            watch_stop.wait.side_effect = [False, True]
            auto_import_worker()
        scan.assert_called_once_with("/tmp/roms", emulator_id="org.DolphinEmu.dolphin-emu")

    def test_auto_import_worker_runs_storefront_and_watch_paths(self):
        from pkg.state.imports import auto_import_worker

        state = {
            "settings": {
                "watch_folders": ["/tmp/watch"],
                "storefront_auto_import": {"steam": True, "heroic": True, "lutris": True, "gameyfin": True},
                "emulator_scan_configs": [],
            },
        }
        with mock.patch("pkg.state.imports.load_state", return_value=state), \
             mock.patch("pkg.state.imports.import_folder_path", return_value=(0, 0, {})) as folder_import, \
             mock.patch("pkg.state.imports.import_steam", return_value=[]), \
             mock.patch("pkg.state.imports.import_heroic", return_value=[]), \
             mock.patch("pkg.state.imports.import_lutris", return_value=[]), \
             mock.patch("pkg.state.imports.catalog_gameyfin", return_value=([], [])), \
             mock.patch("pkg.state.imports.catalog_entries_to_games", return_value=[]), \
             mock.patch("pkg.state.imports.merge_imported_games", return_value=(0, 0)), \
             mock.patch("pkg.state.imports.WATCH_STOP") as watch_stop:
            watch_stop.wait.side_effect = [False, True]
            auto_import_worker()
        folder_import.assert_called_once_with("/tmp/watch")

    def test_auto_import_worker_imports_windows_storefronts(self):
        from pkg.state.imports import auto_import_worker

        state = {
            "settings": {
                "storefront_auto_import": {
                    "gog": {"enabled": True},
                    "ea": {"enabled": True},
                    "ubisoft": {"enabled": True},
                },
                "emulator_scan_configs": [],
            },
        }
        with mock.patch("pkg.state.imports.load_state", return_value=state),              mock.patch("pkg.state.imports.IS_WINDOWS", True),              mock.patch("pkg.parity.parity_storefront_windows.import_gog", return_value=[]) as gog,              mock.patch("pkg.parity.parity_storefront_windows.import_ea", return_value=[]) as ea,              mock.patch("pkg.parity.parity_storefront_windows.import_ubisoft", return_value=[]) as ubisoft,              mock.patch("pkg.state.imports.WATCH_STOP") as watch_stop:
            watch_stop.wait.side_effect = [False, True]
            auto_import_worker()
        gog.assert_called_once()
        ea.assert_called_once()
        ubisoft.assert_called_once()

    def test_auto_import_worker_counts_through_the_real_merge(self):
        """The worker read merge_imported_games(...)["added"] on a (added, found)
        tuple, a TypeError no except clause caught, so the first storefront tick
        killed the auto-import thread. The other tests mocked the merge with a
        dict and could not see it; this one runs the real merge."""
        from pkg.state.imports import auto_import_worker

        library = {"games": [], "settings": {}}
        state = {"settings": {"storefront_auto_import": {"steam": True}, "emulator_scan_configs": []}}

        def update_state(mutator):
            mutator(library)
            return library

        steam_game = {"name": "Portal", "path": "steam://rungameid/400", "steam_app_id": "400", "source": "steam"}
        with mock.patch("pkg.state.imports.load_state", return_value=state), \
             mock.patch("pkg.state.imports.update_state", side_effect=update_state), \
             mock.patch("pkg.state.imports.import_steam", return_value=[steam_game]), \
             mock.patch("pkg.state.imports.LOGGER") as logger, \
             mock.patch("pkg.state.imports.WATCH_STOP") as watch_stop:
            watch_stop.wait.side_effect = [False, True]
            auto_import_worker()
        self.assertEqual([game["name"] for game in library["games"]], ["Portal"])
        logger.warning.assert_not_called()

    def test_auto_import_worker_imports_plugin_storefronts(self):
        from pkg.state.imports import auto_import_worker

        library = {"games": [], "settings": {}}
        state = {
            "settings": {
                "storefront_auto_import": {
                    "plugin:vendor.store": {"enabled": True, "platform": "PC", "include_uninstalled": True},
                },
                "emulator_scan_configs": [],
            },
        }

        def update_state(mutator):
            mutator(library)
            return library

        plugin_game = {
            "game_id": "plugin:vendor.store:one",
            "name": "One",
            "path": "/games/one",
            "launch": "/games/one",
            "source": "Vendor Store",
            "plugin_source": "vendor.store",
        }
        with mock.patch("pkg.state.imports.load_state", return_value=state), \
             mock.patch("pkg.state.imports.update_state", side_effect=update_state), \
             mock.patch(
                 "pkg.parity.parity_plugin_storefront.plugin_storefront_import",
                 return_value=[plugin_game],
             ) as plugin_import, \
             mock.patch("pkg.state.imports.WATCH_STOP") as watch_stop:
            watch_stop.wait.side_effect = [False, True]
            auto_import_worker()
        plugin_import.assert_called_once_with("vendor.store")
        self.assertEqual([game["game_id"] for game in library["games"]], ["plugin:vendor.store:one"])
        self.assertEqual(library["games"][0]["platform"], "PC")

    def test_auto_import_worker_filters_gameyfin_catalog(self):
        from pkg.state.imports import auto_import_worker

        state = {
            "settings": {
                "storefront_auto_import": {
                    "gameyfin": {"enabled": True, "include_uninstalled": False},
                },
                "auto_import_excluded_platforms": ["SNES"],
                "emulator_scan_configs": [],
            },
        }
        catalog = [
            {"id": "1", "name": "Installed PC", "installed": True, "platform": "PC", "path": "/p1", "launch": ""},
            {"id": "2", "name": "Uninstalled PC", "installed": False, "platform": "PC", "path": "/p2", "launch": ""},
            {"id": "3", "name": "Installed SNES", "installed": True, "platform": "SNES", "path": "/p3", "launch": ""},
        ]
        with mock.patch("pkg.state.imports.load_state", return_value=state), \
             mock.patch("pkg.state.imports.catalog_gameyfin", return_value=(catalog, [])), \
             mock.patch("pkg.state.imports.merge_imported_games", return_value=(0, 0)) as merge, \
             mock.patch("pkg.state.imports.WATCH_STOP") as watch_stop:
            watch_stop.wait.side_effect = [False, True]
            auto_import_worker()
        imported = merge.call_args.args[0]
        self.assertEqual([game["name"] for game in imported], ["Installed PC"])

    def test_auto_import_worker_handles_load_failure(self):
        from pkg.state.imports import auto_import_worker

        with mock.patch("pkg.state.imports.load_state", side_effect=OSError("bad state")), \
             mock.patch("pkg.state.imports.WATCH_STOP") as watch_stop:
            watch_stop.wait.side_effect = [False, True]
            auto_import_worker()

    def test_auto_import_worker_honors_cancel_event(self):
        from pkg.state.imports import auto_import_worker

        cancel = threading.Event()
        cancel.set()
        with mock.patch("pkg.state.imports.WATCH_STOP") as watch_stop:
            watch_stop.wait.return_value = False
            auto_import_worker(cancel_event=cancel)

    def test_auto_import_worker_warns_on_import_errors(self):
        from pkg.state.imports import auto_import_worker
        from parity_gameyfin import GameyfinError

        state = {
            "settings": {
                "watch_folders": ["/bad"],
                "storefront_auto_import": {
                    "steam": True,
                    "heroic": True,
                    "lutris": True,
                    "gameyfin": True,
                },
                "emulator_scan_configs": [
                    {"folder": "", "emulator_id": "emu", "auto_update": True},
                    {"folder": "/roms", "emulator_id": "emu", "auto_update": False},
                    {"folder": "/roms", "emulator_id": "emu", "auto_update": True},
                ],
            },
        }
        with mock.patch("pkg.state.imports.load_state", return_value=state), \
             mock.patch("pkg.state.imports.import_folder_path", side_effect=OSError("bad folder")), \
             mock.patch("pkg.state.imports.import_steam", side_effect=ValueError("bad steam")), \
             mock.patch("pkg.state.imports.import_heroic", side_effect=ValueError("bad heroic")), \
             mock.patch("pkg.state.imports.import_lutris", side_effect=ValueError("bad lutris")), \
             mock.patch("pkg.state.imports.catalog_gameyfin", side_effect=GameyfinError("bad gameyfin")), \
             mock.patch("pkg.state.imports.scan_emulator_folder", side_effect=ValueError("bad scan")), \
             mock.patch("pkg.state.imports.merge_imported_games", return_value=(0, 0)), \
             mock.patch("pkg.state.imports.WATCH_STOP") as watch_stop:
            watch_stop.wait.side_effect = [False, True]
            auto_import_worker()

    def test_sync_cloud_exported(self):
        from pkg.state.imports import sync_cloud
        self.assertTrue(callable(sync_cloud))

    def test_webapp_state_re_exports(self):
        """from webapp_state import X still works for moved functions."""
        import webapp_state
        self.assertIs(webapp_state.WATCH_STOP, __import__("pkg.state.imports", fromlist=["WATCH_STOP"]).WATCH_STOP)
        self.assertIs(webapp_state.game_identity, __import__("pkg.state.imports", fromlist=["game_identity"]).game_identity)
        self.assertIs(webapp_state.consolidate_existing_games, __import__("pkg.state.imports", fromlist=["consolidate_existing_games"]).consolidate_existing_games)
        self.assertIs(webapp_state.import_folder_path, __import__("pkg.state.imports", fromlist=["import_folder_path"]).import_folder_path)
        self.assertIs(webapp_state.merge_imported_games, __import__("pkg.state.imports", fromlist=["merge_imported_games"]).merge_imported_games)
        self.assertIs(webapp_state.auto_import_worker, __import__("pkg.state.imports", fromlist=["auto_import_worker"]).auto_import_worker)
        self.assertIs(webapp_state.sync_cloud, __import__("pkg.state.imports", fromlist=["sync_cloud"]).sync_cloud)


if __name__ == "__main__":
    unittest.main()
