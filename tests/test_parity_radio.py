"""Tests for Backlog Radio + abandonment radar (parity_radio) — T4 / 1.11."""

import io
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity import parity_radio  # noqa: E402

NOW = datetime(2026, 9, 12, 20, 0, 0, tzinfo=timezone.utc)  # evening daypart


def _game(
    gid,
    name,
    genre="Platformer",
    platform="SNES",
    rating=0,
    favorite=False,
    play_count=0,
    playtime=0,
    last_played=None,
    added_at="2026-06-01",
    progress="",
    series="",
    developer="",
    publisher="",
    hidden=False,
    path_exists=True,
):
    return {
        "id": gid,
        "game_id": str(gid),
        "name": name,
        "genre": genre,
        "platform": platform,
        "rating": rating,
        "favorite": favorite,
        "play_count": play_count,
        "playtime_seconds": playtime,
        "last_played": last_played,
        "added_at": added_at,
        "progress": progress,
        "series": series,
        "developer": developer,
        "publisher": publisher,
        "hidden": hidden,
        "path_exists": path_exists,
        "has_cover": True,
    }


def _entry(gid, started, seconds=1800, note=""):
    return {"game_id": str(gid), "started": started, "seconds": seconds, "note": note}


def _iso(days_ago, hour=20):
    return (NOW - timedelta(days=days_ago, hours=NOW.hour - hour)).isoformat()


def _platformer_habit():
    """A player whose real habit is evening platformer sessions."""
    games = [
        # Loved + recently played platformers (the habit).
        _game(1, "Celeste", genre="Platformer", platform="PC", rating=5, favorite=True,
              play_count=40, playtime=40 * 3600, last_played=_iso(3), developer="Matt Makes Games"),
        _game(2, "Super Meat Boy", genre="Platformer", platform="PC",
              play_count=30, playtime=20 * 3600, last_played=_iso(6), progress="Beaten"),
        _game(3, "Shovel Knight", genre="Platformer", platform="Switch",
              play_count=15, playtime=10 * 3600, last_played=_iso(10)),
        # Unplayed platformers — the picks the radio should surface.
        _game(10, "Hollow Knight", genre="Platformer", platform="PC", developer="Team Cherry"),
        _game(11, "Cave Story", genre="Platformer", platform="PC"),
        _game(12, "VVVVVV", genre="Platformer", platform="Switch"),
        _game(13, "Shank", genre="Platformer", platform="PC"),
        _game(14, "Fez", genre="Platformer, Puzzle", platform="PC"),
        _game(15, "Ori", genre="Platformer", platform="PC"),
        # Unplayed off-genre games — should lose to platformers.
        _game(20, "Huge RPG", genre="RPG", platform="PC", rating=4),
        _game(21, "Slow Tactics", genre="Strategy", platform="PC"),
        _game(22, "Farm Sim", genre="Simulation", platform="PC"),
    ]
    history = []
    for day in range(1, 45, 3):
        history.append(_entry(1, _iso(day, hour=20), 2400))
        history.append(_entry(2, _iso(day, hour=21), 2100))
        history.append(_entry(3, _iso(day, hour=22), 1800))
    return games, history


