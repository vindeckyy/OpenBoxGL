"""Tests for handlers/picker.py."""
from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path

from api_errors import BadRequest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import web_app  # noqa: E402
import handlers.picker as picker_module  # noqa: E402


def sample_state():
    return {
        "games": [
            {"id": 1, "name": "Quake", "platform": "PC", "genre": "FPS", "year": 1996, "play_count": 5, "playtime_seconds": 5400, "path_exists": True, "favorite": False, "rating": 5, "max_players": 1, "has_cover": False},
            {"id": 2, "name": "Mario Kart", "platform": "SNES", "genre": "Racing", "year": 1992, "play_count": 0, "playtime_seconds": 0, "path_exists": True, "favorite": True, "rating": 0, "max_players": 4, "has_cover": False},
        ],
        "history": [
            {"game_id": 1, "seconds": 1800, "started": "2026-01-01T00:00:00Z"},
        ],
        "playlists": [],
    }


class PickerHandlerTest(unittest.TestCase):
    def handler(self, body=b"{}"):
        h = web_app.Handler.__new__(web_app.Handler)
        h.responses = []
        h.headers = {"Content-Length": str(len(body))}
        h.rfile = io.BytesIO(body)
        h.sent_headers = []
        h.send_json = lambda status, payload: h.responses.append((status, payload))
        h.send_response = lambda status: h.sent_headers.append(("status", status))
        h.headers_common = lambda content_type: h.sent_headers.append(("content-type", content_type))
        h.send_header = lambda name, value: h.sent_headers.append((name, value))
        h.end_headers = lambda: None
        h.wfile = io.BytesIO()
        return h

    def test_picker_happy(self):
        original_load = picker_module.load_state
        try:
            picker_module.load_state = sample_state
            body = json.dumps({"mood": "action", "players": 1, "minutes": 60}).encode()
            h = self.handler(body)
            h._api_post_api_v2_library_pick(json.loads(body))
            status, payload = h.responses[0]
            self.assertEqual(status, 200)
            self.assertIn("picks", payload)
            self.assertEqual(len(payload["picks"]), 1)
            self.assertEqual(payload["picks"][0]["game_id"], "1")
        finally:
            picker_module.load_state = original_load

    def test_picker_bad_mood(self):
        original_load = picker_module.load_state
        try:
            picker_module.load_state = sample_state
            body = json.dumps({"mood": "spooky"}).encode()
            h = self.handler(body)
            with self.assertRaises(BadRequest):
                h._api_post_api_v2_library_pick(json.loads(body))
        finally:
            picker_module.load_state = original_load

    def test_picker_playlist_not_found(self):
        original_load = picker_module.load_state
        try:
            picker_module.load_state = sample_state
            body = json.dumps({"scope": "playlist", "scope_name": "missing"}).encode()
            h = self.handler(body)
            with self.assertRaises(BadRequest):
                h._api_post_api_v2_library_pick(json.loads(body))
        finally:
            picker_module.load_state = original_load

    def test_picker_platform_scope(self):
        original_load = picker_module.load_state
        try:
            picker_module.load_state = sample_state
            body = json.dumps({"scope": "platform", "scope_name": "SNES"}).encode()
            h = self.handler(body)
            h._api_post_api_v2_library_pick(json.loads(body))
            status, payload = h.responses[0]
            self.assertEqual(status, 200)
            self.assertEqual(len(payload["picks"]), 1)
            self.assertEqual(payload["picks"][0]["game_id"], "2")
        finally:
            picker_module.load_state = original_load

    def test_picker_playlist_scope(self):
        state = sample_state()
        state["playlists"] = [{"name": "My List", "members": ["2"]}]
        original_load = picker_module.load_state
        try:
            picker_module.load_state = lambda: state
            body = json.dumps({"scope": "playlist", "scope_name": "My List"}).encode()
            h = self.handler(body)
            h._api_post_api_v2_library_pick(json.loads(body))
            status, payload = h.responses[0]
            self.assertEqual(status, 200)
            self.assertEqual(payload["picks"][0]["game_id"], "2")
        finally:
            picker_module.load_state = original_load

    def test_picker_invalid_players(self):
        original_load = picker_module.load_state
        try:
            picker_module.load_state = sample_state
            body = json.dumps({"players": 99}).encode()
            h = self.handler(body)
            with self.assertRaises(BadRequest):
                h._api_post_api_v2_library_pick(json.loads(body))
        finally:
            picker_module.load_state = original_load

    def test_picker_invalid_scope(self):
        original_load = picker_module.load_state
        try:
            picker_module.load_state = sample_state
            body = json.dumps({"scope": "everywhere"}).encode()
            h = self.handler(body)
            with self.assertRaises(BadRequest):
                h._api_post_api_v2_library_pick(json.loads(body))
        finally:
            picker_module.load_state = original_load

    def test_picker_minutes_session(self):
        state = sample_state()
        state["history"] = [
            {"game_id": 2, "seconds": 1200, "started": "2026-01-01T00:00:00Z"},
        ]
        original_load = picker_module.load_state
        try:
            picker_module.load_state = lambda: state
            body = json.dumps({"minutes": 45, "mood": "party", "players": 2}).encode()
            h = self.handler(body)
            h._api_post_api_v2_library_pick(json.loads(body))
            status, payload = h.responses[0]
            self.assertEqual(status, 200)
            self.assertEqual(len(payload["picks"]), 1)
            self.assertEqual(payload["picks"][0]["game_id"], "2")
        finally:
            picker_module.load_state = original_load


if __name__ == "__main__":
    unittest.main(verbosity=2)
