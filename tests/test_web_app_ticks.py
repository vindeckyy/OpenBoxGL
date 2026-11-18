"""Integration tests for 2.2 background ticks, launch close hook, and warn-and-repair."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import job_manager  # noqa: E402
from pkg.state import launch as launch_mod  # noqa: E402
import state_store  # noqa: E402
import web_app  # noqa: E402
import webapp_state  # noqa: E402


class WebAppTicksAndHooksTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.data_file = self.tmp / "data" / "library.json"
        self.data_file.parent.mkdir(parents=True)
        self.save_dir = self.tmp / "saves"
        self.save_dir.mkdir(parents=True)
        (self.save_dir / "slot1.sav").write_bytes(b"SAVE-CLOSE-TEST")

    def tearDown(self):
        self._tmp.cleanup()

    def test_hourly_ticks_queue_jobs_and_unlock_due_capsules(self):
        jm = mock.Mock()
        state = {
            "games": [{"game_id": "g-1", "name": "Test", "save_paths": [str(self.save_dir)]}],
            "settings": {
                "save_management_enabled": True,
                "save_vault_enabled": True,
                "save_vault_auto_days": 7,
                "deals_enabled": True,
                "deals_poll_hours": 24,
            },
            "time_capsules": [
                {
                    "id": "cap-due",
                    "title": "Due Note",
                    "body": "Hello",
                    "kind": "note",
                    "unlock_at": date.today().isoformat(),
                    "created_at": "2026-01-01T00:00:00Z",
                    "opened_at": None,
                }
            ],
            "deals": {
                "watchlist": [{"id": "d-1", "appid": "367520", "manual": False, "name": "HK"}],
                "prices": {"d-1": []},
                "last_poll_at": "",
            },
            "notifications": [],
        }

        def _update(fn):
            fn(state)

        with mock.patch.object(web_app, "DATA", self.data_file), mock.patch.object(
            web_app, "RUNNING", {}
        ), mock.patch.object(web_app, "load_state", return_value=state), mock.patch.object(
            web_app, "update_state", side_effect=_update
        ), mock.patch.object(web_app, "JOB_MANAGER", jm):
            web_app._save_vault_auto_tick()
            web_app._deals_auto_tick()
            web_app._capsule_unlock_tick()
            # Trigger job lambdas safely without touching real network
            for call in jm.submit.call_args_list:
                job_name, worker = call.args[0], call.args[1]
                self.assertIn(job_name, ("save-vault-auto", "deals-refresh"))
                if job_name == "save-vault-auto":
                    res = worker(mock.Mock(is_set=lambda: False))
                    self.assertEqual(res["snapshotted"], 1)

        self.assertIsNotNone(state["time_capsules"][0]["opened_at"])
        self.assertEqual(len(state["notifications"]), 1)

        # Disabled / running guards short-circuit cleanly
        with mock.patch.object(web_app, "RUNNING", {"g-1": {}}), mock.patch.object(web_app, "JOB_MANAGER", jm):
            jm.submit.reset_mock()
            web_app._save_vault_auto_tick()
            web_app._deals_auto_tick()
            jm.submit.assert_not_called()

        # Exception isolation branches
        with mock.patch.object(web_app, "RUNNING", {}), mock.patch.object(
            web_app, "load_state", side_effect=RuntimeError("boom")
        ):
            web_app._save_vault_auto_tick()
            web_app._capsule_unlock_tick()
            web_app._deals_auto_tick()

    def test_finish_session_runs_save_vault_on_close_hook(self):
        jm = job_manager.JobManager(max_workers=1)
        state = {
            "games": [{"id": 0, "game_id": "g-close", "name": "CloseGame", "save_paths": [str(self.save_dir)]}],
            "history": [],
            "settings": {
                "save_management_enabled": True,
                "save_vault_enabled": True,
                "save_vault_on_close": True,
                "save_vault_keep": 3,
            },
        }
        proc = mock.Mock()
        try:
            with mock.patch.object(webapp_state, "DATA", self.data_file), mock.patch.object(
                webapp_state, "load_state", return_value=state
            ), mock.patch.object(webapp_state, "update_state", side_effect=lambda fn: fn(state)), mock.patch.object(
                webapp_state, "wait_for_exit", return_value=0
            ), mock.patch.object(webapp_state, "JOB_MANAGER", jm), mock.patch(
                "pkg.state.sse._publish_session_event"
            ):
                launch_mod.finish_session("l-1", 0, datetime.now(), proc, None)
                jm.shutdown(wait=True)
            # Also exercise fallback direct path when JOB_MANAGER is None
            with mock.patch.object(webapp_state, "DATA", self.data_file), mock.patch.object(
                webapp_state, "load_state", return_value=state
            ), mock.patch.object(webapp_state, "update_state", side_effect=lambda fn: fn(state)), mock.patch.object(
                webapp_state, "wait_for_exit", return_value=0
            ), mock.patch.object(webapp_state, "JOB_MANAGER", None), mock.patch(
                "pkg.state.sse._publish_session_event"
            ):
                launch_mod.finish_session("l-2", 0, datetime.now(), proc, None)
        finally:
            jm.shutdown(wait=True)

        vault_dir = self.data_file.parent / "save-vault"
        self.assertTrue(vault_dir.is_dir())

    def test_state_store_warn_and_repair_all_22_additive_fields(self):
        # Malformed non-container types
        st1 = {"deals": "bad", "time_capsules": "bad", "itinerary": "bad"}
        self.assertTrue(state_store._normalize_22_additive_fields(st1))
        self.assertEqual(st1["deals"], {"watchlist": [], "prices": {}, "last_poll_at": ""})
        self.assertEqual(st1["time_capsules"], [])
        self.assertIsNone(st1["itinerary"])

        # Malformed sub-structures: non-list watchlist, non-dict prices, >50 points, >4000 body, >200 cap
        big_watch = [{"id": f"d-{i}", "note": "n" * 300} for i in range(205)]
        big_watch.insert(0, {"id": ""})  # invalid id
        big_watch.append({"id": "d-0"})  # duplicate
        st2 = {
            "deals": {
                "watchlist": big_watch,
                "prices": {"d-0": [{"price_cents": i} for i in range(55)], "orphan": [{"price_cents": 1}], "bad-pts": "nope"},
            },
            "time_capsules": ["bad", {"id": "c1", "unlock_at": "2030-01-01T00:00:00Z", "body": "ok"}],
            "itinerary": {"entries": "not-list"},
        }
        self.assertTrue(state_store._normalize_22_additive_fields(st2))
        self.assertEqual(len(st2["deals"]["watchlist"]), 200)
        self.assertEqual(len(st2["deals"]["watchlist"][0]["note"]), 280)
        self.assertEqual(len(st2["deals"]["prices"]["d-0"]), 50)
        self.assertNotIn("orphan", st2["deals"]["prices"])
        self.assertEqual(len(st2["time_capsules"]), 1)
        self.assertEqual(st2["time_capsules"][0]["id"], "c1")
        self.assertIsNone(st2["itinerary"])

        # Also watchlist not a list & prices not a dict branch
        st3 = {"deals": {"watchlist": "nope", "prices": "nope"}}
        self.assertTrue(state_store._normalize_22_additive_fields(st3))
        self.assertEqual(st3["deals"]["watchlist"], [])
        self.assertEqual(st3["deals"]["prices"], {})


if __name__ == "__main__":
    unittest.main()