class HabitModelTest(unittest.TestCase):
    def test_genre_and_platform_shares_from_history(self):
        games, history = _platformer_habit()
        model = parity_radio.build_habit_model(games, history, now=NOW)
        self.assertGreaterEqual(model["sessions"], 8)
        self.assertEqual(model["top_genre"], "Platformer")
        self.assertEqual(model["top_platform"], "PC")
        self.assertGreater(model["median_session_min"], 0)

    def test_window_excludes_old_sessions(self):
        games = [_game(1, "A"), _game(2, "B")]
        history = [_entry(1, _iso(200), 3600)]
        model = parity_radio.build_habit_model(games, history, now=NOW)
        self.assertEqual(model["sessions"], 0)
        self.assertFalse(model["enough"])

    def test_enough_history_threshold(self):
        games = [_game(1, "A")]
        thin = [_entry(1, _iso(1), 600) for _ in range(3)]
        model = parity_radio.build_habit_model(games, thin, now=NOW)
        self.assertFalse(model["enough"])
        full = [_entry(1, _iso(d), 600) for d in range(1, 10)]
        model = parity_radio.build_habit_model(games, full, now=NOW)
        self.assertTrue(model["enough"])

    def test_recently_loved_favorite_recent(self):
        games = [
            _game(1, "Loved", favorite=True, last_played=_iso(2), play_count=5, playtime=5000),
            _game(2, "Stale Fav", favorite=True, last_played=_iso(200), play_count=5, playtime=5000),
            _game(3, "Meh", play_count=1, playtime=60, last_played=_iso(1)),
        ]
        model = parity_radio.build_habit_model(games, [], now=NOW)
        self.assertIn("1", model["loved"])
        self.assertNotIn("2", model["loved"])

    def test_recently_loved_falls_back_to_playtime(self):
        games = [
            _game(1, "Heavy", play_count=9, playtime=90000, last_played=_iso(2)),
            _game(2, "Light", play_count=1, playtime=60, last_played=_iso(1)),
        ]
        model = parity_radio.build_habit_model(games, [], now=NOW)
        self.assertEqual(model["loved"], ["1"])

    def test_daypart_histogram(self):
        games = [_game(1, "A")]
        history = [_entry(1, _iso(1, hour=h), 600) for h in (9, 9, 21)]
        model = parity_radio.build_habit_model(games, history, now=NOW)
        self.assertEqual(model["dayparts"].get("morning", 0) > model["dayparts"].get("evening", 0), True)

    def test_bad_entries_skipped(self):
        games = [_game(1, "A")]
        history = [{"game_id": 1, "started": "not-a-date", "seconds": 10},
                   {"started": _iso(1)},
                   "garbage",
                   _entry("ghost", _iso(1), 100)]
        model = parity_radio.build_habit_model(games, history, now=NOW)
        # Unknown game still counts toward session volume, contributes no genre.
        self.assertEqual(model["sessions"], 2)

    def test_determinism(self):
        games, history = _platformer_habit()
        a = parity_radio.build_habit_model(games, history, now=NOW)
        b = parity_radio.build_habit_model(games, history, now=NOW)
        self.assertEqual(a["genre_seconds"], b["genre_seconds"])
        self.assertEqual(a["median_session_min"], b["median_session_min"])


class ScoringTest(unittest.TestCase):
    def test_platformer_habit_picks_platformers(self):
        """Spec acceptance: synthetic platformer-habit → platformer picks."""
        games, history = _platformer_habit()
        playlist = parity_radio.build_playlist(games, history, now=NOW)
        self.assertFalse(playlist["fallback"])
        self.assertTrue(playlist["picks"])
        for pick in playlist["picks"]:
            self.assertIn("Platformer", pick["genre"], pick)

    def test_scoring_deterministic(self):
        games, history = _platformer_habit()
        a = parity_radio.build_playlist(games, history, now=NOW)
        b = parity_radio.build_playlist(games, history, now=NOW)
        self.assertEqual([p["game_id"] for p in a["picks"]], [p["game_id"] for p in b["picks"]])
        self.assertEqual([p["score"] for p in a["picks"]], [p["score"] for p in b["picks"]])

    def test_reasons_are_true(self):
        """Reason params must match fixture reality."""
        games, history = _platformer_habit()
        playlist = parity_radio.build_playlist(games, history, now=NOW)
        by_id = {g["game_id"]: g for g in games}
        for pick in playlist["picks"]:
            self.assertTrue(pick["reasons"], pick)
            for reason in pick["reasons"]:
                key = reason["key"]
                params = reason["params"]
                if key == "radio.reason.similar_to":
                    loved = by_id.get(params["source_id"])
                    self.assertIsNotNone(loved)
                    self.assertTrue(pick["game_id"] != params["source_id"])
                elif key in ("radio.reason.genre_habit", "radio.reason.genre_finish"):
                    self.assertIn(params["genre"], by_id[pick["game_id"]]["genre"])
                elif key == "radio.reason.platform_habit":
                    self.assertEqual(params["platform"], by_id[pick["game_id"]]["platform"])
                elif key == "radio.reason.short":
                    self.assertGreater(params["minutes"], 0)
                elif key == "radio.reason.fresh":
                    self.assertGreaterEqual(params["days"], 14)
                else:
                    self.fail(f"unexpected reason {key}")

    def test_similar_to_reason_names_real_game(self):
        games, history = _platformer_habit()
        playlist = parity_radio.build_playlist(games, history, now=NOW)
        similar = [p for p in playlist["picks"]
                   if any(r["key"] == "radio.reason.similar_to" for r in p["reasons"])]
        self.assertTrue(similar, "expected a similar_to pick")
        pick = similar[0]
        reason = next(r for r in pick["reasons"] if r["key"] == "radio.reason.similar_to")
        self.assertEqual(reason["params"]["name"], "Celeste")

    def test_completed_and_hidden_games_never_picked(self):
        games, history = _platformer_habit()
        for g in games:
            if g["game_id"] == "10":
                g["progress"] = "Beaten"
            if g["game_id"] == "11":
                g["hidden"] = True
        playlist = parity_radio.build_playlist(games, history, now=NOW)
        picked = {p["game_id"] for p in playlist["picks"]}
        self.assertNotIn("10", picked)
        self.assertNotIn("11", picked)

    def test_limit_five(self):
        games, history = _platformer_habit()
        playlist = parity_radio.build_playlist(games, history, now=NOW)
        self.assertLessEqual(len(playlist["picks"]), 5)

    def test_no_repeat_against_previous(self):
        games, history = _platformer_habit()
        first = parity_radio.build_playlist(games, history, now=NOW)
        previous = [p["game_id"] for p in first["picks"]]
        second = parity_radio.build_playlist(games, history, previous_ids=previous, now=NOW)
        self.assertTrue(second["picks"])
        self.assertFalse(set(previous) & {p["game_id"] for p in second["picks"]})

    def test_no_repeat_exhaustion_shrinks_honestly(self):
        games, history = _platformer_habit()
        keep = games[:3]
        all_ids = [g["game_id"] for g in keep]
        playlist = parity_radio.build_playlist(keep, history, previous_ids=all_ids, now=NOW)
        self.assertEqual(playlist["picks"], [])
        self.assertEqual(playlist["notice_key"], "radio.notice.empty")

    def test_single_genre_diversity_guard(self):
        """Single-genre library still varies platform/series when possible."""
        games, history = _platformer_habit()
        history = []
        for day in range(1, 40, 4):
            history.append(_entry(1, _iso(day, hour=20), 2400))
            history.append(_entry(3, _iso(day, hour=21), 2100))
        playlist = parity_radio.build_playlist(games, history, now=NOW)
        self.assertTrue(playlist["picks"])
        platforms = {p["platform"] for p in playlist["picks"]}
        self.assertGreater(len(platforms), 1, "diversity guard should span platforms")


