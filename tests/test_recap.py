"""Tests for the S2 session recap card.

Covers the finish_session recap publish block, the SSE recap store in
pkg/state/sse.py, and the ``GET /api/v2/sessions/recap`` route. Each test
file runs in its own process via run_all_tests.sh, so a module-level
OPENBOX_DATA_DIR tempdir is safe and keeps every DATA binding consistent.
"""

import json
import os
import queue
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
os.environ["OPENBOX_DATA_DIR"] = _TMP.name
os.environ["OPENBOX_SAFE_MODE"] = "1"


class FinishedProcess:
    """Minimal process double: already exited with a fixed code."""

    pid = 0

    def __init__(self, code=0):
        self._code = code

    def wait(self):
        return self._code

    def poll(self):
        return self._code


def _fresh_state(**overrides):
    state = {"games": [], "profiles": {}, "history": [], "settings": {}, "playlists": []}
    state.update(overrides)
    return state


class RecapBase(unittest.TestCase):
    def setUp(self):
        import web_app
        from openbox import STATE_STORE, load_state
        from webapp_state import PROCESSES, PROCESS_LOCK, RUNNING, SESSION_EVENTS
        from pkg.state import sse

        self.web_app = web_app
        self.sse = sse
        self.STATE_STORE = STATE_STORE
        self.load_state = load_state
        self.RUNNING = RUNNING
        self.PROCESS_LOCK = PROCESS_LOCK
        STATE_STORE.save(_fresh_state())
        with PROCESS_LOCK:
            RUNNING.clear()
            PROCESSES.clear()
        SESSION_EVENTS.clear()
        sse.LAST_SESSION_RECAP.clear()
        sse.SESSION_RA_BASELINES.clear()
        self.subscriber = queue.Queue()
        sse.register_event_subscriber(self.subscriber)
        self.addCleanup(sse.unregister_event_subscriber, self.subscriber)

    def tearDown(self):
        # Drain any leftovers so a failure cannot leak events between tests.
        while not self.subscriber.empty():
            self.subscriber.get_nowait()

    def _events(self):
        events = []
        while True:
            try:
                events.append(self.subscriber.get_nowait())
            except queue.Empty:
                return events

    def _recap_events(self):
        return [
            json.loads(data)
            for kind, data in self._events()
            if kind == "session.recap"
        ]

    def _game_id(self, index=0):
        return self.load_state()["games"][index]["game_id"]

    def _seed_running(self, launch_id="lid-1", game_name="Chrono Trigger", index=0):
        gid = self._game_id(index)
        with self.PROCESS_LOCK:
            self.RUNNING[launch_id] = {"stable_game_id": gid, "game": game_name}
        return gid

    def _finish(self, launch_id="lid-1", index=0, code=0, wait_side_effect=None):
        from webapp_state import finish_session

        if wait_side_effect is None:
            finish_session(launch_id, index, datetime.now(), FinishedProcess(code), mock.Mock())
            return
        real_wait = self._real_wait_for_exit()

        def wait_then(process, game, settings):
            wait_side_effect()
            return real_wait(process, game, settings)

        with mock.patch("webapp_state.wait_for_exit", side_effect=wait_then):
            finish_session(launch_id, index, datetime.now(), FinishedProcess(code), mock.Mock())

    @staticmethod
    def _real_wait_for_exit():
        from pkg.parity.parity_tracking import wait_for_exit
        return wait_for_exit

    def handler(self):
        h = self.web_app.Handler.__new__(self.web_app.Handler)
        h.responses = []
        h.send_json = lambda status, payload: h.responses.append((status, payload))
        return h

    def _ra_cache_dir(self):
        from openbox import DATA
        return Path(DATA).parent / "cache" / "retroachievements"

    def _write_ra_cache(self, game_id, **fields):
        cache_dir = self._ra_cache_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {"game_id": game_id}
        payload.update(fields)
        (cache_dir / f"{game_id}.json").write_text(json.dumps(payload))
        self.addCleanup(lambda: (cache_dir / f"{game_id}.json").unlink(missing_ok=True))


