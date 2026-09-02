"""Tests for the ScreenScraper provider (pkg/parity/parity_screenscraper + handler)."""

from __future__ import annotations

import hashlib
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

from pkg.parity import parity_screenscraper as ss  # noqa: E402


class PlatformMappingTest(unittest.TestCase):
    def test_known_platforms(self):
        self.assertEqual(ss.system_id_for_platform("SNES"), 4)
        self.assertEqual(ss.system_id_for_platform("playstation"), 2)
        self.assertEqual(ss.system_id_for_platform("  Arcade  "), 11)

    def test_unknown_platform(self):
        self.assertIsNone(ss.system_id_for_platform("Toaster"))
        self.assertIsNone(ss.system_id_for_platform(""))

    def test_region_codes(self):
        self.assertEqual(ss.region_codes(["North America", "World", "Japan"]), ["us", "wor", "jp"])
        self.assertEqual(ss.region_codes(["Unknown Region"]), ["wor", "us", "eu", "jp"])
        self.assertEqual(ss.region_codes(None), ["wor", "us", "eu", "jp"])


class HashRomTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rom = Path(self.tmp.name) / "game.sfc"
        self.rom.write_bytes(b"openbox-rom-payload")

    def test_hashes_match_reference(self):
        hashes = ss.hash_rom(self.rom)
        self.assertEqual(hashes["md5"], hashlib.md5(b"openbox-rom-payload").hexdigest())
        self.assertEqual(hashes["sha1"], hashlib.sha1(b"openbox-rom-payload").hexdigest())
        self.assertEqual(len(hashes["crc"]), 8)
        self.assertEqual(hashes["size"], 19)

    def test_rom_type(self):
        self.assertEqual(ss.rom_type_for("game.zip"), "zip")
        self.assertEqual(ss.rom_type_for("disc.iso"), "iso")
        self.assertEqual(ss.rom_type_for("cart.sfc"), "rom")

    def test_missing_and_oversize(self):
        with self.assertRaises(ValueError):
            ss.hash_rom(Path(self.tmp.name) / "missing.sfc")
        big = Path(self.tmp.name) / "big.rom"
        with mock.patch.object(ss, "_HASH_SIZE_LIMIT", 4):
            big.write_bytes(b"123456")
            with self.assertRaises(ValueError):
                ss.hash_rom(big)


class ChooseMediaTest(unittest.TestCase):
    METADATA = {
        "media": [
            {"kind": "cover", "type": "box2D", "url": "https://ss/cover-us.png", "region": "us", "order": 1},
            {"kind": "cover", "type": "box2D", "url": "https://ss/cover-jp.png", "region": "jp", "order": 0},
            {"kind": "screenshots", "type": "ss", "url": "https://ss/shot1.png", "region": "wor", "order": 1},
            {"kind": "screenshots", "type": "ss", "url": "https://ss/shot2.png", "region": "wor", "order": 2},
        ],
    }

    def test_region_priority_picks_us_over_jp(self):
        chosen = ss.choose_media(self.METADATA, ["cover"], region_priority=["North America", "Japan"])
        self.assertEqual(chosen["cover"], "https://ss/cover-us.png")

    def test_jp_priority_flips(self):
        chosen = ss.choose_media(self.METADATA, ["cover"], region_priority=["Japan"])
        self.assertEqual(chosen["cover"], "https://ss/cover-jp.png")

    def test_screenshots_sorted_and_capped(self):
        chosen = ss.choose_media(self.METADATA, ["screenshots"], region_priority=None, limit=1)
        self.assertEqual(chosen["screenshots"], ["https://ss/shot1.png"])

    def test_unrequested_kinds_ignored(self):
        chosen = ss.choose_media(self.METADATA, ["fanart"])
        self.assertEqual(chosen, {})


