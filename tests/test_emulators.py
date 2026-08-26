#!/usr/bin/env python3
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emulators import (  # noqa: E402
    EMULATORS,
    emulator_status,
    install_all_emulators,
    install_emulator,
    launch_emulator,
    recommendations_for_platform,
    update_all_emulators,
    update_emulator,
)
from handlers.emulators import EmulatorsHandlers  # noqa: E402
from parity_emulator_defs import (  # noqa: E402
    build_launch_command,
    find_adapter,
    load_definitions,
    load_registry,
    platform_for_extension,
    resolve_launch,
    scan_folder,
)
from openbox import build_launch  # noqa: E402
from pkg.state.launch import _start_launch_command  # noqa: E402
from webapp_state import INSTALLS, PROCESS_LOCK  # noqa: E402


class Result:
    returncode = 0
    stdout = ""
    stderr = ""


class DummyEmulatorHandler(EmulatorsHandlers):
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


class TestEmulatorCli(unittest.TestCase):
    def test_emulator_self_test(self):
        calls = []

        def run(args, **_):
            calls.append(args)
            return Result()

        statuses = emulator_status(
            run=run,
            which=lambda name: f"/usr/bin/{name}" if name in {"flatpak", "dolphin-emu"} else None,
        )
        dolphin = next(item for item in statuses if item["name"] == "Dolphin")
        self.assertTrue(dolphin["mode"] == "native" and dolphin["profiles"]["Wii"].startswith("/usr/bin/dolphin-emu"))
        profiles = install_emulator(
            "org.ppsspp.PPSSPP",
            run=run,
            which=lambda name: "/usr/bin/flatpak" if name == "flatpak" else None,
        )
        self.assertTrue(profiles["PSP"].startswith("/usr/bin/flatpak run org.ppsspp.PPSSPP"))
        self.assertEqual(calls[-1][-1], "org.ppsspp.PPSSPP")

    def test_registry_emulators_generated(self):
        self.assertIn("org.libretro.RetroArch", EMULATORS)
        self.assertIn("NES", EMULATORS["org.libretro.RetroArch"]["profiles"])

    def test_recommendations_for_platform(self):
        recs = recommendations_for_platform("NES")
        self.assertTrue(any(item["app_id"] == "org.libretro.RetroArch" for item in recs))

    def test_launch_emulator_native(self):
        with mock.patch("emulators.subprocess.Popen") as popen:
            result = launch_emulator(
                "org.ppsspp.PPSSPP",
                which=lambda name: "/usr/bin/ppsspp" if name == "ppsspp" else None,
            )
        self.assertEqual(result["mode"], "native")
        popen.assert_called_once()

    def test_launch_emulator_unknown(self):
        with self.assertRaises(ValueError):
            launch_emulator("unknown.app")

    def test_launch_emulator_flatpak(self):
        with mock.patch("emulators.subprocess.Popen") as popen, \
             mock.patch("emulators.subprocess.run", return_value=Result()):
            result = launch_emulator(
                "org.ppsspp.PPSSPP",
                which=lambda name: "/usr/bin/flatpak" if name == "flatpak" else None,
            )
        self.assertEqual(result["mode"], "flatpak")
        popen.assert_called_once()

    def test_install_emulator_success(self):
        calls = []

        def run(args, **_kwargs):
            calls.append(args)
            result = Result()
            if args[:3] == ["/usr/bin/flatpak", "remotes", "--user"]:
                result.stdout = "flathub"
            return result

        profiles = install_emulator(
            "org.ppsspp.PPSSPP",
            run=run,
            which=lambda name: "/usr/bin/flatpak" if name == "flatpak" else None,
        )
        self.assertIn("PSP", profiles)

    def test_install_all_collects_errors(self):
        statuses = [
            {"app_id": app_id, "name": EMULATORS[app_id]["name"], "installed": app_id == "org.ppsspp.PPSSPP"}
            for app_id in EMULATORS
        ]
        with mock.patch("emulators.emulator_status", return_value=statuses), \
             mock.patch("emulators.install_emulator", side_effect=ValueError("fail")):
            result = install_all_emulators(which=lambda name: "/usr/bin/flatpak" if name == "flatpak" else None)
        self.assertTrue(result["errors"])

    def test_update_all_collects_errors(self):
        statuses = [
            {
                "app_id": app_id,
                "name": EMULATORS[app_id]["name"],
                "installed": app_id == "org.ppsspp.PPSSPP",
                "mode": "flatpak" if app_id == "org.ppsspp.PPSSPP" else "",
            }
            for app_id in EMULATORS
        ]
        with mock.patch("emulators.emulator_status", return_value=statuses), \
             mock.patch("emulators.update_emulator", side_effect=ValueError("fail")):
            result = update_all_emulators(which=lambda name: "/usr/bin/flatpak" if name == "flatpak" else None)
        self.assertTrue(result["errors"])

    def test_update_emulator(self):
        calls = []

        def run(args, **_kwargs):
            calls.append(args)
            return Result()

        updated = update_emulator(
            "org.ppsspp.PPSSPP",
            run=run,
            which=lambda name: "/usr/bin/flatpak" if name == "flatpak" else None,
        )
        self.assertEqual(updated["updated"], "PPSSPP")
        self.assertIn("update", calls[0])

    def test_update_emulator_unknown(self):
        with self.assertRaises(ValueError):
            update_emulator("missing.app")

    def test_install_emulator_install_failure(self):
        failed = Result()
        failed.returncode = 1
        failed.stderr = "install failed"
        with self.assertRaises(RuntimeError):
            install_emulator(
                "org.ppsspp.PPSSPP",
                run=lambda *_args, **_kwargs: failed,
                which=lambda name: "/usr/bin/flatpak" if name == "flatpak" else None,
            )

    def test_install_all_records_success(self):
        statuses = [
            {"app_id": app_id, "name": EMULATORS[app_id]["name"], "installed": app_id != "org.ppsspp.PPSSPP"}
            for app_id in EMULATORS
        ]
        with mock.patch("emulators.emulator_status", return_value=statuses), \
             mock.patch("emulators.install_emulator", return_value={"PSP": "cmd"}):
            result = install_all_emulators(which=lambda name: "/usr/bin/flatpak" if name == "flatpak" else None)
        self.assertIn("PPSSPP", result["installed"])

    def test_install_all_and_update_all(self):
        def run(args, **_kwargs):
            return Result()

        def which(name):
            return "/usr/bin/flatpak" if name == "flatpak" else None
        with mock.patch("emulators.install_emulator", return_value={"PSP": "cmd"}):
            installed = install_all_emulators(run=run, which=which)
        self.assertIn("installed", installed)
        with mock.patch("emulators.update_emulator", return_value={"updated": "PPSSPP"}):
            updated = update_all_emulators(run=run, which=which)
        self.assertIn("updated", updated)

    def test_install_emulator_errors(self):
        with self.assertRaises(ValueError):
            install_emulator("missing.app")
        with self.assertRaises(FileNotFoundError):
            install_emulator("org.ppsspp.PPSSPP", which=lambda _name: None)
        bad_remote = Result()
        bad_remote.returncode = 1
        bad_remote.stderr = "remote failed"
        with self.assertRaises(RuntimeError):
            install_emulator(
                "org.ppsspp.PPSSPP",
                run=lambda *_args, **_kwargs: bad_remote,
                which=lambda name: "/usr/bin/flatpak" if name == "flatpak" else None,
            )


