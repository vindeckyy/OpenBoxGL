"""Standalone tests for the bounded T6 household record core."""

from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity.parity_household import (  # noqa: E402
    MAX_RECORDS,
    MAX_RECORD_BYTES,
    HOUSEHOLD_EVENT_DIRECTORY,
    HOUSEHOLD_SYNC_DIRECTORY,
    SyncValidationError,
    append_record,
    challenge_progress,
    collect_stats,
    compute_leaderboard,
    converge_records,
    current_records,
    household_records,
    make_challenge_record,
    make_challenge_result_record,
    make_record,
    make_member_record,
    make_share_record,
    make_tombstone_record,
    latest_valid,
    materialize_records,
    merge_records,
    household_sync_folder,
    pull_household_records,
    publish_household_outbox,
    read_household_records,
    record_id,
    record_member,
    record_challenge,
    record_challenge_result,
    record_local,
    record_share,
    record_stats_share,
    shareable_stats,
    stats_sharing_enabled,
    state_token,
    tombstone_record,
    validate_record,
    validate_record_set,
)


class HouseholdRecordTests(unittest.TestCase):
    def test_member_is_detached_and_local_only_fields_never_enter_payload(self):
        source = {"member_id": "m1", "display_name": "Alice", "avatar_color": "violet"}
        record = make_member_record(**source, device_id="desktop", created_at="2026-01-01T00:00:00+00:00")
        source["display_name"] = "Changed"
        checked = validate_record(record)
        self.assertEqual(checked["payload"]["display_name"], "Alice")
        self.assertNotIn("path", checked["payload"])
        self.assertEqual(record["event_id"], checked["event_id"])

    def test_two_devices_converge_independent_of_arrival_order(self):
        left = make_member_record(
            "m1", "Desktop", device_id="desktop", created_at="2026-01-02T00:00:00+00:00"
        )
        right = make_member_record(
            "m1", "Deck", device_id="deck", created_at="2026-01-03T00:00:00+00:00"
        )
        first = converge_records([left, right])
        second = converge_records([right, left])
        self.assertEqual(first, second)
        self.assertEqual(first[0]["payload"]["display_name"], "Deck")

    def test_causal_descendant_wins_even_with_an_older_clock(self):
        root = make_member_record("m1", "Alice", device_id="a", created_at="2026-01-03T00:00:00+00:00")
        edit = make_member_record(
            "m1", "Alicia", device_id="b", sequence=1, parents=[root["event_id"]],
            created_at="2026-01-01T00:00:00+00:00",
        )
        self.assertEqual(converge_records([edit, root])[0]["payload"]["display_name"], "Alicia")

    def test_challenge_progress_and_result_winner(self):
        challenge = make_challenge_record(
            "c1", "Beat Celeste", device_id="a", created_by="m1", target=3,
            metric="completions", deadline="2026-01-10T00:00:00+00:00",
        )
        result = make_challenge_result_record(
            "c1", "m1", 3, device_id="a", completed=True,
            completed_at="2026-01-05T00:00:00+00:00",
        )
        progress = challenge_progress(challenge, [result])
        self.assertEqual(progress["current"], 3)
        self.assertTrue(progress["completed"])
        self.assertEqual(progress["status"], "complete")
        self.assertEqual(progress["winner"]["member_id"], "m1")

        partial = make_challenge_result_record("c1", "m1", 1, device_id="a")
        self.assertFalse(challenge_progress(challenge, [partial])["completed"])

        # A synced result may omit completed_at.  Its immutable record time is
        # still the deadline and winner-ordering source of truth.
        result_without_completion_time = make_challenge_result_record(
            "c1", "m1", 3, device_id="peer", completed=True,
            created_at="2026-01-06T00:00:00+00:00",
        )
        progress = challenge_progress(challenge, [result_without_completion_time])
        self.assertEqual(progress["winner"]["member_id"], "m1")

    def test_local_challenge_result_requires_live_matching_challenge(self):
        missing = {"settings": {}}
        with self.assertRaises(SyncValidationError):
            record_challenge_result(missing, "missing", "m1", 1, device_id="d")
        self.assertEqual(missing, {"settings": {}})

        def seeded(**changes):
            state = {"settings": {}}
            record_challenge(
                state,
                "c1",
                "Beat the game",
                created_by="owner",
                target=1,
                game_id="g1",
                participant_ids=["m1"],
                deadline="2026-01-10T00:00:00+00:00",
                status=changes.get("status", "active"),
                device_id="owner-device",
                created_at="2026-01-01T00:00:00+00:00",
            )
            return state

        invalid = (
            (seeded(), {"member_id": "outsider"}),
            (seeded(), {"game_id": "g2"}),
            (seeded(status="closed"), {}),
            (seeded(), {"created_at": "2026-01-10T00:00:00+00:00"}),
        )
        for state, changes in invalid:
            before = copy.deepcopy(state)
            with self.assertRaises(SyncValidationError):
                record_challenge_result(
                    state,
                    "c1",
                    changes.get("member_id", "m1"),
                    1,
                    game_id=changes.get("game_id", "g1"),
                    created_at=changes.get("created_at", "2026-01-09T00:00:00+00:00"),
                    completed=True,
                    completed_at=changes.get("created_at", "2026-01-09T00:00:00+00:00"),
                    device_id="m1-device",
                )
            self.assertEqual(state, before)

        valid = seeded()
        result = record_challenge_result(
            valid,
            "c1",
            "m1",
            1,
            game_id="g1",
            completed=True,
            completed_at="2026-01-09T00:00:00+00:00",
            created_at="2026-01-09T00:00:01+00:00",
            device_id="m1-device",
        )
        self.assertEqual(result["payload"]["game_id"], "g1")
        challenge = next(
            item for item in valid["household"]["records"].values() if item["kind"] == "challenge"
        )
        self.assertTrue(challenge_progress(challenge, valid["household"]["records"].values())["completed"])

    def test_out_of_order_synced_challenge_result_remains_mergeable(self):
        challenge = make_challenge_record(
            "c1",
            "Beat the game",
            created_by="owner",
            target=1,
            game_id="g1",
            participant_ids=["m1"],
            deadline="2026-01-10T00:00:00+00:00",
            status="active",
            device_id="owner-device",
            created_at="2026-01-01T00:00:00+00:00",
        )
        result = make_challenge_result_record(
            "c1",
            "m1",
            1,
            game_id="g1",
            completed=True,
            completed_at="2026-01-05T00:00:00+00:00",
            device_id="m1-device",
            created_at="2026-01-05T00:00:01+00:00",
        )
        state = {"settings": {}}
        self.assertEqual(merge_records(state, [result])["applied"], 1)
        self.assertEqual(merge_records(state, [challenge])["applied"], 1)
        view = challenge_progress(challenge, state["household"]["records"].values())
        self.assertTrue(view["completed"])
        self.assertEqual(view["winner"]["member_id"], "m1")

        foreign_result = make_challenge_result_record(
            "c1",
            "outsider",
            1,
            game_id="g2",
            completed=True,
            completed_at="2026-01-05T00:00:00+00:00",
            device_id="foreign-device",
            created_at="2026-01-05T00:00:01+00:00",
        )
        merge_records(state, [foreign_result])
        view = challenge_progress(challenge, state["household"]["records"].values())
        self.assertEqual([item["member_id"] for item in view["members"]], ["m1"])

    def test_raw_record_append_keeps_out_of_order_sync_context_free(self):
        result = make_challenge_result_record(
            "c1", "m1", 1, game_id="g1", completed=True,
            completed_at="2026-01-05T00:00:00+00:00",
            device_id="m1-device", created_at="2026-01-05T00:00:01+00:00",
        )
        state = {"settings": {}}
        self.assertEqual(append_record(state, result, outbox=False), result)
        self.assertIn(result["event_id"], state["household"]["records"])

        before = copy.deepcopy(state)
        with self.assertRaises(SyncValidationError):
            append_record(state, result, outbox=True)
        self.assertEqual(state, before)

    def test_two_local_states_exchange_challenge_and_result(self):
        desktop = {"settings": {}}
        deck = {"settings": {}}
        challenge = record_challenge(
            desktop, "c1", "Beat Celeste", created_by="desktop", target=1,
            device_id="desktop", created_at="2026-01-01T00:00:00+00:00",
        )
        merge_records(deck, [challenge])
        result = record_challenge_result(
            deck, "c1", "deck", 1, completed=True, device_id="deck",
            created_at="2026-01-02T00:00:00+00:00",
        )
        merge_records(desktop, [result])
        view = challenge_progress(challenge, desktop["household"]["records"])
        self.assertTrue(view["completed"])
        self.assertEqual(view["winner"]["member_id"], "deck")

    def test_tombstone_removes_the_materialized_value(self):
        member = make_member_record("m1", "Alice", device_id="a", created_at="2026-01-01T00:00:00+00:00")
        deleted = make_member_record(
            "m1", "ignored", device_id="b", tombstone=True, parents=[member["event_id"]],
            created_at="2025-12-01T00:00:00+00:00",
        )
        current = converge_records([deleted, member])
        self.assertTrue(current[0].get("tombstone"))
        self.assertEqual(materialize_records([member, deleted]), {})

    def test_tombstoned_challenge_result_does_not_resurface_its_ancestor(self):
        challenge = make_challenge_record("c1", "Beat", created_by="m1", target=1)
        result = make_challenge_result_record("c1", "m1", 1, completed=True, device_id="a")
        deleted = make_challenge_result_record(
            "c1", "m1", tombstone=True, device_id="b", parents=[result["event_id"]]
        )
        progress = challenge_progress(challenge, [result, deleted])
        self.assertFalse(progress["completed"])
        self.assertEqual(progress["members"], [])

    def test_explicit_stats_opt_in_and_honest_leaderboard(self):
        off = {"settings": {}}
        self.assertFalse(stats_sharing_enabled(off))
        self.assertIsNone(record_share(off, "m1", "weekly", {"completions": 1}))
        self.assertEqual(off, {"settings": {}})

        on = {"settings": {"household_stats_sharing": True}}
        member = record_member(on, "m1", "Alice", device_id="a", created_at="2026-01-01T00:00:00+00:00")
        share = record_share(on, "m1", "weekly", {"completions": 2}, device_id="a", created_at="2026-01-02T00:00:00+00:00")
        self.assertIsNotNone(share)
        board = compute_leaderboard(on)
        self.assertTrue(board["available"])
        self.assertEqual(board["entries"][0]["display_name"], "Alice")
        self.assertEqual(board["entries"][0]["completions"], 2)
        self.assertEqual(member["kind"], "member")

    def test_state_merge_is_copy_on_write_on_bad_input(self):
        state = {"settings": {}, "household": {"records": {}}}
        local = record_member(state, "m1", "Alice", device_id="a")
        before = copy.deepcopy(state)
        malformed = dict(local)
        malformed["payload"] = {"member_id": "m1", "display_name": "not trusted", "avatar_color": "blue"}
        with self.assertRaises(SyncValidationError):
            merge_records(state, [malformed])
        self.assertEqual(state, before)

    def test_oversized_payload_and_set_are_rejected_without_mutation(self):
        record = make_member_record("m1", "Alice")
        oversized = copy.deepcopy(record)
        oversized["payload"]["display_name"] = "x" * MAX_RECORD_BYTES
        oversized["event_id"] = "0" * 64
        with self.assertRaises(SyncValidationError):
            validate_record(oversized)
        original = [record]
        with self.assertRaises(SyncValidationError):
            validate_record_set(original + [oversized])
        self.assertEqual(original[0]["payload"]["display_name"], "Alice")

    def test_share_rejects_private_stats(self):
        with self.assertRaises(SyncValidationError):
            make_share_record("m1", "weekly", {"path": "/secret"})

    def test_common_validation_failures_are_fail_closed(self):
        valid = make_member_record("m1", "Alice")

        def bad(**changes):
            candidate = copy.deepcopy(valid)
            candidate.update(changes)
            if changes.get("payload") is not None or "payload" in changes:
                candidate["event_id"] = record_id(candidate)
            return candidate

        for candidate in (
            None,
            {},
            bad(extra=True),
            bad(format=99),
            bad(event_id="0" * 64),
            bad(device_id=True),
            bad(sequence=0),
            bad(created_at="not-a-time"),
            bad(kind="unknown"),
            bad(sync_key="challenge:c1"),
            bad(parents="not-a-list"),
            bad(tombstone=False),
        ):
            with self.assertRaises(SyncValidationError):
                validate_record(candidate)

        both = copy.deepcopy(valid)
        both["tombstone"] = True
        both["event_id"] = record_id(both)
        with self.assertRaises(SyncValidationError):
            validate_record(both)
        with self.assertRaises(SyncValidationError):
            make_record(kind="member", device_id="d", sequence=1, sync_key="member:m1")
        with self.assertRaises(SyncValidationError):
            make_record(kind="bad", device_id="d", sequence=1, sync_key="bad:x", payload={})
        with self.assertRaises(SyncValidationError):
            make_record(kind="member", device_id="d", sequence=1, record_key="m1", payload={})

    def test_kind_payload_validation_and_builder_bounds(self):
        challenge = make_challenge_record(
            "c1", "Beat", created_by="m1", description="details", game_id="g1",
            deadline="2026-02-01T00:00:00+00:00", participant_ids=("m2", "m1"),
        )
        self.assertEqual(challenge["payload"]["participant_ids"], ["m1", "m2"])
        result = make_challenge_result_record(
            "c1", "m1", 2, completed=False, completed_at="2026-01-01T00:00:00+00:00",
            game_id="g1", note="progress",
        )
        share = make_share_record(
            "m1", "all-time", {"playtime_seconds": 3},
            window_start="2026-01-01", window_end="2026-01-31",
        )
        self.assertEqual(share["payload"]["period"], "all_time")

        candidates = [
            (challenge, {"metric": "bad"}),
            (challenge, {"target": 0}),
            (challenge, {"created_by": True}),
            (result, {"value": -1}),
            (result, {"completed": "yes"}),
            (result, {"completed_at": "bad"}),
            (share, {"period": "bad"}),
            (share, {"stats": {"path": 1}}),
            (share, {"stats": {"completions": -1}}),
        ]
        for original, changes in candidates:
            candidate = copy.deepcopy(original)
            candidate["payload"].update(changes)
            candidate["event_id"] = record_id(candidate)
            with self.assertRaises(SyncValidationError):
                validate_record(candidate)

        with self.assertRaises(SyncValidationError):
            make_member_record("m1", "x" * 81)
        with self.assertRaises(SyncValidationError):
            make_challenge_record("c1", "Beat", metric="bad")
        with self.assertRaises(SyncValidationError):
            make_challenge_record("c1", "Beat", participant_ids=["m1", "m1"])
        with self.assertRaises(SyncValidationError):
            make_challenge_result_record("c1", "m1", True)

    def test_record_sets_validate_dag_duplicates_and_limits(self):
        root = make_member_record("m1", "A", device_id="a")
        child = make_member_record("m1", "B", device_id="b", parents=[root["event_id"]])
        self.assertEqual(validate_record_set([child], [root]), [child])
        with self.assertRaises(SyncValidationError):
            validate_record_set([child])
        other = make_challenge_record("c1", "C", created_by="m1")
        cross = copy.deepcopy(child)
        cross["parents"] = [other["event_id"]]
        cross["event_id"] = record_id(cross)
        with self.assertRaises(SyncValidationError):
            validate_record_set([cross], [other])
        with self.assertRaises(SyncValidationError):
            validate_record_set([root, root])
        with self.assertRaises(SyncValidationError):
            validate_record_set((root for _ in range(MAX_RECORDS + 1)))

    def test_latest_helpers_are_deterministic_and_detached(self):
        first = make_member_record("m1", "A", device_id="a", created_at="2026-01-01T00:00:00+00:00")
        second = make_member_record("m1", "B", device_id="b", created_at="2026-01-02T00:00:00+00:00")
        malformed = {"not": "a record"}
        self.assertIsNone(latest_valid([malformed]))
        self.assertEqual(latest_valid([malformed, first, second])["payload"]["display_name"], "B")
        self.assertEqual(latest_valid([first, second], "member:other"), None)
        with self.assertRaises(SyncValidationError):
            latest_valid([malformed], strict=True)
        selected = current_records([first, second])
        selected[0]["payload"]["display_name"] = "changed"
        self.assertEqual(latest_valid([first, second])["payload"]["display_name"], "B")

    def test_tombstone_helpers_and_local_record_paths(self):
        state = {"settings": {}}
        member = record_local(
            state, "member", {"member_id": "m1", "display_name": "A", "avatar_color": "blue"},
            device_id="d", created_at="2026-01-01T00:00:00+00:00",
        )
        self.assertEqual(append_record(state, member), member)
        deleted = tombstone_record(member, device_id="d", sequence=2)
        tombstone = make_tombstone_record(
            "member", "member:m1", device_id="d", created_at="2026-01-02T00:00:00+00:00"
        )
        self.assertEqual(
            tombstone,
            make_tombstone_record("member", "member:m1", device_id="d", created_at="2026-01-02T00:00:00+00:00"),
        )
        append_record(state, deleted)
        self.assertEqual(materialize_records(state["household"]["records"].values()), {})
        self.assertEqual(state["household"]["outbox"][-1], deleted)
        token = state_token(state)
        self.assertEqual(token, state_token(copy.deepcopy(state)))

    def test_state_shapes_and_remote_merge_are_bounded(self):
        state = {"household": {"records": []}}
        merge_result = merge_records(state, [])
        self.assertEqual(merge_result["applied"], 0)
        before = copy.deepcopy(state)
        for invalid in (
            {"household": []},
            {"household": {"records": "bad"}},
            {"household": {"records": [], "outbox": "bad"}},
            {"household": {"records": [], "next_sequence": 0}},
        ):
            with self.assertRaises(SyncValidationError):
                household_records(invalid)
        self.assertEqual(state, before)
        remote = make_member_record("m2", "Remote", device_id="remote")
        merged = merge_records(state, [remote])
        self.assertEqual(merged["applied"], 1)
        self.assertEqual(merged["records"][0]["payload"]["member_id"], "m2")

    def test_stats_collection_and_sharing_helpers(self):
        games = [
            {"game_id": "g1", "playtime_seconds": 120, "play_count": 2, "progress": "Completed", "ra_count": 3},
            {"game_id": "g2", "playtime_seconds": float("nan"), "play_count": "bad", "achievements": 2},
            "bad",
        ]
        history = [{"game_id": "g3", "result": "won"}, "bad"]
        stats = collect_stats(games, history)
        self.assertEqual(stats["playtime_seconds"], 120)
        self.assertEqual(stats["completions"], 1)
        self.assertEqual(stats["ra_count"], 5)
        self.assertEqual(stats["wins"], 1)
        self.assertEqual(collect_stats({"games": games, "history": history}), stats)
        self.assertIsNone(shareable_stats(stats, state={"settings": {}}))
        self.assertEqual(shareable_stats(stats, state={"settings": {"household_stats_sharing": True}}), stats)
        shared_state = {"settings": {"household_stats_sharing": True}}
        self.assertIsNotNone(record_stats_share(shared_state, "m1", "weekly", stats=stats))
        with self.assertRaises(SyncValidationError):
            shareable_stats({"unknown": 1})
        self.assertFalse(stats_sharing_enabled(None))
        self.assertTrue(stats_sharing_enabled({"household_stats_sharing": True}))

    def test_stats_share_windows_use_activity_timestamps(self):
        games = [
            {
                "game_id": "g1",
                "playtime_seconds": 100,
                "play_count": 4,
                "last_played": "2026-01-06T12:00:00+00:00",
                "progress": "Completed",
                "ra_count": 3,
            },
            {
                "game_id": "g2",
                "playtime_seconds": 900,
                "play_count": 5,
                "last_played": "2025-12-31T12:00:00+00:00",
                "progress": "Completed",
                "ra_count": 7,
            },
            {
                "game_id": "g3",
                "playtime_seconds": 50,
                "play_count": 1,
                "last_played": "2026-01-20T12:00:00+00:00",
            },
        ]
        history = [
            {"game_id": "g1", "started": "2026-01-06T13:00:00+00:00", "seconds": 40, "result": "won"},
            {"game_id": "g1", "started": "2026-01-12T13:00:00+00:00", "seconds": 20},
            {"game_id": "g2", "started": "2025-12-31T13:00:00+00:00", "seconds": 300},
        ]

        daily = collect_stats(games, history, period="daily", now="2026-01-06")
        self.assertEqual(daily, {
            "playtime_seconds": 40,
            "completions": 1,
            "ra_count": 3,
            "games_played": 1,
            "sessions": 1,
            "wins": 1,
        })
        weekly = collect_stats(games, history, period="weekly", now="2026-01-10T12:00:00+00:00")
        self.assertEqual(weekly, daily)
        monthly = collect_stats(games, history, period="monthly", now="2026-01-31T12:00:00+00:00")
        self.assertEqual(monthly, {
            "playtime_seconds": 110,
            "completions": 1,
            "ra_count": 3,
            "games_played": 2,
            "sessions": 2,
            "wins": 1,
        })

        state = {
            "settings": {"household_stats_sharing": True},
            "games": games,
            "history": history,
        }
        share = record_stats_share(
            state,
            "m1",
            "weekly",
            now="2026-01-10T12:00:00+00:00",
            device_id="m1-device",
            created_at="2026-01-10T12:00:00+00:00",
        )
        self.assertEqual(share["payload"]["stats"], weekly)
        self.assertEqual(share["payload"]["window_start"], "2026-01-05T00:00:00+00:00")
        self.assertEqual(share["payload"]["window_end"], "2026-01-10T12:00:00+00:00")

        all_time = collect_stats(games, history, period="all-time")
        self.assertEqual(all_time["playtime_seconds"], 1050)
        self.assertEqual(all_time["completions"], 2)
        self.assertEqual(all_time["ra_count"], 10)
        self.assertEqual(all_time["games_played"], 3)
        self.assertEqual(all_time["sessions"], 3)
        self.assertEqual(all_time["wins"], 1)

    def test_leaderboard_uses_the_current_explicit_share_window(self):
        member = make_member_record("m1", "Alice", created_at="2026-01-05T00:00:00+00:00")
        current = make_share_record(
            "m1", "weekly", {"completions": 2}, share_id="m1:current",
            window_start="2026-01-05T00:00:00+00:00",
            window_end="2026-01-10T12:00:00+00:00",
            created_at="2026-01-10T12:00:00+00:00",
        )
        stale = make_share_record(
            "m1", "weekly", {"completions": 99}, share_id="m1:stale",
            window_start="2025-12-29T00:00:00+00:00",
            window_end="2026-01-04T23:59:59+00:00",
            created_at="2026-01-04T23:00:00+00:00",
        )
        board = compute_leaderboard(
            [member, current, stale],
            period="weekly",
            now="2026-01-10T12:00:00+00:00",
        )
        self.assertEqual(board["entries"][0]["completions"], 2)

    def test_progress_expiry_member_filter_and_leaderboard_variants(self):
        challenge = make_challenge_record(
            "c1", "Beat", created_by="m1", target=3, deadline="2026-01-02T00:00:00+00:00",
            status="active",
        )
        partial = make_challenge_result_record("c1", "m1", 1, created_at="2026-01-01T00:00:00+00:00")
        expired = challenge_progress(challenge, [partial], now="2026-01-03T00:00:00+00:00", member_id="m1")
        self.assertEqual(expired["status"], "expired")
        self.assertEqual(expired["current"], 1)
        deleted = make_challenge_record("c1", tombstone=True)
        self.assertTrue(challenge_progress(deleted, [partial])["deleted"])

        member = make_member_record("m1", "A")
        share = make_share_record("m1", "weekly", {"completions": 2, "playtime_seconds": 10})
        self.assertFalse(compute_leaderboard([member, share], stats_sharing=False)["available"])
        board = compute_leaderboard([member, share], period="weekly", limit="bad")
        self.assertEqual(board["entries"][0]["rank"], 1)
        self.assertEqual(compute_leaderboard({"settings": {}})["entries"], [])

    def test_two_devices_exchange_records_through_one_real_shared_folder(self):
        desktop = {"settings": {}, "games": [], "history": []}
        handheld = {"settings": {}, "games": [], "history": []}
        member = record_member(desktop, "m1", "Alice", device_id="desktop")
        challenge = record_challenge(
            desktop, "c1", "Beat one", created_by="m1", target=1, device_id="desktop",
        )

        with tempfile.TemporaryDirectory() as directory:
            published = publish_household_outbox(desktop, directory)
            self.assertEqual(published["published"], 2)
            pulled = pull_household_records(handheld, directory)
            self.assertEqual(pulled["applied"], 2)
            self.assertEqual({item["event_id"] for item in household_records(handheld)}, {
                member["event_id"], challenge["event_id"],
            })

            result = record_challenge_result(
                handheld, "c1", "m1", 1, completed=True, device_id="handheld",
            )
            publish_household_outbox(handheld, directory)
            pulled_back = pull_household_records(desktop, directory)
            self.assertEqual(pulled_back["applied"], 1)
            self.assertTrue(challenge_progress(challenge, desktop["household"]["records"].values())["completed"])
            self.assertIn(result["event_id"], desktop["household"]["records"])

            event_files = list((Path(directory) / HOUSEHOLD_SYNC_DIRECTORY / HOUSEHOLD_EVENT_DIRECTORY).glob("*.json"))
            self.assertEqual(len(event_files), 3)
            self.assertTrue(all((path.stat().st_mode & 0o777) == 0o600 for path in event_files))
            self.assertNotIn("/desktop/", "".join(path.read_text(encoding="utf-8") for path in event_files))

    def test_duplicate_pull_is_idempotent_and_acknowledgement_clears_outbox(self):
        source = {"settings": {}}
        destination = {"settings": {}}
        record = record_member(source, "m1", "Alice", device_id="source")
        self.assertEqual(len(source["household"]["outbox"]), 1)

        with tempfile.TemporaryDirectory() as directory:
            published = publish_household_outbox(source, directory)
            self.assertEqual(published["acknowledged"], 1)
            self.assertEqual(source["household"]["outbox"], [])
            self.assertEqual(publish_household_outbox(source, directory)["published"], 0)

            first = pull_household_records(destination, directory)
            before_repeat = copy.deepcopy(destination)
            second = pull_household_records(destination, directory)
            self.assertEqual(first["applied"], 1)
            self.assertEqual(second["applied"], 0)
            self.assertEqual(destination, before_repeat)
            self.assertEqual(household_records(destination)[0]["event_id"], record["event_id"])

    def test_malformed_and_oversized_remote_records_are_rejected_without_state_mutation(self):
        state = {"settings": {}}
        with tempfile.TemporaryDirectory() as directory:
            events = household_sync_folder(directory, create=True) / HOUSEHOLD_EVENT_DIRECTORY
            malformed = events / "bad.json"
            malformed.write_text("not json", encoding="utf-8")
            before = copy.deepcopy(state)
            with self.assertRaises(SyncValidationError):
                pull_household_records(state, directory)
            self.assertEqual(state, before)
            self.assertEqual(malformed.read_text(encoding="utf-8"), "not json")
            malformed.unlink()

            oversized = events / ("0" * 64 + ".json")
            oversized.write_bytes(b"x" * (MAX_RECORD_BYTES + 2))
            before = copy.deepcopy(state)
            with self.assertRaises(SyncValidationError):
                read_household_records(directory)
            self.assertEqual(state, before)
            self.assertEqual(oversized.stat().st_size, MAX_RECORD_BYTES + 2)

    def test_stats_records_remain_in_outbox_when_device_is_opted_out(self):
        state = {"settings": {}}
        share = make_share_record("m1", "weekly", {"completions": 2})
        append_record(state, share)
        with tempfile.TemporaryDirectory() as directory:
            result = publish_household_outbox(state, directory)
            self.assertEqual(result["published"], 0)
            self.assertEqual(result["stats_skipped"], 1)
            self.assertEqual(len(state["household"]["outbox"]), 1)
            self.assertEqual(read_household_records(directory), [])



if __name__ == "__main__":
    unittest.main(verbosity=2)
