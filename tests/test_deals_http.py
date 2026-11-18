"""HTTP handler tests for Deal Radar routes and problem codes."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api_errors import BadRequest, DealsDisabled, DealsInvalidAppId, DealsLimitExceeded, DealsNotFound  # noqa: E402
from handlers import deals as deals_handler  # noqa: E402
import job_manager  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "deals"


class MockHandler:
    def __init__(self):
        self.responses = []

    def send_json(self, status, payload):
        self.responses.append((status, payload))


class DealsHttpTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.data_file = self.tmp / "data" / "state.json"
        self.data_file.parent.mkdir(parents=True)
        self.state = {
            "games": [{"game_id": "g-hk", "name": "Hollow Knight", "steam_app_id": "367520"}],
            "deals": {"watchlist": [], "prices": {}, "last_poll_at": ""},
            "notifications": [],
            "settings": {"deals_enabled": False, "deals_poll_hours": 168, "deals_min_drop_pct": 10, "deals_store_cc": "us"},
        }
        self.jm = job_manager.JobManager(max_workers=1)

    def tearDown(self):
        self.jm.shutdown(wait=True)
        self._tmp.cleanup()

    def _transact(self, fn):
        return self.state, fn(self.state)

    def _wait_job(self, name: str, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snap = self.jm.snapshot(name)
            if snap.get("state") in {"done", "error", "cancelled"}:
                return snap
            time.sleep(0.01)
        self.fail(f"Timed out waiting for job {name}")

    def test_disabled_blocks_all_mutations_and_fetches(self):
        with mock.patch.object(deals_handler, "load_state_readonly", return_value=self.state):
            h_list = MockHandler()
            deals_handler.deals_list(h_list, SimpleNamespace(query=""))
            self.assertFalse(h_list.responses[0][1]["available"])

            with self.assertRaises(DealsDisabled) as e1:
                deals_handler.deals_search(MockHandler(), SimpleNamespace(query="q=hollow"))
            self.assertEqual(e1.exception.code, "DEALS_DISABLED")

            with self.assertRaises(DealsDisabled):
                deals_handler.deals_watch(MockHandler(), {"appid": "367520"})
            with self.assertRaises(DealsDisabled):
                deals_handler.deals_unwatch(MockHandler(), {"id": "deal-x"})
            with self.assertRaises(DealsDisabled):
                deals_handler.deals_entry(MockHandler(), {"id": "deal-x"})
            with self.assertRaises(DealsDisabled):
                deals_handler.deals_refresh(MockHandler(), {})

    def test_enabled_full_lifecycle_and_error_codes(self):
        self.state["settings"]["deals_enabled"] = True
        search_json = json.loads((FIXTURES / "storesearch.json").read_text(encoding="utf-8"))
        details_json = json.loads((FIXTURES / "appdetails_sale.json").read_text(encoding="utf-8"))

        with mock.patch.object(deals_handler, "DATA", self.data_file), mock.patch.object(
            deals_handler, "load_state_readonly", return_value=self.state
        ), mock.patch.object(deals_handler, "transact_state", side_effect=self._transact), mock.patch.object(
            deals_handler, "JOB_MANAGER", self.jm
        ):
            with mock.patch("pkg.parity.parity_deals._steam_get", return_value=search_json):
                h_s = MockHandler()
                deals_handler.deals_search(h_s, SimpleNamespace(query="q=hollow"))
                self.assertEqual(len(h_s.responses[0][1]["results"]), 2)

            with self.assertRaises(DealsInvalidAppId):
                deals_handler.deals_watch(MockHandler(), {"appid": "bad-id"})

            h_w = MockHandler()
            deals_handler.deals_watch(h_w, {"appid": "367520", "name": "Hollow Knight"})
            self.assertEqual(h_w.responses[0][0], 201)
            did = h_w.responses[0][1]["entry"]["id"]

            h_e = MockHandler()
            deals_handler.deals_entry(h_e, {"id": did, "target_price_cents": 900, "note": "Wait for sale"})
            self.assertEqual(h_e.responses[0][1]["entry"]["target_price_cents"], 900)

            with mock.patch("pkg.parity.parity_deals._steam_get", return_value=details_json):
                h_r = MockHandler()
                deals_handler.deals_refresh(h_r, {})
                self.assertEqual(h_r.responses[0][0], 202)
                job = self._wait_job("deals-refresh")
                self.assertEqual(job["state"], "done")
                self.assertEqual(job["polled"], 1)

            h_u = MockHandler()
            deals_handler.deals_unwatch(h_u, {"id": did})
            self.assertTrue(h_u.responses[0][1]["deleted"])

            with self.assertRaises(DealsNotFound):
                deals_handler.deals_unwatch(MockHandler(), {"id": did})

            with mock.patch("pkg.parity.parity_deals.MAX_WATCHLIST", 0):
                with self.assertRaises(DealsLimitExceeded) as e_lim:
                    deals_handler.deals_watch(MockHandler(), {"appid": "367520", "name": "HK"})
                self.assertEqual(e_lim.exception.code, "DEALS_LIMIT")

            with self.assertRaises(BadRequest):
                deals_handler.deals_unwatch(MockHandler(), "bad")  # type: ignore[arg-type]
            with self.assertRaises(BadRequest):
                deals_handler.deals_unwatch(MockHandler(), {})


if __name__ == "__main__":
    unittest.main()
