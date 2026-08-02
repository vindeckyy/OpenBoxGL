#!/usr/bin/env python3
"""Tests for Playnite-inspired parity helpers."""

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from parity_backup import BACKUP_ITEMS, create_backup, restore_backup, rotate_backups
from parity_deeplinks import build_launch_url, handle_cli, parse_uri
from parity_emulator_defs import build_launch_command, load_definitions, platform_for_extension, scan_folder
from parity_filter_presets import (
    bigbox_quick_presets,
    explorer_facets,
    filter_games,
    game_matches_rules,
    save_preset,
)
from parity_import_policy import add_exclusion, exclusion_key, filter_imported
from parity_igdb import apply_to_game, search_games
from parity_tracking import TRACKING_MODES, resolve_mode


class FilterPresetTests(unittest.TestCase):
    def test_save_and_match_preset(self):
        state = {"filter_presets": []}
        save_preset(state, "Installed PC", {"platform": "PC", "installed": "installed"}, bigbox_quick=True)
        self.assertEqual(len(state["filter_presets"]), 1)
        self.assertEqual(bigbox_quick_presets(state)[0]["name"], "Installed PC")
        game = {"name": "Doom", "platform": "PC", "store_installed": True}
        self.assertTrue(game_matches_rules(game, state["filter_presets"][0]["rules"]))
        self.assertFalse(game_matches_rules({"platform": "PC", "store_installed": False}, state["filter_presets"][0]["rules"]))

    def test_explorer_facets(self):
        games = [
            {"name": "A", "genre": "Action, RPG", "platform": "PC"},
            {"name": "B", "genre": "RPG", "platform": "Switch"},
        ]
        facets = explorer_facets(games, "genre")
        values = {item["value"] for item in facets}
        self.assertIn("Action", values)
        self.assertIn("RPG", values)

    def test_filter_games_with_category(self):
        games = [{"platform": "NES", "name": "Mario"}, {"platform": "PC", "name": "Doom"}]
        filtered = filter_games(games, {"platform_category": "Console"}, lambda game: "Console" if game["platform"] == "NES" else "Computer")
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["name"], "Mario")


class DeeplinkTests(unittest.TestCase):
    def test_parse_uri(self):
        self.assertEqual(parse_uri("openbox://search/half%20life")["query"], "half life")
        self.assertEqual(parse_uri("openbox://showgame/42")["id"], "42")
        self.assertEqual(parse_uri("openbox://start")["action"], "start")

    def test_parse_uri_rejects_foreign_host(self):
        self.assertEqual(parse_uri("openbox://evil.example/launch/42")["action"], "unknown")
        self.assertEqual(parse_uri("openbox://evil.example/showgame/42")["action"], "unknown")

    def test_dispatch_uri_requires_port_file(self):
        from parity_deeplinks import dispatch_uri
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            code = dispatch_uri("openbox://showgame/42", data_dir)
            self.assertEqual(code, 1)
            # A real port makes it through to the API call.
            (data_dir / "server.port").write_text("12345")
            (data_dir / "server.token").write_text("tok")
            with mock.patch("parity_deeplinks.api_request", return_value={}):
                code = dispatch_uri("openbox://showgame/42", data_dir, token="tok")
            self.assertEqual(code, 0)

    def test_build_launch_url(self):
        url = build_launch_url("http://127.0.0.1:8787", "search", query="quake")
        self.assertIn("deeplink=search", url)
        self.assertIn("quake", url)

    def test_handle_cli_uri_flag(self):
        with mock.patch("parity_deeplinks.dispatch_uri", return_value=0) as dispatch:
            code = handle_cli(["--uri", "openbox://bigbox"], "/tmp")
        self.assertEqual(code, 0)
        dispatch.assert_called_once()


