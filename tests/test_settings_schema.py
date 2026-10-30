"""Settings whitelist boundary tests."""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from handlers.settings import (  # noqa: E402
    _clean_bool,
    _clean_bounded_int,
    _clean_bigbox_hidden_lists,
    _clean_controller_map,
    _clean_controller_prompt_hint,
    _clean_cover_models,
    _clean_currency,
    _clean_dict,
    _clean_enum,
    _clean_gamescope_presets,
    _clean_keyboard_map,
    _clean_party,
    _clean_progress_values,
    _clean_screensaver_seconds,
    _clean_sidebar_sections,
    _clean_str_list,
    _clean_tracking,
    _clean_watch_folders,
    clean_settings,
)
from settings_schema import KNOWN_SETTINGS, prune_unknown_settings, sanitize_settings  # noqa: E402


def _form_collected_keys():
    """Parse the keys returned by collectSettings() in static/settings.js."""
    root = Path(__file__).resolve().parent.parent
    src = (root / "static" / "settings.js").read_text(encoding="utf-8")
    match = re.search(r"function collectSettings\(\)\s*\{.*?return\s*\{(.*?)\n\s*\};", src, re.S)
    assert match, "collectSettings return block not found"
    keys = set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", match.group(1), re.M))
    keys |= {"badge_visibility", "controller_map", "gamescope_custom_presets"}
    return keys


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

    def test_currency_defaults_and_validates(self):
        self.assertEqual(_clean_currency({}), "USD")
        self.assertEqual(_clean_currency({"currency": "eur"}), "EUR")
        with self.assertRaises(ValueError):
            _clean_currency({"currency": "EU"})

    def test_bool_normalizer_rejects_numeric_and_string_input(self):
        self.assertFalse(_clean_bool({}, "flag", False))
        self.assertTrue(_clean_bool({"flag": True}, "flag", False))
        for malformed in (1, 0, "true", [], {}):
            with self.assertRaises(ValueError):
                _clean_bool({"flag": malformed}, "flag", False)

    def test_enum_normalizer_rejects_unknown_and_non_string(self):
        self.assertEqual(_clean_enum({}, "mode", {"a", "b"}, "a"), "a")
        self.assertEqual(_clean_enum({"mode": "b"}, "mode", {"a", "b"}, "a"), "b")
        for malformed in ("c", 1, True, []):
            with self.assertRaises(ValueError):
                _clean_enum({"mode": malformed}, "mode", {"a", "b"}, "a")

    def test_bounded_int_rejects_bool_and_out_of_range(self):
        self.assertEqual(_clean_bounded_int({}, "n", 4, 1, 10, "bad"), 4)
        self.assertEqual(_clean_bounded_int({"n": "6"}, "n", 4, 1, 10, "bad"), 6)
        for malformed in (True, 0, 11, "x", []):
            with self.assertRaises(ValueError):
                _clean_bounded_int({"n": malformed}, "n", 4, 1, 10, "bad")

    def test_str_list_caps_dedupes_and_rejects_containers(self):
        self.assertEqual(_clean_str_list({}, "items", 3, 5), [])
        self.assertEqual(
            _clean_str_list({"items": [" a ", "a", "b", ""]}, "items", 4, 5),
            ["a", "b"],
        )
        self.assertEqual(_clean_str_list({"items": ["abcdef"]}, "items", 3, 4), ["abcd"])
        for malformed in ("a", [1], ["a", "b", "c", "d"]):
            with self.assertRaises(ValueError):
                _clean_str_list({"items": malformed}, "items", 3, 5)

    def test_dict_normalizer_copies_and_enforces_limit(self):
        source = {"a": [1]}
        clean = _clean_dict({"map": source}, "map", 2)
        clean["a"].append(2)
        self.assertEqual(source, {"a": [1]})
        for malformed in ([], "x", {"a": 1, "b": 2, "c": 3}):
            with self.assertRaises(ValueError):
                _clean_dict({"map": malformed}, "map", 2)

    def test_sidebar_sections_are_unique_and_preserved(self):
        self.assertEqual(_clean_sidebar_sections({}), ["search", "view", "filters", "platforms", "playlists", "explorer"])
        self.assertEqual(_clean_sidebar_sections({"sidebar_sections": ["filters", "search"]}), ["filters", "search"])
        # Legacy ids stay in the stored order so an upgrade does not erase them.
        self.assertEqual(_clean_sidebar_sections({"sidebar_sections": ["favorites", "recent"]}), ["favorites", "recent"])
        self.assertEqual(_clean_sidebar_sections({"sidebar_sections": ["search", "search"]}), ["search"])
        for malformed in ("search", list(range(21)), [1]):
            with self.assertRaises(ValueError):
                _clean_sidebar_sections({"sidebar_sections": malformed})

    def test_progress_values_are_known_and_ordered(self):
        self.assertEqual(_clean_progress_values({}), ["Unplayed", "Playing", "Paused", "Beaten", "Completed", "Mastered", "Abandoned"])
        self.assertEqual(_clean_progress_values({"progress_values": ["Playing", "Beaten"]}), ["Playing", "Beaten"])
        for malformed in ("Playing", ["Nope"], [""], [1]):
            with self.assertRaises(ValueError):
                _clean_progress_values({"progress_values": malformed})

    def test_cover_model_style_maps_are_validated(self):
        defaults = _clean_cover_models({})
        self.assertEqual(defaults, ("box", {}, "free", {}, {}, {}))
        cleaned = _clean_cover_models({
            "cover_model_by_platform": {"SNES": "cassette"},
            "model_back_by_platform": {"SNES": "straight"},
            "model_case_by_platform": {"SNES": "solid"},
            "model_rotation_lock_by_platform": {"SNES": "vertical"},
        })
        self.assertEqual(cleaned, (
            "box", {"SNES": "cassette"}, "free",
            {"SNES": "straight"}, {"SNES": "solid"}, {"SNES": "vertical"},
        ))
        for key, value in (
            ("cover_model_by_platform", "disc"),
            ("model_back_by_platform", "curved"),
            ("model_case_by_platform", "opaque"),
            ("model_rotation_lock_by_platform", "locked"),
        ):
            with self.assertRaises(ValueError):
                _clean_cover_models({key: {"SNES": value}})

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
            "currency",
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
            "mood_match_enabled", "mood_match_bigbox",
            "party_queue", "party_players", "party_index",
        ):
            self.assertIn(key, KNOWN_SETTINGS, f"save-path key missing from whitelist: {key}")

    def test_clean_settings_emits_every_form_collected_key(self):
        # Anti-jumble regression: any key the settings form collects but
        # clean_settings drops would silently never persist (show_insights,
        # backup_auto_enabled/keep class of bug).
        emitted = set(clean_settings({}))
        for key in sorted(_form_collected_keys()):
            self.assertIn(key, emitted, f"form-collected key never written: {key}")

    def test_native_storefront_auto_import_keys_are_saved(self):
        settings = clean_settings({
            "storefront_auto_import": {
                "gog": {"enabled": True},
                "ea": True,
                "ubisoft": {"enabled": True, "platform": "PC"},
                "bogus": {"enabled": True},
            },
        })
        stores = settings["storefront_auto_import"]
        self.assertTrue(stores["gog"]["enabled"])
        self.assertTrue(stores["ea"]["enabled"])
        self.assertEqual(stores["ubisoft"]["platform"], "PC")
        self.assertNotIn("bogus", stores)

    def test_clean_settings_round_trips_backup_auto_and_insights(self):
        clean = clean_settings({
            "show_insights": False,
            "backup_auto_enabled": True,
            "backup_auto_keep": 8,
        })
        self.assertIs(clean["show_insights"], False)
        self.assertIs(clean["backup_auto_enabled"], True)
        self.assertEqual(clean["backup_auto_keep"], 8)
        self.assertEqual(clean_settings({})["backup_auto_keep"], 4)
        for bad in (0, 53, "many"):
            with self.assertRaises(ValueError, msg=str(bad)):
                clean_settings({"backup_auto_keep": bad})

    def test_safe_coercion_maps_null_to_default_and_garbage_to_400(self):
        self.assertEqual(_clean_screensaver_seconds({"screensaver_seconds": None}), 90)
        self.assertEqual(_clean_tracking({"tracking_frequency": None})[2], 2)
        self.assertEqual(clean_settings({"state_retention": None})["state_retention"], 1)
        self.assertEqual(clean_settings({"attract_mode_seconds": None})["attract_mode_seconds"], 90)
        for payload in (
            {"screensaver_seconds": "soon"},
            {"tracking_delay": "later"},
            {"tracking_frequency": "often"},
            {"save_backup_limit": "lots"},
            {"progress_automation_play_minutes": "x"},
        ):
            with self.assertRaises(ValueError, msg=str(payload)):
                clean_settings(payload)

    def test_prune_unknown_settings_removes_only_unknowns(self):
        state = {"settings": {"library_view": "grid", "ghost_key": 1, "other_ghost": 2}}
        removed = prune_unknown_settings(state)
        self.assertEqual(removed, ["ghost_key", "other_ghost"])
        self.assertEqual(state["settings"], {"library_view": "grid"})
        self.assertEqual(prune_unknown_settings({}), [])
        self.assertEqual(prune_unknown_settings({"settings": None}), [])

    def test_clean_gamescope_presets_valid(self):
        clean = _clean_gamescope_presets({"gamescope_custom_presets": [
            {"name": "Deck 90", "width": 1280, "height": 800, "refresh": 90, "fsr": True, "fsr_sharpness": 4},
            {"name": "crt", "integer": True, "extra_args": "--adaptive-sync -e"},
        ]})
        self.assertEqual(clean[0], {"name": "Deck 90", "width": 1280, "height": 800, "refresh": 90, "fsr": True, "fsr_sharpness": 4})
        self.assertEqual(clean[1], {"name": "crt", "integer": True, "extra_args": ["--adaptive-sync", "-e"]})

    def test_clean_gamescope_presets_rejects_bad_input(self):
        for payload, message in (
            ({"gamescope_custom_presets": "nope"}, "must be a list"),
            ({"gamescope_custom_presets": [{"name": ""}]}, "need a name"),
            ({"gamescope_custom_presets": [{"name": "a"}, {"name": "A"}]}, "Duplicate"),
            ({"gamescope_custom_presets": [{"name": "x", "width": 99999, "height": 10}]}, "width"),
            ({"gamescope_custom_presets": [{"name": "x", "refresh": -1}]}, "refresh"),
            ({"gamescope_custom_presets": [{"name": "x", "fsr_sharpness": 9}]}, "fsr_sharpness"),
        ):
            with self.assertRaises(ValueError) as ctx:
                _clean_gamescope_presets(payload)
            self.assertIn(message, str(ctx.exception))

    def test_clean_gamescope_presets_caps_count(self):
        with self.assertRaises(ValueError) as ctx:
            _clean_gamescope_presets({"gamescope_custom_presets": [{"name": f"p{i}"} for i in range(17)]})
        self.assertIn("At most 16", str(ctx.exception))

    def test_clean_party_round_trip(self):
        queue, players, index = _clean_party({
            "party_queue": ["g-1", "g-2", "g-1", "", "g-3"],
            "party_players": 4,
            "party_index": 1,
        })
        self.assertEqual(queue, ["g-1", "g-2", "g-3"])
        self.assertEqual(players, 4)
        self.assertEqual(index, 1)

    def test_clean_party_defaults(self):
        self.assertEqual(_clean_party({}), ([], 2, 0))

    def test_clean_party_caps_queue_and_clamps_index(self):
        queue, players, index = _clean_party({
            "party_queue": [f"g-{i}" for i in range(60)],
            "party_players": 2,
            "party_index": 99,
        })
        self.assertEqual(len(queue), 50)
        self.assertEqual(index, 49)
        self.assertEqual(players, 2)

    def test_clean_party_rejects_bad_input(self):
        for payload, message in (
            ({"party_queue": "g-1"}, "must be a list"),
            ({"party_players": 1}, "between 2 and 8"),
            ({"party_players": 9}, "between 2 and 8"),
            ({"party_players": "many"}, "must be an integer"),
            ({"party_index": -1}, "must be >= 0"),
            ({"party_index": "first"}, "must be an integer"),
        ):
            with self.assertRaises(ValueError, msg=str(payload)) as ctx:
                _clean_party(payload)
            self.assertIn(message, str(ctx.exception))

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
        with self.assertRaises(ValueError) as ctx:
            _clean_controller_map({"controller_map": {"play": 4, "favorite": 4}})
        self.assertIn("Button 4 is assigned to both play and favorite", str(ctx.exception))


    def test_clean_keyboard_map(self):
        self.assertEqual(
            _clean_keyboard_map({"keyboard_map": {"play": "Enter", "back": "Escape"}}),
            {"play": "Enter", "back": "Escape"},
        )
        self.assertEqual(
            _clean_keyboard_map({"keyboard_map": {"play": "", "unknown": "x"}}),
            {},
        )
        with self.assertRaises(ValueError):
            _clean_keyboard_map({"keyboard_map": ["Enter"]})
        with self.assertRaises(ValueError):
            _clean_keyboard_map({"keyboard_map": {"play": "NotAKey"}})
        with self.assertRaises(ValueError):
            _clean_keyboard_map({"keyboard_map": {"play": "Enter", "back": "enter"}})
        # Physical names are part of the accepted grammar and matchable in
        # the client through matchesKey's aliases.
        self.assertEqual(
            _clean_keyboard_map({"keyboard_map": {"play": "KeyA", "random": "Digit0"}}),
            {"play": "KeyA", "random": "Digit0"},
        )

    def test_input_ranges_and_clamping(self):
        clean = clean_settings({"spinner_sensitivity": 0, "bigbox_wheel_speed": 1.5,
                                "bigbox_back_hold_seconds": 0})
        self.assertEqual(clean["spinner_sensitivity"], 1)
        self.assertEqual(clean["bigbox_wheel_speed"], 1.5)
        self.assertEqual(clean["bigbox_back_hold_seconds"], 0)
        self.assertEqual(clean_settings({"spinner_sensitivity": 99})["spinner_sensitivity"], 20)
        for bad in ({"bigbox_wheel_speed": 0.24}, {"bigbox_wheel_speed": 3.1},
                    {"bigbox_back_hold_seconds": -0.1}, {"bigbox_back_hold_seconds": 2.1}):
            with self.assertRaises(ValueError):
                clean_settings(bad)

    def test_clean_bigbox_hidden_lists(self):
        platforms, categories, playlists = _clean_bigbox_hidden_lists({
            "bigbox_hidden_platforms": [" Arcade ", "Arcade", "DOS"],
            "bigbox_hidden_categories": ["Fighting"],
            "bigbox_hidden_playlists": ["Co-op"],
        })
        self.assertEqual(platforms, ["Arcade", "DOS"])
        self.assertEqual(categories, ["Fighting"])
        self.assertEqual(playlists, ["Co-op"])
        self.assertEqual(_clean_bigbox_hidden_lists({}), ([], [], []))
        with self.assertRaises(ValueError):
            _clean_bigbox_hidden_lists({"bigbox_hidden_platforms": "Arcade"})

    def test_hidden_sidebar_sections_shape_and_drop(self):
        # Row 14: the free-text setting became a checkbox group. The schema
        # key must accept a list of section ids and reject non-lists.
        from handlers.settings import _clean_hidden_sections

        self.assertEqual(
            _clean_hidden_sections({"hidden_sidebar_sections": ["search", "filters"]}),
            ["search", "filters"],
        )
        self.assertEqual(_clean_hidden_sections({}), [])
        with self.assertRaises(ValueError):
            _clean_hidden_sections({"hidden_sidebar_sections": "search"})
        # The whitelist keeps the key and drops unknown siblings.
        clean, dropped = sanitize_settings({
            "hidden_sidebar_sections": ["search"],
            "bogus_sidebar_key": True,
        })
        self.assertEqual(clean["hidden_sidebar_sections"], ["search"])
        self.assertIn("bogus_sidebar_key", dropped)


class LibraryPathSettingsTests(unittest.TestCase):
    def test_path_mode_must_be_absolute_or_portable(self):
        with self.assertRaisesRegex(ValueError, "absolute or portable"):
            clean_settings({"path_mode": "relative"})
        self.assertEqual(clean_settings({"path_mode": "portable"})["path_mode"], "portable")

    def test_library_root_must_be_an_absolute_existing_folder(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, "absolute local path"):
                clean_settings({"library_root": "games"})
            with self.assertRaisesRegex(ValueError, "does not exist"):
                clean_settings({"library_root": str(Path(root) / "missing")})
            clean = clean_settings({"path_mode": "portable", "library_root": root})
            self.assertEqual(Path(clean["library_root"]).resolve(), Path(root).resolve())

    def test_default_path_settings_are_absolute_with_no_root(self):
        clean = clean_settings({})
        self.assertEqual((clean["path_mode"], clean["library_root"]), ("absolute", ""))


if __name__ == "__main__":
    unittest.main()
