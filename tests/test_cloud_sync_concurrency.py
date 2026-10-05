#!/usr/bin/env python3
"""S9 -- `sync_cloud` discarded playtime earned during the network read.

`sync_cloud` deep-copied the state, handed the copy to `sync_statistics` (which
reads a file on a possibly-networked folder -- seconds to minutes), and then
wrote six fields back from that copy with a blind assignment::

    for key in ("play_count", "playtime_seconds", "last_played", ...):
        if key in source:
            game[key] = source[key]

Any `finish_session` increment that landed in the live state during that window
was overwritten by the stale snapshot. The playtime was not deferred or
reconciled -- it was gone.

`cloud_sync._merge_game_stats` already had the right rule (counters take the
max, `last_played` takes the newer, progress/rating/favorite fill only when
empty). It was simply not being used on the write path. `apply_synced_stats`
now re-applies that same rule inside the mutator, against the current state.

The invariant: **a sync must never move a game backwards.**
"""

import copy
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cloud_sync import apply_synced_stats
from pkg.state import imports as state_imports


def _iso(offset_seconds=0):
    return (datetime.now() + timedelta(seconds=offset_seconds)).astimezone().isoformat(timespec="seconds")


def _game(game_id="g1", name="Alpha", **fields):
    game = {"game_id": game_id, "name": name, "play_count": 0, "playtime_seconds": 0, "last_played": ""}
    game.update(fields)
    return game


class ConcurrentIncrementTests(unittest.TestCase):
    """A session finishing mid-sync must survive it."""

    def _run_sync(self, snapshot_state, current_state, synced_at=None):
        """Run sync_cloud with a snapshot for the read and a live state for the write.

        `current_state` is mutated by `update_state`, exactly as the real
        mutator is -- so a test that passes here cannot be passing because the
        write was skipped.
        """
        written = {}

        def fake_update(mutator):
            mutator(current_state)
            written["state"] = current_state
            return current_state

        def fake_sync_statistics(working, folder, now=None):
            # The real read happened against `snapshot_state`'s copy. Merge the
            # cloud record into the working copy the way the real one does.
            from cloud_sync import _merge_game_stats

            for game in working["games"]:
                saved = _CLOUD.get(str(game.get("game_id")))
                if saved:
                    _merge_game_stats(game, saved)
            return {"synced_at": synced_at or _iso(), "changed": 1, "uploaded": True}

        with patch.object(state_imports, "load_state", return_value=copy.deepcopy(snapshot_state)), \
             patch.object(state_imports, "sync_statistics", side_effect=fake_sync_statistics), \
             patch.object(state_imports, "update_state", side_effect=fake_update):
            state_imports.sync_cloud()
        return written["state"]

    def setUp(self):
        self._cloud = {}
        global _CLOUD
        _CLOUD = self._cloud
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = str(Path(self._tmp.name))

    def _base_state(self, **game_fields):
        return {
            "games": [_game(**game_fields)],
            "settings": {"cloud_folder": self.folder},
        }

    def test_playtime_earned_during_the_sync_is_not_lost(self):
        """The reported bug: 500s of playtime disappears."""
        snapshot = self._base_state(playtime_seconds=1000, play_count=4, last_played=_iso(-3600))
        # The session finished while the folder was being read.
        current = copy.deepcopy(snapshot)
        current["games"][0]["playtime_seconds"] = 1500
        current["games"][0]["play_count"] = 5
        current["games"][0]["last_played"] = _iso(0)

        after = self._run_sync(snapshot, current)

        self.assertEqual(
            after["games"][0]["playtime_seconds"], 1500,
            "the session that finished during the cloud read was discarded",
        )
        self.assertEqual(after["games"][0]["play_count"], 5)

    def test_a_newer_local_last_played_is_kept(self):
        snapshot = self._base_state(last_played=_iso(-7200))
        current = copy.deepcopy(snapshot)
        current["games"][0]["last_played"] = _iso(0)

        after = self._run_sync(snapshot, current)
        self.assertEqual(after["games"][0]["last_played"], current["games"][0]["last_played"])

    def test_the_cloud_still_wins_when_it_is_ahead(self):
        """The fix must not turn the sync into a no-op."""
        self._cloud["g1"] = {
            "playtime_seconds": 9000, "play_count": 30,
            "last_played": _iso(60), "progress": "Completed", "rating": 4, "favorite": True,
        }
        snapshot = self._base_state(playtime_seconds=1000, play_count=4, last_played=_iso(-3600))
        current = copy.deepcopy(snapshot)

        after = self._run_sync(snapshot, current)
        game = after["games"][0]
        self.assertEqual(game["playtime_seconds"], 9000)
        self.assertEqual(game["play_count"], 30)
        self.assertEqual(game["last_played"], self._cloud["g1"]["last_played"])
        self.assertEqual(game["progress"], "Completed")
        self.assertEqual(game["rating"], 4)
        self.assertTrue(game["favorite"])

    def test_local_values_are_not_overwritten_by_lower_cloud_values(self):
        self._cloud["g1"] = {"playtime_seconds": 10, "play_count": 1, "last_played": _iso(-99999)}
        snapshot = self._base_state(playtime_seconds=5000, play_count=20, last_played=_iso(0))
        current = copy.deepcopy(snapshot)

        after = self._run_sync(snapshot, current)
        self.assertEqual(after["games"][0]["playtime_seconds"], 5000)
        self.assertEqual(after["games"][0]["play_count"], 20)

    def test_a_local_rating_survives_a_cloud_sync(self):
        """progress/rating/favorite fill only when the local value is missing."""
        self._cloud["g1"] = {"rating": 1, "progress": "10", "favorite": False}
        snapshot = self._base_state(rating=5, progress="Playing", favorite=True)
        current = copy.deepcopy(snapshot)

        after = self._run_sync(snapshot, current)
        self.assertEqual(after["games"][0]["rating"], 5)
        self.assertEqual(after["games"][0]["progress"], "Playing")
        self.assertTrue(after["games"][0]["favorite"])

    def test_the_sync_still_records_when_it_ran(self):
        snapshot = self._base_state()
        current = copy.deepcopy(snapshot)
        stamp = _iso(120)
        after = self._run_sync(snapshot, current, synced_at=stamp)
        self.assertEqual(after["settings"]["last_cloud_sync"], stamp)

    def test_a_game_added_during_the_sync_is_untouched(self):
        snapshot = self._base_state()
        current = copy.deepcopy(snapshot)
        current["games"].append(_game("g2", "Beta", playtime_seconds=777))
        self._cloud["g1"] = {"playtime_seconds": 50, "play_count": 1, "last_played": _iso(0)}

        after = self._run_sync(snapshot, current)
        by_id = {game["game_id"]: game for game in after["games"]}
        self.assertEqual(
            by_id["g2"]["playtime_seconds"], 777,
            "a game that did not exist when the sync started was modified",
        )

    def test_without_a_cloud_folder_it_still_refuses(self):
        with patch.object(state_imports, "load_state", return_value={"games": [], "settings": {}}):
            with self.assertRaises(ValueError):
                state_imports.sync_cloud()


