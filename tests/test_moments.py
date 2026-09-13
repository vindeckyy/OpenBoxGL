"""T1 moments persistence, capability and frontend contracts."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import openbox  # noqa: E402
import webapp_state  # noqa: E402  # installs the parity finder and dep registry
from handlers import moments  # noqa: E402
from pkg.state import _deps  # noqa: E402
from state_store import JsonStateStore  # noqa: E402


class _Handler:
    def __init__(self):
        self.responses = []

    def authorized(self):
        return True

    def handle_unauthorized(self):
        self.responses.append((403, {"error": "unauthorized"}))

    def send_json(self, status, payload):
        self.responses.append((status, payload))


class MomentsTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.data_path = self.root / "library.json"
        self.store = JsonStateStore(self.data_path)
        self.previous_openbox = (openbox.DATA, openbox.STATE_STORE)
        self.previous_webapp = (webapp_state.DATA, webapp_state.STATE_STORE)
        self.previous_deps = {name: _deps.get(name) for name in ("DATA", "STATE_STORE")}
        openbox.DATA = self.data_path
        openbox.STATE_STORE = self.store
        webapp_state.DATA = self.data_path
        webapp_state.STATE_STORE = self.store
        _deps.register("DATA", self.data_path)
        _deps.register("STATE_STORE", self.store)
        rom = self.root / "game.sfc"
        rom.write_bytes(b"rom")
        openbox.save_state({
            "games": [{
                "game_id": "fixture-game",
                "name": "Fixture Game",
                "platform": "SNES",
                "path": str(rom),
                "emulator_adapter_id": "retroarch-snes",
                "screenshots": [],
            }],
            "profiles": {},
            "history": [],
            "settings": {"quick_resume_enabled": True, "moments_autocapture": True},
            "playlists": [],
            "active_sessions": [],
        })
        self.game_id = openbox.load_state()["games"][0]["game_id"]

    def tearDown(self):
        openbox.DATA, openbox.STATE_STORE = self.previous_openbox
        webapp_state.DATA, webapp_state.STATE_STORE = self.previous_webapp
        for name, value in self.previous_deps.items():
            if value is None:
                _deps._REGISTRY.pop(name, None)
            else:
                _deps.register(name, value)
        self.tempdir.cleanup()

    def _create(self, **payload):
        handler = _Handler()
        body = {"game_id": self.game_id, "capture": False, **payload}
        moments.moments_create(handler, body)
        self.assertEqual(handler.responses[-1][0], 201, handler.responses)
        return handler.responses[-1][1]["item"]

    def test_create_captures_screenshot_and_persists_timeline_item(self):
        def fake_capture(path, window_hint=""):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(b"png")
            return str(path)

        with patch.object(moments, "capture_screenshot", side_effect=fake_capture):
            handler = _Handler()
            moments.moments_create(handler, {
                "game_id": self.game_id,
                "note": "A good run",
                "trigger": "pause",
            })
        status, payload = handler.responses[-1]
        self.assertEqual(status, 201)
        item = payload["item"]
        self.assertEqual(item["note"], "A good run")
        self.assertEqual(item["trigger"], "pause")
        self.assertTrue(item["screenshot"].endswith(".png"))
        self.assertIn("/media/moments/", item["screenshot"])
        self.assertEqual(item["screenshot_index"], 0)
        saved = openbox.load_state()["games"][0]
        self.assertEqual(len(saved["moments"]), 1)
        self.assertIn(item["screenshot"], saved["screenshots"])

    def test_capture_failure_keeps_note_only_moment(self):
        with patch.object(moments, "capture_screenshot", side_effect=FileNotFoundError("no tool")):
            item = self._create(note="Keep this even without pixels", trigger="hotkey", capture=True)
        self.assertEqual(item["screenshot"], "")
        self.assertEqual(item["note"], "Keep this even without pixels")
        self.assertEqual(item["capture_error"], "SCREENSHOT_UNAVAILABLE")

    def test_list_update_and_delete_are_stable_by_moment_id(self):
        first = self._create(note="older", created_at="2026-09-12T00:00:00+00:00")
        second = self._create(note="newer", created_at="2026-09-12T01:00:00+00:00")
        handler = _Handler()
        moments.moments_list(handler, SimpleNamespace(query=f"game_id={self.game_id}"))
        self.assertEqual([item["moment_id"] for item in handler.responses[-1][1]["items"]], [second["moment_id"], first["moment_id"]])

        handler = _Handler()
        moments.moments_update(handler, {"game_id": self.game_id, "moment_id": first["moment_id"], "note": "edited"})
        self.assertEqual(handler.responses[-1][1]["item"]["note"], "edited")

        handler = _Handler()
        moments.moments_delete(handler, {"game_id": self.game_id, "moment_id": second["moment_id"]})
        self.assertTrue(handler.responses[-1][1]["deleted"])
        self.assertEqual(len(openbox.load_state()["games"][0]["moments"]), 1)

    def test_partial_update_does_not_reset_omitted_fields(self):
        item = self._create(note="keep this", title="Keep title")
        handler = _Handler()
        moments.moments_update(handler, {
            "game_id": self.game_id,
            "moment_id": item["moment_id"],
            "note": "new note",
        })
        self.assertEqual(handler.responses[-1][1]["item"]["note"], "new note")
        self.assertEqual(handler.responses[-1][1]["item"]["title"], "Keep title")

        handler = _Handler()
        moments.moments_update(handler, {
            "game_id": self.game_id,
            "moment_id": item["moment_id"],
            "title": "new title",
        })
        self.assertEqual(handler.responses[-1][1]["item"]["note"], "new note")
        self.assertEqual(handler.responses[-1][1]["item"]["title"], "new title")

    def test_resume_link_is_only_attached_when_current_state_is_available(self):
        item = self._create(note="No state yet", resume_state={"file": "not-a-real-state"})
        self.assertIsNone(item["resume_state"])

    def test_resume_link_requires_the_current_immutable_capture(self):
        game = openbox.load_state()["games"][0]
        state_dir = moments.state_dir_for(game, self.data_path.parent)
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "resume.state").write_bytes(b"resume")
        status = {
            "enabled": True,
            "capable": True,
            "available": True,
            "stale": False,
            "state": {"file": "resume.state", "capture_id": "capture-new"},
        }
        with patch.object(moments, "resume_status", return_value=status):
            self.assertIsNone(moments._resume_link(game, openbox.load_state(), {
                "file": "resume.state", "capture_id": "capture-old",
            }))
            link = moments._resume_link(game, openbox.load_state(), {
                "file": "resume.state", "capture_id": "capture-new",
            })
        self.assertEqual(link["capture_id"], "capture-new")
        self.assertTrue(link["immutable"])
        self.assertTrue(link["file"].startswith("moments/"))
        self.assertEqual((state_dir / link["file"]).read_bytes(), b"resume")

    def test_public_projection_exposes_ra_counters_for_auto_moments(self):
        game = openbox.load_state()["games"][0]
        game["ra_game_id"] = "123"
        cache_path = self.root / "cache" / "retroachievements" / f"{self.game_id}.json"
        cache_path.parent.mkdir(parents=True)
        cache_path.write_text('{"earned": 7, "total": 20, "progress_pct": 35, "mastered": false}')
        from pkg.state.cache import _project_game
        projected = _project_game(game, 0, set(), set(), None, {}, 0)
        self.assertEqual(projected["ra_achievements_earned"], 7)
        self.assertEqual(projected["ra_achievements_total"], 20)
        self.assertEqual(projected["ra_progress_pct"], 35)

    def test_auto_trigger_detects_first_boot_unlock_progress_and_milestone(self):
        self.assertEqual(moments.auto_moment_trigger({}, {"play_count": 1}), "first_boot")
        self.assertEqual(moments.auto_moment_trigger({"ra_achievements_earned": 2}, {"ra_achievements_earned": 3}), "ra_unlock")
        self.assertEqual(moments.auto_moment_trigger({"progress": 0.4}, {"progress": 1}), "progress")
        self.assertEqual(moments.auto_moment_trigger({"progress": "Playing"}, {"progress": "Beaten"}), "progress")
        self.assertEqual(moments.auto_moment_trigger({"playtime_seconds": 3599}, {"playtime_seconds": 3600}), "milestone")

    def test_routes_manifest_and_frontend_contract_are_registered(self):
        from routes import GET_TABLE, POST_TABLE, PUBLIC_GET_PATHS
        from routes.registry import all_routes

        self.assertEqual(GET_TABLE["/api/v2/moments"], "handlers.moments.moments_list")
        self.assertEqual(POST_TABLE["/api/v2/moments"], "handlers.moments.moments_create")
        self.assertEqual(POST_TABLE["/api/v2/moments/update"], "handlers.moments.moments_update")
        self.assertEqual(POST_TABLE["/api/v2/moments/delete"], "handlers.moments.moments_delete")
        self.assertIn("/static/moments.js", PUBLIC_GET_PATHS)
        self.assertTrue(any(route.path == "/api/v2/moments" for route in all_routes()))
        self.assertIn("handlers/moments.py", (ROOT / "runtime_modules.txt").read_text())

        source = {name: (ROOT / "static" / name).read_text(encoding="utf-8") for name in (
            "moments.js", "app.js", "navigation.js", "bigbox.js", "library.js",
        )}
        self.assertIn("/api/v2/moments", source["moments.js"])
        self.assertIn("shareCardPayload", source["moments.js"])
        self.assertIn("capture_id", source["moments.js"])
        self.assertIn("resume_state: item.resume_state", source["moments.js"])
        self.assertIn("momentsTab", source["library.js"])
        self.assertIn("captureMomentInteractive", source["navigation.js"])
        self.assertIn("pauseMoment", source["bigbox.js"])
        self.assertIn("openMoment", source["app.js"])


if __name__ == "__main__":
    unittest.main()
