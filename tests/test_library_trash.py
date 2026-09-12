#!/usr/bin/env python3
"""Tests for the bounded trash bin + undo (S3, 1.11.0).

v2 delete moves the full record to a bounded ``state["trash"]`` list;
restore re-inserts with the same identity and playlist memberships;
v1 /api/game/delete stays a hard delete.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pkg.parity  # noqa: F401,E402  # register flat-import finder


def _handler():
    from handlers.library import LibraryHandlers

    class MockHandler(LibraryHandlers):
        def __init__(self):
            self._sent = None
            self.headers = {}

        def send_json(self, code, body):
            self._sent = (code, body)

    return MockHandler()


def _game(game_id, name, **extra):
    base = {"game_id": game_id, "name": name, "path": f"/games/{game_id}.rom",
            "platform": "SNES", "tags": ["keep"], "rating": 4}
    base.update(extra)
    return base


class TrashRouteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["OPENBOX_DATA_DIR"] = str(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop("OPENBOX_DATA_DIR", None)

    def _save(self, games=(), playlists=(), settings=None, **extra):
        import openbox
        state = {"schema_version": 6, "games": list(games), "playlists": list(playlists),
                 "settings": settings or {}, "profiles": {}}
        state.update(extra)
        openbox.STATE_STORE.save(state)

    def _trash(self):
        from webapp_state import load_state_view
        return load_state_view().get("trash", [])

    def test_routes_registered_in_tables(self):
        from routes import GET_TABLE, POST_TABLE
        self.assertEqual(GET_TABLE["/api/v2/library/trash"], "_api_get_api_v2_library_trash")
        self.assertEqual(POST_TABLE["/api/v2/library/trash"], "_api_post_api_v2_library_trash")
        self.assertEqual(POST_TABLE["/api/v2/library/trash/restore"], "_api_post_api_v2_library_trash_restore")
        self.assertEqual(POST_TABLE["/api/v2/library/trash/purge"], "_api_post_api_v2_library_trash_purge")

    def test_default_state_has_bounded_trash(self):
        from state_store import default_state
        state = default_state()
        self.assertIn("trash", state)
        self.assertEqual(state["trash"], [])

    def test_v2_delete_moves_record_to_trash(self):
        self._save(games=[_game("game-a1", "Chrono Trigger")])
        handler = _handler()
        handler._api_post_api_v2_library_trash({"game_id": "game-a1"})
        self.assertEqual(handler._sent[0], 200)
        self.assertTrue(handler._sent[1]["ok"])
        self.assertTrue(str(handler._sent[1]["trash_id"]).startswith("trash-"))
        from webapp_state import load_state_view
        state = load_state_view()
        self.assertEqual(state["games"], [])
        trash = state["trash"]
        self.assertEqual(len(trash), 1)
        entry = trash[0]
        self.assertEqual(entry["game_id"], "game-a1")
        self.assertEqual(entry["name"], "Chrono Trigger")
        self.assertEqual(entry["game"]["rating"], 4)
        self.assertTrue(entry["trashed_at"])

    def test_v2_delete_records_and_clears_playlist_membership(self):
        playlists = [{"name": "Co-op", "type": "manual", "rules": {},
                      "members": ["game-b1", "game-b2"], "parent": "", "notes": ""}]
        self._save(games=[_game("game-b1", "One"), _game("game-b2", "Two")],
                   playlists=playlists)
        handler = _handler()
        handler._api_post_api_v2_library_trash({"game_id": "game-b1"})
        self.assertEqual(handler._sent[0], 200)
        from webapp_state import load_state_view
        state = load_state_view()
        self.assertEqual(state["playlists"][0]["members"], ["game-b2"])
        self.assertEqual(state["trash"][0]["playlists"], ["Co-op"])

    def test_get_trash_lists_entries_newest_first(self):
        self._save(games=[_game("game-c1", "First"), _game("game-c2", "Second")])
        handler = _handler()
        handler._api_post_api_v2_library_trash({"game_id": "game-c1"})
        handler._api_post_api_v2_library_trash({"game_id": "game-c2"})
        handler._api_get_api_v2_library_trash(SimpleNamespace(query=""))
        self.assertEqual(handler._sent[0], 200)
        body = handler._sent[1]
        self.assertEqual(body["count"], 2)
        self.assertEqual([item["name"] for item in body["items"]], ["Second", "First"])
        self.assertIn("trashed_at", body["items"][0])
        self.assertIn("trash_id", body["items"][0])

    def test_restore_round_trips_identity_and_playlists(self):
        playlists = [{"name": "RPGs", "type": "manual", "rules": {},
                      "members": ["game-d1"], "parent": "", "notes": ""}]
        self._save(games=[_game("game-d1", "EarthBound", notes="keep me")],
                   playlists=playlists)
        handler = _handler()
        handler._api_post_api_v2_library_trash({"game_id": "game-d1"})
        trash_id = handler._sent[1]["trash_id"]
        handler._api_post_api_v2_library_trash_restore({"trash_id": trash_id})
        self.assertEqual(handler._sent[0], 200)
        self.assertTrue(handler._sent[1]["restored"])
        self.assertEqual(handler._sent[1]["game_id"], "game-d1")
        from webapp_state import load_state_view
        state = load_state_view()
        self.assertEqual(len(state["games"]), 1)
        game = state["games"][0]
        self.assertEqual(game["game_id"], "game-d1")
        self.assertEqual(game["name"], "EarthBound")
        self.assertEqual(game["notes"], "keep me")
        self.assertEqual(state["playlists"][0]["members"], ["game-d1"])
        self.assertEqual(state["trash"], [])

    def test_restore_after_id_collision_gets_new_id_and_keeps_data(self):
        self._save(games=[_game("game-e1", "Original")])
        handler = _handler()
        handler._api_post_api_v2_library_trash({"game_id": "game-e1"})
        trash_id = handler._sent[1]["trash_id"]
        # A different game now owns the original id.
        def mutate(state):
            state["games"].append(_game("game-e1", "Replacement"))
        from webapp_state import transact_state
        transact_state(mutate)
        handler._api_post_api_v2_library_trash_restore({"trash_id": trash_id})
        self.assertEqual(handler._sent[0], 200)
        body = handler._sent[1]
        self.assertTrue(body["restored"])
        self.assertNotEqual(body["game_id"], "game-e1")
        self.assertTrue(body.get("renamed"))
        from webapp_state import load_state_view
        games = load_state_view()["games"]
        self.assertEqual(len(games), 2)
        restored = next(g for g in games if g["name"] == "Original")
        self.assertEqual(restored["rating"], 4)
        self.assertNotEqual(restored["game_id"], "game-e1")

    def test_restore_unknown_trash_id_is_bad_request(self):
        self._save()
        handler = _handler()
        from api_errors import BadRequest
        with self.assertRaises(BadRequest):
            handler._api_post_api_v2_library_trash_restore({"trash_id": "trash-missing"})

    def test_purge_empties_trash_and_empty_purge_is_noop(self):
        self._save(games=[_game("game-f1", "A"), _game("game-f2", "B")])
        handler = _handler()
        handler._api_post_api_v2_library_trash({"game_id": "game-f1"})
        handler._api_post_api_v2_library_trash({"game_id": "game-f2"})
        handler._api_post_api_v2_library_trash_purge({})
        self.assertEqual(handler._sent[0], 200)
        self.assertEqual(handler._sent[1]["purged"], 2)
        self.assertEqual(self._trash(), [])
        # Purge on empty is a 200 no-op.
        handler._api_post_api_v2_library_trash_purge({})
        self.assertEqual(handler._sent[0], 200)
        self.assertEqual(handler._sent[1]["purged"], 0)

    def test_purge_selected_ids_only(self):
        self._save(games=[_game("game-g1", "A"), _game("game-g2", "B")])
        handler = _handler()
        handler._api_post_api_v2_library_trash({"game_id": "game-g1"})
        first = handler._sent[1]["trash_id"]
        handler._api_post_api_v2_library_trash({"game_id": "game-g2"})
        second = handler._sent[1]["trash_id"]
        handler._api_post_api_v2_library_trash_purge({"ids": [first]})
        self.assertEqual(handler._sent[1]["purged"], 1)
        self.assertEqual([e["trash_id"] for e in self._trash()], [second])

    def test_v1_delete_stays_hard_delete(self):
        self._save(games=[_game("game-h1", "Hard")])
        handler = _handler()
        handler._api_post_api_game_delete({"game_id": "game-h1"})
        self.assertEqual(handler._sent[0], 200)
        from webapp_state import load_state_view
        state = load_state_view()
        self.assertEqual(state["games"], [])
        self.assertEqual(state["trash"], [])

    def test_trash_cap_keeps_newest(self):
        from state_store import TRASH_CAP
        games = [_game(f"game-i{i}", f"Game {i}") for i in range(TRASH_CAP + 5)]
        self._save(games=games)
        handler = _handler()
        for i in range(TRASH_CAP + 5):
            handler._api_post_api_v2_library_trash({"game_id": f"game-i{i}"})
        trash = self._trash()
        self.assertEqual(len(trash), TRASH_CAP)
        self.assertEqual(trash[0]["game_id"], "game-i5")
        self.assertEqual(trash[-1]["game_id"], f"game-i{TRASH_CAP + 4}")

    def test_expired_entries_pruned_on_delete(self):
        old = (datetime.now() - timedelta(days=45)).isoformat(timespec="seconds")
        stale = {"trash_id": "trash-old", "trashed_at": old, "game_id": "game-j0",
                 "name": "Stale", "game": {"game_id": "game-j0", "name": "Stale"}, "playlists": []}
        self._save(games=[_game("game-j1", "Fresh")], trash=[stale])
        handler = _handler()
        handler._api_post_api_v2_library_trash({"game_id": "game-j1"})
        trash = self._trash()
        self.assertEqual([e["game_id"] for e in trash], ["game-j1"])

    def test_normalize_repairs_malformed_trash(self):
        from state_store import normalize_state
        state, _changed = normalize_state({"schema_version": 6, "games": [], "trash": "oops"})
        self.assertEqual(state["trash"], [])
        state, _ = normalize_state({"schema_version": 6, "games": [],
                                    "trash": [{"game_id": "x", "trashed_at": "bad"}, "junk", 42]})
        self.assertEqual(len(state["trash"]), 1)
        self.assertEqual(state["trash"][0]["game_id"], "x")

    def test_normalize_keeps_entry_without_trashed_at(self):
        from state_store import normalize_state
        entry = {"trash_id": "trash-no-ts", "game_id": "game-z1", "trashed_at": 7}
        state, _ = normalize_state({"schema_version": 6, "games": [], "trash": [entry]})
        self.assertEqual(state["trash"], [entry])

    def test_journal_event_emitted_when_sync_on(self):
        self._save(games=[_game("game-k1", "Logged")],
                   settings={"library_sync_enabled": True})
        handler = _handler()
        handler._api_post_api_v2_library_trash({"game_id": "game-k1"})
        from webapp_state import load_state_view
        events = load_state_view().get("library_sync", {}).get("events", {})
        tombstones = [e for e in events.values() if e.get("tombstone")]
        self.assertTrue(tombstones, "expected a tombstone journal event for the trashed game")
        # Restore emits a catalog (non-tombstone) event for the same game.
        handler._api_post_api_v2_library_trash_restore({"trash_id": handler._sent[1]["trash_id"]})
        events = load_state_view().get("library_sync", {}).get("events", {})
        self.assertTrue(any(not e.get("tombstone") for e in events.values()))

    def test_delete_missing_game_raises(self):
        self._save(games=[_game("game-l1", "Solo")])
        handler = _handler()
        with self.assertRaises((IndexError, ValueError)):
            handler._api_post_api_v2_library_trash({"game_id": "game-nope"})

    def test_get_trash_projects_minimal_entries(self):
        entry = {"trash_id": "trash-min", "game_id": "game-m0", "trashed_at": "bad-ts"}
        self._save(trash=[entry])
        handler = _handler()
        handler._api_get_api_v2_library_trash(SimpleNamespace(query=""))
        self.assertEqual(handler._sent[0], 200)
        item = handler._sent[1]["items"][0]
        self.assertEqual(item["trash_id"], "trash-min")
        self.assertEqual(item["game_id"], "game-m0")
        self.assertEqual(item["playlists"], [])

    def test_restore_without_trash_id_is_bad_request(self):
        self._save()
        handler = _handler()
        from api_errors import BadRequest
        with self.assertRaises(BadRequest):
            handler._api_post_api_v2_library_trash_restore({})

    def test_restore_recovers_entry_missing_game_id(self):
        entry = {"trash_id": "trash-bare", "trashed_at": datetime.now().isoformat(timespec="seconds"),
                 "index": 99, "game": {"name": "Bare", "platform": "PC"}, "playlists": ["Ghost"]}
        playlists = [{"name": "Ghost", "type": "manual", "rules": {}, "members": [], "parent": "", "notes": ""}]
        self._save(trash=[entry], playlists=playlists)
        handler = _handler()
        handler._api_post_api_v2_library_trash_restore({"trash_id": "trash-bare"})
        self.assertEqual(handler._sent[0], 200)
        body = handler._sent[1]
        self.assertTrue(body["renamed"])
        self.assertTrue(body["game_id"])
        from webapp_state import load_state_view
        state = load_state_view()
        self.assertEqual(state["games"][-1]["name"], "Bare")
        self.assertIn(state["games"][-1]["game_id"], state["playlists"][0]["members"])

    def test_purge_rejects_non_list_ids(self):
        self._save()
        handler = _handler()
        from api_errors import BadRequest
        with self.assertRaises(BadRequest):
            handler._api_post_api_v2_library_trash_purge({"ids": "trash-x"})

    def test_v2_delete_with_media_removes_files(self):
        media_dir = Path(self._tmp.name) / "media"
        media_dir.mkdir(exist_ok=True)
        cover = media_dir / "cover.png"
        cover.write_bytes(b"png")
        os.environ["OPENBOX_MEDIA_ROOTS"] = str(media_dir)
        try:
            self._save(games=[_game("game-n1", "WithCover", cover=str(cover))])
            handler = _handler()
            handler._api_post_api_v2_library_trash({"game_id": "game-n1", "delete_media": True})
        finally:
            os.environ.pop("OPENBOX_MEDIA_ROOTS", None)
        self.assertEqual(handler._sent[0], 200)
        self.assertFalse(cover.exists())
        self.assertIn(str(cover), handler._sent[1]["deleted_media"])
        trash = self._trash()
        self.assertEqual(trash[0]["game"]["cover"], str(cover))

    def test_v2_delete_skips_non_manual_and_broken_playlists(self):
        playlists = [
            {"name": "Smart", "type": "smart", "rules": {"platform": "SNES"}, "members": []},
            {"name": "Broken", "type": "manual", "rules": {}, "members": "oops"},
            {"name": "Manual", "type": "manual", "rules": {}, "members": ["game-o1", "game-o2"]},
        ]
        self._save(games=[_game("game-o1", "One"), _game("game-o2", "Two")], playlists=playlists)
        handler = _handler()
        handler._api_post_api_v2_library_trash({"game_id": "game-o1"})
        self.assertEqual(handler._sent[0], 200)
        from webapp_state import load_state_view
        state = load_state_view()
        self.assertEqual(state["playlists"][0]["members"], [])
        self.assertEqual(state["playlists"][1]["members"], "oops")
        self.assertEqual(state["playlists"][2]["members"], ["game-o2"])
        self.assertEqual(state["trash"][0]["playlists"], ["Manual"])

    def test_v2_delete_media_keeps_shared_media(self):
        media_dir = Path(self._tmp.name) / "media"
        media_dir.mkdir(exist_ok=True)
        cover = media_dir / "shared.png"
        cover.write_bytes(b"png")
        os.environ["OPENBOX_MEDIA_ROOTS"] = str(media_dir)
        try:
            self._save(games=[_game("game-p1", "One", cover=str(cover)),
                              _game("game-p2", "Two", cover=str(cover))])
            handler = _handler()
            handler._api_post_api_v2_library_trash({"game_id": "game-p1", "delete_media": True})
        finally:
            os.environ.pop("OPENBOX_MEDIA_ROOTS", None)
        self.assertEqual(handler._sent[0], 200)
        self.assertTrue(cover.exists())
        self.assertIn(str(cover), handler._sent[1]["shared_media"])
        self.assertNotIn(str(cover), handler._sent[1]["deleted_media"])

    def test_v2_delete_media_tolerates_broken_remaining_path(self):
        # A remaining game's malformed media path (embedded NUL) must not
        # break the shared-media scan during a delete_media trash.
        media_dir = Path(self._tmp.name) / "media"
        media_dir.mkdir(exist_ok=True)
        cover = media_dir / "kept.png"
        cover.write_bytes(b"png")
        os.environ["OPENBOX_MEDIA_ROOTS"] = str(media_dir)
        try:
            self._save(games=[_game("game-s1", "Gone", cover=str(cover)),
                              _game("game-s2", "Stays", cover="bad\x00path.png")])
            handler = _handler()
            handler._api_post_api_v2_library_trash({"game_id": "game-s1", "delete_media": True})
        finally:
            os.environ.pop("OPENBOX_MEDIA_ROOTS", None)
        self.assertEqual(handler._sent[0], 200)
        self.assertFalse(cover.exists())
        from webapp_state import load_state_view
        self.assertEqual([g["game_id"] for g in load_state_view()["games"]], ["game-s2"])

    def test_v2_delete_media_ignores_unapproved_path(self):
        media_dir = Path(self._tmp.name) / "media"
        media_dir.mkdir(exist_ok=True)
        outsider = Path(self._tmp.name) / "outside.png"
        outsider.write_bytes(b"png")
        os.environ["OPENBOX_MEDIA_ROOTS"] = str(media_dir)
        try:
            self._save(games=[_game("game-q1", "Outsider", cover=str(outsider))])
            handler = _handler()
            handler._api_post_api_v2_library_trash({"game_id": "game-q1", "delete_media": True})
        finally:
            os.environ.pop("OPENBOX_MEDIA_ROOTS", None)
        self.assertEqual(handler._sent[0], 200)
        self.assertTrue(outsider.exists())
        self.assertEqual(handler._sent[1]["deleted_media"], [])
        self.assertEqual(self._trash()[0]["game"]["cover"], str(outsider))

    def test_restore_skips_missing_playlist_and_repairs_members(self):
        entry = {"trash_id": "trash-pl", "trashed_at": datetime.now().isoformat(timespec="seconds"),
                 "index": 0, "game_id": "game-r1",
                 "game": {"game_id": "game-r1", "name": "Back"}, "playlists": ["Missing", "Present"]}
        playlists = [{"name": "Present", "type": "manual", "rules": {}, "members": "oops"}]
        self._save(trash=[entry], playlists=playlists)
        handler = _handler()
        handler._api_post_api_v2_library_trash_restore({"trash_id": "trash-pl"})
        self.assertEqual(handler._sent[0], 200)
        from webapp_state import load_state_view
        state = load_state_view()
        self.assertEqual(state["playlists"][0]["members"], ["game-r1"])
        self.assertEqual([p["name"] for p in state["playlists"]], ["Present"])


if __name__ == "__main__":
    unittest.main()