class TestRegistryHelpers(unittest.TestCase):
    def test_find_adapter_by_id(self):
        adapter = find_adapter("retroarch-nes")
        self.assertEqual(adapter["platform"], "NES")

    def test_find_adapter_by_emulator_id(self):
        adapter = find_adapter("", "org.ppsspp.PPSSPP")
        self.assertEqual(adapter["emulator_id"], "org.ppsspp.PPSSPP")

    def test_scan_folder_filters_emulator_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "demo.iso").write_bytes(b"iso")
            games = scan_folder(root, emulator_id="org.duckstation.DuckStation")
            self.assertEqual(len(games), 1)
            self.assertEqual(games[0]["platform"], "PlayStation")

    def test_resolve_launch_precedence_metadata(self):
        with tempfile.NamedTemporaryFile(suffix=".nes", delete=False) as rom:
            rom.write(b"nes")
            path = rom.name
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        resolved = resolve_launch(
            {"name": "Game", "path": path, "platform": "NES", "launch": "custom {path}"},
            {"NES": "profile {path}"},
        )
        self.assertEqual(resolved["precedence"], "game_launch")

    def test_build_launch_command_missing_emulator(self):
        with self.assertRaises(FileNotFoundError):
            build_launch_command({"id": "x", "name": "X", "startup": "{path}"}, "/tmp/a.nes", prefix=[])

    def test_resolve_launch_profile_and_registry(self):
        with tempfile.NamedTemporaryFile(suffix=".nes", delete=False) as rom:
            rom.write(b"nes")
            path = rom.name
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        profile = resolve_launch({"name": "Game", "path": path, "platform": "NES"}, {"NES": "profile {path}"})
        self.assertEqual(profile["precedence"], "platform_profile")
        with mock.patch("pkg.parity.parity_emulator_defs.shutil.which", return_value="/usr/bin/retroarch"):
            detected = resolve_launch({"name": "Game", "path": path, "platform": "NES"}, {})
        self.assertEqual(detected["precedence"], "registry_adapter")

    def test_resolve_launch_shell_script(self):
        sh = tempfile.NamedTemporaryFile(suffix=".sh", delete=False)
        sh.write(b"#!/bin/sh\n")
        sh.close()
        os.chmod(sh.name, 0o755)
        self.addCleanup(lambda: os.path.exists(sh.name) and os.unlink(sh.name))
        resolved = resolve_launch({"name": "Game", "path": sh.name, "platform": "Unknown"}, {})
        self.assertEqual(resolved["args"][0], "bash")

    def test_fallback_parser_without_pyyaml(self):
        import pkg.parity.parity_emulator_defs as module
        with mock.patch.object(module, "yaml", None):
            data = module._parse_yaml(
                "adapter_id: demo\nlabel: Demo\nextensions:\n  - nes\nplatform: NES\n"
            )
        self.assertEqual(data["adapter_id"], "demo")
        self.assertEqual(data["extensions"], ["nes"])

    def test_registry_self_test_main(self):
        import pkg.parity.parity_emulator_defs as module
        module.main()

    def test_resolve_launch_errors(self):
        with self.assertRaises(ValueError):
            resolve_launch({"name": "Game", "path": ""}, {})
        with tempfile.NamedTemporaryFile(suffix=".nes", delete=False) as rom:
            path = rom.name
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        os.unlink(path)
        with self.assertRaises(FileNotFoundError):
            resolve_launch({"name": "Game", "path": path}, {})

    def test_save_scan_config_validation(self):
        from parity_emulator_defs import list_scan_configs, save_scan_config

        with self.assertRaises(ValueError):
            save_scan_config({}, "", "emu")
        state = {"settings": {"emulator_scan_configs": "bad"}}
        self.assertEqual(list_scan_configs(state), [])

    def test_load_skips_invalid_adapter_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "bad.yaml").write_text("platform: only\n", encoding="utf-8")
            (root / "good.yaml").write_text(
                "schema_version: 1\nadapter_id: ok\nemulator_id: emu\nlabel: OK\nplatform: NES\nextensions:\n  - nes\nstartup_args:\n  - \"{path}\"\n",
                encoding="utf-8",
            )
            registry = load_registry(root)
            self.assertEqual(len(registry["adapters"]), 1)

    def test_fallback_parser_scalar_and_comments(self):
        import pkg.parity.parity_emulator_defs as module
        with mock.patch.object(module, "yaml", None):
            data = module._parse_yaml("# comment\nname: Demo\nvalue: plain\nlist:\n  - one\n  - two\n")
        self.assertEqual(data["name"], "Demo")
        self.assertEqual(data["list"], ["one", "two"])

    def test_find_adapter_ambiguous_emulator_id(self):
        self.assertIsNone(find_adapter("", "org.libretro.RetroArch"))

    def test_platform_for_extension_with_definitions(self):
        definitions = load_definitions()
        platform, definition = platform_for_extension("nes", definitions=definitions)
        self.assertEqual(platform, "NES")
        self.assertIsNotNone(definition)

    def test_resolve_launch_profile_without_path_token(self):
        with tempfile.NamedTemporaryFile(suffix=".nes", delete=False) as rom:
            rom.write(b"nes")
            path = rom.name
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        resolved = resolve_launch({"name": "Game", "path": path, "platform": "NES"}, {"NES": "profile-cmd"})
        self.assertEqual(resolved["precedence"], "platform_profile")
        self.assertEqual(resolved["args"][-1], path)

    def test_resolve_launch_missing_adapter_falls_back(self):
        with tempfile.NamedTemporaryFile(suffix=".nes", delete=False) as rom:
            rom.write(b"nes")
            path = rom.name
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        with mock.patch("pkg.parity.parity_emulator_defs.shutil.which", return_value=None):
            resolved = resolve_launch(
                {"name": "Game", "path": path, "platform": "NES", "emulator_adapter_id": "retroarch-nes"},
                {},
            )
        self.assertEqual(resolved["precedence"], "direct_exe")

    def test_build_launch_command_legacy_startup(self):
        command = build_launch_command(
            {"id": "legacy", "name": "Legacy", "startup": "-batch {path}"},
            "/tmp/game.bin",
            prefix=["duckstation-qt"],
        )
        self.assertEqual(command, ["duckstation-qt", "-batch", "/tmp/game.bin"])

    def test_save_scan_config_success(self):
        from parity_emulator_defs import save_scan_config

        state = {"settings": {}}
        entry = save_scan_config(state, "/roms", "org.DolphinEmu.dolphin-emu", auto_update=True)
        self.assertEqual(entry["folder"], "/roms")
        self.assertTrue(state["settings"]["emulator_scan_configs"])


