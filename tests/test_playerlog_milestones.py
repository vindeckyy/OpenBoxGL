"""Milestone derivation tests covering all 9 kinds, crossing dates, and RA degradation."""

from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

from pkg.parity.parity_playerlog import compute_milestones  # noqa: E402


class MilestoneDerivationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_all_nine_kinds_crossing_date_and_full_history_streak(self):
        # Build a 370-day continuous streak (>366d) back from 2026-10-01
        streak_start = date(2025, 9, 25)
        history = []
        for i in range(370):
            day = (streak_start + timedelta(days=i)).isoformat()
            secs = 1800 if i < 10 else 3600
            history.append({"game_id": "g-0", "game": "Game 0", "started": f"{day}T12:00:00", "seconds": secs})

        games = []
        for idx in range(12):
            added = (date(2020, 1, 1) + timedelta(days=idx * 10)).isoformat()
            g = {
                "id": idx,
                "game_id": f"g-{idx}",
                "name": f"Game {idx}",
                "platform": "SNES",
                "added_at": added,
                "playtime_seconds": 0,
            }
            if idx == 0:
                # 360*3600 + 10*1800 = 365h -> unlocks 10, 25, 50, 100, 250h on exact binary-search dates
                g["playtime_seconds"] = sum(h["seconds"] for h in history)
                g["progress"] = "Beaten"
                g["completed_at"] = "2026-08-15"
            elif idx == 1:
                # Playtime without full session log -> approximate=True fallback
                g["playtime_seconds"] = 12 * 3600
                g["last_played"] = "2026-09-01"
            games.append(g)

        # Write RA cache with dated earned achievements (>= 200 pts & mastered)
        ra_dir = self.data_dir / "cache" / "retroachievements"
        ra_dir.mkdir(parents=True)
        (ra_dir / "g-0.json").write_text(
            json.dumps({
                "earned": 2,
                "earned_hardcore": 2,
                "total": 2,
                "mastered": True,
                "beaten": 1,
                "achievements": [
                    {"id": 1, "title": "Start", "points": 100, "earned": True, "date_earned": "2026-01-05 10:00:00"},
                    {"id": 2, "title": "Finish", "points": 150, "earned": True, "date_earned_hardcore": "2026-02-10 20:00:00"},
                ],
            }),
            encoding="utf-8",
        )

        state = {"games": games, "history": history, "ui_state": {}, "trash": []}
        res1 = compute_milestones(state, data_dir=self.data_dir, today=date(2026, 10, 10))
        res2 = compute_milestones(state, data_dir=self.data_dir, today=date(2026, 10, 10))
        self.assertEqual(res1, res2)
        self.assertTrue(res1["ra_available"])

        by_id = {m["id"]: m for m in res1["unlocked"]}
        kinds_present = {m["kind"] for m in res1["unlocked"]}
        self.assertEqual(
            kinds_present,
            {
                "playtime_game",
                "playtime_total",
                "first_clear",
                "sessions",
                "ra_points",
                "ra_complete",
                "streak",
                "library_age",
                "collection",
            },
        )
        # Exact binary-search crossing date for g-0 10h vs fallback for g-1 10h
        self.assertFalse(by_id["m:playtime_game:g-0:10h"]["approximate"])
        self.assertTrue(by_id["m:playtime_game:g-1:10h"]["approximate"])
        self.assertEqual(by_id["m:playtime_game:g-1:10h"]["unlocked_at"], "2026-09-01")
        # Full-history 365d streak present
        self.assertIn("m:streak:365d", by_id)
        self.assertFalse(by_id["m:streak:365d"]["approximate"])
        # RA crossing dates
        self.assertEqual(by_id["m:ra_points:100"]["unlocked_at"], "2026-01-05")
        self.assertEqual(by_id["m:ra_points:200"]["unlocked_at"], "2026-02-10")
        self.assertFalse(by_id["m:ra_complete:g-0"]["approximate"])
        # Library age (2020-01-01 -> 2026-10-10 = 1, 2, 5y)
        self.assertIn("m:library_age:5y", by_id)
        self.assertEqual(by_id["m:library_age:5y"]["unlocked_at"], "2025-01-01")
        # Collection >= 10
        self.assertIn("m:collection:10", by_id)

    def test_ra_absent_and_hidden_or_abandoned_exclusions(self):
        state = {
            "games": [
                {"game_id": "g-keep", "name": "Keep", "added_at": "2025-01-01", "playtime_seconds": 10 * 3600},
                {"game_id": "g-hide", "name": "Hide", "hidden": True, "playtime_seconds": 500 * 3600},
                {"game_id": "g-trash", "name": "Trash", "playtime_seconds": 500 * 3600},
            ],
            "history": [],
            "trash": [{"game_id": "g-trash"}],
            "ui_state": {},
        }
        res = compute_milestones(state, data_dir=self.data_dir, today=date(2026, 6, 1))
        self.assertFalse(res["ra_available"])
        ids = {m["id"] for m in res["unlocked"]}
        self.assertNotIn("m:playtime_game:g-hide:500h", ids)
        self.assertNotIn("m:playtime_game:g-trash:500h", ids)
        self.assertFalse(any(m["kind"] in ("ra_points", "ra_complete") for m in res["unlocked"]))
        self.assertIn("m:playtime_game:g-keep:10h", ids)


if __name__ == "__main__":
    unittest.main()
