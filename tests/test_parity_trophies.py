"""Tests for S6 launcher trophies — parity_trophies rules, persistence, routes.

Pure rule-matrix tests plus a real-HTTP boundary suite for the two
``/api/v2/insights/trophies*`` routes in ``handlers/insights.py``. Runs
standalone via ``python3 -B tests/test_parity_trophies.py``.
"""

import json
import os
import queue
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity import parity_trophies  # noqa: E402


def _game(game_id, **kw):
    game = {"game_id": game_id, "name": game_id}
    game.update(kw)
    return game


def _hist(game_id="g1", started="2026-01-05T20:00:00", seconds=600):
    return {"game_id": game_id, "started": started, "seconds": seconds}


def _played(game_id, **kw):
    kw.setdefault("playtime_seconds", 600)
    return _game(game_id, **kw)


def _awarded_ids(entries):
    return {e["id"] for e in entries if e["awarded"]}


class RuleTableTest(unittest.TestCase):
    def test_rule_ids_unique_and_stable(self):
        ids = [r["id"] for r in parity_trophies.TROPHY_RULES]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids, sorted(ids, key=ids.index))  # declaration order kept
        for rule in parity_trophies.TROPHY_RULES:
            self.assertTrue(rule["id"].isidentifier() or "_" in rule["id"])
            self.assertGreater(rule["target"], 0)
            self.assertTrue(rule["stat"])

    def test_fresh_library_awards_nothing(self):
        entries = parity_trophies.evaluate([], [])
        self.assertEqual(len(entries), len(parity_trophies.TROPHY_RULES))
        self.assertEqual(_awarded_ids(entries), set())
        for entry in entries:
            self.assertEqual(entry["progress"]["current"], 0)
            self.assertEqual(entry["progress"]["target"], entry_target(entry["id"]))

    def test_malformed_entries_skipped(self):
        games = [None, "x", {"name": "no-id"}, _played("ok", year=1985)]
        history = [None, "junk", 42, {"seconds": "bad"}, {"started": "not-a-date", "seconds": 10}]
        entries = parity_trophies.evaluate(games, history)
        # the two dict rows are real sessions (fields sanitize); the lone
        # played pre-1990 game fires old_school; everything else stays dark
        self.assertEqual(_awarded_ids(entries), {"first_launch", "old_school"})

    def test_deterministic_same_input(self):
        games = [_played(f"g{i}", year=1980 + i * 3, platform=f"P{i % 5}") for i in range(30)]
        history = [_hist(f"g{i}", f"2026-02-{(i % 27) + 1:02d}T21:00:00", 900) for i in range(40)]
        self.assertEqual(parity_trophies.evaluate(games, history), parity_trophies.evaluate(games, history))


def entry_target(trophy_id):
    for rule in parity_trophies.TROPHY_RULES:
        if rule["id"] == trophy_id:
            return rule["target"]
    raise AssertionError(f"unknown trophy id {trophy_id}")


