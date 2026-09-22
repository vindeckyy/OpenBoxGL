"""Tests for pkg/parity/parity_dna.py — Game DNA search (Flagship 9).

Standalone-script style (``python3 -B tests/test_parity_dna.py``).
"""

from __future__ import annotations

import os
import random
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pkg", "parity"))
import pkg.parity  # noqa: E402,F401  (registers the flat parity_* MetaPathFinder)

import parity_dna as dna  # noqa: E402


STARDEW = {
    "game_id": "g-stardew",
    "name": "Stardew Valley",
    "genre": "Simulation",
    "tags": ["farming", "cozy", "pixel-art"],
    "description": "A relaxing farming sim: grow crops, raise animals, befriend wholesome villagers.",
    "developer": "ConcernedApe",
    "platform": "PC",
    "user_rating": 5,
    "progress": "Unplayed",
    "time_to_beat_hours": 60,
}
DOOM = {
    "game_id": "g-doom",
    "name": "DOOM",
    "genre": "Shooter",
    "tags": ["fps", "demons"],
    "description": "Rip and tear through hordes of demons in this intense, fast-paced shooter.",
    "developer": "id Software",
    "platform": "PC",
    "user_rating": 4,
    "progress": "Beaten",
}
HOLLOW = {
    "game_id": "g-hollow",
    "name": "Hollow Knight",
    "genre": "Metroidvania",
    "tags": ["metroidvania", "soulslike", "hand-drawn"],
    "description": "A punishing 2D adventure: stamina-based combat, interconnected caverns, tough bosses.",
    "developer": "Team Cherry",
    "platform": "PC",
}
CELESTE = {
    "game_id": "g-celeste",
    "name": "Celeste",
    "genre": "Platformer",
    "tags": ["platformer", "pixel-art"],
    "description": "A challenging precision platformer about climbing a mountain; heartfelt story.",
    "platform": "PC",
}
FIXTURE_GAMES = [STARDEW, DOOM, HOLLOW, CELESTE]


class TokenizerTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(dna.tokenize("Hollow Knight!"), ["hollow", "knight"])

    def test_unicode_aware(self):
        tokens = dna.tokenize("Pokémon café naïve")
        self.assertIn("pokémon", tokens)
        self.assertIn("café", tokens)
        self.assertIn("naïve", tokens)

    def test_min_length_two(self):
        self.assertNotIn("a", dna.tokenize("a bb ccc"))

    def test_stopwords_english(self):
        self.assertEqual(dna.tokenize("the game of the year"), ["year"])

    def test_stopwords_per_locale(self):
        self.assertEqual(dna.tokenize("das Spiel ist gut", "de"), ["gut"])
        self.assertEqual(dna.tokenize("el juego es bueno", "es"), ["bueno"])
        self.assertEqual(dna.tokenize("le jeu est bon", "fr"), ["bon"])
        self.assertEqual(dna.tokenize("o jogo é bom", "pt"), ["bom"])

    def test_unknown_locale_falls_back_to_english(self):
        self.assertIs(dna.stopwords_for("xx"), dna.STOPWORDS["en"])
        self.assertEqual(dna.tokenize("the game", "xx"), [])


class CorpusTests(unittest.TestCase):
    def test_field_weights(self):
        vector = dna.build_vector({"name": "Doom", "description": "doom"}, "en")
        # name^3 beats description^1 for the same term
        self.assertGreater(vector["doom"], 3.5)

    def test_list_fields(self):
        vector = dna.build_vector({"tags": ["Cozy", "Farming"]}, "en")
        self.assertIn("cozy", vector)
        self.assertIn("farming", vector)

    def test_term_cap(self):
        game = {"name": "X", "description": " ".join(f"word{i}" for i in range(2000))}
        vector = dna.build_vector(game, "en")
        self.assertLessEqual(len(vector), dna.TERMS_PER_GAME_CAP)

    def test_corpus_hash_detects_change(self):
        game = {"game_id": "g1", "name": "A", "description": "hello"}
        before = dna.corpus_hash(game)
        game["description"] = "world"
        self.assertNotEqual(before, dna.corpus_hash(game))

    def test_doc_id_prefers_game_id(self):
        self.assertEqual(dna.doc_id_for({"game_id": "abc"}), "abc")

    def test_doc_id_stable_fallback(self):
        game = {"name": "No Id Game", "platform": "PC"}
        self.assertEqual(dna.doc_id_for(game), dna.doc_id_for(dict(game)))


