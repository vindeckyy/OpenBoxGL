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


if __name__ == "__main__":
    unittest.main(verbosity=2)
