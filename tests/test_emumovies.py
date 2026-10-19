#!/usr/bin/env python3
"""EmuMovies provider tests (P8.3)."""

import email.message
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pkg" / "parity"))

import pkg.parity  # noqa: F401,E402  # register flat-import finder

from parity_emumovies import (
    KIND_MAP,
    destination_for,
    download,
    download_emumovies_media,
    extension_for,
    reset_rate_limit,
    search,
)

CREDENTIALS = {"username": "user", "password": "pass"}


class Response(io.BytesIO):
    def __init__(self, body=b"", status=200, content_type="image/png"):
        super().__init__(body)
        self.status = status
        self.headers = email.message.Message()
        self.headers["Content-Type"] = content_type

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class EmuMoviesTests(unittest.TestCase):
    def setUp(self):
        reset_rate_limit()

    def tearDown(self):
        reset_rate_limit()

    def test_kind_map(self):
        self.assertEqual(KIND_MAP["screenshots"], "snap")
        self.assertEqual(KIND_MAP["video_snap"], "video")
        self.assertEqual(KIND_MAP["video_theme"], "video")
        self.assertEqual(KIND_MAP["music"], "music")
        self.assertEqual(KIND_MAP["cart_front"], "cart")
        self.assertEqual(KIND_MAP["cart_back"], "cartback")
        self.assertEqual(KIND_MAP["disc"], "disc")
        self.assertEqual(KIND_MAP["clear_logo"], "logo")

    def test_candidate_carries_game_metadata(self):
        def opener(request, timeout=0):
            return Response(content_type="image/png")

        candidate = search(
            {
                "name": "Game", "platform": "SNES", "publisher": "Publisher",
                "developer": "Developer", "genre": "RPG", "max_players": 2,
                "year": "1994", "region": "USA",
            },
            "cart_front",
            CREDENTIALS,
            opener=opener,
            sleep=lambda _seconds: None,
            clock=lambda: 0.0,
        )
        self.assertEqual(candidate["endpoint_kind"], "cart")
        self.assertEqual(candidate["publisher"], "Publisher")
        self.assertEqual(candidate["developer"], "Developer")
        self.assertEqual(candidate["genre"], "RPG")
        self.assertEqual(candidate["players"], "2")
        self.assertEqual(candidate["release_year"], "1994")
        self.assertEqual(candidate["region"], "USA")

    def test_4xx_is_permanent_without_retry(self):
        calls = []

        def opener(request, timeout=0):
            calls.append(request.full_url)
            return Response(b"nope", status=404)

        sleeps = []
        with self.assertRaises(ValueError) as caught:
            search({"name": "Game", "platform": "SNES"}, "cover", CREDENTIALS,
                   opener=opener, sleep=sleeps.append, clock=lambda: 0.0)
        self.assertIn("404", str(caught.exception))
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])

    def test_5xx_retries_twice_then_recovers(self):
        calls = []

        def opener(request, timeout=0):
            calls.append(request.full_url)
            if len(calls) < 3:
                return Response(status=503)
            return Response(content_type="image/png")

        sleeps = []
        candidate = search({"name": "Game", "platform": "SNES"}, "cover", CREDENTIALS,
                           opener=opener, sleep=sleeps.append, clock=lambda: 0.0)
        self.assertEqual(len(calls), 3)
        self.assertIn(2, sleeps)
        self.assertIn(8, sleeps)
        self.assertEqual(candidate["kind"], "cover")
        self.assertEqual(candidate["content_type"], "image/png")

    def test_5xx_exhausts_and_raises(self):
        calls = []

        def opener(request, timeout=0):
            calls.append(1)
            return Response(status=500)

        with self.assertRaises(ValueError):
            search({"name": "Game"}, "cover", CREDENTIALS,
                   opener=opener, sleep=lambda _seconds: None, clock=lambda: 0.0)
        self.assertEqual(len(calls), 3)

    def test_rate_limit_one_request_per_second(self):
        clock = [100.0]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        def opener(request, timeout=0):
            return Response(content_type="image/png")

        search({"name": "One"}, "cover", CREDENTIALS, opener=opener, sleep=sleep, clock=lambda: clock[0])
        search({"name": "Two"}, "cover", CREDENTIALS, opener=opener, sleep=sleep, clock=lambda: clock[0])
        self.assertTrue(any(abs(value - 1.0) < 1e-6 for value in sleeps), sleeps)

    def test_extension_from_content_type(self):
        self.assertEqual(extension_for("image/png", "x", "cover"), ".png")
        self.assertEqual(extension_for("video/webm", "x", "video_snap"), ".webm")
        self.assertEqual(extension_for("audio/ogg", "x", "music"), ".ogg")
        self.assertEqual(extension_for("application/pdf", "x", "manual"), ".pdf")
        self.assertEqual(extension_for("", "https://x/a.mp4", "video_snap"), ".mp4")
        self.assertEqual(extension_for("", "", "cover"), ".jpg")

    def test_destination_is_contained_and_slugged(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            candidate = {
                "kind": "cover",
                "platform": "../../etc",
                "name": "../Game: Deluxe",
                "content_type": "image/png",
                "url": "https://api.emumovies.com/v1/media/box?x=1",
            }
            destination = destination_for(candidate, root)
            resolved_root = root.resolve()
            self.assertEqual(destination.parent.parent.parent, resolved_root / "emumovies")
            self.assertNotIn("..", destination.parts)
            self.assertTrue(str(destination).endswith("cover.png"))

    def test_download_uses_shared_downloader_and_extension(self):
        captured = {}

        def fake_download(url, destination, **kwargs):
            captured["url"] = url
            captured["destination"] = Path(destination)
            captured["kwargs"] = kwargs
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            Path(destination).write_bytes(b"data")
            return destination

        candidate = {
            "kind": "video_snap",
            "platform": "SNES",
            "name": "Game",
            "content_type": "video/mp4",
            "url": "https://api.emumovies.com/v1/media/video?x=1",
            "headers": {"Authorization": "Basic dXNlcjpwYXNz"},
        }
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch("parity_emumovies.download_file", side_effect=fake_download):
            result = download(candidate, folder)
            self.assertEqual(Path(result), captured["destination"])
            self.assertTrue(result.endswith("video_snap.mp4"))
            self.assertEqual(captured["kwargs"]["expected_types"], ("video/",))
            self.assertEqual(captured["kwargs"]["max_bytes"], 512 * 1024 * 1024)
            self.assertEqual(captured["kwargs"]["headers"]["Authorization"], "Basic dXNlcjpwYXNz")

    def test_download_rejects_http_urls(self):
        candidate = {
            "kind": "cover",
            "platform": "SNES",
            "name": "Game",
            "url": "http://api.emumovies.com/v1/media/box",
        }
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                download(candidate, folder)

    def test_compatibility_entry_accepts_route_media_types(self):
        candidate = {"kind": "screenshots", "url": "https://x/y.png", "content_type": "image/png"}
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch("parity_emumovies.search", return_value=candidate) as search_mock, \
             mock.patch("parity_emumovies.download", return_value="/media/out.png") as download_mock:
            result = download_emumovies_media({"name": "Game"}, CREDENTIALS, folder, "snap")
        self.assertEqual(result, "/media/out.png")
        self.assertEqual(search_mock.call_args.args[1], "screenshots")
        download_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
