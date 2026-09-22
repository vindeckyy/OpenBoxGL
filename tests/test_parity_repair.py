#!/usr/bin/env python3
"""P5: missing-file repair wizard tests.

Covers the pure scan/plan/apply helpers and the v2 routes against a real
isolated state store: a missing game path plus a folder the user points at.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pkg.parity  # noqa: F401,E402  # register flat-import finder

from pkg.parity.parity_repair import (  # noqa: E402
    KIND_GAME,
    KIND_MEDIA,
    MAX_FOLDER_DEPTH,
    apply_repair,
    plan_repair,
    resolve_folder,
    scan_candidates,
    scan_missing_paths,
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


class RepairPureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _state(self):
        return {
            "games": [
                {
                    "game_id": "g1",
                    "name": "Quake",
                    "platform": "PC",
                    "path": str(self.root / "missing" / "quake.exe"),
                    "cover": str(self.root / "missing" / "cover.png"),
                },
                {
                    "game_id": "g2",
                    "name": "Chrono",
                    "platform": "SNES",
                    "path": str(self.root / "roms" / "chrono.sfc"),
                },
            ]
        }

    def test_scan_reports_game_and_media_rows(self):
        state = self._state()
        (self.root / "roms").mkdir()
        (self.root / "roms" / "chrono.sfc").write_bytes(b"rom")
        scan = scan_missing_paths(state)
        self.assertEqual(scan["count"], 2)
        self.assertEqual({item["kind"] for item in scan["items"]}, {KIND_GAME, KIND_MEDIA})
        self.assertFalse(scan["truncated"])

    def test_scan_truncates_at_the_limit(self):
        state = self._state()
        scan = scan_missing_paths(state, limit=1)
        self.assertEqual(scan["count"], 1)
        self.assertTrue(scan["truncated"])

    def test_scan_can_skip_media(self):
        state = self._state()
        (self.root / "roms").mkdir()
        (self.root / "roms" / "chrono.sfc").write_bytes(b"rom")
        scan = scan_missing_paths(state, include_media=False)
        self.assertEqual([item["field"] for item in scan["items"]], ["path"])

    def test_plan_matches_basename_and_flags_ambiguity(self):
        folder = self.root / "found"
        (folder / "a").mkdir(parents=True)
        (folder / "b").mkdir()
        (folder / "a" / "quake.exe").write_bytes(b"one")
        (folder / "b" / "quake.exe").write_bytes(b"two")
        (folder / "cover.png").write_bytes(b"png")
        items = scan_missing_paths(self._state())["items"]
        plan = plan_repair(items, scan_candidates(folder))
        self.assertEqual(plan["counts"]["matched"], 1)
        self.assertEqual(plan["counts"]["ambiguous"], 1)
        self.assertEqual(plan["matches"][0]["field"], "cover")
        self.assertEqual(len(plan["ambiguous"][0]["choices"]), 2)

    def test_plan_uses_stem_match_for_games(self):
        folder = self.root / "found"
        folder.mkdir()
        (folder / "quake.iso").write_bytes(b"iso")
        items = [item for item in scan_missing_paths(self._state())["items"] if item["field"] == "path"]
        plan = plan_repair(items, scan_candidates(folder))
        self.assertEqual(plan["counts"]["matched"], 1)
        self.assertEqual(Path(plan["matches"][0]["path"]).name, "quake.iso")

    def test_scan_candidates_is_bounded_and_skips_hidden(self):
        folder = self.root / "tree"
        folder.mkdir()
        (folder / ".hidden").write_bytes(b"x")
        for index in range(5):
            (folder / f"f{index}.bin").write_bytes(b"x")
        candidates = scan_candidates(folder, limit=3)
        self.assertEqual(len(candidates), 3)
        self.assertNotIn(".hidden", [item["name"] for item in candidates])
        self.assertGreaterEqual(MAX_FOLDER_DEPTH, 1)

    def test_resolve_folder_rejects_root_and_missing(self):
        with self.assertRaises(ValueError):
            resolve_folder("/")
        with self.assertRaises(ValueError):
            resolve_folder(str(self.root / "nope"))
        with self.assertRaises(ValueError):
            resolve_folder("relative/path")
        self.assertEqual(resolve_folder(str(self.root)), self.root.resolve())

    def test_apply_repair_skips_stale_rows(self):
        state = self._state()
        target = self.root / "quake.exe"
        target.write_bytes(b"exe")
        matches = [{
            "id": 0, "game_id": "g1", "field": "path", "from": "different-path",
            "path": str(target),
        }]
        result = apply_repair(state, matches)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["skipped"][0]["reason"], "changed_since_preview")
        # Missing target and unknown field/index are skipped, never guessed.
        result = apply_repair(state, [{"id": 0, "game_id": "g1", "field": "path", "from": state["games"][0]["path"], "path": str(self.root / "nope")}])
        self.assertEqual(result["updated"], 0)
        result = apply_repair(state, [{"id": 99, "field": "path", "path": str(target)}])
        self.assertEqual(result["skipped"][0]["reason"], "missing_game")
        result = apply_repair(state, [{"id": 0, "field": "launch", "path": str(target)}])
        self.assertEqual(result["skipped"][0]["reason"], "invalid_field")

    def test_apply_repair_honors_selection(self):
        state = self._state()
        target = self.root / "quake.exe"
        target.write_bytes(b"exe")
        matches = [
            {"id": 0, "game_id": "g1", "field": "path", "from": state["games"][0]["path"], "path": str(target)},
            {"id": 0, "game_id": "g1", "field": "cover", "from": state["games"][0]["cover"], "path": str(target)},
        ]
        result = apply_repair(state, matches, selection=[[0, "cover"]])
        self.assertEqual(result["updated"], 1)
        self.assertEqual(state["games"][0]["cover"], str(target))


class RepairRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        os.environ["OPENBOX_DATA_DIR"] = cls._tmp.name
        cls.folder = Path(cls._tmp.name) / "found"
        cls.folder.mkdir()
        (cls.folder / "relink.rom").write_bytes(b"rom")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()
        os.environ.pop("OPENBOX_DATA_DIR", None)

    def _save(self):
        import openbox
        state = {
            "schema_version": 6,
            "games": [{
                "game_id": "g1", "name": "Relink", "platform": "PC",
                "path": str(self.folder / "old" / "relink.rom"),
            }],
            "playlists": [],
            "settings": {},
            "profiles": {},
            "history": [],
        }
        openbox.STATE_STORE.save(state)

    def test_routes_registered_in_tables(self):
        from routes.registry import all_routes
        paths = {(entry.method, entry.path) for entry in all_routes()}
        self.assertIn(("GET", "/api/v2/library/repair"), paths)
        self.assertIn(("POST", "/api/v2/library/repair/preview"), paths)
        self.assertIn(("POST", "/api/v2/library/repair/apply"), paths)

    def test_scan_route_lists_missing_paths(self):
        self._save()
        handler = _handler()
        handler._api_get_api_v2_library_repair(SimpleNamespace(query="media=0"))
        status, payload = handler._sent
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["game_id"], "g1")

    def test_preview_and_apply_route(self):
        self._save()
        handler = _handler()
        handler._api_post_api_v2_library_repair_preview({"folder": str(self.folder), "include_media": False})
        status, plan = handler._sent
        self.assertEqual(status, 200)
        self.assertEqual(plan["counts"]["matched"], 1)
        self.assertEqual(plan["folder"], str(self.folder.resolve()))

        handler = _handler()
        handler._api_post_api_v2_library_repair_apply({"folder": str(self.folder), "include_media": False})
        status, result = handler._sent
        self.assertEqual(status, 200)
        self.assertEqual(result["updated"], 1)

        from webapp_state import load_state_view
        self.assertEqual(load_state_view()["games"][0]["path"], str(self.folder / "relink.rom"))

    def test_preview_rejects_bad_folder(self):
        from api_errors import BadRequest
        self._save()
        with self.assertRaises(BadRequest):
            _handler()._api_post_api_v2_library_repair_preview({"folder": "/"})
        with self.assertRaises(BadRequest):
            _handler()._api_post_api_v2_library_repair_preview({"folder": str(self.folder), "fields": "path"})
        with self.assertRaises(BadRequest):
            _handler()._api_post_api_v2_library_repair_apply({"folder": str(self.folder), "selection": "nope"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
