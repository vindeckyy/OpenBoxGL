#!/usr/bin/env python3
"""Regression tests for handheld performance profiles (parity_perf.py).

These tests are deterministic: they never run a real ryzenadj binary or
launch a game. ``shutil.which`` and ``subprocess.run`` are faked so the
exact ryzenadj argv can be asserted.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import parity_perf


def fake_which(binary):
    if binary == "ryzenadj":
        return "/fake/bin/ryzenadj"
    return None


class RecordingRun:
    """subprocess.run fake that records argv and returns a fake result."""

    def __init__(self, returncode=0, stderr=""):
        self.calls = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        return subprocess.CompletedProcess(args, self.returncode, stdout="", stderr=self.stderr)


class EffectiveProfileNameTest(unittest.TestCase):
    def test_selected_override_wins(self):
        profiles = {"Linux": "default {path}", "handheld": "custom {path}"}
        game = {"platform": "Linux", "launch_profile": "handheld"}
        self.assertEqual(parity_perf.effective_profile_name(game, profiles), "handheld")

    def test_blank_selected_falls_back_to_platform(self):
        profiles = {"Linux": "default {path}"}
        self.assertEqual(parity_perf.effective_profile_name({"platform": "Linux", "launch_profile": ""}, profiles), "Linux")

    def test_unknown_selected_falls_back_to_platform(self):
        profiles = {"Linux": "default {path}"}
        self.assertEqual(parity_perf.effective_profile_name({"platform": "Linux", "launch_profile": "missing"}, profiles), "Linux")

    def test_no_platform(self):
        self.assertEqual(parity_perf.effective_profile_name({"platform": ""}, {}), "")


class PerfShouldApplyTest(unittest.TestCase):
    def test_off_never_applies(self):
        state = {"settings": {"apply_perf": "off"}}
        self.assertFalse(parity_perf.perf_should_apply(state))

    def test_always_applies(self):
        state = {"settings": {"apply_perf": "always"}}
        self.assertTrue(parity_perf.perf_should_apply(state))

    def test_auto_applies_on_gamescope_guest(self):
        state = {"settings": {"apply_perf": "auto"}}
        with mock.patch.object(parity_perf, "is_gamescope_guest", return_value=True), \
             mock.patch.object(parity_perf, "_has_battery", return_value=False):
            self.assertTrue(parity_perf.perf_should_apply(state))

    def test_auto_applies_on_battery_host(self):
        state = {"settings": {"apply_perf": "auto"}}
        with mock.patch.object(parity_perf, "is_gamescope_guest", return_value=False), \
             mock.patch.object(parity_perf, "_has_battery", return_value=True):
            self.assertTrue(parity_perf.perf_should_apply(state))

    def test_auto_skips_desktop(self):
        state = {"settings": {"apply_perf": "auto"}}
        with mock.patch.object(parity_perf, "is_gamescope_guest", return_value=False), \
             mock.patch.object(parity_perf, "_has_battery", return_value=False):
            self.assertFalse(parity_perf.perf_should_apply(state))


class RunRyzenadjTest(unittest.TestCase):
    def test_missing_binary_reports_not_installed(self):
        ok, message = parity_perf._run_ryzenadj(["-stapm-limit=12000"], which=lambda _: None)
        self.assertFalse(ok)
        self.assertIn("not installed", message)

    def test_invokes_correct_flag(self):
        runner = RecordingRun()
        ok, _ = parity_perf._run_ryzenadj(["-stapm-limit=12000"], which=fake_which, run=runner)
        self.assertTrue(ok)
        self.assertEqual(runner.calls, [["/fake/bin/ryzenadj", "-stapm-limit=12000"]])

    def test_nonzero_exit_reports_stderr(self):
        runner = RecordingRun(returncode=1, stderr="permission denied")
        ok, message = parity_perf._run_ryzenadj(["-stapm-limit=12000"], which=fake_which, run=runner)
        self.assertFalse(ok)
        self.assertEqual(message, "permission denied")


class ApplyRestorePerfTest(unittest.TestCase):
    def state(self, perf=None, apply_perf="always"):
        return {
            "settings": {"apply_perf": apply_perf},
            "perf_profiles": perf or {},
        }

    def fake_run_ryzenadj(self, runner):
        def side_effect(args, *rest, **kwargs):
            full = ["/fake/bin/ryzenadj", *args]
            runner(full)  # record argv
            return (True, "")
        return side_effect

    def test_apply_enabled_profile(self):
        runner = RecordingRun()
        state = self.state({"Linux": {"enabled": True, "tdp_w": 12, "restore_tdp_w": 15}})
        with mock.patch.object(parity_perf, "_run_ryzenadj", side_effect=self.fake_run_ryzenadj(runner)):
            result = parity_perf.apply_perf_profile("Linux", state)
        self.assertTrue(result.applied)
        self.assertEqual(runner.calls, [["/fake/bin/ryzenadj", "-stapm-limit=12000"]])

    def test_apply_disabled_or_missing_profile_is_noop(self):
        runner = RecordingRun()
        state = self.state({"Linux": {"enabled": False, "tdp_w": 12}})
        with mock.patch.object(parity_perf, "_run_ryzenadj", side_effect=self.fake_run_ryzenadj(runner)):
            result = parity_perf.apply_perf_profile("Linux", state)
        self.assertFalse(result.applied)
        self.assertEqual(runner.calls, [])
        state2 = self.state({})
        result2 = parity_perf.apply_perf_profile("Linux", state2)
        self.assertFalse(result2.applied)

    def test_apply_auto_skipped_on_desktop(self):
        runner = RecordingRun()
        state = self.state({"Linux": {"enabled": True, "tdp_w": 12}}, apply_perf="auto")
        with mock.patch.object(parity_perf, "_run_ryzenadj", side_effect=self.fake_run_ryzenadj(runner)), \
             mock.patch.object(parity_perf, "is_gamescope_guest", return_value=False), \
             mock.patch.object(parity_perf, "_has_battery", return_value=False):
            result = parity_perf.apply_perf_profile("Linux", state)
        self.assertFalse(result.applied)
        self.assertEqual(runner.calls, [])

    def test_apply_off_never_runs(self):
        runner = RecordingRun()
        state = self.state({"Linux": {"enabled": True, "tdp_w": 12}}, apply_perf="off")
        with mock.patch.object(parity_perf, "_run_ryzenadj", side_effect=self.fake_run_ryzenadj(runner)):
            result = parity_perf.apply_perf_profile("Linux", state)
        self.assertFalse(result.applied)
        self.assertEqual(runner.calls, [])

    def test_apply_bad_tdp_is_noop(self):
        runner = RecordingRun()
        state = self.state({"Linux": {"enabled": True, "tdp_w": "abc"}})
        with mock.patch.object(parity_perf, "_run_ryzenadj", side_effect=self.fake_run_ryzenadj(runner)):
            result = parity_perf.apply_perf_profile("Linux", state)
        self.assertFalse(result.applied)
        self.assertEqual(runner.calls, [])

    def test_restore_fires_only_with_restore_value(self):
        runner = RecordingRun()
        state = self.state({"Linux": {"enabled": True, "tdp_w": 12, "restore_tdp_w": 15}})
        with mock.patch.object(parity_perf, "_run_ryzenadj", side_effect=self.fake_run_ryzenadj(runner)):
            result = parity_perf.restore_perf_profile("Linux", state)
        self.assertTrue(result["applied"])
        self.assertEqual(runner.calls, [["/fake/bin/ryzenadj", "-stapm-limit=15000"]])
        state2 = self.state({"Linux": {"enabled": True, "tdp_w": 12}})
        result2 = parity_perf.restore_perf_profile("Linux", state2)
        self.assertFalse(result2["applied"])

    def test_restore_skips_when_apply_skipped(self):
        runner = RecordingRun()
        state = self.state({"Linux": {"enabled": True, "tdp_w": 12, "restore_tdp_w": 15}}, apply_perf="auto")
        with mock.patch.object(parity_perf, "_run_ryzenadj", side_effect=self.fake_run_ryzenadj(runner)), \
             mock.patch.object(parity_perf, "is_gamescope_guest", return_value=False), \
             mock.patch.object(parity_perf, "_has_battery", return_value=False):
            result = parity_perf.restore_perf_profile("Linux", state)
        self.assertFalse(result["applied"])
        self.assertEqual(runner.calls, [])

    def test_apply_never_raises_on_failure(self):
        state = self.state({"Linux": {"enabled": True, "tdp_w": 12}})
        with mock.patch.object(parity_perf, "_run_ryzenadj", return_value=(False, "boom")):
            result = parity_perf.apply_perf_profile("Linux", state)  # must not raise
        self.assertFalse(result.applied)


class SavePerfProfilesTest(unittest.TestCase):
    """End-to-end through the real Handler, without starting a server."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        import web_app
        from openbox import STATE_STORE
        self.web_app = web_app
        # Snapshot every shared global this test mutates so tearDown can
        # restore them. Other test modules import the same web_app/STATE_STORE
        # singletons; leaving them pointed at a tempdir poisons later tests.
        self._prev_data_dir = os.environ.get("OPENBOX_DATA_DIR")
        self._prev_data = web_app.DATA
        self._prev_store_path = STATE_STORE.path
        self._prev_store_lock = STATE_STORE.lock_path
        self._prev_store_backup = STATE_STORE.backup_path
        self._prev_cached_state = STATE_STORE._cached_state
        self._prev_cached_signature = STATE_STORE._cached_signature
        self.addCleanup(self._restore_globals)
        os.environ["OPENBOX_DATA_DIR"] = self.tempdir.name
        self.data_dir = Path(self.tempdir.name)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        web_app.DATA = self.data_dir / "library.json"
        STATE_STORE.path = web_app.DATA
        STATE_STORE.lock_path = web_app.DATA.with_name(f".{web_app.DATA.name}.lock")
        STATE_STORE.backup_path = web_app.DATA.with_name(f"{web_app.DATA.name}.bak")
        STATE_STORE._cached_state = None
        STATE_STORE._cached_signature = None
        # Seed an empty library so transactions have a base to mutate.
        STATE_STORE.save({"games": [], "profiles": {}, "settings": {}, "history": [], "playlists": []})

    def _restore_globals(self):
        from openbox import STATE_STORE

        if self._prev_data_dir is None:
            os.environ.pop("OPENBOX_DATA_DIR", None)
        else:
            os.environ["OPENBOX_DATA_DIR"] = self._prev_data_dir
        self.web_app.DATA = self._prev_data
        STATE_STORE.path = self._prev_store_path
        STATE_STORE.lock_path = self._prev_store_lock
        STATE_STORE.backup_path = self._prev_store_backup
        STATE_STORE._cached_state = self._prev_cached_state
        STATE_STORE._cached_signature = self._prev_cached_signature

    def handler(self):
        handler = self.web_app.Handler.__new__(self.web_app.Handler)
        handler.responses = []
        handler.send_json = lambda status, payload: handler.responses.append((status, payload))
        return handler

    def test_save_perf_profiles_persists(self):
        handler = self.handler()
        handler.save_perf_profiles({
            "perf_profiles": {
                "Linux": {"enabled": True, "tdp_w": 12, "restore_tdp_w": 15},
                "  ": {"enabled": True, "tdp_w": 5},  # blank name dropped
                "PlayStation": {"enabled": False, "tdp_w": 0, "restore_tdp_w": 0},  # all-zero dropped
            }
        })
        status, payload = handler.responses[0]
        self.assertEqual(status, 200)
        self.assertEqual(payload["saved"], 1)
        saved = self.web_app.load_state().get("perf_profiles", {})
        self.assertEqual(saved, {"Linux": {"enabled": True, "tdp_w": 12.0, "restore_tdp_w": 15.0}})

    def test_save_perf_profiles_rejects_bad_tdp(self):
        handler = self.handler()
        with self.assertRaises(ValueError):
            handler.save_perf_profiles({"perf_profiles": {"Linux": {"enabled": True, "tdp_w": "nope"}}})

    def test_apply_perf_setting_validation(self):
        handler = self.handler()
        with self.assertRaises(ValueError):
            handler.save_settings({"apply_perf": "sometimes"})
        handler.save_settings({"apply_perf": "off"})
        status, payload = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload.get("apply_perf"), "off")


if __name__ == "__main__":
    unittest.main(verbosity=2)
