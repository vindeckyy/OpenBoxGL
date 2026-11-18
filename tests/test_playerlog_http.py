"""HTTP handler tests for Player's Log routes and problem codes."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api_errors import (  # noqa: E402
    CapsuleBodyTooLarge,
    CapsuleInvalidDate,
    CapsuleLocked,
    CapsuleNotFound,
)
from handlers import playerlog as handler_mod  # noqa: E402


class MockHandler:
    def __init__(self):
        self.responses = []

    def send_json(self, status, payload):
        self.responses.append((status, payload))


class PlayerLogHttpTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.data_file = self.tmp / "data" / "state.json"
        self.data_file.parent.mkdir(parents=True)
        self.state = {
            "games": [
                {"game_id": "g-1", "name": "Okami", "platform": "PS2", "playtime_seconds": 3600 * 12, "added_at": "2020-01-01"},
            ],
            "history": [],
            "time_capsules": [],
            "notifications": [],
            "ui_state": {},
        }

    def tearDown(self):
        self._tmp.cleanup()

    def _transact(self, fn):
        return self.state, fn(self.state)

    def test_all_six_routes_and_error_codes(self):
        today = date.today()
        tomorrow = (today + timedelta(days=1)).isoformat()
        due_date = today.isoformat()

        with mock.patch.object(handler_mod, "DATA", self.data_file), mock.patch.object(
            handler_mod, "load_state_view", return_value=self.state
        ), mock.patch.object(handler_mod, "transact_state", side_effect=self._transact):
            # Validation errors
            with self.assertRaises(CapsuleInvalidDate) as e_date:
                handler_mod.capsules_create(MockHandler(), {"title": "Bad", "body": "hi", "unlock_at": "bad"})
            self.assertEqual(e_date.exception.code, "CAPSULE_INVALID_DATE")

            with self.assertRaises(CapsuleBodyTooLarge) as e_size:
                handler_mod.capsules_create(MockHandler(), {"title": "Big", "body": "a" * 4001, "unlock_at": tomorrow})
            self.assertEqual(e_size.exception.code, "CAPSULE_BODY_TOO_LARGE")

            # Create sealed future capsule -> locked
            h1 = MockHandler()
            handler_mod.capsules_create(h1, {"game_id": "g-1", "title": "Tomorrow", "body": "Hidden body", "unlock_at": tomorrow})
            self.assertEqual(h1.responses[0][0], 201)
            future_id = h1.responses[0][1]["capsule"]["id"]
            self.assertNotIn("body", h1.responses[0][1]["capsule"])

            with self.assertRaises(CapsuleLocked) as e_lock:
                handler_mod.capsules_open(MockHandler(), {"id": future_id})
            self.assertEqual(e_lock.exception.code, "CAPSULE_LOCKED")

            # Create due capsule, list & next stay redacted until opened
            h2 = MockHandler()
            handler_mod.capsules_create(h2, {"game_id": "g-1", "title": "Now", "body": "Visible after open", "unlock_at": due_date})
            due_id = h2.responses[0][1]["capsule"]["id"]

            h_list = MockHandler()
            handler_mod.capsules_list(h_list, SimpleNamespace(query="game_id=g-1"))
            self.assertEqual(len(h_list.responses[0][1]["capsules"]), 2)
            for item in h_list.responses[0][1]["capsules"]:
                self.assertNotIn("body", item)

            h_next = MockHandler()
            handler_mod.capsules_next(h_next, SimpleNamespace(query=""))
            self.assertEqual(h_next.responses[0][1]["due_count"], 1)
            self.assertNotIn("body", h_next.responses[0][1]["capsule"])

            # Open due capsule -> 200 with body
            h_open = MockHandler()
            handler_mod.capsules_open(h_open, {"id": due_id})
            self.assertEqual(h_open.responses[0][0], 200)
            self.assertEqual(h_open.responses[0][1]["capsule"]["body"], "Visible after open")

            # Delete capsule & verify 404 thereafter
            h_del = MockHandler()
            handler_mod.capsules_delete(h_del, {"id": future_id})
            self.assertTrue(h_del.responses[0][1]["deleted"])
            with self.assertRaises(CapsuleNotFound) as e_miss:
                handler_mod.capsules_open(MockHandler(), {"id": future_id})
            self.assertEqual(e_miss.exception.code, "CAPSULE_NOT_FOUND")

            # Milestones endpoint & mark_seen=1
            h_m1 = MockHandler()
            handler_mod.insights_milestones(h_m1, SimpleNamespace(query="mark_seen=1"))
            self.assertEqual(h_m1.responses[0][0], 200)
            self.assertGreaterEqual(h_m1.responses[0][1]["counts"]["unlocked"], 1)

            h_m2 = MockHandler()
            handler_mod.insights_milestones(h_m2, SimpleNamespace(query=""))
            self.assertEqual(h_m2.responses[0][1]["counts"]["new"], 0)


if __name__ == "__main__":
    unittest.main()
