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
        self._prev_data_dir = os.environ.get("OPENBOX_DATA_DIR")
        os.environ["OPENBOX_DATA_DIR"] = self.tempdir.name
        from openbox import save_state
        from web_app import Handler

        save_state({"games": [], "profiles": {}, "history": [], "settings": {}, "playlists": []})
        self.handler = object.__new__(Handler)
        self.handler.send_json = mock.Mock()

    def tearDown(self):
        self.tempdir.cleanup()
        if self._prev_data_dir is None:
            os.environ.pop("OPENBOX_DATA_DIR", None)
        else:
            os.environ["OPENBOX_DATA_DIR"] = self._prev_data_dir

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

    def test_delete_steam_games_keeps_other_library_entries(self):
        from openbox import load_state, save_state
        from web_app import Handler

        save_state({
            "games": [
                {"name": "Steam import", "source": "Steam", "steam_app_id": "42"},
                {"name": "Manual Steam shortcut", "source": "Manual", "steam_app_id": "43"},
                {"name": "ROM", "source": "Folder"},
            ],
            "profiles": {}, "history": [], "settings": {}, "playlists": [],
        })
        Handler.delete_steam_games(self.handler, {})
        self.assertEqual(self.handler.send_json.call_args[0], (200, {"removed": 1}))
        self.assertEqual([game["name"] for game in load_state()["games"]], ["Manual Steam shortcut", "ROM"])

    def test_diagnostic_log_route_returns_local_log(self):
        from web_app import Handler, TOKEN

        handler = object.__new__(Handler)
        handler.path = "/api/log"
        handler.headers = {"X-OpenBox-Token": TOKEN}
        handler.send_json = mock.Mock()
        with mock.patch("web_app.read_diagnostic_log", return_value="2026-07-30 DEBUG test message"):
            Handler._do_GET(handler)
        handler.send_json.assert_called_once_with(200, {"log": "2026-07-30 DEBUG test message"})

    def test_gameyfin_install_returns_before_download_finishes(self):
        import threading
        import time
        from http.server import ThreadingHTTPServer
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

    def test_settings_save_validates_ui_window_mode(self):
        from openbox import load_state, save_state
        from web_app import Handler

        save_state({
            "games": [],
            "profiles": {},
            "history": [],
            "settings": {},
            "playlists": [],
        })
        handler = object.__new__(Handler)
        handler.send_json = mock.Mock()
        Handler.save_settings(handler, {"ui_window": "browser"})
        self.assertEqual(load_state()["settings"]["ui_window"], "browser")
        with self.assertRaises(ValueError):
            Handler.save_settings(handler, {"ui_window": "teleport"})

    def test_settings_default_ui_window_is_app(self):
        from web_app import public_settings

        self.assertEqual(public_settings({"games": [], "profiles": {}, "history": [], "settings": {}, "playlists": []})["ui_window"], "app")

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

    def test_gameyfin_install_stale_library_id_appends_by_gameyfin_id(self):
        import threading
        import time
        from http.server import ThreadingHTTPServer
        import urllib.request

        import web_app
        from openbox import load_state, save_state

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
            self.assertEqual(status["state"], "done")
            games = load_state()["games"]
            self.assertEqual(len(games), 1)
            self.assertEqual(games[0]["gameyfin_id"], "9")
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

    def test_manual_playlist_preserves_order_and_metadata(self):
        from openbox import load_state, save_state
        from web_app import Handler

        save_state({
            "games": [
                {"name": "Alpha", "game_id": "game-alpha", "path": "/bin/true"},
                {"name": "Beta", "game_id": "game-beta", "path": "/bin/true"},
            ],
            "profiles": {}, "history": [], "settings": {}, "playlists": [],
        })
        handler = object.__new__(Handler)
        handler.send_json = mock.Mock()
        Handler.save_playlist(handler, {
            "name": "Weekend order",
            "type": "manual",
            "members": [1, 0, 1],
            "parent": "Favorites",
            "notes": "Play in this order",
            "rules": {},
        })
        state = load_state()
        playlist = state["playlists"][0]
        self.assertEqual(playlist["type"], "manual")
        self.assertEqual(playlist["members"], [state["games"][1]["game_id"], state["games"][0]["game_id"]])
        self.assertEqual(playlist["parent"], "Favorites")
        self.assertEqual(playlist["notes"], "Play in this order")

    def test_settings_save_persists_badges_and_extended_image_group(self):
        from openbox import load_state, save_state
        from web_app import Handler

        save_state({"games": [], "profiles": {}, "history": [], "settings": {}, "playlists": []})
        handler = object.__new__(Handler)
        handler.send_json = mock.Mock()
        Handler.save_settings(handler, {
            "watch_folders": [],
            "image_group": "fanart",
            "badge_visibility": ["favorite", "missing_media", "controller"],
        })
        settings = load_state()["settings"]
        self.assertEqual(settings["image_group"], "fanart")
        self.assertEqual(settings["badge_visibility"], ["favorite", "missing_media", "controller"])

    def test_backup_listing_reports_created_manifest(self):
        from openbox import save_state
        from web_app import Handler

        save_state({"games": [], "profiles": {}, "history": [], "settings": {}, "playlists": []})
        create_handler = object.__new__(Handler)
        create_handler.send_json = mock.Mock()
        Handler.create_library_backup(create_handler, {"items": ["library", "settings"], "keep": 7})

        list_handler = object.__new__(Handler)
        list_handler.authorized = mock.Mock(return_value=True)
        list_handler.send_json = mock.Mock()
        list_handler.do_GET = Handler.do_GET.__get__(list_handler, Handler)
        list_handler.path = "/api/backups"
        list_handler.headers = {}
        list_handler.do_GET()
        status, payload = list_handler.send_json.call_args[0]
        self.assertEqual(status, 200)
        self.assertTrue(payload["backups"])
        self.assertIn(["library", "settings"], [item["items"] for item in payload["backups"]])

    def test_local_plugin_catalog_file(self):
        catalog = load_local_catalog()
        self.assertTrue(any(item.get("id") == "openbox.library-stats" for item in catalog))

    def test_state_view_flags_manual_media(self):
        from openbox import save_state
        from web_app import DATA, Handler, public_state

        media_root = Path(DATA.parent) / "media"
        manual = media_root / "manual.pdf"
        manual.parent.mkdir(parents=True, exist_ok=True)
        manual.write_bytes(b"%PDF-1.4 test")
        save_state({
            "games": [{"name": "Manual Game", "platform": "NES", "path": "", "manual": str(manual)}],
            "profiles": {},
            "history": [],
            "settings": {},
            "playlists": [],
        })
        view = public_state()
        self.assertTrue(view["games"][0]["has_manual"])
        self.assertEqual(view["games"][0]["manual"], str(manual))
        handler = object.__new__(Handler)
        handler.authorized = mock.Mock(return_value=True)
        handler.send_file = mock.Mock()
        handler.do_GET = Handler.do_GET.__get__(handler, Handler)
        handler.path = "/api/media?id=0&kind=manual"
        handler.headers = {}
        handler.do_GET()
        status, path = handler.send_file.call_args[0]
        self.assertEqual(status, 200)
        self.assertEqual(str(path), str(manual))

    def test_apply_metadata_rejects_manual_without_game_path(self):
        from web_app import Handler

        with self.assertRaises(ValueError):
            Handler.apply_metadata(self.handler, {"id": 0, "database_id": 1, "media": ["manual"], "overwrite": False})


if __name__ == "__main__":
    unittest.main()
