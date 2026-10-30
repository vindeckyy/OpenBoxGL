#!/usr/bin/env python3
"""Performance no-regression gate (plan W7-04).

The gate fails a measurement that got both relatively and absolutely worse. The absolute floor
keeps timer noise from failing a build.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import check_perf_regression as gate  # noqa: E402


def report(**sizes):
    return {size: values for size, values in sizes.items()}


def measure(value):
    return {"p95_ms": value}


class RegressionComparisonTests(unittest.TestCase):
    def test_a_relative_regression_above_the_limit_is_a_regression(self):
        rows = gate.compare(report(**{"10000": {"facet": measure(100.0)}}), report(**{"10000": {"facet": measure(120.0)}}))
        self.assertEqual(len(gate.regressions(rows, 10.0, 1.0)), 1)

    def test_a_change_inside_the_limit_is_not_a_regression(self):
        rows = gate.compare(report(**{"10000": {"facet": measure(100.0)}}), report(**{"10000": {"facet": measure(105.0)}}))
        self.assertEqual(gate.regressions(rows, 10.0, 1.0), [])

    def test_a_sub_millisecond_change_is_never_a_regression(self):
        rows = gate.compare(report(**{"20000": {"coverflow": measure(0.3)}}), report(**{"20000": {"coverflow": measure(0.4)}}))
        self.assertEqual(gate.regressions(rows, 10.0, 1.0), [])

    def test_an_improvement_is_not_a_regression(self):
        rows = gate.compare(report(**{"20000": {"facet": measure(300.0)}}), report(**{"20000": {"facet": measure(250.0)}}))
        self.assertEqual(gate.regressions(rows, 10.0, 1.0), [])

    def test_measurements_missing_from_one_report_are_ignored(self):
        rows = gate.compare(report(**{"10000": {"facet": measure(100.0)}}), report(**{"10000": {"facet": measure(120.0), "other": measure(1.0)}}))
        self.assertEqual([row["key"] for row in rows], ["facet"])


class RegressionCommandTests(unittest.TestCase):
    def run_gate(self, baseline, current):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "base.json"
            cur = Path(directory) / "current.json"
            base.write_text(json.dumps(baseline), encoding="utf-8")
            cur.write_text(json.dumps(current), encoding="utf-8")
            done = subprocess.run(
                [sys.executable, "-B", str(ROOT / "scripts" / "check_perf_regression.py"),
                 "--baseline", str(base), "--current", str(cur)],
                capture_output=True, text=True, timeout=120, check=False,
            )
            return done.returncode, done.stdout

    def test_the_command_fails_on_a_real_regression(self):
        code, out = self.run_gate(report(**{"10000": {"facet": measure(100.0)}}), report(**{"10000": {"facet": measure(150.0)}}))
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out)

    def test_the_command_passes_without_a_regression(self):
        code, out = self.run_gate(report(**{"10000": {"facet": measure(100.0)}}), report(**{"10000": {"facet": measure(100.0)}}))
        self.assertEqual(code, 0)
        self.assertIn("no regression", out)


if __name__ == "__main__":
    unittest.main()
