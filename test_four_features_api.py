"""HTTP contract tests for the four new OpenBox feature surfaces."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer


class FourFeatureApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.previous_data_dir = os.environ.get("OPENBOX_DATA_DIR")
        os.environ["OPENBOX_DATA_DIR"] = cls.tempdir.name
        import web_app
        from openbox import save_state
        cls.web_app = web_app
        cls.save_state = staticmethod(save_state)
        web_app.TOKEN = "four-feature-token"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web_app.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.tempdir.cleanup()
        if cls.previous_data_dir is None:
            os.environ.pop("OPENBOX_DATA_DIR", None)
        else:
            os.environ["OPENBOX_DATA_DIR"] = cls.previous_data_dir

    def setUp(self):
        self.save_state({
            "games": [
                {"game_id": "g-alpha", "name": "Alpha", "path": "/bin/true", "tags": []},
                {"game_id": "g-beta", "name": "Beta", "path": "/bin/true", "tags": []},
            ],
            "profiles": {}, "history": [], "settings": {}, "playlists": [],
            "queue": [], "notifications": [],
        })

    def request(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers={"X-OpenBox-Token": "four-feature-token", "Content-Type": "application/json"},
            method="POST" if data is not None else "GET",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())

    def test_queue_and_tags_round_trip(self):
        _, library = self.request("/api/library")
        game_id = library["games"][0]["game_id"]
        status, payload = self.request("/api/queue", {"action": "enqueue", "game_ids": [game_id], "note": "first"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["queue"][0]["game_id"], game_id)
        self.assertEqual(payload["queue"][0]["note"], "first")
        status, payload = self.request("/api/tags", {"ids": [game_id], "tags": ["RPG", "rpg"]})
        self.assertEqual(status, 200)
        self.assertEqual(payload["updated"], 1)
        status, library = self.request("/api/library")
        self.assertEqual(status, 200)
        self.assertEqual(library["games"][0]["tags"], ["RPG"])

    def test_notifications_and_webhook_read_models(self):
        self.save_state({
            "games": [{"game_id": "g-alpha", "name": "Alpha", "path": "/bin/true"}],
            "profiles": {}, "history": [], "settings": {"webhooks": []}, "playlists": [],
            "queue": [], "notifications": [{"id": "n1", "title": "Notice", "body": "Body", "read": False}],
        })
        status, payload = self.request("/api/notifications")
        self.assertEqual((status, payload["unread"]), (200, 1))
        status, payload = self.request("/api/notifications", {"action": "read", "ids": ["n1"]})
        self.assertEqual((status, payload["unread"]), (200, 0))
        status, payload = self.request("/api/webhooks")
        self.assertEqual(status, 200)
        self.assertIn("events", payload)
        self.assertEqual(payload["webhooks"], [])


if __name__ == "__main__":
    unittest.main()