class TestEmulatorsHandlers(unittest.TestCase):
    def setUp(self):
        with PROCESS_LOCK:
            INSTALLS.clear()

    def tearDown(self):
        with PROCESS_LOCK:
            INSTALLS.clear()

    def test_get_emulators(self):
        h = DummyEmulatorHandler()
        fake_emulators = [{"app_id": "app1", "name": "Emu1"}]
        with mock.patch("handlers.emulators.emulator_status", return_value=fake_emulators):
            h._api_get_api_emulators(mock.Mock())
        self.assertEqual(h.status, 200)
        self.assertEqual(len(h.payload["emulators"]), 1)
        self.assertEqual(h.payload["emulators"][0]["app_id"], "app1")
        self.assertEqual(h.payload["emulators"][0]["job"], {})
        self.assertEqual(h.payload["install_all"], {})
        self.assertEqual(h.payload["update_all"], {})

    def test_get_emulators_with_active_job(self):
        h = DummyEmulatorHandler()
        fake_emulators = [{"app_id": "app1", "name": "Emu1"}]
        with PROCESS_LOCK:
            INSTALLS["app1"] = {"state": "installing"}
        with mock.patch("handlers.emulators.emulator_status", return_value=fake_emulators):
            h._api_get_api_emulators(mock.Mock())
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload["emulators"][0]["job"], {"state": "installing"})

    def test_get_emulators_recommend(self):
        h = DummyEmulatorHandler()
        parsed = mock.Mock(query="platform=Wii")
        with mock.patch("handlers.emulators.recommendations_for_platform", return_value=[{"name": "Dolphin"}]):
            h._api_get_api_emulators_recommend(parsed)
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload, {"recommendations": [{"name": "Dolphin"}]})

    def test_get_emulators_dependencies(self):
        h = DummyEmulatorHandler()
        parsed = mock.Mock(query="name=pcsx2")
        with mock.patch("handlers.emulators.detect_dependencies", return_value={"dependencies": ["libaio"]}):
            h._api_get_api_emulators_dependencies(parsed)
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload, {"dependencies": ["libaio"]})

    def test_get_emulators_definitions(self):
        h = DummyEmulatorHandler()
        with mock.patch("handlers.emulators.load_definitions", return_value=[{"id": "def1"}]):
            h._api_get_api_emulators_definitions(mock.Mock())
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload, {"definitions": [{"id": "def1"}]})

    def test_get_emulators_scan_configs(self):
        h = DummyEmulatorHandler()
        with mock.patch("handlers.emulators.load_state_view", return_value={}), \
             mock.patch("handlers.emulators.list_scan_configs", return_value=[{"folder": "/roms"}]):
            h._api_get_api_emulators_scan_configs(mock.Mock())
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload, {"configs": [{"folder": "/roms"}]})

    def test_post_install_validation(self):
        h = DummyEmulatorHandler()
        with self.assertRaises(ValueError):
            h._api_post_api_emulators_install({})
        with self.assertRaises(ValueError):
            h._api_post_api_emulators_install({"app_id": "   "})

    def test_post_install_already_running(self):
        h = DummyEmulatorHandler()
        with PROCESS_LOCK:
            INSTALLS["app1"] = {"state": "installing"}
        h._api_post_api_emulators_install({"app_id": "app1"})
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload, {"state": "installing"})

    def test_post_install_success_worker(self):
        h = DummyEmulatorHandler()
        with mock.patch("handlers.emulators.JOB_MANAGER.submit", side_effect=lambda name, fn: fn()), \
             mock.patch("handlers.emulators.install_emulator", return_value={"Wii": "dolphin"}), \
             mock.patch("handlers.emulators.transact_state", side_effect=lambda fn: fn({"profiles": {}})):
            h._api_post_api_emulators_install({"app_id": "app_new"})
        self.assertEqual(h.status, 202)
        self.assertEqual(h.payload, {"state": "installing"})
        with PROCESS_LOCK:
            self.assertEqual(INSTALLS["app_new"]["state"], "done")
            self.assertEqual(INSTALLS["app_new"]["profiles"], {"Wii": "dolphin"})

    def test_post_install_error_worker(self):
        h = DummyEmulatorHandler()
        with mock.patch("handlers.emulators.JOB_MANAGER.submit", side_effect=lambda name, fn: fn()), \
             mock.patch("handlers.emulators.install_emulator", side_effect=RuntimeError("Install failed")):
            h._api_post_api_emulators_install({"app_id": "app_err"})
        self.assertEqual(h.status, 202)
        with PROCESS_LOCK:
            self.assertEqual(INSTALLS["app_err"]["state"], "error")
            self.assertIn("Install failed", INSTALLS["app_err"]["error"])

    def test_post_install_all_already_running(self):
        h = DummyEmulatorHandler()
        with PROCESS_LOCK:
            INSTALLS["__all__"] = {"state": "installing"}
        h._api_post_api_emulators_install_all({})
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload, {"state": "installing"})

    def test_post_install_all_success_worker(self):
        h = DummyEmulatorHandler()
        with mock.patch("handlers.emulators.JOB_MANAGER.submit", side_effect=lambda name, fn: fn()), \
             mock.patch("handlers.emulators.install_all_emulators", return_value={"installed": ["emu1"]}):
            h._api_post_api_emulators_install_all({})
        self.assertEqual(h.status, 202)
        with PROCESS_LOCK:
            self.assertEqual(INSTALLS["__all__"]["state"], "done")
            self.assertEqual(INSTALLS["__all__"]["installed"], ["emu1"])

    def test_post_install_all_error_worker(self):
        h = DummyEmulatorHandler()
        with mock.patch("handlers.emulators.JOB_MANAGER.submit", side_effect=lambda name, fn: fn()), \
             mock.patch("handlers.emulators.install_all_emulators", side_effect=OSError("network error")):
            h._api_post_api_emulators_install_all({})
        self.assertEqual(h.status, 202)
        with PROCESS_LOCK:
            self.assertEqual(INSTALLS["__all__"]["state"], "error")
            self.assertIn("network error", INSTALLS["__all__"]["error"])

    def test_post_update_validation(self):
        h = DummyEmulatorHandler()
        with self.assertRaises(ValueError):
            h._api_post_api_emulators_update({})

    def test_post_update_already_running(self):
        h = DummyEmulatorHandler()
        with PROCESS_LOCK:
            INSTALLS["update:app1"] = {"state": "updating"}
        h._api_post_api_emulators_update({"app_id": "app1"})
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload, {"state": "updating"})

    def test_post_update_success_worker(self):
        h = DummyEmulatorHandler()
        with mock.patch("handlers.emulators.JOB_MANAGER.submit", side_effect=lambda name, fn: fn()), \
             mock.patch("handlers.emulators.update_emulator", return_value={"updated": True}):
            h._api_post_api_emulators_update({"app_id": "app1"})
        self.assertEqual(h.status, 202)
        with PROCESS_LOCK:
            self.assertEqual(INSTALLS["update:app1"]["state"], "done")
            self.assertTrue(INSTALLS["update:app1"]["updated"])

    def test_post_update_error_worker(self):
        h = DummyEmulatorHandler()
        with mock.patch("handlers.emulators.JOB_MANAGER.submit", side_effect=lambda name, fn: fn()), \
             mock.patch("handlers.emulators.update_emulator", side_effect=ValueError("update failed")):
            h._api_post_api_emulators_update({"app_id": "app1"})
        self.assertEqual(h.status, 202)
        with PROCESS_LOCK:
            self.assertEqual(INSTALLS["update:app1"]["state"], "error")

    def test_post_update_all_already_running(self):
        h = DummyEmulatorHandler()
        with PROCESS_LOCK:
            INSTALLS["__update_all__"] = {"state": "updating"}
        h._api_post_api_emulators_update_all({})
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload, {"state": "updating"})

    def test_post_update_all_success_worker(self):
        h = DummyEmulatorHandler()
        with mock.patch("handlers.emulators.JOB_MANAGER.submit", side_effect=lambda name, fn: fn()), \
             mock.patch("handlers.emulators.update_all_emulators", return_value={"updated": ["app1"]}):
            h._api_post_api_emulators_update_all({})
        self.assertEqual(h.status, 202)
        with PROCESS_LOCK:
            self.assertEqual(INSTALLS["__update_all__"]["state"], "done")
            self.assertEqual(INSTALLS["__update_all__"]["updated"], ["app1"])

    def test_post_update_all_error_worker(self):
        h = DummyEmulatorHandler()
        with mock.patch("handlers.emulators.JOB_MANAGER.submit", side_effect=lambda name, fn: fn()), \
             mock.patch("handlers.emulators.update_all_emulators", side_effect=ValueError("fail")):
            h._api_post_api_emulators_update_all({})
        self.assertEqual(h.status, 202)
        with PROCESS_LOCK:
            self.assertEqual(INSTALLS["__update_all__"]["state"], "error")

    def test_post_open_emulator(self):
        h = DummyEmulatorHandler()
        with self.assertRaises(ValueError):
            h._api_post_api_emulators_open({})
        with mock.patch("handlers.emulators.launch_emulator", return_value={"launched": True}):
            h._api_post_api_emulators_open({"app_id": "dolphin"})
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload, {"launched": True})

    def test_open_emulator_bad_request(self):
        h = DummyEmulatorHandler()
        from api_errors import BadRequest
        with mock.patch("handlers.emulators.launch_emulator", side_effect=ValueError("bad")):
            with self.assertRaises(BadRequest):
                h._api_post_api_emulators_open({"app_id": "missing"})

    def test_post_scan_emulator_folder(self):
        h = DummyEmulatorHandler()
        with mock.patch("handlers.emulators.scan_emulator_folder", return_value=[{"path": "/tmp/game.iso"}]), \
             mock.patch("handlers.emulators.merge_imported_games", return_value=(1, 1)), \
             mock.patch("handlers.emulators.clear_file_probe_cache"):
            h._api_post_api_emulators_scan({"folder": "/tmp/roms"})
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload, {"added": 1, "found": 1})

    def test_post_save_scan_config(self):
        h = DummyEmulatorHandler()
        fake_entry = {"folder": "/tmp/roms", "emulator_id": "dolphin", "auto_update": True}
        with mock.patch("handlers.emulators.transact_state", return_value=(None, fake_entry)):
            h._api_post_api_emulators_scan_configs({"folder": "/tmp/roms", "emulator_id": "dolphin", "auto_update": True})
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload, {"config": fake_entry})

    def test_get_emulators_registry(self):
        h = DummyEmulatorHandler()
        h._api_get_api_v2_emulators_registry(mock.Mock())
        self.assertEqual(h.status, 200)
        self.assertIn("schema_version", h.payload)
        self.assertIn("adapters", h.payload)
        self.assertTrue(h.payload["adapters"])
        required = (
            "adapter_id", "emulator_id", "label", "platform", "extensions",
            "native_exe", "flatpak_app_id", "startup_args", "recommended", "priority",
        )
        for key in required:
            self.assertIn(key, h.payload["adapters"][0])
        flatpak = next(item for item in h.payload["adapters"] if item.get("flatpak_app_id"))
        self.assertIsNotNone(flatpak["flatpak_app_id"])

    def test_post_scan_emulator_folder_passes_emulator_id(self):
        h = DummyEmulatorHandler()
        with mock.patch("handlers.emulators.scan_emulator_folder", return_value=[]) as scan, \
             mock.patch("handlers.emulators.merge_imported_games", return_value=(0, 0)), \
             mock.patch("handlers.emulators.clear_file_probe_cache"):
            h._api_post_api_emulators_scan({"folder": "/tmp/roms", "emulator_id": "org.DolphinEmu.dolphin-emu"})
        scan.assert_called_once_with("/tmp/roms", emulator_id="org.DolphinEmu.dolphin-emu")

    def test_post_scan_requires_folder(self):
        h = DummyEmulatorHandler()
        from api_errors import BadRequest
        with self.assertRaises(BadRequest):
            h._api_post_api_emulators_scan({})

    def test_save_scan_config_requires_fields(self):
        h = DummyEmulatorHandler()
        from api_errors import BadRequest
        with self.assertRaises(BadRequest):
            h._api_post_api_emulators_scan_configs({"folder": "/tmp"})
        with self.assertRaises(BadRequest):
            h.save_emulator_scan_config({"folder": "", "emulator_id": "x"})


