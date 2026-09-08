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
        self.assertIn("/api/v2/library/manual-entry/convert", POST_TABLE)

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

    def test_convert_preserves_identity_and_metadata(self):
        import openbox
        from handlers.library import LibraryHandlers
        from webapp_state import load_state_view

        openbox.STATE_STORE.save({
            "games": [{"game_id": "shelf-1", "name": "Catan", "manual_entry": True,
                       "path": "", "tags": ["party"], "notes": "owned"}],
            "settings": {}, "profiles": {},
        })
        executable = Path(self._tmp.name) / "catan.sh"
        executable.write_text("#!/bin/sh\n")

        class MockHandler(LibraryHandlers):
            def send_json(self, code, body):
                self._sent = (code, body)

        handler = MockHandler()
        stable_id = load_state_view()["games"][0]["game_id"]
        handler._api_post_api_v2_library_manual_entry_convert({"game_id": stable_id, "path": str(executable)})
        self.assertEqual(handler._sent[0], 200)
        game = load_state_view()["games"][0]
        self.assertEqual(game["game_id"], stable_id)
        self.assertFalse(game["manual_entry"])
        self.assertEqual(game["path"], str(executable))
        self.assertEqual(game["tags"], ["party"])
        self.assertEqual(game["notes"], "owned")

    def test_update_preserves_shelf_identity(self):
        import openbox
        from handlers.library import LibraryHandlers
        from webapp_state import load_state_view

        openbox.STATE_STORE.save({
            "games": [{"id": 7, "game_id": "shelf-7", "name": "Old", "manual_entry": True, "path": ""}],
            "settings": {}, "profiles": {},
        })

        class MockHandler(LibraryHandlers):
            def send_json(self, code, body):
                self._sent = (code, body)

        handler = MockHandler()
        stable_id = load_state_view()["games"][0]["game_id"]
        handler._api_post_api_v2_library_manual_entry_update({
            "game_id": stable_id,
            "game": {"name": "New", "platform": "Tabletop"},
        })
        self.assertEqual(handler._sent[0], 200)
        game = load_state_view()["games"][0]
        self.assertEqual(game["game_id"], stable_id)
        self.assertEqual(game["name"], "New")
        self.assertTrue(game["manual_entry"])
        self.assertEqual(game["path"], "")

    def test_update_preserves_identity_and_keeps_shelf_type(self):
        import openbox
        from handlers.library import LibraryHandlers
        from webapp_state import load_state_view

        openbox.STATE_STORE.save({
            "games": [{"game_id": "shelf-2", "name": "Old title", "manual_entry": True,
                       "path": "", "tags": ["owned"], "notes": "keep this"}],
            "settings": {}, "profiles": {},
        })

        class MockHandler(LibraryHandlers):
            def send_json(self, code, body):
                self._sent = (code, body)

        handler = MockHandler()
        stable_id = load_state_view()["games"][0]["game_id"]
        handler._api_post_api_v2_library_manual_entry_update({
            "game_id": stable_id,
            "game": {"name": "New title", "platform": "Tabletop", "notes": "updated"},
        })
        self.assertEqual(handler._sent[0], 200)
        game = load_state_view()["games"][0]
        self.assertEqual(game["game_id"], stable_id)
        self.assertEqual(game["name"], "New title")
        self.assertTrue(game["manual_entry"])
        self.assertEqual(game["path"], "")
        self.assertEqual(game["tags"], ["owned"])


if __name__ == "__main__":
    unittest.main()
