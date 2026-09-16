"""Tests for handlers/party.py."""
from __future__ import annotations

import io
import sys
import tempfile
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
        self.assertIsNone(payload["empty_reason"])
        # Persisted through settings.
        self.assertEqual(self.store["state"]["settings"]["party_queue"], ["g-1", "g-2"])
        self.assertEqual(self.store["state"]["settings"]["party_players"], 2)
        self.assertEqual(self.store["state"]["settings"]["party_index"], 0)

    def test_build_queue_empty_explains_why(self):
        self.store["state"]["games"] = [
            {"id": 9, "game_id": "g-9", "name": "Solo", "platform": "SNES",
             "path_exists": True, "max_players": 1, "rating": 5},
        ]
        status, payload = self.build({"players": 2, "minutes": 0})
        self.assertEqual(status, 200)
        self.assertEqual(payload["queue"], [])
        self.assertEqual(payload["count"], 0)
        self.assertIn("2 players", payload["empty_reason"])
        self.assertEqual(payload["excluded"]["too_few_players"], 1)

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


class PartyDeckHandlerTest(unittest.TestCase):
    def setUp(self):
        self.store = {"state": sample_state()}
        self._orig_load = party_module.load_state
        self._orig_transact = party_module.transact_state
        store = self.store
        party_module.load_state = lambda: store["state"]

        def fake_transact(mutator):
            return store["state"], mutator(store["state"])

        party_module.transact_state = fake_transact
        self.addCleanup(self._restore)

    def _restore(self):
        party_module.load_state = self._orig_load
        party_module.transact_state = self._orig_transact

    def handler(self):
        h = web_app.Handler.__new__(web_app.Handler)
        h.responses = []
        h.send_json = lambda status, payload: h.responses.append((status, payload))
        return h

    def _save_deck(self, payload):
        h = self.handler()
        h._api_post_api_v2_party_decks(payload)
        return h.responses[0]

    def test_deck_round_trip_save_list_load_delete(self):
        status, payload = self._save_deck({"name": "Friday Night", "players": 2, "minutes": 0})
        self.assertEqual(status, 200)
        deck = payload["deck"]
        self.assertEqual(deck["name"], "Friday Night")
        self.assertTrue(deck["seed"])
        self.assertEqual(set(deck["game_ids"]), {"g-1", "g-2"})
        self.assertEqual(len(self.store["state"]["party_decks"]["decks"]), 1)

        listing = self.handler()
        listing._api_get_api_v2_party_decks(object())
        self.assertEqual(listing.responses[0][1]["count"], 1)
        self.assertEqual(listing.responses[0][1]["presets"][0]["id"], "nineties_racers")

        load = self.handler()
        load._api_post_api_v2_party_decks_load({"deck_id": deck["deck_id"]})
        loaded = load.responses[0][1]
        self.assertEqual(loaded["count"], 2)
        self.assertEqual(set(loaded["queue"]), {"g-1", "g-2"})
        self.assertEqual(self.store["state"]["settings"]["party_players"], 2)
        self.assertEqual(self.store["state"]["settings"]["party_index"], 0)

        delete = self.handler()
        delete._api_post_api_v2_party_decks_delete({"deck_id": deck["deck_id"]})
        self.assertEqual(delete.responses[0][1], {"deleted": True, "count": 0})

    def test_load_unknown_deck_is_an_error(self):
        h = self.handler()
        with self.assertRaises(BadRequest) as raised:
            h._api_post_api_v2_party_decks_load({"deck_id": "nope"})
        self.assertEqual(raised.exception.code, "PARTY_DECK_NOT_FOUND")

    def test_build_route_validates_preset_and_players(self):
        with self.assertRaises(BadRequest) as raised:
            h = self.handler()
            h._api_post_api_v2_party_queue({"players": 2, "preset": "bogus"})
        self.assertEqual(raised.exception.code, "PARTY_INVALID_PRESET")

        with self.assertRaises(BadRequest):
            self._save_deck({"name": "Bad", "players": 9})
        with self.assertRaises(BadRequest):
            self._save_deck({"players": 2})

    def test_preset_build_reports_an_honest_empty_reason(self):
        self.store["state"]["games"] = [
            {"id": 1, "game_id": "g-1", "name": "Puzzle", "platform": "SNES",
             "path_exists": True, "max_players": 4, "rating": 5, "year": 2020, "genre": "Puzzle"},
        ]
        h = self.handler()
        h._api_post_api_v2_party_queue({"players": 2, "preset": "nineties_racers"})
        payload = h.responses[0][1]
        self.assertEqual(payload["queue"], [])
        self.assertEqual(payload["preset"], "nineties_racers")
        self.assertEqual(payload["empty_reason"], "party.preset_empty")

    def test_share_and_import_through_the_household_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            self.store["state"]["settings"]["cloud_folder"] = directory
            _, saved = self._save_deck({"name": "Friday Night", "players": 2})
            deck = saved["deck"]

            h = self.handler()
            h._api_post_api_v2_party_decks_share({"deck_id": deck["deck_id"]})
            self.assertTrue(h.responses[0][1]["shared"])
            self.assertEqual(h.responses[0][1]["signature"], deck["signature"])

            destination = {"games": sample_state()["games"], "settings": {"cloud_folder": directory}}
            self.store["state"] = destination
            imported = self.handler()
            imported._api_post_api_v2_party_decks_import({})
            self.assertEqual(imported.responses[0][1], {"imported": 1, "available": 1, "count": 1})

            repeated = self.handler()
            repeated._api_post_api_v2_party_decks_import({})
            self.assertEqual(repeated.responses[0][1]["imported"], 0)

    def test_share_requires_a_folder(self):
        _, saved = self._save_deck({"name": "Friday Night", "players": 2})
        h = self.handler()
        with self.assertRaises(BadRequest) as raised:
            h._api_post_api_v2_party_decks_share({"deck_id": saved["deck"]["deck_id"]})
        self.assertEqual(raised.exception.code, "PARTY_SHARE_FOLDER_REQUIRED")

    def test_routes_table_includes_decks(self):
        from routes import GET_TABLE, POST_TABLE

        self.assertEqual(GET_TABLE["/api/v2/party/decks"], "_api_get_api_v2_party_decks")
        self.assertEqual(POST_TABLE["/api/v2/party/decks"], "_api_post_api_v2_party_decks")
        self.assertEqual(POST_TABLE["/api/v2/party/decks/load"], "_api_post_api_v2_party_decks_load")
        self.assertEqual(POST_TABLE["/api/v2/party/decks/share"], "_api_post_api_v2_party_decks_share")
        self.assertEqual(POST_TABLE["/api/v2/party/decks/import"], "_api_post_api_v2_party_decks_import")


if __name__ == "__main__":
    unittest.main(verbosity=2)