class RecapPublishTest(RecapBase):
    def test_recap_event_and_store_on_session_end(self):
        self.STATE_STORE.save(_fresh_state(
            games=[{"name": "Chrono Trigger", "path": "/bin/true", "screenshots": ["a.png"]}],
            settings={"track_session_history": True},
        ))
        gid = self._seed_running()
        self._finish()
        recaps = self._recap_events()
        self.assertEqual(len(recaps), 1)
        payload = recaps[0]
        self.assertEqual(payload["launch_id"], "lid-1")
        self.assertEqual(payload["game_id"], gid)
        self.assertEqual(payload["name"], "Chrono Trigger")
        self.assertGreaterEqual(payload["seconds"], 1)
        self.assertEqual(payload["exit_code"], 0)
        self.assertFalse(payload["partial"])
        self.assertFalse(payload["game_missing"])
        self.assertEqual(payload["screenshots_taken"], 0)
        self.assertIn("started_at", payload)
        self.assertIn("stopped_at", payload)
        self.assertIn("ra", payload)
        self.assertIn("resume_state_available", payload)
        self.assertIn("capture", payload)
        self.assertEqual(self.sse.last_session_recap(), payload)

    def test_recap_fields_for_killed_session(self):
        self.STATE_STORE.save(_fresh_state(
            games=[{"name": "Crasher", "path": "/bin/true"}],
            settings={"track_session_history": True},
        ))
        self._seed_running(game_name="Crasher")
        self._finish(code=3)
        recaps = self._recap_events()
        self.assertEqual(len(recaps), 1)
        payload = recaps[0]
        self.assertEqual(payload["exit_code"], 3)
        self.assertTrue(payload["partial"])
        self.assertEqual(payload["name"], "Crasher")

    def test_no_recap_when_history_tracking_off(self):
        self.STATE_STORE.save(_fresh_state(
            games=[{"name": "Chrono Trigger", "path": "/bin/true"}],
            settings={"track_session_history": False},
        ))
        self._seed_running()
        with mock.patch(
            "pkg.parity.parity_resume.resume_status",
            return_value={"enabled": True, "capable": True, "available": True, "stale": False},
        ):
            self._finish()
        self.assertEqual(self._recap_events(), [])
        self.assertIsNone(self.sse.last_session_recap())

    def test_no_recap_when_recap_toggle_off(self):
        self.STATE_STORE.save(_fresh_state(
            games=[{"name": "Chrono Trigger", "path": "/bin/true"}],
            settings={"track_session_history": True, "session_recap_enabled": False},
        ))
        self._seed_running()
        self._finish()
        self.assertEqual(self._recap_events(), [])
        self.assertIsNone(self.sse.last_session_recap())

    def test_recap_missing_game_is_name_only(self):
        self.STATE_STORE.save(_fresh_state(
            games=[{"name": "Deleted Mid Session", "path": "/bin/true"}],
            settings={"track_session_history": True},
        ))
        self._seed_running(game_name="Deleted Mid Session")

        def drop_game():
            state = self.load_state()
            state["games"] = []
            self.STATE_STORE.save(state)

        self._finish(wait_side_effect=drop_game)
        recaps = self._recap_events()
        self.assertEqual(len(recaps), 1)
        payload = recaps[0]
        self.assertTrue(payload["game_missing"])
        self.assertEqual(payload["name"], "Deleted Mid Session")
        self.assertTrue(payload["partial"])

    def test_recap_counts_screenshots_taken_during_session(self):
        self.STATE_STORE.save(_fresh_state(
            games=[{"name": "Chrono Trigger", "path": "/bin/true", "screenshots": ["old.png"]}],
            settings={"track_session_history": True},
        ))
        self._seed_running()

        def add_screenshot():
            state = self.load_state()
            state["games"][0].setdefault("screenshots", []).append("new.png")
            self.STATE_STORE.save(state)

        self._finish(wait_side_effect=add_screenshot)
        recaps = self._recap_events()
        self.assertEqual(len(recaps), 1)
        self.assertEqual(recaps[0]["screenshots_taken"], 1)

    def test_recap_ra_delta_from_cache(self):
        self.STATE_STORE.save(_fresh_state(
            games=[{"name": "Chrono Trigger", "path": "/bin/true", "ra_game_id": "1234"}],
            settings={"track_session_history": True},
        ))
        gid = self._seed_running()
        self._write_ra_cache(gid, earned=5, earned_hardcore=2, total=50, progress_pct=10.0)
        # Session start captures the RA baseline through session_event.
        self.sse.session_event("started", "lid-1", "Chrono Trigger")

        def bump_ra_cache():
            self._write_ra_cache(gid, earned=8, earned_hardcore=4, total=50, progress_pct=16.0)

        self._finish(wait_side_effect=bump_ra_cache)
        recaps = self._recap_events()
        self.assertEqual(len(recaps), 1)
        ra = recaps[0]["ra"]
        self.assertTrue(ra["tracked"])
        self.assertTrue(ra["available"])
        self.assertEqual(ra["earned"], 8)
        self.assertEqual(ra["earned_hardcore"], 4)
        self.assertEqual(ra["total"], 50)
        self.assertEqual(ra["earned_delta"], 3)
        self.assertEqual(ra["earned_hardcore_delta"], 2)
        self.assertTrue(ra["baseline"])

    def test_recap_ra_untracked_game(self):
        self.STATE_STORE.save(_fresh_state(
            games=[{"name": "No RA", "path": "/bin/true"}],
            settings={"track_session_history": True},
        ))
        self._seed_running(game_name="No RA")
        self._finish()
        recaps = self._recap_events()
        self.assertEqual(len(recaps), 1)
        ra = recaps[0]["ra"]
        self.assertFalse(ra["tracked"])
        self.assertFalse(ra["available"])
        self.assertEqual(ra["earned_delta"], 0)

    def test_recap_detects_resume_state(self):
        from openbox import DATA
        self.STATE_STORE.save(_fresh_state(
            games=[{"name": "Chrono Trigger", "path": "/bin/true"}],
            settings={"track_session_history": True},
        ))
        gid = self._seed_running()
        states_dir = Path(DATA).parent / "resume_states" / f"{gid}-slot0"
        states_dir.mkdir(parents=True, exist_ok=True)
        (states_dir / "meta.json").write_text(json.dumps({"game_id": gid}))
        self.addCleanup(lambda: __import__("shutil").rmtree(Path(DATA).parent / "resume_states", ignore_errors=True))
        with mock.patch(
            "pkg.parity.parity_resume.resume_status",
            return_value={"enabled": True, "capable": True, "available": True, "stale": False},
        ):
            self._finish()
        recaps = self._recap_events()
        self.assertEqual(len(recaps), 1)
        self.assertTrue(recaps[0]["resume_state_available"])

    def test_recap_reports_progress_for_quick_actions(self):
        self.STATE_STORE.save(_fresh_state(
            games=[{"name": "Chrono Trigger", "path": "/bin/true", "progress": "Playing"}],
            settings={"track_session_history": True},
        ))
        self._seed_running()
        self._finish()
        recaps = self._recap_events()
        self.assertEqual(recaps[0]["progress"], "Playing")


