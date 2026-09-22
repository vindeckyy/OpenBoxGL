#!/usr/bin/env python3
"""Flagship 8, H1: scoring engine tests (pkg/parity/parity_library_health)."""

import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pkg.parity  # noqa: F401,E402  # register flat-import finder first
from pkg.parity import parity_library_health as engine  # noqa: F401,E402

MEDIA_FIELDS = [
    "advertisement", "background", "banner", "box_3d", "box_back", "box_spine",
    "cart_back", "cart_front", "clear_logo", "disc", "fanart", "icon", "manual",
    "title_screen",
]
PATH_FIELDS = ["path", "cover", *MEDIA_FIELDS]


def healthy_game(game_id, path=None, **overrides):
    """A game that scores 100 on every dimension with a matching probe."""
    path = path or f"/games/{game_id}.exe"  # .exe: no ROM suffix, no emulator check
    game = {
        "game_id": game_id,
        "name": f"Game {game_id}",
        "path": path,
        "cover": f"/media/{game_id}/cover.png",
        "screenshots": [f"/media/{game_id}/shot1.png"],
        "platform": "PC",
        "genre": "Action",
        "year": "2020",
        "developer": "Dev",
        "description": "Desc",
    }
    for field in MEDIA_FIELDS:
        game[field] = f"/media/{game_id}/{field}.png"
    game.update(overrides)
    return game


def existing_for(games):
    """Paths the probe treats as present, derived from the fixture games."""
    existing = set()
    for game in games:
        for field in PATH_FIELDS:
            value = game.get(field)
            if isinstance(value, str) and value:
                existing.add(value)
        for shot in game.get("screenshots", []) or []:
            existing.add(str(shot))
    return existing


def make_probe(existing):
    existing = {str(Path(path)) for path in existing}

    def probe(path, *, file_only=False):
        return str(Path(str(path or ""))) in existing

    return probe


