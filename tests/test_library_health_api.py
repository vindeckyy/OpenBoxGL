#!/usr/bin/env python3
"""Flagship 8, H2/H3: cached v2 health API, fix queue with dry-run + undo."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pkg.parity  # noqa: F401,E402  # register flat-import finder first
import handlers.library_health as health  # noqa: F401,E402
from pkg.parity import parity_library_health as engine  # noqa: F401,E402
from api_errors import BadRequest  # noqa: F401,E402
from pkg.parity.parity_library_sync import state_token  # noqa: F401,E402


class Handler:
    def __init__(self):
        self.responses = []

    def authorized(self):
        return True

    def handle_unauthorized(self):
        self.responses.append((403, {"error": "unauthorized"}))

    def send_json(self, status, payload):
        self.responses.append((status, payload))

    @property
    def last(self):
        return self.responses[-1]


def healthy_game(game_id, **overrides):
    game = {
        "game_id": game_id,
        "name": f"Game {game_id}",
        "path": f"/games/{game_id}.exe",
        "cover": f"/media/{game_id}/cover.png",
        "platform": "PC",
        "genre": "Action",
        "year": "2020",
        "developer": "Dev",
        "description": "Desc",
    }
    game.update(overrides)
    return game


class FakeJobs:
    """JOB_MANAGER stand-in that runs scan workers synchronously."""

    def __init__(self):
        self.submitted = []

    def submit(self, name, worker, replace=False):
        self.submitted.append((name, worker, replace))
        worker(None)
        return {"job_id": "job-1"}

    def snapshots(self):
        return {}

    def history(self):
        return []


class HealthApiTests(unittest.TestCase):
    def setUp(self):
        self.state = {"games": [healthy_game("g1"), healthy_game("g2")], "settings": {}}
        self.jobs = FakeJobs()

    def _patch(self):
        def fake_transact(mutate):
            result = mutate(self.state)
            return self.state, result

        return (
            mock.patch.object(health.openbox, "load_state", return_value=self.state),
            mock.patch.object(health, "transact_state", side_effect=fake_transact),
            mock.patch.object(health, "JOB_MANAGER", self.jobs),
        )

    def _seed_cache(self):
        snapshot = engine.score_library(self.state["games"], self.state, probe=lambda p, *, file_only=False: True)
        engine.store_health_cache(self.state, snapshot)
        return snapshot

    # ── H2: cached snapshot, never scans on GET ──

    def test_snapshot_serves_cache_without_scanning(self):
        seeded = self._seed_cache()
        patches = self._patch()
        for patch in patches:
            patch.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        handler = Handler()
        with mock.patch.object(engine, "score_library") as scorer:
            health.health_snapshot(handler, SimpleNamespace(query=""))
            scorer.assert_not_called()
        status, payload = handler.last
        self.assertEqual(status, 200)
        self.assertTrue(payload["scanned"])
        self.assertEqual(payload["score"], seeded["score"])
        self.assertEqual(payload["recomputed"], False)

    def test_snapshot_without_cache_reports_unscanned(self):
        patches = self._patch()
        for patch in patches:
            patch.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        handler = Handler()
        with mock.patch.object(engine, "score_library") as scorer:
            health.health_snapshot(handler, SimpleNamespace(query=""))
            scorer.assert_not_called()
        status, payload = handler.last
        self.assertEqual(status, 200)
        self.assertFalse(payload["scanned"])
        self.assertIsNone(payload["score"])

    def test_scan_queues_job_and_updates_cache(self):
        patches = self._patch()
        for patch in patches:
            patch.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        handler = Handler()
        health.health_scan(handler, {})
        status, payload = handler.last
        self.assertEqual(status, 202)
        self.assertEqual(payload["job_id"], "job-1")
        self.assertEqual(self.jobs.submitted[0][0], health.SCAN_JOB_NAME)
        # worker ran synchronously: cache now holds a fresh score
        self.assertIsNotNone(engine.get_health_cache(self.state).get("score"))

    def test_issues_paginates(self):
        games = []
        for i in range(6):
            games.append(healthy_game(f"m{i}", genre="", year=""))  # 2 metadata issues each
        self.state["games"] = games
        self._seed_cache()
        patches = self._patch()
        for patch in patches:
            patch.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        handler = Handler()
        health.health_issues(handler, SimpleNamespace(query="dimension=metadata&limit=5&offset=0"))
        status, payload = handler.last
        self.assertEqual(status, 200)
        self.assertTrue(payload["scanned"])
        self.assertEqual(payload["total"], 12)
        self.assertEqual(len(payload["issues"]), 5)
        self.assertEqual(payload["dimension"], "metadata")
        handler = Handler()
        health.health_issues(handler, SimpleNamespace(query="dimension=metadata&limit=5&offset=10"))
        self.assertEqual(len(handler.last[1]["issues"]), 2)
        for issue in handler.last[1]["issues"]:
            self.assertIn("game_id", issue)
            self.assertIn("reason", issue)

    def test_issues_rejects_unknown_dimension(self):
        handler = Handler()
        with self.assertRaises(BadRequest):
            health.health_issues(handler, SimpleNamespace(query="dimension=bogus"))

    def test_issues_unscanned_returns_empty(self):
        patches = self._patch()
        for patch in patches:
            patch.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        handler = Handler()
        health.health_issues(handler, SimpleNamespace(query=""))
        status, payload = handler.last
        self.assertEqual(status, 200)
        self.assertFalse(payload["scanned"])
        self.assertEqual(payload["issues"], [])

    # ── H3: fix queue ──

    def _dup_state(self):
        self.state["games"] = [
            healthy_game("g1", name="Same", steam_app_id="10", path="/games/one.exe"),
            healthy_game("g2", name="Same", steam_app_id="10", path="/games/two.exe"),
            healthy_game("g3", name="Other"),
        ]
        return self.state

    def test_duplicate_fix_dry_run_then_execute_then_undo(self):
        self._dup_state()
        patches = self._patch()
        for patch in patches:
            patch.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        handler = Handler()
        health.health_fix(handler, {"dimension": "duplicates", "issue_ids": "all", "dry_run": True})
        status, preview = handler.last
        self.assertEqual(status, 200)
        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["plan"]["kind"], "merge")
        self.assertEqual(len(preview["plan"]["merges"]), 1)
        self.assertEqual(preview["plan"]["undo"], "trash")
        self.assertTrue(preview["base_token"])

        # execute without a preview token is refused
        handler = Handler()
        with self.assertRaises(BadRequest):
            health.health_fix(handler, {"dimension": "duplicates", "dry_run": False})

        # execute with a stale token is refused
        handler = Handler()
        with self.assertRaises(BadRequest):
            health.health_fix(handler, {"dimension": "duplicates", "dry_run": False, "base_token": "stale"})

        # execute with the fresh token merges; absorbed game lands in trash
        handler = Handler()
        health.health_fix(handler, {"dimension": "duplicates", "dry_run": False, "base_token": preview["base_token"]})
        status, result = handler.last
        self.assertEqual(status, 200)
        self.assertFalse(result["dry_run"])
        self.assertEqual(result["summary"]["merged"], 1)
        self.assertEqual(len(self.state["games"]), 2)
        journal = engine.get_fix_journal(self.state)
        self.assertEqual(len(journal), 1)
        self.assertEqual(journal[0]["fix_id"], result["fix_id"])
        self.assertEqual(journal[0]["inverse"]["kind"], "trash_restore")
        self.assertEqual(len(journal[0]["inverse"]["trash_ids"]), 1)

        # undo restores the absorbed game from trash
        handler = Handler()
        with mock.patch("handlers.library.LibraryHandlers._api_post_api_v2_library_trash_restore") as restore:
            def fake_restore(shim, payload):
                self.state["games"].append(self.state["trash"][0]["game"])
                shim.send_json(200, {"restored": True})

            restore.side_effect = fake_restore
            health.health_undo(handler, {"fix_id": result["fix_id"]})
        status, undone = handler.last
        self.assertEqual(status, 200)
        self.assertTrue(undone["undone"])
        self.assertEqual(len(self.state["games"]), 3)
        self.assertTrue(engine.get_fix_journal(self.state)[0]["undone"])

        # second undo is refused
        handler = Handler()
        with self.assertRaises(BadRequest):
            health.health_undo(handler, {"fix_id": result["fix_id"]})

    def test_fix_preview_token_rejects_library_change(self):
        self._dup_state()
        patches = self._patch()
        for patch in patches:
            patch.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        handler = Handler()
        health.health_fix(handler, {"dimension": "duplicates", "dry_run": True})
        token = handler.last[1]["base_token"]
        # library changes after the preview -> token no longer matches
        self.state["games"].append(healthy_game("g4"))
        handler = Handler()
        with self.assertRaises(BadRequest):
            health.health_fix(handler, {"dimension": "duplicates", "dry_run": False, "base_token": token})

    def test_metadata_fix_is_manual_only(self):
        self.state["games"] = [healthy_game("g1", genre="")]
        patches = self._patch()
        for patch in patches:
            patch.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        handler = Handler()
        health.health_fix(handler, {"dimension": "metadata", "dry_run": True})
        self.assertEqual(handler.last[1]["plan"]["kind"], "manual")
        self.assertEqual(handler.last[1]["plan"]["action"], "open_editor")
        # execute on a manual dimension returns guidance, mutates nothing
        handler = Handler()
        health.health_fix(
            handler,
            {"dimension": "metadata", "dry_run": False, "base_token": state_token(self.state)},
        )
        status, payload = handler.last
        self.assertEqual(status, 200)
        self.assertFalse(payload["executed"])
        self.assertEqual(engine.get_fix_journal(self.state), [])

    def test_fix_rejects_unknown_dimension(self):
        handler = Handler()
        with self.assertRaises(BadRequest):
            health.health_fix(handler, {"dimension": "bogus", "dry_run": True})


class HealthFixExtendedTests(unittest.TestCase):
    """Cover the artwork / file_integrity / manual fix paths and undo kinds."""

    def setUp(self):
        self.state = {"games": [healthy_game("g1"), healthy_game("g2")], "settings": {}}
        self.jobs = FakeJobs()

    def _patch(self):
        def fake_transact(mutate):
            result = mutate(self.state)
            return self.state, result

        patches = [
            mock.patch.object(health.openbox, "load_state", return_value=self.state),
            mock.patch.object(health, "transact_state", side_effect=fake_transact),
            mock.patch.object(health, "JOB_MANAGER", self.jobs),
        ]
        for patch in patches:
            patch.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

    def _fix(self, payload):
        handler = Handler()
        health.health_fix(handler, payload)
        return handler.last

    def test_snapshot_stale_cache_reports_unscanned(self):
        self._patch()
        engine.store_health_cache(self.state, engine.score_library(
            self.state["games"], self.state, probe=lambda p, *, file_only=False: True))
        engine.mark_dirty_ids(self.state, ["g1"])
        handler = Handler()
        health.health_snapshot(handler, SimpleNamespace(query=""))
        status, payload = handler.last
        self.assertEqual(status, 200)
        self.assertFalse(payload["scanned"])
        self.assertIsNone(payload["score"])

    def test_run_health_scan_worker_updates_cache(self):
        self._patch()
        result = health.run_health_scan()
        self.assertEqual(result["game_count"], 2)
        cache = engine.get_health_cache(self.state)
        self.assertEqual(cache["score"], result["score"])
        self.assertEqual(self.state["settings"]["last_health_rescan"], cache["computed_at"])

    def test_issues_rejects_bad_limit(self):
        self._patch()
        handler = Handler()
        with self.assertRaises(BadRequest):
            health.health_issues(handler, SimpleNamespace(query="limit=abc"))
        # limit=0 clamps to 1
        handler = Handler()
        health.health_issues(handler, SimpleNamespace(query="limit=0"))
        self.assertEqual(handler.last[1]["limit"], 1)

    def test_select_issues_by_id_list(self):
        deductions = [
            {"dimension": "metadata", "game_id": "g1", "index": 0},
            {"dimension": "metadata", "game_id": "g2", "index": 1},
        ]
        selected = health._select_issues(deductions, "metadata", ["g2"])
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["game_id"], "g2")
        selected = health._select_issues(deductions, "metadata", ["1"])
        self.assertEqual(len(selected), 1)

    def test_artwork_fix_dry_run_execute_undo(self):
        from pkg.parity import parity_artwork_hygiene
        self.state["games"] = [healthy_game("g1", cover="")]  # missing_cover deduction
        self._patch()
        targets = [{"game_id": "g1", "field": "cover"}]
        with mock.patch.object(parity_artwork_hygiene, "build_report", return_value={"issues": []}), mock.patch.object(
            parity_artwork_hygiene, "select_fixable", return_value=targets
        ):
            status, preview = self._fix({"dimension": "artwork", "issue_ids": "all", "dry_run": True})
        self.assertEqual(status, 200)
        self.assertEqual(preview["plan"]["kind"], "artwork_doctor")
        self.assertEqual(preview["plan"]["targets"], targets)

        import handlers.steamgrid as steamgrid_mod
        def fake_fix(shim, payload):
            self.assertEqual(payload["game_ids"], ["g1"])
            shim.send_json(202, {"job_id": "art-job"})

        with mock.patch.object(steamgrid_mod, "steamgrid_hygiene_fix", side_effect=fake_fix):
            status, result = self._fix(
                {"dimension": "artwork", "dry_run": False, "base_token": preview["base_token"]}
            )
        self.assertEqual(status, 200)
        self.assertEqual(result["summary"]["job_id"], "art-job")
        fix_id = result["fix_id"]

        # undo: job result carries the undo batch id
        self.jobs.snapshots = lambda: {"s": {"job_id": "art-job", "result": {"batch_id": "b1"}}}

        def fake_undo(shim, payload):
            self.assertEqual(payload["batch_id"], "b1")
            shim.send_json(200, {"restored": 3})

        with mock.patch.object(steamgrid_mod, "steamgrid_hygiene_undo", side_effect=fake_undo):
            handler = Handler()
            health.health_undo(handler, {"fix_id": fix_id})
        status, undone = handler.last
        self.assertEqual(status, 200)
        self.assertTrue(undone["undone"])
        self.assertEqual(undone["restored"], 3)

    def test_artwork_undo_without_batch_reports_action(self):
        from pkg.parity import parity_artwork_hygiene
        self.state["games"] = [healthy_game("g1", cover="")]
        self._patch()
        with mock.patch.object(parity_artwork_hygiene, "build_report", return_value={"issues": []}), mock.patch.object(
            parity_artwork_hygiene, "select_fixable", return_value=[{"game_id": "g1", "field": "cover"}]
        ):
            _, preview = self._fix({"dimension": "artwork", "dry_run": True})
        import handlers.steamgrid as steamgrid_mod
        with mock.patch.object(
            steamgrid_mod, "steamgrid_hygiene_fix",
            side_effect=lambda shim, payload: shim.send_json(202, {"job_id": "art-job-2"}),
        ):
            _, result = self._fix({"dimension": "artwork", "dry_run": False, "base_token": preview["base_token"]})
        handler = Handler()
        health.health_undo(handler, {"fix_id": result["fix_id"]})
        status, payload = handler.last
        self.assertEqual(status, 200)
        self.assertFalse(payload["undone"])
        self.assertEqual(payload["action"], "artwork_doctor")

    def test_file_integrity_fix_needs_folder_then_relink_and_undo(self):
        from pkg.parity import parity_repair
        from pathlib import Path
        self.state["games"] = [healthy_game("g1", path="/old/one.exe")]
        self._patch()
        # no folder -> repair wizard guidance
        _, preview = self._fix({"dimension": "file_integrity", "dry_run": True})
        self.assertEqual(preview["plan"]["kind"], "repair_wizard")
        self.assertTrue(preview["plan"]["needs_folder"])

        match = {"id": 0, "game_id": "g1", "field": "path", "from": "/old/one.exe", "path": "/new/one.exe"}
        with mock.patch.object(parity_repair, "resolve_folder", return_value=Path("/new")), mock.patch.object(
            parity_repair, "scan_candidates", return_value=[{"path": "/new/one.exe"}]
        ), mock.patch.object(
            parity_repair, "scan_missing_paths",
            return_value={"items": [{"id": 0, "game_id": "g1", "field": "path", "path": "/old/one.exe"}]},
        ), mock.patch.object(
            parity_repair, "plan_repair",
            return_value={"matches": [match], "ambiguous": [], "unmatched": []},
        ):
            _, preview = self._fix({"dimension": "file_integrity", "dry_run": True, "folder": "/new"})
        self.assertEqual(preview["plan"]["kind"], "relink")
        self.assertEqual(len(preview["plan"]["matches"]), 1)

        with mock.patch.object(parity_repair, "resolve_folder", return_value=Path("/new")), mock.patch.object(
            parity_repair, "scan_candidates", return_value=[{"path": "/new/one.exe"}]
        ), mock.patch.object(
            parity_repair, "scan_missing_paths",
            return_value={"items": [{"id": 0, "game_id": "g1", "field": "path", "path": "/old/one.exe"}]},
        ), mock.patch.object(
            parity_repair, "plan_repair",
            return_value={"matches": [match], "ambiguous": [], "unmatched": []},
        ), mock.patch.object(
            parity_repair, "apply_repair",
            return_value={"updated": 1, "applied": [{"id": 0, "field": "path"}], "skipped": []},
        ):
            # execute without folder is refused even with a token
            handler = Handler()
            with self.assertRaises(BadRequest):
                health.health_fix(handler, {"dimension": "file_integrity", "dry_run": False,
                                            "base_token": preview["base_token"]})
            _, result = self._fix({"dimension": "file_integrity", "dry_run": False,
                                   "base_token": preview["base_token"], "folder": "/new"})
        self.assertEqual(result["summary"]["updated"], 1)
        journal = engine.get_fix_journal(self.state)
        self.assertEqual(journal[0]["inverse"]["kind"], "field_restore")

        # simulate the relink having applied, then undo restores the old path
        self.state["games"][0]["path"] = "/new/one.exe"
        handler = Handler()
        health.health_undo(handler, {"fix_id": result["fix_id"]})
        self.assertTrue(handler.last[1]["undone"])
        self.assertEqual(handler.last[1]["restored"], 1)
        self.assertEqual(self.state["games"][0]["path"], "/old/one.exe")

    def test_launch_readiness_fix_is_manual(self):
        self.state["games"] = [healthy_game("g1", path="/games/x.nes", platform="NES")]
        self._patch()
        _, preview = self._fix({"dimension": "launch_readiness", "dry_run": True})
        self.assertEqual(preview["plan"]["kind"], "manual")
        self.assertEqual(preview["plan"]["action"], "open_emulator_profiles")
        self.assertEqual(preview["plan"]["platforms"], ["NES"])

    def test_hygiene_batch_for_job_searches_history(self):
        self._patch()
        self.jobs.history = lambda: [{"job_id": "j9", "result": {"batch_id": "hb"}}]
        self.assertEqual(health._hygiene_batch_for_job("j9"), "hb")
        self.assertIsNone(health._hygiene_batch_for_job("missing"))
        self.assertIsNone(health._hygiene_batch_for_job(""))

    def test_undo_rejects_unknown_fix(self):
        self._patch()
        handler = Handler()
        with self.assertRaises(BadRequest):
            health.health_undo(handler, {"fix_id": "nope"})
        with self.assertRaises(BadRequest):
            health.health_undo(handler, {})


if __name__ == "__main__":
    unittest.main()