class RecapRouteTest(RecapBase):
    def _call(self, path="/api/v2/sessions/recap"):
        from urllib.parse import urlparse
        h = self.handler()
        h._api_get_api_v2_sessions_recap(urlparse(path))
        return h

    def test_route_returns_last_session_recap(self):
        self.STATE_STORE.save(_fresh_state(
            games=[{"name": "Chrono Trigger", "path": "/bin/true"}],
            settings={"track_session_history": True},
        ))
        self._seed_running()
        self._finish()
        self._events()  # drain SSE queue; route must serve the stored recap
        h = self._call()
        self.assertEqual(h.responses[0][0], 200)
        payload = h.responses[0][1]
        self.assertEqual(payload["launch_id"], "lid-1")
        self.assertEqual(payload["name"], "Chrono Trigger")
        self.assertIn("ra", payload)

    def test_route_404_when_no_session(self):
        self.STATE_STORE.save(_fresh_state(settings={"track_session_history": True}))
        from api_errors import NotFound
        with self.assertRaises(NotFound):
            self._call()

    def test_route_404_when_history_tracking_off(self):
        self.STATE_STORE.save(_fresh_state(
            games=[{"name": "Chrono Trigger", "path": "/bin/true"}],
            settings={"track_session_history": False},
            history=[{"game": "Old", "started": "2026-01-01T00:00:00", "seconds": 5, "exit_code": 0, "game_id": "g1"}],
        ))
        from api_errors import NotFound
        with self.assertRaises(NotFound):
            self._call()

    def test_route_404_when_recap_toggle_off(self):
        self.STATE_STORE.save(_fresh_state(
            games=[{"name": "Chrono Trigger", "path": "/bin/true"}],
            settings={"track_session_history": True, "session_recap_enabled": False},
            history=[{"game": "Old", "started": "2026-01-01T00:00:00", "seconds": 5, "exit_code": 0, "game_id": "g1"}],
        ))
        from api_errors import NotFound
        with self.assertRaises(NotFound):
            self._call()

    def test_route_falls_back_to_latest_history_entry(self):
        """SSE drop / restart: derive a partial recap from the newest history row."""
        self.STATE_STORE.save(_fresh_state(
            games=[{"name": "Chrono Trigger", "path": "/bin/true"}],
            settings={"track_session_history": True},
        ))
        gid = self._game_id()
        state = self.load_state()
        state["history"] = [{
            "game": "Chrono Trigger",
            "started": "2026-09-01T20:00:00",
            "seconds": 900,
            "exit_code": 0,
            "game_id": gid,
        }]
        self.STATE_STORE.save(state)
        h = self._call()
        self.assertEqual(h.responses[0][0], 200)
        payload = h.responses[0][1]
        self.assertEqual(payload["game_id"], gid)
        self.assertEqual(payload["name"], "Chrono Trigger")
        self.assertEqual(payload["seconds"], 900)
        self.assertTrue(payload["partial"])
        self.assertFalse(payload["game_missing"])

    def test_route_history_fallback_missing_game(self):
        self.STATE_STORE.save(_fresh_state(settings={"track_session_history": True}))
        state = self.load_state()
        state["history"] = [{
            "game": "Deleted Game",
            "started": "2026-09-01T20:00:00",
            "seconds": 60,
            "exit_code": 0,
            "game_id": "gone-1",
        }]
        self.STATE_STORE.save(state)
        h = self._call()
        self.assertEqual(h.responses[0][0], 200)
        payload = h.responses[0][1]
        self.assertTrue(payload["game_missing"])
        self.assertEqual(payload["name"], "Deleted Game")

    def test_route_is_registered_and_authenticated(self):
        from routes import GET_TABLE, PUBLIC_GET_PATHS
        from routes.registry import _REGISTRY

        self.assertIn("/api/v2/sessions/recap", GET_TABLE)
        self.assertNotIn("/api/v2/sessions/recap", PUBLIC_GET_PATHS)
        self.assertEqual(
            GET_TABLE["/api/v2/sessions/recap"],
            "_api_get_api_v2_sessions_recap",
        )
        self.assertIn(("GET", "/api/v2/sessions/recap"), _REGISTRY)

    def test_route_requires_auth_in_dispatch(self):
        from urllib.parse import urlparse
        from routes import dispatch_get

        h = self.handler()
        h.authorized = lambda: False
        h.handle_unauthorized = lambda: h.responses.append((401, {"error": "unauthorized"}))
        dispatch_get(h, urlparse("/api/v2/sessions/recap"))
        self.assertEqual(h.responses, [(401, {"error": "unauthorized"})])

    def test_static_recap_js_is_registered_public_asset(self):
        """The card module ships as a public static asset like its siblings."""
        from routes import GET_TABLE, PUBLIC_GET_PATHS
        from routes.registry import _REGISTRY

        self.assertEqual(GET_TABLE["/static/recap.js"], "_api_get_static")
        self.assertIn("/static/recap.js", PUBLIC_GET_PATHS)
        entry = _REGISTRY.get(("GET", "/static/recap.js"))
        self.assertIsNotNone(entry)
        self.assertEqual(entry.spec, "_api_get_static")
        self.assertTrue(entry.public)


