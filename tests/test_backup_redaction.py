import sys
import os
import json
import unittest
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pkg.parity.parity_redact import redact_settings, redact_state_for_export, merge_preserved_credentials
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

if __name__ == "__main__":
    unittest.main()
