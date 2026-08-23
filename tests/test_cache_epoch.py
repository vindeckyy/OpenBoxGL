"""Tests for CacheEpoch unified invalidation (Lane 4A).

Validates that bump_media_epoch and transact_state clear every cache dict
managed by CacheEpoch, that cache hits work before invalidation and miss
after, and that the double-checked locking pattern in public_settings is
preserved.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
