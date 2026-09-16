#!/usr/bin/env python3
"""P1-19: only intentional client errors map to 400.

The dispatcher used to flatten KeyError/AttributeError/RuntimeError/IndexError/
OSError into 400 BAD_REQUEST, hiding real server bugs. These tests pin the new
taxonomy:

* ApiError subclasses keep their own status/code.
* StateCorruptError keeps the 503 STATE_UNAVAILABLE recovery contract.
* ValueError (validation) maps to 400 BAD_REQUEST with a request id.
* Everything else reaches _handle_request's 500 INTERNAL_ERROR, logged with a
  traceback and a request id.
"""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class ErrorTaxonomyTests(unittest.TestCase):
    def _handler(self, *, headers=None, body=b""):
        import web_app

        h = object.__new__(web_app.Handler)
        h.path = "/api/taxonomy"
        h.command = "GET"
        h.headers = {"Host": "127.0.0.1", **(headers or {})}
        h.rfile = io.BytesIO(body)
        h.wfile = io.BytesIO()
        h.send_json = mock.Mock()
        h.do_GET = web_app.Handler.do_GET.__get__(h, web_app.Handler)
        h.do_POST = web_app.Handler.do_POST.__get__(h, web_app.Handler)
        return h

    @staticmethod
    def _response(handler):
        status, payload = handler.send_json.call_args[0]
        return status, payload

    def test_value_error_is_bad_request(self):
        h = self._handler()
        with mock.patch("web_app.dispatch_get", side_effect=ValueError("bad filter")):
            h.do_GET()
        status, payload = self._response(h)
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "BAD_REQUEST")
        self.assertIn("bad filter", payload["error"])
        self.assertTrue(payload["request_id"])

    def test_api_error_keeps_its_status(self):
        from api_errors import NotFound

        h = self._handler()
        with mock.patch("web_app.dispatch_get", side_effect=NotFound("nope")):
            h.do_GET()
        status, payload = self._response(h)
        self.assertEqual(status, 404)
        self.assertEqual(payload["code"], "NOT_FOUND")

    def test_state_corrupt_is_503(self):
        from state_store import StateCorruptError

        h = self._handler()
        with mock.patch("web_app.dispatch_get", side_effect=StateCorruptError("corrupt")):
            h.do_GET()
        status, payload = self._response(h)
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "STATE_UNAVAILABLE")
        self.assertTrue(payload["request_id"])

    def test_server_bugs_are_internal_errors(self):
        failures = [
            KeyError("missing key"),
            AttributeError("missing attribute"),
            RuntimeError("unexpected runtime"),
            IndexError("index out of range"),
            OSError("disk exploded"),
        ]
        for error in failures:
            with self.subTest(error=type(error).__name__):
                h = self._handler()
                with (
                    mock.patch("web_app.dispatch_get", side_effect=error),
                    self.assertLogs("openbox", level="ERROR") as captured,
                ):
                    h.do_GET()
                status, payload = self._response(h)
                self.assertEqual(status, 500)
                self.assertEqual(payload["code"], "INTERNAL_ERROR")
                self.assertTrue(payload["request_id"])
                self.assertTrue(
                    any(type(error).__name__ in line for line in captured.output),
                    captured.output,
                )

    def test_malformed_json_body_is_bad_request(self):
        h = self._handler(headers={"Content-Length": "8"}, body=b"not-json")
        with mock.patch("web_app.dispatch_post") as dispatch:
            h.do_POST()
        status, payload = self._response(h)
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "BAD_REQUEST")
        dispatch.assert_not_called()

    def test_non_object_body_is_bad_request(self):
        h = self._handler(headers={"Content-Length": "2"}, body=b"[]")
        with mock.patch("web_app.dispatch_post") as dispatch:
            h.do_POST()
        status, payload = self._response(h)
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "BAD_REQUEST")
        dispatch.assert_not_called()

    def test_post_server_bug_is_internal_error(self):
        h = self._handler(headers={"Content-Length": "2"}, body=b"{}")
        with (
            mock.patch("web_app.dispatch_post", side_effect=KeyError("payload key")),
            self.assertLogs("openbox", level="ERROR"),
        ):
            h.do_POST()
        status, payload = self._response(h)
        self.assertEqual(status, 500)
        self.assertEqual(payload["code"], "INTERNAL_ERROR")
        self.assertTrue(payload["request_id"])

    def test_post_state_corrupt_is_503(self):
        from state_store import StateCorruptError

        h = self._handler(headers={"Content-Length": "2"}, body=b"{}")
        with mock.patch("web_app.dispatch_post", side_effect=StateCorruptError("corrupt")):
            h.do_POST()
        status, payload = self._response(h)
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "STATE_UNAVAILABLE")
        self.assertTrue(payload["request_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
