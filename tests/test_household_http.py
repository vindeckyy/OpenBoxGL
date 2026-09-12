"""Route-level tests for local-first household records."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api_errors import BadRequest  # noqa: E402
from handlers import household  # noqa: E402
from pkg.parity.parity_household import make_member_record  # noqa: E402


class Handler:
    def __init__(self):
        self.responses = []

    def send_json(self, status, payload):
        self.responses.append((status, payload))


class HouseholdRouteTests(unittest.TestCase):
    def setUp(self):
        self.state = {"games": [], "history": [], "settings": {}}

    def _transact(self, callback):
        return self.state, callback(self.state)

    def test_status_and_leaderboard_are_read_only(self):
        handler = Handler()
        with mock.patch.object(household, "load_state_view", return_value=self.state):
            household.household_status(handler, SimpleNamespace(query=""))
            household.household_leaderboard(handler, SimpleNamespace(query="period=weekly"))
        self.assertEqual(handler.responses[0][0], 200)
        self.assertEqual(handler.responses[0][1]["records"], [])
        self.assertFalse(handler.responses[1][1]["leaderboard"]["available"])

        with self.assertRaises(BadRequest) as raised:
            household.household_status(Handler(), SimpleNamespace(query="period=yearly"))
        self.assertEqual(raised.exception.code, "HOUSEHOLD_INVALID_FIELD")

    def test_member_challenge_and_result_routes_commit_copy_on_write_records(self):
        with mock.patch.object(household, "load_state_view", return_value=self.state), mock.patch.object(
            household, "transact_state", side_effect=self._transact
        ), mock.patch.object(household, "broadcast_event"):
            member_handler = Handler()
            household.household_member(member_handler, {
                "member_id": "m1", "display_name": "Alice", "avatar_color": "violet",
            })
            challenge_handler = Handler()
            household.household_challenge(challenge_handler, {
                "challenge_id": "c1", "title": "Beat one", "created_by": "m1", "target": 1,
            })
            result_handler = Handler()
            household.household_result(result_handler, {
                "challenge_id": "c1", "member_id": "m1", "value": 1, "completed": True,
            })
        self.assertEqual(member_handler.responses[-1][1]["record"]["kind"], "member")
        self.assertEqual(challenge_handler.responses[-1][1]["record"]["kind"], "challenge")
        self.assertTrue(result_handler.responses[-1][1]["challenges"][0]["completed"])
        self.assertEqual(len(self.state["household"]["records"]), 3)

    def test_stats_sharing_merge_and_raw_record_routes(self):
        with mock.patch.object(household, "transact_state", side_effect=self._transact):
            with self.assertRaises(BadRequest) as raised:
                household.household_share(Handler(), {"member_id": "m1", "period": "weekly"})
        self.assertEqual(raised.exception.code, "HOUSEHOLD_STATS_DISABLED")

        self.state["settings"]["household_stats_sharing"] = True
        with mock.patch.object(household, "load_state_view", return_value=self.state), mock.patch.object(
            household, "transact_state", side_effect=self._transact
        ), mock.patch.object(household, "broadcast_event"):
            share_handler = Handler()
            household.household_share(share_handler, {"member_id": "m1", "period": "weekly"})
            incoming = make_member_record("m2", "Bob", device_id="peer")
            merge_handler = Handler()
            household.household_merge(merge_handler, {"records": [incoming]})
            raw_handler = Handler()
            household.household_record(raw_handler, {"record": make_member_record("m3", "Cara")})
        self.assertTrue(share_handler.responses[-1][1]["shared"])
        self.assertEqual(merge_handler.responses[-1][1]["merge"]["applied"], 1)
        self.assertEqual(raw_handler.responses[-1][1]["record"]["kind"], "member")

    def test_shared_folder_publish_pull_routes_exchange_isolated_states(self):
        with tempfile.TemporaryDirectory() as directory:
            self.state["settings"]["cloud_folder"] = directory
            with mock.patch.object(household, "load_state_view", return_value=self.state), mock.patch.object(
                household, "transact_state", side_effect=self._transact
            ), mock.patch.object(household, "broadcast_event"):
                household.household_member(Handler(), {
                    "member_id": "m1", "display_name": "Alice", "avatar_color": "blue",
                })
                published_handler = Handler()
                household.household_sync_publish(published_handler, {})

            published = published_handler.responses[-1][1]["sync"]
            self.assertEqual(published["published"], 1)
            self.assertEqual(published["acknowledged"], 1)
            self.assertEqual(self.state["household"]["outbox"], [])

            destination = {"games": [], "history": [], "settings": {"cloud_folder": directory}}
            self.state = destination
            with mock.patch.object(household, "load_state_view", return_value=self.state), mock.patch.object(
                household, "transact_state", side_effect=self._transact
            ), mock.patch.object(household, "broadcast_event"):
                pulled_handler = Handler()
                household.household_sync_pull(pulled_handler, {})
                repeated_handler = Handler()
                household.household_sync_pull(repeated_handler, {})

            self.assertEqual(pulled_handler.responses[-1][1]["sync"]["applied"], 1)
            self.assertEqual(repeated_handler.responses[-1][1]["sync"]["applied"], 0)
            self.assertEqual(next(iter(self.state["household"]["records"].values()))["payload"]["member_id"], "m1")

    def test_validation_errors_are_stable_and_bounded(self):
        with self.assertRaises(BadRequest) as raised:
            household.household_member(Handler(), {"member_id": "", "display_name": "Alice"})
        self.assertEqual(raised.exception.code, "HOUSEHOLD_INVALID_FIELD")

        with self.assertRaises(BadRequest) as raised:
            household.household_challenge(Handler(), {"title": "Bad", "target": 0})
        self.assertEqual(raised.exception.code, "HOUSEHOLD_INVALID_FIELD")

        with self.assertRaises(BadRequest) as raised:
            household.household_merge(Handler(), {"records": "not-a-list"})
        self.assertEqual(raised.exception.code, "HOUSEHOLD_INVALID_REQUEST")

    def test_routes_and_runtime_manifest_include_household(self):
        from routes import GET_TABLE, POST_TABLE, PUBLIC_GET_PATHS
        from routes.registry import all_routes

        self.assertEqual(GET_TABLE["/api/v2/household"], "handlers.household.household_status")
        self.assertEqual(POST_TABLE["/api/v2/household/member"], "handlers.household.household_member")
        self.assertIn("/static/household.js", PUBLIC_GET_PATHS)
        self.assertTrue(any(route.path == "/api/v2/household/merge" for route in all_routes()))
        self.assertTrue(any(route.path == "/api/v2/household/sync/publish" for route in all_routes()))
        self.assertTrue(any(route.path == "/api/v2/household/sync/pull" for route in all_routes()))
        self.assertIn("handlers/household.py", (ROOT / "runtime_modules.txt").read_text())


if __name__ == "__main__":
    unittest.main()
