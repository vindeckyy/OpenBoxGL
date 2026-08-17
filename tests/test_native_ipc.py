"""Native host IPC route contract tests."""

import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock


class NativeIpcTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls._prev_data_dir = os.environ.get("OPENBOX_DATA_DIR")
        os.environ["OPENBOX_DATA_DIR"] = cls.tempdir.name
        import web_app

        cls.web_app = web_app
        web_app.TOKEN = "native-test-token"
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

    def request(self, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.origin + path,
            data=data,
            method=method,
            headers={"X-OpenBox-Token": "native-test-token", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as response:
            return response.status, json.loads(response.read().decode())

    def test_capabilities_reports_no_host(self):
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("OPENBOX_NATIVE_HOST", None)
            status, payload = self.request("/api/native/capabilities")
        self.assertEqual(status, 200)
        self.assertFalse(payload["dialogs"])
        self.assertFalse(payload["webview"])
        self.assertFalse(payload["tray"])
        self.assertFalse(payload["single_instance"])
        self.assertEqual(payload["gamepad"], "webkit")

    def test_capabilities_reports_host_present(self):
        with mock.patch.dict(os.environ, {"OPENBOX_NATIVE_HOST": "1"}):
            status, payload = self.request("/api/native/capabilities")
        self.assertEqual(status, 200)
        self.assertTrue(payload["webview"])
        self.assertTrue(payload["dialogs"])
        self.assertTrue(payload["tray"])
        self.assertTrue(payload["single_instance"])
        self.assertEqual(payload["gamepad"], "webkit")
        self.assertTrue(payload["fullscreen"])
        self.assertTrue(payload["clipboard"])

    def test_capabilities_requires_auth(self):
        req = urllib.request.Request(self.origin + "/api/native/capabilities")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 403)

    def test_dialog_without_host_is_cancelled(self):
        status, payload = self.request("/api/native/dialog", method="POST", body={"kind": "folder"})
        self.assertEqual(status, 200)
        self.assertIsNone(payload["path"])
        self.assertTrue(payload["cancelled"])

    def test_dialog_rejects_bad_kind(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("/api/native/dialog", method="POST", body={"kind": "nope"})
        self.assertEqual(ctx.exception.code, 400)

    def test_open_external_without_host_is_not_ok(self):
        status, payload = self.request("/api/native/open-external", method="POST", body={"url": "https://example.com"})
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])

    def test_window_without_host_is_not_ok(self):
        status, payload = self.request("/api/native/window", method="POST", body={"action": "minimize"})
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])


if __name__ == "__main__":
    unittest.main()