class RuleMatrixTest(unittest.TestCase):
    """Each rule fires on its minimal fixture and stays dark one step under target."""

    def check(self, trophy_id, games, history):
        entries = parity_trophies.evaluate(games, history)
        by_id = {e["id"]: e for e in entries}
        self.assertIn(trophy_id, by_id)
        return by_id[trophy_id]

    def test_first_launch(self):
        self.assertTrue(self.check("first_launch", [], [_hist()])["awarded"])
        self.assertFalse(self.check("first_launch", [_played("g")], [])["awarded"])

    def test_decade_tourist(self):
        games = [_played("a", year=1985), _played("b", year=1995), _played("c", year=2005), _played("d", year=2015)]
        self.assertTrue(self.check("decade_tourist", games, [])["awarded"])
        self.assertFalse(self.check("decade_tourist", games[:3], [])["awarded"])
        # unplayed games do not count toward tourism
        unplayed = [_game("u", year=1975)] + games[:3]
        self.assertFalse(self.check("decade_tourist", unplayed, [])["awarded"])

    def test_deep_diver(self):
        self.assertTrue(self.check("deep_diver", [_played("g", playtime_seconds=72000)], [])["awarded"])
        self.assertFalse(self.check("deep_diver", [_played("g", playtime_seconds=71999)], [])["awarded"])

    def test_curator(self):
        games = [_game(f"g{i}", has_cover=True) for i in range(25)]
        self.assertTrue(self.check("curator", games, [])["awarded"])
        self.assertFalse(self.check("curator", games[:24], [])["awarded"])

    def test_collector(self):
        games = [_game(f"g{i}") for i in range(100)]
        self.assertTrue(self.check("collector", games, [])["awarded"])
        self.assertFalse(self.check("collector", games[:99], [])["awarded"])

    def test_marathon(self):
        self.assertTrue(self.check("marathon", [], [_hist(seconds=14400)])["awarded"])
        self.assertFalse(self.check("marathon", [], [_hist(seconds=14399)])["awarded"])

    def test_night_owl(self):
        self.assertTrue(self.check("night_owl", [], [_hist(started="2026-03-01T02:30:00")])["awarded"])
        self.assertFalse(self.check("night_owl", [], [_hist(started="2026-03-01T05:00:00")])["awarded"])
        self.assertFalse(self.check("night_owl", [], [_hist(started="2026-03-01")])["awarded"])

    def test_week_streak(self):
        history = [_hist(started=f"2026-04-0{d}T18:00:00") for d in range(1, 8)]
        self.assertTrue(self.check("week_streak", [], history)["awarded"])
        self.assertFalse(self.check("week_streak", [], history[:6])["awarded"])
        # same-day repeats do not inflate the streak
        doubled = [_hist(started="2026-04-01T08:00:00"), _hist(started="2026-04-01T22:00:00")]
        self.assertFalse(self.check("week_streak", [], doubled)["awarded"])

    def test_completionist(self):
        games = [_played(f"g{i}", progress="beaten") for i in range(4)] + [_played("g5", progress="Completed 100%")]
        self.assertTrue(self.check("completionist", games, [])["awarded"])
        self.assertFalse(self.check("completionist", games[:4], [])["awarded"])

    def test_platform_hopper(self):
        games = [_played(f"g{i}", platform=p) for i, p in enumerate(("SNES", "PS1", "GBA", "Dreamcast"))]
        self.assertTrue(self.check("platform_hopper", games, [])["awarded"])
        self.assertFalse(self.check("platform_hopper", games[:3], [])["awarded"])

    def test_century_club(self):
        games = [_played(f"g{i}", playtime_seconds=36000) for i in range(10)]
        self.assertTrue(self.check("century_club", games, [])["awarded"])
        self.assertFalse(self.check("century_club", games[:9], [])["awarded"])

    def test_old_school(self):
        self.assertTrue(self.check("old_school", [_played("g", year=1989)], [])["awarded"])
        self.assertFalse(self.check("old_school", [_played("g", year=1990)], [])["awarded"])
        self.assertFalse(self.check("old_school", [_game("g", year=1980)], [])["awarded"])

    def test_expected_only_fixture(self):
        """A crafted library awards exactly the expected set — nothing extra."""
        games = [
            _played("g70", year=1978, platform="Atari 2600"),
            _played("g80", year=1985, platform="NES"),
            _played("g90", year=1994, platform="SNES"),
            _played("g00", year=2003, platform="PS2", playtime_seconds=80000),
            _game("shelf1", year=2010),
        ]
        history = [
            _hist("g70", "2026-01-10T20:00:00", 900),
            _hist("g00", "2026-01-24T21:00:00", 3600),
        ]
        awarded = _awarded_ids(parity_trophies.evaluate(games, history))
        self.assertEqual(awarded, {"first_launch", "decade_tourist", "platform_hopper", "deep_diver", "old_school"})

    def test_progress_reports_real_counts(self):
        games = [_played("a", year=1985), _played("b", year=1995), _played("c", year=2005)]
        by_id = {e["id"]: e for e in parity_trophies.evaluate(games, [])}
        self.assertEqual(by_id["decade_tourist"]["progress"], {"current": 3, "target": 4})
        # progress caps at target once met
        games.append(_played("d", year=2015))
        by_id = {e["id"]: e for e in parity_trophies.evaluate(games, [])}
        self.assertEqual(by_id["decade_tourist"]["progress"], {"current": 4, "target": 4})


class PersistenceTest(unittest.TestCase):
    def test_case_merges_stored_awards(self):
        state = {"games": [_played("g")], "history": [_hist()], "trophies": {"awarded": {"first_launch": "2026-01-05T20:00:01"}}}
        case = parity_trophies.trophy_case(state)
        self.assertEqual(case["total"], len(parity_trophies.TROPHY_RULES))
        by_id = {t["id"]: t for t in case["trophies"]}
        self.assertTrue(by_id["first_launch"]["awarded"])
        self.assertEqual(by_id["first_launch"]["awarded_at"], "2026-01-05T20:00:01")
        self.assertIsNone(by_id["collector"]["awarded_at"])
        self.assertFalse(by_id["collector"]["awarded"])
        self.assertEqual(case["earned"], 1)

    def test_awards_are_permanent_once_earned(self):
        state = {"games": [], "history": [], "trophies": {"awarded": {"collector": "2026-01-01T00:00:00"}}}
        case = parity_trophies.trophy_case(state)
        by_id = {t["id"]: t for t in case["trophies"]}
        self.assertTrue(by_id["collector"]["awarded"])
        # but its live progress bar still reports the true (now-zero) count
        self.assertEqual(by_id["collector"]["progress"]["current"], 0)

    def test_record_new_awards_idempotent(self):
        state = {"games": [], "history": [_hist()]}
        first = parity_trophies.record_new_awards(state, now="2026-02-01T00:00:00")
        self.assertEqual([a["id"] for a in first], ["first_launch"])
        second = parity_trophies.record_new_awards(state, now="2026-02-02T00:00:00")
        self.assertEqual(second, [])
        self.assertEqual(state["trophies"]["awarded"]["first_launch"], "2026-02-01T00:00:00")

    def test_record_new_awards_ignores_foreign_keys(self):
        state = {"games": [], "history": [_hist()], "trophies": {"awarded": {"not_a_rule": "2020-01-01T00:00:00"}}}
        parity_trophies.record_new_awards(state, now="2026-02-01T00:00:00")
        self.assertIn("not_a_rule", state["trophies"]["awarded"])
        self.assertIn("first_launch", state["trophies"]["awarded"])


