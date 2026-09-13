"""Route-level tests for the local Arcade Room Museum kiosk boundary."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api_errors import BadRequest  # noqa: E402
from handlers import arcade  # noqa: E402


class Handler:
    def __init__(self):
        self.responses = []

    def send_json(self, status, payload):
        self.responses.append((status, payload))


class ArcadeKioskRouteTests(unittest.TestCase):
    def setUp(self):
        self.state = {"settings": {}}

    def _transact(self, callback):
        return self.state, callback(self.state)

    def test_hash_is_salted_and_verifies_without_public_hash(self):
        first = arcade.hash_kiosk_pin("1234", salt=b"0123456789abcdef")
        second = arcade.hash_kiosk_pin("1234", salt=b"fedcba9876543210")
        self.assertNotEqual(first, second)
        self.assertTrue(arcade.verify_kiosk_pin(first, "1234"))
        self.assertFalse(arcade.verify_kiosk_pin(first, "9999"))
        self.assertFalse(arcade.verify_kiosk_pin("not-a-digest", "1234"))
        with self.assertRaises(BadRequest):
            arcade.hash_kiosk_pin("12")

    def test_set_verify_status_and_clear(self):
        with mock.patch.object(arcade, "transact_state", side_effect=self._transact), mock.patch.object(
            arcade, "load_state_view", return_value=self.state
        ):
            set_handler = Handler()
            arcade.kiosk_pin(set_handler, {"pin": "2468", "enabled": True})
            stored = self.state["settings"]["museum_kiosk_pin_hash"]
            self.assertTrue(stored.startswith("pbkdf2-sha256$"))
            self.assertNotEqual(stored, "2468")
            self.assertEqual(set_handler.responses[-1][1], {"enabled": True, "pin_set": True})

            verify_handler = Handler()
            arcade.kiosk_verify(verify_handler, {"pin": "2468"})
            self.assertTrue(verify_handler.responses[-1][1]["ok"])
            arcade.kiosk_verify(verify_handler, {"pin": "0000"})
            self.assertFalse(verify_handler.responses[-1][1]["ok"])

            status_handler = Handler()
            arcade.kiosk_status(status_handler, SimpleNamespace(query=""))
            self.assertEqual(status_handler.responses[-1][1], {"enabled": True, "pin_set": True})

            clear_handler = Handler()
            arcade.kiosk_pin(clear_handler, {"clear": True, "enabled": False})
            self.assertEqual(clear_handler.responses[-1][1], {"enabled": False, "pin_set": False})

    def test_pin_route_rejects_ambiguous_or_malformed_requests(self):
        with self.assertRaises(BadRequest) as raised:
            arcade.kiosk_pin(Handler(), {"pin": "1234", "clear": True})
        self.assertEqual(raised.exception.code, "ARCADE_INVALID_REQUEST")
        with self.assertRaises(BadRequest) as raised:
            arcade.kiosk_pin(Handler(), {"pin": "abcd"})
        self.assertEqual(raised.exception.code, "ARCADE_INVALID_PIN")
        with self.assertRaises(BadRequest):
            arcade.kiosk_verify(Handler(), {"pin": "x" * 13})

    def test_routes_and_manifest_include_kiosk_surface(self):
        from routes import GET_TABLE, POST_TABLE
        from routes.registry import all_routes

        self.assertEqual(GET_TABLE["/api/v2/arcade/kiosk/status"], "handlers.arcade.kiosk_status")
        self.assertEqual(POST_TABLE["/api/v2/arcade/kiosk/verify"], "handlers.arcade.kiosk_verify")
        self.assertTrue(any(route.path == "/api/v2/arcade/kiosk/pin" for route in all_routes()))
        self.assertIn("handlers/arcade.py", (ROOT / "runtime_modules.txt").read_text())


if __name__ == "__main__":
    unittest.main()
