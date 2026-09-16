#!/usr/bin/env python3
"""P1-8: the game projection cache keys on content, never on ``id(game)``.

CPython recycles dict addresses after garbage collection and transactions
mutate game dicts in place, so an identity-keyed cache could return another
game's projection (or a stale one).
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pkg.state import cache  # noqa: E402

BASE_GAME = {
    "game_id": "game-content-key",
    "name": "Keyed",
    "path": "/tmp/keyed",
    "platform": "PC",
}


class ProjectionCacheContentKeyTests(unittest.TestCase):
    def setUp(self):
        cache._GAME_PROJECTION_CACHE.clear()
        self.addCleanup(cache._GAME_PROJECTION_CACHE.clear)

    def _project(self, game, index=0, priority=None):
        return cache._project_game(game, index, set(), set(), priority, {}, 0)

    def test_distinct_games_at_same_address_do_not_collide(self):
        game_a = {**BASE_GAME, "genre": "Action", "developer": "Studio A"}
        game_b = {**BASE_GAME, "genre": "Action", "developer": "Studio B"}
        with mock.patch("pkg.state.cache.id", return_value=4242):
            first = self._project(game_a)
            second = self._project(game_b)
        self.assertEqual(first["developer"], "Studio A")
        self.assertEqual(second["developer"], "Studio B")

    def test_in_place_mutation_invalidates_projection(self):
        game = {**BASE_GAME, "genre": "Action"}
        first = self._project(game)
        game["genre"] = "Puzzle"
        second = self._project(game)
        self.assertEqual(first["genre"], "Action")
        self.assertEqual(second["genre"], "Puzzle")

    def test_nested_collection_change_invalidates_projection(self):
        game = {**BASE_GAME, "tags": [], "versions": [], "save_paths": []}
        first = self._project(game)
        game["tags"] = ["co-op"]
        game["versions"] = [{"path": "/tmp/v2.bin"}]
        game["save_paths"] = ["/tmp/save.dat"]
        second = self._project(game)
        self.assertEqual(first["tags"], [])
        self.assertFalse(first["has_versions"])
        self.assertFalse(first["has_saves"])
        self.assertEqual(second["tags"], ["co-op"])
        self.assertTrue(second["has_versions"])
        self.assertTrue(second["has_saves"])

    def test_same_content_shares_cached_projection(self):
        first = self._project(dict(BASE_GAME))
        second = self._project(dict(BASE_GAME))
        self.assertIs(first, second)

    def test_unencodable_projection_inputs_still_keyed(self):
        circular = []
        circular.append(circular)
        first = self._project({**BASE_GAME, "custom_fields": circular})
        self.assertIn("custom_fields", first)
        second = self._project({**BASE_GAME, "custom_fields": {"note": "dict now"}})
        self.assertIsNot(first, second)

    def test_index_and_media_epoch_still_differentiate(self):
        first = self._project(dict(BASE_GAME), index=0)
        second = self._project(dict(BASE_GAME), index=1)
        self.assertIsNot(first, second)
        third = cache._project_game(dict(BASE_GAME), 0, set(), set(), None, {}, 1)
        self.assertIsNot(first, third)


class ProjectionCacheTrackedFieldTests(unittest.TestCase):
    """Every projected input that used to be excluded must change the key."""

    def setUp(self):
        cache._GAME_PROJECTION_CACHE.clear()
        self.addCleanup(cache._GAME_PROJECTION_CACHE.clear)

    def _project(self, game):
        return cache._project_game(game, 0, set(), set(), None, {}, 0)

    def test_previously_unkeyed_scalar_changes_projection(self):
        cases = {
            "developer": ("Studio A", "Studio B"),
            "publisher": ("Pub A", "Pub B"),
            "genre": ("RPG", "Shooter"),
            "description": ("One", "Two"),
            "region": ("USA", "EUR"),
            "play_mode": ("Singleplayer", "Multiplayer"),
            "esrb": ("E", "M"),
            "extract_archive": (False, True),
            "broken": (False, True),
            "wikipedia_url": ("", "https://example.invalid/game"),
        }
        for field, (before, after) in cases.items():
            with self.subTest(field=field):
                game = {**BASE_GAME, field: before}
                first = self._project(game)
                mutated = {**BASE_GAME, field: after}
                second = self._project(mutated)
                self.assertNotEqual(first, second, field)

    def test_previously_unkeyed_collection_changes_projection(self):
        cases = {
            "tags": ([], ["co-op"]),
            "applications": ([], [{"path": "/tmp/tool.sh"}]),
            "versions": ([], [{"path": "/tmp/v2.bin"}]),
            "save_paths": ([], ["/tmp/save.dat"]),
            "alternate_names": ([], ["Alt Title"]),
            "legacy_game_ids": ([], ["game-legacy"]),
            "custom_fields": ({}, {"region_lock": "PAL"}),
        }
        for field, (before, after) in cases.items():
            with self.subTest(field=field):
                first = self._project({**BASE_GAME, field: before})
                second = self._project({**BASE_GAME, field: after})
                self.assertNotEqual(first, second, field)


if __name__ == "__main__":
    unittest.main()
