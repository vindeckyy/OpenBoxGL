"""Tests for CacheEpoch unified invalidation (Lane 4A).

Validates that bump_media_epoch and transact_state clear every cache dict
managed by CacheEpoch, that cache hits work before invalidation and miss
after, and that the double-checked locking pattern in public_settings is
preserved.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class CacheEpochTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls._prev_data_dir = os.environ.get("OPENBOX_DATA_DIR")
        os.environ["OPENBOX_DATA_DIR"] = cls.tempdir.name
        import openbox
        import webapp_state
        from state_store import JsonStateStore

        openbox.DATA = Path(cls.tempdir.name) / "library.json"
        openbox.STATE_STORE = JsonStateStore(openbox.DATA)
        webapp_state.DATA = openbox.DATA
        webapp_state.STATE_STORE = openbox.STATE_STORE
        from openbox import save_state
        from webapp_state import MEDIA_EPOCH, PLUGIN_EPOCH

        save_state({
            "games": [], "profiles": {}, "history": [],
            "settings": {}, "playlists": [],
        })
        cls.MEDIA_EPOCH = MEDIA_EPOCH
        cls.PLUGIN_EPOCH = PLUGIN_EPOCH
        cls.save_state = staticmethod(save_state)

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()
        if cls._prev_data_dir is None:
            os.environ.pop("OPENBOX_DATA_DIR", None)
        else:
            os.environ["OPENBOX_DATA_DIR"] = cls._prev_data_dir

    def setUp(self):
        from webapp_state import CACHE_EPOCH

        CACHE_EPOCH.media = 0
        CACHE_EPOCH.plugin = 0
        self.MEDIA_EPOCH["value"] = 0
        self.PLUGIN_EPOCH["value"] = 0
        self.save_state({
            "games": [
                {"game_id": "g1", "name": "Alpha", "path": "/bin/true", "cover": ""},
                {"game_id": "g2", "name": "Beta", "path": "/bin/false"},
            ],
            "profiles": {}, "history": [], "settings": {}, "playlists": [],
        })

    # -- bump_media_epoch invalidates all caches ----------------------------

    def test_bump_media_epoch_invalidates_all_caches(self):
        """bump_media_epoch must clear every cache dict managed by CacheEpoch."""
        from webapp_state import (
            CACHE_EPOCH,
            FILE_PROBE_CACHE,
            PLUGIN_LIBRARY_CACHE,
            PUBLIC_SETTINGS_CACHE,
            PUBLIC_STATE_CACHE,
            STATE_VIEW_CACHE,
            _GAME_PROJECTION_CACHE,
            _KNOWN_MEDIA_SET_CACHE,
            _SANITIZE_MEDIA_PATH_CACHE,
            bump_media_epoch,
            load_state_view,
            public_state,
        )

        # Populate caches
        public_state()
        load_state_view()
        _KNOWN_MEDIA_SET_CACHE.update({"key": "fake", "result": {"x"}})
        _GAME_PROJECTION_CACHE["k"] = "v"
        _SANITIZE_MEDIA_PATH_CACHE["k"] = "v"
        FILE_PROBE_CACHE[("k", False)] = (0.0, True)
        PLUGIN_LIBRARY_CACHE.update({
            "at": 999.0, "payload": {"x": 1}, "state_signature": "sig",
        })

        bump_media_epoch()

        # All caches must be cleared
        self.assertIsNone(PUBLIC_STATE_CACHE.get("signature"))
        self.assertIsNone(PUBLIC_SETTINGS_CACHE.get("signature"))
        self.assertIsNone(_KNOWN_MEDIA_SET_CACHE.get("key"))
        self.assertEqual(len(_GAME_PROJECTION_CACHE), 0)
        self.assertEqual(len(_SANITIZE_MEDIA_PATH_CACHE), 0)
        self.assertEqual(len(FILE_PROBE_CACHE), 0)
        self.assertIsNone(PLUGIN_LIBRARY_CACHE["payload"])
        self.assertIsNone(STATE_VIEW_CACHE["state"])

        # Epoch counter must have incremented
        self.assertEqual(CACHE_EPOCH.media, 1)
        self.assertEqual(self.MEDIA_EPOCH["value"], 1)

    # -- cache hit / miss ---------------------------------------------------

    def test_cache_hit_before_and_miss_after_invalidation(self):
        """public_state returns the same object until epoch bumps."""
        from webapp_state import bump_media_epoch, public_state

        first = public_state()
        second = public_state()
        self.assertIs(first, second)  # cache hit

        bump_media_epoch()
        third = public_state()
        self.assertIsNot(first, third)  # cache miss
        self.assertEqual(third["media_epoch"], 1)

    # -- double-checked locking ---------------------------------------------

    def test_double_checked_locking_public_settings(self):
        """public_settings uses sig vs sig_after double-check pattern."""
        from webapp_state import public_settings

        result1 = public_settings()
        result2 = public_settings()
        self.assertEqual(result1["version"], result2["version"])
        self.assertIsInstance(result1, dict)

    # -- transact_state clears caches via _invalidate_all -------------------

    def test_transact_state_clears_caches(self):
        """transact_state must clear caches via _invalidate_all."""
        from webapp_state import (
            PUBLIC_STATE_CACHE,
            STATE_VIEW_CACHE,
            load_state_view,
            public_state,
            transact_state,
        )

        public_state()
        load_state_view()
        self.assertIsNotNone(PUBLIC_STATE_CACHE.get("signature"))

        def noop_mutator(state):
            return state

        transact_state(noop_mutator)
        self.assertIsNone(PUBLIC_STATE_CACHE.get("signature"))
        self.assertIsNone(STATE_VIEW_CACHE["state"])

    # -- epoch counter sync -------------------------------------------------

    def test_epoch_counter_stays_in_sync(self):
        """CACHE_EPOCH.media and MEDIA_EPOCH['value'] must always agree."""
        from webapp_state import CACHE_EPOCH, MEDIA_EPOCH, bump_media_epoch

        for i in range(3):
            bump_media_epoch()
            self.assertEqual(CACHE_EPOCH.media, i + 1)
            self.assertEqual(MEDIA_EPOCH["value"], i + 1)

    # -- CacheEpoch owns the cache dicts ------------------------------------

    def test_cache_epoch_owns_cache_dicts(self):
        """Module-level aliases must point to the same objects as CACHE_EPOCH."""
        from pkg.state import cache

        self.assertIs(cache.PUBLIC_STATE_CACHE, cache.CACHE_EPOCH.state)
        self.assertIs(cache.PUBLIC_SETTINGS_CACHE, cache.CACHE_EPOCH.settings)
        self.assertIs(cache._KNOWN_MEDIA_SET_CACHE, cache.CACHE_EPOCH.media_set)
        self.assertIs(cache._GAME_PROJECTION_CACHE, cache.CACHE_EPOCH.game_projection)
        self.assertIs(cache._SANITIZE_MEDIA_PATH_CACHE, cache.CACHE_EPOCH.sanitize_media_path)
        self.assertIs(cache.FILE_PROBE_CACHE, cache.CACHE_EPOCH.file_probe)

    # -- F09: detached snapshots and settings defaults ----------------------

    def test_load_state_view_nested_mutation_does_not_persist(self):
        """Mutating nested data from load_state_view must not affect stored state."""
        from webapp_state import load_state_view, transact_state

        original = load_state_view()["games"][0]["name"]
        view = load_state_view()
        view["games"][0]["name"] = "Mutated In View"
        reloaded = load_state_view()
        self.assertEqual(reloaded["games"][0]["name"], original)
        transact_state(lambda state: state["games"][0].__setitem__("name", original))

    def test_public_settings_missing_controller_prompt_hint_defaults_empty_string(self):
        """Missing controller_prompt_hint key must default to empty string, not False."""
        from webapp_state import public_settings

        result = public_settings({"settings": {}})
        self.assertEqual(result["controller_prompt_hint"], "")
        self.assertIsInstance(result["controller_prompt_hint"], str)

    def test_public_settings_stringifies_stored_bool_hint(self):
        """Stored bool leftovers stringify on GET after F22."""
        self.save_state({
            "games": [{"game_id": "g1", "name": "Alpha", "path": "/bin/true"}],
            "profiles": {}, "history": [], "settings": {"controller_prompt_hint": True}, "playlists": [],
        })
        from webapp_state import public_settings

        result = public_settings()
        self.assertEqual(result["controller_prompt_hint"], "A Play · B Back · M Menu")
        self.assertIsInstance(result["controller_prompt_hint"], str)

    def test_public_state_projection_and_cache_helpers(self):
        from webapp_state import public_state, public_state_bytes, public_state_etag

        payload = public_state()
        self.assertEqual(len(payload["games"]), 2)
        self.assertIn("settings", payload)
        raw = public_state_bytes()
        self.assertIsInstance(raw, (bytes, bytearray))
        etag = public_state_etag()
        self.assertTrue(etag.startswith('"'))

    def test_load_state_view_cache_hit_returns_detached_copy(self):
        from webapp_state import load_state_view

        first = load_state_view()
        second = load_state_view()
        self.assertIsNot(first, second)
        self.assertEqual(first["games"], second["games"])

    def test_media_set_scan_and_file_probe_cache(self):
        from webapp_state import (
            FILE_PROBE_CACHE,
            _build_known_media_set,
            bump_media_epoch,
            clear_file_probe_cache,
            probe_path,
        )

        media_dir = Path(self.tempdir.name) / "media" / "covers"
        media_dir.mkdir(parents=True)
        cover = media_dir / "alpha.png"
        cover.write_bytes(b"png")
        known = _build_known_media_set()
        self.assertTrue(known)
        exists = probe_path(str(cover), file_only=True)
        self.assertTrue(exists)
        FILE_PROBE_CACHE[("stale", False)] = (0.0, True)
        clear_file_probe_cache()
        self.assertEqual(len(FILE_PROBE_CACHE), 0)
        bump_media_epoch()

    def test_transact_state_commits_mutation(self):
        from webapp_state import load_state_view, transact_state

        transact_state(lambda state: state["games"].append({
            "game_id": "g3", "name": "Gamma", "path": "/bin/ls",
        }))
        self.assertEqual(len(load_state_view()["games"]), 3)

    def test_public_state_projects_edge_case_game_fields(self):
        self.save_state({
            "games": [{
                "game_id": "g-edge",
                "name": "Edge",
                "path": "/bin/true",
                "platform": "Arcade",
                "alternate_names": "Alt One;Alt Two",
                "custom_fields": "bad",
                "tags": "bad",
                "screenshots": "bad",
                "rom_name": "pacman",
            }],
            "profiles": {}, "history": [], "settings": {}, "playlists": [],
        })
        from webapp_state import public_state

        game = public_state()["games"][0]
        self.assertEqual(game["alternate_names"], ["Alt One", "Alt Two"])
        self.assertEqual(game["custom_fields"], {})
        self.assertEqual(game["tags"], [])
        self.assertEqual(game["screenshots"], [])
        self.assertTrue(game["has_highscores"])

    def test_media_roots_env_is_scanned(self):
        import os
        from webapp_state import _build_known_media_set, bump_media_epoch

        extra_root = Path(self.tempdir.name) / "extra-media"
        extra_root.mkdir()
        (extra_root / "box.png").write_bytes(b"x")
        previous = os.environ.get("OPENBOX_MEDIA_ROOTS")
        os.environ["OPENBOX_MEDIA_ROOTS"] = str(extra_root)
        try:
            bump_media_epoch()
            known = _build_known_media_set()
            self.assertTrue(any("box.png" in entry for entry in known))
        finally:
            if previous is None:
                os.environ.pop("OPENBOX_MEDIA_ROOTS", None)
            else:
                os.environ["OPENBOX_MEDIA_ROOTS"] = previous

    def test_cache_helper_functions(self):
        from pkg.state import cache

        self.assertTrue(cache._fast_realpath("~/"))
        self.assertFalse(cache._media_set_contains(set(), ""))
        self.assertFalse(cache._media_set_contains({"/tmp/x"}, ""))
        self.assertGreaterEqual(cache._media_dir_mtime(), 0.0)
        self.assertEqual(cache._ns("missing_symbol", "fallback"), "fallback")
        media_set = {"/tmp/cover.png"}
        self.assertTrue(cache._media_set_contains(media_set, "/tmp/cover.png"))

    def test_public_state_bytes_uses_cache(self):
        from webapp_state import public_state, public_state_bytes

        public_state()
        first = public_state_bytes()
        second = public_state_bytes()
        self.assertIs(first, second)

    def test_safe_mode_skips_plugin_decoration(self):
        import os
        from unittest import mock
        from webapp_state import public_state

        with mock.patch.dict(os.environ, {"OPENBOX_SAFE_MODE": "1"}):
            payload = public_state()
        self.assertEqual(len(payload["games"]), 2)

    def test_media_set_contains_normpath_match(self):
        from pkg.state import cache

        media_set = {"/tmp/cover.png"}
        self.assertTrue(cache._media_set_contains(media_set, "/tmp/./cover.png"))
        self.assertFalse(cache._media_set_contains(media_set, "relative/path"))

    def test_project_game_coerces_non_list_tags(self):
        from pkg.state.cache import _project_game

        game = {"game_id": "g1", "name": "Alpha", "path": "/bin/true", "platform": "PC", "tags": "bad"}
        projected = _project_game(game, 0, set(), set(), None, {}, 0)
        self.assertEqual(projected["tags"], [])

    def test_public_settings_uncached_non_dict_platform_documents(self):
        from pkg.state.cache import _public_settings_uncached

        result = _public_settings_uncached({"settings": {"platform_documents": "bad"}})
        self.assertEqual(result["platform_documents"], {})

    def test_project_game_projection_cache_hit(self):
        from pkg.state.cache import _project_game

        game = {"game_id": "g1", "name": "Alpha", "path": "/bin/true", "platform": "PC"}
        first = _project_game(game, 0, set(), set(), None, {}, 0)
        second = _project_game(game, 0, set(), set(), None, {}, 0)
        self.assertIs(first, second)

    def test_projection_and_platform_category_cache_eviction(self):
        import webapp_state
        from pkg.state import cache

        original_proj = cache._GAME_PROJECTION_MAX
        original_cat = cache._PLATFORM_CATEGORY_MAX
        cache._GAME_PROJECTION_MAX = 1
        cache._PLATFORM_CATEGORY_MAX = 1
        webapp_state._GAME_PROJECTION_MAX = 1
        webapp_state._PLATFORM_CATEGORY_MAX = 1
        cache._GAME_PROJECTION_CACHE.clear()
        cache._PLATFORM_CATEGORY_CACHE.clear()
        try:
            game_a = {"game_id": "ga", "name": "A", "path": "/a", "platform": "PC"}
            game_b = {"game_id": "gb", "name": "B", "path": "/b", "platform": "SNES"}
            cache._project_game(game_a, 0, set(), set(), None, {}, 0)
            cache._project_game(game_b, 1, set(), set(), None, {}, 0)
            cache._project_game(game_a, 0, set(), set(), None, {}, 0)
            self.assertLessEqual(len(cache._GAME_PROJECTION_CACHE), 1)
        finally:
            cache._GAME_PROJECTION_MAX = original_proj
            cache._PLATFORM_CATEGORY_MAX = original_cat
            webapp_state._GAME_PROJECTION_MAX = original_proj
            webapp_state._PLATFORM_CATEGORY_MAX = original_cat
            cache._GAME_PROJECTION_CACHE.clear()
            cache._PLATFORM_CATEGORY_CACHE.clear()

    def test_build_known_media_set_caps_entries(self):
        from pkg.state import cache
        from webapp_state import bump_media_epoch

        original_max = cache._KNOWN_MEDIA_MAX
        cache._KNOWN_MEDIA_MAX = 2
        try:
            media_dir = Path(self.tempdir.name) / "media" / "caps"
            media_dir.mkdir(parents=True)
            for index in range(5):
                (media_dir / f"file{index}.png").write_bytes(b"x")
            bump_media_epoch()
            known = cache._build_known_media_set()
            self.assertLessEqual(len(known), 4)
        finally:
            cache._KNOWN_MEDIA_MAX = original_max

    def test_media_dir_mtime_includes_extra_roots(self):
        from pkg.state import cache

        extra_root = Path(self.tempdir.name) / "mtime-extra"
        nested = extra_root / "nested"
        nested.mkdir(parents=True)
        (nested / "leaf.txt").write_text("x", encoding="utf-8")
        previous = os.environ.get("OPENBOX_MEDIA_ROOTS")
        os.environ["OPENBOX_MEDIA_ROOTS"] = f"{extra_root}{os.pathsep}{os.pathsep}"
        try:
            self.assertGreater(cache._media_dir_mtime(), 0.0)
        finally:
            if previous is None:
                os.environ.pop("OPENBOX_MEDIA_ROOTS", None)
            else:
                os.environ["OPENBOX_MEDIA_ROOTS"] = previous

    def test_media_dir_mtime_includes_nested_library_media(self):
        from pkg.state import cache

        nested = Path(self.tempdir.name) / "media" / "nested"
        nested.mkdir(parents=True)
        (nested / "art.png").write_bytes(b"x")
        self.assertGreater(cache._media_dir_mtime(), 0.0)

    def test_build_known_media_set_skips_unreadable_entries(self):
        from pkg.state import cache
        from webapp_state import bump_media_epoch

        media_dir = Path(self.tempdir.name) / "media" / "broken"
        media_dir.mkdir(parents=True)
        entry = mock.Mock()
        entry.is_file = mock.Mock(side_effect=OSError("stat"))
        entry.is_dir = mock.Mock(return_value=False)
        iterator = mock.Mock()
        iterator.__enter__ = mock.Mock(return_value=iter([entry]))
        iterator.__exit__ = mock.Mock(return_value=False)
        with mock.patch("pkg.state.cache.os.scandir", return_value=iterator):
            bump_media_epoch()
            self.assertEqual(cache._build_known_media_set(), set())

    def test_build_known_media_set_skips_blank_env_roots(self):
        import os
        from pkg.state import cache
        from webapp_state import bump_media_epoch

        previous = os.environ.get("OPENBOX_MEDIA_ROOTS")
        os.environ["OPENBOX_MEDIA_ROOTS"] = f"  {os.pathsep}{os.pathsep}  "
        try:
            bump_media_epoch()
            self.assertIsInstance(cache._build_known_media_set(), set)
        finally:
            if previous is None:
                os.environ.pop("OPENBOX_MEDIA_ROOTS", None)
            else:
                os.environ["OPENBOX_MEDIA_ROOTS"] = previous

    def test_cache_epoch_bump_plugin_invalidates(self):
        from webapp_state import CACHE_EPOCH, PLUGIN_EPOCH, PLUGIN_LIBRARY_CACHE

        PLUGIN_LIBRARY_CACHE.update({"at": 1.0, "payload": {"games": []}, "state_signature": "x"})
        CACHE_EPOCH._invalidate_all(bump_plugin=True)
        self.assertEqual(CACHE_EPOCH.plugin, 1)
        self.assertEqual(PLUGIN_EPOCH["value"], 1)
        self.assertIsNone(PLUGIN_LIBRARY_CACHE["payload"])

    def test_plugin_background_refresh_updates_cache(self):
        import threading
        from webapp_state import (
            PLUGIN_LIBRARY_CACHE,
            PLUGIN_LIBRARY_TTL,
            _PLUGIN_REFRESH_IN_PROGRESS,
            _build_public_state,
        )

        first = _build_public_state()
        PLUGIN_LIBRARY_CACHE["at"] = time.monotonic() - PLUGIN_LIBRARY_TTL - 5
        _PLUGIN_REFRESH_IN_PROGRESS["value"] = False
        done = threading.Event()

        def fake_plugins(_path, _hook, payload):
            done.set()
            return {"games": payload["games"]}

        with mock.patch("webapp_state.run_plugins", side_effect=fake_plugins):
            second = _build_public_state()
        self.assertEqual(len(second["games"]), len(first["games"]))
        self.assertTrue(done.wait(timeout=2.0))

    def test_build_known_media_set_breaks_on_cap_in_extra_root(self):
        from pkg.state import cache
        from webapp_state import bump_media_epoch

        extra_root = Path(self.tempdir.name) / "cap-extra"
        extra_root.mkdir()
        for index in range(5):
            (extra_root / f"img{index}.png").write_bytes(b"x")
        original_max = cache._KNOWN_MEDIA_MAX
        cache._KNOWN_MEDIA_MAX = 2
        previous = os.environ.get("OPENBOX_MEDIA_ROOTS")
        os.environ["OPENBOX_MEDIA_ROOTS"] = str(extra_root)
        try:
            bump_media_epoch()
            known = cache._build_known_media_set()
            self.assertLessEqual(len(known), 4)
        finally:
            cache._KNOWN_MEDIA_MAX = original_max
            if previous is None:
                os.environ.pop("OPENBOX_MEDIA_ROOTS", None)
            else:
                os.environ["OPENBOX_MEDIA_ROOTS"] = previous

    def test_media_dir_mtime_skips_unreadable_dirs(self):
        from pkg.state import cache

        media_dir = Path(self.tempdir.name) / "media" / "nested2"
        media_dir.mkdir(parents=True, exist_ok=True)
        with mock.patch("pkg.state.cache.os.stat", side_effect=OSError("stat")):
            self.assertGreaterEqual(cache._media_dir_mtime(), 0.0)

    def test_public_state_cached_reuses_populated_entry(self):
        from pkg.state.cache import PUBLIC_STATE_CACHE, _public_state_cached, _public_state_signature

        signature = _public_state_signature()
        seeded = {
            "signature": signature,
            "payload": {"games": [], "settings": {}},
            "raw": b"{}",
            "raw_gzip": b"{}",
            "games_by_id": {},
        }
        PUBLIC_STATE_CACHE.update(seeded)
        with mock.patch("pkg.state.cache._build_public_state", side_effect=AssertionError("should not rebuild")):
            cached = _public_state_cached()
        self.assertIs(cached["payload"], seeded["payload"])

    def test_load_state_view_populated_during_load(self):
        import openbox
        from webapp_state import STATE_VIEW_CACHE, load_state_view, load_state_readonly

        signature = openbox.STATE_STORE.signature()
        original = load_state_readonly

        def seeded_load():
            STATE_VIEW_CACHE.update({
                "signature": signature,
                "state": {"games": [{"game_id": "seed"}], "settings": {}},
            })
            return original()

        with mock.patch("webapp_state.load_state_readonly", side_effect=seeded_load):
            view = load_state_view()
        self.assertEqual(view["games"][0]["game_id"], "seed")

    def test_media_dir_mtime_tolerates_stat_failures(self):
        from pkg.state import cache

        with mock.patch("pkg.state.cache.os.stat", side_effect=OSError("stat")):
            self.assertGreaterEqual(cache._media_dir_mtime(), 0.0)

    def test_build_known_media_set_tolerates_scan_failures(self):
        from pkg.state import cache
        from webapp_state import bump_media_epoch

        with mock.patch("pkg.state.cache.os.scandir", side_effect=OSError("scandir")):
            bump_media_epoch()
            self.assertEqual(cache._build_known_media_set(), set())

    def test_public_state_cached_double_check(self):
        from webapp_state import PUBLIC_STATE_CACHE, _public_state_cached, _public_state_signature

        signature = _public_state_signature()
        PUBLIC_STATE_CACHE.update({
            "signature": signature,
            "payload": {"games": []},
            "raw": b"{}",
            "raw_gzip": b"{}",
            "games_by_id": {},
        })
        cached = _public_state_cached()
        self.assertEqual(cached["payload"]["games"], [])

    def test_public_state_cached_double_check_after_build(self):
        from pkg.state.cache import PUBLIC_STATE_CACHE, _public_state_cached, _public_state_signature

        signature = _public_state_signature()
        PUBLIC_STATE_CACHE.update({"signature": None, "raw": None, "payload": None})

        def seed_during_build():
            PUBLIC_STATE_CACHE.update({
                "signature": signature,
                "payload": {"games": [{"game_id": "seeded"}]},
                "raw": b"{}",
                "raw_gzip": b"{}",
                "games_by_id": {},
            })
            return {"games": [{"game_id": "built"}], "settings": {}}

        with mock.patch("webapp_state._build_public_state", side_effect=seed_during_build):
            cached = _public_state_cached()
        self.assertEqual(cached["payload"]["games"][0]["game_id"], "seeded")

    def test_load_state_view_double_check_returns_cached_copy(self):
        import openbox
        from webapp_state import STATE_VIEW_CACHE, load_state_view

        signature = openbox.STATE_STORE.signature()
        STATE_VIEW_CACHE.update({"signature": signature, "state": {"games": [], "settings": {}}})
        view = load_state_view()
        self.assertEqual(view["games"], [])

    def test_public_state_indexes_legacy_ids(self):
        from webapp_state import PUBLIC_STATE_CACHE, _public_state_cached, bump_media_epoch

        self.save_state({
            "games": [{
                "game_id": "g-main",
                "name": "Main",
                "path": "/bin/true",
                "legacy_game_ids": ["legacy-1"],
            }],
            "profiles": {}, "history": [], "settings": {}, "playlists": [],
        })
        bump_media_epoch()
        PUBLIC_STATE_CACHE.update({"signature": None, "raw": None})
        cached = _public_state_cached()
        by_id = cached["games_by_id"]
        self.assertIn("legacy-1", by_id)
        main_id = by_id["legacy-1"]["game_id"]
        self.assertIn(main_id, by_id)
        self.assertIs(by_id["legacy-1"], by_id[main_id])


if __name__ == "__main__":
    unittest.main()
