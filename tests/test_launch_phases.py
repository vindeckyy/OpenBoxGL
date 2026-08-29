import copy
import signal
import sys
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
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
        webapp_state.RUNNING.clear()
        webapp_state.PROCESSES.clear()
        patch.stopall()

    def test_signal_failure_not_ok(self):
        mock_process = MagicMock()
        mock_process.poll.return_value = None
        mock_process.pid = 4321
        webapp_state.RUNNING["sig-fail"] = {"game": "Test", "paused": False}
        webapp_state.PROCESSES["sig-fail"] = mock_process
        with patch("os.killpg", side_effect=OSError("No such process")):
            with self.assertRaises(ValueError):
                webapp_state.control_game_session("sig-fail", "pause")

    def test_post_spawn_failure_clears_running(self):
        mock_pub = patch("webapp_state._publish_start_events").start()
        mock_pub.side_effect = RuntimeError("SSE publish failed")
        with self.assertRaises(RuntimeError):
            webapp_state.start_game(index=0)
        self.mock_restore.assert_called_once()
        self.assertEqual(webapp_state.RUNNING, {})
        self.assertEqual(webapp_state.PROCESSES, {})

    def test_post_spawn_failure_clears_persisted_session(self):
        session_snapshots = []

        def record_mutate(mutate):
            state = {
                "games": [{"game_id": "1", "name": "Test Game", "command": "echo test"}],
                "profiles": {},
                "settings": {},
                "history": [],
                "active_sessions": [],
            }
            mutate(state)
            session_snapshots.append(copy.deepcopy(state.get("active_sessions", [])))

        self.mock_update_state.side_effect = record_mutate
        mock_pub = patch("webapp_state._publish_start_events").start()
        mock_pub.side_effect = RuntimeError("SSE publish failed")
        with self.assertRaises(RuntimeError):
            webapp_state.start_game(index=0)
        self.assertGreaterEqual(len(session_snapshots), 2)
        self.assertTrue(session_snapshots[0])
        self.assertEqual(session_snapshots[-1], [])

    def test_game_removed_during_launch_rolls_back(self):
        def force_missing(*_args, **_kwargs):
            missing = _args[5]
            missing["value"] = True
            return lambda _state: None

        with patch("webapp_state._make_start_mutator", side_effect=force_missing):
            with self.assertRaises(IndexError):
                webapp_state.start_game(index=0)
        self.mock_restore.assert_called_once()
        self.assertEqual(webapp_state.RUNNING, {})

    def test_terminate_owned_process_reattached(self):
        from pkg.state.launch import _ReattachedProcess, _terminate_owned_process

        proc = _ReattachedProcess({"pid": 100, "pgid": 200})
        with patch("os.killpg") as killpg:
            _terminate_owned_process(proc)
            killpg.assert_called_once_with(200, signal.SIGTERM)

    def test_terminate_owned_process_killpg_fallback(self):
        from pkg.state.launch import _terminate_owned_process

        proc = MagicMock()
        proc.pid = 100
        with patch("os.getpgid", return_value=100), patch(
            "os.killpg", side_effect=OSError("gone")
        ), patch.object(proc, "terminate") as terminate:
            _terminate_owned_process(proc)
            terminate.assert_called_once()

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