class EdgeCaseTest(unittest.TestCase):
    def test_parse_day_accepts_datetime_and_rejects_garbage(self):
        self.assertEqual(str(parity_trophies._parse_day("2026-03-01T05:00:00")), "2026-03-01")
        self.assertEqual(str(parity_trophies._parse_day("2026-03-01")), "2026-03-01")
        self.assertIsNone(parity_trophies._parse_day("nope"))
        self.assertIsNone(parity_trophies._parse_day(None))
        self.assertIsNone(parity_trophies._parse_day("2026-13-40"))

    def test_bad_year_and_genre_fields(self):
        games = [
            _played("g1", year="abc", genre="RPG, Action"),
            _played("g2", year=None, platform=""),
            _played("g3", year="1992", genre="RPG,RPG"),
        ]
        stats = parity_trophies.collect_stats(games, [])
        self.assertEqual(stats["decades_played"], 1)  # only g3's 1990s count
        self.assertEqual(stats["genres_played"], 2)   # RPG + Action, deduped
        self.assertEqual(stats["platforms_played"], 0)

    def test_stored_awards_tolerates_wrong_types(self):
        self.assertEqual(parity_trophies._stored_awards(None), {})
        self.assertEqual(parity_trophies._stored_awards({"trophies": {"awarded": {"first_launch": ""}}}), {})
        self.assertEqual(parity_trophies.trophy_case(None)["earned"], 0)
        self.assertEqual(parity_trophies.trophy_case({"trophies": "bad"})["earned"], 0)
        self.assertEqual(parity_trophies.trophy_case({"trophies": {"awarded": "bad"}})["earned"], 0)
        self.assertEqual(parity_trophies.trophy_case({"trophies": {"awarded": {7: None}}})["earned"], 0)

    def test_record_new_awards_wrong_types(self):
        self.assertEqual(parity_trophies.record_new_awards(None), [])
        state = {"games": [], "history": [_hist()], "trophies": "corrupt"}
        self.assertEqual(len(parity_trophies.record_new_awards(state)), 1)
        self.assertIsInstance(state["trophies"], dict)
        state2 = {"games": [], "history": [_hist()], "trophies": {"awarded": "corrupt"}}
        parity_trophies.record_new_awards(state2)
        self.assertIsInstance(state2["trophies"]["awarded"], dict)

    def test_rule_ids_helper(self):
        ids = parity_trophies.trophy_rule_ids()
        self.assertEqual(ids, tuple(r["id"] for r in parity_trophies.TROPHY_RULES))

    def test_unrecorded_awards_read_path(self):
        self.assertEqual(parity_trophies.unrecorded_awards(None), [])
        state = {"games": [_played("g", year=1980)], "history": [_hist()]}
        self.assertEqual(set(parity_trophies.unrecorded_awards(state)), {"first_launch", "old_school"})
        parity_trophies.record_new_awards(state, now="2026-02-01T00:00:00")
        self.assertEqual(parity_trophies.unrecorded_awards(state), [])


class PerfTest(unittest.TestCase):
    def test_evaluate_cheap_at_20k(self):
        games = [_played(f"g{i}", year=1975 + (i % 50), platform=f"P{i % 12}", has_cover=bool(i % 3)) for i in range(20000)]
        history = [_hist(f"g{i}", f"2026-01-{(i % 28) + 1:02d}T{(i % 20) + 3:02d}:00:00", 300 + i % 900) for i in range(3000)]
        start = time.perf_counter()
        entries = parity_trophies.evaluate(games, history)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 3.0)
        self.assertEqual(len(entries), len(parity_trophies.TROPHY_RULES))


