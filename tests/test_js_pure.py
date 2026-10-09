#!/usr/bin/env python3
"""Run the Node behavioural tests for static/pure.js (tests/js/*.test.mjs).

Node is not a runtime dependency, so outside CI a machine without it skips with a
notice. In CI (CI=true) a missing Node is a failure, so the gate cannot pass by
skipping the one check that exercises the frontend logic.
"""

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _node():
    return os.environ.get("OPENBOX_NODE") or shutil.which("node")


class PureJsTests(unittest.TestCase):
    def test_pure_helpers_behave_as_the_frontend_expects(self):
        node = _node()
        if not node:
            if os.environ.get("CI"):
                self.fail("node is required in CI to run tests/js/*.test.mjs")
            print("SKIP: node not found; tests/js/*.test.mjs not run (set OPENBOX_NODE to run them)")
            return
        files = sorted(str(path) for path in (ROOT / "tests" / "js").glob("*.test.mjs"))
        self.assertTrue(files, "no tests/js/*.test.mjs files found")
        result = subprocess.run(
            [node, "--test", *files], cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    sys.exit(unittest.main())
