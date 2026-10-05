#!/usr/bin/env python3
"""S14 -- editing platform categories left every game on its old category.

`_PLATFORM_CATEGORY_CACHE` was keyed on ``(platform, id(settings))``. Settings
are persisted **in place** -- the same dict is mutated and saved, so
``id(settings)`` never changes for the life of the process. `_invalidate_all`
cleared the media set, game projections, plugin library, public state and
settings, state view, facets, and the file probe, but not this dict; the
identifier appeared only at its declaration and at its one use.

So Settings -> edit `platform_categories` -> save left every game showing its old
category until the 5,000-entry clear or an app restart. The sibling
`_GAME_PROJECTION_CACHE` was checked for the same class and is correctly
cleared; this one was simply missed.

The invariant: **a game's category must follow the settings that are current,
without needing a restart or a cache clear.**
"""

import os
import sys
import unittest

sys.path.insert(0, str(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from pkg.state import cache as state_cache


def _game(name="Sonic", platform="Genesis"):
    return {
        "game_id": f"id-{name}",
        "name": name,
        "platform": platform,
        "path": f"/roms/{name}.md",
        "genre": "",
        "year": "",
        "developer": "",
        "publisher": "",
        "series": "",
        "collection": "",
        "description": "",
    }


def _project(game, settings):
    """Project one game, dropping the per-game projection cache first.

    ``_project_game`` keeps its own ``_GAME_PROJECTION_CACHE`` whose key does not
    mention settings at all. In production that is masked because saving
    settings calls ``_invalidate_all``, which clears it. Clearing it here models
    the real path and keeps this file about the platform-category cache, which is
    the one that has to be correct *on its own*.
    """
    state_cache._GAME_PROJECTION_CACHE.clear()
    return state_cache._project_game(
        game, 0, set(), set(), [], settings, state_cache.CACHE_EPOCH.media
    )


class PlatformCategoryCacheTests(unittest.TestCase):
    def setUp(self):
        state_cache._PLATFORM_CATEGORY_CACHE.clear()
        state_cache._GAME_PROJECTION_CACHE.clear()

    def tearDown(self):
        state_cache._PLATFORM_CATEGORY_CACHE.clear()
        state_cache._GAME_PROJECTION_CACHE.clear()

    def test_editing_platform_categories_changes_the_projected_category(self):
        """The reported bug: Settings -> platform categories -> save did nothing."""
        settings = {"platform_categories": {"Genesis": "Console"}}
        self.assertEqual(_project(_game(), settings)["platform_category"], "Console")

        # Same settings object, mutated and saved -- exactly what the settings
        # save path does. id(settings) is unchanged here, which is the whole
        # reason the old key could not notice.
        settings["platform_categories"]["Genesis"] = "Handheld"
        self.assertEqual(
            _project(_game(), settings)["platform_category"], "Handheld",
            "the game kept its old category after the setting changed",
        )

    def test_the_real_settings_save_path_changes_the_category(self):
        """End to end, the way the UI does it: mutate, save, invalidate."""
        settings = {"platform_categories": {"Genesis": "Console"}}
        self.assertEqual(_project(_game(), settings)["platform_category"], "Console")

        settings["platform_categories"]["Genesis"] = "Handheld"
        state_cache.CACHE_EPOCH._invalidate_all()      # what the save path calls
        self.assertEqual(
            _project(_game(), settings)["platform_category"], "Handheld",
        )

    def test_a_replaced_mapping_is_also_noticed(self):
        settings = {"platform_categories": {"Genesis": "Console"}}
        self.assertEqual(_project(_game(), settings)["platform_category"], "Console")
        settings["platform_categories"] = {"Genesis": "Rare"}
        self.assertEqual(_project(_game(), settings)["platform_category"], "Rare")

    def test_adding_a_new_platform_key_is_noticed(self):
        settings = {"platform_categories": {}}
        self.assertEqual(
            _project(_game(), settings)["platform_category"],
            state_cache.category_for_platform("Genesis", {}),
        )
        settings["platform_categories"]["Genesis"] = "Console"
        self.assertEqual(_project(_game(), settings)["platform_category"], "Console")

    def test_removing_an_override_falls_back_to_the_default(self):
        settings = {"platform_categories": {"Genesis": "Console"}}
        self.assertEqual(_project(_game(), settings)["platform_category"], "Console")
        settings["platform_categories"] = {}
        self.assertEqual(
            _project(_game(), settings)["platform_category"],
            state_cache.category_for_platform("Genesis", {}),
            "removing an override did not fall back to the built-in default",
        )

    def test_unrelated_settings_changes_do_not_change_the_category(self):
        settings = {"platform_categories": {"Genesis": "Console"}}
        self.assertEqual(_project(_game(), settings)["platform_category"], "Console")
        settings["theme"] = "harbor-light"
        settings["some_other_flag"] = True
        self.assertEqual(_project(_game(), settings)["platform_category"], "Console")

    def test_the_fingerprint_ignores_key_order(self):
        left = state_cache._platform_category_fingerprint(
            {"platform_categories": {"a": "1", "b": "2"}}
        )
        right = state_cache._platform_category_fingerprint(
            {"platform_categories": {"b": "2", "a": "1"}}
        )
        self.assertEqual(
            left, right,
            "dict insertion order changed the cache key, needlessly missing every entry",
        )

    def test_the_fingerprint_tolerates_odd_settings(self):
        for settings in (None, {}, {"platform_categories": None}, {"platform_categories": "oops"}, []):
            with self.subTest(settings=settings):
                state_cache._platform_category_fingerprint(settings)  # must not raise

    def test_invalidate_all_clears_the_platform_category_cache(self):
        """Belt and braces: the key is content-based, but the dict is also reset.

        Without this the dict would grow by one entry per distinct platform
        configuration the user has ever saved.
        """
        _project(_game(), {"platform_categories": {"Genesis": "Console"}})
        self.assertTrue(state_cache._PLATFORM_CATEGORY_CACHE)
        state_cache.CACHE_EPOCH._invalidate_all()
        self.assertEqual(
            state_cache._PLATFORM_CATEGORY_CACHE, {},
            "_invalidate_all still leaves the platform-category cache populated",
        )

    def test_the_cache_is_still_used_for_repeated_lookups(self):
        """The fix must not turn the cache into a no-op."""
        settings = {"platform_categories": {"Genesis": "Console"}}
        _project(_game("A", "Genesis"), settings)
        first = len(state_cache._PLATFORM_CATEGORY_CACHE)
        for index in range(20):
            _project(_game(f"G{index}", "Genesis"), settings)
        self.assertEqual(
            len(state_cache._PLATFORM_CATEGORY_CACHE), first,
            "the cache accumulated an entry per lookup; the fingerprint is not stable",
        )

    def test_distinct_configurations_get_distinct_entries(self):
        _project(_game(), {"platform_categories": {"Genesis": "Console"}})
        _project(_game(), {"platform_categories": {"Genesis": "Handheld"}})
        self.assertEqual(
            len(state_cache._PLATFORM_CATEGORY_CACHE), 2,
            "two different configurations share a cache entry",
        )


if __name__ == "__main__":
    unittest.main()
