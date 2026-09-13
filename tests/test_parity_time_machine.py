"""Tests for pkg/parity/parity_time_machine — journal mode, as-of replay, field revert (T2-core).

Covers the spec acceptance battery: replay-correctness fuzz, revert-as-event,
stale preview rejection, journal-off zero overhead, crash-mid-write safety,
honest horizon/corruption boundaries, and 20k-event pagination.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pkg.parity  # noqa: F401,E402  # registers the flat parity_* import finder
import parity_time_machine as tm  # noqa: E402
from pkg.parity import parity_library_sync as sync_mod  # noqa: E402
from pkg.parity.parity_library_sync import (  # noqa: E402
    SyncStaleError,
    SyncValidationError,
    bootstrap_local_catalog,
    build_catalog,
    journal_enabled,
    make_event,
    record_local_changes,
    stable_sync_key,
    state_token,
    sync_enabled,
)

EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _ts(seconds):
    return (EPOCH + timedelta(seconds=seconds)).isoformat()


def _game(name, game_id, **extra):
    game = {"game_id": game_id, "name": name, "platform": "PC", "path": f"/games/{game_id}/run.sh"}
    game.update(extra)
    return game


def _expected_catalog(game):
    """Fixture-side catalog projection matching the event-writer semantics."""
    catalog = build_catalog(game)
    key = stable_sync_key(game)
    if key.startswith("verified:") and not catalog.get("library_sync_id"):
        catalog["library_sync_id"] = key.split(":", 1)[1]
    return catalog


def _expected_view(games):
    return {stable_sync_key(game): _expected_catalog(game) for game in games}


def _record(state, before, after, seconds):
    return record_local_changes(state, copy.deepcopy(before), copy.deepcopy(after), now=_ts(seconds))


def _materialized_map(view):
    return {row["sync_key"]: {k: v for k, v in row.items() if k != "sync_key"} for row in view["games"]}


class JournalGateTests(unittest.TestCase):
    """openbox.update_state records the journal without sync; off means zero work."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        import openbox
        from state_store import JsonStateStore

        self.openbox = openbox
        self._prev_store = openbox.STATE_STORE
        self._prev_data = openbox.DATA
        self.data_path = Path(self.tmp.name) / "library.json"
        openbox.DATA = self.data_path
        openbox.STATE_STORE = JsonStateStore(self.data_path)
        self.addCleanup(self._restore)

    def _restore(self):
        self.openbox.STATE_STORE = self._prev_store
        self.openbox.DATA = self._prev_data

    def _seed(self, state):
        baseline = {
            "schema_version": 6, "games": [], "profiles": {}, "history": [],
            "settings": {}, "playlists": [], "ui_state": {}, "queue": [],
            "notifications": [], "active_sessions": [],
        }
        baseline.update(state)
        self.openbox.STATE_STORE.save(baseline)

    def test_journal_enabled_defaults_on_and_explicit_off(self):
        self.assertTrue(journal_enabled({"settings": {}}))
        self.assertTrue(journal_enabled({}))
        self.assertTrue(journal_enabled({"settings": {"library_journal_enabled": True}}))
        self.assertFalse(journal_enabled({"settings": {"library_journal_enabled": False}}))
        self.assertFalse(journal_enabled([]))
        # The sync gate itself is not widened by the journal setting.
        self.assertFalse(sync_enabled({"settings": {"library_journal_enabled": True}}))
        self.assertTrue(sync_enabled({"settings": {"library_sync_enabled": True}}))

    def test_journal_records_without_sync(self):
        self._seed({
            "games": [_game("Quake", "g1")],
            "settings": {"library_sync_enabled": False},
        })
        self.openbox.update_state(lambda state: state["games"][0].update({"name": "Quake II"}))
        state = self.openbox.load_state()
        self.assertFalse(sync_enabled(state))
        events = state["library_sync"]["events"]
        self.assertEqual(len(events), 1)
        event = next(iter(events.values()))
        self.assertEqual(event["catalog"]["name"], "Quake II")
        self.assertNotIn("path", event["catalog"])
        self.assertEqual(state["library_sync"]["heads"]["game:g1"], [event["event_id"]])

    def test_journal_off_zero_events_zero_overhead(self):
        self._seed({
            "games": [_game("Quake", "g1")],
            "settings": {"library_journal_enabled": False, "library_sync_enabled": False},
        })
        with mock.patch.object(sync_mod, "capture_sync_snapshot", wraps=sync_mod.capture_sync_snapshot) as spy:
            self.openbox.update_state(lambda state: state["games"][0].update({"name": "Quake II"}))
        state = self.openbox.load_state()
        self.assertEqual(state["games"][0]["name"], "Quake II")
        self.assertNotIn("library_sync", state)
        spy.assert_not_called()

    def test_journal_enable_bootstrap_records_existing_games(self):
        self._seed({
            "games": [_game("Quake", "g1"), _game("Doom", "g2")],
            "settings": {"library_journal_enabled": False},
        })
        self.openbox.update_state(
            lambda state: state["settings"].update({"library_journal_enabled": True})
        )
        state = self.openbox.load_state()
        events = state["library_sync"]["events"]
        self.assertEqual(len(events), 2)
        names = sorted(event["catalog"]["name"] for event in events.values())
        self.assertEqual(names, ["Doom", "Quake"])

    def test_crash_mid_write_leaves_journal_consistent(self):
        self._seed({
            "games": [_game("Quake", "g1")],
            "settings": {"library_sync_enabled": False},
        })
        store = self.openbox.STATE_STORE
        with mock.patch.object(store, "_write_unlocked", side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                self.openbox.update_state(lambda state: state["games"][0].update({"name": "Lost"}))
        reloaded = self.openbox.load_state()
        self.assertEqual(reloaded["games"][0]["name"], "Quake")
        self.assertNotIn("library_sync", reloaded)


class MaterializeTests(unittest.TestCase):
    def _state_with_events(self):
        state = {"games": [], "settings": {"library_journal_enabled": True}}
        bootstrap_local_catalog(state, now=_ts(0))
        return state

    def test_as_of_replays_adds_edits_and_deletes(self):
        state = self._state_with_events()
        before = [_game("Quake", "g1"), _game("Doom", "g2")]
        _record(state, [], before, 10)
        after = [dict(before[0], genre="FPS"), before[1]]
        _record(state, before, after, 20)
        after2 = [after[0]]  # delete g2
        _record(state, after, after2, 30)

        view_t10 = tm.materialize_as_of(state, _ts(15))
        self.assertEqual(_materialized_map(view_t10), _expected_view(before))
        view_t20 = tm.materialize_as_of(state, _ts(25))
        self.assertEqual(_materialized_map(view_t20), _expected_view(after))
        view_t30 = tm.materialize_as_of(state, _ts(35))
        self.assertEqual(_materialized_map(view_t30), _expected_view(after2))
        self.assertNotIn("path", view_t30["games"][0])

    def test_as_of_before_first_event_is_empty(self):
        state = self._state_with_events()
        _record(state, [], [_game("Quake", "g1")], 100)
        view = tm.materialize_as_of(state, _ts(50))
        self.assertEqual(view["games"], [])
        self.assertTrue(view["before_first_event"])
        self.assertEqual(view["earliest_at"], _ts(100))

    def test_as_of_no_date_materializes_current_catalog(self):
        state = self._state_with_events()
        games = [_game("Quake", "g1")]
        _record(state, [], games, 10)
        view = tm.materialize_as_of(state)
        self.assertEqual(_materialized_map(view), _expected_view(games))

    def test_corrupt_event_marks_honest_boundary(self):
        state = self._state_with_events()
        _record(state, [], [_game("Quake", "g1")], 10)
        events = state["library_sync"]["events"]
        bad = next(iter(events.values()))
        bad["catalog"]["name"] = "Tampered"  # breaks the content hash
        view = tm.materialize_as_of(state, _ts(20))
        self.assertTrue(view["corrupt"])
        self.assertIn("game:g1", view["unknown_keys"])
        self.assertEqual(view["games"], [])

    def test_as_of_before_horizon_is_marked(self):
        state = self._state_with_events()
        _record(state, [], [_game("Quake", "g1")], 10)
        state["library_sync"]["journal_horizon"] = _ts(500)
        view = tm.materialize_as_of(state, _ts(100))
        self.assertTrue(view["truncated"])
        self.assertEqual(view["horizon"], _ts(500))

    def test_as_of_cache_is_bounded(self):
        state = self._state_with_events()
        _record(state, [], [_game("Quake", "g1")], 10)
        tm._AS_OF_CACHE.clear()
        for step in range(tm.AS_OF_CACHE_SIZE + 5):
            tm.materialize_as_of(state, _ts(20 + step))
        self.assertLessEqual(len(tm._AS_OF_CACHE), tm.AS_OF_CACHE_SIZE)


class ListEventsTests(unittest.TestCase):
    def _journal(self):
        state = {"games": [], "settings": {"library_journal_enabled": True}}
        bootstrap_local_catalog(state, now=_ts(0))
        games = [_game("Quake", "g1"), _game("Doom", "g2")]
        _record(state, [], games, 10)
        edited = [dict(games[0], genre="FPS", year=1996), games[1]]
        _record(state, games, edited, 20)
        deleted = [edited[0]]
        _record(state, edited, deleted, 30)
        restored = deleted + [_game("Doom", "g2")]
        _record(state, deleted, restored, 40)
        return state

    def test_event_rows_have_kinds_and_field_diffs(self):
        state = self._journal()
        result = tm.list_events(state)
        rows = result["events"]
        self.assertEqual(result["total"], 5)
        kinds = [row["kind"] for row in rows]
        self.assertEqual(kinds, ["restore", "delete", "edit", "add", "add"])
        edit = rows[2]
        self.assertEqual(edit["changes"]["genre"], {"from": None, "to": "FPS"})
        self.assertEqual(edit["changes"]["year"], {"from": None, "to": 1996})
        self.assertEqual(edit["game_id"], "g1")
        self.assertEqual(edit["name"], "Quake")
        self.assertEqual(result["earliest_at"], _ts(10))
        self.assertEqual(result["latest_at"], _ts(40))

    def test_kind_and_game_filters(self):
        state = self._journal()
        deletes = tm.list_events(state, kind="delete")
        self.assertEqual(deletes["total"], 1)
        self.assertTrue(deletes["events"][0]["tombstone"])
        adds = tm.list_events(state, kind="add")
        self.assertEqual(adds["total"], 2)
        restores = tm.list_events(state, kind="restore")
        self.assertEqual(restores["total"], 1)
        only_g1 = tm.list_events(state, game_id="g1")
        self.assertEqual(only_g1["total"], 2)
        self.assertTrue(all(row["game_id"] == "g1" for row in only_g1["events"]))

    def test_days_filter_and_pagination(self):
        state = self._journal()
        recent = tm.list_events(state, days=0.0001, now=EPOCH + timedelta(seconds=40))
        self.assertEqual(recent["total"], 1)
        page = tm.list_events(state, limit=2, offset=0)
        self.assertEqual(len(page["events"]), 2)
        self.assertEqual(page["total"], 5)
        self.assertTrue(page["has_more"])
        rest = tm.list_events(state, limit=2, offset=4)
        self.assertEqual(len(rest["events"]), 1)
        self.assertFalse(rest["has_more"])

    def test_list_events_empty_and_flag_when_journal_off(self):
        result = tm.list_events({"games": [], "settings": {"library_journal_enabled": False}})
        self.assertEqual(result["events"], [])
        self.assertEqual(result["total"], 0)
        self.assertFalse(result["journal_enabled"])

    def test_gap_in_parent_chain_is_honest(self):
        state = self._journal()
        events = state["library_sync"]["events"]
        # Drop a non-head event to open a real gap in the retained DAG.
        heads = {eid for ids in state["library_sync"]["heads"].values() for eid in ids}
        droppable = [eid for eid, e in sorted(
            events.items(), key=lambda item: (item[1]["created_at"], item[1]["sequence"])
        ) if eid not in heads]
        dropped_id = droppable[0]
        events.pop(dropped_id)
        result = tm.list_events(state)
        self.assertTrue(result["gap"])
        broken = [
            row for row in result["events"]
            if dropped_id in row["parents"]
        ]
        self.assertTrue(broken)
        self.assertTrue(all(row["diff_base_missing"] for row in broken))


class RevertTests(unittest.TestCase):
    def _journal(self):
        state = {"games": [_game("Quake", "g1")], "settings": {"library_journal_enabled": True}}
        bootstrap_local_catalog(state, now=_ts(0))
        before = [_game("Quake", "g1")]
        after = [_game("Quake II", "g1", genre="FPS")]
        _record(state, before, after, 20)
        state["games"] = after
        return state

    def _event_for(self, state, key="game:g1", index=-1):
        events = [
            e for e in state["library_sync"]["events"].values() if e["sync_key"] == key
        ]
        events.sort(key=lambda e: (e["created_at"], e["sequence"], e["event_id"]))
        return events[index]

    def test_plan_revert_field_level(self):
        state = self._journal()
        add_event = self._event_for(state, index=0)
        plan = tm.plan_revert(state, event_id=add_event["event_id"], fields=["name"])
        self.assertEqual(plan["format"], "time-machine-revert-v1")
        self.assertEqual(plan["sync_key"], "game:g1")
        self.assertEqual(plan["base_token"], state_token(state))
        self.assertEqual(
            plan["changes"],
            [{"field": "name", "action": "set", "from": "Quake II", "to": "Quake"}],
        )
        self.assertFalse(plan["noop"])

    def test_plan_revert_unknown_event_and_field(self):
        state = self._journal()
        with self.assertRaises(SyncValidationError):
            tm.plan_revert(state, event_id="0" * 64)
        event = self._event_for(state)
        with self.assertRaises(SyncValidationError):
            tm.plan_revert(state, event_id=event["event_id"], fields=["path"])
        with self.assertRaises(SyncValidationError):
            tm.plan_revert(state, event_id=event["event_id"], fields=["game_id"])

    def test_apply_revert_appends_event_and_restores(self):
        state = self._journal()
        add_event = self._event_for(state, index=0)
        head_before = state["library_sync"]["heads"]["game:g1"][0]
        plan = tm.plan_revert(state, event_id=add_event["event_id"], fields=["name", "genre"])
        before_count = len(state["library_sync"]["events"])
        result = tm.apply_revert(state, plan)
        self.assertTrue(result["changed"])
        self.assertEqual(result["applied"], 2)
        self.assertEqual(state["games"][0]["name"], "Quake")
        self.assertNotIn("genre", state["games"][0])
        events = state["library_sync"]["events"]
        self.assertEqual(len(events), before_count + 1)
        new_event = events[result["event_id"]]
        self.assertEqual(new_event["catalog"]["name"], "Quake")
        self.assertEqual(new_event["parents"], [head_before])
        linkage = state["library_sync"]["journal_reverts"][result["event_id"]]
        self.assertEqual(linkage["source_event"], add_event["event_id"])
        # The revert event is timeline-visible.
        listed = tm.list_events(state)
        self.assertEqual(listed["events"][0]["event_id"], result["event_id"])
        self.assertTrue(listed["events"][0]["revert"])
        reverts = tm.list_events(state, kind="revert")
        self.assertEqual(reverts["total"], 1)

    def test_apply_revert_stale_token_rejected(self):
        state = self._journal()
        event = self._event_for(state, index=0)
        plan = tm.plan_revert(state, event_id=event["event_id"], fields=["name"])
        # A concurrent catalog edit must stale the previewed token.
        _record(state, state["games"], [_game("Quake III", "g1", genre="FPS")], 30)
        with self.assertRaises(SyncStaleError):
            tm.apply_revert(state, plan)

    def test_apply_revert_noop_emits_nothing(self):
        state = self._journal()
        current = self._event_for(state)  # latest event equals the live catalog
        plan = tm.plan_revert(state, event_id=current["event_id"], fields=["name"])
        self.assertTrue(plan["noop"])
        before_count = len(state["library_sync"]["events"])
        result = tm.apply_revert(state, plan)
        self.assertFalse(result["changed"])
        self.assertEqual(len(state["library_sync"]["events"]), before_count)

    def test_revert_undo_edit_restores_prior_values(self):
        state = self._journal()
        edit_event = self._event_for(state)
        plan = tm.plan_revert(state, event_id=edit_event["event_id"], undo=True)
        self.assertTrue(plan["undo"])
        self.assertEqual(
            {row["field"]: row["to"] for row in plan["changes"]},
            {"name": "Quake", "genre": None},
        )
        tm.apply_revert(state, plan)
        self.assertEqual(state["games"][0]["name"], "Quake")
        self.assertNotIn("genre", state["games"][0])

    def test_revert_deleted_game_restores_catalog_record(self):
        state = self._journal()
        before = [_game("Quake", "g1")]
        _record(state, before, [], 30)
        state["games"] = []
        live = self._event_for(state, index=0)  # the bootstrap add
        plan = tm.plan_revert(state, event_id=live["event_id"])
        self.assertFalse(plan["noop"])
        result = tm.apply_revert(state, plan)
        self.assertTrue(result["changed"])
        restored = state["games"][0]
        self.assertEqual(restored["game_id"], "g1")
        self.assertEqual(restored["name"], "Quake")
        self.assertNotIn("path", restored)  # local fields were never journaled

    def test_revert_to_tombstone_deletes(self):
        state = self._journal()
        before = [_game("Quake", "g1")]
        _record(state, before, [], 30)
        state["games"] = before  # game exists again out-of-band for the plan
        tombstone = self._event_for(state)
        self.assertTrue(tombstone["tombstone"])
        plan = tm.plan_revert(state, event_id=tombstone["event_id"])
        result = tm.apply_revert(state, plan)
        self.assertTrue(result["changed"])
        self.assertEqual(state["games"], [])
        self.assertTrue(state["library_sync"]["events"][result["event_id"]]["tombstone"])


class CompactionTests(unittest.TestCase):
    def _journal(self, count=6, start=0, step_days=100):
        state = {"games": [], "settings": {"library_journal_enabled": True}}
        bootstrap_local_catalog(state, now=_ts(start))
        games = [_game("Quake", "g1")]
        _record(state, [], games, start + 1)
        for i in range(count):
            day = start + 2 + i * step_days * 86400
            before = copy.deepcopy(games)
            games[0]["play_count_marker"] = i  # non-catalog fields do not record
            games[0]["notes"] = f"note-{i}"
            _record(state, before, games, day)
        state["games"] = copy.deepcopy(games)
        return state

    def test_compact_drops_expired_events_and_sets_horizon(self):
        state = self._journal(count=4, start=0, step_days=100)
        meta = state["library_sync"]
        self.assertEqual(len(meta["events"]), 5)
        now = EPOCH + timedelta(days=10_000)
        report = tm.compact_journal(state, now=now)
        self.assertTrue(report["dropped"] >= 1)
        self.assertEqual(report["remaining"], len(meta["events"]))
        self.assertTrue(meta["journal_horizon"])
        # Current heads are never dropped: recording can keep chaining.
        heads = {eid for ids in meta["heads"].values() for eid in ids}
        self.assertTrue(heads <= set(meta["events"]))
        # As-of beyond the horizon still resolves; before it is honestly marked.
        view = tm.materialize_as_of(state, now.isoformat())
        self.assertEqual(view["count"], 1)
        early = tm.materialize_as_of(state, _ts(3))
        self.assertTrue(early["truncated"])

    def test_compact_preserves_ancestry_when_sync_enabled(self):
        state = self._journal(count=4, start=0, step_days=100)
        state["settings"]["library_sync_enabled"] = True
        meta = state["library_sync"]
        now = EPOCH + timedelta(days=10_000)
        tm.compact_journal(state, now=now)
        events = meta["events"]
        for event in events.values():
            for parent in event["parents"]:
                self.assertIn(parent, events)

    def test_compact_caps_total_events(self):
        state = self._journal(count=8, start=0, step_days=1)
        with mock.patch.object(tm, "JOURNAL_MAX_EVENTS", 4):
            report = tm.compact_journal(state, now=EPOCH + timedelta(days=10_000))
        self.assertEqual(report["remaining"], 4)
        meta = state["library_sync"]
        heads = {eid for ids in meta["heads"].values() for eid in ids}
        self.assertTrue(heads <= set(meta["events"]))

    def test_maybe_compact_is_cheap_and_gated(self):
        state = {"games": [], "settings": {"library_journal_enabled": True}}
        bootstrap_local_catalog(state, now=_ts(0))
        games = [_game("Quake", "g1")]
        _record(state, [], games, 10)
        _record(state, games, [_game("Quake", "g1", notes="n")], 20)
        self.assertIsNone(tm.maybe_compact_journal(state, now=EPOCH))
        meta = state["library_sync"]
        self.assertIn("journal_compact_checked_at", meta)
        # A second call inside the check interval stays cheap.
        self.assertIsNone(tm.maybe_compact_journal(state, now=EPOCH + timedelta(hours=1)))
        with mock.patch.object(tm, "JOURNAL_MAX_EVENTS", 1):
            report = tm.maybe_compact_journal(state, now=EPOCH + timedelta(days=2))
        self.assertIsNotNone(report)
        self.assertEqual(report["remaining"], 1)


class PerfTests(unittest.TestCase):
    def test_20k_events_paginate_and_materialize(self):
        state = {"games": [], "settings": {"library_journal_enabled": True}}
        meta = sync_mod._metadata(state)
        sync_mod.ensure_device_id(state)
        events, heads, outbox = meta["events"], meta["heads"], meta["outbox"]
        keys = [f"game:g{i}" for i in range(500)]
        seq = 1
        for index in range(20_000):
            key = keys[index % len(keys)]
            event = make_event(
                device_id=meta["device_id"], sequence=seq, sync_key=key,
                parents=heads.get(key, []),
                catalog={"game_id": key.split(":", 1)[1], "name": f"G{index}", "platform": "PC"},
                created_at=_ts(index),
            )
            seq += 1
            events[event["event_id"]] = event
            heads[key] = [event["event_id"]]
            outbox.append(event)
        page = tm.list_events(state, limit=200, offset=0)
        self.assertEqual(page["total"], 20_000)
        self.assertEqual(len(page["events"]), 200)
        self.assertTrue(page["has_more"])
        last = tm.list_events(state, limit=200, offset=19_800)
        self.assertEqual(len(last["events"]), 200)
        self.assertFalse(last["has_more"])
        view = tm.materialize_as_of(state, _ts(19_999))
        self.assertEqual(view["count"], 500)
        view = tm.materialize_as_of(state, _ts(19_999))
        self.assertEqual(view["count"], 500)


class TimeMachineHttpTests(unittest.TestCase):
    """Real HTTP boundary for the v2 time-machine routes."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.prev_dir = os.environ.get("OPENBOX_DATA_DIR")
        os.environ["OPENBOX_DATA_DIR"] = cls.tmp.name
        import openbox
        import web_app
        from state_store import JsonStateStore

        cls.openbox = openbox
        cls.prev_store = openbox.STATE_STORE
        cls.prev_data = openbox.DATA
        openbox.DATA = Path(cls.tmp.name) / "library.json"
        openbox.STATE_STORE = JsonStateStore(openbox.DATA)
        web_app.TOKEN = "tm-http-token"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web_app.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.openbox.STATE_STORE = cls.prev_store
        cls.openbox.DATA = cls.prev_data
        cls.tmp.cleanup()
        if cls.prev_dir is None:
            os.environ.pop("OPENBOX_DATA_DIR", None)
        else:
            os.environ["OPENBOX_DATA_DIR"] = cls.prev_dir

    def setUp(self):
        state = {
            "schema_version": 6,
            "games": [_game("Quake", "g1"), _game("Doom", "g2")],
            "profiles": {}, "history": [], "settings": {}, "playlists": [],
            "ui_state": {}, "queue": [], "notifications": [], "active_sessions": [],
        }
        bootstrap_local_catalog(state)
        self.openbox.STATE_STORE.save(state)
        self.openbox.update_state(
            lambda state: state["games"][0].update({"name": "Quake Remastered"})
        )

    def request(self, method, path, payload=None, token="tm-http-token"):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data,
            headers={"Content-Type": "application/json",
                     **({"X-OpenBox-Token": token} if token else {})},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            finally:
                error.close()

    def test_events_route_lists_journal(self):
        status, body = self.request("GET", "/api/v2/library/time-machine/events")
        self.assertEqual(status, 200)
        self.assertTrue(body["journal_enabled"])
        self.assertEqual(body["total"], 3)
        row = body["events"][0]
        self.assertEqual(row["kind"], "edit")
        self.assertEqual(row["changes"]["name"], {"from": "Quake", "to": "Quake Remastered"})

    def test_events_route_pagination_and_kind_filter(self):
        status, body = self.request(
            "GET", "/api/v2/library/time-machine/events?kind=delete&limit=10")
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 0)
        status, body = self.request(
            "GET", "/api/v2/library/time-machine/events?limit=1&offset=0")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["events"]), 1)
        status, body = self.request(
            "GET", "/api/v2/library/time-machine/events?days=1")
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 3)

    def test_events_route_requires_auth(self):
        status, body = self.request("GET", "/api/v2/library/time-machine/events", token=None)
        self.assertIn(status, (401, 403, 429))

    def test_as_of_route_roundtrip(self):
        status, body = self.request(
            "GET", "/api/v2/library/time-machine/as-of?date=1999-01-01")
        self.assertEqual(status, 200)
        self.assertEqual(body["games"], [])
        self.assertTrue(body["before_first_event"])
        status, body = self.request(
            "GET", "/api/v2/library/time-machine/as-of?date=2999-01-01")
        self.assertEqual(status, 200)
        names = sorted(game["name"] for game in body["games"])
        self.assertEqual(names, ["Doom", "Quake Remastered"])

    def test_as_of_route_requires_date(self):
        status, body = self.request("GET", "/api/v2/library/time-machine/as-of")
        self.assertEqual(status, 400)
        status, body = self.request(
            "GET", "/api/v2/library/time-machine/as-of?date=not-a-date")
        self.assertEqual(status, 400)

    def test_revert_preview_then_apply_and_stale(self):
        status, body = self.request("GET", "/api/v2/library/time-machine/events")
        event_id = body["events"][0]["event_id"]
        # Preview does not mutate.
        status, preview = self.request(
            "POST", "/api/v2/library/time-machine/revert",
            {"event_id": event_id, "fields": ["name"], "undo": True})
        self.assertEqual(status, 200)
        self.assertTrue(preview["preview"])
        plan = preview["plan"]
        self.assertEqual(plan["changes"][0]["to"], "Quake")
        state = self.openbox.load_state()
        self.assertEqual(state["games"][0]["name"], "Quake Remastered")
        # Apply with the previewed token.
        status, applied = self.request(
            "POST", "/api/v2/library/time-machine/revert",
            {"event_id": event_id, "fields": ["name"], "undo": True, "apply": True,
             "base_token": plan["base_token"]})
        self.assertEqual(status, 200)
        self.assertTrue(applied["changed"])
        state = self.openbox.load_state()
        self.assertEqual(state["games"][0]["name"], "Quake")
        # Replaying the stale token is rejected.
        status, stale = self.request(
            "POST", "/api/v2/library/time-machine/revert",
            {"event_id": event_id, "fields": ["name"], "undo": True, "apply": True,
             "base_token": plan["base_token"]})
        self.assertEqual(status, 409)
        self.assertEqual(stale["code"], "TM_REVERT_STALE")

    def test_revert_rejects_bad_requests(self):
        status, _ = self.request("POST", "/api/v2/library/time-machine/revert", {})
        self.assertEqual(status, 400)
        status, body = self.request("GET", "/api/v2/library/time-machine/events")
        event_id = body["events"][0]["event_id"]
        status, _ = self.request(
            "POST", "/api/v2/library/time-machine/revert",
            {"event_id": event_id, "fields": ["path"]})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