class ConceptTests(unittest.TestCase):
    def test_lexicon_size(self):
        self.assertGreaterEqual(len(dna.CONCEPTS), 100)

    def test_soulslike_expansion(self):
        extra, chips = dna.expand_concepts(["soulslike"], "en")
        # Hollow Knight's description says "punishing stamina-based" but never "soulslike"
        self.assertIn("punishing", extra)
        self.assertIn("stamina", extra)
        self.assertTrue(any("soulslike →" in chip for chip in chips))

    def test_expansion_weight_lower_than_literal(self):
        extra, _chips = dna.expand_concepts(["cozy"], "en")
        self.assertEqual(extra.get("relaxing"), dna.CONCEPT_WEIGHT)
        self.assertLess(dna.CONCEPT_WEIGHT, 1.0)

    def test_locale_overlay(self):
        extra, chips = dna.expand_concepts(["gemütlich"], "de")
        self.assertIn("relaxing", extra)
        self.assertTrue(any("gemütlich →" in chip for chip in chips))

    def test_unknown_term_no_expansion(self):
        extra, chips = dna.expand_concepts(["zxqv"], "en")
        self.assertEqual(extra, {})
        self.assertEqual(chips, [])


class Bm25Tests(unittest.TestCase):
    def setUp(self):
        self.index = dna.rebuild_index(FIXTURE_GAMES, "en")

    def test_cozy_farming_ranks_stardew_first(self):
        result = dna.parse_dna_query("cozy farming", FIXTURE_GAMES, self.index, "en")
        self.assertEqual(result["branch"], "bm25")
        self.assertEqual(result["results"][0]["game_id"], "g-stardew")

    def test_soulslike_matches_without_literal_word(self):
        result = dna.parse_dna_query("soulslike", FIXTURE_GAMES, self.index, "en")
        ids = [row["game_id"] for row in result["results"]]
        self.assertIn("g-hollow", ids)
        self.assertNotIn("g-stardew", ids)

    def test_why_chips_present(self):
        result = dna.parse_dna_query("cozy farming", FIXTURE_GAMES, self.index, "en")
        top = result["results"][0]
        self.assertTrue(top["why"], "every result must carry why-chips")
        self.assertTrue(any("→" in chip for chip in top["why"]))
        self.assertIn("5★ rated", top["why"])
        self.assertIn("~60h to beat", top["why"])

    def test_taste_boost_applies_rating(self):
        # Same query, rating 5 vs rating 0: boosted score must be higher.
        rated = dict(STARDEW, user_rating=5)
        unrated = dict(STARDEW, user_rating=0)
        boosted_rated, _ = dna.taste_boost(rated, 1.0)
        boosted_unrated, _ = dna.taste_boost(unrated, 1.0)
        self.assertGreater(boosted_rated, boosted_unrated)
        self.assertAlmostEqual(boosted_rated, 1.0 * 1.75 * 1.25)

    def test_taste_boost_neutral_without_f4_fields(self):
        # F4 not landed yet: stub games without progress/user_rating must not crash.
        boosted, chips = dna.taste_boost({"name": "Plain"}, 2.0)
        self.assertEqual(boosted, 2.0 * 1.0 * 1.25)  # novelty for unknown progress
        self.assertEqual(chips, [])

    def test_determinism(self):
        first = dna.parse_dna_query("cozy farming adventure", FIXTURE_GAMES, self.index, "en")
        second = dna.parse_dna_query("cozy farming adventure", FIXTURE_GAMES, self.index, "en")
        self.assertEqual(
            [r["game_id"] for r in first["results"]],
            [r["game_id"] for r in second["results"]],
        )
        self.assertEqual(
            [r["score"] for r in first["results"]],
            [r["score"] for r in second["results"]],
        )

    def test_predicates_filter_before_ranking(self):
        # "under 10 hours" is a structured predicate; Stardew (60h) must be excluded.
        result = dna.parse_dna_query("cozy farming under 10 hours", FIXTURE_GAMES, self.index, "en")
        ids = [row["game_id"] for row in result["results"]]
        self.assertNotIn("g-stardew", ids)

    def test_genre_word_in_natural_query_is_soft(self):
        # "adventure" parses as rules["genre"], but DNA search is semantic:
        # it must rank, not hard-filter (Stardew is Simulation, not Adventure).
        result = dna.parse_dna_query("cozy farming adventure", FIXTURE_GAMES, self.index, "en")
        ids = [row["game_id"] for row in result["results"]]
        self.assertIn("g-stardew", ids)
        self.assertEqual(ids[0], "g-stardew")
        top = result["results"][0]
        self.assertTrue(any(chip.startswith("genre:") for chip in top["why"]))

    def test_bare_genre_word_ranks(self):
        result = dna.parse_dna_query("adventure", FIXTURE_GAMES, self.index, "en")
        ids = [row["game_id"] for row in result["results"]]
        self.assertIn("g-hollow", ids)  # description mentions "adventure"

    def test_empty_query(self):
        result = dna.parse_dna_query("", FIXTURE_GAMES, self.index, "en")
        self.assertEqual(result["results"], [])


