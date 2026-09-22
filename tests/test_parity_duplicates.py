#!/usr/bin/env python3
"""P5: duplicate detection and merge tests.

Detection groups identity/path/title collisions; merge keeps the record with
history and sends the duplicates to the trash bin so the operation is
reversible through the existing undo/restore path.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pkg.parity  # noqa: F401,E402  # register flat-import finder

from pkg.parity.parity_duplicates import (  # noqa: E402
    KIND_IDENTITY,
    KIND_PATH,
    KIND_TITLE,
    apply_merge,
    find_duplicates,
    merge_plan,
    normalize_path,
    normalize_title,
)


def _handler():
    from handlers.library import LibraryHandlers

    class MockHandler(LibraryHandlers):
        def __init__(self):
            self._sent = None
            self.headers = {}

        def send_json(self, code, body):
            self._sent = (code, body)

    return MockHandler()


class DuplicatesPureTests(unittest.TestCase):
    def test_normalize_title_drops_region_and_edition_markers(self):
        self.assertEqual(normalize_title("Chrono Trigger (USA)"), normalize_title("chrono trigger"))
        self.assertEqual(normalize_title("Sonic [Rev A]"), normalize_title("SONIC"))
        self.assertEqual(normalize_title("Game - Disc 1"), normalize_title("Game"))
        self.assertNotEqual(normalize_title("Quake"), normalize_title("Quake II"))

    def test_normalize_path_is_case_and_dot_insensitive(self):
        self.assertEqual(normalize_path("/Games/Roms/../Roms/A.rom"), normalize_path("/games/roms/a.rom"))

    def _state(self):
        return {
            "history": [
                {"game_id": "g2", "game": "Quake", "seconds": 60},
                {"game_id": "g2", "game": "Quake", "seconds": 30},
            ],
            "playlists": [],
            "games": [
                {"game_id": "g1", "name": "Quake (USA)", "platform": "PC", "path": "/roms/quake", "playtime_seconds": 10},
                {"game_id": "g2", "name": "Quake", "platform": "PC", "path": "/roms/quake-local", "playtime_seconds": 500, "cover": "/media/q.png", "tags": ["fps"]},
                {"game_id": "g3", "name": "Quake", "platform": "PC", "path": "/roms/other", "tags": ["retro"]},
                {"game_id": "g4", "name": "Doom", "platform": "PC", "path": "/roms/doom"},
            ],
        }

    def test_find_duplicates_groups_title_collisions(self):
        payload = find_duplicates(self._state())
        self.assertGreaterEqual(payload["count"], 1)
        titles = [group for group in payload["groups"] if group["kind"] == KIND_TITLE]
        self.assertEqual(len(titles), 1)
        self.assertEqual(len(titles[0]["games"]), 3)

    def test_path_and_identity_groups_take_priority(self):
        state = {
            "history": [],
            "games": [
                {"game_id": "same", "name": "One", "platform": "PC", "path": "/roms/a"},
                {"game_id": "same", "name": "Two", "platform": "PC", "path": "/roms/a"},
            ],
        }
        payload = find_duplicates(state)
        self.assertEqual([group["kind"] for group in payload["groups"]], [KIND_IDENTITY])
        path_state = {
            "history": [],
            "games": [
                {"game_id": "g1", "name": "One", "platform": "PC", "path": "/roms/a.ROM",
                 "source_identities": ["test:g1"]},
                {"game_id": "g2", "name": "Two", "platform": "PC", "path": "/roms/A.rom",
                 "source_identities": ["test:g2"]},
            ],
        }
        payload = find_duplicates(path_state)
        self.assertEqual([group["kind"] for group in payload["groups"]], [KIND_PATH])
        # Each game appears in exactly one group.
        seen = [row["id"] for group in payload["groups"] for row in group["games"]]
        self.assertEqual(len(seen), len(set(seen)))

    def test_identity_group_maps_repeated_game_id_to_distinct_indexes(self):
        # Both rows share the same game_id value, so detect_duplicate_identities
        # reports ["same", "same"]; the group must map each occurrence to a
        # distinct row index instead of repeating index 0.
        state = {
            "history": [],
            "games": [
                {"game_id": "same", "name": "One", "platform": "PC", "path": "/roms/a", "source_identity": "steam:123"},
                {"game_id": "same", "name": "Two", "platform": "PC", "path": "/roms/b", "source_identity": "steam:123"},
            ],
        }
        payload = find_duplicates(state)
        identity = [group for group in payload["groups"] if group["kind"] == KIND_IDENTITY]
        self.assertEqual(len(identity), 1)
        rows = identity[0]["games"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(row["id"] for row in rows), [0, 1])

    def test_merge_plan_keeps_history_record_and_unions_fields(self):
        plan = merge_plan(self._state(), [0, 1, 2])
        self.assertEqual(plan["primary"]["game_id"], "g2")
        self.assertEqual(plan["primary"]["sessions"], 2)
        self.assertEqual(plan["changes"]["tags"]["value"], ["fps", "retro"])
        self.assertNotIn("cover", plan["changes"])  # primary already has one

    def test_merge_plan_requires_two_records(self):
        with self.assertRaises(ValueError):
            merge_plan(self._state(), [0])
        with self.assertRaises(ValueError):
            merge_plan(self._state(), [99])

    def test_apply_merge_uses_trash_callback(self):
        state = self._state()
        trashed = []

        def trash_game(current, game):
            entry = {"trash_id": f"trash-{len(trashed)}", "name": game.get("name"), "game": dict(game)}
            trashed.append(entry)
            return entry

        result = apply_merge(state, [0, 1, 2], trash_game=trash_game)
        self.assertEqual(result["merged"], 2)
        self.assertEqual(len(state["games"]), 2)
        self.assertEqual(state["games"][0]["game_id"], "g2")
        self.assertEqual(len(state["trash"]), 2)
        self.assertEqual(sorted(entry["name"] for entry in trashed), ["Quake", "Quake (USA)"])


class DuplicatesRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        os.environ["OPENBOX_DATA_DIR"] = cls._tmp.name

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()
        os.environ.pop("OPENBOX_DATA_DIR", None)

    def _save(self):
        import openbox
        state = {
            "schema_version": 6,
            "games": [
                {"game_id": "g1", "name": "Quake (USA)", "platform": "PC", "path": "/roms/quake", "playtime_seconds": 10},
                {"game_id": "g2", "name": "Quake", "platform": "PC", "path": "/roms/quake-2", "playtime_seconds": 500},
            ],
            "playlists": [],
            "settings": {},
            "profiles": {},
            "history": [{"game_id": "g2", "game": "Quake", "seconds": 60}],
        }
        openbox.STATE_STORE.save(state)

    def test_routes_registered(self):
        from routes.registry import all_routes
        paths = {(entry.method, entry.path) for entry in all_routes()}
        self.assertIn(("GET", "/api/v2/library/duplicates"), paths)
        self.assertIn(("POST", "/api/v2/library/duplicates/preview"), paths)
        self.assertIn(("POST", "/api/v2/library/duplicates/merge"), paths)

    def test_merge_route_moves_duplicate_to_trash(self):
        self._save()
        handler = _handler()
        handler._api_post_api_v2_library_duplicates_preview({"ids": [0, 1]})
        status, plan = handler._sent
        self.assertEqual(status, 200)
        self.assertEqual(plan["primary"]["game_id"], "g2")

        handler = _handler()
        handler._api_post_api_v2_library_duplicates_merge({"ids": [0, 1]})
        status, result = handler._sent
        self.assertEqual(status, 200)
        self.assertEqual(result["merged"], 1)

        from webapp_state import load_state_view
        state = load_state_view()
        self.assertEqual([game["game_id"] for game in state["games"]], ["g2"])
        self.assertEqual(len(state["trash"]), 1)

    def test_merge_route_rejects_bad_payload(self):
        from api_errors import BadRequest
        self._save()
        with self.assertRaises(BadRequest):
            _handler()._api_post_api_v2_library_duplicates_merge({"ids": [0]})
        with self.assertRaises(BadRequest):
            _handler()._api_post_api_v2_library_duplicates_preview({"ids": "x"})
        with self.assertRaises(BadRequest):
            _handler()._api_post_api_v2_library_duplicates_preview({"ids": [98, 99]})


if __name__ == "__main__":
    unittest.main(verbosity=2)
