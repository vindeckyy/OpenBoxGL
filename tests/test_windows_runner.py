#!/usr/bin/env python3
"""Contract tests for the Windows CI test runner.

`scripts/run_windows_tests.py` is what the `windows-latest` job actually runs,
so it is the only gate that exercises the suite on a real Windows runner. Two
properties are worth pinning here, because both fail silently otherwise:

1. A suite is retried up to ATTEMPTS, matching `scripts/check_tests.py`. Without
   it, a wall-clock assertion can fail on a loaded shared CI runner while
   passing locally, and the defect is only ever visible in CI.
2. A retry is *reported* and the first failure is retained. A retry that
   swallowed the evidence would turn a real intermittent bug into a green job.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from run_windows_tests import ATTEMPTS, run_file  # noqa: E402


def _result(status, stderr=""):
    return {
        "status": status,
        "returncode": 0 if status == "pass" else 1,
        "seconds": 1.0,
        "stderr_tail": stderr or ("AssertionError: simulated" if status != "pass" else ""),
        "stdout_tail": "",
    }


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.calls = 0
        self._globals = run_file.__globals__
        self._original = self._globals["_run_once"]

    def tearDown(self):
        self._globals["_run_once"] = self._original

    def _patch(self, statuses):
        """Fail with each status in order, then pass forever after."""
        sequence = list(statuses)

        def fake(path, env):
            self.calls += 1
            return _result(sequence.pop(0) if sequence else "pass")

        self._globals["_run_once"] = fake

    def test_passing_file_runs_once(self):
        self._patch([])
        result = run_file(Path("test_x.py"), {})
        self.assertEqual("pass", result["status"])
        self.assertEqual(1, self.calls, "a clean pass must not be retried")

    def test_transient_failure_is_retried_and_reported(self):
        self._patch(["fail"])
        result = run_file(Path("test_x.py"), {})
        self.assertEqual(2, self.calls, "must take a second attempt")
        self.assertEqual("retried", result["status"])
        self.assertIn("first_failure", result, "the first failure must be kept, not discarded")
        self.assertIn("AssertionError", result["first_failure"]["stderr_tail"])

    def test_persistent_failure_still_fails_after_every_attempt(self):
        """The retry must not launder a real bug into a green job."""
        self._patch(["fail"] * (ATTEMPTS + 5))
        result = run_file(Path("test_x.py"), {})
        self.assertEqual(ATTEMPTS, self.calls, "must stop at ATTEMPTS, not loop forever")
        self.assertEqual("fail", result["status"])
        self.assertEqual(ATTEMPTS, result["attempts"])

    def test_retry_budget_matches_the_local_gate(self):
        """A tolerance that differs from check_tests.py reopens the gap this
        closes: the two runners must report the same thing."""
        source = (ROOT / "scripts" / "check_tests.py").read_text(encoding="utf-8")
        self.assertIn("range(3)", source, "check_tests.py must still allow 3 attempts")
        self.assertEqual(3, ATTEMPTS, "keep the two runners' tolerance identical")

    def test_retried_counts_as_passed_in_the_summary(self):
        """main() must not treat a retried suite as a failure, and must still
        name it -- otherwise the job is red, or the retry is invisible."""
        source = (ROOT / "scripts" / "run_windows_tests.py").read_text(encoding="utf-8")
        self.assertIn('r["status"] in ("pass", "retried")', source)
        self.assertIn("retried and passed", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
