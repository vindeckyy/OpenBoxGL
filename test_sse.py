"""SSE event stream contract tests."""

import os
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer


class SseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls._prev_data_dir = os.environ.get("OPENBOX_DATA_DIR")
        os.environ["OPENBOX_DATA_DIR"] = cls.tempdir.name
        import web_app

        cls.web_app = web_app
        web_app.TOKEN = "sse-test-token"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web_app.Handler)
        cls.port = cls.server.server_address[1]
        cls.origin = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.tempdir.cleanup()
        if cls._prev_data_dir is None:
            os.environ.pop("OPENBOX_DATA_DIR", None)
        else:
            os.environ["OPENBOX_DATA_DIR"] = cls._prev_data_dir

    def read_event(self, resp, needle, timeout=5.0):
        deadline = time.time() + timeout
        got = False
        while time.time() < deadline:
            line = resp.readline()
            if not line:
                break
            if needle in line.decode(errors="replace"):
                got = True
            if got and line.strip() == b"":
                return True
        return got

    def test_events_streams_broadcast(self):
        def emit():
            time.sleep(0.3)
            self.web_app.broadcast_event("session.started", {"id": 1, "game": "Quake"})

        threading.Thread(target=emit, daemon=True).start()
        req = urllib.request.Request(
            self.origin + "/api/events",
            headers={"X-OpenBox-Token": "sse-test-token", "Accept": "text/event-stream"},
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            self.assertEqual(resp.status, 200)
            self.assertTrue(resp.headers["Content-Type"].startswith("text/event-stream"))
            self.assertTrue(self.read_event(resp, "session.started"))

    def test_events_requires_auth(self):
        req = urllib.request.Request(self.origin + "/api/events")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