class FallbackTest(unittest.TestCase):
    def test_two_week_library_honest_fallback(self):
        """Spec acceptance: thin history → labeled fallback to rating/edge picks."""
        games = [
            _game(1, "New Hotness", rating=5, added_at=_iso(10)[:10]),
            _game(2, "Decent", rating=3, added_at=_iso(9)[:10]),
            _game(3, "Unrated Thing", added_at=_iso(8)[:10]),
        ]
        history = [_entry(1, _iso(2), 1200), _entry(2, _iso(1), 900)]
        playlist = parity_radio.build_playlist(games, history, now=NOW)
        self.assertTrue(playlist["fallback"])
        self.assertEqual(playlist["notice_key"], "radio.notice.not_enough_history")
        self.assertTrue(playlist["picks"])
        for pick in playlist["picks"]:
            keys = {r["key"] for r in pick["reasons"]}
            self.assertTrue(keys & {"radio.reason.fallback_rating", "radio.reason.fallback_fresh"})
        self.assertEqual(playlist["picks"][0]["name"], "New Hotness")

    def test_empty_history_fallback_labeled(self):
        games = [_game(1, "A", rating=4), _game(2, "B")]
        playlist = parity_radio.build_playlist(games, [], now=NOW)
        self.assertTrue(playlist["fallback"])
        self.assertEqual(playlist["notice_key"], "radio.notice.not_enough_history")
        for pick in playlist["picks"]:
            for reason in pick["reasons"]:
                self.assertIn(reason["key"], ("radio.reason.fallback_rating", "radio.reason.fallback_fresh"))

    def test_empty_library(self):
        playlist = parity_radio.build_playlist([], [], now=NOW)
        self.assertEqual(playlist["picks"], [])
        self.assertTrue(playlist["fallback"])

    def test_fallback_rating_reason_truth(self):
        games = [_game(1, "A", rating=5)]
        playlist = parity_radio.build_playlist(games, [], now=NOW)
        pick = playlist["picks"][0]
        reason = pick["reasons"][0]
        self.assertEqual(reason["key"], "radio.reason.fallback_rating")
        self.assertEqual(reason["params"]["rating"], 5)


