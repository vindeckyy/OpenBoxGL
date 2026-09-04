#!/usr/bin/env python3
"""Regression test for the parity flat-import MetaPathFinder (ADR 0003).

Verifies that `import parity_x` resolves to `pkg.parity.parity_x` via the
finder registered in ``pkg/parity/__init__.py`` after the root shims were
deleted.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pkg.parity  # noqa: F401,E402  # register flat-import finder


class ParityFinderTest(unittest.TestCase):
    def test_flat_import_resolves_to_canonical(self):
        mod = importlib.import_module("parity_picker")
        canonical = importlib.import_module("pkg.parity.parity_picker")
        self.assertIs(mod, canonical)

    def test_unknown_parity_module_returns_none(self):
        spec = importlib.util.find_spec("parity_nonexistent_module")
        self.assertIsNone(spec)

    def test_finder_is_registered(self):
        from pkg.parity import _ParityFlatFinder

        self.assertTrue(any(isinstance(f, _ParityFlatFinder) for f in sys.meta_path))


if __name__ == "__main__":
    unittest.main()