class ApplySyncedStatsTests(unittest.TestCase):
    """The merge rule itself, independent of sync_cloud's plumbing."""

    def test_it_is_idempotent(self):
        game = _game(playtime_seconds=100, play_count=2, last_played=_iso(-10))
        record = {"playtime_seconds": 500, "play_count": 7, "last_played": _iso(0), "progress": "50"}
        apply_synced_stats(game, record)
        once = dict(game)
        apply_synced_stats(game, record)
        self.assertEqual(game, once, "re-applying a merged record changed the result")

    def test_it_never_lowers_a_counter(self):
        game = _game(playtime_seconds=999, play_count=50, last_played=_iso(0))
        apply_synced_stats(game, {"playtime_seconds": 1, "play_count": 1, "last_played": _iso(-99999)})
        self.assertEqual(game["playtime_seconds"], 999)
        self.assertEqual(game["play_count"], 50)

    def test_it_takes_the_newer_timestamp(self):
        game = _game(last_played=_iso(0))
        apply_synced_stats(game, {"last_played": _iso(600)})
        self.assertEqual(game["last_played"], _iso(600))

    def test_an_invalid_cloud_counter_is_ignored(self):
        game = _game(playtime_seconds=42, play_count=3)
        apply_synced_stats(game, {"playtime_seconds": "not a number", "play_count": None})
        self.assertEqual(game["playtime_seconds"], 42)
        self.assertEqual(game["play_count"], 3)


if __name__ == "__main__":
    unittest.main()
