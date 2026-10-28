#!/usr/bin/env python3
"""Platform fallbacks: what each feature does on macOS and Windows when a tool is missing.

Each test pins one branch by patching the platform flag in the module that reads it,
so the branch runs on every platform. The Game Mode refusal is checked both in
process (for coverage) and in a subprocess (for the real import-time platform flag).
"""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pkg.parity  # noqa: E402,F401  (flat-import finder for parity modules)
import importers  # noqa: E402
import saves  # noqa: E402
from pkg import platform_compat as pc  # noqa: E402
from pkg.parity import parity_emulator_defs as defs  # noqa: E402
from pkg.parity import parity_gamescope, parity_identity, parity_perf, parity_wine  # noqa: E402


class StorefrontFallbackTests(unittest.TestCase):
    def test_steam_on_macos_opens_its_link_with_the_system_opener(self):
        with mock.patch.object(importers, "IS_MACOS", True), \
             mock.patch.object(importers, "url_opener_command", return_value="/usr/bin/open"):
            binary, command = importers.steam_command(roots=[Path("/Applications/Steam.app")])
        self.assertEqual(binary, "/usr/bin/open")
        self.assertIn("steam://rungameid/{app_id}", command)

    def test_steam_on_macos_without_any_opener_is_refused(self):
        with mock.patch.object(importers, "IS_MACOS", True), \
             mock.patch.object(importers, "url_opener_command", return_value=None):
            with self.assertRaisesRegex(FileNotFoundError, "open Steam links"):
                importers.steam_command(roots=[Path("/Applications/Steam.app")])

    def test_steam_on_macos_is_found_under_the_home_library(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            steam = home / "Library" / "Application Support" / "Steam"
            (steam / "steamapps").mkdir(parents=True)
            with mock.patch.object(importers, "IS_MACOS", True), \
                 mock.patch.object(pc, "IS_MACOS", True), \
                 mock.patch.object(importers, "url_opener_command", return_value="/usr/bin/open"):
                self.assertEqual(importers.steam_roots(home), [steam])
                binary, _command = importers.steam_command(home=home)
        self.assertEqual(binary, "/usr/bin/open")

    def test_steam_on_windows_falls_back_to_the_url_opener(self):
        with mock.patch.object(importers, "IS_MACOS", False), \
             mock.patch.object(importers, "IS_WINDOWS", True), \
             mock.patch.object(importers, "steam_install_roots", return_value=[]), \
             mock.patch.object(importers.shutil, "which", return_value=None), \
             mock.patch.object(importers, "_flatpak_installed", return_value=False), \
             mock.patch.object(importers, "url_opener_command", return_value="C:\\Windows\\explorer.exe"):
            binary, command = importers.steam_command()
        self.assertEqual(binary, "C:\\Windows\\explorer.exe")
        self.assertIn("steam://rungameid/{app_id}", command)

    def test_steam_on_windows_ignores_a_git_xdg_open(self):
        def which(name):
            return r"C:\msys64\usr\bin\xdg-open.exe" if name == "xdg-open" else None

        with mock.patch.object(importers, "IS_MACOS", False), \
             mock.patch.object(importers, "IS_WINDOWS", True), \
             mock.patch.object(importers, "steam_install_roots", return_value=[]), \
             mock.patch.object(importers.shutil, "which", side_effect=which), \
             mock.patch.object(importers, "_flatpak_installed", return_value=False), \
             mock.patch.object(importers, "url_opener_command", return_value="C:\\Windows\\explorer.exe"):
            binary, _command = importers.steam_command()
        self.assertEqual(binary, "C:\\Windows\\explorer.exe")

    def test_heroic_has_no_store_on_macos(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".config" / "heroic").mkdir(parents=True)
            with mock.patch.object(importers, "IS_MACOS", True):
                self.assertEqual(importers.heroic_bases(home), [])

    def test_heroic_import_needs_an_opener_on_linux(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(importers, "IS_MACOS", False), \
                 mock.patch.object(importers, "IS_WINDOWS", False), \
                 mock.patch.object(importers, "url_opener_command", return_value=None):
                with self.assertRaisesRegex(FileNotFoundError, "open Heroic links"):
                    importers.import_heroic(Path(directory))


class SaveFallbackTests(unittest.TestCase):
    def test_a_macos_steam_cloud_folder_is_skipped_when_steam_is_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            found = saves._discover_save_paths_darwin({"name": "Game", "steam_app_id": "42"}, Path(directory))
        self.assertEqual(found, [])


class EmulatorFallbackTests(unittest.TestCase):
    def test_a_windows_definition_uses_its_windows_executable(self):
        adapter = {"native_exe": "retroarch", "native_exe_windows": "retroarch.exe", "native_exe_macos": ""}
        with mock.patch.object(defs, "IS_MACOS", False), mock.patch.object(defs, "IS_WINDOWS", True):
            self.assertEqual(defs.adapter_executable(adapter), "retroarch.exe")

    def test_a_definition_without_a_macos_build_keeps_its_posix_name(self):
        adapter = {"native_exe": "mame", "native_exe_windows": "mame.exe"}
        with mock.patch.object(defs, "IS_MACOS", True), mock.patch.object(defs, "IS_WINDOWS", False):
            self.assertEqual(defs.adapter_executable(adapter), "mame")

    def test_a_definition_with_an_empty_macos_build_reports_no_executable(self):
        adapter = {"native_exe": "azahar", "native_exe_windows": "azahar.exe", "native_exe_macos": ""}
        with mock.patch.object(defs, "IS_MACOS", True), mock.patch.object(defs, "IS_WINDOWS", False):
            self.assertEqual(defs.adapter_executable(adapter), "")

    def test_an_unavailable_macos_build_is_not_detected_through_its_linux_name(self):
        adapter = {
            "native_exe": "azahar",
            "native_exe_macos": "",
            "flatpak_app_id": None,
            "executable_patterns": ["azahar"],
        }
        with mock.patch.object(defs, "IS_MACOS", True), mock.patch.object(defs, "IS_WINDOWS", False):
            prefix = defs.detect_adapter_prefix(adapter, which=lambda name: f"/usr/bin/{name}")
        self.assertEqual(prefix, [])

    def test_a_bundled_definition_without_a_macos_build_is_reported_missing(self):
        root = Path(defs.__file__).resolve().parent.parent.parent / "emulator_defs"
        with mock.patch.object(defs, "IS_MACOS", True):
            adapters = {adapter["adapter_id"]: adapter for adapter in defs.load_adapters(root)}
        adapter = adapters["azahar-3ds"]
        self.assertEqual(adapter["native_exe_macos"], "")
        with mock.patch.object(defs, "IS_MACOS", True), mock.patch.object(defs, "IS_WINDOWS", False):
            self.assertEqual(defs.adapter_executable(adapter), "")
            self.assertEqual(defs.detect_adapter_prefix(adapter, which=lambda name: f"/usr/bin/{name}"), [])

    def test_the_emulator_map_does_not_fall_back_to_the_linux_name_on_macos(self):
        adapter = {
            "emulator_id": "emu",
            "label": "Emu",
            "platform": "A",
            "native_exe": "emu-bin",
            "native_exe_macos": "",
            "startup_args": [],
        }
        with mock.patch.object(defs, "IS_MACOS", True), mock.patch.object(defs, "IS_WINDOWS", False):
            emulators = defs.build_emulators_dict([adapter])
        self.assertEqual(emulators["emu"]["native"], "")

    def test_normalization_tells_an_empty_macos_value_from_a_missing_one(self):
        base = {"adapter_id": "demo", "emulator_id": "emu", "platform": "Demo", "native_exe": "emu"}
        with mock.patch.object(defs, "IS_MACOS", True):
            declared = defs._normalize_adapter({**base, "native_exe_macos": ""})
            absent = defs._normalize_adapter(dict(base))
        self.assertEqual(declared["native_exe_macos"], "")
        self.assertIsNone(absent["native_exe_macos"])

    def test_the_emulator_map_takes_the_first_usable_executable(self):
        # The first definition of an emulator has no executable; a later one does.
        adapters = [
            {"emulator_id": "emu", "label": "Emu (empty)", "platform": "A", "native_exe": ""},
            {"emulator_id": "emu", "label": "Emu (usable)", "platform": "B", "native_exe": "emu-bin",
             "startup_args": []},
        ]
        emulators = defs.build_emulators_dict(adapters)
        self.assertEqual(emulators["emu"]["native"], "emu-bin")
        self.assertEqual(emulators["emu"]["name"], "Emu")


class HostFallbackTests(unittest.TestCase):
    def test_identity_falls_back_to_the_lexical_path_when_it_cannot_resolve(self):
        raw = "/tmp/Games/Demo/"
        with mock.patch.object(Path, "resolve", side_effect=OSError("denied")):
            identity = parity_identity.normalize_path_identity(raw)
        self.assertEqual(identity, pc.path_identity_fold(os.path.normpath(raw)).rstrip(os.sep))

    def test_the_handheld_gate_follows_battery_presence(self):
        with mock.patch.object(parity_perf, "battery_status", return_value={"present": True, "percent": 80, "charging": False}):
            self.assertTrue(parity_perf._has_battery())
        with mock.patch.object(parity_perf, "battery_status", return_value={"present": False, "percent": 0, "charging": False}):
            self.assertFalse(parity_perf._has_battery())

    def test_a_runner_on_the_path_is_listed_with_its_source(self):
        def which(name):
            return "/usr/bin/wine" if name == "wine" else None

        with mock.patch.object(parity_wine, "IS_LINUX", True), \
             mock.patch.object(parity_wine.shutil, "which", side_effect=which):
            versions = parity_wine.list_proton_versions(search_roots=[])
        self.assertIn({"name": "/usr/bin/wine", "path": "/usr/bin/wine", "source": "path"}, versions)

    def test_gamescope_says_so_when_no_window_can_open(self):
        stderr = io.StringIO()
        with mock.patch.object(parity_gamescope, "external_open_disabled", return_value=True), \
             mock.patch.object(parity_gamescope, "IS_MACOS", False), \
             mock.patch.object(parity_gamescope, "IS_WINDOWS", False), \
             contextlib.redirect_stderr(stderr):
            result = parity_gamescope.open_ui("http://127.0.0.1:1/", which=lambda name: None)
        self.assertEqual(result["mode"], "webbrowser")
        self.assertIn("open this URL manually", stderr.getvalue())

    def test_gamescope_walks_macos_children_through_the_process_table(self):
        children = {1: [2, 3], 2: [4], 3: [], 4: []}
        with mock.patch.object(parity_gamescope, "_platform_child_pids", side_effect=lambda pid: children.get(pid, [])):
            self.assertEqual(parity_gamescope._macos_child_pids(1), [1, 2, 3, 4])


class GameModeTests(unittest.TestCase):
    def test_game_mode_is_refused_off_linux_in_process(self):
        import web_app

        stderr = io.StringIO()
        with mock.patch.object(web_app, "IS_LINUX", False), \
             mock.patch.object(web_app, "bootstrap_env"), \
             mock.patch.object(web_app, "configure_logging"), \
             mock.patch.object(web_app, "ensure_macos_tool_path", side_effect=lambda value: value), \
             mock.patch.object(sys, "argv", ["web_app.py", "--game-mode"]), \
             contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                web_app.main()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("only available on Linux", stderr.getvalue())

    def test_game_mode_is_refused_off_linux_with_exit_status_two(self):
        # web_app reads the platform flag when it is imported, so set it first.
        script = (
            "import sys\n"
            "sys.argv = ['web_app.py', '--game-mode']\n"
            "import pkg.platform_compat as pc\n"
            "pc.IS_LINUX = False\n"
            "import web_app\n"
            "web_app.main()\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, OPENBOX_DATA_DIR=directory)
            result = subprocess.run(
                [sys.executable, "-B", "-c", script],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=120, check=False,
            )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("only available on Linux", result.stderr)


if __name__ == "__main__":
    unittest.main()
