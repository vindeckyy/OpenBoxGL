"""Tests for the SteamGridDB provider (pkg/parity/parity_steamgrid + handlers/steamgrid)."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity import parity_steamgrid as sg  # noqa: E402
from updates import VERSION  # noqa: E402


def _env(key="sgdb-key"):
    values = {"STEAMGRIDDB_API_KEY": key} if key else {}
    return mock.patch.dict(os.environ, values, clear=True)


def _no_env_load():
    # Keep tests hermetic: pretend .env discovery already ran.
    return mock.patch("env_config._env_bootstrapped", True)


def _http_error(code, headers=None):
    from urllib.error import HTTPError

    return HTTPError("https://www.steamgriddb.com/api/v2/x", code, "err", headers or {}, io.BytesIO(b""))


class CredentialsTest(unittest.TestCase):
    def test_missing_key_raises(self):
        with _env(None), _no_env_load():
            with self.assertRaises(ValueError):
                sg.credentials()

    def test_env_key_and_openbox_fallback(self):
        with _env("primary"), _no_env_load():
            self.assertEqual(sg.credentials(), "primary")
        with mock.patch.dict(os.environ, {"OPENBOX_STEAMGRIDDB_API_KEY": "alt"}, clear=True), _no_env_load():
            self.assertEqual(sg.credentials(), "alt")

    def test_is_configured(self):
        with _env("k"), _no_env_load():
            self.assertTrue(sg.is_configured())
        with _env(None), _no_env_load():
            self.assertFalse(sg.is_configured())

    def test_enabled_flag(self):
        self.assertTrue(sg.enabled({}))
        self.assertTrue(sg.enabled({"steamgrid_enabled": True}))
        self.assertFalse(sg.enabled({"steamgrid_enabled": False}))
        self.assertTrue(sg.enabled(None))

    def test_provider_available(self):
        with _env("k"), _no_env_load(), mock.patch.dict(sg._SESSION_DISABLED, {"value": False}):
            self.assertTrue(sg.provider_available({}))
            self.assertFalse(sg.provider_available({"steamgrid_enabled": False}))
        with _env(None), _no_env_load(), mock.patch.dict(sg._SESSION_DISABLED, {"value": False}):
            self.assertFalse(sg.provider_available({}))
        with _env("k"), _no_env_load(), mock.patch.dict(sg._SESSION_DISABLED, {"value": True}):
            self.assertFalse(sg.provider_available({}))

    def test_env_keys_registered(self):
        import env_config

        self.assertIn("STEAMGRIDDB_API_KEY", env_config.ENV_KEYS)
        self.assertIn("OPENBOX_STEAMGRIDDB_API_KEY", env_config.ENV_KEYS)


class RequestTest(unittest.TestCase):
    def setUp(self):
        self._disabled = mock.patch.dict(sg._SESSION_DISABLED, {"value": False})
        self._disabled.start()
        self.addCleanup(self._disabled.stop)

    def _fake_response(self, payload):
        body = json.dumps(payload).encode()
        response = mock.Mock()
        response.headers = {}
        chunks = [body, b""]
        response.read = lambda max_bytes=None: chunks.pop(0) if chunks else b""
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=False)
        return response

    def test_request_builds_https_bearer_url(self):
        payload = {"success": True, "data": []}
        with _env("key-1"), _no_env_load(), \
             mock.patch.object(sg, "urlopen", return_value=self._fake_response(payload)) as opener:
            result = sg.sgdb_request("search/autocomplete/zelda", {"limit": 3})
        self.assertEqual(result, payload)
        request = opener.call_args[0][0]
        self.assertTrue(request.full_url.startswith("https://www.steamgriddb.com/api/v2/"))
        self.assertIn("search/autocomplete/zelda", request.full_url)
        self.assertIn("limit=3", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer key-1")
        self.assertEqual(request.get_header("User-agent"), f"OpenBox/{VERSION}")

    def test_request_rejects_non_https_endpoint(self):
        with _env("k"), _no_env_load(), \
             mock.patch.object(sg, "SGDB_ENDPOINT", "http://insecure.example/api"), \
             mock.patch.object(sg, "urlopen") as opener:
            with self.assertRaises(ValueError):
                sg.sgdb_request("games/id/1")
        opener.assert_not_called()

    def test_request_429_backs_off_exponentially(self):
        payload = {"success": True, "data": [1]}
        responses = [_http_error(429), _http_error(429), self._fake_response(payload)]
        with _env("k"), _no_env_load(), \
             mock.patch.object(sg, "_throttle"), \
             mock.patch.object(sg, "urlopen", side_effect=responses) as opener, \
             mock.patch.object(sg.time, "sleep") as sleep:
            self.assertEqual(sg.sgdb_request("grids/game/1"), payload)
        self.assertEqual(opener.call_count, 3)
        delays = [call.args[0] for call in sleep.call_args_list]
        self.assertEqual(delays, [sg._BACKOFF_BASE, sg._BACKOFF_BASE * 2])

    def test_request_429_honors_retry_after(self):
        payload = {"success": True, "data": []}
        responses = [_http_error(429, headers={"Retry-After": "7"}), self._fake_response(payload)]
        with _env("k"), _no_env_load(), \
             mock.patch.object(sg, "_throttle"), \
             mock.patch.object(sg, "urlopen", side_effect=responses), \
             mock.patch.object(sg.time, "sleep") as sleep:
            self.assertEqual(sg.sgdb_request("grids/game/1"), payload)
        sleep.assert_called_once_with(7.0)

    def test_request_429_exhausts_retries(self):
        responses = [_http_error(429)] * (sg._MAX_RETRIES + 1)
        with _env("k"), _no_env_load(), \
             mock.patch.object(sg, "_throttle"), \
             mock.patch.object(sg, "urlopen", side_effect=responses) as opener, \
             mock.patch.object(sg.time, "sleep"):
            with self.assertRaises(ValueError):
                sg.sgdb_request("grids/game/1")
        self.assertEqual(opener.call_count, sg._MAX_RETRIES)

    def test_request_401_disables_provider_for_session(self):
        with _env("k"), _no_env_load(), \
             mock.patch.object(sg, "_throttle"), \
             mock.patch.object(sg, "urlopen", side_effect=_http_error(401)) as opener, \
             mock.patch.object(sg.time, "sleep"):
            with self.assertRaises(ValueError):
                sg.sgdb_request("search/autocomplete/x")
            self.assertTrue(sg.session_disabled())
            with self.assertRaises(ValueError):
                sg.sgdb_request("search/autocomplete/x")
        self.assertEqual(opener.call_count, 1)  # surfaced once, no repeat network hits

    def test_request_404_not_retried(self):
        with _env("k"), _no_env_load(), \
             mock.patch.object(sg, "_throttle"), \
             mock.patch.object(sg, "urlopen", side_effect=_http_error(404)) as opener, \
             mock.patch.object(sg.time, "sleep") as sleep:
            with self.assertRaises(ValueError):
                sg.sgdb_request("games/id/999")
        opener.assert_called_once()
        sleep.assert_not_called()

    def test_request_503_retried(self):
        payload = {"success": True, "data": {"id": 5}}
        with _env("k"), _no_env_load(), \
             mock.patch.object(sg, "_throttle"), \
             mock.patch.object(sg, "urlopen", side_effect=[_http_error(503), self._fake_response(payload)]), \
             mock.patch.object(sg.time, "sleep"):
            self.assertEqual(sg.sgdb_request("games/id/5"), payload)


class CacheTest(unittest.TestCase):
    def test_roundtrip_and_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            params = {"sgdb_game": 1}
            self.assertIsNone(sg.cache_get(tmp, params))
            sg.cache_put(tmp, params, {"data": {"id": 1}})
            self.assertEqual(sg.cache_get(tmp, params), {"data": {"id": 1}})
            entry = next((Path(tmp) / "steamgrid").iterdir())
            os.utime(entry, (0, 0))
            self.assertIsNone(sg.cache_get(tmp, params))

    def test_cache_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(sg.cache_size(tmp), 0)
            sg.cache_put(tmp, {"a": 1}, {"x": 1})
            sg.cache_put(tmp, {"b": 2}, {"y": 2})
            self.assertEqual(sg.cache_size(tmp), 2)

    def test_cache_get_tolerates_corrupt_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            sg.cache_put(tmp, {"a": 1}, {"x": 1})
            entry = next((Path(tmp) / "steamgrid").iterdir())
            entry.write_text("not-json", encoding="utf-8")
            self.assertIsNone(sg.cache_get(tmp, {"a": 1}))


class SearchTest(unittest.TestCase):
    def test_search_parses_and_tags_provider(self):
        payload = {"success": True, "data": [
            {"id": 7, "name": "Alpha", "release_date": 662688000, "types": ["steam"], "verified": True},
            "junk",
            {"id": 8, "name": "Beta", "types": []},
        ]}
        with mock.patch.object(sg, "sgdb_request", return_value=payload) as request:
            results = sg.search_games("alpha", limit=5)
        request.assert_called_once()
        self.assertIn("search/autocomplete/alpha", request.call_args[0][0])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["id"], 7)
        self.assertEqual(results[0]["provider"], "steamgrid")
        self.assertEqual(results[0]["year"], "1991")
        self.assertTrue(results[0]["verified"])

    def test_search_empty_query_and_limit_clamp(self):
        self.assertEqual(sg.search_games("   "), [])
        rows = [{"id": index, "name": f"G{index}"} for index in range(20)]
        with mock.patch.object(sg, "sgdb_request", return_value={"data": rows}):
            self.assertEqual(len(sg.search_games("x", limit=5)), 5)
            self.assertEqual(len(sg.search_games("x", limit="junk")), 12)

    def test_search_uses_cache(self):
        payload = {"data": [{"id": 1, "name": "Cached"}]}
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(sg, "sgdb_request", return_value=payload) as request:
            self.assertEqual(sg.search_games("cached", cache_dir=tmp)[0]["name"], "Cached")
            self.assertEqual(sg.search_games("cached", cache_dir=tmp)[0]["name"], "Cached")
        request.assert_called_once()


class GameInfoTest(unittest.TestCase):
    DETAIL = {"success": True, "data": {"id": 42, "name": "Game", "release_date": 662688000, "types": ["steam"], "verified": True}}

    def _assets_payloads(self):
        return {
            "grids": {"data": [
                {"url": "https://cdn/vert.png", "score": 5, "width": 600, "height": 900},
                {"url": "https://cdn/nsfw.png", "score": 99, "nsfw": True},
                {"url": "https://cdn/humor.png", "score": 88, "humor": True},
            ]},
            "heroes": {"data": [{"url": "https://cdn/hero.png", "score": 3}, {"url": "https://cdn/bad.png", "epilepsy": True}]},
            "logos": {"data": [{"url": "https://cdn/logo.png", "score": 7}]},
            "icons": {"data": [{"url": "https://cdn/icon.png", "score": 1}]},
        }

    def test_game_assets_merges_filters_and_sorts(self):
        payloads = self._assets_payloads()

        def fake(path, params=None):
            return payloads[path.split("/")[0]]

        with mock.patch.object(sg, "sgdb_request", side_effect=fake) as request:
            media = sg.game_assets(42)
        kinds = {entry["kind"] for entry in media}
        self.assertEqual(kinds, {"cover", "banner", "background", "clear_logo", "icon"})
        urls = [entry["url"] for entry in media]
        self.assertNotIn("https://cdn/nsfw.png", urls)
        self.assertNotIn("https://cdn/humor.png", urls)
        self.assertNotIn("https://cdn/bad.png", urls)
        self.assertEqual([entry["url"] for entry in media], sorted(urls, key=lambda u: -[e["score"] for e in media if e["url"] == u][0]))
        grid_calls = [call for call in request.call_args_list if call[0][0].startswith("grids/")]
        self.assertEqual(len(grid_calls), 2)  # vertical covers + horizontal banners

    def test_game_assets_cache(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(sg, "sgdb_request", return_value={"data": []}) as request:
            self.assertEqual(sg.game_assets(1, cache_dir=tmp), [])
            sg.game_assets(1, cache_dir=tmp)
        self.assertEqual(request.call_count, 5)  # grids x2 + heroes + logos + icons, all cached

    def test_game_info_normalizes_and_caches(self):
        def fake(path, params=None):
            if path.startswith("games/"):
                return self.DETAIL
            return {"data": []}

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(sg, "sgdb_request", side_effect=fake) as request:
            metadata = sg.game_info(42, cache_dir=tmp)
            again = sg.game_info(42, cache_dir=tmp)
        self.assertEqual(metadata["id"], 42)
        self.assertEqual(metadata["name"], "Game")
        self.assertEqual(metadata["year"], "1991")
        self.assertEqual(metadata["types"], ["steam"])
        self.assertEqual(metadata["media"], [])
        self.assertEqual(again, metadata)
        self.assertEqual(request.call_count, 6)  # detail + 5 asset requests, all cached

    def test_game_info_not_found_and_bad_id(self):
        with mock.patch.object(sg, "sgdb_request", return_value={"data": {}}):
            with self.assertRaises(ValueError):
                sg.game_info(7)
        with self.assertRaises(ValueError):
            sg.game_info("abc")
        with self.assertRaises(ValueError):
            sg.game_info(None)


class ChooseMediaTest(unittest.TestCase):
    def test_top_score_wins_and_kinds_filtered(self):
        metadata = {"media": [
            {"kind": "cover", "url": "https://a/low.png", "score": 1},
            {"kind": "cover", "url": "https://a/high.png", "score": 9},
            {"kind": "icon", "url": "https://a/i.png", "score": 2},
            {"kind": "banner", "url": "https://a/b.png", "score": 5},
        ]}
        self.assertEqual(
            sg.choose_media(metadata, ["cover", "icon"]),
            {"cover": "https://a/high.png", "icon": "https://a/i.png"},
        )

    def test_non_https_url_skipped(self):
        metadata = {"media": [
            {"kind": "cover", "url": "http://insecure/x.png", "score": 9},
            {"kind": "cover", "url": "https://a/ok.png", "score": 1},
        ]}
        self.assertEqual(sg.choose_media(metadata, ["cover"]), {"cover": "https://a/ok.png"})

    def test_bare_media_list_and_empty(self):
        self.assertEqual(sg.choose_media([{"kind": "icon", "url": "https://a/i.png"}], ["icon"]), {"icon": "https://a/i.png"})
        self.assertEqual(sg.choose_media({"media": []}, ["cover"]), {})
        self.assertEqual(sg.choose_media(None, ["cover"]), {})


class MergeResultsTest(unittest.TestCase):
    def test_provider_ordering(self):
        existing = [
            {"name": "L", "provider": "launchbox"},
            {"name": "S", "provider": "screenscraper"},
        ]
        additions = [{"name": "G"}, {"name": "H", "provider": "steamgrid"}]
        merged = sg.merge_results(existing, additions, provider="steamgrid")
        self.assertEqual([item["provider"] for item in merged], ["launchbox", "screenscraper", "steamgrid", "steamgrid"])

    def test_reorders_by_provider_rank(self):
        merged = sg.merge_results([{"name": "G", "provider": "steamgrid"}], [{"name": "I"}], provider="igdb")
        self.assertEqual([item["name"] for item in merged], ["I", "G"])

    def test_unknown_provider_sorts_last(self):
        merged = sg.merge_results([{"name": "X", "provider": "other"}], [{"name": "G"}], provider="steamgrid")
        self.assertEqual([item["name"] for item in merged], ["G", "X"])

    def test_non_dict_rows_dropped(self):
        merged = sg.merge_results([{"name": "A", "provider": "launchbox"}, "junk"], [{"name": "G"}])
        self.assertEqual([item["name"] for item in merged], ["A", "G"])


class ApplyTest(unittest.TestCase):
    def test_fields_and_id(self):
        game = {"name": "Old"}
        sg.apply_to_game(game, {"id": 5, "name": "New", "year": "1991"}, fields=("name", "year"))
        self.assertEqual(game["name"], "New")
        self.assertEqual(game["year"], "1991")
        self.assertEqual(game["steamgrid_id"], 5)

    def test_empty_values_skipped_and_junk_rejected(self):
        game = {"name": "Old"}
        sg.apply_to_game(game, {"id": 5, "name": "", "year": "1991"}, fields=("name", "year"))
        self.assertEqual(game["name"], "Old")
        self.assertEqual(game["year"], "1991")
        with self.assertRaises(ValueError):
            sg.apply_to_game("junk", {})

    def test_clean_media_url(self):
        self.assertEqual(sg.clean_media_url("https://cdn.sgdb/img.png"), "https://cdn.sgdb/img.png")
        self.assertEqual(sg.clean_media_url("http://cdn.sgdb/img.png"), "")
        self.assertEqual(sg.clean_media_url("javascript:alert(1)"), "")
        self.assertEqual(sg.clean_media_url(""), "")


class SettingsPlumbingTest(unittest.TestCase):
    def test_known_settings(self):
        from settings_schema import KNOWN_SETTINGS

        self.assertIn("steamgrid_enabled", KNOWN_SETTINGS)
        self.assertIn("steamgrid_key_configured", KNOWN_SETTINGS)

    def test_clean_settings(self):
        from handlers.settings import clean_settings

        self.assertIs(clean_settings({})["steamgrid_enabled"], True)
        self.assertIs(clean_settings({"steamgrid_enabled": False})["steamgrid_enabled"], False)

    def test_public_settings(self):
        from pkg.state.cache import _public_settings_uncached

        with mock.patch("pkg.state.cache.steamgrid_is_configured", return_value=True):
            public = _public_settings_uncached({"settings": {"steamgrid_enabled": False}, "games": []})
        self.assertIs(public["steamgrid_enabled"], False)
        self.assertIs(public["steamgrid_key_configured"], True)


class _FakeHandler:
    def __init__(self):
        self.responses = []

    def authorized(self):
        return True

    def handle_unauthorized(self):
        self.responses.append((401, {"error": "unauthorized"}))

    def send_json(self, status, payload):
        self.responses.append((status, payload))


class SteamgridHandlerTest(unittest.TestCase):
    def setUp(self):
        import handlers.steamgrid as module

        self.module = module
        self._disabled = mock.patch.dict(sg._SESSION_DISABLED, {"value": False})
        self._disabled.start()
        self.addCleanup(self._disabled.stop)

    def handler(self):
        return _FakeHandler()

    def job_manager(self):
        captured = {}

        class FakeJobManager:
            def submit(self, name, worker, **kwargs):
                captured["name"] = name
                captured["result"] = worker(None)
                return {"job_id": "job-sg"}

        self.last_job = captured
        return FakeJobManager()

    def test_status_route(self):
        with mock.patch.object(self.module, "provider_status", return_value={"provider": "steamgrid", "enabled": True}) as status, \
             mock.patch.object(self.module.openbox, "load_state", return_value={"settings": {"steamgrid_enabled": False}}):
            h = self.handler()
            self.module.steamgrid_status(h, type("P", (), {"query": ""})())
        status.assert_called_once()
        self.assertEqual(status.call_args[0][0], {"steamgrid_enabled": False})
        self.assertEqual(h.responses[0], (200, {"provider": "steamgrid", "enabled": True}))

    def test_search_route(self):
        with mock.patch.object(self.module, "provider_available", return_value=True), \
             mock.patch.object(self.module, "search_games", return_value=[{"id": 1, "name": "X", "provider": "steamgrid"}]) as search, \
             mock.patch.object(self.module.openbox, "load_state", return_value={"settings": {}}):
            h = self.handler()
            self.module.steamgrid_search(h, type("P", (), {"query": "q=alpha&limit=4"})())
        search.assert_called_once()
        self.assertEqual(search.call_args[0][0], "alpha")
        self.assertEqual(h.responses[0][0], 200)
        self.assertEqual(h.responses[0][1]["provider"], "steamgrid")
        self.assertEqual(h.responses[0][1]["results"][0]["provider"], "steamgrid")

    def test_search_route_requires_provider(self):
        from api_errors import BadRequest

        with mock.patch.object(self.module, "provider_available", return_value=False), \
             mock.patch.object(self.module.openbox, "load_state", return_value={"settings": {}}):
            with self.assertRaises(BadRequest):
                self.module.steamgrid_search(self.handler(), type("P", (), {"query": "q=x"})())

    def test_info_route(self):
        from api_errors import BadRequest

        with mock.patch.object(self.module, "provider_available", return_value=True), \
             mock.patch.object(self.module.openbox, "load_state", return_value={"settings": {}}):
            with self.assertRaises(BadRequest):
                self.module.steamgrid_info(self.handler(), {})
            with mock.patch.object(self.module, "game_info", return_value={"id": 9, "media": []}) as info:
                h = self.handler()
                self.module.steamgrid_info(h, {"steamgrid_id": 9})
        info.assert_called_once()
        self.assertEqual(info.call_args[0][0], 9)
        self.assertEqual(h.responses[0][1]["id"], 9)

    def test_test_route(self):
        with mock.patch.object(self.module, "provider_available", return_value=True), \
             mock.patch.object(self.module, "search_games", return_value=[{"id": 1}]) as search, \
             mock.patch.object(self.module.openbox, "load_state", return_value={"settings": {}}):
            h = self.handler()
            self.module.steamgrid_test(h, {})
        search.assert_called_once()
        self.assertEqual(h.responses[0][0], 200)
        self.assertTrue(h.responses[0][1]["ok"])

    def test_apply_job_roundtrip(self):
        game = {"game_id": "game-a", "name": "Alpha", "platform": "SNES"}
        state = {"games": [game], "settings": {}}
        metadata = {"id": 9, "name": "Alpha Remapped", "media": [{"kind": "cover", "url": "https://sg/c.png", "score": 5}]}
        with tempfile.TemporaryDirectory() as tmp:
            media_root = Path(tmp)
            with mock.patch.object(self.module, "provider_available", return_value=True), \
                 mock.patch.object(self.module.openbox, "load_state", return_value=state), \
                 mock.patch.object(self.module, "game_from_payload", side_effect=lambda state_, payload_: game), \
                 mock.patch.object(self.module, "game_info", return_value=metadata) as info, \
                 mock.patch.object(self.module, "choose_media", return_value={"cover": "https://sg/c.png"}), \
                 mock.patch.object(self.module, "download_bytes", return_value=str(media_root / "cover.png")) as download, \
                 mock.patch.object(self.module, "transact_state", side_effect=lambda mutate: (None, mutate(state))), \
                 mock.patch.object(self.module, "bump_media_epoch") as epoch, \
                 mock.patch.object(self.module, "JOB_MANAGER", self.job_manager()), \
                 mock.patch.object(self.module.openbox, "DATA", Path(tmp)):
                h = self.handler()
                self.module.steamgrid_apply(h, {"id": "game-a", "steamgrid_id": 9, "media": ["cover"]})
        self.assertEqual(h.responses[0][0], 202)
        info.assert_called_once()
        download.assert_called_once()
        epoch.assert_called_once()
        self.assertEqual(game.get("cover"), str(media_root / "cover.png"))
        self.assertEqual(game.get("steamgrid_id"), 9)
        self.assertEqual(game.get("name"), "Alpha Remapped")

    def test_apply_falls_back_to_name_search(self):
        game = {"game_id": "game-a", "name": "Alpha"}
        state = {"games": [game], "settings": {}}
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(self.module, "provider_available", return_value=True), \
             mock.patch.object(self.module.openbox, "load_state", return_value=state), \
             mock.patch.object(self.module, "game_from_payload", side_effect=lambda state_, payload_: game), \
             mock.patch.object(self.module, "search_games", return_value=[{"id": 4, "name": "Alpha"}]) as search, \
             mock.patch.object(self.module, "game_info", return_value={"id": 4, "media": []}) as info, \
             mock.patch.object(self.module, "choose_media", return_value={}), \
             mock.patch.object(self.module, "transact_state", side_effect=lambda mutate: (None, mutate(state))), \
             mock.patch.object(self.module, "bump_media_epoch"), \
             mock.patch.object(self.module, "JOB_MANAGER", self.job_manager()), \
             mock.patch.object(self.module.openbox, "DATA", Path(tmp)):
            h = self.handler()
            self.module.steamgrid_apply(h, {"id": "game-a"})
        search.assert_called_once_with("Alpha", limit=1, cache_dir=mock.ANY)
        info.assert_called_once()
        self.assertEqual(h.responses[0][0], 202)

    def test_apply_no_match_reports(self):
        game = {"game_id": "game-a", "name": "Alpha"}
        state = {"games": [game], "settings": {}}
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(self.module, "provider_available", return_value=True), \
             mock.patch.object(self.module.openbox, "load_state", return_value=state), \
             mock.patch.object(self.module, "game_from_payload", side_effect=lambda state_, payload_: game), \
             mock.patch.object(self.module, "search_games", return_value=[]), \
             mock.patch.object(self.module, "JOB_MANAGER", self.job_manager()), \
             mock.patch.object(self.module.openbox, "DATA", Path(tmp)):
            h = self.handler()
            self.module.steamgrid_apply(h, {"id": "game-a"})
        self.assertEqual(h.responses[0][0], 202)
        self.assertFalse(self.last_job["result"]["applied"])

    def test_match_job_fills_missing_art(self):
        games = [
            {"game_id": "a", "name": "HasCover", "cover": "/x.png"},
            {"game_id": "b", "name": "NeedsArt"},
            {"game_id": "c", "name": "NoMatch"},
            {"game_id": "d", "name": "  "},
        ]
        state = {"games": games, "settings": {}}

        def fake_search(name, limit=12, cache_dir=None):
            return [{"id": 1, "name": "NeedsArt"}] if name == "NeedsArt" else []

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(self.module, "provider_available", return_value=True), \
             mock.patch.object(self.module.openbox, "load_state", return_value=state), \
             mock.patch.object(self.module, "search_games", side_effect=fake_search), \
             mock.patch.object(self.module, "game_assets", return_value=[{"kind": "cover", "url": "https://sg/c.png", "score": 5}]), \
             mock.patch.object(self.module, "choose_media", return_value={"cover": "https://sg/c.png"}), \
             mock.patch.object(self.module, "download_bytes", return_value=str(Path(tmp) / "cover.png")), \
             mock.patch.object(self.module, "transact_state", side_effect=lambda mutate: (None, mutate(state))), \
             mock.patch.object(self.module, "game_from_payload", side_effect=lambda state_, payload_: games[1]), \
             mock.patch.object(self.module, "bump_media_epoch"), \
             mock.patch.object(self.module, "JOB_MANAGER", self.job_manager()), \
             mock.patch.object(self.module.openbox, "DATA", Path(tmp)):
            h = self.handler()
            self.module.steamgrid_match(h, {"media": ["cover"]})
        self.assertEqual(h.responses[0][0], 202)
        matches = self.last_job["result"]["matches"]
        statuses = {entry["name"]: entry["status"] for entry in matches}
        self.assertNotIn("HasCover", statuses)  # covered games are not scanned in fill mode
        self.assertEqual(statuses["NeedsArt"], "matched")
        self.assertEqual(statuses["NoMatch"], "not_found")
        self.assertEqual(statuses["  "], "no_name")
        self.assertEqual(games[1].get("cover"), str(Path(tmp) / "cover.png"))
        self.assertEqual(games[1].get("steamgrid_id"), 1)

    def test_match_error_status(self):
        games = [{"game_id": "a", "name": "Broken"}]
        state = {"games": games, "settings": {}}
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(self.module, "provider_available", return_value=True), \
             mock.patch.object(self.module.openbox, "load_state", return_value=state), \
             mock.patch.object(self.module, "search_games", side_effect=ValueError("rate limited")), \
             mock.patch.object(self.module, "JOB_MANAGER", self.job_manager()), \
             mock.patch.object(self.module.openbox, "DATA", Path(tmp)):
            h = self.handler()
            self.module.steamgrid_match(h, {})
        self.assertEqual(self.last_job["result"]["matches"][0]["status"], "error")

    def test_ext_for(self):
        self.assertEqual(self.module._ext_for("https://x/a.png", ".jpg"), ".png")
        self.assertEqual(self.module._ext_for("https://x/asset", ".png"), ".png")


class RegistrationTest(unittest.TestCase):
    def test_routes_registered_and_tabled(self):
        import routes as routes_mod
        from routes.registry import _REGISTRY

        import handlers.steamgrid  # noqa: F401

        for path in ("/api/v2/steamgrid/search", "/api/v2/steamgrid/status"):
            self.assertIn(("GET", path), _REGISTRY)
            self.assertEqual(routes_mod.GET_TABLE[path], _REGISTRY[("GET", path)].spec)
        for path in ("/api/v2/steamgrid/apply", "/api/v2/steamgrid/info", "/api/v2/steamgrid/match", "/api/v2/steamgrid/test"):
            self.assertIn(("POST", path), _REGISTRY)
            self.assertEqual(routes_mod.POST_TABLE[path], _REGISTRY[("POST", path)].spec)

    def test_module_in_manifest_and_loader(self):
        import inspect

        import routes.registry as registry

        manifest = (ROOT / "runtime_modules.txt").read_text()
        self.assertIn("pkg/parity/parity_steamgrid.py", manifest)
        self.assertIn("handlers/steamgrid.py", manifest)
        self.assertIn("handlers.steamgrid", inspect.getsource(registry._ensure_handlers_loaded))


if __name__ == "__main__":
    unittest.main(verbosity=2)
