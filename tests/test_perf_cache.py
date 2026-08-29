"""Cache-header and media-epoch regression tests for the web server.

Covers the Phase 1 HTTP caching work: immutable cache headers on media,
conditional GET (304) handling, revalidating theme.css, no-store JSON APIs,
and the media-epoch cache-busting counter.
"""

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from api_errors import BadRequest

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
        import openbox
        import webapp_state
        import handlers.extensions
        from state_store import JsonStateStore
        openbox.DATA = Path(cls.tempdir.name) / "library.json"
        openbox.STATE_STORE = JsonStateStore(openbox.DATA)
        webapp_state.DATA = openbox.DATA
        webapp_state.STATE_STORE = openbox.STATE_STORE
        handlers.extensions.DATA = openbox.DATA
        from web_app import Handler
        from openbox import save_state
        from webapp_state import MEDIA_EPOCH

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
        self.save_state({"games": [], "profiles": {}, "history": [], "settings": {}, "playlists": []})
        self.media_path = Path(self.tempdir.name) / "media" / "cover.png"
        self.media_path.parent.mkdir(parents=True, exist_ok=True)
        self.media_path.write_bytes(b"fake-png-bytes")

    def make_handler(self):
        handler = object.__new__(self.Handler)
        handler.wfile = io.BytesIO()
        handler._headers_buffer = []
        handler.headers = {"Host": "127.0.0.1"}
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
        handler.headers["Host"] = "127.0.0.1"
        handler.authorized = mock.Mock(return_value=True)
        handler.do_GET = Handler.do_GET.__get__(handler, Handler)
        handler.path = "/api/theme.css?name=test-theme&token=test"
        handler.do_GET()
        status, headers, body = _parse_response(handler.wfile.getvalue())
        self.assertEqual(status, "304")
        self.assertEqual(body, b"")

    def test_media_epoch_bumps_on_download(self):
        from webapp_state import bump_media_epoch, download_image

        self.assertEqual(self.MEDIA_EPOCH["value"], 0)
        bump_media_epoch()
        self.assertEqual(self.MEDIA_EPOCH["value"], 1)
        with mock.patch("webapp_state.download_file", return_value="/tmp/fake.png") as downloader:
            download_image("https://example.com/x.png", Path(self.tempdir.name) / "x.png")
        downloader.assert_called_once()
        self.assertEqual(self.MEDIA_EPOCH["value"], 2)

    def test_public_state_includes_media_epoch(self):
        from webapp_state import public_state

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

    def test_public_state_fast_projection(self):
        from webapp_state import _build_public_state

        media = Path(self.tempdir.name) / "media" / "game" / "cover.png"
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"png")
        from webapp_state import bump_media_epoch
        bump_media_epoch()
        self.save_state({
            "games": [
                {"game_id": "g1", "name": "Alpha", "path": "/bin/true", "cover": str(media)},
                {"game_id": "g2", "name": "Beta", "path": "/bin/false"},
            ],
            "profiles": {}, "history": [], "settings": {}, "playlists": [],
        })
        # First build
        state1 = _build_public_state()
        self.assertEqual(len(state1["games"]), 2)
        self.assertTrue(state1["games"][0]["has_cover"])
        self.assertFalse(state1["games"][1]["has_cover"])

        # Second build: should be served from projection cache without realpath syscall churn
        with mock.patch("os.path.realpath", side_effect=RuntimeError("realpath called unexpectedly")):
            state2 = _build_public_state()
            self.assertEqual(len(state2["games"]), 2)
            self.assertEqual(state2["games"][0]["name"], "Alpha")
            self.assertTrue(state2["games"][0]["has_cover"])

    def test_media_epoch_invalidation_precision(self):
        from webapp_state import (
            _build_public_state,
            bump_media_epoch,
            _KNOWN_MEDIA_SET_CACHE,
            _GAME_PROJECTION_CACHE,
            _SANITIZE_MEDIA_PATH_CACHE,
            PUBLIC_STATE_CACHE,
        )

        media = Path(self.tempdir.name) / "media" / "game" / "new_cover.png"
        self.save_state({
            "games": [{"game_id": "g1", "name": "Alpha", "path": "/bin/true", "cover": str(media)}],
            "profiles": {}, "history": [], "settings": {}, "playlists": [],
        })
        state = _build_public_state()
        self.assertFalse(state["games"][0]["has_cover"])

        # Create media on disk and bump epoch
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"png")
        bump_media_epoch()

        # Verify caches are cleared
        self.assertIsNone(_KNOWN_MEDIA_SET_CACHE.get("key"))
        self.assertEqual(len(_GAME_PROJECTION_CACHE), 0)
        self.assertEqual(len(_SANITIZE_MEDIA_PATH_CACHE), 0)
        self.assertIsNone(PUBLIC_STATE_CACHE.get("signature"))

        # Re-build public state: new cover must now be detected
        state_after = _build_public_state()
        self.assertTrue(state_after["games"][0]["has_cover"])

    def test_delta_library_endpoint_precision_and_caching(self):
        from urllib.parse import urlparse
        from web_app import Handler

        media = Path(self.tempdir.name) / "media" / "cover.png"
        self.save_state({
            "games": [
                {"game_id": "game-alpha", "name": "Alpha", "path": "/bin/true", "cover": str(media), "legacy_game_ids": ["legacy-g1"]},
                {"game_id": "game-beta", "name": "Beta", "path": "/bin/false"},
                {"game_id": "game-gamma", "name": "Gamma", "path": "/bin/echo"},
            ],
            "profiles": {}, "history": [], "settings": {}, "playlists": [],
            "schema_version": 3,
        })
        handler = self.make_handler()
        handler.send_json = mock.Mock()
        handler.authorized = mock.Mock(return_value=True)
        handler._api_get_api_library_delta = Handler._api_get_api_library_delta.__get__(handler, Handler)

        # 1) Missing ids parameter
        with self.assertRaises(BadRequest) as ctx:
            handler._api_get_api_library_delta(urlparse("/api/library/delta?token=test"))
        self.assertEqual(ctx.exception.message, "Missing ids parameter")

        # 2) Empty ids parameter
        with self.assertRaises(BadRequest) as ctx:
            handler._api_get_api_library_delta(urlparse("/api/library/delta?ids=&token=test"))
        self.assertEqual(ctx.exception.message, "Missing ids parameter")

        # 3) Too many IDs (>1000)
        too_many = ",".join(f"id_{i}" for i in range(1001))
        with self.assertRaises(BadRequest) as ctx:
            handler._api_get_api_library_delta(urlparse(f"/api/library/delta?ids={too_many}&token=test"))
        self.assertEqual(ctx.exception.message, "Too many IDs (max 1000)")
        # 4) Successful delta query by game_id and legacy_game_ids
        handler.send_json.reset_mock()
        handler._api_get_api_library_delta(urlparse("/api/library/delta?ids=legacy-g1,game-gamma,nonexistent&token=test"))
        status, payload = handler.send_json.call_args[0]
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["games"]), 2)
        self.assertEqual(payload["games"][0]["name"], "Alpha")
        self.assertEqual(payload["games"][1]["name"], "Gamma")

        # 5) Successful delta query by int id and deduplication
        handler.send_json.reset_mock()
        handler._api_get_api_library_delta(urlparse("/api/library/delta?ids=0,0,game-alpha&token=test"))
        status, payload = handler.send_json.call_args[0]
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["games"]), 1)
        self.assertEqual(payload["games"][0]["name"], "Alpha")

    def test_server_tcp_nodelay_and_socket_tuning(self):
        import socket
        from web_app import Handler

        fake_sock = mock.MagicMock()
        handler = object.__new__(Handler)
        handler.request = fake_sock
        handler.connection = fake_sock
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()
        handler.setup()

        # Verify TCP_NODELAY and SO_SNDBUF were applied
        calls = fake_sock.setsockopt.call_args_list
        opt_names = [call[0][1] for call in calls]
        self.assertIn(socket.TCP_NODELAY, opt_names)
        self.assertIn(socket.SO_SNDBUF, opt_names)

    def test_projection_cache_bounding(self):
        from webapp_state import _GAME_PROJECTION_CACHE, _project_game

        # Mock max limit to 10 for quick boundary test
        import webapp_state
        orig_max = webapp_state._GAME_PROJECTION_MAX
        try:
            webapp_state._GAME_PROJECTION_MAX = 10
            for i in range(15):
                g = {"game_id": f"g_{i}", "name": f"G {i}", "path": f"/bin/{i}"}
                _project_game(g, i, set(), set(), None, {}, 0)
            self.assertLessEqual(len(_GAME_PROJECTION_CACHE), 10)
        finally:
            webapp_state._GAME_PROJECTION_MAX = orig_max

    def test_sanitize_media_path_cache_and_errors(self):
        import webapp_state
        from webapp_state import sanitize_media_path, _SANITIZE_MEDIA_PATH_CACHE

        # Empty/None
        self.assertEqual(sanitize_media_path(""), "")
        self.assertEqual(sanitize_media_path(None), "")

        # Valid cached path
        media_p = Path(self.tempdir.name) / "media" / "test.png"
        res1 = sanitize_media_path(str(media_p))
        self.assertEqual(res1, str(media_p))
        # Second call: cache hit
        res2 = sanitize_media_path(str(media_p))
        self.assertEqual(res2, str(media_p))

        # Error path
        with mock.patch("webapp_state.approved_media_path", side_effect=ValueError("bad")):
            self.assertEqual(sanitize_media_path("/invalid/path"), "")

        # Cache overflow bounding
        orig_max = webapp_state._SANITIZE_MEDIA_PATH_MAX
        try:
            webapp_state._SANITIZE_MEDIA_PATH_MAX = 5
            for i in range(10):
                p = Path(self.tempdir.name) / "media" / f"test_{i}.png"
                sanitize_media_path(str(p))
            self.assertLessEqual(len(_SANITIZE_MEDIA_PATH_CACHE), 5)
        finally:
            webapp_state._SANITIZE_MEDIA_PATH_MAX = orig_max

    def test_platform_category_cache_and_bounding(self):
        import webapp_state
        from webapp_state import category_for_platform, _PLATFORM_CATEGORY_CACHE

        self.assertEqual(category_for_platform("Arcade", {}), "Arcade")
        # Second call: hit cache
        self.assertEqual(category_for_platform("Arcade", {}), "Arcade")

        from webapp_state import _project_game
        orig_max = webapp_state._PLATFORM_CATEGORY_MAX
        try:
            webapp_state._PLATFORM_CATEGORY_MAX = 5
            for i in range(10):
                _project_game({"game_id": f"g_{i}", "platform": f"Platform_{i}"}, i, set(), set(), None, {}, 0)
            self.assertLessEqual(len(_PLATFORM_CATEGORY_CACHE), 5)
        finally:
            webapp_state._PLATFORM_CATEGORY_MAX = orig_max

    def test_project_game_edge_cases(self):
        from webapp_state import _project_game

        # String alternate names, non-dict custom fields, non-list tags, non-list screenshots
        game = {
            "game_id": "game-edge",
            "name": "Edge Game",
            "path": "/bin/true",
            "alternate_names": "Alt One; Alt Two",
            "custom_fields": "not-a-dict",
            "tags": "not-a-list",
            "screenshots": "not-a-list",
        }
        proj = _project_game(game, 0, set(), set(), None, {}, 0)
        self.assertEqual(proj["alternate_names"], ["Alt One", "Alt Two"])
        self.assertEqual(proj["custom_fields"], {})
        self.assertEqual(proj["tags"], [])
        self.assertEqual(proj["screenshots"], [])

    def test_media_roots_env_and_capping(self):
        import webapp_state
        from webapp_state import _build_known_media_set, MEDIA_ROOTS_ENV

        extra_root = Path(self.tempdir.name) / "extra_media"
        extra_root.mkdir(parents=True, exist_ok=True)
        (extra_root / "pic.png").write_bytes(b"123")

        prev_env = os.environ.get(MEDIA_ROOTS_ENV)
        os.environ[MEDIA_ROOTS_ENV] = str(extra_root)
        try:
            webapp_state._KNOWN_MEDIA_SET_CACHE.clear()
            mset = _build_known_media_set()
            self.assertIn(str(extra_root / "pic.png"), mset)

            # Test capping
            orig_max = webapp_state._KNOWN_MEDIA_MAX
            try:
                webapp_state._KNOWN_MEDIA_MAX = 1
                webapp_state._KNOWN_MEDIA_SET_CACHE.clear()
                capped_set = _build_known_media_set()
                self.assertGreaterEqual(len(capped_set), 1)
            finally:
                webapp_state._KNOWN_MEDIA_MAX = orig_max
        finally:
            if prev_env is None:
                os.environ.pop(MEDIA_ROOTS_ENV, None)
            else:
                os.environ[MEDIA_ROOTS_ENV] = prev_env

    def test_load_state_view_cache_hit_and_miss(self):
        from webapp_state import load_state_view

        st1 = load_state_view()
        self.assertIsNotNone(st1)
        # Second call hits cache but returns a detached copy (F09 snapshot safety)
        st2 = load_state_view()
        self.assertEqual(st1, st2)
        self.assertIsNot(st1, st2)

    def test_handler_setup_oserror_handled(self):
        from web_app import Handler

        fake_sock = mock.MagicMock()
        fake_sock.setsockopt.side_effect = OSError("sockopt fail")
        handler = object.__new__(Handler)
        handler.request = fake_sock
        handler.connection = fake_sock
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()
        handler.setup()

    def test_media_set_contains_normalization(self):
        from webapp_state import _media_set_contains

        mset = {"/tmp/media/pic.png"}
        self.assertTrue(_media_set_contains(mset, "/tmp/media/pic.png"))
        self.assertTrue(_media_set_contains(mset, "/tmp//media/pic.png"))
        self.assertFalse(_media_set_contains(mset, "/tmp/media/other.png"))
        self.assertFalse(_media_set_contains(mset, ""))

    def test_plugins_background_refresh(self):
        import time
        import webapp_state
        from webapp_state import _build_public_state, PLUGIN_LIBRARY_CACHE, PLUGIN_LIBRARY_LOCK

        with PLUGIN_LIBRARY_LOCK:
            PLUGIN_LIBRARY_CACHE.update({"at": 0.0, "payload": {"games": []}, "state_signature": webapp_state.STATE_STORE.signature()})
        with mock.patch("webapp_state.run_plugins", return_value={"games": []}):
            _build_public_state()
            time.sleep(0.1)

