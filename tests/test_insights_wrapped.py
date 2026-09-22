"""Additional parity_insights tests for wrapped + timeline."""
from __future__ import annotations

import os
import sys
import time
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity.parity_insights import _parse_date, timeline_groups, wrapped_summary  # noqa: E402


class WrappedSummaryTest(unittest.TestCase):
    def _state(self, games, history):
        return {"games": games, "history": history}

    def test_empty(self):
        result = wrapped_summary(self._state([], []), 2026)
        self.assertEqual(result["year"], 2026)
        self.assertEqual(result["totals"]["playtime_seconds"], 0)
        self.assertEqual(result["per_month"], [0] * 12)

    def test_year_scoping(self):
        games = [{"id": 1, "game_id": "g-1", "name": "Quake", "year": 1996}]
        history = [
            {"game_id": "g-1", "started": "2026-06-01T12:00:00", "seconds": 3600},
            {"game_id": "g-1", "started": "2025-06-01T12:00:00", "seconds": 3600},
        ]
        result = wrapped_summary(self._state(games, history), 2026)
        self.assertEqual(result["totals"]["playtime_seconds"], 3600)
        self.assertEqual(result["totals"]["sessions"], 1)

    def test_leap_year(self):
        games = [{"id": 1, "game_id": "g-1", "name": "Quake"}]
        history = [{"game_id": "g-1", "started": "2024-02-29T12:00:00", "seconds": 3600}]
        result = wrapped_summary(self._state(games, history), 2024)
        self.assertEqual(result["totals"]["sessions"], 1)

    def test_per_month_length(self):
        games = [{"id": 1, "game_id": "g-1", "name": "Quake"}]
        history = [{"game_id": "g-1", "started": "2026-03-01T12:00:00", "seconds": 3600}]
        result = wrapped_summary(self._state(games, history), 2026)
        self.assertEqual(len(result["per_month"]), 12)
        self.assertEqual(result["per_month"][2], 3600)

    def test_oldest_played(self):
        games = [
            {"id": 1, "game_id": "g-old", "name": "Old", "year": 1987},
            {"id": 2, "game_id": "g-new", "name": "New", "year": 2020},
        ]
        history = [
            {"game_id": "g-old", "started": "2026-01-01T12:00:00", "seconds": 600},
            {"game_id": "g-new", "started": "2026-01-02T12:00:00", "seconds": 600},
        ]
        result = wrapped_summary(self._state(games, history), 2026)
        self.assertEqual(result["oldest_played"]["year"], 1987)


class TimelineGroupsTest(unittest.TestCase):
    def _state(self, games, history):
        return {"games": games, "history": history}

    def test_empty(self):
        result = timeline_groups(self._state([], []), days=90)
        self.assertEqual(result["groups"], [])

    def test_groups_desc_and_basename(self):
        today = date.today()
        games = [{"id": 1, "game_id": "g-1", "name": "Quake", "has_cover": True}]
        history = [
            {"game_id": "g-1", "started": today.isoformat() + "T12:00:00", "seconds": 3600, "recording": "/tmp/obs/recording.mkv"},
        ]
        result = timeline_groups(self._state(games, history), days=90)
        self.assertEqual(len(result["groups"]), 1)
        self.assertEqual(result["groups"][0]["entries"][0]["recording"], "recording.mkv")

    def _use_tz(self, name):
        """Pin the process local timezone for the test (POSIX only)."""
        if not hasattr(time, "tzset"):
            self.skipTest("requires POSIX time.tzset")
        old = os.environ.get("TZ")
        os.environ["TZ"] = name
        time.tzset()
        self.addCleanup(self._restore_tz, old)

    @staticmethod
    def _restore_tz(old):
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()

    def test_parse_date_aware_converts_to_local(self):
        self._use_tz("America/New_York")  # EDT (-04:00) in September
        # Shared vectors with the story-view frontend (localDayKey).
        self.assertEqual(_parse_date("2026-09-21T23:30:00-04:00"), date(2026, 9, 21))
        self.assertEqual(_parse_date("2026-09-21T23:00:00"), date(2026, 9, 21))
        # The discriminating vector: the same instant stored the way moments.py
        # stores it (UTC-aware via datetime.now(timezone.utc)). Pre-fix,
        # .date() yielded the UTC calendar date 2026-09-22; the local day is
        # 2026-09-21. (The -04:00 vector above passes pre-fix too, since
        # .date() keeps the carried offset's calendar date.)
        self.assertEqual(_parse_date("2026-09-22T03:30:00+00:00"), date(2026, 9, 21))
        self.assertEqual(_parse_date("2026-09-21"), date(2026, 9, 21))
        self.assertIsNone(_parse_date(""))
        self.assertIsNone(_parse_date("not-a-date"))

    def test_timeline_groups_mixed_aware_naive_share_local_day(self):
        self._use_tz("America/New_York")  # EDT (-04:00) in September
        today = date.today()
        games = [{"id": 1, "game_id": "g-1", "name": "Quake"}]
        # The aware entry is the UTC instant of today 23:30 local: pre-fix it
        # parsed to the UTC date (tomorrow) and was filtered out of the
        # 90-day window entirely.
        aware_started = (today + timedelta(days=1)).isoformat() + "T03:30:00+00:00"
        history = [
            {"game_id": "g-1", "started": aware_started, "seconds": 600},
            {"game_id": "g-1", "started": today.isoformat() + "T23:00:00", "seconds": 600},
        ]
        result = timeline_groups(self._state(games, history), days=90)
        self.assertEqual(len(result["groups"]), 1)
        self.assertEqual(result["groups"][0]["date"], today.isoformat())
        self.assertEqual(len(result["groups"][0]["entries"]), 2)

    def test_timeline_entries_have_no_dead_ended_field(self):
        today = date.today()
        games = [{"id": 1, "game_id": "g-1", "name": "Quake"}]
        history = [{"game_id": "g-1", "started": today.isoformat() + "T12:00:00", "seconds": 60}]
        result = timeline_groups(self._state(games, history), days=90)
        self.assertEqual(len(result["groups"]), 1)
        self.assertNotIn("ended", result["groups"][0]["entries"][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