class EngineContractTests(unittest.TestCase):
    def test_weights_sum_to_100(self):
        self.assertEqual(sum(engine.DIMENSION_WEIGHTS.values()), 100)
        self.assertEqual(set(engine.DIMENSION_WEIGHTS), set(engine.DIMENSIONS))

    def test_perfect_library_scores_100(self):
        games = [healthy_game("g1"), healthy_game("g2")]
        snapshot = engine.score_library(games, {"profiles": {}}, probe=make_probe(existing_for(games)))
        self.assertEqual(snapshot["score"], 100)
        for dim in engine.DIMENSIONS:
            self.assertEqual(snapshot["dimensions"][dim]["score"], 100)
        self.assertEqual(snapshot["deductions"], [])
        self.assertEqual(snapshot["game_count"], 2)
        self.assertFalse(snapshot["dirty"])
        self.assertTrue(snapshot["full"])

    def test_empty_library_scores_100(self):
        snapshot = engine.score_library([], {}, probe=make_probe(set()))
        self.assertEqual(snapshot["score"], 100)

    def test_every_deduction_carries_game_ids(self):
        games = [
            healthy_game("g1", path="/games/missing.exe"),
            healthy_game("g2", path="/games/missing2.exe", cover=""),
        ]
        snapshot = engine.score_library(games, {"profiles": {}}, probe=make_probe(set()))
        self.assertTrue(snapshot["deductions"])
        for deduction in snapshot["deductions"]:
            self.assertTrue(deduction["game_ids"], deduction)
            self.assertIn("dimension", deduction)
            self.assertGreater(deduction["points"], 0)
            self.assertTrue(deduction["reason"])

    def test_missing_file_deduction_math(self):
        games = [healthy_game("g1", path="/games/gone.exe")]
        existing = existing_for(games) - {"/games/gone.exe"}
        snapshot = engine.score_library(games, {"profiles": {}}, probe=make_probe(existing))
        integrity = snapshot["dimensions"]["file_integrity"]
        self.assertEqual(integrity["score"], 100 - engine.DEDUCT_MISSING_GAME)
        self.assertEqual(integrity["issues"], 1)
        expected = round((integrity["score"] * 35 + 100 * 65) / 100)
        self.assertEqual(snapshot["score"], expected)

    def test_clamping_at_zero(self):
        games = [healthy_game(f"g{i}", path=f"/games/gone{i}.exe", cover="") for i in range(10)]
        snapshot = engine.score_library(games, {"profiles": {}}, probe=make_probe(set()))
        for dim in engine.DIMENSIONS:
            score = snapshot["dimensions"][dim]["score"]
            self.assertGreaterEqual(score, 0)
            self.assertLessEqual(score, 100)
        self.assertGreaterEqual(snapshot["score"], 0)

    def test_manual_entry_exempt_from_path_checks(self):
        games = [healthy_game("g1", manual_entry=True, path="")]
        issues = engine.detect_issues(games, {"profiles": {}}, probe=make_probe(set()))
        codes = {issue["code"] for issue in issues}
        self.assertNotIn("missing_game", codes)
        self.assertNotIn("missing_cover", codes)

    def test_duplicate_detection(self):
        games = [
            healthy_game("g1", steam_app_id="10"),
            healthy_game("g2", steam_app_id="10"),
            healthy_game("g3", steam_app_id="11"),
        ]
        issues = engine.detect_issues(games, {"profiles": {}}, probe=make_probe(existing_for(games)))
        dupes = [issue for issue in issues if issue["code"] == "duplicate"]
        self.assertEqual(len(dupes), 1)
        self.assertEqual(dupes[0]["game_id"], "g2")
        self.assertEqual(dupes[0]["legacy"]["type"], "Duplicate")
        self.assertEqual(dupes[0]["points"], engine.DEDUCT_DUPLICATE)

    def test_v1_legacy_shape(self):
        """detect_issues carries the exact v1 /api/health type/detail pairs."""
        missing = "/games/gone.exe"
        games = [
            healthy_game("g1", path=missing, cover=""),
            healthy_game("g2", path=missing, cover=""),
        ]
        issues = engine.detect_issues(games, {"profiles": {}}, probe=make_probe(set()))
        legacy = [(issue["legacy"]["type"], issue["legacy"]["detail"]) for issue in issues if issue["legacy"]]
        types = [kind for kind, _ in legacy]
        self.assertIn("Missing game", types)
        self.assertIn("Missing box front", types)
        self.assertIn("Duplicate", types)
        for kind, detail in legacy:
            if kind == "Missing game":
                self.assertEqual(detail, str(Path(missing)))
            if kind == "Missing box front":
                self.assertEqual(detail, "No local cover image")

    def test_artwork_weighted_by_priority(self):
        probe = make_probe(set())
        base = healthy_game("g1")
        no_cover = engine.detect_issues([healthy_game("g1", cover="")], {"profiles": {}}, probe=probe)
        no_banner = engine.detect_issues([healthy_game("g1", banner="")], {"profiles": {}}, probe=probe)
        cover_points = sum(i["points"] for i in no_cover if i["code"] == "missing_cover")
        banner_points = sum(i["points"] for i in no_banner if i["code"] == "missing_artwork" and i["detail"] == "banner")
        self.assertGreater(cover_points, banner_points)
        self.assertEqual(base["cover"], "/media/g1/cover.png")

    def test_metadata_presence_only(self):
        games = [healthy_game("g1", genre="", year="")]
        issues = engine.detect_issues(games, {"profiles": {}}, probe=make_probe(existing_for(games)))
        meta = [issue for issue in issues if issue["dimension"] == "metadata"]
        self.assertEqual(len(meta), 2)
        self.assertTrue(all(issue["points"] == engine.DEDUCT_METADATA_FIELD for issue in meta))

    def test_no_emulator_and_broken(self):
        games = [healthy_game("g1", path="/games/x.nes")]
        probe = make_probe(existing_for(games))
        issues = engine.detect_issues(games, {"profiles": {}}, probe=probe)
        emu = [issue for issue in issues if issue["code"] == "no_emulator"]
        self.assertEqual(len(emu), 1)
        self.assertEqual(emu[0]["legacy"]["type"], "No emulator")
        self.assertEqual(emu[0]["points"], engine.DEDUCT_NO_EMULATOR)
        # emulator profile present -> no deduction
        issues = engine.detect_issues(games, {"profiles": {"PC": {"path": "/emu"}}}, probe=probe)
        self.assertFalse(any(issue["code"] == "no_emulator" for issue in issues))
        # broken flag (engine-only: v1 never reported it, so no legacy mapping)
        games = [healthy_game("g1", broken=True)]
        issues = engine.detect_issues(games, {"profiles": {}}, probe=probe)
        broken = [issue for issue in issues if issue["code"] == "broken"]
        self.assertEqual(len(broken), 1)
        self.assertIsNone(broken[0]["legacy"])


