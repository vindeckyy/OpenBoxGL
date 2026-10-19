"""Tests for pkg/parity/parity_steam_achievements.py (P11.1)."""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pkg.parity  # noqa: F401,E402  # register flat-import finder

from pkg.parity import parity_steam_achievements as steam  # noqa: E402


class FakeResponse:
    def __init__(self, payload):
        if isinstance(payload, (bytes, bytearray)):
            self.body = bytes(payload)
        else:
            self.body = json.dumps(payload).encode("utf-8")
        self.headers = {}

    def read(self, size=-1):
        if size is None or size < 0:
            size = len(self.body)
        chunk = self.body[:size]
        self.body = self.body[size:]
        return chunk

    def close(self):
        pass


class Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, request, timeout=None):
        self.calls.append((request, timeout))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item if hasattr(item, "read") else FakeResponse(item)


def http_error(code, payload=b""):
    return HTTPError("https://api.steampowered.com/x", code, "err", {}, io.BytesIO(payload))


def _fast_requests():
    """Disable the 1 s throttle so only retry delays are observable."""
    steam._LAST_REQUEST[0] = 0.0
    return mock.patch.object(steam, "MIN_REQUEST_INTERVAL", 0.0)


def _clear_env():
    return mock.patch.dict(os.environ, {}, clear=True)


SCHEMA_PAYLOAD = {
    "game": {
        "availableGameStats": {
            "achievements": [
                {"name": "ACH_A", "displayName": "Alpha", "description": "First", "hidden": 0,
                 "icon": "https://cdn.example/a.png", "icongray": "https://cdn.example/ag.png"},
                {"name": "ACH_B", "displayName": "Beta", "description": "Second", "hidden": 1},
                {"name": "ACH_C", "displayName": "Gamma", "description": "Third", "hidden": 0},
            ]
        }
    }
}

PLAYER_PAYLOAD = {
    "playerstats": {
        "success": True,
        "achievements": [
            {"apiname": "ACH_A", "achieved": 1, "unlocktime": 1600000000},
            {"apiname": "ACH_B", "achieved": 0, "unlocktime": 0},
        ],
    }
}

OWNED_PAYLOAD = {
    "response": {
        "games": [
            {"appid": 440, "name": "Team Fortress 2", "playtime_forever": 12, "rtime_last_played": 1700000000},
            {"appid": 570, "name": "Dota 2", "playtime_forever": 0, "rtime_last_played": 0},
            {"appid": "bad"},
        ]
    }
}


class CredentialsTests(unittest.TestCase):
    def test_missing_credentials_are_empty(self):
        with tempfile.TemporaryDirectory() as directory, _clear_env():
            self.assertEqual(steam.load_steam_credentials(directory), {"api_key": "", "steam_id": ""})
            self.assertEqual(steam.credential_status(data_dir=directory)["configured"], False)

    def test_save_roundtrip_is_owner_only_and_redacted(self):
        with tempfile.TemporaryDirectory() as directory, _clear_env():
            status = steam.save_steam_credentials(directory, api_key="secret-key", steam_id="76561198000000000")
            self.assertEqual(status, {"configured": True, "steam_id": "76561198000000000", "key_set": True})
            self.assertNotIn("secret-key", json.dumps(status))
            path = Path(directory) / "steam.json"
            self.assertTrue(path.is_file())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            values = steam.load_steam_credentials(directory)
            self.assertEqual(values, {"api_key": "secret-key", "steam_id": "76561198000000000"})

    def test_partial_save_keeps_existing_value(self):
        with tempfile.TemporaryDirectory() as directory, _clear_env():
            steam.save_steam_credentials(directory, api_key="key", steam_id="1")
            steam.save_steam_credentials(directory, steam_id="2")
            self.assertEqual(
                steam.load_steam_credentials(directory), {"api_key": "key", "steam_id": "2"}
            )

    def test_env_is_a_fallback_and_file_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"STEAM_API_KEY": "env-key", "STEAM_ID": "env-id"}, clear=True):
                self.assertEqual(
                    steam.load_steam_credentials(directory),
                    {"api_key": "env-key", "steam_id": "env-id"},
                )
            steam.save_steam_credentials(directory, api_key="file-key")
            with mock.patch.dict(os.environ, {"STEAM_API_KEY": "env-key", "STEAM_ID": "env-id"}, clear=True):
                self.assertEqual(
                    steam.load_steam_credentials(directory),
                    {"api_key": "file-key", "steam_id": "env-id"},
                )

    def test_malformed_file_falls_back_to_env(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "steam.json").write_text("{not json", encoding="utf-8")
            with mock.patch.dict(os.environ, {"STEAM_API_KEY": "env-key", "STEAM_ID": "9"}, clear=True):
                self.assertEqual(
                    steam.load_steam_credentials(directory), {"api_key": "env-key", "steam_id": "9"}
                )


