#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emulators import emulator_status, install_emulator  # noqa: E402
from handlers.emulators import EmulatorsHandlers  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
