"""HTTP smoke tests for platform administration, tokens, and bulk find/replace."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from unittest import mock
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class AdminHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.previous_data_dir = os.environ.get("OPENBOX_DATA_DIR")
        os.environ["OPENBOX_DATA_DIR"] = cls.tempdir.name
        import web_app
        from openbox import save_state
        cls.web_app = web_app
        cls.save_state = staticmethod(save_state)
        web_app.TOKEN = "admin-feature-token"
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
        if cls.previous_data_dir is None:
            os.environ.pop("OPENBOX_DATA_DIR", None)
        else:
            os.environ["OPENBOX_DATA_DIR"] = cls.previous_data_dir

    def setUp(self):
        self.save_state({
            "games": [
                {"game_id": "g-alpha", "name": "Alpha", "path": "/bin/true", "platform": "NES"},
                {"game_id": "g-beta", "name": "Beta", "path": "/bin/true", "platform": "SNES"},
            ],
            "profiles": {"NES": "fceux {path}"},
            "history": [],
            "settings": {"platform_categories": {"NES": "Console"}},
            "playlists": [],
            "queue": [],
            "notifications": [],
        })

    def request(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers={"X-OpenBox-Token": "admin-feature-token", "Content-Type": "application/json"},
            method="POST" if data is not None else "GET",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())

    def test_bigbox_startup_video_is_served_with_token(self):
        video = Path(self.tempdir.name) / "media" / "startup.mp4"
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"video-bytes")
        self.save_state({
            "games": [],
            "profiles": {},
            "history": [],
            "settings": {"bigbox_startup_video": str(video)},
            "playlists": [],
            "queue": [],
            "notifications": [],
        })
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/bigbox/startup-video",
            headers={"X-OpenBox-Token": "admin-feature-token"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"video-bytes")

    def test_platform_rename_moves_library_and_profile(self):
        status, payload = self.request("/api/v2/platform/rename", {"old": "NES", "new": "Famicom", "migrate_media": False})
        self.assertEqual(status, 200)
        self.assertEqual(payload["games"], 1)
        _, library = self.request("/api/library")
        platforms = {game["name"]: game["platform"] for game in library["games"]}
        self.assertEqual(platforms, {"Alpha": "Famicom", "Beta": "SNES"})

    def test_platform_delete_moves_games_to_trash(self):
        status, payload = self.request("/api/v2/platform/delete", {"name": "NES", "confirm": "NES"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["games"], 1)
        _, library = self.request("/api/library")
        self.assertEqual([game["name"] for game in library["games"]], ["Beta"])
        _, trash = self.request("/api/v2/library/trash")
        self.assertEqual(len(trash["items"]), 1)

    def test_platform_documents_can_be_reordered_but_not_replaced(self):
        platform = "DocsTest"
        first = Path(self.tempdir.name) / "media" / "first.txt"
        second = Path(self.tempdir.name) / "media" / "second.txt"
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_text("first", encoding="utf-8")
        second.write_text("second", encoding="utf-8")
        documents = [
            {"name": "First", "path": str(first)},
            {"name": "Second", "path": str(second)},
        ]
        status, _ = self.request("/api/platform/documents", {"platform": platform, "documents": documents})
        self.assertEqual(status, 200)
        status, _ = self.request("/api/platform/documents", {
            "platform": platform,
            "documents": list(reversed(documents)),
            "reorder": True,
        })
        self.assertEqual(status, 200)
        _, payload = self.request(f"/api/platform/documents?platform={platform}")
        self.assertEqual([item["name"] for item in payload["documents"]], ["Second", "First"])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/api/platform/documents", {
                "platform": platform,
                "documents": documents[:1],
                "reorder": True,
            })
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()

    def test_plugin_disable_removes_its_persisted_games(self):
        plugin_id = "test.drop"
        package = Path(self.tempdir.name) / "drop.zip"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("plugin.json", json.dumps({
                "id": plugin_id, "name": "Drop", "version": "1", "hooks": [],
            }))
            archive.writestr("plugin.py", "VALUE = 1\n")
        status, _ = self.request("/api/plugins/install", {"path": str(package)})
        self.assertEqual(status, 200)
        self.save_state({
            "games": [
                {"game_id": "g-alpha", "name": "Alpha", "path": "/bin/true", "platform": "NES"},
                {"game_id": f"plugin:{plugin_id}:1", "name": "Plugin Game", "path": "/bin/true", "platform": "PC", "plugin_source": plugin_id},
            ],
            "profiles": {},
            "history": [],
            "settings": {},
            "playlists": [],
            "queue": [],
            "notifications": [],
        })
        status, payload = self.request("/api/plugins/toggle", {"id": plugin_id, "enabled": False})
        self.assertEqual(status, 200)
        self.assertFalse(payload["enabled"])
        _, library = self.request("/api/library")
        self.assertEqual([game["name"] for game in library["games"]], ["Alpha"])

    def test_bigbox_startup_video_rejects_paths_outside_media(self):
        self.save_state({
            "games": [],
            "profiles": {},
            "history": [],
            "settings": {"bigbox_startup_video": "/etc/passwd"},
            "playlists": [],
            "queue": [],
            "notifications": [],
        })
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/api/bigbox/startup-video")
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()

    def test_plugin_remove_deletes_games_and_local_source(self):
        from handlers import extensions

        plugin_id = "test.remove"
        package = Path(self.tempdir.name) / "remove.zip"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("plugin.json", json.dumps({
                "id": plugin_id, "name": "Remove", "version": "1", "hooks": [],
            }))
            archive.writestr("plugin.py", "VALUE = 1\n")
        status, _ = self.request("/api/plugins/install", {"path": str(package)})
        self.assertEqual(status, 200)
        self.save_state({
            "games": [
                {"game_id": "g-alpha", "name": "Alpha", "path": "/bin/true", "platform": "NES"},
                {"game_id": f"plugin:{plugin_id}:1", "name": "Plugin Game", "path": "/bin/true", "platform": "PC", "plugin_source": plugin_id},
            ],
            "profiles": {},
            "history": [],
            "settings": {},
            "playlists": [],
            "queue": [],
            "notifications": [],
        })
        status, payload = self.request("/api/plugins/remove", {"id": plugin_id})
        self.assertEqual(status, 200)
        self.assertEqual(payload["removed"], plugin_id)
        self.assertFalse((Path(self.tempdir.name) / "plugins" / plugin_id).exists())
        _, library = self.request("/api/library")
        self.assertEqual([game["name"] for game in library["games"]], ["Alpha"])
        # The recorded local package is gone, so Repair cannot resurrect it.
        with mock.patch.object(extensions, "fetch_plugin_catalog", return_value=[]):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.request("/api/v2/plugins/repair", {"id": plugin_id})
        self.assertIn(caught.exception.code, (400, 404))
        caught.exception.close()

    def test_plugin_repair_reuses_a_recorded_local_package(self):
        from handlers import extensions
        plugin_id = "test.localrepair"
        package = Path(self.tempdir.name) / "local-repair.zip"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("plugin.json", json.dumps({
                "id": plugin_id, "name": "Local Repair", "version": "1", "hooks": [],
                "settings": {"type": "object", "properties": {"mode": {"type": "string", "default": "fast"}}},
            }))
            archive.writestr("plugin.py", "VALUE = 1\n")
        status, _ = self.request("/api/plugins/install", {"path": str(package)})
        self.assertEqual(status, 200)
        status, _ = self.request("/api/v2/plugins/trust", {"id": plugin_id, "trusted": True})
        self.assertEqual(status, 200)
        status, _ = self.request("/api/v2/plugins/settings", {"id": plugin_id, "values": {"mode": "slow"}})
        self.assertEqual(status, 200)
        stats_file = Path(self.tempdir.name) / "plugin-stats" / f"{plugin_id}.json"
        stats_file.parent.mkdir(parents=True, exist_ok=True)
        stats_file.write_text("{}", encoding="utf-8")
        installed = Path(self.tempdir.name) / "plugins" / plugin_id / "plugin.py"
        installed.write_text("VALUE = 0\n", encoding="utf-8")
        with mock.patch.object(extensions, "fetch_plugin_catalog", return_value=[]):
            status, payload = self.request("/api/v2/plugins/repair", {"id": plugin_id})
        self.assertEqual(status, 200)
        self.assertTrue(payload["repaired"])
        self.assertEqual(installed.read_text(encoding="utf-8"), "VALUE = 1\n")
        _, trust = self.request(f"/api/v2/plugins/trust?id={plugin_id}")
        self.assertTrue(trust["trusted"])
        _, settings = self.request(f"/api/v2/plugins/settings?id={plugin_id}")
        self.assertEqual(settings["values"]["mode"], "slow")
        self.assertTrue(stats_file.exists())

    def test_plugin_repair_reinstalls_from_the_catalog(self):
        from handlers import extensions
        plugin_id = "test.repair"
        package = Path(self.tempdir.name) / "repair.zip"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("plugin.json", json.dumps({
                "id": plugin_id, "name": "Repair", "version": "1", "hooks": [],
            }))
            archive.writestr("plugin.py", "VALUE = 1\n")
        plugins_root = Path(self.tempdir.name) / "plugins" / plugin_id
        plugins_root.mkdir(parents=True)
        (plugins_root / "plugin.json").write_text(json.dumps({
            "id": plugin_id, "name": "Repair", "version": "1", "hooks": [],
        }), encoding="utf-8")
        (plugins_root / "plugin.py").write_text("VALUE = 0\n", encoding="utf-8")
        with mock.patch.object(extensions, "fetch_plugin_catalog", return_value=[{"id": plugin_id}]), \
             mock.patch.object(extensions, "download_plugin_package", return_value=package):
            status, payload = self.request("/api/v2/plugins/repair", {"id": plugin_id})
        self.assertEqual(status, 200)
        self.assertTrue(payload["repaired"])
        installed = json.loads((plugins_root / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(installed["version"], "1")

    def test_bulk_find_replace_preview_then_apply(self):
        _, library = self.request("/api/library")
        ids = {game["name"]: game["game_id"] for game in library["games"]}
        status, preview = self.request("/api/v2/bulk/find-replace", {
            "ids": list(ids.values()), "field": "name", "find": "a", "replace": "A", "dry_run": True,
        })
        self.assertEqual(status, 200)
        self.assertEqual(preview["matched"], 2)
        status, applied = self.request("/api/v2/bulk/find-replace", {
            "ids": [ids["Alpha"]], "field": "name", "find": "Alpha", "replace": "ALPHA", "dry_run": False,
        })
        self.assertEqual(status, 200)
        self.assertEqual(applied["matched"], 1)
        _, library = self.request("/api/library")
        self.assertEqual({game["name"] for game in library["games"]}, {"ALPHA", "Beta"})

    def test_achievement_override_is_projected_and_clearable(self):
        _, library = self.request("/api/library")
        game_id = next(item["game_id"] for item in library["games"] if item["name"] == "Alpha")
        status, payload = self.request("/api/v2/achievements/override", {
            "game_id": game_id, "source": "steam", "earned": 3, "total": 5,
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["earned"], 3)
        _, library = self.request("/api/library")
        game = next(item for item in library["games"] if item["game_id"] == game_id)
        self.assertEqual(game["achievement_sources"]["steam"]["earned"], 3)
        self.assertTrue(game["achievement_sources"]["steam"]["manual"])
        self.assertTrue(game["has_achievements"])
        status, payload = self.request("/api/v2/achievements/override", {
            "game_id": game_id, "source": "steam", "clear": True,
        })
        self.assertEqual(status, 200)
        _, library = self.request("/api/library")
        game = next(item for item in library["games"] if item["game_id"] == game_id)
        self.assertNotIn("steam", game["achievement_sources"])

    def test_plugin_emulator_settings_require_their_declared_scope(self):
        plugin_id = "test.scope"
        package = Path(self.tempdir.name) / "scope.zip"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("plugin.json", json.dumps({
                "id": plugin_id,
                "name": "Scope",
                "version": "1",
                "hooks": ["emulator"],
                "capabilities": {"emulator_settings": [{
                    "emulator_id": "mame",
                    "label": "Vendor MAME",
                    "schema": {"type": "object", "properties": {}},
                    "scopes": ["platform"],
                }]},
            }))
            archive.writestr("plugin.py", "VALUE = 1\n")
        status, _ = self.request("/api/plugins/install", {"path": str(package)})
        self.assertEqual(status, 200)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/api/v2/emulators/settings", {
                "plugin_id": plugin_id, "emulator_id": "mame", "platform": "", "values": {},
            })
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()
        status, _ = self.request("/api/v2/emulators/settings", {
            "plugin_id": plugin_id, "emulator_id": "mame", "platform": "NES", "values": {},
        })
        self.assertEqual(status, 200)

    def test_plugin_metadata_apply_persists_provider(self):
        _, library = self.request("/api/library")
        game_id = next(item["game_id"] for item in library["games"] if item["name"] == "Alpha")
        updated = {
            "game_id": game_id,
            "name": "Alpha",
            "developer": "Plugin Studio",
            "metadata_provider": "plugin:vendor.meta",
        }
        with mock.patch(
            "pkg.parity.parity_plugin_providers.plugin_metadata_apply",
            return_value=updated,
        ):
            status, _ = self.request("/api/v2/metadata/candidates/apply", {
                "game_id": game_id,
                "candidate": {"plugin_id": "vendor.meta", "id": "m1"},
            })
        self.assertEqual(status, 200)
        import openbox

        stored = next(
            item for item in openbox.load_state()["games"] if item.get("game_id") == game_id
        )
        self.assertEqual(stored["metadata_provider"], "plugin:vendor.meta")
        _, library = self.request("/api/library")
        game = next(item for item in library["games"] if item["game_id"] == game_id)
        self.assertEqual(game["developer"], "Plugin Studio")

    def test_plugin_stats_merge_into_library_projection(self):
        _, library = self.request("/api/library")
        game = next(item for item in library["games"] if item["name"] == "Alpha")
        package = Path(self.tempdir.name) / "stats-provider.zip"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("plugin.json", json.dumps({
                "id": "vendor.stats",
                "name": "Vendor Stats",
                "version": "1",
                "hooks": ["stats"],
                "capabilities": {"stats": {"id": "vendor.stats", "name": "Vendor Stats"}},
            }))
            archive.writestr("plugin.py", "VALUE = 1\n")
        status, _ = self.request("/api/plugins/install", {"path": str(package)})
        self.assertEqual(status, 200)
        stats_dir = Path(self.tempdir.name) / "plugin-stats"
        stats_dir.mkdir(parents=True, exist_ok=True)
        (stats_dir / "vendor.stats.json").write_text(json.dumps({
            "plugin_id": "vendor.stats",
            "games": {game["game_id"]: {
                "playtime_seconds": 7200,
                "last_played": "2025-01-01T12:00:00",
                "sources": {"plugin:vendor.stats:sets": {
                    "name": "Vendor", "earned": 2, "total": 4, "items": [],
                }},
            }},
        }), encoding="utf-8")
        from pkg.state.cache import invalidate_state_cache
        invalidate_state_cache()
        _, library = self.request("/api/library")
        game = next(item for item in library["games"] if item["name"] == "Alpha")
        self.assertEqual(game["playtime_seconds"], 7200)
        self.assertEqual(game["last_played"], "2025-01-01T12:00:00")
        self.assertEqual(game["achievement_sources"]["plugin:vendor.stats:sets"]["earned"], 2)
        self.assertTrue(game["has_achievements"])

    def test_highscore_table_and_media_cache_cleanup_routes(self):
        status, payload = self.request("/api/v2/highscores/table?id=0")
        self.assertEqual(status, 200)
        self.assertFalse(payload["parsed"])
        cache = Path(self.tempdir.name) / "cache" / "media"
        cache.mkdir(parents=True, exist_ok=True)
        old = cache / "old.bin"
        old.write_bytes(b"x")
        os.utime(old, (0, 0))
        status, payload = self.request("/api/media/cache/cleanup", {"days": 30})
        self.assertEqual(status, 200)
        self.assertEqual(payload["deleted"], 1)
        self.assertFalse(old.exists())

    def test_plugin_data_round_trip_and_credentials_redaction(self):
        status, payload = self.request("/api/v2/plugins/data", {"id": "test.data", "values": {"cursor": "abc"}})
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["cursor"], "abc")
        _, payload = self.request("/api/v2/plugins/data?id=test.data")
        self.assertEqual(payload["data"]["cursor"], "abc")
        status, payload = self.request("/api/v2/plugins/credentials", {
            "id": "test.data", "values": {"token": "secret-value"},
        })
        self.assertEqual(status, 200)
        _, payload = self.request("/api/v2/plugins/credentials?id=test.data")
        self.assertEqual(payload["credentials"]["set"], {"token": True})
        self.assertNotIn("secret-value", json.dumps(payload))

    def test_integration_tokens_are_masked_and_removable(self):
        status, payload = self.request("/api/v2/integration-tokens", {"provider": "igdb", "token": "super-secret-token"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["saved"])
        _, tokens = self.request("/api/v2/integration-tokens")
        self.assertEqual(tokens["tokens"]["igdb"], "supe…en")
        _, settings = self.request("/api/settings")
        self.assertTrue(settings["integration_tokens"]["igdb"])
        self.assertNotIn("super-secret-token", json.dumps(settings))
        status, payload = self.request("/api/v2/integration-tokens/remove", {"provider": "igdb"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["removed"])


if __name__ == "__main__":
    unittest.main()
