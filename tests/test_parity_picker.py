"""Tests for pkg.parity.parity_picker."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity.parity_picker import _days_since_last_play, _eligibility, _estimated_minutes_for_unplayed, _fits_minutes, _game_history_seconds, _median, _score, pick_games  # noqa: E402


class ParityPickerTests(unittest.TestCase):
    def _game(self, **kwargs):
        defaults = {
            "id": 1,
            "name": "Game",
            "platform": "PC",
            "genre": "Action",
            "year": 1996,
            "favorite": False,
            "rating": 0,
            "play_count": 0,
            "playtime_seconds": 0,
            "last_played": "",
            "path_exists": True,
            "hidden": False,
            "hide_in_bigbox": False,
            "store_installed": False,
            "max_players": 1,
        }
        defaults.update(kwargs)
        return defaults

    def test_empty_library(self):
        self.assertEqual(pick_games([], [], {}), [])

    def test_filters_hidden_and_missing(self):
        games = [
            self._game(id=1, name="Visible"),
            self._game(id=2, name="Hidden", hidden=True),
            self._game(id=3, name="Missing", path_exists=False),
        ]
        picks = pick_games(games, [], {})
        self.assertEqual([p["id"] for p in picks], [1])

    def test_familiarity_new_excludes_played(self):
        games = [
            self._game(id=1, name="New"),
            self._game(id=2, name="Played", play_count=1),
        ]
        picks = pick_games(games, [], {"familiarity": "new"})
        self.assertEqual([p["id"] for p in picks], [1])

    def test_familiarity_favorite(self):
        games = [
            self._game(id=1, name="Not favorite", rating=3),
            self._game(id=2, name="Favorite", favorite=True, rating=5),
        ]
        picks = pick_games(games, [], {"familiarity": "favorite"})
        self.assertEqual([p["id"] for p in picks], [2])

    def test_mood_action(self):
        games = [
            self._game(id=1, name="FPS", genre="First-Person Shooter"),
            self._game(id=2, name="Puzzle", genre="Puzzle"),
        ]
        picks = pick_games(games, [], {"mood": "action"})
        self.assertEqual([p["id"] for p in picks], [1])

    def test_mood_retro_year(self):
        games = [
            self._game(id=1, name="Old", year=1990),
            self._game(id=2, name="New", year=2010),
        ]
        picks = pick_games(games, [], {"mood": "retro"})
        self.assertEqual([p["id"] for p in picks], [1])

    def test_party_requires_players(self):
        games = [
            self._game(id=1, name="Solo", max_players=1),
            self._game(id=2, name="Couch", max_players=4),
        ]
        picks = pick_games(games, [], {"mood": "party", "players": 2})
        self.assertEqual([p["id"] for p in picks], [2])

    def test_minutes_hides_long_sessions(self):
        # Median 90 minutes should be excluded from a 30-minute slot.
        games = [
            self._game(id=1, name="Short", play_count=1),
            self._game(id=2, name="Long", play_count=1),
        ]
        history = [
            {"game_id": 1, "seconds": 1200, "started": "2026-01-01T00:00:00Z"},
            {"game_id": 2, "seconds": 5400, "started": "2026-01-01T00:00:00Z"},
        ]
        picks = pick_games(games, history, {"minutes": 30})
        self.assertEqual([p["id"] for p in picks], [1])

    def test_reasons_include_name(self):
        games = [self._game(id=1, name="Quake")]
        picks = pick_games(games, [], {})
        self.assertEqual(len(picks), 1)
        self.assertEqual(picks[0]["reason_params"]["name"], "Quake")

    def test_limit_three(self):
        games = [self._game(id=i, name=f"Game {i}") for i in range(1, 10)]
        picks = pick_games(games, [], {})
        self.assertLessEqual(len(picks), 3)

    def test_playlist_scope_pre_filtered(self):
        # pick_games expects caller to pre-filter games; verify it doesn't choke.
        games = [self._game(id=1, name="A"), self._game(id=2, name="B")]
        picks = pick_games(games, [], {})
        self.assertEqual(len(picks), 2)

    def test_eligibility_returns_reason(self):
        game = self._game(id=1, name="Quake", favorite=True, last_played="2025-01-01T00:00:00Z")
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        ok, key, params = _eligibility(game, [], {"mood": "any", "familiarity": "any", "players": 1, "minutes": 0}, now)
        self.assertTrue(ok)
        self.assertIn("name", params)

    def test_score_mood_and_time_bonuses(self):
        game = self._game(id=1, name="Quake", genre="Shooter")
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        s = _score(game, [], {"mood": "action", "minutes": 60, "players": 1}, now)
        self.assertGreater(s, 0)

    def test_median_and_history_helpers(self):
        self.assertEqual(_median([]), 0.0)
        self.assertEqual(_median([5]), 5.0)
        self.assertEqual(_median([1, 2, 3, 4]), 2.5)
        history = [
            {"game_id": 1, "seconds": "bad"},
            {"game_id": 1, "seconds": 60},
            {"game_id": 1, "seconds": 120},
        ]
        self.assertEqual(_game_history_seconds(1, history), [60.0, 120.0])

    def test_days_since_last_play(self):
        game = self._game(id=1, name="Quake", last_played="2026-01-01T00:00:00Z")
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        self.assertEqual(_days_since_last_play(game, [], now), 245)

    def test_fits_minutes_no_history(self):
        # Without history, genre estimates decide inclusion.
        rpg = self._game(id=1, name="RPG", genre="RPG")
        puzzle = self._game(id=2, name="Puzzle", genre="Puzzle")
        self.assertTrue(_fits_minutes(puzzle, [], 30))
        self.assertFalse(_fits_minutes(rpg, [], 30))

    def test_estimated_minutes_for_unplayed(self):
        self.assertEqual(_estimated_minutes_for_unplayed(self._game(genre="RPG")), 120.0)
        self.assertEqual(_estimated_minutes_for_unplayed(self._game(genre="Shooter")), 60.0)
        self.assertEqual(_estimated_minutes_for_unplayed(self._game(genre="Puzzle")), 30.0)
        self.assertEqual(_estimated_minutes_for_unplayed(self._game(genre="Unknown")), 45.0)

    def test_weighted_pick_top_three(self):
        games = [self._game(id=i, name=f"Game {i}", rating=5) for i in range(1, 5)]
        games[2]["favorite"] = True
        picks = pick_games(games, [], {"limit": 3})
        self.assertLessEqual(len(picks), 3)
        ids = [p["id"] for p in picks]
        self.assertEqual(len(ids), len(set(ids)))

    def test_eligibility_rejections(self):
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        self.assertFalse(_eligibility(self._game(hidden=True), [], {}, now)[0])
        self.assertFalse(_eligibility(self._game(path_exists=False, store_installed=False), [], {}, now)[0])
        self.assertFalse(_eligibility(self._game(max_players=1), [], {"players": 2}, now)[0])
        self.assertFalse(_eligibility(self._game(genre="RPG"), [], {"mood": "action"}, now)[0])
        self.assertFalse(_eligibility(self._game(play_count=1), [], {"familiarity": "new"}, now)[0])
        self.assertFalse(_eligibility(self._game(play_count=1), [], {"familiarity": "favorite"}, now)[0])

    def test_fits_minutes_session_range(self):
        game = self._game(id=1)
        history = [{"game_id": 1, "seconds": 900}, {"game_id": 1, "seconds": 1200}]
        self.assertTrue(_fits_minutes(game, history, 20))
        self.assertTrue(_fits_minutes(game, history, 20))  # median 1050 <= 20*60*1.5=1800

    def test_days_since_last_play_history_fallback(self):
        game = self._game(id=1, last_played="not-a-date")
        history = [{"game_id": 1, "started": "2026-01-01T00:00:00Z", "seconds": 1}]
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        self.assertEqual(_days_since_last_play(game, history, now), 245)

    def test_score_with_no_history(self):
        game = self._game(id=1, favorite=True)
        now = datetime(2026, 9, 3, tzinfo=timezone.utc)
        s = _score(game, [], {"mood": "any", "players": 1, "minutes": 0}, now)
        self.assertGreater(s, 10)


if __name__ == "__main__":
    unittest.main()
