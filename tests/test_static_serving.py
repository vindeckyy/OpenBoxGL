#!/usr/bin/env python3
"""Regression pins for static-asset HTTP caching (plan row 15).

Row 4: frontend modules must be served `no-cache` (revalidated, never
stale) while the logo is intentionally `immutable` (year-long cache).
"""
from __future__ import annotations

import http.client
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class StaticServingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls._prev_data_dir = os.environ.get("OPENBOX_DATA_DIR")
        os.environ["OPENBOX_DATA_DIR"] = cls.tempdir.name
        Path(cls.tempdir.name, "library.json").write_text("{}", encoding="utf-8")
        import web_app

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
        if cls._prev_data_dir is None:
            os.environ.pop("OPENBOX_DATA_DIR", None)
        else:
            os.environ["OPENBOX_DATA_DIR"] = cls._prev_data_dir

    def get(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", path, headers=headers or {})
            resp = conn.getresponse()
            resp.read()
            return resp.status, dict(resp.getheaders())
        finally:
            conn.close()

    def test_app_js_no_cache_with_etag(self):
        # Row 4: JS/CSS must be served no-cache (revalidated) so clients
        # never run stale modules for a year.
        status, headers = self.get("/static/app.js")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-cache")
        etag = headers.get("ETag")
        self.assertTrue(etag, "expected an ETag on /static/app.js")

    def test_app_js_conditional_returns_304(self):
        _status, headers = self.get("/static/app.js")
        etag = headers.get("ETag")
        self.assertTrue(etag)
        status, _ = self.get("/static/app.js", {"If-None-Match": etag})
        self.assertEqual(status, 304)

    def test_logo_immutable_is_intentional(self):
        # The logo is cache-busted by filename/content, so a year-long
        # immutable cache is deliberate — do not "fix" this to no-cache.
        status, headers = self.get("/static/logo.png")
        self.assertEqual(status, 200)
        cache_control = headers.get("Cache-Control", "")
        self.assertIn("immutable", cache_control)
        self.assertIn("max-age=31536000", cache_control)
        etag = headers.get("ETag")
        self.assertTrue(etag, "expected an ETag on /static/logo.png")
        status, _ = self.get("/static/logo.png", {"If-None-Match": etag})
        self.assertEqual(status, 304)

    def test_unknown_static_rejected(self):
        status, _ = self.get("/static/notes.txt")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
