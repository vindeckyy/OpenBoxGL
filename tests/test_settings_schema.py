"""Settings whitelist boundary tests."""
from __future__ import annotations

import unittest

from settings_schema import KNOWN_SETTINGS, sanitize_settings


class SettingsSchemaTests(unittest.TestCase):
    def test_sanitize_drops_unknown_keys(self):
        clean, dropped = sanitize_settings({
            "library_view": "grid",
            "totally_made_up_key": 123,
            "another_unknown": "value",
        })
        self.assertEqual(clean, {"library_view": "grid"})
        self.assertEqual(set(dropped), {"totally_made_up_key", "another_unknown"})

    def test_sanitize_passes_known_keys_unchanged(self):
        payload = {
            "library_view": "list",
            "bigbox_mode": "stage",
            "locale": "en",
            "watch_folders": ["/games"],
            "theme": "Midnight Circuit",
        }
        clean, dropped = sanitize_settings(payload)
        self.assertEqual(clean, payload)
        self.assertEqual(dropped, [])

    def test_non_dict_input(self):
        clean, dropped = sanitize_settings(["not", "a", "dict"])
        self.assertEqual(clean, {})
        self.assertEqual(dropped, [])

    def test_non_dict_string_input(self):
        # A string is malformed input, not a list of keys to iterate.
        clean, dropped = sanitize_settings("theme")
        self.assertEqual(clean, {})
        self.assertEqual(dropped, [])

    def test_registry_contains_every_key_the_save_path_writes(self):
        # The save handler normalizes and writes these keys; the whitelist
        # must cover them or a fresh settings save would drop them.
        for key in (
            "watch_folders", "screensaver_seconds", "controller_map", "badge_visibility",
            "cloud_folder", "startup_commands", "shutdown_commands", "track_session_history",
            "backup_on_close", "save_backup_limit", "progress_automation_enabled",
            "progress_automation_play_minutes", "progress_automation_idle_days",
            "welcome_completed", "image_group", "auto_import_media_types",
            "media_download_limit", "region_priority", "video_priority", "library_music",
            "video_bgm_mix", "bigbox_mode", "show_playlist_actions", "hidden_sidebar_sections",
            "storefront_auto_import", "obs_auto_attach", "obs_recording_path",
            "dynamic_play_button", "custom_field_defs", "platform_categories", "list_columns",
            "library_view", "locale", "attract_mode_seconds", "bigbox_startup_video",
            "bigbox_shutdown_commands", "tray_enabled", "minimize_to_tray", "gameyfin_url",
            "gameyfin_username", "gameyfin_password", "gameyfin_install_dir",
            "gameyfin_provider", "ludusavi_backup_path", "tracking_mode", "tracking_delay",
            "tracking_frequency", "apply_perf", "progress_on_first_play",
            "auto_close_store_clients",
        ):
            self.assertIn(key, KNOWN_SETTINGS, f"save-path key missing from whitelist: {key}")


if __name__ == "__main__":
    unittest.main()
