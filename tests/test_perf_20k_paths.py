#!/usr/bin/env python3
"""P2-1/P2-2/P2-3: structural-sharing state snapshots and targeted journaling.

At 20k games a full ``copy.deepcopy`` of the library costs hundreds of
milliseconds.  ``load()``/``load_state_view()``/``update()`` now return
copy-on-write views over a detached snapshot: repeated reads share the
snapshot's structure, a write through a view detaches only the touched nodes,
and a state mutation replaces the shared snapshot.
"""

import copy
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_DATA_DIR = tempfile.mkdtemp(prefix="openbox-perf20k-")
os.environ["OPENBOX_DATA_DIR"] = _DATA_DIR

GAME_COUNT = 5000


def _make_games(count, prefix="game"):
    return [
        {
            "game_id": f"{prefix}-{index:05d}",
            "name": f"Game {index}",
            "platform": "PC",
            "path": f"/tmp/openbox-perf/{index}",
            "tags": ["perf"],
        }
        for index in range(count)
    ]


class SnapshotSharingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import openbox
        from state_store import STATE_SCHEMA_VERSION

        cls.openbox = openbox
        openbox.STATE_STORE.save({
            "schema_version": STATE_SCHEMA_VERSION,
            "games": _make_games(GAME_COUNT),
            "profiles": {},
            "history": [],
            "settings": {},
            "playlists": [],
            "queue": [],
            "notifications": [],
            "ui_state": {},
            "active_sessions": [],
        })

    def test_load_state_view_reuses_snapshot_structure(self):
        from pkg.state.cache import load_state_view, state_view_snapshot
        import state_store

        first = load_state_view()
        self.assertGreaterEqual(len(first["games"]), GAME_COUNT)
        snapshot_before = state_view_snapshot()
        self.assertIsNotNone(snapshot_before)

        with mock.patch.object(state_store.copy, "deepcopy", wraps=state_store.copy.deepcopy) as spy:
            for _ in range(5):
                view = load_state_view()
                self.assertGreaterEqual(len(view["games"]), GAME_COUNT)
            snapshot_after = state_view_snapshot()
        self.assertIs(snapshot_before, snapshot_after, "repeated reads must share one snapshot")
        self.assertEqual(spy.call_count, 0, "warm reads must not deep-copy the library")

    def test_view_mutation_is_isolated_and_mutation_invalidates(self):
        from pkg.state.cache import load_state_view, state_view_snapshot
        from webapp_state import transact_state

        view = load_state_view()
        original_name = view["games"][0]["name"]
        view["games"][0]["name"] = "Mutated Through View"
        self.assertEqual(load_state_view()["games"][0]["name"], original_name)

        before = state_view_snapshot()
        transact_state(lambda state: state.get("settings", {}).update({"perf_probe": 1}))
        after = state_view_snapshot()
        self.assertIsNot(before, after, "a mutation must replace the shared snapshot")
        self.assertEqual(load_state_view().get("settings", {}).get("perf_probe"), 1)

    def test_load_reuses_detached_snapshot(self):
        from state_store import JsonStateStore

        store = JsonStateStore(Path(_DATA_DIR) / "library-load.json")
        store.save({"games": _make_games(200, "load"), "profiles": {}, "history": [], "settings": {}, "playlists": []})
        store.load()
        first = store._snapshot_state
        self.assertIsNotNone(first)
        import state_store

        with mock.patch.object(state_store.copy, "deepcopy", wraps=state_store.copy.deepcopy) as spy:
            for _ in range(3):
                view = store.load()
                self.assertEqual(len(view["games"]), 200)
        self.assertIs(store._snapshot_state, first)
        self.assertEqual(spy.call_count, 0, "warm loads must not deep-copy the library")

    def test_load_view_mutation_does_not_alias_store(self):
        from state_store import JsonStateStore

        store = JsonStateStore(Path(_DATA_DIR) / "library-mutation.json")
        store.save({"games": _make_games(10, "mut"), "profiles": {}, "history": [], "settings": {}, "playlists": []})
        view = store.load()
        view["games"][0]["name"] = "Changed"
        view["settings"]["locale"] = "de"
        reloaded = store.load()
        self.assertEqual(reloaded["games"][0]["name"], "Game 0")
        self.assertNotIn("locale", reloaded["settings"])

    def test_update_does_not_deepcopy_the_catalog(self):
        from state_store import JsonStateStore

        store = JsonStateStore(Path(_DATA_DIR) / "library-update.json")
        store.save({"games": _make_games(1000, "upd"), "profiles": {}, "history": [], "settings": {}, "playlists": []})
        store.load()
        import state_store

        with mock.patch.object(state_store.copy, "deepcopy", wraps=state_store.copy.deepcopy) as spy:
            state, _ = store.update_with_result(
                lambda current: current["settings"].update({"warm": True}),
                isolate=False,
            )
            self.assertTrue(state["settings"]["warm"])
        self.assertEqual(spy.call_count, 0, "warm writes must not deep-copy the catalog")

    def test_repeated_reads_do_not_take_proportional_time(self):
        """A warm read must not scale with the catalog size like a deepcopy."""
        from pkg.state.cache import load_state_view

        load_state_view()
        started = time.perf_counter()
        for _ in range(20):
            self.assertGreaterEqual(len(load_state_view()["games"]), GAME_COUNT)
        elapsed = time.perf_counter() - started
        # Twenty deepcopies of 5k games are seconds; a shared view is fast.
        self.assertLess(elapsed, 2.0, f"20 warm views took {elapsed:.2f}s")