class NormalizeTest(unittest.TestCase):
    def test_rating_converts_to_five_scale(self):
        self.assertEqual(ss._rating({"text": "16"}), 0.8)
        self.assertIsNone(ss._rating({"text": ""}))
        self.assertIsNone(ss._rating("junk"))

    def test_normalize_jeu(self):
        payload = {"response": {"jeu": {
            "id": 1234,
            "nom": {"text": "Super Game"},
            "synopsis": {"text": "A game."},
            "year": {"text": "1991"},
            "developpeur": {"text": "Dev"},
            "editeur": {"text": "Pub"},
            "genre": {"text": "Platform"},
            "note": {"text": "20"},
            "medias": [
                {"type": "box2D", "url": "https://ss/box.png", "region": "us", "order": 1},
                {"type": "ss", "url": "https://ss/a.png", "region": "wor", "order": 1},
            ],
        }}}
        metadata = ss._normalize_jeu(payload)
        self.assertEqual(metadata["name"], "Super Game")
        self.assertEqual(metadata["rating"], 1.0)
        self.assertEqual(metadata["id"], 1234)
        kinds = {entry["kind"] for entry in metadata["media"]}
        self.assertIn("cover", kinds)
        self.assertIn("screenshots", kinds)


class CacheTest(unittest.TestCase):
    def test_roundtrip_and_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            params = {"gameid": 1}
            self.assertIsNone(ss.cache_get(tmp, params))
            ss.cache_put(tmp, params, {"response": {"jeu": {"id": 1}}})
            self.assertEqual(ss.cache_get(tmp, params), {"response": {"jeu": {"id": 1}}})
            path = Path(tmp) / "screenscraper"
            entry = next(path.iterdir())
            os.utime(entry, (0, 0))
            self.assertIsNone(ss.cache_get(tmp, params))

    def test_cache_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ss.cache_size(tmp), 0)
            ss.cache_put(tmp, {"a": 1}, {"x": 1})
            ss.cache_put(tmp, {"b": 2}, {"y": 2})
            self.assertEqual(ss.cache_size(tmp), 2)


