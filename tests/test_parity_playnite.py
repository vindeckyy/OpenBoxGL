#!/usr/bin/env python3
"""Tests for Playnite-inspired parity helpers."""

import json
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pkg" / "parity"))

import pkg.parity  # noqa: F401,E402  # register flat-import finder

from parity_backup import create_backup, restore_backup, rotate_backups
from parity_deeplinks import build_launch_url, handle_cli, parse_uri
from parity_emulator_defs import (
    build_launch_command,
    candidates_for_extension,
    load_definitions,
    load_registry,
    merge_profiles_from_definitions,
    platform_for_extension,
    scan_folder,
)
from parity_filter_presets import (
    bigbox_quick_presets,
    explorer_facets,
    filter_games,
    game_matches_rules,
    save_preset,
)
from parity_gamescope import kiosk_command
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

    def test_acronym_match_search(self):
        games = [
            {"name": "The Legend of Zelda: Ocarina of Time", "platform": "N64"},
            {"name": "Metal Gear Solid", "platform": "PS1"},
            {"name": "Castlevania: Symphony of the Night", "platform": "PS1"},
            {"name": "Final Fantasy VII", "platform": "PS1"},
        ]
        self.assertEqual([g["name"] for g in filter_games(games, {"query": "oot"})], ["The Legend of Zelda: Ocarina of Time"])
        self.assertEqual([g["name"] for g in filter_games(games, {"query": "mgs"})], ["Metal Gear Solid"])
        self.assertEqual([g["name"] for g in filter_games(games, {"query": "sotn"})], ["Castlevania: Symphony of the Night"])
        self.assertEqual([g["name"] for g in filter_games(games, {"query": "ff"})], ["Final Fantasy VII"])

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
        self.assertEqual(parse_uri("openbox:search/quake")["query"], "quake")
        self.assertEqual(parse_uri("openbox://localhost/showgame/stable-id")["id"], "stable-id")
        self.assertEqual(parse_uri("openbox://settings/audio")["panel"], "audio")
        self.assertEqual(parse_uri("openbox://fullscreen")["mode"], "bigbox")
        self.assertEqual(parse_uri("openbox://game/abc")["id"], "abc")
        self.assertEqual(parse_uri("openbox://")["action"], "start")

    def test_parse_uri_rejects_foreign_host(self):
        self.assertEqual(parse_uri("openbox://evil.example/launch/42")["action"], "unknown")
        self.assertEqual(parse_uri("openbox://evil.example/showgame/42")["action"], "unknown")
        self.assertEqual(parse_uri("openbox://evil.com")["action"], "unknown")

    def test_parse_uri_bare_localhost_is_start(self):
        self.assertEqual(parse_uri("openbox://localhost")["action"], "start")

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

    def test_dispatch_launch_accepts_stable_game_id(self):
        from parity_deeplinks import dispatch_uri

        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            (data_dir / "server.port").write_text("12345")
            (data_dir / "server.token").write_text("tok")
            with mock.patch("parity_deeplinks.api_request", return_value={}) as api_request:
                code = dispatch_uri(
                    "openbox://launch/game-0123456789abcdef01234567-1",
                    data_dir,
                    token="tok",
                )
            self.assertEqual(code, 0)
            api_request.assert_called_once_with(
                "127.0.0.1",
                12345,
                "tok",
                "/api/launch",
                "POST",
                {"game_id": "game-0123456789abcdef01234567-1"},
            )

    def test_dispatch_launch_legacy_numeric_index(self):
        from parity_deeplinks import dispatch_uri

        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            (data_dir / "server.port").write_text("12345")
            (data_dir / "server.token").write_text("tok")
            with mock.patch("parity_deeplinks.api_request", return_value={}) as api_request:
                code = dispatch_uri("openbox://launch/3", data_dir, token="tok")
            self.assertEqual(code, 0)
            api_request.assert_called_once_with(
                "127.0.0.1",
                12345,
                "tok",
                "/api/launch",
                "POST",
                {"id": 3},
            )

    def test_dispatch_showgame_accepts_stable_game_id(self):
        from parity_deeplinks import dispatch_uri

        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            (data_dir / "server.port").write_text("12345")
            (data_dir / "server.token").write_text("tok")
            with mock.patch("webbrowser.open") as open_browser:
                code = dispatch_uri(
                    "openbox://showgame/game-stable-id",
                    data_dir,
                    token="tok",
                    open_browser=True,
                )
            self.assertEqual(code, 0)
            open_browser.assert_called_once_with(
                "http://127.0.0.1:12345/?deeplink=showgame&id=game-stable-id"
            )

    def test_dispatch_launch_requires_game_id(self):
        from parity_deeplinks import dispatch_uri

        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            (data_dir / "server.port").write_text("12345")
            code = dispatch_uri("openbox://launch/", data_dir, token="tok")
            self.assertEqual(code, 1)

    def test_dispatch_search_builds_url(self):
        from parity_deeplinks import dispatch_uri

        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            (data_dir / "server.port").write_text("12345")
            with mock.patch("webbrowser.open") as open_browser:
                code = dispatch_uri(
                    "openbox://search/half%20life",
                    data_dir,
                    open_browser=True,
                )
            self.assertEqual(code, 0)
            self.assertIn("deeplink=search", open_browser.call_args[0][0])
            self.assertIn("half", open_browser.call_args[0][0])

    def test_dispatch_bigbox_and_settings(self):
        from parity_deeplinks import dispatch_uri

        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            (data_dir / "server.port").write_text("12345")
            with mock.patch("webbrowser.open") as open_browser:
                self.assertEqual(dispatch_uri("openbox://bigbox", data_dir, open_browser=True), 0)
                self.assertEqual(dispatch_uri("openbox://settings/general", data_dir, open_browser=True), 0)
            urls = [call.args[0] for call in open_browser.call_args_list]
            self.assertTrue(any("deeplink=bigbox" in url for url in urls))
            self.assertTrue(any("deeplink=settings" in url for url in urls))

    def test_launcher_menu_items(self):
        from parity_deeplinks import launcher_menu_items

        items = launcher_menu_items([{"name": "Doom", "id": 7}])
        self.assertEqual(items[0]["id"], "bigbox")
        self.assertEqual(items[-1]["label"], "Doom")

    def test_api_request_posts_json(self):
        from parity_deeplinks import api_request

        response = mock.Mock()
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=False)
        with mock.patch("parity_deeplinks.urllib.request.urlopen", return_value=response), \
             mock.patch("parity_deeplinks.read_limited", return_value=b'{"ok": true}'):
            payload = api_request("127.0.0.1", 8787, "tok", "/api/launch", "POST", {"game_id": "g1"})
        self.assertTrue(payload["ok"])

    def test_read_port_file(self):
        from parity_deeplinks import read_port_file

        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            self.assertEqual(read_port_file(data_dir), 0)
            (data_dir / "server.port").write_text("4321")
            self.assertEqual(read_port_file(data_dir), 4321)
            (data_dir / "server.port").write_text("bad")
            self.assertEqual(read_port_file(data_dir), 0)

    def test_handle_cli_openbox_scheme_arg(self):
        with mock.patch("parity_deeplinks.dispatch_uri", return_value=0) as dispatch:
            code = handle_cli(["openbox://bigbox"], "/tmp")
        self.assertEqual(code, 0)
        dispatch.assert_called_once()

    def test_handle_cli_launcher_flag(self):
        with mock.patch("parity_deeplinks.run_keyboard_launcher", return_value=0) as launcher:
            code = handle_cli(["--launcher"], "/tmp")
        self.assertEqual(code, 0)
        launcher.assert_called_once()

    def test_dispatch_start_with_running_server(self):
        from parity_deeplinks import dispatch_uri

        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            (data_dir / "server.port").write_text("12345")
            (data_dir / "server.token").write_text("tok")
            with mock.patch("webbrowser.open"):
                code = dispatch_uri("openbox://start", data_dir, open_browser=True)
            self.assertEqual(code, 0)

    def test_dispatch_unknown_action(self):
        from parity_deeplinks import dispatch_uri

        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            (data_dir / "server.port").write_text("12345")
            code = dispatch_uri("openbox://unknown/action", data_dir)
            self.assertEqual(code, 1)

    def test_run_keyboard_launcher_without_picker(self):
        from parity_deeplinks import run_keyboard_launcher

        with mock.patch("pathlib.Path.exists", return_value=False), \
             mock.patch("shutil.which", return_value=None):
            code = run_keyboard_launcher("/tmp")
        self.assertEqual(code, 1)

    def test_build_launch_url(self):
        url = build_launch_url("http://127.0.0.1:8787", "search", query="quake")
        self.assertIn("deeplink=search", url)
        self.assertIn("quake", url)
        self.assertEqual(build_launch_url("http://127.0.0.1:8787", "start"), "http://127.0.0.1:8787")
        self.assertIn("showgame", build_launch_url("http://127.0.0.1:8787", "showgame", id="g1"))
        self.assertIn("bigbox", build_launch_url("http://127.0.0.1:8787", "bigbox"))
        self.assertIn("settings", build_launch_url("http://127.0.0.1:8787", "settings"))
        self.assertEqual(build_launch_url("http://127.0.0.1:8787", "unknown"), "http://127.0.0.1:8787")

    def test_handle_cli_uri_flag(self):
        with mock.patch("parity_deeplinks.dispatch_uri", return_value=0) as dispatch:
            code = handle_cli(["--uri", "openbox://bigbox"], "/tmp")
        self.assertEqual(code, 0)
        dispatch.assert_called_once()

    def test_handle_cli_uri_missing_argument(self):
        with mock.patch("builtins.print"):
            code = handle_cli(["--uri"], "/tmp")
        self.assertEqual(code, 2)

    def test_dispatch_showgame_prints_url(self):
        from parity_deeplinks import dispatch_uri

        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            (data_dir / "server.port").write_text("12345")
            with mock.patch("builtins.print") as printer:
                code = dispatch_uri("openbox://showgame/42", data_dir, open_browser=False)
            self.assertEqual(code, 0)
            self.assertIn("showgame", printer.call_args[0][0])

    def test_handle_cli_help(self):
        with mock.patch("builtins.print") as mock_print:
            code = handle_cli(["--help"], "/tmp")
            self.assertEqual(code, 0)
            mock_print.assert_called()
        with mock.patch("builtins.print") as mock_print:
            code = handle_cli(["-h"], "/tmp")
            self.assertEqual(code, 0)
            mock_print.assert_called()

    def test_handle_cli_unrecognized_returns_none(self):
        self.assertIsNone(handle_cli(["--web"], "/tmp"))

    def test_dispatch_start_without_server_returns_none(self):
        from parity_deeplinks import dispatch_uri

        with tempfile.TemporaryDirectory() as temporary:
            code = dispatch_uri("openbox://start", Path(temporary))
        self.assertIsNone(code)

    def test_dispatch_start_webbrowser_failure_still_ok(self):
        from parity_deeplinks import dispatch_uri

        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            (data_dir / "server.port").write_text("12345")
            (data_dir / "server.token").write_text("tok")
            with mock.patch("webbrowser.open", side_effect=OSError("no browser")):
                code = dispatch_uri("openbox://start", data_dir, open_browser=True)
            self.assertEqual(code, 0)

    def test_dispatch_prints_urls_without_browser(self):
        from parity_deeplinks import dispatch_uri

        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            (data_dir / "server.port").write_text("12345")
            with mock.patch("builtins.print") as printer:
                self.assertEqual(dispatch_uri("openbox://search/quake", data_dir), 0)
                self.assertEqual(dispatch_uri("openbox://bigbox", data_dir), 0)
                self.assertEqual(dispatch_uri("openbox://settings/audio", data_dir), 0)
            printed = " ".join(call.args[0] for call in printer.call_args_list)
            self.assertIn("deeplink=search", printed)
            self.assertIn("deeplink=bigbox", printed)
            self.assertIn("deeplink=settings", printed)

    def test_dispatch_api_error_returns_one(self):
        from parity_deeplinks import dispatch_uri
        import urllib.error

        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            (data_dir / "server.port").write_text("12345")
            with mock.patch(
                "parity_deeplinks.api_request",
                side_effect=urllib.error.URLError("down"),
            ):
                code = dispatch_uri("openbox://launch/1", data_dir, token="tok")
            self.assertEqual(code, 1)

    def _run_keyboard_launcher_menu(self, picker, selection):
        from parity_deeplinks import run_keyboard_launcher

        with mock.patch("pathlib.Path.exists", return_value=False), \
             mock.patch("shutil.which", return_value=f"/usr/bin/{picker}"), \
             mock.patch("pathlib.Path.is_file", return_value=False), \
             mock.patch("subprocess.check_output", return_value=selection.encode()), \
             mock.patch("builtins.print") as printer, \
             mock.patch("builtins.input", return_value="quake"):
            code = run_keyboard_launcher("/tmp")
        return code, printer

    def test_run_keyboard_launcher_rofi_selections(self):
        for selection, expected in (
            ("#bigbox\tOpen Big Box", "openbox://bigbox"),
            ("#settings\tOpen Settings", "openbox://settings"),
            ("/search\tSearch library", "openbox://search/quake"),
            ("/refresh\tRefresh library", "openbox://start"),
            ("custom-uri", "custom-uri"),
        ):
            code, printer = self._run_keyboard_launcher_menu("rofi", selection)
            self.assertEqual(code, 0)
            self.assertEqual(printer.call_args[0][0], expected)

    def test_run_keyboard_launcher_wofi_and_dmenu(self):
        code, printer = self._run_keyboard_launcher_menu("wofi", "#bigbox\tOpen Big Box")
        self.assertEqual(code, 0)
        self.assertEqual(printer.call_args[0][0], "openbox://bigbox")
        code, printer = self._run_keyboard_launcher_menu("dmenu", "#settings\tOpen Settings")
        self.assertEqual(code, 0)
        self.assertEqual(printer.call_args[0][0], "openbox://settings")

    def test_run_keyboard_launcher_empty_selection(self):
        code, printer = self._run_keyboard_launcher_menu("rofi", "")
        self.assertEqual(code, 0)
        printer.assert_not_called()

    def test_run_keyboard_launcher_script_path(self):
        from parity_deeplinks import run_keyboard_launcher

        with mock.patch("pathlib.Path.exists", return_value=False), \
             mock.patch("shutil.which", return_value="/usr/bin/rofi"), \
             mock.patch("pathlib.Path.is_file", return_value=True), \
             mock.patch("subprocess.call", return_value=0) as call_script:
            code = run_keyboard_launcher("/tmp")
        self.assertEqual(code, 0)
        call_script.assert_called_once()

    def test_run_keyboard_launcher_subprocess_errors(self):
        from parity_deeplinks import run_keyboard_launcher
        import subprocess

        with mock.patch("pathlib.Path.exists", return_value=False), \
             mock.patch("shutil.which", return_value="/usr/bin/rofi"), \
             mock.patch("pathlib.Path.is_file", return_value=False), \
             mock.patch(
                 "subprocess.check_output",
                 side_effect=subprocess.CalledProcessError(1, "rofi"),
             ):
            code = run_keyboard_launcher("/tmp")
        self.assertEqual(code, 0)