class RadarTest(unittest.TestCase):
    def _fixture(self):
        games = [
            # Winnable: 60min into a 120min RPG, stalled 20d → invested ≥25%.
            _game(1, "Half-Finished RPG", genre="RPG", platform="PC",
                  play_count=4, playtime=3600, last_played=_iso(20)),
            # Park: 15min into a 120min RPG stalled 45d → <15% invested.
            _game(2, "Bounced-Off RPG", genre="RPG", platform="PC",
                  play_count=1, playtime=900, last_played=_iso(45)),
            # Excluded: already beaten.
            _game(3, "Done", genre="RPG", platform="PC", progress="Beaten",
                  play_count=9, playtime=3600, last_played=_iso(40)),
            # Excluded: still warm (5d).
            _game(4, "Active", genre="RPG", platform="PC",
                  play_count=2, playtime=3000, last_played=_iso(5)),
            # Excluded: barely started (<15min observed).
            _game(5, "Peeked", genre="RPG", platform="PC",
                  play_count=1, playtime=300, last_played=_iso(30)),
            # Completed 60-min game → the user's demonstrated finish budget.
            _game(6, "Finished Short", genre="Puzzle", platform="PC", progress="Completed",
                  play_count=3, playtime=3600, last_played=_iso(30)),
        ]
        history = [_entry(1, _iso(d), 1800) for d in (20, 22, 24)] + [_entry(2, _iso(45), 900)]
        return games, history

    def test_winnable_vs_park_partition(self):
        games, history = self._fixture()
        result = parity_radio.radar(games, history, now=NOW)
        winnable_ids = {g["game_id"] for g in result["winnable"]}
        park_ids = {g["game_id"] for g in result["park"]}
        self.assertIn("1", winnable_ids)
        self.assertIn("2", park_ids)
        self.assertFalse(winnable_ids & park_ids)
        for excluded in ("3", "4", "5"):
            self.assertNotIn(excluded, winnable_ids)
            self.assertNotIn(excluded, park_ids)

    def test_radar_reasons_true(self):
        games, history = self._fixture()
        result = parity_radio.radar(games, history, now=NOW)
        for group in ("winnable", "park"):
            for item in result[group]:
                self.assertGreaterEqual(item["days_stalled"], 14)
                self.assertGreaterEqual(item["observed_seconds"], 900)
                keys = {r["key"] for r in item["reasons"]}
                expected = "radio.reason.winnable" if group == "winnable" else "radio.reason.park"
                self.assertIn(expected, keys)

    def test_radar_thin_history_honest(self):
        games = [_game(1, "A", play_count=1, playtime=3600, last_played=_iso(30))]
        result = parity_radio.radar(games, [], now=NOW)
        self.assertFalse(result["enough_history"])
        self.assertEqual(result["winnable"], [])
        self.assertEqual(result["park"], [])

    def test_radar_excludes_parked(self):
        games, history = self._fixture()
        result = parity_radio.radar(games, history, parked_ids={"1"}, now=NOW)
        self.assertNotIn("1", {g["game_id"] for g in result["winnable"]})

    def test_radar_hidden_excluded(self):
        games, history = self._fixture()
        games[0]["hidden"] = True
        result = parity_radio.radar(games, history, now=NOW)
        self.assertNotIn("1", {g["game_id"] for g in result["winnable"]})


