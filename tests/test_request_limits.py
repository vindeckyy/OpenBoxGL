#!/usr/bin/env python3
"""P2-15: bounded connection threads and a total request deadline."""

import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("OPENBOX_DATA_DIR", tempfile.mkdtemp(prefix="openbox-limits-"))

import web_app  # noqa: E402


class ConnectionBudgetTests(unittest.TestCase):
    def _server(self, slots=1):
        server = web_app.BoundedThreadingHTTPServer.__new__(web_app.BoundedThreadingHTTPServer)
        server._connection_slots = __import__("threading").BoundedSemaphore(slots)
        return server

    def test_bounded_server_is_module_alias(self):
        self.assertIs(web_app.ThreadingHTTPServer, web_app.BoundedThreadingHTTPServer)

    def test_slot_is_released_when_spawning_fails(self):
        import threading
        from http.server import ThreadingHTTPServer

        server = self._server()
        with mock.patch.object(ThreadingHTTPServer, "process_request", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                server.process_request(object(), ("127.0.0.1", 1234))
        # A leaked slot would block (False); a released one acquires.
        self.assertTrue(server._connection_slots.acquire(blocking=False))
        self.assertIsInstance(server._connection_slots, threading.BoundedSemaphore)


class RequestDeadlineTests(unittest.TestCase):
    def _handler(self):
        handler = object.__new__(web_app.Handler)
        handler._request_deadline_timer = None
        return handler

    def test_deadline_timer_arms_and_is_cancelled(self):
        handler = self._handler()
        handler.REQUEST_DEADLINE = 60
        timer = mock.Mock()
        with mock.patch.object(web_app.threading, "Timer", return_value=timer) as timer_factory:
            handler._arm_request_deadline()
        timer_factory.assert_called_once()
        timer.start.assert_called_once()
        self.assertIs(handler._request_deadline_timer, timer)
        handler._cancel_request_deadline()
        timer.cancel.assert_called_once()
        self.assertIsNone(handler._request_deadline_timer)

    def test_sse_requests_opt_out_of_the_total_deadline(self):
        handler = self._handler()
        handler._request_deadline_disabled = True
        handler.REQUEST_DEADLINE = 60
        with mock.patch.object(web_app.threading, "Timer") as timer_factory:
            handler._arm_request_deadline()
        timer_factory.assert_not_called()

    def test_expired_deadline_shuts_the_connection_down(self):
        handler = self._handler()
        handler.connection = mock.Mock()
        handler.close_connection = False
        handler._request_deadline_expired()
        self.assertTrue(handler.close_connection)
        handler.connection.shutdown.assert_called_once()
        self.assertGreater(web_app.Handler.REQUEST_DEADLINE, 0)


if __name__ == "__main__":
    unittest.main()
