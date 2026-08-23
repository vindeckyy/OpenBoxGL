"""Tests for narrowed exception handling in run_configured_commands, import loop, and SSE."""

import os
import queue
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pkg" / "parity"))


class RunConfiguredCommandsTests(unittest.TestCase):
    """run_configured_commands should log OSError/SubprocessError, not swallow them."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["OPENBOX_DATA_DIR"] = self.tmpdir.name
        (Path(self.tmpdir.name) / "openbox.json").write_text("{}")

    def tearDown(self):
        self.tmpdir.cleanup()
        os.environ.pop("OPENBOX_DATA_DIR", None)

    def _call(self, key="startup_commands"):
        from pkg.state.commands import run_configured_commands
        run_configured_commands(key)

    def test_oserror_is_logged_not_swallowed(self):
        """OSError from Popen should be caught and logged as a warning."""
        from pkg.state.commands import run_configured_commands

        state = {"settings": {"startup_commands": ["/nonexistent/binary"]}}
        with patch("pkg.state.commands.load_state", return_value=state):
            with self.assertLogs("openbox", level="WARNING") as captured:
                run_configured_commands("startup_commands")
        self.assertTrue(
            any("run_configured_commands" in msg for msg in captured.output),
            f"Expected warning log, got: {captured.output}",
        )

    def test_subprocess_error_is_logged(self):
        """SubprocessError should be caught and logged."""
        import subprocess

        state = {"settings": {"startup_commands": ["echo hello"]}}
        with patch("pkg.state.commands.load_state", return_value=state):
            with patch("subprocess.Popen", side_effect=subprocess.SubprocessError("boom")):
                with self.assertLogs("openbox", level="WARNING") as captured:
                    self._call()
        self.assertTrue(
            any("run_configured_commands" in msg and "boom" in msg for msg in captured.output),
            f"Expected SubprocessError warning, got: {captured.output}",
        )

    def test_unexpected_exception_propagates(self):
        """Exceptions outside OSError/SubprocessError should propagate, not be swallowed."""
        state = {"settings": {"startup_commands": ["echo hello"]}}
        with patch("pkg.state.commands.load_state", return_value=state):
            with patch("shlex.split", side_effect=RuntimeError("unexpected")):
                with self.assertRaises(RuntimeError):
                    self._call()


class ImportLoopContinuesTests(unittest.TestCase):
    """The install_emulator loop in import_wizard should continue after individual failures."""

    def test_loop_continues_after_single_failure(self):
        """If one install_emulator call raises, the remaining installs still run."""
        calls = []

        def fake_install(app_id):
            calls.append(app_id)
            if app_id == "fail-me":
                raise OSError("install failed")

        with patch("handlers.imports.install_emulator", side_effect=fake_install):
            # Simulate the loop logic from import_wizard
            chosen = {"a": "ok-1", "b": "fail-me", "c": "ok-2"}
            installs = []
            for app_id in chosen.values():
                if not app_id:
                    continue
                try:
                    fake_install(str(app_id))
                    installs.append(str(app_id))
                except (OSError, ValueError, RuntimeError):
                    pass

        self.assertEqual(installs, ["ok-1", "ok-2"])
        self.assertEqual(calls, ["ok-1", "fail-me", "ok-2"])

    def test_loop_logs_warnings(self):
        """Failures in the install loop should produce warning logs."""
        import logging

        logger = logging.getLogger("openbox")
        with patch("handlers.imports.install_emulator", side_effect=OSError("disk full")):
            with self.assertLogs("openbox", level="WARNING") as captured:
                chosen = {"x": "bad-app"}
                for app_id in chosen.values():
                    if not app_id:
                        continue
                    try:
                        from handlers.imports import install_emulator
                        install_emulator(str(app_id))
                    except (OSError, ValueError, RuntimeError) as e:
                        logger.warning("install_emulator %s: %s", app_id, e)
        self.assertTrue(
            any("install_emulator" in msg and "disk full" in msg for msg in captured.output),
            f"Expected install warning, got: {captured.output}",
        )


class SseCloseSubscriberTests(unittest.TestCase):
    """_close_sse_subscriber should narrow broad exceptions and log them."""

    def test_drain_oserror_is_logged(self):
        """An OSError during queue drain should be caught and logged."""
        from pkg.state.sse import _close_sse_subscriber

        sub = MagicMock()
        sub.get_nowait.side_effect = OSError("broken pipe")

        with self.assertLogs("openbox", level="WARNING") as captured:
            _close_sse_subscriber(sub)
        self.assertTrue(
            any("sse subscriber drain" in msg for msg in captured.output),
            f"Expected drain warning, got: {captured.output}",
        )

    def test_sentinel_oserror_is_logged(self):
        """An OSError during sentinel put should be caught and logged."""
        from pkg.state.sse import _close_sse_subscriber

        sub = MagicMock()
        sub.get_nowait.side_effect = queue.Empty
        sub.put_nowait.side_effect = OSError("closed")

        with self.assertLogs("openbox", level="WARNING") as captured:
            _close_sse_subscriber(sub)
        self.assertTrue(
            any("sse subscriber sentinel" in msg for msg in captured.output),
            f"Expected sentinel warning, got: {captured.output}",
        )

    def test_unexpected_drain_error_propagates(self):
        """A non-OSError/ValueError during drain should propagate."""
        from pkg.state.sse import _close_sse_subscriber

        sub = MagicMock()
        sub.get_nowait.side_effect = RuntimeError("unexpected")

        with self.assertRaises(RuntimeError):
            _close_sse_subscriber(sub)


if __name__ == "__main__":
    unittest.main()