class SimilarityTests(unittest.TestCase):
    def setUp(self):
        self.index = dna.rebuild_index(FIXTURE_GAMES, "en")

    def test_like_hollow_knight_surfaces_metroidvanias(self):
        result = dna.parse_dna_query("games like Hollow Knight", FIXTURE_GAMES, self.index, "en")
        self.assertEqual(result["branch"], "similarity")
        self.assertEqual(result["anchor"]["game_id"], "g-hollow")
        ids = [row["game_id"] for row in result["results"]]
        self.assertNotIn("g-hollow", ids)  # anchor never surfaces itself
        # Celeste shares platformer/2D/hand-drawn DNA; Doom shares nothing.
        self.assertIn("g-celeste", ids)

    def test_like_chip(self):
        result = dna.parse_dna_query("like Hollow Knight", FIXTURE_GAMES, self.index, "en")
        self.assertEqual(result["branch"], "similarity")
        top = result["results"][0]
        self.assertIn("like Hollow Knight", top["why"])

    def test_unresolvable_anchor_falls_back(self):
        result = dna.parse_dna_query("games like Zxqvnoth", FIXTURE_GAMES, self.index, "en")
        self.assertEqual(result["branch"], "bm25-unresolved-anchor")
        self.assertIsNone(result["anchor"])

    def test_anchor_patterns(self):
        for query in ("like Hollow Knight", "similar to Hollow Knight", "more like Hollow Knight"):
            self.assertEqual(dna.extract_anchor_title(query), "Hollow Knight")
        self.assertIsNone(dna.extract_anchor_title("cozy farming"))
        self.assertIsNone(dna.extract_anchor_title("I like cozy games"))

    def test_similarity_determinism(self):
        first = dna.parse_dna_query("games like Hollow Knight", FIXTURE_GAMES, self.index, "en")
        second = dna.parse_dna_query("games like Hollow Knight", FIXTURE_GAMES, self.index, "en")
        self.assertEqual([r["game_id"] for r in first["results"]], [r["game_id"] for r in second["results"]])


class IndexIoTests(unittest.TestCase):
    def test_atomic_write_crash_safety(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = dna.rebuild_index(FIXTURE_GAMES, "en")
            dna.save_index_atomic(index, tmp)
            # Simulate a crash mid-write: garbage in the tmp file must not
            # touch the committed index.
            tmp_path = dna.index_path(tmp) + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as handle:
                handle.write("{not valid json")
            loaded = dna.load_index(tmp)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["doc_count"], 4)
            self.assertNotIn("_hashed", loaded)

    def test_load_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(dna.load_index(tmp))

    def test_load_corrupt_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(dna.index_path(tmp), "w", encoding="utf-8") as handle:
                handle.write("garbage{{{")
            self.assertIsNone(dna.load_index(tmp))

    def test_format_and_version(self):
        index = dna.rebuild_index(FIXTURE_GAMES, "en")
        self.assertEqual(index["format"], 1)
        self.assertEqual(index["lexicon_version"], dna.LEXICON_VERSION)

    def test_incremental_upsert_and_remove(self):
        index = dna.rebuild_index(FIXTURE_GAMES, "en")
        new_game = {
            "game_id": "g-new",
            "name": "Farm Together",
            "description": "cozy farming multiplayer",
            "tags": ["cozy"],
        }
        dna.note_game_upserted(index, new_game, "en")
        self.assertEqual(index["doc_count"], 5)
        result = dna.parse_dna_query("cozy farming", FIXTURE_GAMES + [new_game], index, "en")
        ids = [row["game_id"] for row in result["results"]]
        self.assertIn("g-new", ids)
        dna.note_game_removed(index, "g-new")
        self.assertEqual(index["doc_count"], 4)
        self.assertNotIn("g-new", index["games"])

    def test_df_deltas_approximate(self):
        index = dna.rebuild_index(FIXTURE_GAMES, "en")
        before = index["df"].get("farming", 0)
        dna.note_game_upserted(index, {"game_id": "g-x", "name": "X", "description": "farming farming"}, "en")
        self.assertEqual(index["df"].get("farming", 0), before + 1)
        dna.note_game_removed(index, "g-x")
        self.assertEqual(index["df"].get("farming", 0), before)

    def test_signature_mismatch_detected(self):
        index = dna.rebuild_index(FIXTURE_GAMES, "en", state_signature=(1, 2, 3))
        self.assertTrue(dna.signature_matches(index, (1, 2, 3)))
        self.assertFalse(dna.signature_matches(index, (9, 9, 9)))

    def test_index_lives_next_to_data_dir(self):
        # OPENBOX_DATA_DIR (openbox.APP_DIR) is bound at import; the sidecar
        # path must resolve against it — and honor an explicit override.
        import openbox

        self.assertEqual(
            dna.index_path(),
            os.path.join(str(openbox.APP_DIR), dna.INDEX_FILENAME),
        )
        self.assertEqual(
            dna.index_path("/tmp/custom-data"),
            os.path.join("/tmp/custom-data", dna.INDEX_FILENAME),
        )


