#!/usr/bin/env python3
"""P2-4: the auto-import tick must not rewrite state when nothing changed.

The 10s loop used to deep-copy, rescan, and rewrite the library even for an
unchanged watch tree.  It now fingerprints each tree (directory mtimes and
entry counts), imports only on a fingerprint change, and backs off while idle.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Import-time DATA binding resolves the data dir once per process.
_DATA_DIR = tempfile.mkdtemp(prefix="openbox-auto-import-")
os.environ["OPENBOX_DATA_DIR"] = _DATA_DIR

from pkg.state import imports as auto_import  # noqa: E402


class WatchFingerprintTests(unittest.TestCase):
    def test_fingerprint_changes_when_a_file_appears(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "roms"
            folder.mkdir()
            first = auto_import.watch_folder_fingerprint(folder)
            (folder / "game.rom").write_text("rom")
            second = auto_import.watch_folder_fingerprint(folder)
            self.assertNotEqual(first, second)
            self.assertEqual(second, auto_import.watch_folder_fingerprint(folder))


class AutoImportPassTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.state = {"games": [], "settings": {"watch_folders": []}, "library_sync": {}}

    def test_missing_folder_fingerprint_is_stable_after_first_pass(self):
        self.state["settings"]["watch_folders"] = ["/no/such/folder"]
        fingerprints = {}
        self.assertTrue(self._run_pass(fingerprints), "first pass must attempt the missing folder")
        self.assertFalse(self._run_pass(fingerprints), "an unchanged missing folder must be skipped")
        self.assertEqual(len(self.calls), 0)

    def _run_pass(self, fingerprints):
        def fake_load():
            return self.state

        def fake_update(mutator):
            self.calls.append("update_state")
            mutator(self.state)
            return self.state

        with mock.patch.object(auto_import, "load_state", side_effect=fake_load), \
             mock.patch.object(auto_import, "update_state", side_effect=fake_update):
            return auto_import.auto_import_pass(self.state, fingerprints)

    def test_unchanged_folder_never_calls_import_or_update_state(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "roms"
            folder.mkdir()
            self.state["settings"]["watch_folders"] = [str(folder)]
            fingerprints = {}

            self.assertTrue(self._run_pass(fingerprints), "first pass must scan a new folder")
            self.assertEqual(len(self.calls), 0, "an empty folder has nothing to import")

            for _ in range(3):
                self.assertFalse(self._run_pass(fingerprints))
            self.assertEqual(len(self.calls), 0, "unchanged passes must not open a transaction")

            (folder / "new.rom").write_text("rom")
            self.assertTrue(self._run_pass(fingerprints), "a new file must trigger an import")
            self.assertEqual(len(self.calls), 1, "the new file must produce exactly one transaction")

            self.assertFalse(self._run_pass(fingerprints))
            self.assertEqual(len(self.calls), 1)

    def test_backoff_constants_bound_the_idle_delay(self):
        self.assertGreaterEqual(auto_import.BASE_IDLE_DELAY, 1)
        self.assertGreater(auto_import.MAX_IDLE_DELAY, auto_import.BASE_IDLE_DELAY)
        self.assertLessEqual(auto_import.MAX_IDLE_DELAY, 300)


if __name__ == "__main__":
    unittest.main()
