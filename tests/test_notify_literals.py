#!/usr/bin/env python3
"""Count English-literal notify() messages in the frontend. The allowance is zero.

`notify('...')` with a literal English string skips t(), so a de, es, fr or pt user
gets an English toast. The i18n gate counts keys, so it cannot see this. The
migration to t() is complete (1.16.1); the allowance stays at zero, so the next
literal fails the gate.

Two call shapes carry a message: notify(message) and notify(kind, message), where
kind is info, success, warning or error. Both are counted.
"""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ALLOWED_LITERALS = 0

KINDS = ("info", "success", "warning", "error")
FIRST_ARG = re.compile(r"""\bnotify\(\s*(['"`])([^'"`]*)""")
SECOND_ARG = re.compile(r"""\bnotify\(\s*['"`](?:info|success|warning|error)['"`]\s*,\s*(['"`])([^'"`]*)""")


def count_literal_notifies() -> tuple[int, dict[str, int]]:
    per_file: dict[str, int] = {}
    for path in sorted((ROOT / "static").glob("*.js")):
        source = path.read_text(encoding="utf-8")
        found = 0
        for match in FIRST_ARG.finditer(source):
            text = match.group(2)
            if text in KINDS:
                continue  # a severity keyword; the message is the second argument
            if text and text[0].isalpha():
                found += 1
        for match in SECOND_ARG.finditer(source):
            text = match.group(2)
            if text and text[0].isalpha():
                found += 1
        if found:
            per_file[path.name] = found
    return sum(per_file.values()), per_file


class NotifyLiteralRatchetTests(unittest.TestCase):
    def test_no_english_literal_reaches_a_toast(self):
        total, per_file = count_literal_notifies()
        if total > ALLOWED_LITERALS:
            self.fail(
                f"{total} English-literal notify() message(s); route them through t() with a key in all five "
                f"locales. By file: {per_file}"
            )
        self.assertEqual(total, ALLOWED_LITERALS)


if __name__ == "__main__":
    sys.exit(unittest.main())
