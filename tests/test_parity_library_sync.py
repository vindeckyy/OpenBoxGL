"""Focused tests for the causal, catalog-only library synchronization engine."""

from __future__ import annotations

import copy
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pkg.parity.parity_library_sync import (
    SYNC_DIRECTORY,
    SyncFolderError,
    SyncStaleError,
    SyncValidationError,
    _common_base,
    _merge_catalogs,
    apply_sync,
    bootstrap_local_catalog,
    build_catalog,
    capture_sync_snapshot,
    event_id,
    ensure_device_id,
    ingest_event_set,
    make_event,
    preview_sync,
    publish_outbox,
    read_events,
    record_local_changes,
    stable_sync_key,
    sync_enabled,
    sync_folder,
    validate_event,
    validate_event_set,
    write_event,
    write_events,
)


def game(name="Quake", *, game_id="g1", platform="PC", path="/device/game", favorite=False):
    return {
        "game_id": game_id,
        "name": name,
        "platform": platform,
        "path": path,
        "favorite": favorite,
        "play_count": 12,
        "launch": "dangerous command",
        "plugin_custom": "must stay local",
    }


class LibrarySyncTests(unittest.TestCase):
    def test_catalog_projection_and_identity_never_use_local_fields(self):
        row = build_catalog(game())
        self.assertEqual(row["name"], "Quake")
        self.assertNotIn("path", row)
        self.assertNotIn("favorite", row)
        self.assertNotIn("launch", row)
        self.assertNotIn("plugin_custom", row)
        self.assertEqual(stable_sync_key({"steam_app_id": 42, "game_id": "local"}), "game:local")
        self.assertEqual(
            stable_sync_key({"steam_app_id": 42, "library_sync_id": "verified-steam-42"}),
            "verified:verified-steam-42",
        )
        with self.assertRaises(SyncValidationError):
            stable_sync_key({"steam_app_id": 42})
        with self.assertRaises(SyncValidationError):
            stable_sync_key({"name": "same title", "path": "/same"})

    def test_device_id_is_persistent_and_snapshot_is_detached(self):
        state = {"games": [game()]}
        first = ensure_device_id(state)
        self.assertEqual(first, ensure_device_id(state))
        snapshot = capture_sync_snapshot(state)
        state["games"][0]["name"] = "Changed"
        self.assertEqual(snapshot[0]["name"], "Quake")

    def test_bootstrap_is_idempotent_and_ambiguous_identity_is_metadata_error(self):
        state = {"games": [game()]}
        first = bootstrap_local_catalog(state, now="2026-01-01T00:00:00+00:00")
        second = bootstrap_local_catalog(state, now="2026-01-02T00:00:00+00:00")
        self.assertEqual(first["changed"], 1)
        self.assertEqual(second["changed"], 0)
        self.assertEqual(len(state["library_sync"]["outbox"]), 1)
        ambiguous = {"games": [{"steam_app_id": "same"}, {"steam_app_id": "same"}]}
        result = bootstrap_local_catalog(ambiguous)
        self.assertEqual(result["changed"], 0)
        self.assertEqual(ambiguous["library_sync"]["error"]["code"], "SYNC_IDENTITY_AMBIGUOUS")

    def test_reenable_reconciles_changes_made_while_sync_was_disabled(self):
        state = {"games": [game()]}
        bootstrap_local_catalog(state, now="2026-01-01T00:00:00+00:00")
        state["games"][0]["name"] = "Changed while disabled"
        state["games"].append(game(name="Added while disabled", game_id="g2"))
        result = bootstrap_local_catalog(state, now="2026-01-03T00:00:00+00:00")
        self.assertEqual(result["changed"], 2)
        self.assertEqual({event["catalog"]["name"] for event in result["events"]}, {
            "Changed while disabled", "Added while disabled",
        })
        alias_state = {"games": [{"game_id": "alias", "name": "Alias", "sync_identity": "one"}]}
        bootstrap_local_catalog(alias_state, now="2026-01-01T00:00:00+00:00")
        self.assertEqual(bootstrap_local_catalog(alias_state, now="2026-01-02T00:00:00+00:00")["changed"], 0)

    def test_unchanged_and_local_only_changes_do_not_make_revisions(self):
        state = {"games": [game()]}
        before = copy.deepcopy(state)
        result = record_local_changes(state, before, state)
        self.assertEqual(result["changed"], 0)
        changed = copy.deepcopy(state)
        changed["games"][0]["favorite"] = True
        changed["games"][0]["path"] = "/other-device"
        result = record_local_changes(state, before, changed)
        self.assertEqual(result["changed"], 0)
        self.assertEqual(state["library_sync"].get("outbox", []), [])

    def test_two_devices_add_and_repeated_pull_are_idempotent(self):
        source = {"games": [game(path="/desktop")]}
        record_local_changes(source, [], source["games"], now="2026-09-08T00:00:00+00:00")
        destination = {"games": []}
        with tempfile.TemporaryDirectory() as directory:
            publish_outbox(source, directory)
            events = read_events(directory)
            plan = preview_sync(destination, events)
            applied = apply_sync(destination, plan)
            self.assertEqual(applied["applied"], 1)
            self.assertEqual(destination["games"][0]["name"], "Quake")
            self.assertNotIn("path", destination["games"][0])
            repeat = preview_sync(destination, events)
            self.assertEqual(repeat["changes"], [])

    def test_disjoint_concurrent_fields_merge_and_keep_local_path(self):
        source = {"games": [game()]}
        record_local_changes(source, [], source["games"], now="2026-01-01T00:00:00+00:00")
        destination = {"games": []}
        with tempfile.TemporaryDirectory() as directory:
            publish_outbox(source, directory)
            apply_sync(destination, preview_sync(destination, read_events(directory)))
        before_source = copy.deepcopy(source["games"])
        source["games"][0]["name"] = "Desktop title"
        record_local_changes(source, before_source, source["games"], now="2026-01-02T00:00:00+00:00")
        before_destination = copy.deepcopy(destination["games"])
        destination["games"][0]["platform"] = "Handheld"
        destination["games"][0]["path"] = "/handheld/game"
        record_local_changes(destination, before_destination, destination["games"], now="2026-01-02T00:01:00+00:00")
        with tempfile.TemporaryDirectory() as remote:
            publish_outbox(source, remote)
            plan = preview_sync(destination, read_events(remote))
            self.assertEqual(plan["conflicts"], [])
            apply_sync(destination, plan)
        self.assertEqual(destination["games"][0]["name"], "Desktop title")
        self.assertEqual(destination["games"][0]["platform"], "Handheld")
        self.assertEqual(destination["games"][0]["path"], "/handheld/game")

    def test_same_field_conflict_preserves_alternatives_until_choice(self):
        base = {"games": [game()]}
        record_local_changes(base, [], base["games"], now="2026-01-01T00:00:00+00:00")
        left = copy.deepcopy(base)
        right = copy.deepcopy(base)
        right["library_sync"]["device_id"] = "device-right"
        for state, value in ((left, "left"), (right, "right")):
            before = copy.deepcopy(state["games"])
            state["games"][0]["name"] = value
            record_local_changes(state, before, state["games"], now="2026-01-02T00:00:00+00:00")
        with tempfile.TemporaryDirectory() as remote:
            publish_outbox(right, remote)
            plan = preview_sync(left, read_events(remote))
            self.assertEqual(plan["counts"]["conflicts"], 1)
            unchanged = apply_sync(left, plan)
            self.assertEqual(unchanged["conflicts"][0]["local"], "left")
            self.assertEqual(unchanged["conflicts"][0]["remote"], "right")
            apply_sync(left, plan, conflicts={"game:g1": {"name": "remote"}})
        self.assertEqual(left["games"][0]["name"], "right")
        self.assertTrue(left["library_sync"]["outbox"])

    def test_delete_stale_publication_cannot_resurrect_and_readd_is_new_event(self):
        source = {"games": [game()]}
        record_local_changes(source, [], source["games"], now="2026-01-01T00:00:00+00:00")
        destination = {"games": []}
        with tempfile.TemporaryDirectory() as initial:
            publish_outbox(source, initial)
            apply_sync(destination, preview_sync(destination, read_events(initial)))
        before = copy.deepcopy(source["games"])
        source["games"].clear()
        deletion = record_local_changes(source, before, source["games"], now="2026-01-02T00:00:00+00:00")["events"][0]
        with tempfile.TemporaryDirectory() as remote:
            publish_outbox(source, remote)
            apply_sync(destination, preview_sync(destination, read_events(remote)))
            self.assertEqual(destination["games"], [])
            # The old event is an ancestor of deletion, so it produces no plan.
            self.assertEqual(preview_sync(source, read_events(remote))["changes"], [])
        source["games"].append(game(name="Restored"))
        restored = record_local_changes(source, [], source["games"], now="2026-01-03T00:00:00+00:00")["events"][0]
        self.assertIn(deletion["event_id"], restored["parents"])

    def test_preview_rejects_changed_base_and_apply_is_pure_on_failure(self):
        state = {"games": []}
        event = make_event(device_id="a", sequence=1, sync_key="game:g1", catalog={"game_id": "g1", "name": "Q"})
        plan = preview_sync(state, [event])
        state["games"].append(game())
        with self.assertRaises(SyncStaleError):
            apply_sync(state, plan)
        self.assertEqual(state["games"][0]["name"], "Quake")

    def test_apply_recomputes_tampered_change_catalog(self):
        state = {"games": []}
        event = make_event(
            device_id="remote", sequence=1, sync_key="game:g1",
            catalog={"game_id": "g1", "name": "Trusted"},
        )
        plan = preview_sync(state, [event])
        plan["changes"][0]["merged"] = {"game_id": "g1", "name": "Tampered"}
        applied = apply_sync(state, plan)
        self.assertEqual(applied["applied"], 1)
        self.assertEqual(state["games"][0]["name"], "Trusted")

    def test_causal_multi_head_merge_preserves_parents_and_pending_conflicts(self):
        base = {"games": [game(name="base")]}
        record_local_changes(base, [], base["games"], device="base", now="2026-01-01T00:00:00+00:00")
        parent = base["library_sync"]["outbox"][0]
        first = make_event(
            device_id="one", sequence=1, sync_key="game:g1", parents=[parent["event_id"]],
            catalog={"game_id": "g1", "name": "one"}, created_at="2026-01-03T00:00:00+00:00",
        )
        second = make_event(
            device_id="two", sequence=1, sync_key="game:g1", parents=[parent["event_id"]],
            catalog={"game_id": "g1", "name": "two"}, created_at="2026-01-02T00:00:00+00:00",
        )
        plan = preview_sync(base, [parent, first, second])
        self.assertEqual(len(plan["changes"][0]["event_ids"]), 2)
        result = apply_sync(base, plan)
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertTrue(base["library_sync"]["pending_conflicts"])
        repeat = preview_sync(base, [parent, first, second])
        self.assertTrue(repeat["pending_conflicts"])
        self.assertTrue(preview_sync(base, []) ["conflicts"])
        resolved = apply_sync(base, repeat, conflicts={"game:g1": {"name": "remote"}})
        self.assertEqual(resolved["applied"], 1)
        head = base["library_sync"]["heads"]["game:g1"][0]
        resolution = base["library_sync"]["events"][head]
        self.assertEqual(set(resolution["parents"]), {first["event_id"], second["event_id"], parent["event_id"]})

    def test_common_base_uses_causal_maximum_over_timestamp(self):
        root = make_event(
            device_id="base", sequence=1, sync_key="game:g1", catalog={"game_id": "g1", "name": "root"},
            created_at="2026-01-03T00:00:00+00:00",
        )
        middle = make_event(
            device_id="base", sequence=2, sync_key="game:g1", parents=[root["event_id"]],
            catalog={"game_id": "g1", "name": "middle"}, created_at="2026-01-01T00:00:00+00:00",
        )
        left = make_event(
            device_id="left", sequence=1, sync_key="game:g1", parents=[middle["event_id"]],
            catalog={"game_id": "g1", "name": "left"}, created_at="2026-01-02T00:00:00+00:00",
        )
        right = make_event(
            device_id="right", sequence=1, sync_key="game:g1", parents=[middle["event_id"]],
            catalog={"game_id": "g1", "name": "right"}, created_at="2026-01-04T00:00:00+00:00",
        )
        events = {item["event_id"]: item for item in (root, middle, left, right)}
        self.assertEqual(_common_base(left["event_id"], right["event_id"], events)["name"], "middle")

    def test_criss_cross_common_bases_keep_conflicting_resolutions(self):
        root = make_event(device_id="base", sequence=1, sync_key="game:g1", catalog={"game_id": "g1", "name": "root"})
        left = make_event(device_id="left", sequence=1, sync_key="game:g1", parents=[root["event_id"]], catalog={"game_id": "g1", "name": "left"})
        right = make_event(device_id="right", sequence=1, sync_key="game:g1", parents=[root["event_id"]], catalog={"game_id": "g1", "name": "right"})
        local = make_event(device_id="local", sequence=1, sync_key="game:g1", parents=[left["event_id"], right["event_id"]], catalog={"game_id": "g1", "name": "left"})
        remote = make_event(device_id="remote", sequence=1, sync_key="game:g1", parents=[left["event_id"], right["event_id"]], catalog={"game_id": "g1", "name": "right"})
        events = {item["event_id"]: item for item in (root, left, right, local)}
        state = {"games": [{"game_id": "g1", "name": "left"}], "library_sync": {"events": events, "heads": {"game:g1": [local["event_id"]]}}}
        plan = preview_sync(state, [remote])
        self.assertEqual(plan["counts"]["conflicts"], 1)
        self.assertEqual(plan["conflicts"][0]["field"], "name")

    def test_criss_cross_remote_tombstone_choice_removes_record(self):
        root = make_event(device_id="base", sequence=1, sync_key="game:g1", catalog={"game_id": "g1", "name": "root"})
        left = make_event(device_id="left", sequence=1, sync_key="game:g1", parents=[root["event_id"]], catalog={"game_id": "g1", "name": "left"})
        right = make_event(device_id="right", sequence=1, sync_key="game:g1", parents=[root["event_id"]], catalog={"game_id": "g1", "name": "right"})
        local = make_event(device_id="local", sequence=1, sync_key="game:g1", parents=[left["event_id"], right["event_id"]], catalog={"game_id": "g1", "name": "left"})
        remote = make_event(device_id="remote", sequence=1, sync_key="game:g1", parents=[left["event_id"], right["event_id"]], tombstone=True)
        events = {item["event_id"]: item for item in (root, left, right, local)}
        state = {"games": [{"game_id": "g1", "name": "left"}], "library_sync": {"events": events, "heads": {"game:g1": [local["event_id"]]}}}
        plan = preview_sync(state, [remote])
        self.assertEqual(plan["counts"]["conflicts"], 1)
        result = apply_sync(state, plan, conflicts={"game:g1": {"__record__": "remote"}})
        self.assertEqual(result["applied"], 1)
        self.assertEqual(state["games"], [])
        head = state["library_sync"]["heads"]["game:g1"][0]
        self.assertTrue(state["library_sync"]["events"][head].get("tombstone"))

    def test_criss_cross_local_choice_preserves_field_deletion(self):
        root = make_event(device_id="base", sequence=1, sync_key="game:g1", catalog={"game_id": "g1", "name": "root"})
        left = make_event(device_id="left", sequence=1, sync_key="game:g1", parents=[root["event_id"]], catalog={"game_id": "g1", "name": "left"})
        right = make_event(device_id="right", sequence=1, sync_key="game:g1", parents=[root["event_id"]], catalog={"game_id": "g1", "name": "right", "notes": "old"})
        local = make_event(device_id="local", sequence=1, sync_key="game:g1", parents=[left["event_id"], right["event_id"]], catalog={"game_id": "g1", "name": "left"})
        remote = make_event(device_id="remote", sequence=1, sync_key="game:g1", parents=[left["event_id"], right["event_id"]], catalog={"game_id": "g1", "name": "right", "notes": "remote"})
        events = {item["event_id"]: item for item in (root, left, right, local)}
        state = {"games": [{"game_id": "g1", "name": "left"}], "library_sync": {"events": events, "heads": {"game:g1": [local["event_id"]]}}}
        plan = preview_sync(state, [remote])
        self.assertTrue(any(row["field"] == "notes" for row in plan["conflicts"]))
        apply_sync(state, plan, conflicts={"game:g1": {"notes": "local"}})
        self.assertNotIn("notes", state["games"][0])

    def test_tombstone_and_edit_heads_are_a_conflict_in_any_order(self):
        root = make_event(device_id="base", sequence=1, sync_key="game:g1", catalog={"game_id": "g1", "name": "root"})
        tombstone = make_event(device_id="delete", sequence=1, sync_key="game:g1", parents=[root["event_id"]], tombstone=True)
        edit = make_event(device_id="edit", sequence=1, sync_key="game:g1", parents=[root["event_id"]], catalog={"game_id": "g1", "name": "edited"})
        events = {root["event_id"]: root}
        state = {"games": [{"game_id": "g1", "name": "root"}], "library_sync": {"events": events, "heads": {"game:g1": [root["event_id"]]}}}
        for incoming in ([tombstone, edit], [edit, tombstone]):
            plan = preview_sync(state, incoming)
            self.assertEqual(plan["counts"]["conflicts"], 1)

    def test_three_mixed_remote_heads_keep_deletion_conflict(self):
        root = make_event(device_id="base", sequence=1, sync_key="game:g1", catalog={"game_id": "g1", "name": "root"})
        heads = [
            make_event(device_id="live-a", sequence=1, sync_key="game:g1", parents=[root["event_id"]], catalog={"game_id": "g1", "name": "A"}),
            make_event(device_id="delete", sequence=1, sync_key="game:g1", parents=[root["event_id"]], tombstone=True),
            make_event(device_id="live-b", sequence=1, sync_key="game:g1", parents=[root["event_id"]], catalog={"game_id": "g1", "name": "B"}),
        ]
        state = {"games": [], "library_sync": {"events": {root["event_id"]: root}, "heads": {"game:g1": [root["event_id"]]}}}
        plan = preview_sync(state, heads)
        self.assertGreaterEqual(plan["counts"]["conflicts"], 1)

    def test_multiple_field_alternatives_are_independently_selectable(self):
        root = make_event(device_id="base", sequence=1, sync_key="game:g1", catalog={"game_id": "g1", "name": "root"})
        heads = [
            make_event(device_id="a", sequence=1, sync_key="game:g1", parents=[root["event_id"]], catalog={"game_id": "g1", "name": "A"}),
            make_event(device_id="b", sequence=1, sync_key="game:g1", parents=[root["event_id"]], catalog={"game_id": "g1", "name": "B"}),
            make_event(device_id="c", sequence=1, sync_key="game:g1", parents=[root["event_id"]], catalog={"game_id": "g1", "name": "C"}),
        ]
        state = {"games": [{"game_id": "g1", "name": "root"}], "library_sync": {"events": {root["event_id"]: root}, "heads": {"game:g1": [root["event_id"]]}}}
        plan = preview_sync(state, heads)
        row = next(row for row in plan["conflicts"] if row["field"] == "name")
        self.assertEqual(len(row["alternatives"]), 3)
        self.assertEqual(len({item["value"] for item in row["alternatives"]}), 3)
        selected = row["alternatives"][2]["choice"]
        result = apply_sync(state, plan, conflicts={"game:g1": {row["conflict_id"]: selected}})
        self.assertEqual(result["applied"], 1)
        self.assertEqual(state["games"][0]["name"], row["alternatives"][2]["value"])

    def test_multiple_record_alternatives_include_tombstone_choice(self):
        event_time = "2026-01-01T00:00:00Z"
        root = make_event(device_id="base", sequence=1, sync_key="game:g1", created_at=event_time, catalog={"game_id": "g1", "name": "root"})
        heads = [
            # Event ids are sorted during head reduction; this makes the
            # tombstone the first head so two record alternatives are kept.
            make_event(device_id="del7", sequence=1, sync_key="game:g1", created_at=event_time, parents=[root["event_id"]], tombstone=True),
            make_event(device_id="a", sequence=1, sync_key="game:g1", created_at=event_time, parents=[root["event_id"]], catalog={"game_id": "g1", "name": "A"}),
            make_event(device_id="b", sequence=1, sync_key="game:g1", created_at=event_time, parents=[root["event_id"]], catalog={"game_id": "g1", "name": "B"}),
        ]
        state = {"games": [], "library_sync": {"events": {root["event_id"]: root}, "heads": {"game:g1": [root["event_id"]]}}}
        plan = preview_sync(state, heads)
        row = next(row for row in plan["conflicts"] if row["field"] == "__record__")
        self.assertEqual(len(row["alternatives"]), 3)
        tombstone = next(item for item in row["alternatives"] if not item["present"])
        result = apply_sync(state, plan, conflicts={"game:g1": {row["conflict_id"]: tombstone["choice"]}})
        self.assertEqual(result["applied"], 1)
        self.assertEqual(state["games"], [])

    def test_unknown_alternative_choice_does_not_apply_untrusted_catalog(self):
        root = make_event(device_id="base", sequence=1, sync_key="game:g1", catalog={"game_id": "g1", "name": "root"})
        heads = [
            make_event(device_id="a", sequence=1, sync_key="game:g1", parents=[root["event_id"]], catalog={"game_id": "g1", "name": "A"}),
            make_event(device_id="b", sequence=1, sync_key="game:g1", parents=[root["event_id"]], catalog={"game_id": "g1", "name": "B"}),
        ]
        state = {"games": [{"game_id": "g1", "name": "root"}], "library_sync": {"events": {root["event_id"]: root}, "heads": {"game:g1": [root["event_id"]]}}}
        plan = preview_sync(state, heads)
        row = next(row for row in plan["conflicts"] if row["field"] == "name")
        result = apply_sync(state, plan, conflicts={"game:g1": {row["conflict_id"]: "untrusted-value"}})
        self.assertEqual(result["applied"], 0)
        self.assertEqual(state["games"][0]["name"], "root")
        self.assertEqual(len(result["conflicts"]), 1)

    def test_verified_identity_survives_revisions_and_does_not_duplicate(self):
        first = make_event(device_id="remote", sequence=1, sync_key="verified:one", catalog={"game_id": "g1", "name": "One"})
        state = {"games": []}
        apply_sync(state, preview_sync(state, [first]))
        self.assertEqual(state["games"][0]["library_sync_id"], "one")
        second = make_event(device_id="remote", sequence=2, sync_key="verified:one", parents=[first["event_id"]], catalog={"game_id": "g1", "name": "Two"})
        apply_sync(state, preview_sync(state, [second]))
        self.assertEqual(len(state["games"]), 1)
        self.assertEqual(state["games"][0]["name"], "Two")

    def test_event_key_namespace_and_game_identity_are_bound(self):
        with self.assertRaises(SyncValidationError):
            make_event(device_id="remote", sequence=1, sync_key="game:g1", catalog={"game_id": "g2"})
        with self.assertRaises(SyncValidationError):
            make_event(device_id="remote", sequence=1, sync_key="provider:g1", catalog={"game_id": "g1"})

    def test_files_are_content_addressed_atomic_and_invalid_source_is_preserved(self):
        event = make_event(device_id="a", sequence=1, sync_key="game:g1", catalog={"game_id": "g1"})
        with tempfile.TemporaryDirectory() as directory:
            target = write_event(directory, event)
            self.assertEqual(target.parent.parent.name, SYNC_DIRECTORY)
            write_event(directory, event)
            bad = target.parent / "bad.json"
            bad.write_text("not json", encoding="utf-8")
            with self.assertRaises(SyncValidationError):
                read_events(directory)
            self.assertEqual(bad.read_text(encoding="utf-8"), "not json")

    def test_validation_rejects_missing_parent_duplicate_and_missing_folder(self):
        event = make_event(device_id="a", sequence=1, sync_key="game:g1", parents=["0" * 64], catalog={"game_id": "g1"})
        with self.assertRaises(SyncValidationError):
            validate_event_set([event])
        with self.assertRaises(SyncFolderError):
            read_events(Path(tempfile.gettempdir()) / "openbox-sync-folder-does-not-exist")

    def test_catalog_and_event_validation_limits(self):
        with self.assertRaises(SyncValidationError):
            event_id({"value": object()})
        for value in (None, True, "\x00bad", "x" * 257):
            with self.assertRaises(SyncValidationError):
                stable_sync_key({"game_id": value})
        with self.assertRaises(SyncValidationError):
            build_catalog(None)
        with self.assertRaises(SyncValidationError):
            build_catalog({"description": "x" * 300_000})
        with self.assertRaises(SyncValidationError):
            build_catalog({"description": math.nan})
        with self.assertRaises(SyncValidationError):
            build_catalog({"description": list(range(10_001))})
        with self.assertRaises(SyncValidationError):
            build_catalog({"description": {str(index): index for index in range(1_001)}})
        with self.assertRaises(SyncValidationError):
            build_catalog({"description": {1: "bad key"}})
        nested = value = {}
        for _ in range(10):
            value = {"nested": value}
        nested["description"] = value
        with self.assertRaises(SyncValidationError):
            build_catalog(nested)
        with self.assertRaises(SyncValidationError):
            build_catalog({"description": object()})
        with self.assertRaises(SyncValidationError):
            capture_sync_snapshot({"games": "not a list"})
        with self.assertRaises(SyncValidationError):
            capture_sync_snapshot({"games": ["not a game"]})

        with self.assertRaises(SyncValidationError):
            make_event(device_id="a", sequence=0, sync_key="game:g1", catalog={"game_id": "g1"})
        with self.assertRaises(SyncValidationError):
            make_event(device_id="a", sequence=1, sync_key="game:g1", parents=["a", "a"], catalog={"game_id": "g1"})
        with self.assertRaises(SyncValidationError):
            make_event(device_id="a", sequence=1, sync_key="game:g1", parents=[str(i) for i in range(257)], catalog={"game_id": "g1"})
        with self.assertRaises(SyncValidationError):
            make_event(device_id="a", sequence=1, sync_key="game:g1", created_at="not a timestamp", catalog={"game_id": "g1"})
        with self.assertRaises(SyncValidationError):
            make_event(device_id="a", sequence=1, sync_key="game:g1", catalog={"game_id": "g1"}, tombstone=True)
        with self.assertRaises(SyncValidationError):
            make_event(device_id="a", sequence=1, sync_key="game:g1")

        event = make_event(device_id="a", sequence=1, sync_key="game:g1", catalog={"game_id": "g1"})

        def invalid(changes, *, rehash=False):
            candidate = copy.deepcopy(event)
            changes(candidate)
            if rehash:
                candidate["event_id"] = event_id(candidate)
            with self.assertRaises(SyncValidationError):
                validate_event(candidate)

        with self.assertRaises(SyncValidationError):
            validate_event(42)
        invalid(lambda candidate: candidate.update(extra=True))
        invalid(lambda candidate: candidate.pop("catalog"))
        invalid(lambda candidate: candidate.update(format=99))
        invalid(lambda candidate: candidate.update(event_id="bad"))
        invalid(lambda candidate: candidate.update(event_id="0" * 64))
        invalid(lambda candidate: candidate.update(sequence=True), rehash=True)
        invalid(lambda candidate: candidate.update(created_at="bad"), rehash=True)
        invalid(lambda candidate: candidate.update(parents="bad"), rehash=True)
        invalid(lambda candidate: candidate.update(parents=["bad"]), rehash=True)
        invalid(lambda candidate: candidate.update(parents=["f" * 64, "0" * 64]), rehash=True)
        invalid(lambda candidate: candidate.update(catalog="bad"), rehash=True)
        invalid(lambda candidate: candidate.update(catalog={"private": "field"}), rehash=True)
        both = copy.deepcopy(event)
        both["tombstone"] = True
        both["event_id"] = event_id(both)
        with self.assertRaises(SyncValidationError):
            validate_event(both)
        with self.assertRaises(SyncValidationError):
            validate_event_set([event] * 50_001)
        parent = make_event(device_id="a", sequence=2, sync_key="game:other", catalog={"game_id": "other"})
        child = make_event(device_id="a", sequence=3, sync_key="game:g1", parents=[parent["event_id"]], catalog={"game_id": "g1"})
        with self.assertRaises(SyncValidationError):
            validate_event_set([parent, child])

    def test_transport_failures_and_legacy_helpers(self):
        event = make_event(device_id="a", sequence=1, sync_key="game:g1", catalog={"game_id": "g1"})
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(read_events(directory), [])
            target = write_event(directory, event)
            target.write_text("not json", encoding="utf-8")
            with self.assertRaises(SyncValidationError):
                write_event(directory, event)
            target.write_text(json.dumps(make_event(device_id="b", sequence=1, sync_key="game:g1", catalog={"game_id": "g1"})), encoding="utf-8")
            with self.assertRaises(SyncValidationError):
                write_event(directory, event)
            target.unlink()
            self.assertEqual(len(write_events(directory, [event])), 1)
            self.assertEqual(len(read_events(directory)), 1)
            target.unlink()
            events_dir = target.parent
            (events_dir / "wrong-name.json").write_text(json.dumps(event), encoding="utf-8")
            with self.assertRaises(SyncValidationError):
                read_events(directory)
            (events_dir / "wrong-name.json").unlink()
            (events_dir / f"{event['event_id']}.json").write_text("x" * (1024 * 1024 + 1), encoding="utf-8")
            with self.assertRaises(SyncValidationError):
                read_events(directory)
            (events_dir / f"{event['event_id']}.json").unlink()
            with mock.patch("pkg.parity.parity_library_sync.os.replace", side_effect=OSError("read only")):
                with self.assertRaises(SyncFolderError):
                    write_event(directory, event)
        with tempfile.TemporaryDirectory() as directory:
            sync_root = Path(directory) / SYNC_DIRECTORY
            sync_root.mkdir()
            (sync_root / "events").write_text("not a directory", encoding="utf-8")
            with self.assertRaises(SyncFolderError):
                read_events(directory)
            with mock.patch.object(Path, "mkdir", side_effect=OSError("read only")):
                with self.assertRaises(SyncFolderError):
                    sync_folder(directory, create=True)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SyncValidationError):
                from pkg.parity.parity_library_sync import event_path
                event_path(directory, "bad")

    def test_state_shape_guards_and_merge_choices(self):
        event = make_event(device_id="a", sequence=1, sync_key="game:g1", catalog={"game_id": "g1"})
        with self.assertRaises(SyncValidationError):
            ensure_device_id(None)
        with self.assertRaises(SyncValidationError):
            ensure_device_id({"library_sync": []})
        self.assertFalse(sync_enabled([]))
        self.assertTrue(sync_enabled({"library_sync": {"enabled": True}}))
        self.assertTrue(sync_enabled({"settings": {"library_sync_enabled": True}}))
        self.assertFalse(sync_enabled({"settings": {"library_sync_enabled": False}}))

        state = {"games": [], "library_sync": {"events": [event]}}
        self.assertEqual(preview_sync(state, []) ["changes"], [])
        with self.assertRaises(SyncValidationError):
            preview_sync({"games": [], "library_sync": {"events": "bad"}}, [])
        with self.assertRaises(SyncValidationError):
            preview_sync({"games": [], "library_sync": {"events": {"wrong": event}}}, [])
        with self.assertRaises(SyncValidationError):
            record_local_changes({"games": [], "library_sync": {"outbox": "bad"}}, [], [game()])
        sequence_state = {"games": [], "library_sync": {"next_sequence": True}}
        self.assertEqual(record_local_changes(sequence_state, [], [game()])["events"][0]["sequence"], 1)
        with self.assertRaises(SyncValidationError):
            record_local_changes({"games": [], "library_sync": {"heads": []}}, [], [game()])
        with self.assertRaises(SyncValidationError):
            record_local_changes({"games": [], "library_sync": {"heads": {"game:g1": "bad"}}}, [], [game()])
        with self.assertRaises(SyncValidationError):
            record_local_changes({"games": [game(), game()]}, [], [game(), game()], device="same")
        with self.assertRaises(SyncValidationError):
            record_local_changes([], [], [])
        with self.assertRaises(SyncValidationError):
            record_local_changes({}, None, None)
        list_form = record_local_changes([game()], [game()], {"games": [game()]})
        self.assertEqual(list_form["changed"], 0)
        list_form = record_local_changes([game()], [])
        self.assertEqual(list_form["changed"], 1)
        self.assertEqual(ingest_event_set([event])[0]["event_id"], event["event_id"])

        state = {"games": [], "library_sync": {"events": {}, "outbox": [event]}}
        with tempfile.TemporaryDirectory() as directory:
            result = publish_outbox(state, directory)
            self.assertEqual(result["published"], 1)
        with self.assertRaises(SyncValidationError):
            publish_outbox({"games": [], "library_sync": {"outbox": "bad"}}, tempfile.gettempdir())

        self.assertEqual(_merge_catalogs("game:g1", None, {"game_id": "g1"}, {})[0]["game_id"], "g1")
        self.assertEqual(_merge_catalogs("game:g1", None, None, {})[0], None)
        self.assertEqual(_merge_catalogs("game:g1", None, {"game_id": "g1"}, {}, {"game:g1": "remote"})[0]["game_id"], "g1")
        self.assertEqual(_merge_catalogs("game:g1", {"game_id": "g1"}, None, {}, {"game:g1": "local"})[0]["game_id"], "g1")
        merged, conflicts = _merge_catalogs(
            "game:g1", {"name": "local", "platform": "same"}, {"name": "remote"},
            {"name": "base", "platform": "same"}, {"game:g1": {"name": "remote"}},
        )
        self.assertEqual(merged["name"], "remote")
        self.assertNotIn("platform", merged)
        self.assertEqual(conflicts, [])
        merged, conflicts = _merge_catalogs(
            "game:g1", {"name": "local"}, {"name": "remote"}, {"name": "base"},
            {"game:g1": {"name": "local"}},
        )
        self.assertEqual(merged["name"], "local")
        self.assertEqual(conflicts, [])

    def test_apply_guards_and_divergent_delete_resolution(self):
        state = {"games": []}
        with self.assertRaises(SyncValidationError):
            apply_sync(state, None)
        plan = preview_sync(state, [])
        plan["changes"] = [{"sync_key": None, "event_id": None, "remote": None, "conflicts": []}]
        result = apply_sync(state, plan)
        self.assertEqual(result["applied"], 0)
        bad_games = {"games": "bad"}
        with mock.patch("pkg.parity.parity_library_sync.state_token", return_value="token"):
            with self.assertRaises(SyncValidationError):
                apply_sync(bad_games, {"format": 3, "source_token": "token", "events": [], "changes": []})

        base = {"games": [game()]}
        record_local_changes(base, [], base["games"], now="2026-01-01T00:00:00+00:00", device="device-base")
        left = copy.deepcopy(base)
        right = copy.deepcopy(base)
        left["library_sync"]["device_id"] = "device-left"
        right["library_sync"]["device_id"] = "device-right"
        left_before = copy.deepcopy(left["games"])
        right_before = copy.deepcopy(right["games"])
        left["games"][0]["name"] = "Left title"
        right["games"].clear()
        record_local_changes(left, left_before, left["games"], now="2026-01-02T00:00:00+00:00")
        record_local_changes(right, right_before, right["games"], now="2026-01-02T00:01:00+00:00")
        with tempfile.TemporaryDirectory() as directory:
            publish_outbox(right, directory)
            plan = preview_sync(left, read_events(directory), conflicts={"game:g1": {"__record__": "remote"}})
            applied = apply_sync(
                left, plan, conflicts={"game:g1": {"__record__": "remote"}},
                now="2026-01-03T00:00:00+00:00",
            )
        self.assertEqual(applied["applied"], 1)
        self.assertEqual(left["games"], [])
        self.assertTrue(left["library_sync"]["outbox"][-1].get("tombstone"))


if __name__ == "__main__":
    unittest.main()