class BackupTests(unittest.TestCase):
    def test_create_and_restore(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = {"settings": {"theme": "dark"}, "games": [{"name": "Test"}]}
            archive = create_backup(root, state, ["library", "settings"], keep=0)
            self.assertTrue(archive.is_file())
            with zipfile.ZipFile(archive) as package:
                manifest = json.loads(package.read("manifest.json"))
            self.assertEqual(set(manifest["items"]), {"library", "settings"})
            (root / "library.json").write_text("{}")
            restore_backup(archive, root)
            restored = json.loads((root / "library.json").read_text())
            self.assertEqual(restored["games"][0]["name"], "Test")

    def test_rotate_backups(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            old = folder / "OpenBoxBackup-2020-01-01.zip"
            newer = folder / "OpenBoxBackup-2020-01-02.zip"
            old.write_bytes(b"old")
            newer.write_bytes(b"new")
            rotate_backups(folder, 1)
            self.assertFalse(old.exists())
            self.assertTrue(newer.exists())

    def test_restore_merges_settings_into_library(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = {"settings": {"theme": "dark", "volume": 7}, "games": [{"name": "Test"}]}
            archive = create_backup(root, state, ["library", "settings"], keep=0)
            # Simulate the running library diverging after the backup.
            (root / "library.json").write_text(json.dumps({"settings": {"theme": "light"}, "games": []}))
            restore_backup(archive, root)
            restored = json.loads((root / "library.json").read_text())
            # Archived settings are merged back into the restored state.
            self.assertEqual(restored["settings"]["theme"], "dark")
            self.assertEqual(restored["settings"]["volume"], 7)
            self.assertEqual(restored["games"][0]["name"], "Test")

    def test_restore_refuses_older_backup_unless_forced(self):
        import time as _time
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = {"settings": {}, "games": [{"name": "Old"}]}
            archive = create_backup(root, state, ["library"], keep=0)
            # Let the backup's manifest timestamp age, then write a newer
            # library so the backup is strictly older than the current state.
            _time.sleep(1.1)
            current = {"settings": {}, "games": [{"name": "New"}]}
            (root / "library.json").write_text(json.dumps(current))
            with self.assertRaises(ValueError):
                restore_backup(archive, root)
            # Force restores anyway.
            restore_backup(archive, root, force=True)
            restored = json.loads((root / "library.json").read_text())
            self.assertEqual(restored["games"][0]["name"], "Old")


class TrackingTests(unittest.TestCase):
    def test_resolve_mode_defaults(self):
        config = resolve_mode({}, {})
        self.assertEqual(config["mode"], "default")
        self.assertIn(config["mode"], TRACKING_MODES)

    def test_per_game_override(self):
        config = resolve_mode({"tracking_mode": "process_name", "tracking_process_name": "dolphin"}, {"tracking_mode": "default"})
        self.assertEqual(config["mode"], "process_name")
        self.assertEqual(config["process_name"], "dolphin")

    def test_invalid_per_game_timing_uses_safe_defaults(self):
        config = resolve_mode({"tracking_delay": "later", "tracking_frequency": "often"}, {})
        self.assertEqual(config["delay"], 0)
        self.assertEqual(config["frequency"], 2)


class ImportExclusionTests(unittest.TestCase):
    def test_filter_imported(self):
        state = {"settings": {}}
        add_exclusion(state, "steam", "570")
        imported = [
            {"name": "Dota", "steam_app_id": "570"},
            {"name": "Portal", "steam_app_id": "400"},
        ]
        kept = filter_imported(imported, state)
        self.assertEqual([game["name"] for game in kept], ["Portal"])
        self.assertEqual(exclusion_key(imported[0]), ("steam", "570"))


class IgdbTests(unittest.TestCase):
    def test_search_games_mocked(self):
        payload = [{"id": 1, "name": "Hollow Knight", "summary": "Metroidvania", "genres": [{"name": "Platform"}], "platforms": [{"name": "Linux"}]}]
        with mock.patch("parity_igdb.igdb_request", return_value=payload):
            results = search_games("Hollow Knight", platform="Linux")
        self.assertEqual(results[0]["name"], "Hollow Knight")

    def test_apply_to_game(self):
        game = {"name": "Old"}
        updated = apply_to_game(game, {"name": "New", "description": "Desc", "igdb_id": 9})
        self.assertEqual(updated["name"], "New")
        self.assertEqual(updated["igdb_id"], 9)


class EmulatorDefinitionTests(unittest.TestCase):
    def test_launch_command_keeps_rom_paths_with_spaces(self):
        command = build_launch_command(
            {"id": "dolphin", "name": "Dolphin", "startup": "-b -e {path}"},
            "/tmp/My Games/demo.iso",
            prefix=["dolphin-emu"],
        )
        self.assertEqual(command, ["dolphin-emu", "-b", "-e", "/tmp/My Games/demo.iso"])

    def test_load_definitions(self):
        definitions = load_definitions()
        self.assertTrue(any(item.get("id") == "retroarch" for item in definitions))

    def test_scan_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rom = root / "demo.nes"
            rom.write_bytes(b"nes")
            games = scan_folder(root, definitions=load_definitions())
            self.assertEqual(len(games), 1)
            self.assertEqual(games[0]["platform"], "NES")

    def test_platform_for_extension(self):
        platform, definition = platform_for_extension("nes")
        self.assertEqual(platform, "NES")
        self.assertIsNotNone(definition)


class BackupSafetyTests(unittest.TestCase):
    def test_restore_rejects_zip_slip_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("manifest.json", json.dumps({"items": ["media"]}))
                package.writestr("media/../escape.txt", "unsafe")
            with self.assertRaises(ValueError):
                restore_backup(archive, root, items=["media"])
            self.assertFalse((root / "escape.txt").exists())


if __name__ == "__main__":
    unittest.main()
