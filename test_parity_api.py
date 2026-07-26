"""Smoke tests for parity API routes."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from plugin_catalog import load_local_catalog


class ParityApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        os.environ["OPENBOX_DATA_DIR"] = self.tempdir.name
        from openbox import save_state
        from web_app import Handler

        save_state({"games": [], "profiles": {}, "history": [], "settings": {}, "playlists": []})
        self.handler = object.__new__(Handler)
        self.handler.send_json = mock.Mock()

    def tearDown(self):
        self.tempdir.cleanup()

    def payload(self, response):
        return response[0][1]

    def test_plugin_catalog_route(self):
        from web_app import Handler

        handler = object.__new__(Handler)
        handler.authorized = mock.Mock(return_value=True)
        handler.send_json = mock.Mock()
        handler.do_GET = Handler.do_GET.__get__(handler, Handler)
        handler.path = "/api/plugins/catalog"
        handler.headers = {}
        handler.do_GET()
        handler.send_json.assert_called_once()
        status, payload = handler.send_json.call_args[0]
        self.assertEqual(status, 200)
        self.assertTrue(payload["catalog"])

    def test_storefront_import_route(self):
        from web_app import Handler

        handler = object.__new__(Handler)
        handler.authorized = mock.Mock(return_value=True)
        handler.body = mock.Mock(return_value={"source": "steam", "installed_only": True, "uninstalled_only": False})
        handler.send_json = mock.Mock()
        with mock.patch("web_app.storefront_catalog", return_value=[]):
            with mock.patch("web_app.catalog_entries_to_games", return_value=[]):
                Handler.import_storefront_catalog(handler, handler.body())
        handler.send_json.assert_called_with(200, {"added": 0, "found": 0, "imported": 0})

    def test_gameyfin_install_returns_before_download_finishes(self):
        import threading
        import time
        from http.server import ThreadingHTTPServer
        from urllib.error import HTTPError
        import urllib.request

        import web_app
        from openbox import save_state

        save_state({
            "games": [{"name": "Nebula", "gameyfin_id": "7", "store_installed": False}],
            "profiles": {},
            "history": [],
            "settings": {"gameyfin_url": "http://gameyfin.local"},
            "playlists": [],
        })
        web_app.TOKEN = "testtoken"
        web_app.INSTALLS.clear()
        started = threading.Event()
        release = threading.Event()

        def slow_install(settings, game_id, client=None):
            started.set()
            release.wait(timeout=5)
            return {"gameyfin_id": str(game_id), "store_installed": True, "path": "/tmp/fake", "launch": "/tmp/fake"}

        server = ThreadingHTTPServer(("127.0.0.1", 0), web_app.Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/gameyfin/install",
                data=json.dumps({"gameyfin_id": "7", "library_id": 0}).encode(),
                headers={"Content-Type": "application/json", "X-OpenBox-Token": "testtoken"},
                method="POST",
            )
            with mock.patch("web_app.install_gameyfin_game", side_effect=slow_install):
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 202)
                    payload = json.loads(response.read())
                    self.assertEqual(payload["state"], "installing")
                self.assertTrue(started.wait(timeout=2))
                status_request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/gameyfin/install/status?gameyfin_id=7",
                    headers={"X-OpenBox-Token": "testtoken"},
                )
                with urllib.request.urlopen(status_request, timeout=5) as response:
                    status = json.loads(response.read())
                    self.assertEqual(status["state"], "installing")
                release.set()
                deadline = time.time() + 5
                while time.time() < deadline:
                    with urllib.request.urlopen(status_request, timeout=5) as response:
                        status = json.loads(response.read())
                    if status.get("state") == "done":
                        break
                    time.sleep(0.05)
                else:
                    self.fail("Gameyfin install job did not finish")
                self.assertTrue(status["game"]["store_installed"])
        finally:
            server.shutdown()
            server.server_close()
            web_app.INSTALLS.clear()

    def test_settings_save_preserves_storefront_auto_import_when_omitted(self):
        from openbox import load_state, save_state
        from web_app import Handler

        save_state({
            "games": [],
            "profiles": {},
            "history": [],
            "settings": {
                "storefront_auto_import": {"steam": True, "heroic": False, "lutris": True, "gameyfin": False},
            },
            "playlists": [],
        })
        handler = object.__new__(Handler)
        handler.send_json = mock.Mock()
        Handler.save_settings(handler, {"watch_folders": []})
        settings = load_state()["settings"]
        self.assertTrue(settings["storefront_auto_import"]["steam"])
        self.assertTrue(settings["storefront_auto_import"]["lutris"])
        self.assertFalse(settings["storefront_auto_import"]["gameyfin"])

    def test_settings_save_preserves_unrelated_fields_on_partial_post(self):
        from openbox import load_state, save_state
        from web_app import Handler

        save_state({
            "games": [],
            "profiles": {},
            "history": [],
            "settings": {
                "watch_folders": ["/tmp"],
                "screensaver_seconds": 120,
                "storefront_auto_import": {"steam": True, "heroic": False, "lutris": False, "gameyfin": False},
            },
            "playlists": [],
        })
        handler = object.__new__(Handler)
        handler.send_json = mock.Mock()
        Handler.save_settings(handler, {
            "storefront_auto_import": {"steam": False, "heroic": True, "lutris": False, "gameyfin": False},
            "gameyfin_url": "http://gameyfin.local",
        })
        settings = load_state()["settings"]
        self.assertEqual(settings["watch_folders"], ["/tmp"])
        self.assertEqual(settings["screensaver_seconds"], 120)
        self.assertTrue(settings["storefront_auto_import"]["heroic"])
        self.assertFalse(settings["storefront_auto_import"]["steam"])

    def test_storefront_catalog_route_returns_json_error(self):
        from web_app import Handler

        handler = object.__new__(Handler)
        handler.authorized = mock.Mock(return_value=True)
        handler.send_json = mock.Mock()
        handler.do_GET = Handler.do_GET.__get__(handler, Handler)
        handler.path = "/api/storefront/catalog?source=heroic"
        handler.headers = {}
        with mock.patch("web_app.storefront_catalog", side_effect=FileNotFoundError("xdg-open missing")):
            handler.do_GET()
        status, payload = handler.send_json.call_args[0]
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    def test_gameyfin_providers_route_returns_json_error(self):
        from parity_gameyfin import GameyfinError
        from web_app import Handler

        handler = object.__new__(Handler)
        handler.authorized = mock.Mock(return_value=True)
        handler.send_json = mock.Mock()
        handler.do_GET = Handler.do_GET.__get__(handler, Handler)
        handler.path = "/api/gameyfin/providers"
        handler.headers = {}
        with mock.patch("web_app.catalog_gameyfin", side_effect=GameyfinError("Gameyfin URL is not configured.")):
            handler.do_GET()
        status, payload = handler.send_json.call_args[0]
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    def test_gameyfin_install_invalid_library_id_reports_error(self):
        import threading
        import time
        from http.server import ThreadingHTTPServer
        import urllib.request

        import web_app
        from openbox import save_state

        save_state({
            "games": [],
            "profiles": {},
            "history": [],
            "settings": {"gameyfin_url": "http://gameyfin.local"},
            "playlists": [],
        })
        web_app.TOKEN = "testtoken"
        web_app.INSTALLS.clear()

        def fast_install(settings, game_id, client=None):
            return {"gameyfin_id": str(game_id), "store_installed": True, "path": "/tmp/fake", "launch": "/tmp/fake"}

        server = ThreadingHTTPServer(("127.0.0.1", 0), web_app.Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/gameyfin/install",
                data=json.dumps({"gameyfin_id": "9", "library_id": 0}).encode(),
                headers={"Content-Type": "application/json", "X-OpenBox-Token": "testtoken"},
                method="POST",
            )
            with mock.patch("web_app.install_gameyfin_game", side_effect=fast_install):
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 202)
                status_request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/gameyfin/install/status?gameyfin_id=9",
                    headers={"X-OpenBox-Token": "testtoken"},
                )
                deadline = time.time() + 5
                while time.time() < deadline:
                    with urllib.request.urlopen(status_request, timeout=5) as response:
                        status = json.loads(response.read())
                    if status.get("state") in {"error", "done"}:
                        break
                    time.sleep(0.05)
                else:
                    self.fail("Gameyfin install job did not reach a terminal state")
            self.assertEqual(status["state"], "error")
        finally:
            server.shutdown()
            server.server_close()
            web_app.INSTALLS.clear()

    def test_post_wrong_json_shapes_return_json_error(self):
        import threading
        from http.server import ThreadingHTTPServer
        import urllib.error
        import urllib.request

        import web_app

        web_app.TOKEN = "testtoken"
        server = ThreadingHTTPServer(("127.0.0.1", 0), web_app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for body in (b"[]", b"null", b'"text"'):
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_address[1]}/api/settings",
                    data=body,
                    headers={"Content-Type": "application/json", "X-OpenBox-Token": "testtoken"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(caught.exception.code, 400)
                self.assertIn("error", json.loads(caught.exception.read()))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_premium_routes_require_auth(self):
        from web_app import Handler

        handler = object.__new__(Handler)
        handler.authorized = mock.Mock(return_value=False)
        handler.send_json = mock.Mock()
        handler.do_GET = Handler.do_GET.__get__(handler, Handler)
        handler.headers = {}
        for path in ("/api/premium/strings", "/api/premium/media-packs", "/api/premium/platform-categories"):
            handler.send_json.reset_mock()
            handler.path = path
            handler.do_GET()
            status, payload = handler.send_json.call_args[0]
            self.assertEqual(status, 403, path)

    def test_update_route_returns_json_error_for_malformed_release(self):
        from web_app import Handler

        handler = object.__new__(Handler)
        handler.authorized = mock.Mock(return_value=True)
        handler.send_json = mock.Mock()
        handler.do_GET = Handler.do_GET.__get__(handler, Handler)
        handler.path = "/api/update"
        handler.headers = {}
        with mock.patch("web_app.check_update", side_effect=AttributeError("release missing tag_name")):
            handler.do_GET()
        status, payload = handler.send_json.call_args[0]
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    def test_settings_save_preserves_gameyfin_when_omitted(self):
        from openbox import load_state, save_state
        from web_app import Handler

        save_state({
            "games": [],
            "profiles": {},
            "history": [],
            "settings": {
                "gameyfin_url": "http://gameyfin.local",
                "gameyfin_username": "player",
                "gameyfin_password": "secret",
                "gameyfin_install_dir": "/tmp/gameyfin",
            },
            "playlists": [],
        })
        handler = object.__new__(Handler)
        handler.send_json = mock.Mock()
        Handler.save_settings(handler, {"watch_folders": []})
        settings = load_state()["settings"]
        self.assertEqual(settings["gameyfin_url"], "http://gameyfin.local")
        self.assertEqual(settings["gameyfin_username"], "player")
        self.assertEqual(settings["gameyfin_password"], "secret")
        self.assertEqual(settings["gameyfin_install_dir"], "/tmp/gameyfin")

    def test_local_plugin_catalog_file(self):
        catalog = load_local_catalog()
        self.assertTrue(any(item.get("id") == "openbox.library-stats" for item in catalog))


if __name__ == "__main__":
    unittest.main()
