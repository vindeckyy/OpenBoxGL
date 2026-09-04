#!/usr/bin/env python3
"""Tests for the central _deps registry (ADR 0009)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.state._deps import register, get, registered_names  # noqa: E402


class DepsRegistryTest(unittest.TestCase):
    def test_get_returns_default_when_unregistered(self):
        self.assertIsNone(get("nonexistent_name"))
        self.assertEqual(get("nonexistent_name", "fallback"), "fallback")

    def test_register_and_get_round_trip(self):
        sentinel = object()
        register("test_round_trip", sentinel)
        self.assertIs(get("test_round_trip"), sentinel)

    def test_duplicate_register_overwrites(self):
        first = object()
        second = object()
        register("test_overwrite", first)
        register("test_overwrite", second)
        self.assertIs(get("test_overwrite"), second)

    def test_registered_names_includes_registered(self):
        register("test_listed", 42)
        names = registered_names()
        self.assertIn("test_listed", names)


if __name__ == "__main__":
    unittest.main()
