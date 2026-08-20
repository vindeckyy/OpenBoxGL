#!/usr/bin/env python3
"""Native handler contract tests for handlers/native.py allowlists."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api_errors import BadRequest  # noqa: E402
from handlers.native import capabilities, dialog, open_external, reveal, window  # noqa: E402

class DummyHandler:
    def __init__(self, authorized=True):
        self._authorized = authorized
        self.status = None
        self.payload = None
        self.headers = {}

    def authorized(self):
        return self._authorized

    def send_json(self, status, payload):
        self.status = status
        self.payload = payload

    def send_error(self, status, msg=""):
        self.status = status
        self.payload = {"error": msg}


class TestNativeHandlers(unittest.TestCase):
    def test_capabilities_unauthorized(self):
        h = DummyHandler(authorized=False)
        capabilities(h, mock.Mock())
        self.assertEqual(h.status, 403)
        self.assertIn("error", h.payload)

    def test_capabilities_authorized(self):
        h = DummyHandler(authorized=True)
        with mock.patch.dict("os.environ", {}, clear=False):
            if "OPENBOX_NATIVE_HOST" in __import__("os").environ:
                del __import__("os").environ["OPENBOX_NATIVE_HOST"]
            capabilities(h, mock.Mock())
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload["gamepad"], "webkit")
        self.assertTrue(h.payload["fullscreen"])
        self.assertFalse(h.payload["webview"])

    def test_capabilities_host_attached(self):
        h = DummyHandler(authorized=True)
        with mock.patch.dict("os.environ", {"OPENBOX_NATIVE_HOST": "1"}):
            capabilities(h, mock.Mock())
        self.assertEqual(h.status, 200)
        self.assertTrue(h.payload["webview"])

    def test_dialog_allowlist(self):
        for kind in ("folder", "file", "save"):
            h = DummyHandler()
            dialog(h, {"kind": kind})
            self.assertEqual(h.status, 200)
            self.assertTrue(h.payload["cancelled"])
        h = DummyHandler()
        with self.assertRaises(BadRequest) as ctx:
            dialog(h, {"kind": "bad"})
        self.assertIn("Native dialog kind", str(ctx.exception))

    def test_open_external_validation(self):
        h = DummyHandler()
        with self.assertRaises(BadRequest):
            open_external(h, {})
        with self.assertRaises(BadRequest):
            open_external(h, {"path": "   "})
        h = DummyHandler()
        open_external(h, {"path": "/tmp/file"})
        self.assertEqual(h.status, 200)
        self.assertFalse(h.payload["ok"])
        h = DummyHandler()
        open_external(h, {"url": "https://example.com"})
        self.assertEqual(h.status, 200)

    def test_reveal_validation(self):
        h = DummyHandler()
        with self.assertRaises(BadRequest):
            reveal(h, {})
        with self.assertRaises(BadRequest):
            reveal(h, {"path": ""})
        h = DummyHandler()
        reveal(h, {"path": "/tmp/file"})
        self.assertEqual(h.status, 200)

    def test_window_allowlist(self):
        for action in ("minimize", "toggle-maximize", "close", "set-fullscreen", "unset-fullscreen"):
            h = DummyHandler()
            window(h, {"action": action})
            self.assertEqual(h.status, 200)
            self.assertFalse(h.payload["ok"])
        h = DummyHandler()
        with self.assertRaises(BadRequest):
            window(h, {"action": "bad"})
        with self.assertRaises(BadRequest):
            window(h, {})
if __name__ == "__main__":
    unittest.main()
