#!/usr/bin/env python3
"""P1-23: BIOS hints follow the current HOME/data dir instead of the import-time one."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pkg.parity  # noqa: F401,E402  # register flat-import finder
from pkg.parity import parity_import


class BiosHintsTests(unittest.TestCase):
    def test_hints_follow_home_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "home-one"
            second = Path(directory) / "home-two"
            first.mkdir()
            second.mkdir()
            with mock.patch.dict(os.environ, {"HOME": str(first)}):
                parity_import.reset_bios_hints()
                hints = parity_import.bios_hints()
                self.assertTrue(str(hints["DuckStation"][0][1]).startswith(str(first)))
            with mock.patch.dict(os.environ, {"HOME": str(second)}):
                hints = parity_import.bios_hints()
                self.assertFalse(str(first) in str(hints["DuckStation"][0][1]))
                self.assertTrue(str(hints["DuckStation"][0][1]).startswith(str(second)))

    def test_detect_dependencies_uses_current_home(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            for relative in (
                ".local/share/duckstation/bios",
                ".var/app/org.duckstation.DuckStation/data/duckstation/bios",
            ):
                bios = home / relative
                bios.mkdir(parents=True)
                (bios / "scph1001.bin").write_text("bios", encoding="utf-8")
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                parity_import.reset_bios_hints()
                result = parity_import.detect_dependencies("DuckStation")
            self.assertEqual(result["missing"], [])
            self.assertTrue(all(item["found"] for item in result["required"]))

    def test_data_dir_change_refreshes_the_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"OPENBOX_DATA_DIR": str(Path(directory) / "one")}):
                parity_import.reset_bios_hints()
                parity_import.bios_hints()
                first_key = parity_import._BIOS_HINTS_CACHE_KEY
            with mock.patch.dict(os.environ, {"OPENBOX_DATA_DIR": str(Path(directory) / "two")}):
                parity_import.bios_hints()
                second_key = parity_import._BIOS_HINTS_CACHE_KEY
            self.assertIsNotNone(first_key)
            self.assertNotEqual(first_key, second_key)

    def test_reset_forces_a_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir()
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                parity_import.reset_bios_hints()
                self.assertIsNone(parity_import._BIOS_HINTS_CACHE_KEY)
                parity_import.bios_hints()
                self.assertEqual(parity_import._BIOS_HINTS_CACHE_KEY, parity_import._bios_hints_key())


if __name__ == "__main__":
    unittest.main()