class SnapshotProtocolTests(unittest.TestCase):
    """Exercise the copy-on-write dict/list protocol used by view consumers."""

    def test_dict_protocol(self):
        from state_store import SnapshotDict, snapshot_value

        source = {
            "games": [{"game_id": "g1", "tags": ["a"]}, {"game_id": "g2"}],
            "settings": {"locale": "en"},
        }
        view = snapshot_value(source)
        self.assertIsInstance(view, SnapshotDict)
        self.assertIn("games", view)
        self.assertEqual(view.keys() and set(view.keys()), {"games", "settings"})
        self.assertEqual([key for key in view], ["games", "settings"])
        self.assertEqual(dict(view.items())["settings"], {"locale": "en"})
        self.assertEqual(list(view.values())[0][0]["game_id"], "g1")
        self.assertEqual(view.get("missing", "fallback"), "fallback")
        # setdefault returns the wrapped child and only detaches when inserting.
        child = view.setdefault("settings", {})
        child["locale"] = "de"
        self.assertEqual(source["settings"]["locale"], "en")
        view.setdefault("new_key", [])
        self.assertEqual(view["new_key"], [])
        # pop/popitem/clear return detached values.
        popped = view.pop("new_key")
        popped.append(1)
        self.assertEqual(view.get("new_key"), None)
        copy_view = view.copy()
        key, value = copy_view.popitem()
        self.assertIn(key, {"games", "settings"})
        copy_view.clear()
        self.assertEqual(len(copy_view), 0)
        self.assertEqual(len(view), 2)
        # update and delete detach.
        view.update({"extra": {"n": 1}})
        self.assertEqual(source.get("extra"), None)
        del view["extra"]
        self.assertNotIn("extra", view)
        # deepcopy produces plain containers.
        deep = copy.deepcopy(view)
        self.assertIs(type(deep), dict)
        self.assertIs(type(deep["games"]), list)
        self.assertIs(type(copy.copy(view)), SnapshotDict)
        # dict equality and json round-trip.
        self.assertEqual(SnapshotDict({"a": 1}) == {"a": 1}, True)
        self.assertEqual(json.loads(json.dumps(view))["settings"]["locale"], "de")

    def test_list_protocol(self):
        from state_store import SnapshotList, snapshot_value

        source = {"games": [{"n": 1}, {"n": 2}, {"n": 3}]}
        view = snapshot_value(source)
        games = view["games"]
        self.assertIsInstance(games, SnapshotList)
        self.assertEqual(games[1:], [{"n": 2}, {"n": 3}])
        self.assertEqual(len(games), 3)
        self.assertIn({"n": 2}, games)
        self.assertEqual(games.index({"n": 3}), 2)
        self.assertEqual(games.count({"n": 1}), 1)
        games.append({"n": 4})
        games.extend([{"n": 5}])
        games.insert(0, {"n": 0})
        self.assertEqual(len(source["games"]), 3)
        self.assertEqual([game["n"] for game in games], [0, 1, 2, 3, 4, 5])
        games[0] = {"n": -1}
        games.sort(key=lambda game: game["n"])
        games.reverse()
        self.assertEqual(games[0]["n"], 5)
        self.assertEqual(games.pop()["n"], -1)
        games.remove({"n": 1})
        games += [{"n": 9}]
        games *= 1
        del games[0]
        copied = games.copy()
        self.assertIsInstance(copied, SnapshotList)
        copied.clear()
        self.assertGreater(len(games), 0)
        games.clear()
        self.assertEqual(len(games), 0)
        self.assertEqual(len(source["games"]), 3)
        deep = copy.deepcopy(copied)
        self.assertIs(type(deep), list)


class JournalTargetedTests(unittest.TestCase):
    """P2-3: journaling projects only records the mutator touched."""

    def setUp(self):
        import openbox
        from state_store import STATE_SCHEMA_VERSION

        self.openbox = openbox
        openbox.STATE_STORE.save({
            "schema_version": STATE_SCHEMA_VERSION,
            "games": [
                {"game_id": "g1", "name": "Quake", "path": "/old"},
                {"game_id": "g2", "name": "Doom", "path": "/doom"},
            ],
            "profiles": {},
            "history": [],
            "settings": {"library_journal_enabled": True},
            "playlists": [],
            "queue": [],
            "notifications": [],
            "ui_state": {},
            "active_sessions": [],
        })

    def test_no_change_write_records_no_events_or_snapshots(self):
        import parity_library_sync

        with mock.patch.object(
            parity_library_sync, "capture_sync_snapshot", wraps=parity_library_sync.capture_sync_snapshot
        ) as spy:
            self.openbox.update_state(lambda state: state.get("settings", {}).update({"noop": 1}))
        self.assertEqual(spy.call_count, 0, "a settings-only write must not project the catalog")
        state = self.openbox.load_state()
        self.assertNotIn("library_sync", state)

    def test_real_change_still_journals(self):
        self.openbox.update_state(lambda state: state["games"][0].update({"name": "Quake II"}))
        state = self.openbox.load_state()
        events = state["library_sync"]["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(next(iter(events.values()))["catalog"]["name"], "Quake II")

    def test_deletion_journals_tombstone(self):
        self.openbox.update_state(lambda state: state["games"].pop(1))
        state = self.openbox.load_state()
        tombstones = [event for event in state["library_sync"]["events"].values() if event.get("tombstone")]
        self.assertEqual(len(tombstones), 1)
        self.assertEqual(tombstones[0]["sync_key"], "game:g2")


if __name__ == "__main__":
    unittest.main()
