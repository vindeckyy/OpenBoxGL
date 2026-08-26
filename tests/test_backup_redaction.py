import sys
import os
import json
import threading
import time
import unittest
import tempfile
import types
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pkg.parity.parity_redact import (
    detach_state_view,
    redact_settings,
    redact_state_for_export,
    merge_preserved_credentials,
)
from pkg.parity.parity_backup import create_backup, restore_backup

class TestBackupRedaction(unittest.TestCase):
    def test_redact_settings_removes_password(self):
        settings = {"gameyfin_password": "my_secret_password", "theme": "dark"}
        redacted = redact_settings(settings)
        self.assertNotIn("gameyfin_password", redacted)
        self.assertEqual(redacted["theme"], "dark")

    def test_redact_settings_strips_webhook_secrets(self):
        settings = {
            "webhooks": [
                {"id": "1", "url": "http://foo", "secret": "shh"},
                {"id": "2", "url": "http://bar"}
            ]
        }
        redacted = redact_settings(settings)
        self.assertNotIn("secret", redacted["webhooks"][0])
        self.assertTrue(redacted["webhooks"][0]["secret_set"])
        self.assertNotIn("secret", redacted["webhooks"][1])
        self.assertFalse(redacted["webhooks"][1]["secret_set"])

    def test_redact_settings_handles_non_dict_inputs(self):
        self.assertEqual(redact_settings(None), {})
        redacted = redact_settings({"webhooks": ["not-a-hook"]})
        self.assertEqual(redacted["webhooks"], ["not-a-hook"])

    def test_redact_state_for_export_handles_non_dict(self):
        self.assertEqual(redact_state_for_export(None), {})

    def test_redact_state_for_export_preserves_non_secrets(self):
        state = {
            "games": [{"id": "g1"}],
            "settings": {"api_key": "123", "theme": "light"}
        }
        redacted = redact_state_for_export(state)
        self.assertNotIn("api_key", redacted["settings"])
        self.assertEqual(redacted["settings"]["theme"], "light")
        self.assertEqual(redacted["games"][0]["id"], "g1")

    def test_redact_state_handles_missing_settings(self):
        state = {"games": []}
        redacted = redact_state_for_export(state)
        self.assertNotIn("settings", redacted)

    def test_detach_state_view_unwraps_mapping_proxy(self):
        nested = types.MappingProxyType({"gameyfin_password": "secret"})
        view = types.MappingProxyType({"settings": nested, "games": []})
        detached = detach_state_view(view)
        self.assertIsInstance(detached, dict)
        self.assertIsInstance(detached["settings"], dict)
        json.dumps(detached)

    def test_redact_state_excludes_preview_and_operation_fields(self):
        state = {
            "games": [],
            "settings": {},
            "previews": [{"id": "scan-1"}],
            "operation_history": [{"job_id": "job-1"}],
            "operations": {"job-1": {"state": "done"}},
        }
        redacted = redact_state_for_export(state)
        self.assertNotIn("previews", redacted)
        self.assertNotIn("operation_history", redacted)
        self.assertNotIn("operations", redacted)

    def test_merge_preserved_credentials_preserves_password(self):
        existing = {"gameyfin_password": "local_pass"}
        restored = {"theme": "dark"}
        merged = merge_preserved_credentials(restored, existing)
        self.assertEqual(merged["gameyfin_password"], "local_pass")
        self.assertEqual(merged["theme"], "dark")

    def test_merge_preserved_credentials_preserves_webhook_secrets(self):
        existing = {
            "webhooks": [
                {"id": "1", "secret": "local_shh"}
            ]
        }
        restored = {
            "webhooks": [
                {"id": "1", "url": "http://foo", "secret_set": True},
                {"id": "2", "url": "http://bar"}
            ]
        }
        merged = merge_preserved_credentials(restored, existing)
        self.assertEqual(merged["webhooks"][0]["secret"], "local_shh")
        self.assertNotIn("secret_set", merged["webhooks"][0])
        self.assertNotIn("secret", merged["webhooks"][1])

    def test_merge_preserved_credentials_handles_invalid_inputs(self):
        merged = merge_preserved_credentials(None, {"gameyfin_password": "local"})
        self.assertEqual(merged["gameyfin_password"], "local")
        merged = merge_preserved_credentials({"webhooks": ["raw-hook"]}, {})
        self.assertEqual(merged["webhooks"], ["raw-hook"])
        merged = merge_preserved_credentials({"theme": "dark"}, None)
        self.assertEqual(merged["theme"], "dark")
        
    def test_integration_create_and_restore(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            
            state = {
                "games": [{"id": "g1"}],
                "settings": {
                    "gameyfin_password": "secret_pass",
                    "webhooks": [{"id": "wh1", "secret": "wh_secret"}],
                    "theme": "dark"
                }
            }
            
            archive_path = create_backup(temp_dir, state, items=["settings", "library"])
            
            with zipfile.ZipFile(archive_path) as z:
                manifest = json.loads(z.read("manifest.json"))
                self.assertTrue(manifest.get("redacted_secrets"))
                
                settings_json = json.loads(z.read("settings.json"))
                self.assertNotIn("gameyfin_password", settings_json)
                self.assertNotIn("secret", settings_json["webhooks"][0])
                self.assertTrue(settings_json["webhooks"][0]["secret_set"])
                
                library_json = json.loads(z.read("library.json"))
                self.assertNotIn("gameyfin_password", library_json["settings"])
                
            with (temp_path / "settings.json").open("w") as f:
                json.dump({"gameyfin_password": "local_password", "webhooks": [{"id": "wh1", "secret": "local_wh_secret"}]}, f)
                
            with (temp_path / "library.json").open("w") as f:
                json.dump({"games": []}, f)
                
            restore_backup(archive_path, temp_dir, force=True)
            
            with (temp_path / "settings.json").open() as f:
                restored_settings = json.load(f)
            self.assertEqual(restored_settings["gameyfin_password"], "local_password")
            self.assertEqual(restored_settings["webhooks"][0]["secret"], "local_wh_secret")
            
            with (temp_path / "library.json").open() as f:
                restored_library = json.load(f)
            self.assertEqual(restored_library["settings"]["gameyfin_password"], "local_password")
            self.assertEqual(restored_library["settings"]["webhooks"][0]["secret"], "local_wh_secret")


class BackupExportRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.previous_data_dir = os.environ.get("OPENBOX_DATA_DIR")
        os.environ["OPENBOX_DATA_DIR"] = cls.tempdir.name
        import web_app
        from openbox import save_state

        cls.web_app = web_app
        cls.save_state = staticmethod(save_state)
        web_app.TOKEN = "backup-export-token"
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
            "games": [{"name": "Fixture", "path": "/bin/true"}],
            "profiles": {},
            "history": [],
            "settings": {
                "gameyfin_password": "export-secret",
                "webhooks": [{"id": "wh1", "url": "http://example", "secret": "hook-secret"}],
            },
            "playlists": [],
        })

    def request(self, path):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            headers={"X-OpenBox-Token": "backup-export-token"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, dict(response.headers), response.read()

    def test_get_api_backup_succeeds_and_redacts_secrets(self):
        status, headers, body = self.request("/api/backup")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        payload = json.loads(body)
        settings = payload.get("settings", {})
        self.assertNotIn("gameyfin_password", settings)
        self.assertNotIn("secret", settings.get("webhooks", [{}])[0])
        self.assertIn("portable", headers.get("Content-Disposition", "").lower())

    def test_get_api_v1_backup_matches_redacted_export(self):
        status, _, body = self.request("/api/v1/backup")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertNotIn("gameyfin_password", payload.get("settings", {}))

    def test_get_api_diagnostic_includes_recent_job_ids(self):
        from webapp_state import JOB_MANAGER

        gate = threading.Event()
        JOB_MANAGER.submit("export-diagnostic-test", lambda _ctx: None)
        for _ in range(100):
            if any(item.get("name") == "export-diagnostic-test" for item in JOB_MANAGER.history(limit=10)):
                break
            time.sleep(0.01)
        JOB_MANAGER.submit("export-diagnostic-active", lambda _ctx: gate.wait(timeout=1))
        for _ in range(100):
            if JOB_MANAGER.snapshots().get("export-diagnostic-active", {}).get("state") == "running":
                break
            time.sleep(0.01)
        status, _, payload = self.request("/api/diagnostic")
        gate.set()
        self.assertEqual(status, 200)
        report = json.loads(json.loads(payload)["report"])
        self.assertGreaterEqual(len(report["recent_job_ids"]), 2)
        self.assertNotIn("gameyfin_password", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