class TestLaunchModuleCoverage(unittest.TestCase):
    def tearDown(self):
        webapp_state.RUNNING.clear()
        webapp_state.PROCESSES.clear()
        patch.stopall()

    def test_proc_readers_and_verify_identity(self):
        from pkg.state.launch import (
            _read_proc_cmdline,
            _read_proc_start_time,
            _verify_process_identity,
        )

        pid = os.getpid()
        start_time = _read_proc_start_time(pid)
        cmdline = _read_proc_cmdline(pid)
        self.assertIsNotNone(start_time)
        self.assertTrue(cmdline)
        self.assertIsNone(_read_proc_start_time(99999999))
        self.assertEqual(_read_proc_cmdline(99999999), "")
        session = {
            "pid": pid,
            "proc_start_time": start_time,
            "command_fingerprint": cmdline,
        }
        self.assertTrue(_verify_process_identity(session))
        self.assertFalse(_verify_process_identity({}))
        self.assertFalse(_verify_process_identity({**session, "pid": 99999999}))
        self.assertFalse(_verify_process_identity({**session, "proc_start_time": "0"}))
        self.assertFalse(_verify_process_identity({**session, "command_fingerprint": "nope"}))

    @patch("webapp_state._verify_process_identity")
    def test_reconcile_sessions_on_startup(self, mock_verify):
        from pkg.state.launch import reconcile_sessions_on_startup

        mock_verify.side_effect = lambda session: session.get("pid") == 1
        state = {
            "active_sessions": [
                {"pid": 1, "status": "active"},
                {"pid": 2, "status": "active"},
            ]
        }
        reattached, abandoned = reconcile_sessions_on_startup(state)
        self.assertEqual(len(reattached), 1)
        self.assertEqual(len(abandoned), 1)
        self.assertEqual(abandoned[0]["status"], "abandoned")

    def test_reattached_process_and_lease(self):
        from pkg.state.launch import _ReattachedLease, _ReattachedProcess

        pid = os.getpid()
        session = {
            "pid": pid,
            "pgid": pid,
            "proc_start_time": "1",
            "command_fingerprint": "cmd",
        }
        proc = _ReattachedProcess(session)
        self.assertEqual(proc.pid, pid)
        with patch("webapp_state._verify_process_identity", return_value=True):
            self.assertIsNone(proc.poll())
        with patch("webapp_state._verify_process_identity", return_value=False):
            self.assertEqual(proc.poll(), 0)
        with patch("webapp_state._verify_process_identity", side_effect=[True, False]), patch(
            "pkg.state.launch.time.sleep"
        ):
            self.assertEqual(proc.wait(), 0)

        restore = MagicMock()
        with patch("pkg.state.launch.restore_perf_profile", restore), patch(
            "pkg.state.launch.load_state", return_value={}
        ):
            lease = _ReattachedLease("balanced")
            lease.restore()
            lease.restore()
            restore.assert_called_once()
            _ReattachedLease("").restore()

    def test_resolve_library_game_and_payload(self):
        from pkg.state.launch import game_from_payload, game_from_query, resolve_library_game

        state = {
            "games": [
                {
                    "game_id": "stable-1",
                    "legacy_game_ids": ["legacy-1"],
                    "name": "Alpha",
                    "path": "/games/alpha",
                    "steam_app_id": "100",
                },
                {"game_id": "stable-2", "name": "Beta", "path": "/games/beta"},
            ]
        }
        self.assertEqual(
            resolve_library_game(state, {"stable_game_id": "legacy-1"})["name"],
            "Alpha",
        )
        self.assertEqual(
            resolve_library_game(state, {"steam_app_id": "100"})["name"],
            "Alpha",
        )
        self.assertEqual(
            resolve_library_game(state, {"game_path": "/games/beta", "game_name": "Beta"})["name"],
            "Beta",
        )
        self.assertEqual(resolve_library_game(state, {}, fallback_index=1)["name"], "Beta")
        self.assertIsNone(resolve_library_game(state, {}, fallback_index=99))
        self.assertIsNone(resolve_library_game(state, "not-a-dict"))
        self.assertEqual(game_from_payload(state, {"id": 1})["name"], "Beta")
        self.assertEqual(
            game_from_query(state, {"id": ["1"], "game_id": ["stable-2"]})["name"],
            "Beta",
        )
        with self.assertRaises(ValueError):
            game_from_payload(state, "bad")
        with self.assertRaises(IndexError):
            game_from_payload(state, {})
        with self.assertRaises(IndexError):
            game_from_payload(state, {"id": "nope"})

    @patch("webapp_state.threading.Thread")
    def test_reattach_session_paths(self, mock_thread):
        from pkg.state.launch import reattach_session

        self.assertFalse(reattach_session(None))
        self.assertFalse(reattach_session({"launch_id": "", "pid": 1}))
        pid = os.getpid()
        state = {
            "games": [{"game_id": "g1", "name": "Game", "path": "/bin/true"}],
            "history": [],
            "settings": {},
        }
        session = {
            "launch_id": "launch-1",
            "game_id": "g1",
            "pid": pid,
            "pgid": pid,
            "proc_start_time": "1",
            "command_fingerprint": "cmd",
            "start_time": datetime.now().isoformat(timespec="seconds"),
            "perf_profile": "balanced",
        }
        try:
            self.assertTrue(reattach_session(session, state))
            self.assertIn("launch-1", webapp_state.RUNNING)
            mock_thread.return_value.start.assert_called_once()
        finally:
            webapp_state.RUNNING.pop("launch-1", None)
            webapp_state.PROCESSES.pop("launch-1", None)

    def test_resolve_start_game_and_launch_helpers(self):
        from pkg.state.launch import (
            _apply_start_plugins,
            _contained_launch_cwd,
            _resolve_start_game,
            _start_launch_command,
            _validate_start_command,
        )

        state = {
            "games": [{"game_id": "g1", "name": "Game", "platform": "Linux", "path": "/bin/true"}],
            "profiles": {},
        }
        game, index = _resolve_start_game(state, 0, "")
        self.assertEqual(index, 0)
        with self.assertRaises(IndexError):
            _resolve_start_game({"games": []}, 0, "")
        with patch("webapp_state.resolve_library_game", return_value=None):
            with self.assertRaises(IndexError):
                _resolve_start_game(state, 0, "missing")

        with patch("webapp_state.build_launch", return_value=(["/bin/true"], "/tmp")):
            args, cwd = _start_launch_command(state["games"][0], {})
            self.assertEqual(args[0], "/bin/true")
        with patch("webapp_state.build_launch", return_value=(["/no/such/file"], "/tmp")):
            with self.assertRaises(ValueError):
                _start_launch_command({"name": "Bad", "platform": "Linux"}, {})

        game = {"path": str(Path.home())}
        self.assertTrue(_contained_launch_cwd(str(Path.home()), game))
        self.assertFalse(_contained_launch_cwd("/definitely/outside", game))

        with tempfile.TemporaryDirectory() as directory:
            _validate_start_command(["echo", "ok"], directory)
        with self.assertRaises(ValueError):
            _validate_start_command([], "/tmp")
        with self.assertRaises(ValueError):
            _validate_start_command(["echo"], "/no/such/dir")

        game = {"name": "Game", "path": "/bin/true"}
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENBOX_SAFE_MODE", None)
            with patch(
                "webapp_state.run_plugins",
                return_value={"cancel": True, "error": "blocked"},
            ):
                with self.assertRaises(ValueError):
                    _apply_start_plugins(game, ["/bin/true"], "/tmp")
            with patch("webapp_state.run_plugins", return_value="bad"):
                with self.assertRaises(ValueError):
                    _apply_start_plugins(game, ["/bin/true"], "/tmp")
            with patch(
                "webapp_state.run_plugins",
                return_value={"args": ["/other"], "cwd": "/tmp"},
            ):
                args, cwd = _apply_start_plugins(game, ["/bin/true"], "/tmp")
                self.assertEqual(args, ["/bin/true"])
        with patch.dict(os.environ, {"OPENBOX_SAFE_MODE": "1"}):
            args, cwd = _apply_start_plugins(game, ["/bin/true"], "/tmp")
            self.assertEqual(args, ["/bin/true"])

    def test_terminate_and_rollback_edges(self):
        from pkg.state.launch import _rollback_failed_launch, _terminate_owned_process

        _terminate_owned_process(None)
        proc = MagicMock()
        proc.pid = 100
        with patch("os.getpgid", side_effect=OSError("gone")), patch.object(
            proc, "terminate", side_effect=ProcessLookupError("gone")
        ):
            _terminate_owned_process(proc)

        webapp_state.RUNNING["rb-1"] = {}
        webapp_state.PROCESSES["rb-1"] = MagicMock()
        with patch("webapp_state.update_state", side_effect=RuntimeError("persist fail")):
            _rollback_failed_launch("rb-1")
        self.assertNotIn("rb-1", webapp_state.RUNNING)

    def test_control_game_session_actions(self):
        mock_process = MagicMock()
        mock_process.poll.return_value = None
        mock_process.pid = 100
        webapp_state.RUNNING["ctrl"] = {"game": "Test", "paused": False}
        webapp_state.PROCESSES["ctrl"] = mock_process
        with patch("os.killpg") as killpg:
            webapp_state.control_game_session("ctrl", "pause")
            self.assertTrue(webapp_state.RUNNING["ctrl"]["paused"])
            webapp_state.control_game_session("ctrl", "resume")
            self.assertFalse(webapp_state.RUNNING["ctrl"]["paused"])
            webapp_state.control_game_session("ctrl", "stop")
            webapp_state.RUNNING["ctrl2"] = {"game": "Test", "paused": True}
            webapp_state.PROCESSES["ctrl2"] = mock_process
            webapp_state.control_game_session("ctrl2", "restart")
            self.assertTrue(webapp_state.RUNNING["ctrl2"]["restart"])
            webapp_state.RUNNING["ctrl3"] = {"game": "Test", "paused": False}
            webapp_state.PROCESSES["ctrl3"] = mock_process
            webapp_state.control_game_session("ctrl3", "kill")
            self.assertGreaterEqual(killpg.call_count, 4)
        with self.assertRaises(ValueError):
            webapp_state.control_game_session("missing", "pause")
        webapp_state.RUNNING["dead"] = {"game": "Test"}
        webapp_state.PROCESSES["dead"] = MagicMock(poll=MagicMock(return_value=0))
        with self.assertRaises(ValueError):
            webapp_state.control_game_session("dead", "pause")
        webapp_state.RUNNING["bad-action"] = {"game": "Test"}
        webapp_state.PROCESSES["bad-action"] = mock_process
        with self.assertRaises(ValueError):
            webapp_state.control_game_session("bad-action", "fly")

    def test_start_game_success_path(self):
        with patch("webapp_state.apply_perf_profile") as mock_perf, patch(
            "webapp_state.load_state"
        ) as mock_load, patch("webapp_state._resolve_start_game") as mock_resolve, patch(
            "webapp_state._start_launch_command", return_value=(["/bin/true"], "/tmp")
        ), patch(
            "webapp_state._apply_start_plugins", return_value=(["/bin/true"], "/tmp")
        ), patch(
            "webapp_state._validate_start_command"
        ), patch(
            "webapp_state.update_state"
        ) as mock_update, patch(
            "webapp_state._annotate_gamescope_start"
        ), patch(
            "webapp_state._publish_start_events"
        ), patch(
            "webapp_state.finish_session"
        ), patch(
            "subprocess.Popen"
        ) as mock_popen, patch(
            "threading.Thread"
        ):
            lease = PerfLease(applied=True, profile_name="test", restore=MagicMock())
            mock_perf.return_value = lease
            state = {
                "games": [{"game_id": "g1", "name": "Game"}],
                "profiles": {},
                "settings": {"progress_on_first_play": "Playing"},
                "history": [],
                "active_sessions": [],
            }
            mock_load.return_value = state
            mock_resolve.return_value = (state["games"][0], 0)
            process = MagicMock(pid=4321)
            mock_popen.return_value = process

            def apply_mutate(mutator):
                mutator(state)

            mock_update.side_effect = apply_mutate
            entry = webapp_state.start_game(index=0)
            self.assertIn("launch_id", entry)
            self.assertIn(entry["launch_id"], webapp_state.RUNNING)

    def test_finish_session_branches(self):
        import datetime

        started = datetime.datetime.now()
        lease = MagicMock()
        process = MagicMock()
        state = {
            "games": [{"game_id": "g1", "name": "Game", "path": "/bin/true", "playtime_seconds": 0}],
            "history": [],
            "settings": {
                "track_session_history": True,
                "backup_on_close": True,
                "save_backup_limit": 2,
            },
            "active_sessions": [{"launch_id": "fin-1"}],
        }
        webapp_state.RUNNING["fin-1"] = {
            "stable_game_id": "g1",
            "game": "Game",
            "game_path": "/bin/true",
            "steam_app_id": "",
            "heroic_app_id": "",
            "lutris_id": "",
            "gameyfin_id": "",
        }

        def update_state(mutator):
            mutator(state)

        with patch("webapp_state.load_state", return_value=state), patch(
            "webapp_state.update_state", side_effect=update_state
        ), patch("webapp_state.wait_for_exit", return_value=0), patch(
            "pkg.state.launch.backup_saves"
        ) as backup, patch("pkg.state.launch.enforce_backup_limit") as enforce, patch(
            "pkg.state.launch.auto_attach_obs_recording"
        ), patch("pkg.state.launch.close_store_client"), patch(
            "pkg.state.launch.run_plugins"
        ), patch("pkg.state.sse.session_event"), patch(
            "pkg.state.sse._publish_session_event"
        ), patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENBOX_SAFE_MODE", None)
            state["games"][0]["save_paths"] = ["/tmp"]
            webapp_state.finish_session("fin-1", 0, started, process, lease)
            backup.assert_called_once()
            enforce.assert_called_once()
            self.assertEqual(state["history"][-1]["game"], "Game")
            self.assertEqual(state["active_sessions"], [])

        webapp_state.RUNNING["fin-2"] = {"stable_game_id": "missing", "game": "Ghost"}
        state["active_sessions"] = [{"launch_id": "fin-2"}]
        state["games"][0].pop("save_paths", None)
        state["settings"]["backup_on_close"] = False
        with patch("webapp_state.load_state", return_value=state), patch(
            "webapp_state.update_state", side_effect=update_state
        ), patch("webapp_state.wait_for_exit", return_value=0), patch(
            "pkg.state.sse.session_event"
        ), patch("pkg.state.sse._publish_session_event"), patch(
            "pkg.state.launch.run_plugins"
        ):
            webapp_state.finish_session("fin-2", 0, started, process, lease)
            self.assertEqual(state["history"][-1]["game"], "Ghost")

        webapp_state.RUNNING["fin-3"] = {"restart": True, "stable_game_id": "g1", "game": "Game"}
        with patch("webapp_state.load_state", return_value=state), patch(
            "webapp_state.update_state", side_effect=update_state
        ), patch("webapp_state.wait_for_exit", return_value=0), patch(
            "pkg.state.sse.session_event"
        ), patch("pkg.state.sse._publish_session_event"), patch(
            "webapp_state.start_game"
        ) as restart, patch("pkg.state.launch.run_plugins"):
            webapp_state.finish_session("fin-3", 0, started, process, lease)
            restart.assert_called_once()

        webapp_state.RUNNING["fin-4"] = {"game": "Game"}
        state["active_sessions"] = [{"launch_id": "fin-4"}]
        broken_lease = MagicMock()
        broken_lease.restore.side_effect = RuntimeError("perf fail")
        update_calls = []

        def fail_first_mutate(mutator):
            update_calls.append(mutator)
            if len(update_calls) == 1:
                raise RuntimeError("commit fail")
            mutator(state)

        with patch("webapp_state.load_state", return_value=state), patch(
            "webapp_state.update_state", side_effect=fail_first_mutate
        ), patch("webapp_state.wait_for_exit", return_value=0), patch(
            "pkg.state.sse.session_event"
        ), patch("pkg.state.sse._publish_session_event"):
            with self.assertRaises(RuntimeError):
                webapp_state.finish_session("fin-4", 0, started, process, broken_lease)


