"""Tests for handlers/party.py."""
from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from urllib.parse import urlparse

from api_errors import BadRequest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import web_app  # noqa: E402
import handlers.party as party_module  # noqa: E402


def sample_state():
    return {
        "games": [
            {"id": 1, "game_id": "g-1", "name": "Mario Kart", "platform": "SNES",
             "path_exists": True, "max_players": 4, "rating": 5},
            {"id": 2, "game_id": "g-2", "name": "Street Fighter", "platform": "Arcade",
             "path_exists": True, "max_players": 2, "rating": 4},
            {"id": 3, "game_id": "g-3", "name": "Solo Quest", "platform": "SNES",
             "path_exists": True, "max_players": 1, "rating": 5},
        ],
        "settings": {},
    }


class PartyHandlerTest(unittest.TestCase):
    def setUp(self):
        self.store = {"state": sample_state()}
        self._orig_load = party_module.load_state
        self._orig_transact = party_module.transact_state
        store = self.store
        party_module.load_state = lambda: store["state"]

        def fake_transact(mutator):
            mutator(store["state"])

        party_module.transact_state = fake_transact
        self.addCleanup(self._restore)

    def _restore(self):
        party_module.load_state = self._orig_load
        party_module.transact_state = self._orig_transact

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

    def build(self, payload):
        h = self.handler()
        h._api_post_api_v2_party_queue(payload)
        return h.responses[0]

    def test_build_queue_happy(self):
        status, payload = self.build({"players": 2, "minutes": 0})
        self.assertEqual(status, 200)
        # g-3 is single-player only; rating 5 sorts first.
        self.assertEqual(payload["queue"], ["g-1", "g-2"])
        self.assertEqual(payload["count"], 2)
        # Persisted through settings.
        self.assertEqual(self.store["state"]["settings"]["party_queue"], ["g-1", "g-2"])
        self.assertEqual(self.store["state"]["settings"]["party_players"], 2)
        self.assertEqual(self.store["state"]["settings"]["party_index"], 0)

    def test_build_bad_players(self):
        for bad in (1, 9, 0, "four", None):
            with self.assertRaises(BadRequest, msg=f"players={bad!r}"):
                self.build({"players": bad})

    def test_build_bad_minutes(self):
        with self.assertRaises(BadRequest):
            self.build({"players": 2, "minutes": -5})
        with self.assertRaises(BadRequest):
            self.build({"players": 2, "minutes": "long"})

    def test_build_non_object_body(self):
        h = self.handler()
        with self.assertRaises(BadRequest):
            h._api_post_api_v2_party_queue([1, 2])

    def test_get_queue_round_trip(self):
        self.build({"players": 2})
        h = self.handler()
        h._api_get_api_v2_party_queue(urlparse("/api/v2/party/queue"))
        status, payload = h.responses[0]
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"queue": ["g-1", "g-2"], "index": 0})

    def test_get_queue_empty(self):
        h = self.handler()
        h._api_get_api_v2_party_queue(urlparse("/api/v2/party/queue"))
        status, payload = h.responses[0]
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"queue": [], "index": 0})

    def test_next_advances_and_wraps(self):
        self.build({"players": 2})
        h = self.handler()
        h._api_post_api_v2_party_next({})
        self.assertEqual(h.responses[0][0], 200)
        self.assertEqual(
            h.responses[0][1], {"game_id": "g-2", "name": "Street Fighter", "index": 1}
        )
        h2 = self.handler()
        h2._api_post_api_v2_party_next({})
        # Wraps back to the head of the queue.
        self.assertEqual(
            h2.responses[0][1], {"game_id": "g-1", "name": "Mario Kart", "index": 0}
        )

    def test_next_empty_queue_400(self):
        h = self.handler()
        with self.assertRaises(BadRequest):
            h._api_post_api_v2_party_next({})

    def test_next_unknown_game_id_uses_id_as_name(self):
        self.store["state"]["settings"] = {
            "party_queue": ["g-zzz"],
            "party_players": 2,
            "party_index": 0,
        }
        # Two entries needed for wrap math; single entry wraps to itself.
        h = self.handler()
        h._api_post_api_v2_party_next({})
        self.assertEqual(h.responses[0][1], {"game_id": "g-zzz", "name": "g-zzz", "index": 0})


if __name__ == "__main__":
    unittest.main(verbosity=2)
