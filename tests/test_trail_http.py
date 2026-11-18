"""HTTP handler tests for Achievement Trail routes, offline reads, and refresh job."""

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

from api_errors import RaDisabled, RaNotLinked, TrailGameUnknown  # noqa: E402
from handlers import trail as trail_handler  # noqa: E402
import job_manager  # noqa: E402


class MockHandler:
    def __init__(self):
        self.responses = []

    def send_json(self, status, payload):
        self.responses.append((status, payload))


class TrailHttpTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.data_file = self.tmp / "data" / "state.json"
        self.data_dir = self.data_file.parent
        self.data_dir.mkdir(parents=True)
        self.creds_path = self.data_dir / "retroachievements.json"
        self.state = {
            "games": [
                {"game_id": "g-ra", "name": "Mega Man 2", "platform": "NES", "ra_game_id": "77"},
                {"game_id": "g-plain", "name": "Unlinked", "platform": "NES"},
            ],
            "settings": {"retroachievements_enabled": True},
            "trash": [],
        }
        self.jm = job_manager.JobManager(max_workers=1)

    def tearDown(self):
        self.jm.shutdown(wait=True)
        self._tmp.cleanup()

    def _wait_job(self, name: str, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snap = self.jm.snapshot(name)
            if snap.get("state") in {"done", "error", "cancelled"}:
                return snap
            time.sleep(0.01)
        self.fail(f"Timed out waiting for job {name}")

    def test_disabled_reads_write_no_cache_and_refresh_returns_409(self):
        fake_sig = mock.Mock()
        fake_sig.signature.return_value = "sig-0"
        with mock.patch.object(trail_handler, "DATA", self.data_file), mock.patch.object(
            trail_handler, "STATE_STORE", fake_sig
        ), mock.patch.object(trail_handler, "load_state_view", return_value=self.state):
            h_list = MockHandler()
            trail_handler.trail_list(h_list, SimpleNamespace(query=""))
            self.assertFalse(h_list.responses[0][1]["available"])
            self.assertFalse((self.data_dir / "cache" / "trail.json").exists())

            h_game = MockHandler()
            trail_handler.trail_game(h_game, SimpleNamespace(query="game_id=g-ra"))
            self.assertFalse(h_game.responses[0][1]["available"])

            with self.assertRaises(RaDisabled) as err:
                trail_handler.trail_refresh(MockHandler(), {})
            self.assertEqual(err.exception.code, "RA_DISABLED")

    def test_refresh_job_writes_dates_and_routes_query_offline(self):
        self.creds_path.write_text(json.dumps({"username": "u", "api_key": "k"}), encoding="utf-8")
        fake_sig = mock.Mock()
        fake_sig.signature.return_value = "sig-1"

        ra_payload = {
            "Title": "Mega Man 2",
            "NumAwardedToUser": 1,
            "NumAwardedToUserHardcore": 1,
            "NumAchievements": 2,
            "Achievements": {
                "1": {
                    "ID": 1,
                    "Title": "Metal Man",
                    "Description": "Clear",
                    "Points": 10,
                    "BadgeName": "B1",
                    "DateEarned": "2026-11-01 10:00:00",
                    "DateEarnedHardcore": "2026-11-01 10:00:00",
                },
                "2": {"ID": 2, "Title": "Wily", "Description": "End", "Points": 25, "BadgeName": "B2"},
            },
        }
        with mock.patch.object(trail_handler, "DATA", self.data_file), mock.patch.object(
            trail_handler, "STATE_STORE", fake_sig
        ), mock.patch.object(trail_handler, "load_state_view", return_value=self.state), mock.patch.object(
            trail_handler, "JOB_MANAGER", self.jm
        ), mock.patch("retroachievements.api_get", return_value=ra_payload):
            h_ref = MockHandler()
            trail_handler.trail_refresh(h_ref, {"game_ids": ["g-ra"]})
            self.assertEqual(h_ref.responses[0][0], 202)
            done = self._wait_job("trail-refresh")
            self.assertEqual(done["state"], "done")
            self.assertEqual(done["refreshed"], 1)

            h_list = MockHandler()
            trail_handler.trail_list(h_list, SimpleNamespace(query="sort=points"))
            self.assertTrue(h_list.responses[0][1]["available"])
            self.assertEqual(h_list.responses[0][1]["items"][0]["earned_points"], 10)

            h_game = MockHandler()
            trail_handler.trail_game(h_game, SimpleNamespace(query="game_id=g-ra"))
            self.assertTrue(h_game.responses[0][1]["game"]["has_dates"])

            with self.assertRaises(TrailGameUnknown) as err_unk:
                trail_handler.trail_game(MockHandler(), SimpleNamespace(query="game_id=missing"))
            self.assertEqual(err_unk.exception.code, "TRAIL_GAME_UNKNOWN")

            with self.assertRaises(RaNotLinked) as err_link:
                trail_handler.trail_game(MockHandler(), SimpleNamespace(query="game_id=g-plain"))
            self.assertEqual(err_link.exception.code, "RA_NOT_LINKED")


if __name__ == "__main__":
    unittest.main()
