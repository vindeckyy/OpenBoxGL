#!/usr/bin/env python3
"""Failure codes the UI explains in full (plan W6-07, ADR 0091).

The frontend replaces a server message with a localized explanation when the problem
document carries one of these codes (static/state.js CODE_MESSAGE_KEYS). Each code must
therefore have an English explanation that is real prose, not the code itself or the raw
exception text.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import api_errors  # noqa: E402
from state_store import StateFutureVersionError, StateMigrationError  # noqa: E402

EXPLAINED_CODES = {
    "STATE_FUTURE_VERSION": StateFutureVersionError,
    "STATE_MIGRATION_FAILED": StateMigrationError,
    "MOVE_PLAN_STALE": api_errors.MovePlanStale,
    "BACKUP_FORMAT_UNSUPPORTED": api_errors.BackupFormatUnsupported,
}


class ExplainedFailureCodeTests(unittest.TestCase):
    def setUp(self):
        self.english = json.loads((ROOT / "locales" / "en.json").read_text(encoding="utf-8"))["errors"]

    def test_each_code_is_the_stable_key_of_its_error_class(self):
        for code, error_class in EXPLAINED_CODES.items():
            with self.subTest(code=code):
                self.assertEqual(error_class.code, code)

    def test_each_code_has_a_real_english_explanation(self):
        for code in EXPLAINED_CODES:
            with self.subTest(code=code):
                message = self.english.get(code.lower(), "")
                self.assertGreater(len(message.split()), 6)
                self.assertNotIn(code, message)


if __name__ == "__main__":
    unittest.main()
