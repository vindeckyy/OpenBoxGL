#!/usr/bin/env python3
"""P1-21: export downloads stream from disk instead of read_bytes().

The download route used to read the whole export into memory before writing.
It now goes through send_file, so a multi-MB export arrives in chunks while
Content-Type, Content-Disposition, and ETag stay intact.
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity.parity_export import EXPORT_DIR_NAME  # noqa: E402


class _RecordingWriter(io.BytesIO):
    def __init__(self):
        super().__init__()
        self.writes = []

    def write(self, data):
        self.writes.append(bytes(data))
        return super().write(data)


class ExportStreamingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.exports = self.data_dir / EXPORT_DIR_NAME
        self.exports.mkdir()
        self.payload = b"x" * (2 * 1024 * 1024 + 123)
        self.export = self.exports / "openbox-library-20260101-120000.json"
        self.export.write_bytes(self.payload)

    def _handler(self, headers=None):
        import web_app

        h = object.__new__(web_app.Handler)
        h.headers = dict(headers or {})
        h.sent_headers = []
        h.wfile = _RecordingWriter()
        h.send_response = lambda status: h.sent_headers.append(("status", status))
        h.headers_common = lambda content_type, **kw: h.sent_headers.append(("content-type", content_type))
        h.send_header = lambda name, value: h.sent_headers.append((name, value))
        h.end_headers = lambda: None
        return h

    def _download(self, handler, export_name):
        import handlers.export as export_module

        original = export_module.DATA
        export_module.DATA = self.data_dir / "library.json"
        try:
            parsed = urlparse(f"/api/v2/library/export/download?file={export_name}")
            handler._api_get_api_v2_library_export_download(parsed)
        finally:
            export_module.DATA = original
        return handler

    @staticmethod
    def _header(handler, name):
        return [value for key, value in handler.sent_headers if key == name]

    def test_download_streams_without_read_bytes(self):
        h = self._handler()
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file read")):
            self._download(h, self.export.name)
        self.assertGreater(len(h.wfile.writes), 1, "multi-MB export must be written in chunks")
        self.assertEqual(b"".join(h.wfile.writes), self.payload)
        self.assertEqual(self._header(h, "Content-Length"), [str(len(self.payload))])
        self.assertEqual(
            self._header(h, "Content-Disposition"),
            [f'attachment; filename="{self.export.name}"'],
        )
        self.assertEqual(self._header(h, "content-type"), ["application/json"])
        self.assertEqual(len(self._header(h, "ETag")), 1)

    def test_csv_keeps_content_type(self):
        csv_path = self.exports / "openbox-library-20260101-120001.csv"
        csv_path.write_bytes(b"name\r\nAlpha\r\n")
        h = self._download(self._handler(), csv_path.name)
        self.assertEqual(self._header(h, "content-type"), ["text/csv; charset=utf-8"])
        self.assertEqual(b"".join(h.wfile.writes), b"name\r\nAlpha\r\n")

    def test_matching_etag_returns_304_without_reading(self):
        first = self._download(self._handler(), self.export.name)
        etag = self._header(first, "ETag")[0]
        second = self._handler({"If-None-Match": etag})
        self._download(second, self.export.name)
        self.assertIn(("status", 304), second.sent_headers)
        self.assertEqual(second.wfile.writes, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