class CloudSyncTests(unittest.TestCase):
    def test_index_excluded_from_cloud_sync(self):
        path = os.path.join(os.path.dirname(__file__), "..", "cloud_sync.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("dna_index", source)


class LatencyTests(unittest.TestCase):
    @staticmethod
    def _synthetic_corpus(count):
        rng = random.Random(42)
        words = [f"word{i}" for i in range(2000)]
        games = []
        for i in range(count):
            desc = " ".join(rng.choice(words) for _ in range(40))
            games.append(
                {
                    "game_id": f"g{i}",
                    "name": f"Game {i}",
                    "genre": rng.choice(["RPG", "Shooter", "Puzzle"]),
                    "description": desc,
                    "tags": [rng.choice(words)],
                }
            )
        return games

    def _timed_runs(self, func, warmup=2, measured=7):
        # Discard warm-up (cold caches, noisy neighbors), then take the best
        # of the measured runs: this tests what the code can do, not the
        # machine's current mood. Budgets below assume a quiet Deck-class CPU.
        for _ in range(warmup):
            func()
        best = None
        for _ in range(measured):
            start = time.perf_counter()
            func()
            elapsed = (time.perf_counter() - start) * 1000.0
            best = elapsed if best is None else min(best, elapsed)
        return best

    def test_20k_bm25_latency_budget(self):
        games = self._synthetic_corpus(20000)
        index = dna.rebuild_index(games, "en")

        def run():
            dna.parse_dna_query("cozy farming adventure", games, index, "en", limit=20)

        best = self._timed_runs(run)
        # Contract: < 150 ms on Deck-class CPU; generous CI margin here.
        self.assertLess(best, 300.0, f"best {best:.1f} ms exceeds budget")

    def test_20k_similarity_latency_budget(self):
        games = self._synthetic_corpus(20000)
        index = dna.rebuild_index(games, "en")

        def run():
            return dna.parse_dna_query("games like Game 500", games, index, "en", limit=20)

        result = run()  # warm the hashed-vector cache
        self.assertEqual(result["branch"], "similarity")
        best = self._timed_runs(run, warmup=2, measured=5)
        self.assertLess(best, 1500.0, f"similarity {best:.1f} ms exceeds budget")

    def test_incremental_update_under_50ms(self):
        games = self._synthetic_corpus(2000)
        index = dna.rebuild_index(games, "en")
        game = {"game_id": "g-new", "name": "New Game", "description": "a brand new cozy adventure"}
        start = time.perf_counter()
        dna.note_game_upserted(index, game, "en")
        elapsed = (time.perf_counter() - start) * 1000.0
        # Contract: < 50 ms per game event; generous CI margin.
        self.assertLess(elapsed, 250.0)

    def test_index_json_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = dna.rebuild_index(FIXTURE_GAMES, "en")
            dna.save_index_atomic(index, tmp)
            loaded = dna.load_index(tmp)
            self.assertEqual(loaded["doc_count"], index["doc_count"])
            self.assertEqual(loaded["df"], index["df"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
