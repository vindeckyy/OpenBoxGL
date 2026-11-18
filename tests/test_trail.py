"""Unit tests for Achievement Trail metrics, year boundaries, and degradation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity import parity_trail as pt  # noqa: E402


class TrailUnitTests(unittest.TestCase):
    def test_year_boundary_velocity_hardcore_precedence_and_next_up_ordering(self):
        # 2026-01-04 is Sunday of ISO week 2026-W01; 2025-12-29 is Monday of 2026-W01
        now = datetime(2026, 1, 7, 12, 0, tzinfo=timezone.utc)
        game = {"game_id": "g-ra", "name": "Super Metroid", "platform": "SNES"}
        ra_cache = {
            "earned": 2,
            "earned_hardcore": 1,
            "total": 6,
            "mastered": False,
            "achievements": [
                {
                    "id": 10,
                    "title": "Hardcore Wins Date",
                    "points": 25,
                    "earned": True,
                    "hardcore": True,
                    "date_earned": "2025-12-20 10:00:00",
                    "date_earned_hardcore": "2026-01-03 18:00:00",
                },
                {
                    "id": 11,
                    "title": "Prev Week",
                    "points": 10,
                    "earned": True,
                    "hardcore": False,
                    "date_earned": "2025-12-25 12:00:00",
                },
                {"id": 99, "title": "Twenty Points", "points": 20, "earned": False},
                {"id": 2, "title": "Cheapest Two", "points": 5, "earned": False},
                {"id": 1, "title": "Cheapest One", "points": 5, "earned": False},
                {"id": 5, "title": "Zero Points", "points": 0, "earned": False},
            ],
        }
        res = pt.compute_game_trail(game, ra_cache, now=now)
        self.assertTrue(res["has_dates"])
        # Hardcore date (2026-01-03 -> 2026-W01) should land in 2026-W01, NOT 2025-W51
        by_week = {row["week"]: row for row in res["weekly_velocity"]}
        self.assertEqual(by_week["2026-W01"]["count"], 1)
        self.assertEqual(by_week["2026-W01"]["points"], 25)
        self.assertEqual(by_week["2025-W52"]["count"], 1)
        self.assertEqual(res["streak_weeks"], 2)
        self.assertEqual(res["next_milestone"], {"points": 100, "remaining": 65})
        # Low active weeks (<4) -> eta_days is None with honest confidence
        self.assertIsNone(res["eta_days"])
        self.assertEqual(res["eta_confidence"], "low")
        # Next up: positive cheapest first (5 pts id 1, then id 2), then 20 pts, zero points last
        self.assertEqual([a["id"] for a in res["next_up"]], [1, 2, 99, 5])

    def test_confidence_thresholds_eta_forecast_and_date_less_degradation(self):
        now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        game = {"game_id": "g-1", "name": "Castlevania", "platform": "NES"}

        def make_earned(weeks_back_list):
            items = []
            for idx, w in enumerate(weeks_back_list):
                dt = (now - timedelta(weeks=w)).strftime("%Y-%m-%d %H:%M:%S")
                items.append({"id": idx + 1, "title": f"A{idx}", "points": 10, "earned": True, "date_earned": dt})
            for j in range(10):
                items.append({"id": 100 + j, "title": f"U{j}", "points": 5, "earned": False})
            return {"earned": len(weeks_back_list), "earned_hardcore": 0, "total": len(weeks_back_list) + 10, "achievements": items}

        med = pt.compute_game_trail(game, make_earned([0, 1, 2, 3, 4]), now=now)
        self.assertEqual(med["eta_confidence"], "medium")
        self.assertIsInstance(med["eta_days"], int)
        self.assertEqual(med["eta_days"], 70)  # remaining=10, rate=1/wk -> 70d

        high = pt.compute_game_trail(game, make_earned(list(range(8))), now=now)
        self.assertEqual(high["eta_confidence"], "high")
        self.assertEqual(high["streak_weeks"], 8)

        dateless = pt.compute_game_trail(
            game,
            {
                "earned": 3,
                "earned_hardcore": 3,
                "total": 10,
                "achievements": [{"id": 1, "title": "Old", "points": 10, "earned": True}],
            },
            now=now,
        )
        self.assertFalse(dateless["has_dates"])
        self.assertEqual(dateless["weekly_velocity"], [])
        self.assertEqual(dateless["eta_reason"], "no_dates")
        self.assertEqual(dateless["streak_weeks"], 0)

        done = pt.compute_game_trail(
            game,
            {"earned": 2, "earned_hardcore": 2, "total": 2, "mastered": True, "achievements": []},
            now=now,
        )
        self.assertEqual(done["eta_confidence"], "complete")
        self.assertEqual(done["eta_days"], 0)

    def test_timestamp_parsing_sorts_streaks_and_refresh_edges(self):
        self.assertIsNone(pt.parse_ra_timestamp(""))
        self.assertIsNone(pt.parse_ra_timestamp("not-a-timestamp"))
        self.assertIsNotNone(pt.parse_ra_timestamp("2026-06-01T12:00:00Z"))
        self.assertIsNotNone(pt.parse_ra_timestamp("2026-06-01T12:00:00+02:00"))
        self.assertEqual(pt._id_sort_key("abc")[0], 1)

        # Previous week anchor preserves streak; zero activity sets streak 0
        now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        prev_wk = now - timedelta(weeks=1)
        self.assertEqual(pt._compute_week_streak([], now), 0)
        self.assertEqual(pt._compute_week_streak([prev_wk], now), 1)
        self.assertEqual(pt._compute_week_streak([now - timedelta(weeks=5)], now), 0)

        # Sort orders (closest, active, longest, points)
        rows = [
            {"name": "B", "remaining": 5, "progress_pct": 50.0, "total_points": 100, "earned_points": 50, "last_unlock_at": "2026-06-01T00:00:00Z"},
            {"name": "A", "remaining": 2, "progress_pct": 80.0, "total_points": 200, "earned_points": 80, "last_unlock_at": "2026-06-05T00:00:00Z"},
            {"name": "C", "remaining": 0, "progress_pct": 100.0, "total_points": 50, "earned_points": 50, "last_unlock_at": ""},
        ]
        self.assertEqual([r["name"] for r in pt._sort_items(rows, "closest")], ["A", "B", "C"])
        self.assertEqual([r["name"] for r in pt._sort_items(rows, "active")], ["A", "B", "C"])
        self.assertEqual([r["name"] for r in pt._sort_items(rows, "longest")], ["B", "A", "C"])
        self.assertEqual([r["name"] for r in pt._sort_items(rows, "points")], ["A", "B", "C"])


if __name__ == "__main__":
    unittest.main()