class RequestTest(unittest.TestCase):
    def _fake_response(self, payload):
        body = json.dumps(payload).encode()
        response = mock.Mock()
        response.headers = {}
        chunks = [body, b""]
        response.read = lambda max_bytes=None: chunks.pop(0) if chunks else b""
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=False)
        return response

    def test_ss_request_success_and_params(self):
        payload = {"response": {"jeux": []}}
        env = {
            "SCREENSCRAPER_USER": "user1", "SCREENSCRAPER_PASSWORD": "pass1",
            "SCREENSCRAPER_DEV_ID": "dev1", "SCREENSCRAPER_DEV_PASSWORD": "devpw1",
        }
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(ss, "urlopen", return_value=self._fake_response(payload)) as opener:
            result = ss.ss_request("jeuRecherche.php", {"recherche": "zelda"})
        self.assertEqual(result, payload)
        url = opener.call_args[0][0].full_url
        self.assertIn("jeuRecherche.php", url)
        self.assertIn("user=user1", url)
        self.assertIn("devid=dev1", url)
        self.assertIn("output=json", url)

    def test_ss_request_non_retryable_error_raises(self):
        from urllib.error import HTTPError

        with mock.patch.dict(os.environ, {"SCREENSCRAPER_USER": "u", "SCREENSCRAPER_PASSWORD": "p"}, clear=True), \
             mock.patch.object(ss, "urlopen", side_effect=HTTPError("u", 403, "no", None, io.BytesIO(b""))), \
             mock.patch.object(ss.time, "sleep"):
            with self.assertRaises(ValueError):
                ss.ss_request("jeuInfos.php", {"gameid": 1})

    def test_ss_request_retries_then_succeeds(self):
        from urllib.error import HTTPError

        payload = {"response": {"jeu": {"id": 5}}}
        responses = [HTTPError("u", 503, "busy", None, io.BytesIO(b"")), self._fake_response(payload)]
        with mock.patch.dict(os.environ, {"SCREENSCRAPER_USER": "u", "SCREENSCRAPER_PASSWORD": "p"}, clear=True), \
             mock.patch.object(ss, "urlopen", side_effect=responses), \
             mock.patch.object(ss.time, "sleep"):
            self.assertEqual(ss.ss_request("jeuInfos.php", {"gameid": 5}), payload)

    def test_game_info_by_id_and_cache(self):
        payload = {"response": {"jeu": {"id": 9, "nom": {"text": "Cached Game"}}}}
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(ss, "ss_request", return_value=payload) as request:
            first = ss.game_info(9, system_id=4, cache_dir=tmp)
            second = ss.game_info(9, system_id=4, cache_dir=tmp)
        request.assert_called_once()
        self.assertEqual(first["name"], "Cached Game")
        self.assertEqual(second["name"], "Cached Game")

    def test_game_info_by_rom_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            rom = Path(tmp) / "g.sfc"
            rom.write_bytes(b"payload")
            with mock.patch.object(ss, "ss_request", return_value={"response": {"jeu": {"id": 3}}}) as request:
                metadata = ss.game_info(rom_path=rom, system_id=4)
        params = request.call_args[0][1]
        self.assertIn("rommd5", params)
        self.assertEqual(params["romtype"], "rom")
        self.assertEqual(metadata["id"], 3)

    def test_game_info_rejects_missing_args_and_empty(self):
        with self.assertRaises(ValueError):
            ss.game_info()
        with mock.patch.object(ss, "ss_request", return_value={"response": {}}):
            with self.assertRaises(ValueError):
                ss.game_info(1)

    def test_user_info_uses_cache(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(ss, "ss_request", return_value={"response": {"ssuser": {}}}) as request:
            ss.user_info(cache_dir=tmp)
            ss.user_info(cache_dir=tmp)
        request.assert_called_once()

    def test_apply_to_game_fields(self):
        game = {"name": "Old"}
        metadata = {"id": 5, "name": "New", "description": "Desc", "year": "", "rating": 0.9}
        ss.apply_to_game(game, metadata, fields=("name", "description", "year"))
        self.assertEqual(game["name"], "New")
        self.assertEqual(game["description"], "Desc")
        self.assertNotIn("year", game)
        self.assertEqual(game["screenscraper_id"], 5)
        with self.assertRaises(ValueError):
            ss.apply_to_game("junk", metadata)

    def test_search_games_parses_response(self):
        payload = {"response": {"jeux": [
            {"id": 7, "name": "Alpha", "systemid": 4, "systemname": "Super Nintendo", "year": "1991"},
            "junk",
            {"id": 8, "name": "Beta", "systemid": 3, "systemname": "NES", "year": "1986"},
        ]}}
        with mock.patch.object(ss, "ss_request", return_value=payload) as request:
            results = ss.search_games("alpha", system_id=4, limit=5)
        request.assert_called_once()
        self.assertEqual(results[0]["id"], 7)
        self.assertEqual(len(results), 2)

    def test_search_games_empty_query(self):
        self.assertEqual(ss.search_games("   "), [])

    def test_clean_media_url(self):
        self.assertEqual(ss.clean_media_url("https://media.ss/img.png"), "https://media.ss/img.png")
        self.assertEqual(ss.clean_media_url("http://media.ss/img.png"), "")
        self.assertEqual(ss.clean_media_url("javascript:alert(1)"), "")
        self.assertEqual(ss.clean_media_url(""), "")

    def test_credentials_require_env(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                ss.credentials()

    def test_is_configured(self):
        env = {"SCREENSCRAPER_USER": "u", "SCREENSCRAPER_PASSWORD": "p"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertTrue(ss.is_configured())
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(ss.is_configured())


SCREENSCRAPER_HANDLER_PAYLOAD = {"response": {"ssuser": {"quota": {"requeststoday": 5}}}}


class ScreenScraperHandlerTest(unittest.TestCase):
    def handler(self):
        import web_app
        h = web_app.Handler.__new__(web_app.Handler)
        h.responses = []
        h.send_json = lambda status, payload: h.responses.append((status, payload))
        return h

    def test_status_unconfigured(self):
        import handlers.screenscraper as handler_module
        with mock.patch.object(handler_module, "is_configured", return_value=False), \
             mock.patch.object(handler_module, "cache_size", return_value=0):
            h = self.handler()
            h._api_get_api_v2_screenscraper_status(None)
        self.assertEqual(h.responses[0][0], 200)
        self.assertEqual(h.responses[0][1], {"configured": False, "cache_entries": 0})

    def test_search_route(self):
        import handlers.screenscraper as handler_module
        with mock.patch.object(handler_module, "search_games", return_value=[{"id": 1, "name": "X"}]) as search:
            h = self.handler()
            h._api_get_api_v2_screenscraper_search(type("P", (), {"query": "q=x&platform=SNES"})())
        search.assert_called_once_with("x", system_id=4)
        self.assertEqual(h.responses[0][0], 200)
        self.assertEqual(h.responses[0][1]["results"][0]["name"], "X")

    def test_info_route_bad_request(self):
        from api_errors import BadRequest

        import handlers.screenscraper as handler_module
        with mock.patch.object(handler_module, "game_info", side_effect=ValueError("not found")):
            h = self.handler()
            with self.assertRaises(BadRequest):
                h._api_post_api_v2_screenscraper_info({})

    def test_apply_job_roundtrip(self):
        import handlers.screenscraper as handler_module
        from pkg.parity.parity_export import approved_export_file  # noqa: F401  (import sanity)

        game = {"game_id": "game-a", "name": "Alpha", "platform": "SNES", "path": "/tmp/rom.sfc"}
        state = {"games": [game], "settings": {"region_priority": ["Japan"]}}
        metadata = {"id": 9, "name": "Alpha Remapped", "media": []}

        with tempfile.TemporaryDirectory() as tmp:
            media_root = Path(tmp)
            with mock.patch.object(handler_module, "load_state", return_value=state), \
                 mock.patch.object(handler_module, "game_from_payload", side_effect=lambda state_, payload_: game), \
                 mock.patch.object(handler_module, "game_info", return_value=metadata) as info, \
                 mock.patch.object(handler_module, "choose_media", return_value={"cover": "https://ss/c.png"}), \
                 mock.patch.object(handler_module, "clean_media_url", side_effect=lambda url: url), \
                 mock.patch.object(handler_module, "download_bytes", return_value=str(media_root / "cover.jpg")) as download, \
                 mock.patch.object(handler_module, "transact_state", side_effect=lambda mutate: (None, mutate(state))) as transact, \
                 mock.patch.object(handler_module, "JOB_MANAGER", self.job_manager()):
                h = self.handler()
                h._api_post_api_v2_screenscraper_apply({"id": "game-a", "scraper_id": 9, "media": ["cover"]})
            self.assertEqual(h.responses[0][0], 202)
            info.assert_called_once()
            download.assert_called_once()
            transact.assert_called_once()
            self.assertEqual(game.get("cover"), str(media_root / "cover.jpg"))

    def test_test_route_reports_connection(self):
        with mock.patch.object(ss, "user_info", return_value={"response": {"ssuser": {"quota": {"requeststoday": 5}}}}):
            h = self.handler()
            h._api_post_api_v2_screenscraper_test({})
        self.assertEqual(h.responses[0][0], 200)
        self.assertTrue(h.responses[0][1]["ok"])

    def test_match_job_reports_statuses(self):
        import handlers.screenscraper as handler_module

        with tempfile.TemporaryDirectory() as tmp:
            rom = Path(tmp) / "b.sfc"
            rom.write_bytes(b"rom-bytes")
            games = [
                {"game_id": "game-a", "name": "NoRom", "platform": "SNES", "path": ""},
                {"game_id": "game-b", "name": "Matched", "platform": "SNES", "path": str(rom)},
                {"game_id": "game-c", "name": "Broken", "platform": "SNES", "path": str(rom)},
            ]
            state = {"games": games, "settings": {}}
            with mock.patch.object(handler_module, "load_state", return_value=state), \
                 mock.patch.object(ss, "hash_rom", return_value={"md5": "x"}), \
                 mock.patch.object(handler_module, "game_info", side_effect=[{"id": 12, "name": "Match"}, ValueError("rate limited")]), \
                 mock.patch.object(handler_module, "JOB_MANAGER", self.job_manager()):
                h = self.handler()
                h._api_post_api_v2_screenscraper_match({"ids": ["game-a", "game-b", "game-c"]})
        self.assertEqual(h.responses[0][0], 202)
        result = self.last_job["result"]
        self.assertEqual(result["matches"][0]["status"], "no_rom")
        self.assertEqual(result["matches"][1]["status"], "matched")
        self.assertEqual(result["matches"][2]["status"], "error")

    def job_manager(self):
        captured = {}

        class FakeJobManager:
            def submit(self, name, worker, **kwargs):
                captured["name"] = name
                captured["result"] = worker(None)
                return {"job_id": "job-ss"}

        self.last_job = captured
        return FakeJobManager()

    def _ext_for_test(self):
        from handlers.screenscraper import _ext_for
        self.assertEqual(_ext_for("https://x/a.png", ".jpg"), ".png")
        self.assertEqual(_ext_for("https://x/video", ".mp4"), ".mp4")


if __name__ == "__main__":
    unittest.main(verbosity=2)
