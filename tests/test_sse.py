"""SSE event stream contract tests."""

import os
import queue
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


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

    def test_operation_terminal_event_on_sse(self):
        import webapp_state
        from pkg.state.operations import get_operation_service

        subscriber = queue.Queue(maxsize=8)
        self.assertTrue(webapp_state.register_event_subscriber(subscriber))
        try:
            service = get_operation_service()
            created = service.create(operation_type="library.backup", title="Backup")
            service.finish(created["job_id"], state="done", result={"completed": 1})
            kinds = []
            deadline = time.time() + 2
            while time.time() < deadline and "job.finished" not in kinds:
                try:
                    kind, data = subscriber.get(timeout=0.2)
                    kinds.append(kind)
                    if kind == "job.finished":
                        self.assertIn(created["job_id"], data)
                        return
                except queue.Empty:
                    continue
            self.fail(f"Expected job.finished SSE event, got {kinds}")
        finally:
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


class SseModuleTests(unittest.TestCase):
    def test_emit_operation_event_and_webhook_helpers(self):
        from pkg.state import sse

        subscriber = queue.Queue(maxsize=4)
        self.assertTrue(sse.register_event_subscriber(subscriber))
        try:
            sse.emit_operation_event("job.queued", {"job_id": "abc"})
            kind, data = subscriber.get(timeout=1)
            self.assertEqual(kind, "job.queued")
            self.assertIn("abc", data)
            self.assertEqual(sse.event_matches({"events": ["session.started"]}, {"type": "session.started"}), True)
            self.assertEqual(sse.public_webhook_configs({"settings": {"webhooks": [{"id": "w1", "secret": "s"}]}})[0]["secret_set"], True)
            with mock.patch.dict(os.environ, {"OPENBOX_SAFE_MODE": "1"}):
                self.assertIsNone(sse.get_webhook_dispatcher())
            sse._emit_webhook_failure("evt-1", "delivery failed")
            sse.publish_event("session.started", {"id": 1})
            sse.shutdown_webhooks()
        finally:
            sse.unregister_event_subscriber(subscriber)

    def test_session_event_broadcasts(self):
        from pkg.state import sse

        subscriber = queue.Queue(maxsize=4)
        self.assertTrue(sse.register_event_subscriber(subscriber))
        try:
            sse.session_event("session.started", "launch-1", "Quake", exit_code=0, seconds=12)
            kind, data = subscriber.get(timeout=1)
            self.assertEqual(kind, "session.started")
            self.assertIn("Quake", data)
        finally:
            sse.unregister_event_subscriber(subscriber)

    def test_webhook_delivery_paths(self):
        from pkg.state import sse

        class FakeDispatcher:
            def __init__(self):
                self.enqueued = []

            def enqueue(self, configs, envelope):
                self.enqueued.append((configs, envelope))
                return True

            def start(self):
                return None

            def shutdown(self, wait_seconds=2.0):
                return None

        fake = FakeDispatcher()
        with mock.patch.object(sse, "get_webhook_dispatcher", return_value=fake):
            event_id = sse._webhook_payload({"id": "evt-1", "type": "session.started"}, [{"enabled": True, "events": ["session.started"]}])
            self.assertEqual(event_id, "evt-1")
        sse._commit_webhook_result("w1", "evt-2", 500, "bad gateway", "2026-01-01T00:00:00Z", True)
        sse.emit_notification(kind="system", title="t", body="b")
        sse.broadcast_event("bad", object())
        self.assertEqual(sse.webhook_configs({"settings": {"webhooks": "bad"}}), [])
        sse._close_sse_subscriber(queue.Queue(maxsize=1))
        sse._publish_session_event({"type": "session.started", "data": {"id": 1}})
        with mock.patch("pkg.state.sse.build_event", side_effect=ValueError("bad event")):
            self.assertEqual(sse.publish_event("session.started", {}), "")
        with mock.patch.object(sse, "get_webhook_dispatcher", return_value=None):
            self.assertEqual(sse._webhook_payload({"id": "evt-3", "type": "x"}, []), "evt-3")
        with mock.patch("pkg.state.cache.transact_state") as transact:
            transact.side_effect = lambda mutate: (mutate({"settings": {"webhooks": [{"id": "w1"}]}}), True)
            sse._commit_webhook_result("w1", "evt-4", 200, "", "2026-01-01T00:00:00Z", False)
        with mock.patch("pkg.state.cache.transact_state", side_effect=RuntimeError("db fail")):
            sse._commit_webhook_result("w1", "evt-4b", 500, "bad", "2026-01-01T00:00:00Z", True)
        broken = queue.Queue(maxsize=1)
        broken.put("stale")
        sse._close_sse_subscriber(broken)

    def test_ns_fallback_and_notification_errors(self):
        from pkg.state import sse

        self.assertEqual(sse._ns("missing_attr", "fallback"), "fallback")
        import webapp_state
        self.assertIs(sse._ns("load_state", None), webapp_state.load_state)
        with mock.patch("pkg.state.cache.transact_state", side_effect=RuntimeError("notify fail")):
            sse.emit_notification(title="t", body="b")
        with mock.patch.object(sse, "emit_notification", None):
            sse._emit_webhook_failure("evt-x", "delivery failed")
        with mock.patch.object(sse, "_ns", return_value=None):
            sse._emit_webhook_failure("evt-none", "delivery failed")
        with mock.patch.object(sse, "emit_notification", side_effect=RuntimeError("emit fail")):
            sse._emit_webhook_failure("evt-y", "delivery failed")

    def test_webhook_queue_full_and_publish_errors(self):
        from pkg.state import sse

        class FullDispatcher:
            def enqueue(self, configs, envelope):
                return False

        class BrokenDispatcher:
            def enqueue(self, configs, envelope):
                raise RuntimeError("enqueue failed")

        with mock.patch.object(sse, "get_webhook_dispatcher", return_value=FullDispatcher()):
            sse._webhook_payload({"id": "evt-full", "type": "session.started"}, [{"enabled": True, "events": ["session.started"]}])
        with mock.patch.object(sse, "get_webhook_dispatcher", return_value=BrokenDispatcher()):
            sse._webhook_payload({"id": "evt-broken", "type": "session.started"}, [{"enabled": True, "events": ["session.started"]}])
        with mock.patch.object(sse, "webhook_configs", side_effect=RuntimeError("load fail")):
            event_id = sse.publish_event("session.started", {"id": 1})
            self.assertTrue(event_id)
        dispatcher = mock.Mock(shutdown=mock.Mock(side_effect=RuntimeError("shutdown")))
        with mock.patch.object(sse, "get_webhook_dispatcher", return_value=dispatcher):
            sse.WEBHOOK_DISPATCHER = dispatcher
            sse.shutdown_webhooks()
        with mock.patch.object(sse, "publish_event", side_effect=RuntimeError("publish fail")):
            sse._publish_session_event({"type": "session.started", "data": {"id": 1}})

    def test_commit_webhook_result_branches(self):
        from pkg.state import sse

        with mock.patch("pkg.state.cache.transact_state") as transact:
            transact.side_effect = lambda mutate: (mutate({
                "settings": {"webhooks": ["bad", {"id": "w1", "enabled": True}]},
            }), True)
            sse._commit_webhook_result("w1", "evt-term", 500, "bad gateway", "2026-01-01T00:00:00Z", True)
        with mock.patch("pkg.state.cache.transact_state") as transact:
            transact.side_effect = lambda mutate: (mutate({
                "settings": {"webhooks": [{"id": "w1", "enabled": True}]},
            }), True)
            sse._commit_webhook_result("w1", "evt-ok", 200, "", "2026-01-01T00:00:00Z", True)

    def test_close_sse_subscriber_os_errors(self):
        from pkg.state import sse

        class BrokenDrainQueue:
            def get_nowait(self):
                raise OSError("broken")

            def put_nowait(self, _item):
                raise ValueError("broken")

        sse._close_sse_subscriber(BrokenDrainQueue())

        class BrokenPutQueue:
            def get_nowait(self):
                raise queue.Empty

            def put_nowait(self, _item):
                raise OSError("broken put")

        sse._close_sse_subscriber(BrokenPutQueue())

    def test_broadcast_subscriber_exception_is_dropped(self):
        from pkg.state import sse

        class BadQueue:
            def put_nowait(self, _item):
                raise RuntimeError("queue broke")

        subscriber = BadQueue()
        sse.register_event_subscriber(subscriber)
        try:
            sse.broadcast_event("session.started", {"id": 1})
        finally:
            sse.unregister_event_subscriber(subscriber)


if __name__ == "__main__":
    unittest.main()

