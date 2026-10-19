#!/usr/bin/env python3
"""Regression tests for wrapper tracking, reattachment identity, and dedupe."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import webapp_state  # noqa: E402
from api_errors import Conflict  # noqa: E402
from pkg.parity import parity_tracking  # noqa: E402
from pkg.state import launch  # noqa: E402


class _ExitedProcess:
    pid = 1

    def poll(self):
        return 0

    def wait(self):
        raise AssertionError("a wrapper exit must not end child tracking")


class WrapperTrackingTests(unittest.TestCase):
    def test_wrapper_exit_keeps_tracking_a_live_child(self):
        process = _ExitedProcess()
        with mock.patch.object(parity_tracking, "find_pids_in_folder", return_value=[2]), \
                mock.patch.object(parity_tracking, "_alive", side_effect=[True, False]):
            result = parity_tracking.wait_for_exit(
                process,
                {"install_dir": "/fake"},
                {"tracking_mode": "folder", "tracking_frequency": 0.01},
                max_tracking_duration=1,
            )
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.timed_out)


class ReattachmentTests(unittest.TestCase):
    def test_reattachment_rejects_a_reused_pid(self):
        pid = os.getpid()
        session = {
            "pid": pid,
            "proc_start_time": "original-start",
            "command_fingerprint": "game --flag",
        }
        with mock.patch.object(webapp_state, "_read_proc_start_time", return_value="new-start"), \
                mock.patch.object(webapp_state, "_read_proc_cmdline", return_value="game --flag"):
            self.assertFalse(launch._verify_process_identity(session))
        with mock.patch.object(webapp_state, "_read_proc_start_time", return_value="original-start"), \
                mock.patch.object(webapp_state, "_read_proc_cmdline", return_value="game --flag"):
            self.assertTrue(launch._verify_process_identity(session))

    def test_a_second_launch_is_refused_while_the_first_is_pending(self):
        saved = dict(launch.PENDING_LAUNCHES)
        launch.PENDING_LAUNCHES.clear()
        try:
            launch._reserve_launch("game-1", "launch-1", game_name="Game")
            with self.assertRaises(Conflict) as raised:
                launch._reserve_launch("game-1", "launch-2", game_name="Game")
            self.assertEqual(raised.exception.code, "LAUNCH_ALREADY_ACTIVE")
            self.assertEqual(raised.exception.detail["state"], "pending")
        finally:
            launch.PENDING_LAUNCHES.clear()
            launch.PENDING_LAUNCHES.update(saved)


if __name__ == "__main__":
    unittest.main()
