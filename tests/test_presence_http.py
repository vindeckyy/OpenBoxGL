"""Route-level tests for presence heartbeats and the household activity feed (F1)."""

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
from pkg.parity.parity_presence import set_presence_opt_in  # noqa: E402


class Handler:
    def __init__(self):
        self.responses = []

    def send_json(self, status, payload):
        self.responses.append((status, payload))


class PresenceRouteTests(unittest.TestCase):
    def setUp(self):
        member = make_member_record("m1", "Alice", device_id="dev-a")
        self.state = {
            "games": [],
            "history": [],
            "settings": {},
            "household": {"device_id": "dev-a", "records": {member["event_id"]: member}},
        }

    def _transact(self, callback):
        return self.state, callback(self.state)

    def _call_presence(self, payload):
        handler = Handler()
        with mock.patch.object(household, "load_state_view", return_value=self.state), mock.patch.object(
            household, "transact_state", side_effect=self._transact
        ):
            household.household_presence(handler, payload)
        return handler

    def test_opt_in_is_per_member_and_off_by_default(self):
        handler = self._call_presence({"member_id": "m1", "enabled": True, "toast": True})
        self.assertEqual(handler.responses[0][0], 200)
        self.assertEqual(handler.responses[0][1]["presence"], {"opted_in": {"m1": True}, "toast": True})

        other = self._call_presence({"member_id": "m1", "enabled": False})
        self.assertEqual(other.responses[0][1]["presence"]["opted_in"], {})

        with self.assertRaises(BadRequest) as raised:
            self._call_presence({"member_id": "m2", "enabled": True})
        self.assertEqual(raised.exception.code, "HOUSEHOLD_MEMBER_UNKNOWN")

        with self.assertRaises(BadRequest) as raised:
            self._call_presence({"member_id": "m1"})
        self.assertEqual(raised.exception.code, "HOUSEHOLD_INVALID_FIELD")

    def test_heartbeat_requires_opt_in_then_folder(self):
        with mock.patch.object(household, "load_state_view", return_value=self.state):
            with self.assertRaises(BadRequest) as raised:
                household.household_heartbeat(Handler(), {"member_id": "m1"})
        self.assertEqual(raised.exception.code, "HOUSEHOLD_PRESENCE_DISABLED")

        set_presence_opt_in(self.state, "m1", True)
        with mock.patch.object(household, "load_state_view", return_value=self.state):
            with self.assertRaises(BadRequest) as raised:
                household.household_heartbeat(Handler(), {"member_id": "m1"})
        self.assertEqual(raised.exception.code, "HOUSEHOLD_SYNC_FOLDER_REQUIRED")

    def test_heartbeat_writes_and_activity_projects(self):
        with tempfile.TemporaryDirectory() as directory:
            self.state["settings"]["cloud_folder"] = directory
            set_presence_opt_in(self.state, "m1", True)
            handler = Handler()
            with mock.patch.object(household, "load_state_view", return_value=self.state):
                household.household_heartbeat(handler, {
                    "member_id": "m1",
                    "display_name": "Alice",
                    "game_id": "g-1",
                    "game_name": "Mario Kart",
                    "platform": "SNES",
                    "game_night": {"seed": "abcd1234", "players": 4, "junk": "ignored"},
                })
            self.assertTrue(handler.responses[0][1]["ok"])
            self.assertTrue(handler.responses[0][1]["expires_at"])

            activity = Handler()
            with mock.patch.object(household, "load_state_view", return_value=self.state):
                household.household_activity(activity, SimpleNamespace(query=""))
            status, payload = activity.responses[0]
            self.assertEqual(status, 200)
            self.assertTrue(payload["available"])
            self.assertEqual(payload["count"], 1)
            member = payload["members"][0]
            self.assertEqual(member["member_id"], "m1")
            self.assertEqual(member["game_name"], "Mario Kart")
            self.assertEqual(member["join_hint"], "game-night")
            self.assertGreaterEqual(member["elapsed_seconds"], 0)
            self.assertEqual(member["game_night"], {"seed": "abcd1234", "players": 4})

    def test_activity_without_a_folder_is_honest(self):
        handler = Handler()
        with mock.patch.object(household, "load_state_view", return_value=self.state):
            household.household_activity(handler, SimpleNamespace(query=""))
        status, payload = handler.responses[0]
        self.assertEqual(status, 200)
        self.assertFalse(payload["available"])
        self.assertEqual(payload["reason"], "sync_folder")
        self.assertEqual(payload["members"], [])
        self.assertEqual(payload["presence"], {"opted_in": {}, "toast": False})

        self.state["settings"]["cloud_folder"] = str(Path(tempfile.gettempdir()) / "missing-openbox-folder")
        handler = Handler()
        with mock.patch.object(household, "load_state_view", return_value=self.state):
            household.household_activity(handler, SimpleNamespace(query=""))
        self.assertEqual(handler.responses[0][1]["reason"], "unavailable")

    def test_routes_and_manifest_include_presence(self):
        from routes import GET_TABLE, POST_TABLE
        from routes.registry import all_routes

        self.assertEqual(GET_TABLE["/api/v2/household/activity"], "handlers.household.household_activity")
        self.assertEqual(POST_TABLE["/api/v2/household/presence"], "handlers.household.household_presence")
        self.assertEqual(
            POST_TABLE["/api/v2/household/presence/heartbeat"],
            "handlers.household.household_heartbeat",
        )
        self.assertTrue(any(route.path == "/api/v2/household/activity" for route in all_routes()))
        manifest = (ROOT / "runtime_modules.txt").read_text()
        self.assertIn("pkg/parity/parity_presence.py", manifest)


if __name__ == "__main__":
    unittest.main(verbosity=2)
