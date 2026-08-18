import os
import sys
import time
import unittest
from datetime import datetime
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webapp_state import (
    _read_proc_start_time,
    _read_proc_cmdline,
    _verify_process_identity,
    reconcile_sessions_on_startup,
    start_game,
    finish_session
)
from state_store import default_state, normalize_state
from handlers.sessions import SessionHandlers
from pkg.parity.parity_tracking import wait_for_exit

class MockProcess:
    def __init__(self, pid=99999):
        self.pid = pid
        self._poll = None

    def poll(self):
        return self._poll

    def wait(self):
        return self._poll

class TestSessionPersistence(unittest.TestCase):
    def test_read_proc_start_time(self):
        start_time = _read_proc_start_time(os.getpid())
        self.assertIsNotNone(start_time)
        self.assertNotEqual(start_time, "")

    def test_read_proc_cmdline(self):
        cmdline = _read_proc_cmdline(os.getpid())
        self.assertIsNotNone(cmdline)
        self.assertTrue(len(cmdline) > 0)

    def test_verify_process_identity(self):
        pid = os.getpid()
        start_time = _read_proc_start_time(pid)
        cmdline = _read_proc_cmdline(pid)

        session = {
            "pid": pid,
            "proc_start_time": start_time,
            "command_fingerprint": cmdline
        }

        # Returns True for current process with correct fingerprint
        self.assertTrue(_verify_process_identity(session))

        # Returns False for non-existent PID
        bad_pid_session = session.copy()
        bad_pid_session["pid"] = 99999999
        self.assertFalse(_verify_process_identity(bad_pid_session))

        # Returns False for wrong start time
        bad_time_session = session.copy()
        bad_time_session["proc_start_time"] = "0"
        self.assertFalse(_verify_process_identity(bad_time_session))

        # Returns False for wrong command
        bad_cmd_session = session.copy()
        bad_cmd_session["command_fingerprint"] = "definitely_not_the_command"
        self.assertFalse(_verify_process_identity(bad_cmd_session))

    def test_schema_migration_v5_v6(self):
        v5_state = {
            "schema_version": 5,
            "games": [],
            "profiles": {},
            "history": [],
            "settings": {},
            "playlists": [],
            "queue": [],
            "notifications": [],
            "ui_state": {}
        }
        normalized, changed = normalize_state(v5_state)
        self.assertTrue(changed)
        self.assertEqual(normalized["schema_version"], 6)
        self.assertIn("active_sessions", normalized)
        self.assertEqual(normalized["active_sessions"], [])

    @patch('webapp_state._verify_process_identity')
    def test_reconcile_sessions_on_startup(self, mock_verify):
        mock_verify.side_effect = lambda s: s["pid"] == 1

        state = {
            "active_sessions": [
                {"pid": 1, "status": "active"},
                {"pid": 2, "status": "active"}
            ]
        }

        reattached, abandoned = reconcile_sessions_on_startup(state)
        
        self.assertEqual(len(reattached), 1)
        self.assertEqual(reattached[0]["pid"], 1)

        self.assertEqual(len(abandoned), 1)
        self.assertEqual(abandoned[0]["pid"], 2)
        self.assertEqual(abandoned[0]["status"], "abandoned")

        self.assertEqual(len(state["active_sessions"]), 2)
        
    @patch('webapp_state.subprocess.Popen')
    @patch('openbox.STATE_STORE')
    def test_session_record_added_on_launch_and_removed_on_exit(self, mock_store, mock_popen):
        state = default_state()
        state["games"].append({
            "game_id": "game-1234",
            "name": "Test Game",
            "path": "/bin/echo",
            "launch": "echo test"
        })
        mock_store.load.return_value = state
        mock_store.load_readonly.return_value = state
        
        def update_state_mock(mutator):
            mutator(state)
            return state
        mock_store.update.side_effect = update_state_mock
        mock_store.update_with_result.side_effect = lambda m: (update_state_mock(m), None)

        mock_process = MockProcess(pid=12345)
        mock_popen.return_value = mock_process

        with patch('webapp_state._read_proc_start_time', return_value="1000"), \
             patch('webapp_state._read_proc_cmdline', return_value="test_command"), \
             patch('os.getpgid', return_value=12345):
            
            entry = start_game(index=0)

            self.assertIn("active_sessions", state)
            self.assertEqual(len(state["active_sessions"]), 1)
            session = state["active_sessions"][0]
            self.assertEqual(session["pid"], 12345)
            self.assertEqual(session["status"], "active")
            
            # Now finish session
            class DummyLease:
                def restore(self): pass
            
            finish_session(entry["launch_id"], 0, datetime.now(), mock_process, DummyLease())
            
            # Record should be removed
            self.assertEqual(len(state["active_sessions"]), 0)

    @patch('handlers.sessions.load_state_view')
    def test_abandoned_sessions_in_running_response(self, mock_load_state_view):
        mock_load_state_view.return_value = {
            "active_sessions": [
                {"launch_id": "abc", "status": "abandoned"}
            ]
        }
        
        class MockReq:
            query = ""
            
        handler = SessionHandlers()
        handler.send_json = MagicMock()
        
        handler._api_get_api_running(MockReq())
        
        handler.send_json.assert_called_once()
        payload = handler.send_json.call_args[0][1]
        self.assertIn("abandoned", payload)
        self.assertEqual(len(payload["abandoned"]), 1)
        self.assertEqual(payload["abandoned"][0]["launch_id"], "abc")

    @patch('handlers.sessions.SessionHandlers')
    @patch('openbox.update_state')
    def test_cleanup_action_removes_abandoned(self, mock_update_state, MockHandler):
        def update_state_side_effect(mutator):
            state = {
                "active_sessions": [
                    {"launch_id": "abc", "status": "abandoned"},
                    {"launch_id": "def", "status": "abandoned"}
                ]
            }
            mutator(state)
            self.assertEqual(len(state["active_sessions"]), 1)
            self.assertEqual(state["active_sessions"][0]["launch_id"], "def")
            return state
            
        mock_update_state.side_effect = update_state_side_effect
        
        handler = SessionHandlers()
        handler.send_json = MagicMock()
        
        handler._api_post_api_session_cleanup({"launch_id": "abc"})
        
        handler.send_json.assert_called_once()

    @patch('pkg.parity.parity_tracking.find_pids_in_folder')
    @patch('pkg.parity.parity_tracking._alive')
    def test_bounded_tracking_timeout(self, mock_alive, mock_find_pids):
        # Setup process that has exited
        mock_process = MockProcess(pid=1)
        mock_process._poll = 0
        
        # But tracking condition is unresolved
        mock_find_pids.return_value = [2]
        mock_alive.return_value = True
        
        game = {"install_dir": "/fake"}
        settings = {"tracking_mode": "folder", "tracking_frequency": 0.01}
        
        # Will timeout after 0.05 seconds
        t0 = time.time()
        result = wait_for_exit(mock_process, game, settings, max_tracking_duration=0.05)
        t1 = time.time()
        
        self.assertEqual(result, "tracking_timeout")
        self.assertTrue(t1 - t0 >= 0.05)

if __name__ == "__main__":
    unittest.main()