class BackupTests(unittest.TestCase):
    def test_create_and_restore(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = {"settings": {"theme": "dark"}, "games": [{"name": "Test"}]}
            (root / "library.json").write_text("{}")
            archive = create_backup(root, state, ["library", "settings"], keep=0)
            self.assertTrue(archive.is_file())
            self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(archive.parent.stat().st_mode), 0o700)
            with zipfile.ZipFile(archive) as package:
                manifest = json.loads(package.read("manifest.json"))
            self.assertEqual(set(manifest["items"]), {"library", "settings"})
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
            restore_backup(archive, root, force=True)
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

    def test_launch_command_expanded_variables(self):
        command = build_launch_command(
            {"id": "custom", "name": "Custom", "startup": "--dir {dir} --file {file} --stem {stem} --emu {EmulatorDir} {ImagePath}"},
            "/roms/nes/mario.nes",
            prefix=["/usr/bin/fceux"],
        )
        self.assertEqual(
            command,
            ["/usr/bin/fceux", "--dir", "/roms/nes", "--file", "mario.nes", "--stem", "mario", "--emu", "/usr/bin", "/roms/nes/mario.nes"],
        )

    def test_load_definitions(self):
        definitions = load_definitions()
        self.assertTrue(any(item.get("id") == "retroarch-nes" for item in definitions))

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

    def test_fallback_parser_loads_flat_adapter(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "demo-nes.yaml").write_text(
                "schema_version: 1\n"
                "adapter_id: demo-nes\n"
                "emulator_id: demo.emulator\n"
                "label: Demo NES\n"
                "platform: NES\n"
                "extensions:\n"
                "  - nes\n"
                "native_exe: demo\n"
                "startup_args:\n"
                "  - \"{path}\"\n"
                "recommended: true\n"
                "priority: 1\n",
                encoding="utf-8",
            )
            registry = load_registry(root)
            self.assertEqual(registry["schema_version"], 1)
            self.assertEqual(len(registry["adapters"]), 1)
            self.assertEqual(registry["adapters"][0]["adapter_id"], "demo-nes")

    def test_snes_retroarch_not_snes9x_on_nes(self):
        registry = load_registry()
        nes = next(item for item in registry["adapters"] if item["adapter_id"] == "retroarch-nes")
        snes = next(item for item in registry["adapters"] if item["adapter_id"] == "retroarch-snes")
        self.assertNotEqual(nes["startup_args"], snes["startup_args"])
        self.assertIn("fceumm", " ".join(nes["startup_args"]))
        self.assertIn("snes9x", " ".join(snes["startup_args"]))

    def test_iso_returns_multiple_candidates(self):
        candidates = candidates_for_extension("iso")
        self.assertGreater(len(candidates), 1)
        platforms = {item["platform"] for item in candidates}
        self.assertGreater(len(platforms), 1)

    def test_custom_profile_not_overwritten_by_reload(self):
        profiles = {"NES": "custom {path}"}
        updated = merge_profiles_from_definitions(profiles)
        self.assertEqual(updated["NES"], "custom {path}")

    def test_merge_profiles_from_custom_defs_dir(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "demo-nes.yaml").write_text(
                "schema_version: 1\n"
                "adapter_id: demo-nes\n"
                "emulator_id: demo.emulator\n"
                "label: Demo NES\n"
                "platform: NES\n"
                "extensions:\n"
                "  - nes\n"
                "native_exe: demo\n"
                "startup_args:\n"
                "  - \"{path}\"\n"
                "recommended: true\n"
                "priority: 1\n",
                encoding="utf-8",
            )
            with mock.patch("pkg.parity.parity_emulator_defs.shutil.which", return_value="/usr/bin/demo"):
                updated = merge_profiles_from_definitions({}, defs_dir=root)
            self.assertIn("NES", updated)

    def test_scan_folder_missing_directory(self):
        with self.assertRaises(ValueError):
            scan_folder("/no/such/folder")

    def test_candidates_for_extension_with_definitions(self):
        definitions = load_definitions()
        candidates = candidates_for_extension("nes", definitions=definitions)
        self.assertTrue(candidates)

    def test_normalize_legacy_fields(self):
        import pkg.parity.parity_emulator_defs as module
        adapter = module._normalize_adapter(
            {
                "id": "legacy-nes",
                "flatpak": "org.demo.App",
                "name": "Legacy",
                "platform": "NES",
                "extensions": "nes",
                "native": "demo",
                "startup": "-load {path}",
            }
        )
        self.assertEqual(adapter["adapter_id"], "legacy-nes")
        self.assertEqual(adapter["extensions"], ["nes"])


class WindowResolutionTests(unittest.TestCase):
    def test_kiosk_command_with_resolution(self):
        cmd = kiosk_command(["google-chrome"], "http://127.0.0.1:8787/", width=1920, height=1080)
        self.assertIn("--window-size=1920,1080", cmd)

        ff_cmd = kiosk_command(["firefox"], "http://127.0.0.1:8787/", width=1280, height=720)
        self.assertIn("--width", ff_cmd)
        self.assertIn("1280", ff_cmd)
        self.assertIn("--height", ff_cmd)
        self.assertIn("720", ff_cmd)


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
