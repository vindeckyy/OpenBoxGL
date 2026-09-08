"""Launch reservation and HTTP boundary regressions."""

from __future__ import annotations

import json
import os
import threading
import tempfile
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import webapp_state  # noqa: E402
from api_errors import Conflict  # noqa: E402
from pkg.parity.parity_perf import PerfLease  # noqa: E402
from pkg.state import launch as launch_state  # noqa: E402
from pkg.state.registry import PENDING_LAUNCHES, PROCESSES, RUNNING  # noqa: E402


class LaunchReservationTests(unittest.TestCase):
    def setUp(self):
        RUNNING.clear()
        PROCESSES.clear()
        PENDING_LAUNCHES.clear()
        self.state = {
            "games": [{"game_id": "game-1", "name": "Game", "path": "/tmp/game"}],
            "profiles": {},
            "settings": {},
            "history": [],
            "active_sessions": [],
        }
        self.process = MagicMock(pid=1234)
        self.process.poll.return_value = None
        self.lease = PerfLease(applied=True, profile_name="balanced", restore=MagicMock())
        self.real_thread = threading.Thread

    def tearDown(self):
        RUNNING.clear()
        PROCESSES.clear()
        PENDING_LAUNCHES.clear()

    def _launch_patches(self, command=(['/bin/true'], '/tmp'), *, popen=None):
        popen = popen or (lambda *args, **kwargs: self.process)
        return patch.multiple(
            webapp_state,
            load_state=MagicMock(return_value=self.state),
            _resolve_start_game=MagicMock(return_value=(self.state["games"][0], 0)),
            _start_launch_command=MagicMock(return_value=command),
            _apply_start_plugins=MagicMock(side_effect=lambda game, args, cwd: (args, cwd)),
            _validate_start_command=MagicMock(),
            apply_perf_profile=MagicMock(return_value=self.lease),
            update_state=MagicMock(side_effect=lambda mutator: mutator(self.state)),
            _annotate_gamescope_start=MagicMock(),
            _publish_start_events=MagicMock(),
            finish_session=MagicMock(),
            subprocess=MagicMock(Popen=MagicMock(side_effect=popen)),
        )

    def test_concurrent_requests_reserve_once(self):
        entered = threading.Event()
        release = threading.Event()

        def slow_command(game, profiles):
            entered.set()
            release.wait(timeout=2)
            return ["/bin/true"], "/tmp"

        with self._launch_patches(), patch.object(webapp_state, "_start_launch_command", side_effect=slow_command), patch(
            "pkg.state.launch.threading.Thread"
        ) as watcher:
            results = []

            def launch():
                try:
                    results.append(webapp_state.start_game(0))
                except Exception as error:  # one expected conflict
                    results.append(error)

            first = self.real_thread(target=launch)
            first.start()
            self.assertTrue(entered.wait(timeout=2))
            second = self.real_thread(target=launch)
            second.start()
            second.join(timeout=2)
            release.set()
            first.join(timeout=2)

        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(sum(type(result).__name__ == "Conflict" for result in results), 1)
        self.assertEqual(len(RUNNING), 1)
        self.assertEqual(len(self.state["active_sessions"]), 1)
        self.assertFalse(PENDING_LAUNCHES)
        watcher.assert_called_once()

    def test_wrapper_exit_does_not_clear_tracked_session(self):
        """Folder/process-name tracking outlives a launcher wrapper."""
        RUNNING["tracked"] = {
            "stable_game_id": "game-1", "game": "Game", "tracking_mode": "folder",
        }
        wrapper = MagicMock()
        wrapper.poll.return_value = 0
        PROCESSES["tracked"] = wrapper
        with self.assertRaises(Conflict) as raised:
            launch_state._reserve_launch("game-1", "second", game_name="Game")
        self.assertEqual(raised.exception.code, "LAUNCH_ALREADY_ACTIVE")
        self.assertIn("tracked", RUNNING)

    def test_validation_failure_releases_reservation_for_retry(self):
        bad = ValueError("invalid working directory")
        with self._launch_patches(), patch.object(webapp_state, "_start_launch_command", side_effect=[bad, (["/bin/true"], "/tmp")]), patch.object(
            webapp_state, "_validate_start_command", side_effect=[None, None]
        ), patch("pkg.state.launch.threading.Thread"):
            with self.assertRaises(ValueError):
                webapp_state.start_game(0)
            entry = webapp_state.start_game(0)
        self.assertTrue(entry["launch_id"])
        self.assertFalse(PENDING_LAUNCHES)

    def test_reorder_uses_stable_id_and_delete_rolls_back(self):
        second = {"game_id": "game-2", "name": "Other", "path": "/tmp/other"}
        self.state["games"].append(second)
        gate = threading.Event()

        def command(game, profiles):
            gate.wait(timeout=2)
            return ["/bin/true"], "/tmp"

        entries = []
        with self._launch_patches(), patch.object(webapp_state, "_start_launch_command", side_effect=command), patch(
            "pkg.state.launch.threading.Thread"
        ):
            first = self.real_thread(target=lambda: entries.append(webapp_state.start_game(0)))
            first.start()
            while not PENDING_LAUNCHES:
                pass
            self.state["games"][:] = [second, self.state["games"][0]]
            gate.set()
            first.join(timeout=2)
        self.assertEqual(self.state["games"][1]["play_count"], 1)
        self.assertEqual(self.state["games"][0].get("play_count", 0), 0)
        self.assertEqual(entries[0]["game_id"], 1)

        RUNNING.clear()
        PROCESSES.clear()
        PENDING_LAUNCHES.clear()
        self.state["games"].pop(1)
        gate.clear()
        with self._launch_patches(), patch.object(webapp_state, "_start_launch_command", side_effect=command), patch(
            "pkg.state.launch.threading.Thread"
        ), patch("pkg.state.launch._terminate_owned_process") as terminate:
            first = self.real_thread(target=lambda: self._expect_failure(webapp_state.start_game, 0))
            first.start()
            while not PENDING_LAUNCHES:
                pass
            self.state["games"].clear()
            gate.set()
            first.join(timeout=2)
        self.assertFalse(RUNNING)
        self.assertFalse(PROCESSES)
        self.assertFalse(PENDING_LAUNCHES)
        terminate.assert_called_once()

    @staticmethod
    def _expect_failure(function, *args):
        try:
            function(*args)
        except IndexError:
            return
        raise AssertionError("launch should fail after the game is deleted")

    def test_post_registration_failure_cleans_all_maps(self):
        with self._launch_patches(), patch.object(webapp_state, "_publish_start_events", side_effect=RuntimeError("publish")), patch(
            "pkg.state.launch._terminate_owned_process"
        ) as terminate, patch("pkg.state.launch.threading.Thread"):
            with self.assertRaises(RuntimeError):
                webapp_state.start_game(0)
        self.assertFalse(RUNNING)
        self.assertFalse(PROCESSES)
        self.assertFalse(PENDING_LAUNCHES)
        self.assertEqual(self.state["active_sessions"], [])
        terminate.assert_called_once()


class RealLaunchHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.previous_data_dir = os.environ.get("OPENBOX_DATA_DIR")
        os.environ["OPENBOX_DATA_DIR"] = cls.tempdir.name
        import openbox
        import web_app
        from state_store import JsonStateStore

        cls.openbox = openbox
        cls.web_app = web_app
        cls.previous_store = openbox.STATE_STORE
        cls.data_path = Path(cls.tempdir.name) / "library.json"
        openbox.DATA = cls.data_path
        openbox.STATE_STORE = JsonStateStore(cls.data_path)
        web_app.TOKEN = "launch-http-token"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web_app.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.openbox.STATE_STORE = cls.previous_store
        cls.tempdir.cleanup()
        if cls.previous_data_dir is None:
            os.environ.pop("OPENBOX_DATA_DIR", None)
        else:
            os.environ["OPENBOX_DATA_DIR"] = cls.previous_data_dir

    def setUp(self):
        self.openbox.save_state({
            "games": [{"game_id": "game-http", "name": "HTTP Game", "path": "/bin/true"}],
            "profiles": {},
            "history": [],
            "settings": {},
            "playlists": [],
        })
        RUNNING.clear()
        PROCESSES.clear()
        PENDING_LAUNCHES.clear()

    def request(self, payload, token="launch-http-token"):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/launch",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **({"X-OpenBox-Token": token} if token else {})},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            finally:
                error.close()

    def test_auth_validation_and_conflict(self):
        status, payload = self.request({"id": 0}, token=None)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "Unauthorized")

        status, payload = self.request({})
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "BAD_REQUEST")

        stable_id = self.openbox.load_state()["games"][0]["game_id"]
        RUNNING["existing"] = {"stable_game_id": stable_id, "game": "HTTP Game"}
        status, payload = self.request({"game_id": stable_id})
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "LAUNCH_ALREADY_ACTIVE")


if __name__ == "__main__":
    unittest.main()
