"""Flat parity_* imports are gone: the gate is clean and a flat name no longer resolves."""

import importlib
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class FlatImportTests(unittest.TestCase):
    def test_gate_finds_no_flat_parity_imports(self):
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "scripts" / "check_flat_imports.py")],
            capture_output=True,
            check=False,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_flat_name_does_not_resolve_and_canonical_name_does(self):
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("parity_" + "media")
        self.assertIsNotNone(importlib.import_module("pkg.parity.parity_media"))

    def test_no_meta_path_bridge_is_installed(self):
        import pkg.parity  # noqa: F401

        self.assertFalse(any("ParityFlatFinder" in type(finder).__name__ for finder in sys.meta_path))


if __name__ == "__main__":
    unittest.main()
