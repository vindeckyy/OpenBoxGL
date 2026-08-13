"""Cache-header and media-epoch regression tests for the web server.

Covers the Phase 1 HTTP caching work: immutable cache headers on media,
conditional GET (304) handling, revalidating theme.css, no-store JSON APIs,
and the media-epoch cache-busting counter.
"""

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _parse_response(raw):
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = lines[0].split(" ", 2)[1]
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    return status, headers, body


class PerfCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls._prev_data_dir = os.environ.get("OPENBOX_DATA_DIR")
        os.environ["OPENBOX_DATA_DIR"] = cls.tempdir.name
        from openbox import save_state
        from web_app import Handler, MEDIA_EPOCH

        save_state({"games": [], "profiles": {}, "history": [], "settings": {}, "playlists": []})
        cls.Handler = Handler
        cls.MEDIA_EPOCH = MEDIA_EPOCH
        cls.save_state = staticmethod(save_state)

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()
        if cls._prev_data_dir is None:
            os.environ.pop("OPENBOX_DATA_DIR", None)
        else:
            os.environ["OPENBOX_DATA_DIR"] = cls._prev_data_dir

    def setUp(self):
        self.MEDIA_EPOCH["value"] = 0
        self.media_path = Path(self.tempdir.name) / "media" / "cover.png"
        self.media_path.parent.mkdir(parents=True, exist_ok=True)
        self.media_path.write_bytes(b"fake-png-bytes")

    def make_handler(self):
        handler = object.__new__(self.Handler)
        handler.wfile = io.BytesIO()
        handler._headers_buffer = []
        handler.headers = {}
        handler.request_version = "HTTP/1.1"
        handler.log_request = lambda *args, **kwargs: None
        return handler

    def send_file(self, path=None, extra_headers=None):
        handler = self.make_handler()
        for key, value in (extra_headers or {}).items():
            handler.headers[key] = value
        handler.send_file(200, path or self.media_path)
        return _parse_response(handler.wfile.getvalue())

    def test_media_has_cache_headers(self):
        status, headers, body = self.send_file()
        self.assertEqual(status, "200")
        self.assertEqual(body, b"fake-png-bytes")
        self.assertIn("immutable", headers.get("cache-control", ""))
        self.assertTrue(headers.get("etag"))
        self.assertTrue(headers.get("last-modified"))
        self.assertEqual(headers.get("accept-ranges"), "bytes")

    def test_media_conditional_etag_304(self):
        _, headers, _ = self.send_file()
        etag = headers["etag"]
        status, headers, body = self.send_file(extra_headers={"If-None-Match": etag})
        self.assertEqual(status, "304")
        self.assertEqual(body, b"")
        self.assertIn("immutable", headers.get("cache-control", ""))

    def test_media_if_modified_since_304(self):
        _, headers, _ = self.send_file()
        status, _, body = self.send_file(extra_headers={"If-Modified-Since": headers["last-modified"]})
        self.assertEqual(status, "304")
        self.assertEqual(body, b"")

    def test_media_range_keeps_cache_headers(self):
        status, headers, body = self.send_file(extra_headers={"Range": "bytes=0-4"})
        self.assertEqual(status, "206")
        self.assertEqual(body, b"fake-")
        self.assertEqual(headers.get("content-range"), "bytes 0-4/14")
        self.assertTrue(headers.get("etag"))
        self.assertIn("immutable", headers.get("cache-control", ""))

    def test_json_stays_no_store(self):
        from web_app import Handler

        handler = self.make_handler()
        handler.send_json = mock.Mock()
        handler.authorized = mock.Mock(return_value=True)
        handler.do_GET = Handler.do_GET.__get__(handler, Handler)
        handler.path = "/api/settings?token=test"
        handler.do_GET()
        status, payload = handler.send_json.call_args[0]
        self.assertEqual(status, 200)

    def test_theme_css_revalidates(self):
        from web_app import Handler

        theme_dir = Path(self.tempdir.name) / "themes"
        theme_dir.mkdir(parents=True, exist_ok=True)
        (theme_dir / "test-theme.css").write_text("body { color: red; }")
        handler = self.make_handler()
        handler.authorized = mock.Mock(return_value=True)
        handler.do_GET = Handler.do_GET.__get__(handler, Handler)
        handler.path = "/api/theme.css?name=test-theme&token=test"
        handler.do_GET()
        status, headers, body = _parse_response(handler.wfile.getvalue())
        self.assertEqual(status, "200")
        self.assertEqual(headers.get("cache-control"), "public, max-age=0, must-revalidate")
        self.assertTrue(headers.get("etag"))
        self.assertEqual(body, b"body { color: red; }")

        etag = headers["etag"]
        handler = self.make_handler()
        handler.headers["If-None-Match"] = etag
        handler.authorized = mock.Mock(return_value=True)
        handler.do_GET = Handler.do_GET.__get__(handler, Handler)
        handler.path = "/api/theme.css?name=test-theme&token=test"
        handler.do_GET()
        status, headers, body = _parse_response(handler.wfile.getvalue())
        self.assertEqual(status, "304")
        self.assertEqual(body, b"")

    def test_media_epoch_bumps_on_download(self):
        from web_app import bump_media_epoch, download_image

        self.assertEqual(self.MEDIA_EPOCH["value"], 0)
        bump_media_epoch()
        self.assertEqual(self.MEDIA_EPOCH["value"], 1)
        with mock.patch("web_app.download_file", return_value="/tmp/fake.png") as downloader:
            download_image("https://example.com/x.png", Path(self.tempdir.name) / "x.png")
        downloader.assert_called_once()
        self.assertEqual(self.MEDIA_EPOCH["value"], 2)

    def test_public_state_includes_media_epoch(self):
        from web_app import public_state

        state = public_state()
        self.assertEqual(state["media_epoch"], self.MEDIA_EPOCH["value"])
        self.assertEqual(state["games"], [])

    def test_delete_game_with_media_bumps_epoch(self):

        media = Path(self.tempdir.name) / "media" / "game" / "cover.png"
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"png")
        self.save_state({
            "games": [{"game_id": "g1", "name": "Alpha", "path": "/bin/true", "cover": str(media)}],
            "profiles": {}, "history": [], "settings": {}, "playlists": [],
        })
        handler = self.make_handler()
        handler.send_json = mock.Mock()
        handler.delete_game({"id": 0, "delete_media": True})
        status, payload = handler.send_json.call_args[0]
        self.assertEqual(status, 200)
        self.assertEqual(payload["removed"], "Alpha")
        self.assertEqual(self.MEDIA_EPOCH["value"], 1)
        self.assertFalse(media.exists())

    def test_delete_game_without_media_does_not_bump(self):

        self.save_state({
            "games": [{"game_id": "g1", "name": "Alpha", "path": "/bin/true"}],
            "profiles": {}, "history": [], "settings": {}, "playlists": [],
        })
        handler = self.make_handler()
        handler.send_json = mock.Mock()
        handler.delete_game({"id": 0, "delete_media": False})
        handler.send_json.assert_called_once()
        self.assertEqual(self.MEDIA_EPOCH["value"], 0)


if __name__ == "__main__":
    unittest.main()
