"""Tests for pkg.state.commands — extracted command validation and execution."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unittest


class TestStateCommands(unittest.TestCase):
    def test_module_importable(self):
        import pkg.state.commands
        self.assertIsNotNone(pkg.state.commands)

    def test_clean_commands_exported(self):
        from pkg.state.commands import clean_commands
        self.assertTrue(callable(clean_commands))

    def test_clean_commands_valid(self):
        from pkg.state.commands import clean_commands
        result = clean_commands(["echo hello", "ls -la"])
        self.assertEqual(result, ["echo hello", "ls -la"])

    def test_clean_commands_strips_whitespace(self):
        from pkg.state.commands import clean_commands
        result = clean_commands(["  echo hello  ", "  ls -la  "])
        self.assertEqual(result, ["echo hello", "ls -la"])

    def test_clean_commands_filters_empty(self):
        from pkg.state.commands import clean_commands
        result = clean_commands(["echo hello", "", "  ", "ls -la"])
        self.assertEqual(result, ["echo hello", "ls -la"])

    def test_clean_commands_rejects_non_list(self):
        from pkg.state.commands import clean_commands
        with self.assertRaises(ValueError):
            clean_commands("not a list")

    def test_clean_commands_rejects_too_many(self):
        from pkg.state.commands import clean_commands
        with self.assertRaises(ValueError):
            clean_commands([f"cmd{i}" for i in range(26)])

    def test_run_configured_commands_exported(self):
        from pkg.state.commands import run_configured_commands
        self.assertTrue(callable(run_configured_commands))

    def test_webapp_state_re_exports(self):
        """from webapp_state import X still works for moved functions."""
        import webapp_state
        from pkg.state.commands import clean_commands, run_configured_commands
        self.assertIs(webapp_state.clean_commands, clean_commands)
        self.assertIs(webapp_state.run_configured_commands, run_configured_commands)


if __name__ == "__main__":
    unittest.main()