class TestBuildLaunchPrecedence(unittest.TestCase):
    def setUp(self):
        self.rom = tempfile.NamedTemporaryFile(suffix=".nes", delete=False)
        self.rom.write(b"nes")
        self.rom.close()
        self.addCleanup(lambda: os.path.exists(self.rom.name) and os.unlink(self.rom.name))

    def test_game_launch_wins_over_adapter(self):
        game = {
            "name": "Game",
            "path": self.rom.name,
            "platform": "NES",
            "launch": "custom-launch {path}",
            "emulator_adapter_id": "retroarch-nes",
        }
        profiles = {"NES": "profile-launch {path}"}
        args, _cwd = build_launch(game, profiles)
        self.assertEqual(args[0], "custom-launch")

    def test_adapter_wins_over_profile(self):
        game = {
            "name": "Game",
            "path": self.rom.name,
            "platform": "NES",
            "launch": "",
            "emulator_adapter_id": "retroarch-nes",
        }
        profiles = {"NES": "profile-launch {path}"}
        with mock.patch("shutil.which", return_value="/usr/bin/retroarch"):
            args, _cwd = build_launch(game, profiles)
        self.assertEqual(args[0], "/usr/bin/retroarch")
        self.assertIn("-L", args)

    def test_profile_used_when_no_launch_or_adapter(self):
        game = {"name": "Game", "path": self.rom.name, "platform": "NES", "launch": ""}
        profiles = {"NES": "profile-launch {path}"}
        args, _cwd = build_launch(game, profiles)
        self.assertEqual(args[0], "profile-launch")

    def test_registry_adapter_when_no_profile(self):
        game = {"name": "Game", "path": self.rom.name, "platform": "NES", "launch": ""}
        with mock.patch("shutil.which", return_value="/usr/bin/retroarch"):
            args, _cwd = build_launch(game, {})
        self.assertEqual(args[0], "/usr/bin/retroarch")

    def test_direct_exe_fallback(self):
        exe = tempfile.NamedTemporaryFile(suffix=".bin", delete=False)
        exe.write(b"#!/bin/sh\nexit 0\n")
        exe.close()
        os.chmod(exe.name, 0o755)
        self.addCleanup(lambda: os.path.exists(exe.name) and os.unlink(exe.name))
        game = {"name": "Game", "path": exe.name, "platform": "Unknown", "launch": ""}
        args, _cwd = build_launch(game, {})
        self.assertEqual(args, [exe.name])

    def test_build_launch_extract_archive(self):
        archive = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        archive.write(b"data")
        archive.close()
        extracted = tempfile.NamedTemporaryFile(suffix=".bin", delete=False)
        extracted.write(b"rom")
        extracted.close()
        self.addCleanup(lambda: os.path.exists(archive.name) and os.unlink(archive.name))
        self.addCleanup(lambda: os.path.exists(extracted.name) and os.unlink(extracted.name))
        with mock.patch("openbox.extract_game", return_value=extracted.name) as extract:
            args, _cwd = build_launch(
                {"name": "Game", "path": archive.name, "platform": "NES", "extract_archive": True, "launch": "run {path}"},
                {},
            )
        extract.assert_called_once()
        self.assertEqual(args[0], "run")
        self.assertEqual(args[1], extracted.name)

    def test_build_launch_rejects_empty_path(self):
        with self.assertRaises(ValueError):
            build_launch({"name": "Game", "path": ""}, {})

    def test_build_launch_rejects_missing_path(self):
        with self.assertRaises(FileNotFoundError):
            build_launch({"name": "Game", "path": "/no/such/file.nes"}, {})


