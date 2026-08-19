"""Adversarial real-HTTP API boundary regressions."""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pkg" / "parity"))

class ApiSweep(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls._prev_data_dir = os.environ.get("OPENBOX_DATA_DIR")
        os.environ["OPENBOX_DATA_DIR"] = cls.tempdir.name
        import web_app
        from openbox import save_state

        cls.web_app = web_app
        cls.save_state = staticmethod(save_state)
        web_app.TOKEN = "sweep-token"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web_app.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.tempdir.cleanup()
        if cls._prev_data_dir is None:
            os.environ.pop("OPENBOX_DATA_DIR", None)
        else:
            os.environ["OPENBOX_DATA_DIR"] = cls._prev_data_dir

    def setUp(self):
        self.save_state({
            "games": [{"name": "Fixture", "path": "/bin/true", "save_paths": []}],
            "profiles": {},
            "history": [],
            "settings": {
                "watch_folders": [self.tempdir.name],
                "screensaver_seconds": 120,
                "gameyfin_password": "canary-secret",
                "storefront_auto_import": {"steam": True, "heroic": False, "lutris": True, "gameyfin": False},
            },
            "playlists": [],
        })

    def request(self, path, body=None, token="sweep-token", raw=False):
        headers = {}
        if token is not None:
            headers["X-OpenBox-Token"] = token
        data = body if raw else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers=headers,
            method="POST" if data is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                payload = response.read()
                return response.status, json.loads(payload) if payload else {}
        except urllib.error.HTTPError as error:
            payload = error.read()
            return error.code, json.loads(payload) if payload else {}

    def assert_alive(self):
        status, payload = self.request("/api/health", {})
        self.assertEqual(status, 200)
        self.assertIn("issues", payload)

    def test_fixture(self):
        status, payload = self.request("/api/library")
        self.assertEqual(status, 200)
        self.assertEqual(payload["games"][0]["name"], "Fixture")
        self.assertTrue(payload["games"][0]["game_id"])
        self.assertTrue(str(self.web_app.DATA).startswith(self.tempdir.name))
        self.assert_alive()

    def test_auth(self):
        source = Path("web_app.py").read_text()
        get_source = source[source.index("    def do_GET"):source.index("    def do_POST")]
        get_routes = sorted(set(re.findall(r'["\'](/api/[^"\']+)["\']', get_source)))
        for route in get_routes:
            status, _ = self.request(route, token=None)
            self.assertEqual(status, 403, route)
            status, _ = self.request(route, token="wrong")
            self.assertEqual(status, 403, route)
        status, _ = self.request("/api/library?token=sweep-token", token=None)
        self.assertEqual(status, 200)
        self.assert_alive()

    def test_post_auth(self):
        from routes import POST_TABLE

        for route in sorted(POST_TABLE):
            status, _ = self.request(route, {}, token=None)
            self.assertEqual(status, 403, route)
            status, _ = self.request(route, {}, token="wrong")
            self.assertEqual(status, 403, route)
        self.assert_alive()

    def test_validation(self):
        for body in (b"{", b"[]", b"null", b'"text"'):
            status, payload = self.request("/api/settings", body, raw=True)
            self.assertEqual(status, 400, body)
            self.assertIn("error", payload)
            self.assert_alive()
        status, payload = self.request("/api/not-a-route", {})
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "Not found")
        self.assertEqual(payload["code"], "ROUTE_NOT_FOUND")
        status, payload = self.request("/api/settings", b'{' + b'"x":"' + b'x' * 65537 + b'"}', raw=True)
        self.assertEqual(status, 400)
        self.assertIn("large", payload["error"].lower())
        self.assert_alive()

    def test_exceptions(self):
        with mock.patch("handlers.health.check_update", side_effect=AttributeError("missing tag")):
            status, payload = self.request("/api/update")
        self.assertEqual(status, 400)
        self.assertIn("missing tag", payload["error"])
        with mock.patch("handlers.imports.storefront_catalog", side_effect=ValueError("bad source")):
            status, payload = self.request("/api/storefront/catalog?source=fixture")
        self.assertEqual(status, 400)
        self.assertIn("bad source", payload["error"])
        with mock.patch(
            "handlers.imports.storefront_catalog",
            side_effect=subprocess.TimeoutExpired(cmd=["lutris"], timeout=30),
        ):
            status, payload = self.request("/api/storefront/catalog?source=lutris")
        self.assertEqual(status, 400)
        self.assertIn("error", payload)
        with mock.patch(
            "handlers.imports.import_lutris",
            side_effect=subprocess.CalledProcessError(1, ["lutris"]),
        ):
            status, payload = self.request("/api/import/lutris", {})
        self.assertEqual(status, 400)
        self.assertIn("error", payload)
        self.assert_alive()

    def test_settings(self):
        status, _ = self.request("/api/settings", {"screensaver_seconds": 45})
        self.assertEqual(status, 200)
        status, settings = self.request("/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(settings["watch_folders"], [self.tempdir.name])
        self.assertTrue(settings["storefront_auto_import"]["steam"])
        self.assertNotIn("gameyfin_password", settings)
        self.assertTrue(settings["gameyfin_password_set"])
        self.assert_alive()

    def test_concurrent_partial_settings_saves(self):
        # Concurrent partial saves of distinct keys must not lost-update.
        keys = [
            "screensaver_seconds", "save_backup_limit", "media_download_limit",
            "tracking_delay", "tracking_frequency", "progress_automation_play_minutes",
            "progress_automation_idle_days", "locale",
        ]
        for iteration in range(8):
            values = [30 + iteration, 1 + iteration, 1 + iteration, 1 + iteration,
                      1.0 + iteration * 0.1, 1 + iteration, 1 + iteration, f"en{iteration}"]
            barrier = threading.Barrier(len(keys))
            statuses = {}

            def worker(key, value, statuses=statuses, barrier=barrier):
                barrier.wait()
                status, _ = self.request("/api/settings", {key: value})
                statuses[key] = status

            threads = [
                threading.Thread(target=worker, args=(key, value))
                for key, value in zip(keys, values, strict=True)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(set(statuses.values()), {200}, statuses)
            status, settings = self.request("/api/settings")
            self.assertEqual(status, 200)
            expected = {
                "screensaver_seconds": values[0],
                "save_backup_limit": values[1],
                "media_download_limit": values[2],
                "tracking_delay": values[3],
                "tracking_frequency": float(values[4]),
                "progress_automation_play_minutes": values[5],
                "progress_automation_idle_days": values[6],
                "locale": str(values[7]),
            }
            for key, value in expected.items():
                self.assertEqual(settings.get(key), value, f"iteration {iteration} lost {key}")
        self.assert_alive()

    def test_lifecycle(self):
        for path in ("/api/saves?id=99", "/api/saves/discover?id=99"):
            status, payload = self.request(path)
            self.assertEqual(status, 404)
            self.assertIn("error", payload)
        status, payload = self.request("/api/gameyfin/install/status")
        self.assertEqual(status, 400)
        self.assertIn("gameyfin_id", payload["error"])
        status, payload = self.request("/api/session/control", {"launch_id": "missing", "action": "stop"})
        self.assertIn(status, (200, 400, 404))
        self.assertIsInstance(payload, dict)
        self.assert_alive()

    def test_bigbox_mode_switch(self):
        status, payload = self.request("/api/bigbox/mode", {"entering": False})
        self.assertEqual(status, 200)
        self.assertTrue(payload.get("ok"))
        self.assertFalse(payload.get("entering"))

    def test_related_rich_by_game_id(self):
        status, library = self.request("/api/library")
        self.assertEqual(status, 200)
        game_id = library["games"][0]["game_id"]
        status, payload = self.request(f"/api/related/rich?game_id={game_id}")
        self.assertEqual(status, 200)
        self.assertIn("items", payload)

    def test_settings_preservation(self):
        status, _ = self.request("/api/settings", {
            "tracking_process_name": "custom_proc",
            "sidebar_sections": ["favorites", "recent"],
            "controller_prompt_hint": True,
            "controller_prompt_pack": "playstation",
        })
        self.assertEqual(status, 200)
        status, settings = self.request("/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(settings.get("tracking_process_name"), "custom_proc")
        self.assertEqual(settings.get("sidebar_sections"), ["favorites", "recent"])
        self.assertEqual(settings.get("controller_prompt_hint"), True)
        self.assertEqual(settings.get("controller_prompt_pack"), "playstation")

    def test_emulator_validation(self):
        for path in ("/api/emulators/install", "/api/emulators/open", "/api/emulators/update"):
            status, payload = self.request(path, {"app_id": ""})
            self.assertEqual(status, 400, f"Expected 400 for empty app_id on {path}")

    def test_import_validation(self):
        invalid_requests = (
            ("/api/import", {"folder": ""}, "Folder path is required."),
            ("/api/import/wizard", {"folder": ""}, "Folder path is required."),
            ("/api/import/wizard", {"folder": "/tmp", "chosen_emulators": "invalid"}, "chosen_emulators must be an object."),
            ("/api/import/xbox360", {"folder": ""}, "Folder path is required."),
            ("/api/import/arcade", {"folder": ""}, "Folder path is required."),
            ("/api/import/exclusions", {"source": ""}, "source and external_id are required."),
            ("/api/import/exclusions/delete", {"source": ""}, "source and external_id are required."),
            ("/api/storefront/import", {"source": "unsupported_store"}, "Storefront source must be steam, heroic, lutris, or gameyfin."),
        )
        for path, body, expected_error in invalid_requests:
            status, payload = self.request(path, body)
            self.assertEqual(status, 400, f"Expected 400 for {path}")
            self.assertEqual(payload.get("code"), "BAD_REQUEST")
            self.assertEqual(payload.get("error"), expected_error)
        spaced_folder = Path(self.tempdir.name) / "roms "
        spaced_folder.mkdir(exist_ok=True)
        status, payload = self.request("/api/import", {"folder": str(spaced_folder)})
        self.assertEqual(status, 200)
        self.assertEqual(payload.get("found"), 0)


GROUPS = {
    "fixture": "test_fixture",
    "auth": "test_auth",
    "validation": "test_validation",
    "exceptions": "test_exceptions",
    "settings": "test_settings",
    "lifecycle": "test_lifecycle",
    "bigbox": "test_bigbox_mode_switch",
    "related": "test_related_rich_by_game_id",
    "preservation": "test_settings_preservation",
    "emulator_val": "test_emulator_validation",
    "import_val": "test_import_validation",
}

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", choices=GROUPS)
    args = parser.parse_args()
    names = [GROUPS[args.group]] if args.group else list(GROUPS.values())
    suite = unittest.TestSuite(ApiSweep(name) for name in names)
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
