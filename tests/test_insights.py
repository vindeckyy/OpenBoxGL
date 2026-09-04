"""Tests for Play Insights (parity_insights) — 1.7.1."""

import datetime
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity.parity_insights import (  # noqa: E402
    compute_heatmap,
    compute_streak,
    compute_top_platforms,
    compute_top_genres,
    compute_top_games,
    compute_momentum,
    compute_totals,
    summarize,
)


class HeatmapTest(unittest.TestCase):
    def test_empty_history_produces_zeros(self):
        end = datetime.date(2026, 8, 28)
        hm = compute_heatmap([], days=7, end_date=end)
        self.assertEqual(len(hm), 7)
        for cell in hm:
            self.assertEqual(cell["count"], 0)
            self.assertEqual(cell["seconds"], 0)
            self.assertEqual(cell["level"], 0)

    def test_counts_and_seconds(self):
        history = [
            {"started": "2026-08-28T10:00:00", "seconds": 3600},
            {"started": "2026-08-27T10:00:00", "seconds": 1800},
            {"started": "2026-08-27T14:00:00", "seconds": 600},
        ]
        hm = compute_heatmap(history, days=3, end_date=datetime.date(2026, 8, 28))
        by_date = {c["date"]: c for c in hm}
        self.assertEqual(by_date["2026-08-28"]["count"], 1)
        self.assertEqual(by_date["2026-08-28"]["seconds"], 3600)
        self.assertEqual(by_date["2026-08-27"]["count"], 2)
        self.assertEqual(by_date["2026-08-27"]["seconds"], 2400)

    def test_leap_year(self):
        history = [{"started": "2024-02-29T10:00:00", "seconds": 100}]
        hm = compute_heatmap(history, days=5, end_date=datetime.date(2024, 3, 1))
        by_date = {c["date"]: c for c in hm}
        self.assertEqual(by_date["2024-02-29"]["count"], 1)

    def test_level_range(self):
        history = [{"started": "2026-08-28T10:00:00", "seconds": 100}]
        hm = compute_heatmap(history, end_date=datetime.date(2026, 8, 28))
        for cell in hm:
            self.assertIn(cell["level"], [0, 1, 2, 3, 4])

    def test_days_param(self):
        hm = compute_heatmap([], days=7, end_date=datetime.date(2026, 8, 28))
        self.assertEqual(len(hm), 7)
        self.assertEqual(hm[-1]["date"], "2026-08-28")
        self.assertEqual(hm[0]["date"], "2026-08-22")


class StreakTest(unittest.TestCase):
    def test_streak_current_and_longest(self):
        hm = [
            {"date": "2026-08-26", "count": 1},
            {"date": "2026-08-27", "count": 1},
            {"date": "2026-08-28", "count": 0},
            {"date": "2026-08-29", "count": 1},
            {"date": "2026-08-30", "count": 1},
        ]
        for c in hm:
            c.setdefault("seconds", 10)
            c.setdefault("level", 1)
        streak = compute_streak(hm)
        self.assertEqual(streak["current"], 2)
        self.assertEqual(streak["longest"], 2)
        self.assertEqual(streak["last_played"], "2026-08-30")

    def test_no_history(self):
        hm = [{"date": "2026-08-28", "count": 0, "seconds": 0, "level": 0}]
        streak = compute_streak(hm)
        self.assertEqual(streak["current"], 0)
        self.assertEqual(streak["longest"], 0)


class TotalsTest(unittest.TestCase):
    def test_totals(self):
        games = [
            {"platform": "PC", "genre": "RPG", "playtime_seconds": 3600, "last_played": "2026-08-28"},
            {"platform": "PC", "genre": "", "playtime_seconds": 0},
        ]
        history = [{"started": "2026-08-28T10:00:00", "seconds": 100}]
        totals = compute_totals(games, history)
        self.assertEqual(totals["games"], 2)
        self.assertEqual(totals["played"], 1)
        self.assertEqual(totals["total_playtime_seconds"], 3600)
        self.assertEqual(totals["total_sessions"], 1)


class TopPlatformsTest(unittest.TestCase):
    def test_top_platforms(self):
        games = [
            {"platform": "PC", "playtime_seconds": 100},
            {"platform": "PC", "playtime_seconds": 200},
            {"platform": "SNES", "playtime_seconds": 50},
        ]
        top = compute_top_platforms(games, limit=2)
        self.assertEqual(top[0]["platform"], "PC")
        self.assertEqual(top[0]["count"], 2)
        self.assertEqual(top[0]["playtime_seconds"], 300)


