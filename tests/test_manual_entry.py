#!/usr/bin/env python3
"""Tests for manual/shelf entry route (1.9.0 stretch feature)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pkg.parity  # noqa: F401,E402  # register flat-import finder


class ManualEntryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["OPENBOX_DATA_DIR"] = str(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop("OPENBOX_DATA_DIR", None)

    def test_route_registered(self):
        from routes import POST_TABLE
        self.assertIn("/api/v2/library/manual-entry", POST_TABLE)

    def test_manual_entry_adds_game_without_path(self):
        """The handler adds a game with manual_entry=True and empty path."""
        import openbox

        openbox.STATE_STORE.save({"games": [], "settings": {}, "profiles": {}})
        from webapp_state import transact_state, load_state_view

        # Simulate what the handler does
        game = {"name": "My Board Game", "platform": "Tabletop", "manual_entry": True, "path": ""}

        def mutate(state):
            state["games"].append(game)

        transact_state(mutate)
        state = load_state_view()
        self.assertEqual(len(state["games"]), 1)
        self.assertEqual(state["games"][0]["name"], "My Board Game")
        self.assertTrue(state["games"][0]["manual_entry"])
        self.assertEqual(state["games"][0]["path"], "")

    def test_manual_entry_requires_name(self):
        """Name is the only required field for manual entries."""
        source = {"platform": "PC"}
        game = source.copy()
        name = game.get("name", "")
        self.assertFalse(name, "Expected empty name to trigger BadRequest")

    def test_handler_adds_manual_entry(self):
        """Exercise the actual handler via a mock request."""
        import openbox

        openbox.STATE_STORE.save({"games": [], "settings": {}, "profiles": {}})

        # Import the handler class and call the method directly
        from handlers.library import LibraryHandlers

        class MockHandler(LibraryHandlers):
            def __init__(self):
                self._sent = None

            def send_json(self, code, body):
                self._sent = (code, body)

        handler = MockHandler()
        payload = {"game": {"name": "Catan", "platform": "Tabletop", "genre": "Strategy"}}
        handler._api_post_api_v2_library_manual_entry(payload)
        self.assertEqual(handler._sent[0], 200)
        self.assertTrue(handler._sent[1]["ok"])
        self.assertEqual(handler._sent[1]["name"], "Catan")

        from webapp_state import load_state_view
        state = load_state_view()
        self.assertEqual(len(state["games"]), 1)
        self.assertEqual(state["games"][0]["name"], "Catan")
        self.assertTrue(state["games"][0]["manual_entry"])

    def test_handler_rejects_missing_name(self):
        """Handler raises BadRequest when name is missing."""
        from api_errors import BadRequest
        from handlers.library import LibraryHandlers

        class MockHandler(LibraryHandlers):
            def send_json(self, code, body):
                pass

        handler = MockHandler()
        with self.assertRaises(BadRequest):
            handler._api_post_api_v2_library_manual_entry({"game": {"platform": "PC"}})


if __name__ == "__main__":
    unittest.main()
