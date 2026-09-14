#!/usr/bin/env python3
"""Route-level tests for smart collections and the game story endpoint (1.12.0)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api_errors import BadRequest, NotFound  # noqa: E402
from handlers import collections as collections_handler  # noqa: E402
from handlers.insights import InsightsHandlers  # noqa: E402


class Handler:
    def __init__(self):
        self.responses = []

    def send_json(self, status, payload):
        self.responses.append((status, payload))

    def authorized(self):
        return True


class StoryHandler(Handler, InsightsHandlers):
    """Mirrors the real HTTP handler, which composes InsightsHandlers."""


def _state():
    return {
        "games": [
            {"game_id": "g-1", "name": "Quake", "platform": "PC", "id": 0, "favorite": True},
            {"game_id": "g-2", "name": "Tetris", "platform": "GB", "id": 1},
        ],
        "history": [
            {"game_id": "g-1", "game": "Quake", "started": "2026-01-12T18:00:00", "seconds": 3600},
        ],
        "settings": {},
    }


class CollectionsRouteTests(unittest.TestCase):
    def _transact(self, callback):
        return self.state, callback(self.state)

    def setUp(self):
        self.state = _state()

    def test_save_list_delete_round_trip(self):
        with mock.patch.object(collections_handler.openbox, "load_state", return_value=self.state), mock.patch.object(
            collections_handler, "transact_state", side_effect=self._transact
        ):
            save_handler = Handler()
            collections_handler.collections_save(save_handler, {"name": "Faves", "query": "favorite"})
            self.assertEqual(save_handler.responses[0][1], {"ok": True, "saved": "Faves"})

            list_handler = Handler()
            collections_handler.collections_list(list_handler, SimpleNamespace(query=""))
            items = list_handler.responses[0][1]["items"]
            self.assertEqual(items, [{"name": "Faves", "query": "favorite", "count": 1}])

            delete_handler = Handler()
            collections_handler.collections_delete(delete_handler, {"name": "Faves"})
            self.assertTrue(delete_handler.responses[0][1]["ok"])
            self.assertEqual(self.state["smart_collections"], [])

    def test_save_rejects_blank_query(self):
        with mock.patch.object(collections_handler, "transact_state", side_effect=self._transact):
            with self.assertRaises(BadRequest) as raised:
                collections_handler.collections_save(Handler(), {"name": "Empty", "query": "  "})
            self.assertEqual(raised.exception.code, "COLLECTION_INVALID")

    def test_delete_missing_is_404(self):
        with mock.patch.object(collections_handler, "transact_state", side_effect=self._transact):
            with self.assertRaises(NotFound) as raised:
                collections_handler.collections_delete(Handler(), {"name": "Nope"})
            self.assertEqual(raised.exception.code, "COLLECTION_NOT_FOUND")


class StoryRouteTests(unittest.TestCase):
    def test_story_returns_ordered_events(self):
        handler = StoryHandler()
        with mock.patch("handlers.insights.load_state", return_value=_state()):
            handler._api_get_api_v2_story(SimpleNamespace(query="game_id=g-1"))
        status, payload = handler.responses[0]
        self.assertEqual(status, 200)
        self.assertEqual(payload["name"], "Quake")
        self.assertEqual(payload["totals"]["sessions"], 1)
        self.assertTrue(payload["events"])

    def test_story_unknown_game_is_400(self):
        with mock.patch("handlers.insights.load_state", return_value=_state()):
            with self.assertRaises(BadRequest) as raised:
                StoryHandler()._api_get_api_v2_story(SimpleNamespace(query="game_id=missing"))
        self.assertEqual(raised.exception.code, "GAME_NOT_FOUND")


if __name__ == "__main__":
    unittest.main(verbosity=2)