class I18nContractTest(unittest.TestCase):
    def test_every_rule_has_locale_strings_in_all_five(self):
        for locale in ("en", "es", "de", "fr", "pt"):
            data = json.loads((ROOT / "locales" / f"{locale}.json").read_text())
            rules = data.get("trophy", {}).get("rules", {})
            for rule in parity_trophies.TROPHY_RULES:
                self.assertIn(rule["id"], rules, f"{locale} missing trophy rule {rule['id']}")
                self.assertTrue(rules[rule["id"]].get("name"), f"{locale} trophy {rule['id']} empty name")
                self.assertTrue(rules[rule["id"]].get("desc"), f"{locale} trophy {rule['id']} empty desc")


class TrophyHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.previous_data_dir = os.environ.get("OPENBOX_DATA_DIR")
        os.environ["OPENBOX_DATA_DIR"] = cls.tempdir.name
        os.environ.setdefault("OPENBOX_SAFE_MODE", "1")
        import openbox
        import web_app
        from state_store import JsonStateStore

        cls.openbox = openbox
        cls.web_app = web_app
        cls.previous_store = openbox.STATE_STORE
        cls.data_path = Path(cls.tempdir.name) / "library.json"
        openbox.DATA = cls.data_path
        openbox.STATE_STORE = JsonStateStore(cls.data_path)
        web_app.TOKEN = "trophy-http-token"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web_app.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.openbox.STATE_STORE = cls.previous_store
        cls.tempdir.cleanup()
        if cls.previous_data_dir is None:
            os.environ.pop("OPENBOX_DATA_DIR", None)
        else:
            os.environ["OPENBOX_DATA_DIR"] = cls.previous_data_dir

    def setUp(self):
        self.openbox.save_state({
            "games": [_played("g1", year=1985)],
            "profiles": {},
            "history": [_hist("g1")],
            "settings": {},
            "playlists": [],
            "active_sessions": [],
        })
        from pkg.state import sse
        self.subscriber = queue.Queue()
        sse.register_event_subscriber(self.subscriber)
        self.addCleanup(sse.unregister_event_subscriber, self.subscriber)

    def request(self, method, path, payload=None, token="trophy-http-token"):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers={"Content-Type": "application/json", **({"X-OpenBox-Token": token} if token else {})},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            finally:
                error.close()

    def _drain_events(self):
        events = []
        while True:
            try:
                events.append(self.subscriber.get_nowait())
            except queue.Empty:
                return events

    def test_get_requires_auth(self):
        status, _payload = self.request("GET", "/api/v2/insights/trophies", token=None)
        self.assertEqual(status, 403)

    def test_post_requires_auth(self):
        status, _payload = self.request("POST", "/api/v2/insights/trophies/evaluate", {}, token=None)
        self.assertEqual(status, 403)

    def test_case_shape(self):
        status, payload = self.request("GET", "/api/v2/insights/trophies")
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], len(parity_trophies.TROPHY_RULES))
        self.assertIsInstance(payload["trophies"], list)
        entry = payload["trophies"][0]
        self.assertIn("id", entry)
        self.assertIn("awarded", entry)
        self.assertIn("awarded_at", entry)
        self.assertIn("progress", entry)

    def test_evaluate_persists_and_broadcasts_once(self):
        self._drain_events()
        status, payload = self.request("POST", "/api/v2/insights/trophies/evaluate", {})
        self.assertEqual(status, 200)
        self.assertEqual({a["id"] for a in payload["newly_awarded"]}, {"first_launch", "old_school"})
        self.assertGreaterEqual(payload["earned"], 2)
        events = self._drain_events()
        trophy_events = [e for e in events if e[0] == "trophy.awarded"]
        self.assertEqual(len(trophy_events), 2)
        for _kind, data in trophy_events:
            body = json.loads(data)
            self.assertIn(body["id"], ("first_launch", "old_school"))
            self.assertTrue(body["awarded_at"])
        # second evaluation awards nothing again and takes the no-write path:
        # the on-disk state file must be byte-identical afterwards.
        before = self.data_path.read_bytes()
        status, payload = self.request("POST", "/api/v2/insights/trophies/evaluate", {})
        self.assertEqual(status, 200)
        self.assertEqual(payload["newly_awarded"], [])
        self.assertEqual(self.data_path.read_bytes(), before)
        status, case = self.request("GET", "/api/v2/insights/trophies")
        by_id = {t["id"]: t for t in case["trophies"]}
        self.assertTrue(by_id["first_launch"]["awarded"])
        self.assertTrue(by_id["first_launch"]["awarded_at"])

    def test_fresh_library_empty_case_over_http(self):
        self.openbox.save_state({"games": [], "profiles": {}, "history": [], "settings": {}, "playlists": []})
        status, payload = self.request("GET", "/api/v2/insights/trophies")
        self.assertEqual(status, 200)
        self.assertEqual(payload["earned"], 0)
        for entry in payload["trophies"]:
            self.assertFalse(entry["awarded"])
            self.assertEqual(entry["progress"]["current"], 0)


if __name__ == "__main__":
    unittest.main()
