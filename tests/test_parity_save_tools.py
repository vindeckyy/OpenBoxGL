"""Regression tests for pkg.parity.parity_save_tools command construction."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pkg.parity  # noqa: E402,F401
from pkg.parity.parity_save_tools import run_ludusavi  # noqa: E402


class LudusaviCommandTests(unittest.TestCase):
    def _capture(self, **kwargs):
        seen = {}

        def run(command, **_kw):
            seen["command"] = command
            return mock.Mock(returncode=0, stdout="{}", stderr="")

        run_ludusavi(which=lambda name: "/usr/bin/ludusavi", run=run, **kwargs)
        return seen["command"]

    def test_dash_prefixed_game_name_is_not_an_option(self):
        command = self._capture(action="restore", game_name="--path=/tmp/evil", path="/backups")
        self.assertEqual(command[-2:], ["--", "--path=/tmp/evil"])
        self.assertLess(command.index("--path"), command.index("--"))

    def test_no_separator_without_game_name(self):
        command = self._capture(action="backups")
        self.assertNotIn("--", command)


if __name__ == "__main__":
    unittest.main()
