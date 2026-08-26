#!/usr/bin/env python3
"""Tests for scripts/check_changed_coverage.py."""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import check_changed_coverage as ccc  # noqa: E402


class FakeCoverage:
    def __init__(self, executed: dict[str, set[int]], module_pct: dict[str, float] | None = None):
        self.executed = executed
        self.module_pct = module_pct or {}

    def load(self):
        return None

    def analysis2(self, abs_path: str):
        rel = None
        for key in self.executed:
            if str((ROOT / key).resolve()) == abs_path:
                rel = key
                break
        if rel is None:
            raise OSError("not measured")
        statements = sorted(self.executed[rel])
        missing = {line for line in statements if line % 10 == 0}
        return abs_path, statements, set(), sorted(missing), ""

    def report(self, include=None, fail_under=None):
        return 0


class CheckChangedCoverageTests(unittest.TestCase):
    def test_measure_changed_lines_all_hit(self):
        with mock.patch.object(ccc, "_diff_base", return_value="base"), \
             mock.patch.object(ccc, "_changed_python_files", return_value=["sample.py"]), \
             mock.patch.object(ccc, "_changed_line_numbers", return_value={11, 12, 13, 14, 15, 16, 17, 18, 19, 20}), \
             mock.patch.object(ccc, "_load_coverage", return_value=FakeCoverage({"sample.py": set(range(1, 30))})):
            hit, total, files = ccc.measure_changed_lines()
        self.assertEqual(files, ["sample.py"])
        self.assertEqual(total, 10)
        self.assertEqual(hit, 9)

    def test_main_fails_under_95_with_nine_of_ten(self):
        with mock.patch.object(ccc, "_diff_base", return_value="base"), \
             mock.patch.object(ccc, "_changed_python_files", return_value=["sample.py"]), \
             mock.patch.object(ccc, "_changed_line_numbers", return_value={11, 12, 13, 14, 15, 16, 17, 18, 19, 20}), \
             mock.patch.object(ccc, "_load_coverage", return_value=FakeCoverage({"sample.py": set(range(1, 30))})), \
             mock.patch.object(ccc, "_run", return_value=mock.Mock(returncode=0, stdout="", stderr="")):
            code = ccc.main(["--fail-under=95"])
        self.assertEqual(code, 1)

    def test_main_passes_with_all_ten_hit(self):
        with mock.patch.object(ccc, "_diff_base", return_value="base"), \
             mock.patch.object(ccc, "_changed_python_files", return_value=["sample.py"]), \
             mock.patch.object(ccc, "_changed_line_numbers", return_value={11, 12, 13, 14, 15, 16, 17, 18, 19}), \
             mock.patch.object(ccc, "_load_coverage", return_value=FakeCoverage({"sample.py": set(range(1, 30))})), \
             mock.patch.object(ccc, "_run", return_value=mock.Mock(returncode=0, stdout="", stderr="")):
            code = ccc.main(["--fail-under=95"])
        self.assertEqual(code, 0)

    def test_touched_module_zero_percent_fails(self):
        with mock.patch.object(ccc, "_diff_base", return_value="base"), \
             mock.patch.object(ccc, "_changed_python_files", return_value=["empty.py"]), \
             mock.patch.object(ccc, "_changed_line_numbers", return_value={1, 2, 3}), \
             mock.patch.object(ccc, "_load_coverage", return_value=FakeCoverage({"empty.py": {1, 2, 3}}, {"empty.py": 0.0})), \
             mock.patch.object(ccc, "_module_percent", return_value=0.0), \
             mock.patch.object(ccc, "_run", return_value=mock.Mock(returncode=1, stdout="", stderr="")):
            code = ccc.main(["--fail-under=95"])
        self.assertEqual(code, 1)


class CheckTestsFloorConstants(unittest.TestCase):
    def test_coverage_floors_ratcheted(self):
        import importlib
        check_tests = importlib.import_module("scripts.check_tests")
        self.assertEqual(check_tests.COVERAGE_FLOOR, 70.0)
        self.assertEqual(check_tests.WEB_APP_FLOOR, 54.0)
        self.assertEqual(check_tests.CHANGED_LINE_FLOOR, 80.0)
        self.assertEqual(check_tests.NEW_MODULE_FLOOR, 85.0)


if __name__ == "__main__":
    unittest.main()