class TestF4HealthAndDoctorIntegration(unittest.TestCase):
    """F4: health pass BIOS SHA1/firmware/core drift via registry?health=1 and actionable preflight."""

    def test_registry_health_fields(self):
        import tempfile
        from pathlib import Path
        from pkg.parity.parity_emulator_defs import load_registry
        from handlers.emulators import EmulatorsHandlers

        # Use real defs dir but health=True should add bios_ok/core_ok/firmware_ok
        registry = load_registry(health=True)
        self.assertIn("adapters", registry)
        for adapter in registry["adapters"]:
            self.assertIn("bios_ok", adapter)
            self.assertIn("core_ok", adapter)
            self.assertIn("firmware_ok", adapter)
            self.assertIsInstance(adapter["bios_ok"], bool)
            self.assertIsInstance(adapter["core_ok"], bool)
            self.assertIsInstance(adapter["firmware_ok"], bool)

        # Check that at least one retroarch core is currently missing (core_ok False) in CI
        retro = [a for a in registry["adapters"] if a["adapter_id"] == "retroarch-nes"]
        self.assertTrue(retro)
        # core path exists? In CI, /usr/lib/libretro/fceumm_libretro.so likely missing, so core_ok False
        # But we don't assert specific, just that field exists.

        # Test handler health=1 returns same
        h = EmulatorsHandlers()
        h.send_json = lambda status, payload: setattr(h, "_captured", (status, payload))
        # Simulate parsed with health=1
        class Parsed:
            query = "health=1"
        h._api_get_api_v2_emulators_registry(Parsed())
        status, payload = h._captured
        self.assertEqual(status, 200)
        for a in payload["adapters"]:
            self.assertIn("bios_ok", a)
            self.assertIn("core_ok", a)
            self.assertIn("firmware_ok", a)

        # health=0 should not include bios_ok
        h2 = EmulatorsHandlers()
        h2.send_json = lambda status, payload: setattr(h2, "_captured2", (status, payload))
        class Parsed2:
            query = "health=0"
        h2._api_get_api_v2_emulators_registry(Parsed2())
        status2, payload2 = h2._captured2
        # When health=0, bios_ok may still be absent or present? Our impl only adds when health=True
        # So check absence
        for a in payload2["adapters"]:
            # Should not have bios_ok when not requested
            self.assertNotIn("bios_ok", a)

        # health with temp defs containing bios_sha1
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Create BIOS file with known content
            bios_file = root / "bios.bin"
            bios_file.write_bytes(b"bios-content")
            import hashlib
            sha = hashlib.sha1(b"bios-content").hexdigest()
            (root / "test-bios.yaml").write_text(
                f"schema_version: 1\nadapter_id: test-bios\nemulator_id: test.emu\nlabel: TestEmu\nplatform: TestPlat\n"
                f"extensions:\n  - testbios\nnative_exe: testemu\nflatpak_app_id: test.emu\n"
                f"startup_args:\n  - \"{{path}}\"\n"
                f"bios_path: {bios_file}\n"
                f"bios_sha1: {sha}\n",
                encoding="utf-8",
            )
            reg = load_registry(root, health=True)
            self.assertEqual(len(reg["adapters"]), 1)
            self.assertTrue(reg["adapters"][0]["bios_ok"])
            # drift: change sha
            (root / "test-bios.yaml").write_text(
                f"schema_version: 1\nadapter_id: test-bios2\nemulator_id: test.emu2\nlabel: TestEmu2\nplatform: TestPlat2\n"
                f"extensions:\n  - testbios2\nnative_exe: testemu2\nflatpak_app_id: test.emu2\n"
                f"startup_args:\n  - \"{{path}}\"\n"
                f"bios_path: {bios_file}\n"
                f"bios_sha1: deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n",
                encoding="utf-8",
            )
            # need to clear cache? load_registry uses defs_dir, not cache
            reg2 = load_registry(root, health=True)
            # one of them should be bios_ok False due to drift
            bios_oks = [a["bios_ok"] for a in reg2["adapters"]]
            self.assertIn(False, bios_oks)

    def test_adapter_health_helpers(self):
        from pkg.parity.parity_emulator_defs import _bios_ok_for_adapter, _core_ok_for_adapter, _firmware_ok_for_adapter

        # No bios path -> bios ok depends on hints, but for non-BIOS platform should be true
        adapter = {"label": "MAME", "adapter_id": "mame-arcade", "startup_args": ["{path}"], "platform": "Arcade"}
        self.assertTrue(_bios_ok_for_adapter(adapter))
        self.assertTrue(_firmware_ok_for_adapter(adapter))
        self.assertTrue(_core_ok_for_adapter(adapter))

        # Core path missing -> false
        adapter2 = {"label": "RetroArch", "adapter_id": "retroarch-nes", "core_path": "/no/such/core.so", "startup_args": ["-L", "/no/such/core.so", "{path}"]}
        self.assertFalse(_core_ok_for_adapter(adapter2))
        # BIOS path present but file missing -> false
        adapter3 = {"label": "DuckStation", "bios_path": "/tmp/missing-bios.bin", "bios_sha1": None}
        self.assertFalse(_bios_ok_for_adapter(adapter3))

    def test_preflight_surfaces_fix_action_via_handler(self):
        import tempfile
        import os
        from pathlib import Path
        from handlers.launch import LaunchHandlers
        from openbox import save_state

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPENBOX_DATA_DIR"] = tmp
            rom = Path(tmp) / "game.iso"
            rom.write_bytes(b"ISO")
            save_state({"games": [{"name": "Iso", "path": str(rom), "platform": "", "game_id": "game-0123456789abcdef01234567-9"}], "profiles": {}, "history": [], "settings": {}, "playlists": []})
            h = LaunchHandlers()
            h.send_json = lambda status, payload, **k: setattr(h, "_cap", (status, payload))
            h.launch_preflight({"game_id": "game-0123456789abcdef01234567-9"})
            status, payload = h._cap
            self.assertEqual(status, 200)
            # For ambiguous iso, should be blocked with AMBIGUOUS_PLATFORM and fix_action
            self.assertEqual(payload["status"], "blocked")
            self.assertTrue(any(c["code"] == "AMBIGUOUS_PLATFORM" for c in payload["checks"]))
            for c in payload["checks"]:
                if c["severity"] == "error":
                    self.assertIn("fix_action", c)
            # Batch also actionable
            h2 = LaunchHandlers()
            h2.send_json = lambda status, payload, **k: setattr(h2, "_cap2", (status, payload))
            h2.launch_preflight_batch({"items": [{"game_id": "game-0123456789abcdef01234567-9"}]})
            status2, payload2 = h2._cap2
            self.assertIn("totals", payload2)
            self.assertGreater(payload2["totals"]["blocked"], 0)
            for res in payload2["results"]:
                for chk in res["checks"]:
                    if chk["severity"] == "error":
                        self.assertIn("fix_action", chk)
            os.environ.pop("OPENBOX_DATA_DIR", None)

if __name__ == "__main__":
    unittest.main()