class RequestTests(unittest.TestCase):
    def setUp(self):
        steam._LAST_REQUEST[0] = 0.0

    def test_fetch_schema_normalizes_and_sends_key(self):
        opener = Opener(SCHEMA_PAYLOAD)
        with _fast_requests():
            rows = steam.fetch_schema(440, opener, api_key="KEY", sleep=lambda _: None,
                                      clock=lambda: 100.0)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["apiname"], "ACH_A")
        self.assertEqual(rows[0]["title"], "Alpha")
        self.assertEqual(rows[0]["icon"], "https://cdn.example/a.png")
        self.assertTrue(opener.calls[0][0].full_url.startswith("https://api.steampowered.com/"))
        self.assertIn("appid=440", opener.calls[0][0].full_url)
        self.assertIn("key=KEY", opener.calls[0][0].full_url)
        self.assertEqual(opener.calls[0][1], steam.REQUEST_TIMEOUT)

    def test_fetch_player_success_false(self):
        opener = Opener({"playerstats": {"success": False, "achievements": []}})
        with _fast_requests():
            result = steam.fetch_player(440, "7656", opener, api_key="K", sleep=lambda _: None,
                                        clock=lambda: 100.0)
        self.assertEqual(result, {"success": False, "achievements": []})

    def test_fetch_player_rows(self):
        opener = Opener(PLAYER_PAYLOAD)
        with _fast_requests():
            result = steam.fetch_player(440, "7656", opener, api_key="K", sleep=lambda _: None,
                                        clock=lambda: 100.0)
        self.assertTrue(result["success"])
        self.assertEqual(result["achievements"][0],
                         {"apiname": "ACH_A", "achieved": 1, "unlock_time": 1600000000})
        self.assertIn("steamid=7656", opener.calls[0][0].full_url)
        self.assertIn("l=english", opener.calls[0][0].full_url)

    def test_fetch_owned_games_filters_invalid(self):
        opener = Opener(OWNED_PAYLOAD)
        with _fast_requests():
            owned = steam.fetch_owned_games("7656", opener, api_key="K", sleep=lambda _: None,
                                            clock=lambda: 100.0)
        self.assertEqual(len(owned), 2)
        self.assertEqual(owned[0]["appid"], 440)
        self.assertEqual(owned[0]["playtime_forever"], 12)
        self.assertIn("include_played_free_games=1", opener.calls[0][0].full_url)

    def test_retry_on_503_then_success(self):
        delays = []
        opener = Opener(http_error(503), SCHEMA_PAYLOAD)
        with _fast_requests():
            rows = steam.fetch_schema(440, opener, api_key="K", sleep=delays.append,
                                      clock=lambda: 100.0)
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(delays, [2.0])

    def test_retries_twice_with_backoff(self):
        delays = []
        opener = Opener(http_error(429), http_error(502), SCHEMA_PAYLOAD)
        with _fast_requests():
            steam.fetch_schema(440, opener, api_key="K", sleep=delays.append, clock=lambda: 100.0)
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(delays, [2.0, 8.0])

    def test_4xx_is_final(self):
        opener = Opener(http_error(403))
        with _fast_requests(), self.assertRaises(ValueError):
            steam.fetch_schema(440, opener, api_key="K", sleep=lambda _: None, clock=lambda: 100.0)
        self.assertEqual(len(opener.calls), 1)

    def test_invalid_json_is_a_value_error(self):
        opener = Opener(b"not json")
        with _fast_requests(), self.assertRaises(ValueError):
            steam.fetch_schema(440, opener, api_key="K", sleep=lambda _: None, clock=lambda: 100.0)
        self.assertEqual(len(opener.calls), 1)

    def test_http_url_is_refused_without_calling_opener(self):
        opener = Opener(SCHEMA_PAYLOAD)
        with self.assertRaises(ValueError):
            steam._request_json("http://api.steampowered.com/x", opener=opener,
                                clock=lambda: 1.0, sleep=lambda _: None)
        self.assertEqual(opener.calls, [])

    def test_missing_key_raises_before_network(self):
        opener = Opener(SCHEMA_PAYLOAD)
        with tempfile.TemporaryDirectory() as directory, _clear_env():
            with self.assertRaises(ValueError):
                steam.fetch_schema(440, opener, data_dir=directory)
        self.assertEqual(opener.calls, [])

    def test_app_id_must_be_numeric(self):
        with self.assertRaises(ValueError):
            steam.fetch_schema("abc", Opener(), api_key="K")


