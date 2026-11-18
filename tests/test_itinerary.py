"""Unit tests for Backlog Itinerary eligibility, estimation, capacity, status, and replan."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api_errors import ItineraryInvalidStrategy, ItineraryNoGames  # noqa: E402
from pkg.parity import parity_itinerary as pi  # noqa: E402
import state_store  # noqa: E402


class ItineraryUnitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_eligibility_own_history_ge80pct_and_ra_refinement(self):
        # 3 completed SNES games give own_history median = 4.0h (>=80% non-default w/o creds)
        completed = [
            {"game_id": f"done-{i}", "name": f"Done {i}", "platform": "SNES", "progress": "Beaten", "playtime_seconds": h * 3600}
            for i, h in enumerate((2, 4, 6))
        ]
        backlog = [
            {"game_id": "g-1", "name": "Alpha", "platform": "SNES", "progress": "Playing", "playtime_seconds": 3600},
            {"game_id": "g-2", "name": "Beta", "platform": "SNES", "progress": "Unplayed"},
            {"game_id": "g-3", "name": "Gamma", "platform": "GBA", "progress": "Playing"},
            {"game_id": "g-ab", "name": "Dropped", "platform": "SNES", "progress": "Abandoned"},
            {"game_id": "g-tr", "name": "Trashed", "platform": "SNES", "progress": "Unplayed"},
            {"game_id": "g-igdb", "name": "Stored", "platform": "SNES", "time_to_beat_hours": 10.0},
        ]
        ra_dir = self.data_dir / "cache" / "retroachievements"
        ra_dir.mkdir(parents=True)
        # g-igdb is 50% earned -> 10.0 * (1 - 0.5) = 5.0h refined_by_ra
        (ra_dir / "g-igdb.json").write_text(json.dumps({"earned": 5, "total": 10}), encoding="utf-8")

        state = {
            "games": completed + backlog,
            "trash": [{"game_id": "g-tr"}],
            "settings": {"progress_values": ["Unplayed", "Playing", "Beaten", "Abandoned"]},
        }
        plan = pi.build_itinerary(
            state,
            horizon_days=30,
            minutes_per_day=120,
            strategy="quick-wins",
            data_dir=self.data_dir,
            now=datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc),
        )
        ids = [e["game_id"] for e in plan["entries"]] + plan["later"]
        self.assertNotIn("g-ab", ids)
        self.assertNotIn("g-tr", ids)
        self.assertNotIn("done-0", ids)
        by_gid = {e["game_id"]: e for e in plan["entries"]}
        self.assertEqual(by_gid["g-1"]["est_source"], "own_history")
        self.assertEqual(by_gid["g-3"]["est_source"], "own_history")  # widens from >=3 global completed
        self.assertEqual(by_gid["g-igdb"]["est_source"], "igdb")
        self.assertEqual(by_gid["g-igdb"]["est_hours"], 5.0)
        self.assertTrue(by_gid["g-igdb"]["refined_by_ra"])
        self.assertEqual(plan["estimate_breakdown"]["default"], 0)

    def test_strategies_capacity_live_status_replan_and_repair(self):
        t0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
        games = [
            {"game_id": "g-a", "name": "Short", "platform": "PC", "time_to_beat_hours": 4.0, "playtime_seconds": 1800},
            {"game_id": "g-b", "name": "Medium", "platform": "PC", "time_to_beat_hours": 5.0, "playtime_seconds": 7200},
            {"game_id": "g-c", "name": "Overflow", "platform": "PC", "time_to_beat_hours": 3.0, "playtime_seconds": 0},
        ]
        state = {"games": games, "trash": [], "settings": {}, "itinerary": None}
        # 7 days * 60 min/day = 7.0h capacity -> exact fit 4.0+... stops before 4+5=9.0 -> Medium overflows?
        # Use comeback: Medium (5h, 7200s played) first, then Overflow (3h) overflows 5+3=8 > 7, Short fits if ordered next?
        # quick-wins: Overflow (3.0) + Short (4.0) == 7.0h exact fit -> Medium (5.0) goes to later!
        plan = pi.build_itinerary(state, horizon_days=7, minutes_per_day=60, strategy="quick-wins", now=t0)
        self.assertEqual([e["game_id"] for e in plan["entries"]], ["g-c", "g-a"])
        self.assertEqual(plan["planned_hours"], 7.0)
        self.assertEqual(plan["later"], ["g-b"])
        state["itinerary"] = plan

        # Comeback puts g-b first
        cb = pi.build_itinerary(state, horizon_days=14, minutes_per_day=60, strategy="comeback", now=t0)
        self.assertEqual(cb["entries"][0]["game_id"], "g-b")

        # Raided falls back cleanly to quick-wins without RA cache
        rf = pi.build_itinerary(state, horizon_days=14, minutes_per_day=60, strategy="raided", now=t0)
        self.assertEqual(rf["strategy_fallback"], "quick-wins")

        # Simulate elapsed day 5 (elapsed_capacity = 6 * 1.0h = 6.0h):
        # g-c (3h, cum 3h <= 6h) -> "behind" unless completed; mark g-c Beaten & remove g-a
        games[2]["progress"] = "Beaten"
        state["games"] = [games[1], games[2]]  # g-a removed -> stale!
        live = pi.project_live_itinerary(state, now=t0 + timedelta(days=5))["plan"]
        statuses = {e["game_id"]: e["live_status"] for e in live["entries"]}
        self.assertEqual(statuses["g-c"], "done")
        self.assertEqual(statuses["g-a"], "stale")

        # Replan pins done g-c, reports dropped_ids=["g-a"], schedules remaining g-b!
        rep = pi.replan_itinerary(state, {"horizon_days": 14}, now=t0 + timedelta(days=5))
        self.assertEqual(rep["dropped_ids"], ["g-a"])
        new_gids = [e["game_id"] for e in rep["plan"]["entries"]]
        self.assertIn("g-c", new_gids)
        self.assertIn("g-b", new_gids)

        # Invalid strategy & empty library
        with self.assertRaises(ItineraryInvalidStrategy):
            pi.build_itinerary(state, strategy="franchise")
        with self.assertRaises(ItineraryNoGames):
            pi.build_itinerary({"games": []}, strategy="quick-wins")

        # Warn-and-repair resets malformed itinerary
        state["itinerary"] = {"bad": "shape"}
        state_store._normalize_22_additive_fields(state)
        self.assertIsNone(state["itinerary"])

    def test_clamps_raided_with_ra_cache_and_custom_progress_filter(self):
        self.assertEqual(pi.clamp_horizon_days("bad"), 30)
        self.assertEqual(pi.clamp_minutes_per_day("bad"), 60)
        self.assertEqual(pi._trashed_ids({"trash": ["g-x", {"game": {"game_id": "g-y"}}]}), {"g-x", "g-y"})

        ra_dir = self.data_dir / "cache" / "retroachievements"
        ra_dir.mkdir(parents=True)
        (ra_dir / "g-low.json").write_text(json.dumps({"earned": 1, "total": 10}), encoding="utf-8")
        (ra_dir / "g-high.json").write_text(json.dumps({"earned": 8, "total": 10}), encoding="utf-8")
        state = {
            "games": [
                {"game_id": "g-low", "name": "LowRA", "platform": "GBA", "progress": "Playing"},
                {"game_id": "g-high", "name": "HighRA", "platform": "GBA", "progress": "Playing"},
                {"game_id": "g-custom", "name": "CustomOut", "platform": "GBA", "progress": "Archived"},
                {"game_id": "g-missing", "name": "Uninstalled", "platform": "GBA", "path_exists": False, "store_installed": False},
            ],
            "trash": [],
            "settings": {"progress_values": ["Unplayed", "Playing"]},
        }
        plan = pi.build_itinerary(state, strategy="raided", platforms=["GBA"], data_dir=self.data_dir)
        self.assertIsNone(plan["strategy_fallback"])
        self.assertEqual([e["game_id"] for e in plan["entries"]], ["g-high", "g-low"])


if __name__ == "__main__":
    unittest.main()