class RecapSettingsTest(RecapBase):
    def test_settings_key_registered_and_cleaned(self):
        from handlers.settings import clean_settings
        from settings_schema import KNOWN_SETTINGS

        self.assertIn("session_recap_enabled", KNOWN_SETTINGS)
        cleaned = clean_settings({"session_recap_enabled": False})
        self.assertIs(cleaned["session_recap_enabled"], False)
        cleaned_default = clean_settings({})
        self.assertIs(cleaned_default["session_recap_enabled"], True)

    def test_public_settings_exposes_key(self):
        from pkg.state.cache import _public_settings_uncached

        public = _public_settings_uncached({"settings": {"session_recap_enabled": False}, "games": []})
        self.assertIs(public["session_recap_enabled"], False)
        public_default = _public_settings_uncached({"settings": {}, "games": []})
        self.assertIs(public_default["session_recap_enabled"], True)


class RecapInternalsTest(RecapBase):
    """Branch coverage for the recap helpers' defensive paths."""

    def test_ra_cache_snapshot_rejects_empty_game_id(self):
        self.assertEqual(self.sse.ra_cache_snapshot(Path("/tmp"), ""), {})

    def test_capture_baseline_ignores_empty_launch_id(self):
        self.sse.capture_session_ra_baseline("", "g1")
        self.assertNotIn("", self.sse.SESSION_RA_BASELINES)

    def test_baseline_store_is_bounded(self):
        for i in range(self.sse.SESSION_RA_BASELINE_LIMIT + 3):
            self.sse.capture_session_ra_baseline(f"l{i}", "g")
        self.assertLessEqual(
            len(self.sse.SESSION_RA_BASELINES), self.sse.SESSION_RA_BASELINE_LIMIT
        )

    def test_ra_int_coerces_bad_values(self):
        self.assertEqual(self.sse._ra_int({"earned": object()}, "earned"), 0)

    def test_ra_block_tolerates_bad_progress_pct(self):
        block = self.sse._ra_block({"ra_game_id": "1"}, {}, {"progress_pct": "junk"})
        self.assertEqual(block["progress_pct"], 0.0)
        self.assertTrue(block["available"])

    def test_resume_state_rejects_empty_game_id(self):
        self.assertFalse(self.sse._resume_state_available(Path("/tmp"), ""))

    def test_resume_state_uses_validated_status(self):
        from openbox import DATA
        game = {"game_id": "g-77", "path": "/tmp/game.rom"}
        with mock.patch(
            "pkg.parity.parity_resume.resume_status",
            return_value={"enabled": True, "capable": True, "available": True, "stale": False},
        ):
            self.assertTrue(self.sse._resume_state_available(Path(DATA).parent, "g-77", game=game))
        with mock.patch(
            "pkg.parity.parity_resume.resume_status",
            return_value={"enabled": True, "capable": True, "available": False, "stale": False},
        ):
            self.assertFalse(self.sse._resume_state_available(Path(DATA).parent, "g-77", game=game))

    def test_capture_flags_survive_static_dir_failure(self):
        with mock.patch.object(Path, "is_file", side_effect=OSError):
            self.assertEqual(self.sse._capture_flags(), {"moment": False, "clip": False})

    def test_store_recap_ignores_non_dict(self):
        self.sse.store_session_recap("not-a-payload")
        self.assertIsNone(self.sse.last_session_recap())

    def test_publish_session_recap_swallows_internal_errors(self):
        """publish_session_recap's own except guards every step it wraps."""
        with mock.patch(
            "pkg.state.sse.build_session_recap", side_effect=RuntimeError("boom")
        ):
            self.sse.publish_session_recap(
                launch_id="x", session={}, game_name="g", exit_code=0,
                seconds=1, state={}, game_before=None, identity={},
                data_parent=Path("/tmp"),
            )
        self.assertIsNone(self.sse.last_session_recap())

    def test_publish_failure_never_breaks_finish_session(self):
        """A recap-publish crash is logged, never raised (launch.py publish block)."""
        self.STATE_STORE.save(_fresh_state(
            games=[{"name": "Chrono Trigger", "path": "/bin/true"}],
            settings={"track_session_history": True},
        ))
        self._seed_running()
        with mock.patch(
            "pkg.state.sse.publish_session_recap", side_effect=RuntimeError("boom")
        ):
            self._finish()
        self.assertEqual(self._recap_events(), [])
        state = self.load_state()
        self.assertEqual(len(state["history"]), 1)


class RecapStoreTest(RecapBase):
    def test_last_session_recap_returns_none_initially(self):
        self.assertIsNone(self.sse.last_session_recap())

    def test_store_session_recap_roundtrip(self):
        self.sse.store_session_recap({"launch_id": "x", "name": "Game"})
        self.assertEqual(self.sse.last_session_recap(), {"launch_id": "x", "name": "Game"})

    def test_recap_event_survives_sse_drop(self):
        """No subscriber at publish time still leaves the recap retrievable."""
        self.sse.unregister_event_subscriber(self.subscriber)
        self.STATE_STORE.save(_fresh_state(
            games=[{"name": "Chrono Trigger", "path": "/bin/true"}],
            settings={"track_session_history": True},
        ))
        self._seed_running()
        self._finish()
        recap = self.sse.last_session_recap()
        self.assertIsNotNone(recap)
        self.assertEqual(recap["launch_id"], "lid-1")


if __name__ == "__main__":
    unittest.main()
