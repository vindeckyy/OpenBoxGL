"""Tests for pkg/parity/parity_party.py."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity.parity_party import (  # noqa: E402
    COUCH_PLATFORMS,
    PARTY_QUEUE_LIMIT,
    build_party_queue,
    eligible_party_games,
)


def game(game_id, **overrides):
    base = {
        "id": game_id,
        "game_id": f"g-{game_id}",
        "name": f"Game {game_id}",
        "platform": "SNES",
        "path": "/bin/true",
        "path_exists": True,
        "max_players": 4,
        "rating": 3,
    }
    base.update(overrides)
    return base


class EligiblePartyGamesTest(unittest.TestCase):
    def test_console_qualifies_without_controller_flag(self):
        games = [game(1)]
        self.assertEqual(len(eligible_party_games(games, players=2)), 1)

    def test_pc_requires_controller_support(self):
        pc = game(1, platform="PC", controller_support="")
        self.assertEqual(eligible_party_games([pc], players=2), [])
        pc["controller_support"] = "Gamepad"
        self.assertEqual(len(eligible_party_games([pc], players=2)), 1)

    def test_unknown_platform_requires_controller_support(self):
        weird = game(1, platform="Some Future Console")
        self.assertEqual(eligible_party_games([weird], players=2), [])
        weird["controller_support"] = "Yes"
        self.assertEqual(len(eligible_party_games([weird], players=2)), 1)

    def test_max_players_filter(self):
        solo = game(1, max_players=1)
        duo = game(2, max_players=2)
        self.assertEqual([g["game_id"] for g in eligible_party_games([solo, duo], players=2)], ["g-2"])

    def test_hidden_and_bigbox_flags_excluded(self):
        hidden = game(1, hidden=True)
        bigbox_hidden = game(2, hide_in_bigbox=True)
        visible = game(3)
        self.assertEqual(
            [g["game_id"] for g in eligible_party_games([hidden, bigbox_hidden, visible], players=2)],
            ["g-3"],
        )

    def test_missing_path_excluded_unless_store_installed(self):
        missing = game(1, path_exists=False)
        self.assertEqual(eligible_party_games([missing], players=2), [])
        missing["store_installed"] = True
        self.assertEqual(len(eligible_party_games([missing], players=2)), 1)

    def test_non_dict_entries_skipped(self):
        self.assertEqual(eligible_party_games([None, "junk", game(1)], players=2), [game(1)])


class BuildPartyQueueTest(unittest.TestCase):
    def test_empty_library(self):
        self.assertEqual(build_party_queue([]), [])

    def test_rating_descending(self):
        games = [game(1, rating=1), game(2, rating=5), game(3, rating=3)]
        queue = build_party_queue(games)
        self.assertEqual(queue[0], "g-2")

    def test_cap_at_limit(self):
        games = [game(i, rating=5) for i in range(60)]
        queue = build_party_queue(games)
        self.assertEqual(len(queue), PARTY_QUEUE_LIMIT)
        self.assertEqual(len(queue), 50)

    def test_explicit_limit(self):
        games = [game(i, rating=5) for i in range(10)]
        self.assertEqual(len(build_party_queue(games, limit=4)), 4)

    def test_minutes_budget_skips_marathon_games(self):
        # 30-minute session: a game averaging 3h per sitting is out
        # (budget is 3x the session length, same factor as the picker).
        marathon = game(1, play_count=4, playtime_seconds=4 * 3 * 3600)
        quick = game(2, play_count=10, playtime_seconds=10 * 1200)
        queue = build_party_queue([marathon, quick], minutes=30)
        self.assertEqual(queue, ["g-2"])

    def test_minutes_zero_disables_budget(self):
        marathon = game(1, play_count=4, playtime_seconds=4 * 3 * 3600)
        self.assertEqual(build_party_queue([marathon], minutes=0), ["g-1"])

    def test_returns_game_ids(self):
        games = [game(1), game(2)]
        queue = build_party_queue(games)
        self.assertEqual(set(queue), {"g-1", "g-2"})

    def test_couch_platforms_cover_consoles_and_arcade(self):
        for platform in ("SNES", "PlayStation 2", "Xbox 360", "Arcade", "Switch"):
            self.assertIn(platform, COUCH_PLATFORMS)
        for platform in ("PC", "Windows", "Linux", "macOS"):
            self.assertNotIn(platform, COUCH_PLATFORMS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