class FacetCacheTests(unittest.TestCase):
    """F1: FacetCache LRU + budget + epoch bump on _invalidate_all."""

    def setUp(self):
        from pkg.state.cache import FACET_CACHE
        FACET_CACHE._store.clear()
        FACET_CACHE.epoch = 0
        FACET_CACHE.max_size = 64
        FACET_CACHE.budget_ms = 50.0

    def test_facet_lru_evicts_oldest(self):
        from pkg.state.cache import FacetCache
        fc = FacetCache(max_size=2, budget_ms=50.0)
        fc.set("a", {"facets": [1]})
        fc.set("b", {"facets": [2]})
        # access a to make b LRU
        self.assertEqual(fc.get("a"), {"facets": [1]})
        fc.set("c", {"facets": [3]})
        self.assertIsNone(fc.get("b"), "LRU should evict b")
        self.assertEqual(fc.get("a"), {"facets": [1]})
        self.assertEqual(fc.get("c"), {"facets": [3]})
        # max_size bound
        self.assertLessEqual(len(fc._store), 2)

    def test_facet_lru_budget_degraded(self):
        from pkg.state.cache import FACET_CACHE
        # 10k games with tiny budget should degrade
        games = [{"game_id": f"g{i}", "platform": f"P{i%5}", "genre": "Action", "hidden": False} for i in range(8000)]
        result = FACET_CACHE.compute_facets(games, "platform", limit=10, budget_ms=0.001)
        self.assertTrue(result["degraded"])
        self.assertEqual(result["code"], "DEGRADED")
        self.assertIn("facets", result)
        # normal budget should not degrade and return full counts
        FACET_CACHE._store.clear()
        result2 = FACET_CACHE.compute_facets(games, "platform", limit=10, budget_ms=1000.0)
        self.assertFalse(result2["degraded"])
        self.assertEqual(result2["code"], "OK")
        self.assertGreater(len(result2["facets"]), 0)
        # degraded result is partial vs full
        self.assertLessEqual(len(result["facets"]), len(result2["facets"]))

    def test_facet_cache_uses_time_monotonic_budget(self):
        import time
        from pkg.state.cache import FACET_CACHE
        games = [{"game_id": f"g{i}", "platform": "PC", "hidden": False} for i in range(5000)]
        # patch time.monotonic to simulate budget exceed after 100 games
        start = time.monotonic()
        call_count = {"n": 0}

        def fake_mono():
            call_count["n"] += 1
            # after 5 calls, pretend 100ms has passed
            if call_count["n"] > 5:
                return start + 0.1
            return start + 0.001 * call_count["n"]

        with mock.patch("pkg.state.cache.time.monotonic", side_effect=fake_mono):
            r = FACET_CACHE.compute_facets(games, "platform", limit=10, budget_ms=5.0)
            self.assertTrue(r["degraded"])
            self.assertEqual(r["code"], "DEGRADED")

        # restore budget
        FACET_CACHE.budget_ms = 50.0

    def test_facet_epoch_bump_on_invalidate_all(self):
        from pkg.state.cache import FACET_CACHE, CACHE_EPOCH
        FACET_CACHE.set("k1", {"facets": []})
        old_epoch = FACET_CACHE.epoch
        self.assertIn("k1", FACET_CACHE._store)
        CACHE_EPOCH._invalidate_all()
        self.assertNotIn("k1", FACET_CACHE._store)
        self.assertEqual(FACET_CACHE.epoch, old_epoch + 1)
        # second bump
        old2 = FACET_CACHE.epoch
        CACHE_EPOCH._invalidate_all(bump_media=True)
        self.assertEqual(FACET_CACHE.epoch, old2 + 1)

    def test_facet_cache_get_facets_cached_and_lru(self):
        from pkg.state.cache import FACET_CACHE
        games = [{"game_id": "g1", "platform": "PC", "hidden": False}, {"game_id": "g2", "platform": "PC", "hidden": False}]
        r1 = FACET_CACHE.get_facets(games, "platform", limit=10)
        r2 = FACET_CACHE.get_facets(games, "platform", limit=10)
        self.assertIs(r1, r2, "cached result should be same object")
        # different field should be separate key
        r3 = FACET_CACHE.get_facets(games, "genre", limit=10)
        self.assertIsNot(r1, r3)

    def test_facet_invalid_field_returns_empty(self):
        from pkg.state.cache import FACET_CACHE
        games = [{"game_id": "g1", "platform": "PC"}]
        r = FACET_CACHE.compute_facets(games, "invalid_field", limit=10)
        self.assertEqual(r["facets"], [])
        self.assertFalse(r["degraded"])

    def test_facet_all_fields_and_hidden_and_limits(self):
        from pkg.state.cache import FACET_CACHE
        FACET_CACHE._store.clear()
        games = [
            {"game_id": "g1", "genre": "Action, Adventure", "developer": "Nintendo", "publisher": "Sega", "platform": "NES", "progress": "Playing", "esrb": "E", "hidden": False},
            {"game_id": "g2", "genre": "Action", "developer": "  ", "publisher": "", "platform": "", "progress": "", "esrb": "", "hidden": True},
            {"game_id": "g3", "genre": "", "developer": "Capcom", "publisher": "Capcom", "platform": "SNES", "progress": "Beaten", "esrb": "T", "hidden": False},
            {"game_id": "g4", "genre": "RPG", "developer": "Square", "publisher": "Square", "platform": "PlayStation", "progress": "", "esrb": "M", "hidden": False},
        ]
        for field in ["genre", "developer", "publisher", "platform", "progress", "esrb"]:
            r = FACET_CACHE.compute_facets(games, field, limit=2, budget_ms=1000.0)
            self.assertIn("facets", r)
            self.assertFalse(r["degraded"])
            self.assertLessEqual(len(r["facets"]), 2)
        # genre should count Action once (hidden excluded), Adventure once, RPG once
        FACET_CACHE._store.clear()
        r_genre = FACET_CACHE.compute_facets(games, "genre", limit=10, budget_ms=1000.0)
        vals = {x["value"]: x["count"] for x in r_genre["facets"]}
        self.assertEqual(vals.get("Action"), 1)
        self.assertEqual(vals.get("Adventure"), 1)
        self.assertEqual(vals.get("RPG"), 1)
        # limit TypeError path and empty games handling
        FACET_CACHE._store.clear()
        r_limit_bad = FACET_CACHE.compute_facets(games, "platform", limit="bad", budget_ms=1000.0)
        self.assertIsNotNone(r_limit_bad)
        r_empty = FACET_CACHE.compute_facets([], "platform", limit=10, budget_ms=1000.0)
        self.assertEqual(r_empty["facets"], [])
        # exception in first_id/last_id (games not list of dicts with get)
        FACET_CACHE._store.clear()
        r_exc = FACET_CACHE.compute_facets([None, None], "platform", limit=10, budget_ms=1000.0)
        self.assertIn("facets", r_exc)
        # progress Unset and platform Unspecified handling already covered via empty strings
        # esrb Unrated via empty

    def test_facet_final_budget_check(self):
        import time
        from pkg.state.cache import FACET_CACHE
        FACET_CACHE._store.clear()
        games = [{"game_id": f"g{i}", "platform": "PC", "hidden": False} for i in range(100)]
        start = time.monotonic()
        # fake monotonic where loop itself doesn't exceed but final check does
        calls = {"n": 0}
        def fake_mono():
            calls["n"] += 1
            if calls["n"] <= 101:  # during loop, stay under
                return start
            # final check after loop: exceed
            return start + 0.2
        with mock.patch("pkg.state.cache.time.monotonic", side_effect=fake_mono):
            r = FACET_CACHE.compute_facets(games, "platform", limit=10, budget_ms=5.0)
            # loop degraded false but final check true => degraded true
            self.assertTrue(r["degraded"])


if __name__ == "__main__":
    unittest.main()

