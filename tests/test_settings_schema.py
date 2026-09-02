"""Settings whitelist boundary tests."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from handlers.settings import _clean_controller_map, _clean_controller_prompt_hint, _clean_screensaver_seconds, _clean_watch_folders, clean_settings  # noqa: E402
from settings_schema import KNOWN_SETTINGS, sanitize_settings  # noqa: E402


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

    def test_registry_covers_every_key_clean_settings_emits(self):
        # Structural regression: any key clean_settings can normalize but the
        # whitelist drops would silently never persist (the 1.7.2
        # gamescope_preset/mangohud_enabled bug). Defaults must be side-effect
        # free, so clean_settings({}) is a safe probe of the full key set.
        self.assertEqual(set(clean_settings({})), set(clean_settings({})) & KNOWN_SETTINGS)
        self.assertLessEqual(set(clean_settings({})), KNOWN_SETTINGS)

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
            "auto_close_store_clients", "gamescope_preset", "mangohud_enabled",
            "show_insights", "list_sort", "list_sort_dir",
        ):
            self.assertIn(key, KNOWN_SETTINGS, f"save-path key missing from whitelist: {key}")

    def test_clean_watch_folders_over_50(self):
        payload = {"watch_folders": [f"/tmp/folder_{i}" for i in range(51)]}
        with self.assertRaises(ValueError) as ctx:
            _clean_watch_folders(payload)
        self.assertIn("at most 50 paths", str(ctx.exception))

    def test_clean_watch_folders_non_list(self):
        with self.assertRaises(ValueError) as ctx:
            _clean_watch_folders({"watch_folders": "not_a_list"})
        self.assertIn("at most 50 paths", str(ctx.exception))

    def test_clean_watch_folders_valid_and_nonexistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_clean_watch_folders({"watch_folders": [tmp]}), [tmp])
        with self.assertRaises(ValueError) as ctx:
            _clean_watch_folders({"watch_folders": ["/nonexistent_openbox_test_path_xyz_123"]})
        self.assertIn("Watch folder does not exist", str(ctx.exception))

    def test_clean_screensaver_seconds_out_of_range(self):
        for bad in (10, 29, 3601, -5):
            with self.assertRaises(ValueError) as ctx:
                _clean_screensaver_seconds({"screensaver_seconds": bad})
            self.assertIn("Screensaver delay must be 0 or between 30 and 3600 seconds", str(ctx.exception))

    def test_clean_screensaver_seconds_valid(self):
        for val in (0, 30, 90, 3600):
            self.assertEqual(_clean_screensaver_seconds({"screensaver_seconds": val}), val)
        self.assertEqual(_clean_screensaver_seconds({}), 90)

    def test_clean_controller_map_non_dict(self):
        for bad in (["not", "dict"], "string", 123):
            with self.assertRaises(ValueError) as ctx:
                _clean_controller_map({"controller_map": bad})
            self.assertIn("Controller mapping must be an object", str(ctx.exception))

    def test_clean_controller_map_valid_and_invalid(self):
        valid = {"play": 0, "back": 1, "menu": 31}
        self.assertEqual(_clean_controller_map({"controller_map": valid}), valid)

    def test_clean_controller_prompt_hint_string_and_bool(self):
        self.assertEqual(_clean_controller_prompt_hint({"controller_prompt_hint": "A · B"}), "A · B")
        self.assertEqual(_clean_controller_prompt_hint({"controller_prompt_hint": True}), "A Play · B Back · M Menu")
        self.assertEqual(_clean_controller_prompt_hint({"controller_prompt_hint": False}), "")
        with self.assertRaises(ValueError):
            _clean_controller_prompt_hint({"controller_prompt_hint": 42})

        for bad_map in (
            {"invalid_action": 0},
            {"play": -1},
            {"play": 32},
            {"play": "0"},
        ):
            with self.assertRaises(ValueError) as ctx:
                _clean_controller_map({"controller_map": bad_map})
            self.assertIn("Controller button mappings must use buttons 0 through 31", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
