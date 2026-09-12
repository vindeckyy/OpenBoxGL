"""Quick Resume v2 route tests — real HTTP boundary (T1-core)."""

from __future__ import annotations

import json
import os
import shutil
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


class ResumeHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.previous_data_dir = os.environ.get("OPENBOX_DATA_DIR")
        os.environ["OPENBOX_DATA_DIR"] = cls.tempdir.name
        import openbox
        import web_app
        from state_store import JsonStateStore
        from pkg.parity import parity_emulator_defs as defs

        cls.openbox = openbox
        cls.web_app = web_app
        cls.defs = defs
        cls.previous_store = openbox.STATE_STORE
        cls.previous_data = openbox.DATA
        cls.data_path = Path(cls.tempdir.name) / "library.json"
        openbox.DATA = cls.data_path
        openbox.STATE_STORE = JsonStateStore(cls.data_path)
        # ``webapp_state`` snapshots ``from openbox import DATA`` at import
        # time and ``_populate_deps`` copies both into the _deps registry —
        # when another suite imported webapp_state before this env was set,
        # those snapshots still point at the real default store.  Rebind them
        # so ``_ns("DATA")`` inside start_game resolves the tempdir.
        import webapp_state
        from pkg.state import _deps

        cls.previous_deps = {
            name: _deps.get(name) for name in ("DATA", "STATE_STORE")
        }
        cls.previous_ws = (webapp_state.DATA, webapp_state.STATE_STORE)
        webapp_state.DATA = cls.data_path
        webapp_state.STATE_STORE = openbox.STATE_STORE
        _deps.register("DATA", cls.data_path)
        _deps.register("STATE_STORE", openbox.STATE_STORE)
        web_app.TOKEN = "resume-http-token"
        cls.adapter = {
            "adapter_id": "t1-http-ra",
            "emulator_id": "fake.ra",
            "label": "FakeRA",
            "platform": "SNES",
            "extensions": ["sfc"],
            "native_exe": "/bin/true",
            "flatpak_app_id": "",
            "startup_args": ["{path}"],
            "executable_patterns": [],
            "state": {
                "kind": "retroarch",
                "template": ["-s", "{state_path}", "--appendconfig", "{state_config}"],
                "capture": ["--appendconfig", "{state_config}"],
                "glob": ["*.state*"],
            },
        }
        registry = defs._registry()
        registry["by_adapter_id"]["t1-http-ra"] = cls.adapter
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web_app.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.defs._registry()["by_adapter_id"].pop("t1-http-ra", None)
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.openbox.STATE_STORE = cls.previous_store
        cls.openbox.DATA = cls.previous_data
        import webapp_state
        from pkg.state import _deps

        webapp_state.DATA, webapp_state.STATE_STORE = cls.previous_ws
        for name, value in cls.previous_deps.items():
            if value is None:
                _deps._REGISTRY.pop(name, None)
            else:
                _deps.register(name, value)
        cls.tempdir.cleanup()
        if cls.previous_data_dir is None:
            os.environ.pop("OPENBOX_DATA_DIR", None)
        else:
            os.environ["OPENBOX_DATA_DIR"] = cls.previous_data_dir

    def setUp(self):
        from pkg.state.registry import PENDING_LAUNCHES, PROCESSES, RUNNING

        self.rom = Path(self.tempdir.name) / "rom.sfc"
        self.rom.write_bytes(b"rom")
        self.openbox.save_state({
            "games": [{
                "game_id": "g-http",
                "name": "HTTP Game",
                "path": str(self.rom),
                "platform": "SNES",
                "emulator_adapter_id": "t1-http-ra",
            }],
            "profiles": {},
            "history": [],
            "settings": {},
            "playlists": [],
            "active_sessions": [],
        })
        # State normalization rewrites non-legacy ids to the stable digest;
        # read the effective id back instead of assuming "g-http" survives.
        self.game_id = self.openbox.load_state()["games"][0]["game_id"]
        # resume_states/ lives in the class-level tempdir; wipe it so planted
        # states never leak across tests.
        from pkg.parity import parity_resume

        shutil.rmtree(parity_resume.resume_root(self.tempdir.name), ignore_errors=True)
        RUNNING.clear()
        PROCESSES.clear()
        PENDING_LAUNCHES.clear()

    def tearDown(self):
        from pkg.state.registry import PENDING_LAUNCHES, PROCESSES, RUNNING

        RUNNING.clear()
        PROCESSES.clear()
        PENDING_LAUNCHES.clear()

    def request(self, method, path, payload=None, token="resume-http-token"):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers={"Content-Type": "application/json", **({"X-OpenBox-Token": token} if token else {})},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            finally:
                error.close()

    def _plant_state(self, version=None):
        from pkg.parity import parity_resume

        sdir = parity_resume.state_dir_for({"game_id": self.game_id}, Path(self.tempdir.name))
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "resume.state").write_bytes(b"state")
        meta = {
            "adapter_id": "t1-http-ra",
            "emulator_version": version or parity_resume.emulator_fingerprint(self.adapter),
            "kind": "retroarch",
            "file": "resume.state",
            "captured_at": "2026-09-12T00:00:00",
        }
        (sdir / "state.json").write_text(json.dumps(meta))

    def test_status_requires_auth(self):
        status, _payload = self.request("GET", f"/api/v2/resume/status?game_id={self.game_id}", token=None)
        self.assertEqual(status, 403)

    def test_status_reports_capability_and_state(self):
        status, payload = self.request("GET", f"/api/v2/resume/status?game_id={self.game_id}")
        self.assertEqual(status, 200)
        self.assertTrue(payload["capable"])
        self.assertTrue(payload["enabled"])
        self.assertFalse(payload["available"])
        self.assertEqual(payload["kind"], "retroarch")

        self._plant_state()
        status, payload = self.request("GET", f"/api/v2/resume/status?game_id={self.game_id}")
        self.assertEqual(status, 200)
        self.assertTrue(payload["available"])
        self.assertFalse(payload["stale"])
        self.assertEqual(payload["state"]["file"], "resume.state")

    def test_status_unknown_game_400(self):
        status, payload = self.request("GET", "/api/v2/resume/status?game_id=nope")
        self.assertEqual(status, 400)

    def test_status_missing_id_400(self):
        status, _payload = self.request("GET", "/api/v2/resume/status")
        self.assertEqual(status, 400)

    def test_resume_launches_with_state_args(self):
        self._plant_state()
        process = MagicMock(pid=987)
        process.poll.return_value = None
        # Patch the module *reference* for threading: patching
        # ``pkg.state.launch.threading.Thread`` would rewrite the stdlib
        # ``threading`` module globally and the test's own HTTP server could
        # never spawn its handler thread.
        with patch("pkg.state.launch.subprocess.Popen", return_value=process) as popen, patch(
            "pkg.state.launch.threading", MagicMock()
        ):
            status, payload = self.request("POST", "/api/v2/resume", {"game_id": self.game_id})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["resumed_from_state"])
        argv = popen.call_args.args[0]
        self.assertIn("-s", argv)
        self.assertIn("resume.state", " ".join(argv))
        self.assertIn("--appendconfig", argv)

    def test_resume_missing_state_404(self):
        status, payload = self.request("POST", "/api/v2/resume", {"game_id": self.game_id})
        self.assertEqual(status, 404)
        self.assertEqual(payload["code"], "RESUME_STATE_MISSING")

    def test_resume_stale_409_then_allow_stale(self):
        self._plant_state(version="ancient")
        status, payload = self.request("POST", "/api/v2/resume", {"game_id": self.game_id})
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "RESUME_STATE_STALE")

        process = MagicMock(pid=988)
        process.poll.return_value = None
        with patch("pkg.state.launch.subprocess.Popen", return_value=process), patch(
            "pkg.state.launch.threading", MagicMock()
        ):
            status, payload = self.request("POST", "/api/v2/resume", {"game_id": self.game_id, "allow_stale": True})
        self.assertEqual(status, 200, payload)

    def test_resume_conflict_when_running_409(self):
        from pkg.state.registry import RUNNING

        self._plant_state()
        RUNNING["other"] = {"stable_game_id": self.game_id, "game": "HTTP Game"}
        status, payload = self.request("POST", "/api/v2/resume", {"game_id": self.game_id})
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "LAUNCH_ALREADY_ACTIVE")

    def test_resume_disabled_400(self):
        self._plant_state()
        state = self.openbox.load_state()
        state["settings"] = {"quick_resume_enabled": False}
        self.openbox.save_state(state)
        status, payload = self.request("POST", "/api/v2/resume", {"game_id": self.game_id})
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "QUICK_RESUME_DISABLED")

    def test_resume_unsupported_adapter_400(self):
        state = self.openbox.load_state()
        state["games"].append({"game_id": "g-plain", "name": "Plain", "path": str(self.rom), "platform": "None", "emulator_adapter_id": ""})
        self.openbox.save_state(state)
        plain_id = self.openbox.load_state()["games"][1]["game_id"]
        self._plant_state()
        status, payload = self.request("POST", "/api/v2/resume", {"game_id": plain_id})
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "RESUME_UNSUPPORTED")

    def test_discard_clears_state(self):
        self._plant_state()
        status, payload = self.request("POST", "/api/v2/resume/discard", {"game_id": self.game_id})
        self.assertEqual(status, 200)
        self.assertTrue(payload["discarded"])
        status, _payload = self.request("GET", f"/api/v2/resume/status?game_id={self.game_id}")
        self.assertFalse(_payload["available"])

    def test_discard_without_state_404(self):
        status, payload = self.request("POST", "/api/v2/resume/discard", {"game_id": self.game_id})
        self.assertEqual(status, 404)
        self.assertEqual(payload["code"], "RESUME_STATE_MISSING")

    def test_routes_in_tables(self):
        from routes import GET_TABLE, POST_TABLE

        self.assertEqual(GET_TABLE["/api/v2/resume/status"], "handlers.resume.resume_status")
        self.assertEqual(POST_TABLE["/api/v2/resume"], "handlers.resume.resume_launch")
        self.assertEqual(POST_TABLE["/api/v2/resume/discard"], "handlers.resume.resume_discard")


if __name__ == "__main__":
    unittest.main()
