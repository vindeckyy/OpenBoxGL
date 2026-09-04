"""Tests for handlers/constellation.py."""
from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import web_app  # noqa: E402
import handlers.constellation as constellation_module  # noqa: E402
from api_errors import BadRequest  # noqa: E402


def sample_state():
    return {
        "games": [
            {"id": 1, "game_id": "g-1", "name": "Quake", "platform": "PC", "genre": "Shooter", "playtime_seconds": 3600, "has_cover": False},
            {"id": 2, "game_id": "g-2", "name": "Doom", "platform": "PC", "genre": "Shooter", "playtime_seconds": 1800, "has_cover": False},
            {"id": 3, "game_id": "g-3", "name": "Chrono Trigger", "platform": "SNES", "genre": "RPG", "playtime_seconds": 0, "has_cover": False},
        ],
        "history": [],
    }


class ConstellationHandlerTest(unittest.TestCase):
    def handler(self, query=""):
        h = web_app.Handler.__new__(web_app.Handler)
        h.responses = []
        h.headers = {}
        h.sent_headers = []
        h.send_json = lambda status, payload: h.responses.append((status, payload))
        h.send_response = lambda status: h.sent_headers.append(("status", status))
        h.headers_common = lambda content_type: h.sent_headers.append(("content-type", content_type))
        h.send_header = lambda name, value: h.sent_headers.append((name, value))
        h.end_headers = lambda: None
        h.wfile = io.BytesIO()
        return h

    def test_happy(self):
        original_load = constellation_module.load_state
        try:
            constellation_module.load_state = sample_state
            h = self.handler()
            h._api_get_api_v2_library_constellation(urlparse("/api/v2/library/constellation?kinds=genre&limit=200"))
            status, payload = h.responses[0]
            self.assertEqual(status, 200)
            self.assertEqual(len(payload["nodes"]), 3)
            self.assertTrue(payload["edges"])
        finally:
            constellation_module.load_state = original_load

    def test_bad_kind(self):
        original_load = constellation_module.load_state
        try:
            constellation_module.load_state = sample_state
            h = self.handler()
            with self.assertRaises(BadRequest):
                h._api_get_api_v2_library_constellation(urlparse("/api/v2/library/constellation?kinds=invalid"))
        finally:
            constellation_module.load_state = original_load

    def test_bad_limit(self):
        original_load = constellation_module.load_state
        try:
            constellation_module.load_state = sample_state
            h = self.handler()
            with self.assertRaises(BadRequest):
                h._api_get_api_v2_library_constellation(urlparse("/api/v2/library/constellation?limit=10"))
        finally:
            constellation_module.load_state = original_load


if __name__ == "__main__":
    unittest.main(verbosity=2)