class MergeTests(unittest.TestCase):
    def test_merge_counts_earned_and_total(self):
        source = steam.merge_schema_player(SCHEMA_PAYLOAD, PLAYER_PAYLOAD)
        self.assertEqual(source["source"], "steam")
        self.assertEqual(source["name"], "Steam")
        self.assertEqual(source["earned"], 1)
        self.assertEqual(source["total"], 3)
        self.assertEqual(source["progress_pct"], 33.3)
        self.assertEqual(len(source["items"]), 3)
        self.assertTrue(source["items"][0]["earned"])
        self.assertFalse(source["items"][1]["earned"])
        self.assertEqual(source["items"][0]["unlock_time"], 1600000000)

    def test_empty_schema_has_no_source(self):
        self.assertIsNone(steam.merge_schema_player({"game": {"availableGameStats": {}}}, PLAYER_PAYLOAD))

    def test_success_false_has_no_source(self):
        player = {"playerstats": {"success": False, "achievements": []}}
        self.assertIsNone(steam.merge_schema_player(SCHEMA_PAYLOAD, player))

    def test_items_are_capped(self):
        schema = [{"name": f"A{i}", "displayName": f"Achievement {i}"} for i in range(600)]
        source = steam.merge_schema_player(schema, [])
        self.assertEqual(source["total"], steam.MAX_ACHIEVEMENT_ITEMS)
        self.assertEqual(len(source["items"]), steam.MAX_ACHIEVEMENT_ITEMS)
        self.assertEqual(source["earned"], 0)


class CacheTests(unittest.TestCase):
    def test_roundtrip_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            steam.save_cache(directory, 440, {"achievement": {"source": "steam"}})
            path = Path(directory) / "cache" / "steam" / "440.json"
            self.assertTrue(path.is_file())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(steam.load_cache(directory, 440), {"achievement": {"source": "steam"}})

    def test_ttl_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            steam.save_cache(directory, 440, {"ok": True})
            self.assertIsNone(steam.load_cache(directory, 440, now=time.time() + steam.STEAM_CACHE_TTL + 5))

    def test_malformed_and_non_object_cache_reads_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache" / "steam" / "440.json"
            path.parent.mkdir(parents=True)
            path.write_text("{broken", encoding="utf-8")
            self.assertIsNone(steam.load_cache(directory, 440))
            path.write_text("[1, 2]", encoding="utf-8")
            self.assertIsNone(steam.load_cache(directory, 440))
            path.unlink()
            self.assertIsNone(steam.load_cache(directory, 440))

    def test_get_achievements_uses_cache_on_second_call(self):
        with tempfile.TemporaryDirectory() as directory:
            opener = Opener(SCHEMA_PAYLOAD, PLAYER_PAYLOAD)
            with _fast_requests():
                first = steam.get_achievements(440, "7656", data_dir=directory, opener=opener,
                                               api_key="K", sleep=lambda _: None, clock=lambda: 100.0)
            self.assertEqual(first["earned"], 1)

            def forbidden(*_args, **_kwargs):
                raise AssertionError("cache hit must not touch the network")

            with _fast_requests():
                second = steam.get_achievements(440, "7656", data_dir=directory, opener=forbidden,
                                                api_key="K", sleep=lambda _: None, clock=lambda: 100.0)
            self.assertEqual(second, first)


class StatsMergeTests(unittest.TestCase):
    def test_playtime_takes_the_larger_value(self):
        games = [
            {"steam_app_id": "440", "playtime_seconds": 100},
            {"steam_app_id": "570", "playtime_seconds": 9999},
        ]
        owned = [
            {"appid": 440, "playtime_forever": 5},
            {"appid": 570, "playtime_forever": 1},
        ]
        steam.merge_steam_stats(games, owned)
        self.assertEqual(games[0]["playtime_seconds"], 300)
        self.assertEqual(games[1]["playtime_seconds"], 9999)

    def test_last_played_takes_the_newer_value(self):
        newer = datetime(2024, 1, 1, 12, 0, 0)
        older = datetime(2020, 1, 1, 12, 0, 0)
        games = [
            {"steam_app_id": 1, "last_played": older.isoformat()},
            {"steam_app_id": 2, "last_played": newer.isoformat()},
        ]
        owned = [
            {"appid": 1, "rtime_last_played": int(newer.timestamp())},
            {"appid": 2, "rtime_last_played": int(older.timestamp())},
        ]
        steam.merge_steam_stats(games, owned)
        self.assertEqual(games[0]["last_played"], newer.isoformat(timespec="seconds"))
        self.assertEqual(games[1]["last_played"], newer.isoformat())

    def test_zero_and_missing_values_never_lower_or_delete(self):
        games = [{
            "steam_app_id": 7,
            "playtime_seconds": 42,
            "last_played": "2022-05-01T10:00:00",
        }]
        steam.merge_steam_stats(games, [{"appid": 7, "playtime_forever": 0, "rtime_last_played": 0}])
        self.assertEqual(games[0]["playtime_seconds"], 42)
        self.assertEqual(games[0]["last_played"], "2022-05-01T10:00:00")
        steam.merge_steam_stats(games, [])
        self.assertEqual(games[0]["playtime_seconds"], 42)

    def test_unmatched_games_are_untouched(self):
        game = {"name": "Local", "playtime_seconds": 10}
        steam.merge_steam_stats([game], [{"appid": 440, "playtime_forever": 5}])
        self.assertEqual(game, {"name": "Local", "playtime_seconds": 10})


if __name__ == "__main__":
    unittest.main()