class TestStartLaunchCommandAdapter(unittest.TestCase):
    def test_adapter_only_does_not_raise(self):
        rom = tempfile.NamedTemporaryFile(suffix=".nes", delete=False)
        rom.write(b"nes")
        rom.close()
        self.addCleanup(lambda: os.path.exists(rom.name) and os.unlink(rom.name))
        game = {
            "name": "Game",
            "path": rom.name,
            "platform": "NES",
            "emulator_adapter_id": "retroarch-nes",
        }
        with mock.patch("shutil.which", return_value="/usr/bin/retroarch"):
            args, cwd = _start_launch_command(game, {"NES": "ignored {path}"})
        self.assertEqual(args[0], "/usr/bin/retroarch")
        self.assertTrue(cwd)

    def test_missing_launch_command_raises(self):
        rom = tempfile.NamedTemporaryFile(suffix=".nes", delete=False)
        rom.write(b"nes")
        rom.close()
        self.addCleanup(lambda: os.path.exists(rom.name) and os.unlink(rom.name))
        game = {"name": "Game", "path": rom.name, "platform": "NES"}
        with mock.patch("pkg.parity.parity_emulator_defs.shutil.which", return_value=None):
            with self.assertRaises(ValueError):
                _start_launch_command(game, {})


if __name__ == "__main__":
    unittest.main()
