import sys
import os
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webapp_state
from pkg.parity.parity_perf import PerfLease, apply_perf_profile

class TestLaunchPhases(unittest.TestCase):
    def setUp(self):
        self.mock_apply_perf = patch('webapp_state.apply_perf_profile').start()
        self.mock_restore = MagicMock()
        self.mock_lease = PerfLease(applied=True, profile_name="test", restore=self.mock_restore)
        self.mock_apply_perf.return_value = self.mock_lease
        
        self.mock_popen = patch('subprocess.Popen').start()
        self.mock_process = MagicMock()
        self.mock_process.pid = 1234
        self.mock_process.poll.return_value = 0
        self.mock_popen.return_value = self.mock_process
        
        self.mock_load_state = patch('webapp_state.load_state').start()
        self.mock_load_state.return_value = {
            "games": [{"game_id": "1", "name": "Test Game", "command": "echo test"}],
            "profiles": {},
            "settings": {},
            "history": []
        }
        self.mock_resolve_start_game = patch('webapp_state._resolve_start_game').start()
        self.mock_resolve_start_game.return_value = ({"game_id": "1", "name": "Test Game", "command": "echo test"}, 0)
        
        self.mock_start_launch_command = patch('webapp_state._start_launch_command').start()
        self.mock_start_launch_command.return_value = (["echo", "test"], "/tmp")
        
        self.mock_apply_start_plugins = patch('webapp_state._apply_start_plugins').start()
        self.mock_apply_start_plugins.return_value = (["echo", "test"], "/tmp")
        
        self.mock_validate_start_command = patch('webapp_state._validate_start_command').start()
        
        self.mock_update_state = patch('webapp_state.update_state').start()
        
        self.mock_thread = patch('threading.Thread').start()

    def tearDown(self):
        patch.stopall()
        
    def test_plugin_rejection_restores_perf(self):
        self.mock_apply_start_plugins.side_effect = ValueError("Plugin rejected launch")
        with self.assertRaises(ValueError):
            webapp_state.start_game(index=0)
        self.mock_restore.assert_called_once()
        
    def test_invalid_working_directory_restores_perf(self):
        self.mock_validate_start_command.side_effect = OSError("Invalid working dir")
        with self.assertRaises(OSError):
            webapp_state.start_game(index=0)
        self.mock_restore.assert_called_once()
        
    def test_popen_failure_restores_perf(self):
        self.mock_popen.side_effect = FileNotFoundError("Command not found")
        with self.assertRaises(FileNotFoundError):
            webapp_state.start_game(index=0)
        self.mock_restore.assert_called_once()
        
    def test_state_commit_failure_restores_perf(self):
        self.mock_update_state.side_effect = RuntimeError("State commit failed")
        self.mock_killpg = patch('os.killpg').start()
        with self.assertRaises(RuntimeError):
            webapp_state.start_game(index=0)
        self.mock_restore.assert_called_once()
        self.mock_killpg.assert_called_once()
        
    def test_normal_exit_restores_perf(self):
        # We need to test the watcher thread finish_session
        import datetime
        started = datetime.datetime.now()
        
        mock_wait = patch('webapp_state.wait_for_exit').start()
        mock_wait.return_value = 0
        
        webapp_state.RUNNING["test_launch"] = {"game_name": "Test"}
        webapp_state.PROCESSES["test_launch"] = self.mock_process
        
        try:
            webapp_state.finish_session("test_launch", 0, started, self.mock_process, self.mock_lease)
            self.mock_restore.assert_called_once()
        finally:
            webapp_state.RUNNING.pop("test_launch", None)
            webapp_state.PROCESSES.pop("test_launch", None)
            
    def test_watcher_thread_exception_restores_perf(self):
        import datetime
        started = datetime.datetime.now()
        
        mock_wait = patch('webapp_state.wait_for_exit').start()
        mock_wait.side_effect = RuntimeError("Watcher failed")
        
        webapp_state.RUNNING["test_launch"] = {"game_name": "Test"}
        webapp_state.PROCESSES["test_launch"] = self.mock_process
        
        try:
            with self.assertRaises(RuntimeError):
                webapp_state.finish_session("test_launch", 0, started, self.mock_process, self.mock_lease)
            self.mock_restore.assert_called_once()
        finally:
            webapp_state.RUNNING.pop("test_launch", None)
            webapp_state.PROCESSES.pop("test_launch", None)
            
    def test_lease_restore_idempotency(self):
        pass
        
    def test_lease_restore_idempotent_impl(self):
        from pkg.parity.parity_perf import _make_idempotent_restore
        mock_target = patch('pkg.parity.parity_perf.restore_perf_profile').start()
        restore_func = _make_idempotent_restore("test", {})
        
        restore_func()
        restore_func()
        mock_target.assert_called_once()
        
    def test_perf_lease_applied_false_noop(self):
        mock_should = patch('pkg.parity.parity_perf.perf_should_apply').start()
        mock_should.return_value = False
        
        lease = apply_perf_profile("test", {"settings": {"apply_perf": "off"}})
        self.assertFalse(lease.applied)
        lease.restore()  # Should not error

if __name__ == "__main__":
    unittest.main()
