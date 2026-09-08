#!/usr/bin/env python3
"""Real HTTP tests for opt-in causal sync preview/apply."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class SyncHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["OPENBOX_DATA_DIR"] = cls.tmp.name
        import openbox
        import web_app

        cls.openbox = openbox
        cls.web_app = web_app
        web_app.TOKEN = "sync-http-token"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web_app.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.tmp.cleanup()
        os.environ.pop("OPENBOX_DATA_DIR", None)

    def setUp(self):
        self.openbox.STATE_STORE.save({
            "games": [], "profiles": {}, "settings": {"library_sync_enabled": True},
        })

    def post(self, path, body, token="sync-http-token"):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-OpenBox-Token": token},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_preview_apply_and_auth(self):
        from pkg.parity.parity_library_sync import make_event

        event = make_event(device_id="remote", sequence=1, sync_key="game:g1", catalog={"game_id": "g1", "name": "Quake"})
        status, payload = self.post("/api/v2/library/sync/preview", {"events": [event]})
        self.assertEqual(status, 200)
        self.assertEqual(payload["counts"]["additions"], 1)
        status, result = self.post("/api/v2/library/sync/apply", {"plan": payload})
        self.assertEqual(status, 200)
        self.assertEqual(result["applied"], 1)
        self.assertEqual(self.openbox.load_state()["games"][0]["name"], "Quake")
        cloud = Path(self.tmp.name) / "cloud"
        cloud.mkdir()
        self.openbox.STATE_STORE.update(lambda state: state["settings"].update({"cloud_folder": str(cloud)}))
        from webapp_state import transact_state
        transact_state(lambda state: state["games"][0].update({"name": "Quake II"}))
        status, published = self.post("/api/v2/library/sync/publish", {"protocol": "v3"})
        self.assertEqual(status, 200)
        self.assertEqual(published["protocol"], "v3")
        self.assertEqual(published["acknowledged"], 1)
        status, body = self.post("/api/v2/library/sync/preview", {"events": [event]}, token="wrong")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "Unauthorized")


if __name__ == "__main__":
    unittest.main()
