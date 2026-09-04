"""Additional parity_insights tests for wrapped + timeline."""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity.parity_insights import timeline_groups, wrapped_summary  # noqa: E402


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

    # --- D5: basenames-only redaction (posix + windows) ---
    def test_timeline_windows_basename(self):
        today = date.today()
        games = [{"id": 1, "game_id": "g-1", "name": "Quake"}]
        history = [
            {"game_id": "g-1", "started": today.isoformat() + "T12:00:00", "seconds": 60, "recording": "C:\\Videos\\OBS\\run.mkv"},
        ]
        result = timeline_groups(self._state(games, history), days=90)
        rec = result["groups"][0]["entries"][0]["recording"]
        self.assertEqual(rec, "run.mkv")

    def test_timeline_no_separators_leak(self):
        today = date.today()
        games = [{"id": 1, "game_id": "g-1", "name": "Quake"}]
        for raw in ("/a/b/c.mkv", "C:\\a\\b\\c.mkv", "plain.mkv", ""):
            history = [{"game_id": "g-1", "started": today.isoformat() + "T12:00:00", "seconds": 60, "recording": raw}]
            result = timeline_groups(self._state(games, history), days=90)
            rec = result["groups"][0]["entries"][0]["recording"]
            self.assertNotIn("/", rec)
            self.assertNotIn("\\", rec)

    def test_timeline_js_basename_defense(self):
        text = (Path(__file__).resolve().parent.parent / "static" / "timeline.js").read_text(encoding="utf-8")
        self.assertIn("recording", text)
        # Frontend must not interpolate a full path; basename split is the guard.
        self.assertRegex(text, r"split\(\s*/\[\\\\\/\]/\s*\)|split\(['\"]")

    def test_wrapped_print_css_no_raw_hex_and_hides_chrome(self):
        import re
        css = (Path(__file__).resolve().parent.parent / "static" / "app.css").read_text(encoding="utf-8")
        m = re.search(r"@media\s+print\s*\{(.*?)\n    \}", css, re.DOTALL)
        self.assertIsNotNone(m, "print block missing")
        block = m.group(1)
        self.assertNotRegex(block, r"#[0-9a-fA-F]{3,6}")
        self.assertIn(".wrapped-card", block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
