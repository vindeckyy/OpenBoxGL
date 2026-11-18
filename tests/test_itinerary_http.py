"""HTTP handler tests for Backlog Itinerary routes, clamps, error codes, and async IGDB job."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api_errors import GameNotFound, ItineraryInvalidStrategy, ItineraryNoGames  # noqa: E402
from handlers import itinerary as itin_handler  # noqa: E402
import job_manager  # noqa: E402


class MockHandler:
    def __init__(self):
        self.responses = []

    def send_json(self, status, payload):
        self.responses.append((status, payload))


class ItineraryHttpTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.data_file = self.tmp / "data" / "state.json"
        self.data_file.parent.mkdir(parents=True)
        self.state = {
            "games": [
                {"id": 0, "game_id": "g-1", "name": "Celeste", "platform": "PC"},
            ],
            "trash": [],
            "settings": {},
            "itinerary": None,
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

    def test_all_five_routes_error_codes_and_async_igdb(self):
        with mock.patch.object(itin_handler, "DATA", self.data_file), mock.patch.object(
            itin_handler, "load_state_view", return_value=self.state
        ), mock.patch.object(itin_handler, "transact_state", side_effect=self._transact), mock.patch.object(
            itin_handler, "JOB_MANAGER", self.jm
        ):
            # GET initially empty
            h_get0 = MockHandler()
            itin_handler.itinerary_get(h_get0, SimpleNamespace(query=""))
            self.assertIsNone(h_get0.responses[0][1]["plan"])

            # Error codes
            with self.assertRaises(ItineraryInvalidStrategy) as e_str:
                itin_handler.itinerary_plan(MockHandler(), {"strategy": "bad"})
            self.assertEqual(e_str.exception.code, "ITINERARY_INVALID_STRATEGY")

            with self.assertRaises(ItineraryNoGames) as e_empty:
                itin_handler.itinerary_plan(MockHandler(), {"platforms": ["Atari"]})
            self.assertEqual(e_empty.exception.code, "ITINERARY_NO_GAMES")

            with self.assertRaises(GameNotFound):
                itin_handler.itinerary_estimate(MockHandler(), SimpleNamespace(query="game_id=missing"))

            # Estimate route
            h_est = MockHandler()
            itin_handler.itinerary_estimate(h_est, SimpleNamespace(query="game_id=g-1"))
            self.assertEqual(h_est.responses[0][1]["estimate"]["est_source"], "default")

            # Sync plan (clamps out-of-range horizon & pace)
            h_sync = MockHandler()
            itin_handler.itinerary_plan(h_sync, {"horizon_days": 9999, "minutes_per_day": 1, "strategy": "quick-wins"})
            self.assertEqual(h_sync.responses[0][0], 200)
            self.assertEqual(h_sync.responses[0][1]["plan"]["horizon_days"], 365)
            self.assertEqual(h_sync.responses[0][1]["plan"]["minutes_per_day"], 5)

            # Replan & Abandon
            h_rep = MockHandler()
            itin_handler.itinerary_replan(h_rep, {"strategy": "comeback"})
            self.assertEqual(h_rep.responses[0][1]["plan"]["strategy"], "comeback")

            h_ab = MockHandler()
            itin_handler.itinerary_abandon(h_ab, {})
            self.assertTrue(h_ab.responses[0][1]["abandoned"])
            self.assertIsNone(self.state["itinerary"])

            # Async IGDB job when fetch_igdb=True & credentials exist
            with mock.patch("pkg.parity.parity_igdb.credentials", return_value=("cid", "csec")), mock.patch(
                "pkg.parity.parity_igdb.search_games", return_value=[{"igdb_id": 123}]
            ), mock.patch("pkg.parity.parity_igdb._fetch_time_to_beat", return_value=11.5):
                h_async = MockHandler()
                itin_handler.itinerary_plan(h_async, {"fetch_igdb": True, "horizon_days": 30, "minutes_per_day": 60})
                self.assertEqual(h_async.responses[0][0], 202)
                done = self._wait_job("itinerary-plan")
                self.assertEqual(done["state"], "done")
                self.assertEqual(self.state["itinerary"]["entries"][0]["est_hours"], 11.5)
                self.assertEqual(self.state["itinerary"]["entries"][0]["est_source"], "igdb")


if __name__ == "__main__":
    unittest.main()
