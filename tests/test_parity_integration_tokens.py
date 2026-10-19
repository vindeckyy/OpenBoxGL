#!/usr/bin/env python3
"""Integration token validation and masking."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pkg.parity  # noqa: E402,F401
from pkg.parity import parity_integration_tokens as tokens  # noqa: E402


class TokenTests(unittest.TestCase):
    def test_clean_tokens_keeps_known_providers_only(self):
        clean = tokens.clean_tokens({"igdb": " secret-token ", "unknown": "x", "gameyfin": ""})
        self.assertEqual(clean, {"igdb": "secret-token"})

    def test_clean_tokens_rejects_non_object(self):
        with self.assertRaises(ValueError):
            tokens.clean_tokens(["token"])

    def test_validate_provider_and_token(self):
        self.assertEqual(tokens.validate_provider("IGDB"), "igdb")
        with self.assertRaises(ValueError):
            tokens.validate_provider("nope")
        with self.assertRaises(ValueError):
            tokens.validate_token("bad\nvalue")
        with self.assertRaises(ValueError):
            tokens.validate_token("x" * (tokens.MAX_TOKEN_LENGTH + 1))

    def test_masked_never_returns_full_token(self):
        masked = tokens.masked_tokens({"igdb": "abcdefghijkl", "gameyfin": "short"})
        self.assertEqual(masked["igdb"], "abcd…kl")
        self.assertEqual(masked["gameyfin"], "••••")

    def test_public_view_reports_presence_only(self):
        view = tokens.public_tokens({"igdb": "secret"})
        self.assertTrue(view["igdb"])
        self.assertFalse(view["steamgriddb"])
        self.assertNotIn("secret", str(view))


if __name__ == "__main__":
    unittest.main()
