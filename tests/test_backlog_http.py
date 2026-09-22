"""F4 backlog v2 routes: progress/rating/playtime/notes CRUD via HTTP boundary."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pkg.parity  # noqa: F401,E402  # register flat-import finder
import openbox  # noqa: E402
from api_errors import BadRequest  # noqa: E402
from handlers.library import LibraryHandlers  # noqa: E402


class MockHandler(LibraryHandlers):
    def __init__(self):
        self._sent = None
        self.headers = {}

    def send_json(self, code, body):
        self._sent = (code, body)


class BacklogRoutesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["OPENBOX_DATA_DIR"] = self._tmp.name
        openbox.STATE_STORE.save({
            "schema_version": 6,
            "games": [{"game_id": "game-1", "name": "Game", "progress": "",
                       "rating": 4.5, "notes": "legacy note"}],
            "settings": {}, "profiles": {},
        })
        self.handler = MockHandler()

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop("OPENBOX_DATA_DIR", None)

    def _call(self, name, payload):
        self.handler._sent = None
        getattr(self.handler, name)(payload)
        return self.handler._sent

    def _game(self):
        return openbox.STATE_STORE.load()["games"][0]

    # F4a: progress setter canonicalizes "Unplayed" to "".
    def test_progress_set_unplayed_alias(self):
        code, body = self._call("_api_post_api_v2_library_progress_set",
                               {"game_id": "game-1", "progress": "Unplayed"})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        game = self._game()
        self.assertEqual(game["progress"], "")
        self.assertTrue(game["progress_suggested"])

    def test_progress_set_rejects_unknown(self):
        with self.assertRaises(BadRequest):
            self._call("_api_post_api_v2_library_progress_set",
                       {"game_id": "game-1", "progress": "Almost"})

    # F4b: personal rating is distinct from metadata rating.
    def test_rating_set_writes_user_rating(self):
        code, body = self._call("_api_post_api_v2_library_rating_set",
                                {"game_id": "game-1", "user_rating": 4})
        self.assertEqual(code, 200)
        self.assertEqual(body["user_rating"], 4)
        game = self._game()
        self.assertEqual(game["user_rating"], 4)
        self.assertEqual(game["rating"], 4.5)  # metadata untouched

    def test_rating_set_rejects_out_of_range(self):
        with self.assertRaises(BadRequest):
            self._call("_api_post_api_v2_library_rating_set",
                       {"game_id": "game-1", "user_rating": 6})

    # F4c: manual playtime log/update/delete with recomputed totals.
    def test_playtime_log_update_delete(self):
        code, body = self._call("_api_post_api_v2_library_playtime_log",
                                {"game_id": "game-1", "date": "2026-01-02",
                                 "seconds": 3600, "note": "evening"})
        self.assertEqual(code, 200)
        game = self._game()
        self.assertEqual(game["manual_playtime_seconds"], 3600)
        self.assertEqual(game["manual_sessions"],
                         [{"date": "2026-01-02", "seconds": 3600, "note": "evening"}])

        self._call("_api_post_api_v2_library_playtime_update",
                   {"game_id": "game-1", "index": 0, "date": "2026-01-03",
                    "seconds": 60, "note": ""})
        game = self._game()
        self.assertEqual(game["manual_playtime_seconds"], 60)

        self._call("_api_post_api_v2_library_playtime_delete",
                   {"game_id": "game-1", "index": 0})
        game = self._game()
        self.assertEqual(game["manual_sessions"], [])
        self.assertEqual(game["manual_playtime_seconds"], 0)

    def test_playtime_rejects_bad_seconds_and_date(self):
        with self.assertRaises(BadRequest):
            self._call("_api_post_api_v2_library_playtime_log",
                       {"game_id": "game-1", "seconds": 0})
        with self.assertRaises(BadRequest):
            self._call("_api_post_api_v2_library_playtime_log",
                       {"game_id": "game-1", "seconds": 60, "date": "02/01/2026"})

    # F4d: legacy string notes migrate on first mutation; dated CRUD.
    def test_notes_legacy_migration_and_crud(self):
        code, body = self._call("_api_post_api_v2_library_notes_add",
                                {"game_id": "game-1", "text": "new entry"})
        self.assertEqual(code, 200)
        game = self._game()
        self.assertEqual(game["notes"][0], {"ts": "", "text": "legacy note"})
        self.assertEqual(game["notes"][1]["text"], "new entry")
        self.assertTrue(game["notes"][1]["ts"])

        self._call("_api_post_api_v2_library_notes_update",
                   {"game_id": "game-1", "index": 1, "text": "edited"})
        game = self._game()
        self.assertEqual(game["notes"][1]["text"], "edited")
        # legacy entry keeps its empty timestamp
        self.assertEqual(game["notes"][0], {"ts": "", "text": "legacy note"})

        self._call("_api_post_api_v2_library_notes_delete",
                   {"game_id": "game-1", "index": 0})
        game = self._game()
        self.assertEqual(len(game["notes"]), 1)

    def test_notes_rejects_empty_and_oversize(self):
        with self.assertRaises(BadRequest):
            self._call("_api_post_api_v2_library_notes_add",
                       {"game_id": "game-1", "text": "   "})
        with self.assertRaises(BadRequest):
            self._call("_api_post_api_v2_library_notes_add",
                       {"game_id": "game-1", "text": "x" * 2001})

    def test_routes_registered_on_v2_surface(self):
        from routes import POST_TABLE
        for path, name in [
            ("/api/v2/library/playtime/log", "_api_post_api_v2_library_playtime_log"),
            ("/api/v2/library/playtime/update", "_api_post_api_v2_library_playtime_update"),
            ("/api/v2/library/playtime/delete", "_api_post_api_v2_library_playtime_delete"),
            ("/api/v2/library/notes/add", "_api_post_api_v2_library_notes_add"),
            ("/api/v2/library/notes/update", "_api_post_api_v2_library_notes_update"),
            ("/api/v2/library/notes/delete", "_api_post_api_v2_library_notes_delete"),
            ("/api/v2/library/progress/set", "_api_post_api_v2_library_progress_set"),
            ("/api/v2/library/rating/set", "_api_post_api_v2_library_rating_set"),
        ]:
            self.assertEqual(POST_TABLE[path], name, path)


if __name__ == "__main__":
    unittest.main()