class ManagedPlaylistTest(unittest.TestCase):
    def _state(self, games=None, history=None):
        return {"games": games or [], "history": history or [],
                "playlists": [], "settings": {}, "profiles": {}}

    def test_materialize_writes_managed_playlist(self):
        games, history = _platformer_habit()
        state = self._state(games, history)
        playlist = parity_radio.build_playlist(games, history, now=NOW)
        entry = parity_radio.materialize_playlist(state, playlist, now=NOW)
        self.assertEqual(entry["name"], parity_radio.RADIO_PLAYLIST_NAME)
        self.assertEqual(entry["managed"], "radio")
        self.assertEqual(entry["type"], "manual")
        self.assertEqual(entry["members"], [p["game_id"] for p in playlist["picks"]])
        self.assertIn(entry, state["playlists"])

    def test_materialize_replaces_existing(self):
        games, history = _platformer_habit()
        state = self._state(games, history)
        first = parity_radio.build_playlist(games, history, now=NOW)
        parity_radio.materialize_playlist(state, first, now=NOW)
        second = parity_radio.build_playlist(
            games, history, previous_ids=[p["game_id"] for p in first["picks"]], now=NOW)
        parity_radio.materialize_playlist(state, second, now=NOW)
        managed = [p for p in state["playlists"] if p.get("managed") == "radio"]
        self.assertEqual(len(managed), 1)
        self.assertEqual(managed[0]["members"], [p["game_id"] for p in second["picks"]])

    def test_materialize_preserves_user_playlists(self):
        games, history = _platformer_habit()
        state = self._state(games, history)
        state["playlists"].append({"name": "Mine", "type": "manual", "members": ["1"]})
        playlist = parity_radio.build_playlist(games, history, now=NOW)
        parity_radio.materialize_playlist(state, playlist, now=NOW)
        names = {p["name"] for p in state["playlists"]}
        self.assertIn("Mine", names)
        self.assertIn(parity_radio.RADIO_PLAYLIST_NAME, names)

    def test_stale_after_week(self):
        entry = {"generated_at": (NOW - timedelta(days=8)).isoformat()}
        self.assertTrue(parity_radio.playlist_stale(entry, now=NOW))
        fresh = {"generated_at": (NOW - timedelta(days=2)).isoformat()}
        self.assertFalse(parity_radio.playlist_stale(fresh, now=NOW))
        self.assertTrue(parity_radio.playlist_stale({"generated_at": "junk"}, now=NOW))
        self.assertTrue(parity_radio.playlist_stale({}, now=NOW))

    def test_managed_playlist_lookup(self):
        state = {"playlists": [{"name": "x"}, {"name": "r", "managed": "radio"}]}
        self.assertEqual(parity_radio.managed_playlist(state)["name"], "r")
        self.assertIsNone(parity_radio.managed_playlist({"playlists": [{"name": "x"}]}))
        self.assertIsNone(parity_radio.managed_playlist({}))

    def test_radio_payload_regen_no_repeat(self):
        games, history = _platformer_habit()
        state = self._state(games, history)
        first = parity_radio.radio_payload(state, now=NOW, force=True)
        second = parity_radio.radio_payload(state, now=NOW, force=True)
        first_ids = {p["game_id"] for p in first["picks"]}
        second_ids = {p["game_id"] for p in second["picks"]}
        self.assertFalse(first_ids & second_ids)

    def test_radio_payload_fresh_reuse(self):
        games, history = _platformer_habit()
        state = self._state(games, history)
        first = parity_radio.radio_payload(state, now=NOW, force=True)
        again = parity_radio.radio_payload(state, now=NOW)
        self.assertEqual([p["game_id"] for p in first["picks"]],
                         [p["game_id"] for p in again["picks"]])

    def test_entry_payload_drops_removed_games(self):
        games, history = _platformer_habit()
        state = self._state(games, history)
        payload = parity_radio.radio_payload(state, now=NOW, force=True)
        removed = payload["picks"][0]["game_id"]
        state["games"] = [g for g in games if g["game_id"] != removed]
        hydrated = parity_radio.entry_payload(parity_radio.managed_playlist(state), state)
        self.assertNotIn(removed, {p["game_id"] for p in hydrated["picks"]})

    def test_park_game(self):
        games, history = _platformer_habit()
        state = self._state(games, history)
        result = parity_radio.park_game(state, "10", now=NOW)
        self.assertTrue(result["ok"])
        self.assertIn("10", parity_radio.parked_ids(state))
        game = next(g for g in state["games"] if g["game_id"] == "10")
        self.assertEqual(game["progress"], "Paused")

    def test_park_missing_game(self):
        state = self._state([_game(1, "A")], [])
        self.assertIsNone(parity_radio.park_game(state, "ghost", now=NOW))

    def test_park_completed_refused(self):
        state = self._state([_game(1, "A", progress="Beaten")], [])
        result = parity_radio.park_game(state, "1", now=NOW)
        self.assertFalse(result["ok"])
        self.assertNotIn("1", parity_radio.parked_ids(state))


