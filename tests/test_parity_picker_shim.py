"""Compatibility shim test for parity_picker.py."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import parity_picker  # noqa: E402


class ParityPickerShimTest(unittest.TestCase):
    def test_shim_exports_same_module(self):
        import pkg.parity.parity_picker as real
        self.assertIs(parity_picker, real)


if __name__ == "__main__":
    unittest.main(verbosity=2)
