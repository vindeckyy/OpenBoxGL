"""Tests for the RetroArch netplay lobby browser (parity_netplay / P12.1)."""

from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity import parity_netplay  # noqa: E402

BASE_FIELDS = {
    "username": "Akiro",
    "game_name": "Pokemon Quetzal",
    "core_name": "mGBA",
    "ip": "38.25.85.191",
    "port": 55435,
    "mitm_ip": "us-east1.relay.retroarch.com",
    "mitm_port": 55435,
    "player_count": 1,
    "has_password": False,
    "connectable": True,
}


def _entry(**overrides):
    fields = dict(BASE_FIELDS)
    fields.update(overrides)
    return {"fields": fields}


class _Response:
    def __init__(self, payload):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload).encode("utf-8")
        self.payload = payload
        self.closed = False

    def read(self, *_args):
        return self.payload

    def close(self):
        self.closed = True


class _Opener:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = 0
        self.timeouts = []

    def __call__(self, url, timeout=None):
        self.calls += 1
        self.timeouts.append(timeout)
        item = self.payloads.pop(0) if self.payloads else []
        if isinstance(item, Exception):
            raise item
        return _Response(item)


class LobbyTests(unittest.TestCase):
    def setUp(self):
        parity_netplay.reset_cache()

    def test_lobby_url_is_https(self):
        self.assertTrue(parity_netplay.LOBBY_URL.startswith("https://"))
        self.assertEqual(parity_netplay.LOBBY_URL, "https://lobby.retroarch.com/api/lobby")

    def test_maps_fields_and_prefers_mitm_host(self):
        opener = _Opener([_entry()])
        result = parity_netplay.lobby(opener=opener)
        self.assertTrue(result["available"])
        self.assertEqual(result["rows"], [{
            "host": "us-east1.relay.retroarch.com",
            "port": 55435,
            "nick": "Akiro",
            "game": "Pokemon Quetzal",
            "core": "mGBA",
            "players": 1,
            "private": False,
        }])
        self.assertEqual(opener.timeouts, [10])
        self.assertEqual(opener.calls, 1)

    def test_falls_back_to_direct_ip_and_port(self):
        opener = _Opener([_entry(mitm_ip="", mitm_port=None, ip="98.97.59.245", port="55436")])
        rows = parity_netplay.lobby(opener=opener)["rows"]
        self.assertEqual(rows[0]["host"], "98.97.59.245")
        self.assertEqual(rows[0]["port"], 55436)

    def test_only_connectable_rooms_are_kept(self):
        opener = _Opener([
            _entry(username="ok"),
            _entry(username="no", connectable=False),
            _entry(username="string", connectable="false"),
            {"fields": {k: v for k, v in BASE_FIELDS.items() if k != "connectable"}},
            {"no_fields": {}},
            "junk",
        ])
        rows = parity_netplay.lobby(opener=opener)["rows"]
        self.assertEqual([row["nick"] for row in rows], ["ok"])

    def test_bad_host_and_port_rows_are_dropped(self):
        bad_hosts = ["", "bad host", "http://example.com", "host/path", "-leading.example", "example..com"]
        bad_ports = [0, -1, 65536, "abc", True, None]
        entries = [_entry(username=f"host{i}", mitm_ip=host, ip="") for i, host in enumerate(bad_hosts)]
        entries += [
            _entry(username=f"port{i}", mitm_port=port, port=port)
            for i, port in enumerate(bad_ports)
        ]
        opener = _Opener(entries)
        result = parity_netplay.lobby(opener=opener)
        self.assertFalse(result["available"])
        self.assertEqual(result["error"], parity_netplay.LOBBY_ERROR)

    def test_accepts_ip_and_hostname_hosts(self):
        opener = _Opener([
            _entry(username="v4", mitm_ip="203.0.113.7"),
            _entry(username="v6", mitm_ip="2001:db8::1"),
            _entry(username="host", mitm_ip="relay.example.org"),
            _entry(username="zone", mitm_ip="localhost"),
        ])
        rows = parity_netplay.lobby(opener=opener)["rows"]
        self.assertEqual([row["nick"] for row in rows], ["v4", "v6", "host", "zone"])

    def test_control_characters_are_stripped(self):
        opener = _Opener([_entry(
            username="Ak\x00iro\r\n",
            game_name="Ga\x07me\x1b",
            core_name="mG\x9fBA\t",
        )])
        row = parity_netplay.lobby(opener=opener)["rows"][0]
        self.assertEqual(row["nick"], "Akiro")
        self.assertEqual(row["game"], "Game")
        self.assertEqual(row["core"], "mGBA")

    def test_players_and_private_are_normalized(self):
        opener = _Opener([
            _entry(player_count=3, has_password=True),
            _entry(player_count="4", has_password="true"),
            _entry(player_count=-2, has_password=False),
            _entry(player_count=True, has_password="false"),
        ])
        rows = parity_netplay.lobby(opener=opener)["rows"]
        self.assertEqual([row["players"] for row in rows], [3, 4, 0, 0])
        self.assertEqual([row["private"] for row in rows], [True, True, False, False])

    def test_cache_reuses_result_for_sixty_seconds(self):
        clock = [100.0]
        opener = _Opener([_entry(username="first")], [_entry(username="second")])
        first = parity_netplay.lobby(opener=opener, now=lambda: clock[0])
        clock[0] = 159.0
        second = parity_netplay.lobby(opener=opener, now=lambda: clock[0])
        self.assertEqual(opener.calls, 1)
        self.assertEqual(first["rows"], second["rows"])
        self.assertEqual(second["rows"][0]["nick"], "first")
        clock[0] = 160.0
        third = parity_netplay.lobby(opener=opener, now=lambda: clock[0])
        self.assertEqual(opener.calls, 2)
        self.assertEqual(third["rows"][0]["nick"], "second")

    def test_cached_rows_are_copies(self):
        opener = _Opener([_entry()])
        parity_netplay.lobby(opener=opener)
        cached = parity_netplay.lobby(opener=opener)
        cached["rows"][0]["nick"] = "mutated"
        again = parity_netplay.lobby(opener=opener)
        self.assertEqual(again["rows"][0]["nick"], "Akiro")
        self.assertEqual(opener.calls, 1)

    def test_reset_cache_forces_refetch(self):
        opener = _Opener([_entry(username="one")], [_entry(username="two")])
        parity_netplay.lobby(opener=opener)
        parity_netplay.reset_cache()
        result = parity_netplay.lobby(opener=opener)
        self.assertEqual(opener.calls, 2)
        self.assertEqual(result["rows"][0]["nick"], "two")

    def test_http_error_returns_unavailable(self):
        opener = _Opener(HTTPError(parity_netplay.LOBBY_URL, 503, "down", {}, io.BytesIO(b"")))
        result = parity_netplay.lobby(opener=opener)
        self.assertEqual(result, {"available": False, "error": "Netplay lobby is unavailable."})

    def test_timeout_returns_unavailable(self):
        opener = _Opener(TimeoutError("timed out"))
        result = parity_netplay.lobby(opener=opener)
        self.assertEqual(result["available"], False)
        self.assertEqual(result["error"], parity_netplay.LOBBY_ERROR)

    def test_url_error_returns_unavailable(self):
        opener = _Opener(URLError("offline"))
        self.assertFalse(parity_netplay.lobby(opener=opener)["available"])

    def test_malformed_payload_returns_unavailable(self):
        for payload in (b"not json", {"fields": []}, {"data": []}, None):
            parity_netplay.reset_cache()
            opener = _Opener(payload)
            result = parity_netplay.lobby(opener=opener)
            self.assertFalse(result["available"], payload)
            self.assertEqual(result["error"], parity_netplay.LOBBY_ERROR)

    def test_empty_listing_is_unavailable_and_not_cached(self):
        opener = _Opener([], [_entry(username="later")])
        first = parity_netplay.lobby(opener=opener)
        self.assertEqual(first, {"available": False, "error": parity_netplay.LOBBY_ERROR})
        second = parity_netplay.lobby(opener=opener)
        self.assertTrue(second["available"])
        self.assertEqual(second["rows"][0]["nick"], "later")
        self.assertEqual(opener.calls, 2)

    def test_rows_are_capped_at_five_hundred(self):
        entries = [_entry(username=f"room{i}") for i in range(600)]
        rows = parity_netplay.lobby(opener=_Opener(entries))["rows"]
        self.assertEqual(len(rows), 500)
        self.assertEqual(rows[0]["nick"], "room0")
        self.assertEqual(rows[-1]["nick"], "room499")

    def test_missing_and_oversized_text_is_bounded(self):
        opener = _Opener([_entry(username="x" * 500, game_name=None, core_name=123)])
        row = parity_netplay.lobby(opener=opener)["rows"][0]
        self.assertEqual(len(row["nick"]), parity_netplay.TEXT_LIMITS["nick"])
        self.assertEqual(row["game"], "")
        self.assertEqual(row["core"], "123")


if __name__ == "__main__":
    unittest.main()