class HandlerTest(unittest.TestCase):
    """Route handlers in handlers/insights.py."""

    def _handler(self, body=b"{}"):
        import web_app
        h = web_app.Handler.__new__(web_app.Handler)
        h.responses = []
        h.headers = {"Content-Length": str(len(body))}
        h.rfile = io.BytesIO(body)
        h.send_json = lambda status, payload: h.responses.append((status, payload))
        return h

    def _parsed(self, path="/api/v2/insights/radio"):
        from urllib.parse import urlsplit
        return urlsplit(path)

    def _state(self):
        games, history = _platformer_habit()
        return {"games": games, "history": history,
                "playlists": [], "settings": {}, "profiles": {}}

    def test_get_radio_creates_managed_playlist(self):
        import handlers.insights as mod
        state = self._state()
        h = self._handler()
        with mock.patch.object(mod, "load_state", return_value=state), \
                mock.patch.object(mod, "transact_state",
                                  side_effect=lambda fn: (state, fn(state))):
            h._api_get_api_v2_insights_radio(self._parsed())
        status, payload = h.responses[0]
        self.assertEqual(status, 200)
        self.assertIn("playlist", payload)
        self.assertTrue(payload["playlist"]["picks"])
        managed = [p for p in state["playlists"] if p.get("managed") == "radio"]
        self.assertEqual(len(managed), 1)
        members = managed[0]["members"]
        self.assertEqual(members, [p["game_id"] for p in payload["playlist"]["picks"]])

    def test_get_radio_reuses_fresh(self):
        import handlers.insights as mod
        state = self._state()
        parity_radio.materialize_playlist(
            state,
            parity_radio.build_playlist(state["games"], state["history"], now=NOW),
            now=NOW)
        h = self._handler()
        called = []
        with mock.patch.object(mod, "load_state", return_value=state), \
                mock.patch.object(mod, "transact_state",
                                  side_effect=lambda fn: called.append(fn) or (state, fn(state))):
            h._api_get_api_v2_insights_radio(self._parsed())
        status, payload = h.responses[0]
        self.assertEqual(status, 200)
        self.assertFalse(called, "fresh playlist must not rewrite state")

    def test_post_refresh_no_repeat(self):
        import handlers.insights as mod
        state = self._state()
        parity_radio.materialize_playlist(
            state,
            parity_radio.build_playlist(state["games"], state["history"], now=NOW),
            now=NOW)
        before = set(parity_radio.managed_playlist(state)["members"])
        h = self._handler()
        with mock.patch.object(mod, "transact_state",
                               side_effect=lambda fn: (state, fn(state))):
            h._api_post_api_v2_insights_radio_refresh({})
        status, payload = h.responses[0]
        self.assertEqual(status, 200)
        after = {p["game_id"] for p in payload["playlist"]["picks"]}
        self.assertFalse(before & after)

    def test_get_radar(self):
        import handlers.insights as mod
        state = self._state()
        h = self._handler()
        with mock.patch.object(mod, "load_state", return_value=state):
            h._api_get_api_v2_insights_radar(self._parsed("/api/v2/insights/radar"))
        status, payload = h.responses[0]
        self.assertEqual(status, 200)
        self.assertIn("radar", payload)
        self.assertIn("winnable", payload["radar"])
        self.assertIn("park", payload["radar"])

    def test_post_park(self):
        import handlers.insights as mod
        state = self._state()
        h = self._handler()
        with mock.patch.object(mod, "transact_state",
                               side_effect=lambda fn: (state, fn(state))):
            h._api_post_api_v2_insights_radar_park({"game_id": "10"})
        status, payload = h.responses[0]
        self.assertEqual(status, 200)
        self.assertTrue(payload["parked"])
        self.assertIn("10", parity_radio.parked_ids(state))

    def test_post_park_bad_body(self):
        from api_errors import BadRequest
        h = self._handler()
        with self.assertRaises(BadRequest):
            h._api_post_api_v2_insights_radar_park({})
        with self.assertRaises(BadRequest):
            h._api_post_api_v2_insights_radar_park("nope")

    def test_post_park_unknown_game(self):
        import handlers.insights as mod
        from api_errors import GameNotFound
        state = self._state()
        h = self._handler()
        with mock.patch.object(mod, "transact_state",
                               side_effect=lambda fn: (state, fn(state))):
            with self.assertRaises(GameNotFound):
                h._api_post_api_v2_insights_radar_park({"game_id": "ghost"})


class RouteTableTest(unittest.TestCase):
    def test_routes_in_tables_and_registry(self):
        from routes import GET_TABLE, POST_TABLE
        from routes.registry import get_routes_for_method
        self.assertEqual(GET_TABLE["/api/v2/insights/radio"], "_api_get_api_v2_insights_radio")
        self.assertEqual(GET_TABLE["/api/v2/insights/radar"], "_api_get_api_v2_insights_radar")
        self.assertEqual(POST_TABLE["/api/v2/insights/radio/refresh"], "_api_post_api_v2_insights_radio_refresh")
        self.assertEqual(POST_TABLE["/api/v2/insights/radar/park"], "_api_post_api_v2_insights_radar_park")
        self.assertIn("/api/v2/insights/radio", get_routes_for_method("GET"))
        self.assertIn("/api/v2/insights/radio/refresh", get_routes_for_method("POST"))


if __name__ == "__main__":
    unittest.main()
