"""SSE event stream contract tests."""

import os
import queue
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
        import webapp_state

        def emit():
            time.sleep(0.3)
            webapp_state.broadcast_event("session.started", {"id": 1, "game": "Quake"})

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

    def test_slow_subscriber_is_dropped_without_blocking_broadcast(self):
        import webapp_state

        subscriber = queue.Queue(maxsize=1)
        subscriber.put(("old", "{}"))
        self.assertTrue(webapp_state.register_event_subscriber(subscriber))
        try:
            started = time.monotonic()
            webapp_state.broadcast_event("session.started", {"id": 1})
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertNotIn(subscriber, webapp_state.EVENT_SUBSCRIBERS)
            self.assertIsNone(subscriber.get_nowait())
        finally:
            webapp_state.unregister_event_subscriber(subscriber)

    def test_large_events_are_replaced_with_bounded_payloads(self):
        import webapp_state

        subscriber = queue.Queue(maxsize=2)
        self.assertTrue(webapp_state.register_event_subscriber(subscriber))
        try:
            webapp_state.broadcast_event(
                "session.started",
                {"blob": "x" * (webapp_state.SSE_MAX_EVENT_BYTES + 1)},
            )
            kind, data = subscriber.get(timeout=1)
            self.assertEqual(kind, "session.started")
            self.assertLessEqual(len(data.encode()), webapp_state.SSE_MAX_EVENT_BYTES)
            self.assertIn('"truncated":true', data)
        finally:
            webapp_state.unregister_event_subscriber(subscriber)

    def test_subscriber_count_is_bounded(self):
        import webapp_state

        with webapp_state.EVENT_SUBSCRIBERS_LOCK:
            available = max(0, webapp_state.SSE_MAX_SUBSCRIBERS - len(webapp_state.EVENT_SUBSCRIBERS))
        subscribers = [queue.Queue(maxsize=1) for _ in range(available)]
        registered = []
        try:
            for subscriber in subscribers:
                self.assertTrue(webapp_state.register_event_subscriber(subscriber))
                registered.append(subscriber)
            self.assertFalse(webapp_state.register_event_subscriber(queue.Queue(maxsize=1)))
        finally:
            for subscriber in registered:
                webapp_state.unregister_event_subscriber(subscriber)

    def test_delta_library_endpoint_concurrency(self):
        import concurrent.futures
        import json
        from openbox import update_state

        def populate(state):
            state["games"] = [
                {"game_id": f"game_{i}", "name": f"Game {i}", "path": f"/bin/{i}"}
                for i in range(100)
            ]
        update_state(populate)

        def fetch_delta(ids_param):
            req = urllib.request.Request(
                f"{self.origin}/api/library/delta?ids={ids_param}&token=sse-test-token"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode())
                return len(data.get("games", []))

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [
                pool.submit(fetch_delta, f"game_{i % 100},game_{(i + 1) % 100}")
                for i in range(50)
            ]
            results = [f.result() for f in futures]
            self.assertTrue(all(r == 2 for r in results))


if __name__ == "__main__":
    unittest.main()

