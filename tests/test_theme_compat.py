#!/usr/bin/env python3
"""Theme validation (plan W6-03): missing tokens are reported and the theme still renders.

A theme is checked against the token contract in static/app.css ``:root``. A token the theme
does not set falls back to the built-in value, so the UI renders; the report names the fallback.
A token the contract does not know is reported and ignored. Declarations outside ``:root`` do not
count, because they do not set a token for the whole UI.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pkg.parity import parity_compat as compat  # noqa: E402

CONTRACT = {"--brand", "--focus", "--surface-card"}


class ThemeValidationTests(unittest.TestCase):
    def test_a_complete_theme_has_no_fallbacks(self):
        row = compat.theme_report("full", ":root { --brand: #123; --focus: #456; --surface-card: #789; }", CONTRACT)
        self.assertEqual((row["missing"], row["unknown"], row["disposition"]), ([], [], "complete"))

    def test_a_theme_missing_tokens_names_them_and_falls_back(self):
        row = compat.theme_report("partial", ":root { --brand: #123; }", CONTRACT)
        self.assertEqual(row["missing"], ["--focus", "--surface-card"])
        self.assertEqual(row["disposition"], "fallbacks")

    def test_an_unknown_token_is_reported_and_ignored(self):
        css = ":root { --brand: #1; --focus: #2; --surface-card: #3; --made-up: 9; }"
        row = compat.theme_report("extra", css, CONTRACT)
        self.assertEqual(row["unknown"], ["--made-up"])
        self.assertEqual(row["missing"], [])

    def test_a_declaration_outside_root_does_not_set_a_token(self):
        row = compat.theme_report("scoped", ".card { --brand: #1; } :root { --focus: #2; --surface-card: #3; }", CONTRACT)
        self.assertEqual(row["missing"], ["--brand"])

    def test_the_repository_stock_themes_declare_only_contract_tokens(self):
        contract = compat.contract_tokens(ROOT / "static" / "app.css")
        for row in compat.theme_reports(ROOT / "themes", contract):
            with self.subTest(theme=row["name"]):
                self.assertEqual(row["unknown"], [])


if __name__ == "__main__":
    unittest.main()