class IncrementalTests(unittest.TestCase):
    def _library(self, seed, count=60):
        rng = random.Random(seed)
        games = []
        for i in range(count):
            game = healthy_game(f"g{i}")
            if rng.random() < 0.3:
                game["path"] = f"/games/missing{i}.exe"
            if rng.random() < 0.2:
                game["cover"] = ""
            if rng.random() < 0.2:
                game["genre"] = ""
            if rng.random() < 0.1:
                game["steam_app_id"] = "dup-shared"
            games.append(game)
        return games, make_probe(existing_for([g for g in games if not g["path"].startswith("/games/missing")]))

    def test_incremental_equals_full_recompute(self):
        for seed in range(5):
            games, probe = self._library(seed)
            state = {"profiles": {}}
            full = engine.score_library(games, state, probe=probe)
            rng = random.Random(1000 + seed)
            dirty = rng.sample([g["game_id"] for g in games], 12)
            # mutate some dirty games so the merge has real work to do
            for game in games:
                if game["game_id"] in dirty[:4]:
                    game["path"] = "/games/now-missing.exe"
            expected = engine.score_library(games, state, probe=probe)
            merged = engine.merge_incremental(full, games, state, dirty, probe=probe)
            self.assertEqual(merged["score"], expected["score"], f"seed {seed}")
            self.assertEqual(merged["dimensions"], expected["dimensions"], f"seed {seed}")
            self.assertEqual(len(merged["deductions"]), len(expected["deductions"]), f"seed {seed}")

    def test_incremental_empty_dirty_is_noop(self):
        games, probe = self._library(7)
        snapshot = engine.score_library(games, {"profiles": {}}, probe=probe)
        merged = engine.merge_incremental(snapshot, games, {"profiles": {}}, [], probe=probe)
        self.assertEqual(merged["score"], snapshot["score"])
        self.assertFalse(merged["dirty"])


class CacheAndJournalTests(unittest.TestCase):
    def test_health_cache_roundtrip(self):
        state = {}
        self.assertEqual(engine.get_health_cache(state), {})
        snapshot = {"score": 90, "game_count": 3}
        engine.store_health_cache(state, snapshot)
        self.assertEqual(engine.get_health_cache(state)["score"], 90)

    def test_dirty_ids_overflow_forces_rescan_flag(self):
        state = {}
        engine.mark_dirty_ids(state, [f"g{i}" for i in range(6000)])
        cache = engine.get_health_cache(state)
        self.assertTrue(cache.get("full_rescan_needed"))
        self.assertEqual(engine.take_dirty_ids(state), [])
        self.assertNotIn("full_rescan_needed", engine.get_health_cache(state))

    def test_fix_journal_capped_at_50(self):
        state = {}
        for i in range(60):
            engine.record_fix(state, {"fix_id": f"f{i}", "dimension": "duplicates"})
        journal = engine.get_fix_journal(state)
        self.assertEqual(len(journal), 50)
        self.assertEqual(journal[0]["fix_id"], "f10")
        self.assertTrue(engine.mark_fix_undone(state, "f59"))
        self.assertTrue(engine.get_fix_journal(state)[-1]["undone"])
        self.assertFalse(engine.mark_fix_undone(state, "nope"))


class RescanScheduleTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(engine.normalize_rescan_setting("daily"), "daily")
        self.assertEqual(engine.normalize_rescan_setting("bogus"), "weekly")
        self.assertEqual(engine.normalize_rescan_setting(None), "weekly")

    def test_due_logic(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        self.assertTrue(engine.rescan_due({"health_rescan": "weekly"}, now))
        self.assertFalse(engine.rescan_due({"health_rescan": "off"}, now))
        self.assertFalse(engine.rescan_due({"health_rescan": "on_startup"}, now))
        recent = (now - timedelta(hours=1)).isoformat()
        self.assertFalse(engine.rescan_due({"health_rescan": "daily", "last_health_rescan": recent}, now))
        old = (now - timedelta(days=8)).isoformat()
        self.assertTrue(engine.rescan_due({"health_rescan": "weekly", "last_health_rescan": old}, now))


if __name__ == "__main__":
    unittest.main()
