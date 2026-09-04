"""Tests for parity_insights mastery_summary."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity.parity_insights import mastery_summary  # noqa: E402


class MasterySummaryTest(unittest.TestCase):
    def test_empty(self):
        result = mastery_summary([])
        self.assertEqual(result["overall"]["total"], 0)

    def test_progress_states(self):
        games = [
            {"game_id": "g-1", "name": "A", "platform": "SNES", "progress": "mastered"},
            {"game_id": "g-2", "name": "B", "platform": "SNES", "progress": "completed"},
            {"game_id": "g-3", "name": "C", "platform": "PC", "playtime_seconds": 10},
        ]
        result = mastery_summary(games)
        self.assertEqual(result["overall"]["mastered"], 1)
        self.assertEqual(result["overall"]["completed"], 1)
        self.assertEqual(result["overall"]["played"], 1)
        self.assertEqual(result["overall"]["total"], 3)

    def test_decade_bucket(self):
        games = [
            {"game_id": "g-1", "year": 1996, "progress": "mastered"},
            {"game_id": "g-2", "year": 2010, "progress": "completed"},
        ]
        result = mastery_summary(games)
        self.assertIn("1990s", result["decades"])
        self.assertIn("2010s", result["decades"])

    # --- D5: RA-cache-missing affordance (zero new network calls) ---
    def test_ra_cache_missing_flag(self):
        result = mastery_summary([], ra_cache_dir="/nonexistent-ra-cache-d5")
        self.assertIn("ra_available", result)
        self.assertFalse(result["ra_available"])
        self.assertFalse(result["overall"]["ra_available"])

    def test_ra_cache_present_flag(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "g-1.json").write_text(
                json.dumps({"game_id": "g-1", "mastered": True, "progress_pct": 100.0}),
                encoding="utf-8",
            )
            games = [{"game_id": "g-1", "platform": "SNES"}]
            result = mastery_summary(games, ra_cache_dir=tmp)
            self.assertTrue(result["ra_available"])
            self.assertEqual(result["overall"]["ra_tracked"], 1)

    def test_mastery_js_local_only_affordance(self):
        text = (ROOT / "static" / "mastery.js").read_text(encoding="utf-8")
        self.assertIn("mastery.local_only", text)
        self.assertIn("ra_available", text)

    def test_mastery_js_zero_new_network_calls(self):
        text = (ROOT / "static" / "mastery.js").read_text(encoding="utf-8")
        self.assertEqual(text.count("api('/api/v2/insights/mastery')"), 1)
        self.assertNotIn("fetch(", text)
        self.assertNotIn("XMLHttpRequest", text)
        self.assertNotIn("retroachievements", text.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
