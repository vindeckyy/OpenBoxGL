#!/usr/bin/env python3
"""Native handler contract tests for handlers/native.py allowlists."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api_errors import BadRequest  # noqa: E402
from handlers.native import battery, capabilities, dialog, open_external, reveal, window  # noqa: E402
from pkg import platform_compat as pc  # noqa: E402
from pkg.platform_compat import battery_status  # noqa: E402

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

    def handle_unauthorized(self):
        self.status = 403
        self.payload = {"error": "Unauthorized"}


class TestNativeHandlers(unittest.TestCase):
    def test_battery_from_sysfs(self):
        if pc.IS_MACOS:
            self.skipTest("the sysfs battery reader is Linux-only; macOS reads pmset")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "BAT0").mkdir()
            (root / "BAT0" / "capacity").write_text("77\n", encoding="utf-8")
            (root / "BAT0" / "status").write_text("Charging\n", encoding="utf-8")
            self.assertEqual(battery_status(root), {"present": True, "percent": 77, "charging": True})
            (root / "BAT0" / "capacity").write_text("150", encoding="utf-8")
            (root / "BAT0" / "status").write_text("Discharging", encoding="utf-8")
            self.assertEqual(battery_status(root), {"present": True, "percent": 100, "charging": False})
        self.assertFalse(battery_status(Path("/nonexistent-openbox-battery"))["present"])

    def test_battery_route_authorized_and_unauthorized(self):
        h = DummyHandler(authorized=True)
        with mock.patch("handlers.native.battery_status", return_value={"present": False, "percent": 0, "charging": False}):
            battery(h, mock.Mock())
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload, {"present": False, "percent": 0, "charging": False})
        h = DummyHandler(authorized=False)
        battery(h, mock.Mock())
        self.assertEqual(h.status, 403)

    def test_battery_route_serves_the_windows_reader(self):
        h = DummyHandler(authorized=True)
        with mock.patch.object(pc, "IS_WINDOWS", True), \
             mock.patch.object(pc, "IS_MACOS", False), \
             mock.patch.object(pc, "_battery_cache", None), \
             mock.patch.object(
                 pc, "_windows_battery_status",
                 return_value={"present": True, "percent": 55, "charging": False},
             ):
            battery(h, mock.Mock())
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload, {"present": True, "percent": 55, "charging": False})

    def test_dialog_start_path_must_be_absolute(self):
        h = DummyHandler()
        dialog(h, {"kind": "folder", "start_path": "/tmp"})
        self.assertEqual(h.status, 200)
        with self.assertRaises(BadRequest):
            dialog(h, {"kind": "folder", "start_path": "relative/path"})

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
        self.assertEqual(h.payload["features"], pc.platform_features())

    def test_capabilities_features_are_false_without_a_payload(self):
        # An old or failing host must hide every gated control, not show a
        # control that fails on click (ADR 0086).
        h = DummyHandler(authorized=True)
        with mock.patch("handlers.native.platform_features", return_value={}):
            capabilities(h, mock.Mock())
        self.assertEqual(h.status, 200)
        self.assertEqual(h.payload["features"], {})

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

    def test_post_handlers_unauthorized(self):
        for fn, payload in [
            (dialog, {"kind": "folder"}),
            (open_external, {"url": "https://example.com"}),
            (reveal, {"path": "/tmp/test"}),
            (window, {"action": "minimize"}),
        ]:
            h = DummyHandler(authorized=False)
            fn(h, payload)
            self.assertEqual(h.status, 403)
            self.assertEqual(h.payload.get("error"), "Unauthorized")

    def test_unauthorized_fallback_without_handle_unauthorized_method(self):
        class MinimalHandler:
            def __init__(self):
                self.status = None
                self.payload = None

            def authorized(self):
                return False

            def send_json(self, status, payload):
                self.status = status
                self.payload = payload

        h = MinimalHandler()
        dialog(h, {"kind": "folder"})
        self.assertEqual(h.status, 403)
        self.assertEqual(h.payload.get("error"), "Unauthorized")

if __name__ == "__main__":
    unittest.main()
