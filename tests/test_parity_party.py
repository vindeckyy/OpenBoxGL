"""Tests for pkg/parity/parity_party.py."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity.parity_library_sync import SyncValidationError  # noqa: E402
from pkg.parity.parity_party import (  # noqa: E402
    COUCH_PLATFORMS,
    PARTY_QUEUE_LIMIT,
    THEME_PRESETS,
    apply_theme_preset,
    build_deck,
    build_party_queue,
    deck_queue,
    delete_deck,
    eligible_party_games,
    empty_queue_reason,
    find_deck,
    import_decks,
    list_decks,
    queue_exclusion_breakdown,
    read_shared_decks,
    save_deck,
    seeded_shuffle,
    write_shared_deck,
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


class QueueExclusionBreakdownTest(unittest.TestCase):
    def test_empty_library(self):
        breakdown = queue_exclusion_breakdown([])
        self.assertEqual(breakdown["total"], 0)
        self.assertIn("import games", empty_queue_reason(breakdown))

    def test_missing_max_players_is_top_blocker(self):
        games = [game(1, max_players=None), game(2, max_players=None)]
        breakdown = queue_exclusion_breakdown(games, players=2)
        self.assertEqual(breakdown, {
            "total": 2, "hidden": 0, "unusable_path": 0, "too_few_players": 2,
            "no_controller_or_platform": 0, "over_budget": 0,
        })
        self.assertIn("2 players", empty_queue_reason(breakdown, 2))

    def test_mirrors_eligibility_rules(self):
        games = [
            game(1, hidden=True),
            game(2, path_exists=False),
            game(3, platform="PC", controller_support="", max_players=4),
            game(4, play_count=4, playtime_seconds=4 * 3 * 3600),
        ]
        breakdown = queue_exclusion_breakdown(games, players=2, minutes=30)
        self.assertEqual(breakdown["hidden"], 1)
        self.assertEqual(breakdown["unusable_path"], 1)
        self.assertEqual(breakdown["no_controller_or_platform"], 1)
        self.assertEqual(breakdown["over_budget"], 1)
        # budget is a build-time filter only: g-4 stays eligible, the rest drop
        self.assertEqual([g["game_id"] for g in eligible_party_games(games, players=2)], ["g-4"])

    def test_reason_names_largest_blocker(self):
        breakdown = {"total": 5, "hidden": 0, "unusable_path": 4,
                     "too_few_players": 1, "no_controller_or_platform": 0, "over_budget": 0}
        self.assertIn("missing game files", empty_queue_reason(breakdown))


class SeedAndPresetTest(unittest.TestCase):
    def test_seeded_order_is_reproducible(self):
        games = [game(i, rating=3) for i in range(10)]
        first = build_party_queue(games, seed="abcd1234")
        second = build_party_queue(games, seed="abcd1234")
        self.assertEqual(first, second)
        self.assertEqual(set(first), {f"g-{i}" for i in range(10)})
        self.assertEqual(seeded_shuffle(["a", "b", "c"], "abcd1234"), seeded_shuffle(["a", "b", "c"], "abcd1234"))
        with self.assertRaises(SyncValidationError):
            seeded_shuffle(["a"], "not-hex")

    def test_seed_selects_from_the_same_rating_ranked_set(self):
        games = [game(i, rating=5 if i < 3 else 1) for i in range(8)]
        seeded = set(build_party_queue(games, seed="abcd1234", limit=3))
        unseeded = set(build_party_queue(games, limit=3))
        self.assertEqual(seeded, unseeded)

    def test_nineties_racers_preset(self):
        racer = game(1, year=1994, name="Kart Fury", genre="Racing", rating=5)
        modern = game(2, year=2019, name="Kart Fury 2", genre="Racing", rating=5)
        other = game(3, year=1994, name="Puzzle Thing", genre="Puzzle", rating=5)
        result = apply_theme_preset([racer, modern, other], "nineties_racers")
        self.assertEqual([item["game_id"] for item in result], ["g-1"])
        queue = build_party_queue([racer, modern, other], preset="nineties_racers")
        self.assertEqual(queue, ["g-1"])

    def test_coop_only_preset_needs_evidence(self):
        coop = game(1, genre="Co-op Action", rating=5)
        solo = game(2, genre="Solo Adventure", rating=5)
        controller = game(3, controller_support="Yes", rating=5)
        result = apply_theme_preset([coop, solo, controller], "coop_only")
        self.assertEqual({item["game_id"] for item in result}, {"g-1", "g-3"})

    def test_eight_plus_preset(self):
        big = game(1, max_players=8, rating=5)
        small = game(2, max_players=4, rating=5)
        result = apply_theme_preset([big, small], "eight_plus")
        self.assertEqual([item["game_id"] for item in result], ["g-1"])

    def test_unknown_preset_is_rejected_and_none_is_a_noop(self):
        games = [game(1)]
        self.assertEqual(apply_theme_preset(games, None), games)
        self.assertEqual(apply_theme_preset(games, ""), games)
        with self.assertRaises(SyncValidationError):
            apply_theme_preset(games, "not-a-preset")
        self.assertIn("nineties_racers", THEME_PRESETS)


class DeckTest(unittest.TestCase):
    def _games(self):
        return [game(i, rating=5 - i % 3, year=1995 + i, genre="Racing") for i in range(8)]

    def test_deck_queue_reproduces_shared_order_from_the_seed(self):
        deck = build_deck(self._games(), name="Friday Night", players=4, seed="deadbeef")
        queue = deck_queue(deck)
        self.assertEqual(queue, deck_queue(deck))
        self.assertEqual(queue, seeded_shuffle(deck["game_ids"], deck["seed"]))
        self.assertEqual(set(queue), set(deck["game_ids"]))
        self.assertEqual(deck["players"], 4)

    def test_build_deck_rejects_unknown_game_ids(self):
        with self.assertRaises(SyncValidationError):
            build_deck(self._games(), name="Bad", game_ids=["g-not-here"])

    def test_save_find_delete_round_trip(self):
        state = {}
        deck = build_deck(self._games(), name="Friday Night", players=2)
        save_deck(state, deck)
        self.assertEqual(len(list_decks(state)), 1)
        self.assertEqual(find_deck(state, deck["deck_id"])["name"], "Friday Night")
        self.assertEqual(find_deck(state, "Friday Night")["deck_id"], deck["deck_id"])
        self.assertTrue(delete_deck(state, deck["deck_id"]))
        self.assertEqual(list_decks(state), [])
        self.assertFalse(delete_deck(state, deck["deck_id"]))

    def test_save_is_upsert_by_name(self):
        state = {}
        first = build_deck(self._games(), name="Friday Night")
        save_deck(state, first)
        second = build_deck(self._games(), name="Friday Night", players=8)
        save_deck(state, second)
        decks = list_decks(state)
        self.assertEqual(len(decks), 1)
        self.assertEqual(decks[0]["players"], 8)

    def test_shared_folder_round_trip_and_tamper_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            deck = build_deck(self._games(), name="Friday Night", seed="deadbeef")
            write_shared_deck(directory, deck)
            shared = read_shared_decks(directory)
            self.assertEqual(len(shared), 1)
            self.assertEqual(shared[0]["name"], "Friday Night")
            self.assertEqual(deck_queue(shared[0]), deck_queue(deck))
            self.assertTrue(shared[0]["shared"])

            target = next((Path(directory) / "openbox-household-v1" / "party-decks").glob("*.json"))
            target.write_text(target.read_text().replace("Friday Night", "Spoofed"))
            self.assertEqual(read_shared_decks(directory), [])

            state = {}
            self.assertEqual(import_decks(state, read_shared_decks(directory)), 0)
            self.assertEqual(import_decks(state, [deck]), 1)
            self.assertEqual(import_decks(state, [deck]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

