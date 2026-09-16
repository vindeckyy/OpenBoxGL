"""Pure tests for expiring, signed presence heartbeats (F1)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity.parity_library_sync import SyncFolderError, SyncValidationError  # noqa: E402
from pkg.parity.parity_presence import (  # noqa: E402
    HEARTBEAT_TTL_SECONDS,
    heartbeat_expired,
    heartbeat_id,
    make_heartbeat,
    presence_opted_in,
    presence_settings,
    presence_slot,
    project_activity,
    read_heartbeats,
    set_presence_opt_in,
    validate_heartbeat,
    write_heartbeat,
)


NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)


def heartbeat(**overrides):
    base = {
        "member_id": "m1",
        "device_id": "dev-a",
        "display_name": "Alice",
        "avatar_color": "violet",
        "game_id": "g-1",
        "game_name": "Mario Kart",
        "platform": "SNES",
        "started_at": NOW - timedelta(minutes=12),
        "sent_at": NOW,
    }
    base.update(overrides)
    return make_heartbeat(**base)


class HeartbeatValidationTests(unittest.TestCase):
    def test_make_and_validate_round_trip(self):
        event = heartbeat()
        self.assertEqual(event["kind"], "heartbeat")
        self.assertEqual(event["event_id"], heartbeat_id(event))
        self.assertEqual(validate_heartbeat(event), event)
        self.assertEqual(event["expires_at"], (NOW + timedelta(seconds=HEARTBEAT_TTL_SECONDS)).isoformat())

    def test_tampering_is_rejected(self):
        event = heartbeat()
        event["game_name"] = "Spoofed"
        with self.assertRaises(SyncValidationError):
            validate_heartbeat(event)

    def test_bad_activity_and_bounds(self):
        with self.assertRaises(SyncValidationError):
            heartbeat(activity="streaming")
        with self.assertRaises(SyncValidationError):
            heartbeat(member_id="")
        with self.assertRaises(SyncValidationError):
            heartbeat(ttl_seconds=10)
        with self.assertRaises(SyncValidationError):
            heartbeat(started_at=NOW + timedelta(hours=1))

    def test_game_night_hint_is_bounded(self):
        event = heartbeat(game_night={"seed": "abcd1234", "players": 3, "junk": "drop"})
        self.assertEqual(event["game_night"], {"seed": "abcd1234", "players": 3})
        with self.assertRaises(SyncValidationError):
            heartbeat(game_night={"players": 99})

    def test_expired_flag(self):
        fresh = heartbeat()
        self.assertFalse(heartbeat_expired(fresh, now=NOW + timedelta(minutes=9)))
        self.assertTrue(heartbeat_expired(fresh, now=NOW + timedelta(minutes=11)))
        self.assertTrue(heartbeat_expired({"junk": True}, now=NOW))


class ProjectionTests(unittest.TestCase):
    def test_projects_elapsed_and_join_hint(self):
        event = heartbeat(game_night={"seed": "abcd1234", "players": 4})
        result = project_activity([event], now=NOW + timedelta(minutes=2))
        self.assertEqual(result["count"], 1)
        member = result["members"][0]
        self.assertEqual(member["member_id"], "m1")
        self.assertEqual(member["elapsed_seconds"], 14 * 60)
        self.assertEqual(member["join_hint"], "game-night")
        self.assertEqual(member["game_night"]["players"], 4)

    def test_expired_and_duplicate_events_drop(self):
        stale = heartbeat(sent_at=NOW - timedelta(minutes=30), started_at=NOW - timedelta(minutes=40))
        newer = heartbeat(sent_at=NOW, game_name="Newer")
        older = heartbeat(sent_at=NOW - timedelta(minutes=5), game_name="Older")
        other = heartbeat(member_id="m2", device_id="dev-b", display_name="Bob")
        result = project_activity([stale, newer, older, other], now=NOW)
        self.assertEqual(result["count"], 2)
        names = {(item["member_id"], item["game_name"]) for item in result["members"]}
        self.assertEqual(names, {("m1", "Newer"), ("m2", "Mario Kart")})
        self.assertEqual(result["dropped"], 1)

    def test_invalid_events_never_project(self):
        result = project_activity([{"format": 1, "kind": "heartbeat"}], now=NOW)
        self.assertEqual(result["count"], 0)


class FolderTransportTests(unittest.TestCase):
    def test_one_slot_per_member_and_expired_pruning(self):
        import json

        with tempfile.TemporaryDirectory() as directory:
            write_heartbeat(directory, heartbeat(sent_at=NOW))
            second = heartbeat(sent_at=NOW + timedelta(minutes=2), game_name="Second")
            path = write_heartbeat(directory, second)
            presence_dir = Path(directory) / "openbox-household-v1" / "presence"
            self.assertEqual(path, presence_dir / f"{presence_slot('m1', 'dev-a')}.json")
            self.assertEqual(len(list(presence_dir.glob("*.json"))), 1)

            # A planted-but-expired signed event is pruned on read and never
            # projects; a live second member still does.
            stale = heartbeat(member_id="m2", device_id="dev-b", sent_at=NOW - timedelta(minutes=20),
                              started_at=NOW - timedelta(minutes=30))
            (presence_dir / f"{presence_slot('m2', 'dev-b')}.json").write_text(json.dumps(stale))
            live = read_heartbeats(directory, now=NOW + timedelta(minutes=2))
            self.assertEqual([item["member_id"] for item in live], ["m1"])
            remaining = sorted(item.name for item in presence_dir.glob("*.json"))
            self.assertEqual(remaining, [f"{presence_slot('m1', 'dev-a')}.json"])

    def test_tampered_file_is_ignored(self):
        import json

        with tempfile.TemporaryDirectory() as directory:
            write_heartbeat(directory, heartbeat())
            presence_dir = Path(directory) / "openbox-household-v1" / "presence"
            target = next(presence_dir.glob("*.json"))
            payload = json.loads(target.read_text())
            payload["game_name"] = "Spoofed"
            target.write_text(json.dumps(payload))
            self.assertEqual(read_heartbeats(directory, now=NOW), [])


class OptInStateTests(unittest.TestCase):
    def test_off_by_default_and_per_member(self):
        state = {}
        self.assertEqual(presence_settings(state), {"opted_in": {}, "toast": False})
        self.assertFalse(presence_opted_in(state, "m1"))
        settings = set_presence_opt_in(state, "m1", True, toast=True)
        self.assertEqual(settings, {"opted_in": {"m1": True}, "toast": True})
        self.assertTrue(presence_opted_in(state, "m1"))
        self.assertFalse(presence_opted_in(state, "m2"))
        set_presence_opt_in(state, "m1", False)
        self.assertFalse(presence_opted_in(state, "m1"))

    def test_presence_settings_survives_records_bucket(self):
        state = {"household": {"presence": {"opted_in": {"m1": True}, "toast": False}, "records": {}}}
        self.assertTrue(presence_opted_in(state, "m1"))

    def test_presence_settings_ignores_malformed_shapes(self):
        self.assertEqual(presence_settings({"household": []}), {"opted_in": {}, "toast": False})
        self.assertEqual(presence_settings({"household": {"presence": "on"}}), {"opted_in": {}, "toast": False})
        state = {"household": {"presence": {"opted_in": {"": True, 5: True, "m1": False, "m2": True}, "toast": 1}}}
        self.assertEqual(presence_settings(state), {"opted_in": {"m2": True}, "toast": True})

    def test_opt_in_limit(self):
        state = {}
        for index in range(256):
            set_presence_opt_in(state, f"m{index}", True)
        with self.assertRaises(SyncValidationError):
            set_presence_opt_in(state, "overflow", True)


class DefensiveBranchTests(unittest.TestCase):
    def test_text_and_timestamp_validation(self):
        with self.assertRaises(SyncValidationError):
            heartbeat(member_id=123)
        with self.assertRaises(SyncValidationError):
            heartbeat(display_name="x" * 121)
        with self.assertRaises(SyncValidationError):
            heartbeat(game_name="bad\x07control")
        with self.assertRaises(SyncValidationError):
            heartbeat(ttl_seconds="soon")
        with self.assertRaises(SyncValidationError):
            heartbeat(started_at="not-a-date")
        with self.assertRaises(SyncValidationError):
            heartbeat(game_night="party")
        with self.assertRaises(SyncValidationError):
            heartbeat(game_night={"players": True})
        with self.assertRaises(SyncValidationError):
            heartbeat(game_night={"players": "many"})
        with self.assertRaises(SyncValidationError):
            heartbeat(game_night={"seed": "x" * 200})

    def test_validate_shape_failures(self):
        cases = []

        extra = heartbeat()
        extra["extra"] = True
        cases.append(extra)

        missing = heartbeat()
        missing.pop("sent_at")
        missing["event_id"] = heartbeat_id(missing)
        cases.append(missing)

        wrong_format = heartbeat()
        wrong_format["format"] = 2
        wrong_format["event_id"] = heartbeat_id(wrong_format)
        cases.append(wrong_format)

        bad_id = heartbeat()
        bad_id["event_id"] = "zz"
        cases.append(bad_id)

        bad_sent = heartbeat()
        bad_sent["sent_at"] = "nope"
        bad_sent["event_id"] = heartbeat_id(bad_sent)
        cases.append(bad_sent)

        zero_ttl = heartbeat()
        zero_ttl["expires_at"] = zero_ttl["sent_at"]
        zero_ttl["event_id"] = heartbeat_id(zero_ttl)
        cases.append(zero_ttl)

        long_ttl = heartbeat()
        long_ttl["expires_at"] = (NOW + timedelta(days=2)).isoformat()
        long_ttl["event_id"] = heartbeat_id(long_ttl)
        cases.append(long_ttl)

        bad_started = heartbeat()
        bad_started["started_at"] = "later"
        bad_started["event_id"] = heartbeat_id(bad_started)
        cases.append(bad_started)

        for case in cases:
            with self.subTest(case=list(case)):
                with self.assertRaises(SyncValidationError):
                    validate_heartbeat(case)
        with self.assertRaises(SyncValidationError):
            validate_heartbeat("not-a-heartbeat")
        with self.assertRaises(SyncValidationError):
            validate_heartbeat({"format": 1, "kind": "heartbeat"})

    def test_naive_timestamps_are_treated_as_utc(self):
        event = heartbeat()
        event["sent_at"] = "2026-09-16T12:00:00"
        event["expires_at"] = "2026-09-16T12:10:00"
        event["started_at"] = "2026-09-16T11:50:00"
        event["event_id"] = heartbeat_id(event)
        checked = validate_heartbeat(event)
        self.assertEqual(checked["sent_at"], "2026-09-16T12:00:00")
        self.assertTrue(heartbeat_expired(checked, now="2026-09-16T12:11:00"))

    def test_empty_game_night_is_dropped(self):
        event = heartbeat()
        event["game_night"] = {}
        event["event_id"] = heartbeat_id(event)
        checked = validate_heartbeat(event)
        self.assertNotIn("game_night", checked)

    def test_project_activity_handles_bad_input_and_clamps_elapsed(self):
        self.assertEqual(project_activity("junk")["count"], 0)
        old = heartbeat(started_at=NOW - timedelta(days=400))
        result = project_activity([old], now=NOW)
        self.assertEqual(result["members"][0]["elapsed_seconds"], 7 * 24 * 3600)

    def test_slot_and_device_helpers(self):
        with self.assertRaises(SyncValidationError):
            presence_slot("", "dev")
        with self.assertRaises(SyncValidationError):
            presence_slot("m1", "dev\x01")
        from pkg.parity.parity_presence import make_heartbeat, new_device_id

        self.assertEqual(len(new_device_id()), 32)
        event = make_heartbeat(
            member_id="m1", device_id="dev-a", game_id="g-9", game_name="Nine",
        )
        self.assertEqual(event["game_id"], "g-9")
        self.assertEqual(event["game_name"], "Nine")

    def test_clear_heartbeat_removes_only_its_slot(self):
        from pkg.parity.parity_presence import clear_heartbeat

        with tempfile.TemporaryDirectory() as directory:
            write_heartbeat(directory, heartbeat())
            self.assertTrue(clear_heartbeat(directory, member_id="m1", device_id="dev-a"))
            self.assertFalse(clear_heartbeat(directory, member_id="m1", device_id="dev-a"))
        self.assertFalse(clear_heartbeat("/nonexistent-openbox-folder", member_id="m1", device_id="dev-a"))

    def test_read_rejects_too_many_files_and_skips_junk(self):
        with tempfile.TemporaryDirectory() as directory:
            write_heartbeat(directory, heartbeat())
            presence_dir = Path(directory) / "openbox-household-v1" / "presence"
            (presence_dir / "not-a-slot.json").write_text("{}")
            (presence_dir / f"{presence_slot('m9', 'dev-z')}.json").write_text('{"truncated"')
            live = read_heartbeats(directory, now=NOW)
            self.assertEqual([item["member_id"] for item in live], ["m1"])

            for index in range(256):
                (presence_dir / f"{index:032x}.json").write_text("{}")
            with self.assertRaises(SyncValidationError):
                read_heartbeats(directory, now=NOW)

    def test_presence_directory_must_be_a_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            namespace = Path(directory) / "openbox-household-v1"
            namespace.mkdir()
            (namespace / "presence").write_text("file")
            with self.assertRaises(SyncFolderError):
                write_heartbeat(directory, heartbeat())


if __name__ == "__main__":
    unittest.main(verbosity=2)