class TopGenresTest(unittest.TestCase):
    def test_top_genres(self):
        games = [
            {"genre": "RPG, Action"},
            {"genre": "RPG"},
            {"genre": ""},
        ]
        top = compute_top_genres(games, limit=2)
        self.assertEqual(top[0]["genre"], "RPG")
        self.assertEqual(top[0]["count"], 2)


class TopGamesTest(unittest.TestCase):
    def test_top_games_sorted_and_skips_zero(self):
        games = [
            {"game_id": "game-a", "name": "Alpha", "platform": "PC", "playtime_seconds": 100, "play_count": 2},
            {"game_id": "game-b", "name": "Beta", "platform": "SNES", "playtime_seconds": 500, "play_count": 1},
            {"game_id": "game-c", "name": "Gamma", "platform": "PC", "playtime_seconds": 0},
        ]
        top = compute_top_games(games, limit=2)
        self.assertEqual(len(top), 2)
        self.assertEqual(top[0]["game_id"], "game-b")
        self.assertEqual(top[0]["playtime_seconds"], 500)
        self.assertEqual(top[1]["name"], "Alpha")
        self.assertNotIn("game-c", [item["game_id"] for item in top])

    def test_top_games_limit_and_tiebreak(self):
        games = [
            {"game_id": "game-a", "name": "Alpha", "playtime_seconds": 100, "play_count": 1},
            {"game_id": "game-b", "name": "Beta", "playtime_seconds": 100, "play_count": 5},
            {"game_id": "game-c", "name": "Gamma", "playtime_seconds": 100, "play_count": 5},
        ]
        top = compute_top_games(games, limit=10)
        self.assertEqual([item["game_id"] for item in top[:2]], ["game-b", "game-c"])

    def test_top_games_garbage_values(self):
        games = [{"name": "Weird", "playtime_seconds": "not-a-number", "play_count": None}, None, "not-a-dict"]
        top = compute_top_games(games)
        self.assertEqual(top, [])
        self.assertEqual(compute_top_games("not-a-list"), [])

    def test_top_games_play_count_garbage_falls_back(self):
        games = [{"game_id": "game-a", "name": "Alpha", "playtime_seconds": 100, "play_count": "not-a-number"}]
        top = compute_top_games(games)
        self.assertEqual(top[0]["play_count"], 0)


class MomentumTest(unittest.TestCase):
    def test_momentum(self):
        base = datetime.date(2026, 8, 28)
        heat = []
        for i in range(60):
            date = base - datetime.timedelta(days=59 - i)
            seconds = 100 if i >= 30 else 10
            heat.append({"date": date.isoformat(), "count": 1, "seconds": seconds, "level": 1})
        mom = compute_momentum(heat)
        self.assertEqual(mom["last_30_days_seconds"], 3000)
        self.assertEqual(mom["previous_30_days_seconds"], 300)
        self.assertEqual(mom["delta_seconds"], 2700)


class SummarizeTest(unittest.TestCase):
    def test_summarize_keys(self):
        state = {"games": [], "history": []}
        result = summarize(state, end_date=datetime.date(2026, 8, 28))
        for key in ("heatmap", "streak", "totals", "top_platforms", "top_genres", "top_games", "momentum", "last_30_days", "days"):
            self.assertIn(key, result)
        self.assertEqual(len(result["heatmap"]), 366)
        self.assertEqual(len(result["last_30_days"]), 30)

    def test_summarize_days_scopes_heatmap(self):
        state = {"games": [], "history": []}
        result = summarize(state, end_date=datetime.date(2026, 8, 28), days=30)
        self.assertEqual(result["days"], 30)
        self.assertEqual(len(result["heatmap"]), 30)

    def test_summarize_days_out_of_range_falls_back(self):
        state = {"games": [], "history": []}
        for bad in (0, -5, 999, "30"):
            result = summarize(state, days=bad)
            self.assertEqual(len(result["heatmap"]), 366)
            self.assertEqual(result["days"], 366)

    def test_performance_20k(self):
        import time

        history = [
            {"started": f"2026-08-{(i % 28) + 1:02d}T10:00:00", "seconds": 100 + i % 3000}
            for i in range(20000)
        ]
        start = time.monotonic()
        hm = compute_heatmap(history, end_date=datetime.date(2026, 8, 28))
        elapsed_ms = (time.monotonic() - start) * 1000
        self.assertEqual(len(hm), 366)
        self.assertLess(elapsed_ms, 200, f"heatmap 20k took {elapsed_ms:.1f}ms >200ms")


class InsightsHandlerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        import web_app
        from openbox import STATE_STORE

        self.web_app = web_app
        self._prev_data_dir = os.environ.get("OPENBOX_DATA_DIR")
        self._prev_data = web_app.DATA
        self._prev_store_path = STATE_STORE.path
        self._prev_store_backup = STATE_STORE.backup_path
        self._prev_store_lock = STATE_STORE.lock_path
        self._prev_cached_state = STATE_STORE._cached_state
        self._prev_cached_signature = STATE_STORE._cached_signature
        self.addCleanup(self._restore)

        os.environ["OPENBOX_DATA_DIR"] = self.tmp.name
        data_dir = Path(self.tmp.name)
        data_dir.mkdir(parents=True, exist_ok=True)
        web_app.DATA = data_dir / "library.json"
        STATE_STORE.path = web_app.DATA
        STATE_STORE.lock_path = web_app.DATA.with_name(f".{web_app.DATA.name}.lock")
        STATE_STORE.backup_path = web_app.DATA.with_name(f"{web_app.DATA.name}.bak")
        STATE_STORE._cached_state = None
        STATE_STORE._cached_signature = None
        STATE_STORE.save({"games": [], "profiles": {}, "settings": {}, "history": [], "playlists": []})

    def _restore(self):
        from openbox import STATE_STORE

        if self._prev_data_dir is None:
            os.environ.pop("OPENBOX_DATA_DIR", None)
        else:
            os.environ["OPENBOX_DATA_DIR"] = self._prev_data_dir
        self.web_app.DATA = self._prev_data
        STATE_STORE.path = self._prev_store_path
        STATE_STORE.backup_path = self._prev_store_backup
        STATE_STORE.lock_path = self._prev_store_lock
        STATE_STORE._cached_state = self._prev_cached_state
        STATE_STORE._cached_signature = self._prev_cached_signature

    def handler(self):
        h = self.web_app.Handler.__new__(self.web_app.Handler)
        h.responses = []
        h.send_json = lambda status, payload: h.responses.append((status, payload))
        return h

    def test_summary_ok(self):
        from urllib.parse import urlparse

        h = self.handler()
        parsed = urlparse("/api/v2/insights/summary?end_date=2026-08-28")
        h._api_get_api_v2_insights_summary(parsed)
        self.assertEqual(h.responses[0][0], 200)
        payload = h.responses[0][1]
        self.assertIn("heatmap", payload)
        self.assertEqual(len(payload["heatmap"]), 366)

    def test_summary_days_param(self):
        from urllib.parse import urlparse

        h = self.handler()
        parsed = urlparse("/api/v2/insights/summary?days=30&end_date=2026-08-28")
        h._api_get_api_v2_insights_summary(parsed)
        self.assertEqual(h.responses[0][0], 200)
        payload = h.responses[0][1]
        self.assertEqual(payload["days"], 30)
        self.assertEqual(len(payload["heatmap"]), 30)
        self.assertIn("top_games", payload)

    def test_summary_bad_days(self):
        from urllib.parse import urlparse

        from api_errors import BadRequest

        h = self.handler()
        for query in ("days=999", "days=abc"):
            parsed = urlparse(f"/api/v2/insights/summary?{query}&end_date=2026-08-28")
            with self.assertRaises(BadRequest):
                h._api_get_api_v2_insights_summary(parsed)

    def test_heatmap_days_param(self):
        from urllib.parse import urlparse

        h = self.handler()
        parsed = urlparse("/api/v2/insights/heatmap?days=7&end_date=2026-08-28")
        h._api_get_api_v2_insights_heatmap(parsed)
        self.assertEqual(h.responses[0][0], 200)
        self.assertEqual(len(h.responses[0][1]["heatmap"]), 7)

    def test_bad_days(self):
        from urllib.parse import urlparse

        from api_errors import BadRequest

        h = self.handler()
        parsed = urlparse("/api/v2/insights/heatmap?days=999&end_date=2026-08-28")
        with self.assertRaises(BadRequest):
            h._api_get_api_v2_insights_heatmap(parsed)

    def test_bad_date(self):
        from urllib.parse import urlparse

        from api_errors import BadRequest

        h = self.handler()
        parsed = urlparse("/api/v2/insights/summary?end_date=bad-date")
        with self.assertRaises(BadRequest):
            h._api_get_api_v2_insights_summary(parsed)

    def test_mastery_ok(self):
        from urllib.parse import urlparse

        h = self.handler()
        parsed = urlparse("/api/v2/insights/mastery")
        h._api_get_api_v2_insights_mastery(parsed)
        self.assertEqual(h.responses[0][0], 200)
        payload = h.responses[0][1]
        for key in ("platforms", "overall", "decades"):
            self.assertIn(key, payload)
        self.assertIn("total", payload["overall"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
