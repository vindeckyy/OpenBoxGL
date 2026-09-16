#!/usr/bin/env python3
"""P1-13: save path registration is contained to home and approved roots."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pkg.parity  # noqa: F401,E402  # register flat-import finder
import handlers.data as data_handlers
from handlers.data import DataHandlers, approved_save_path


class ApprovedSavePathTests(unittest.TestCase):
    def test_rejects_etc(self):
        with self.assertRaises(ValueError):
            approved_save_path("/etc")

    def test_rejects_home_itself(self):
        with self.assertRaises(ValueError):
            approved_save_path(str(Path.home()))

    def test_accepts_path_under_home(self):
        target = Path.home() / f".openbox-save-test-{os.getpid()}"
        target.mkdir(exist_ok=True)
        try:
            self.assertEqual(approved_save_path(target), target.resolve())
        finally:
            target.rmdir()

    def test_env_approved_root_allows_temp_dir(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"OPENBOX_SAVE_ROOTS": directory}):
                result = approved_save_path(Path(directory) / "saves")
            self.assertEqual(result, (Path(directory) / "saves").resolve())

    def test_relative_path_rejected(self):
        with self.assertRaises(ValueError):
            approved_save_path("relative/saves")

    def test_env_filesystem_root_rejected(self):
        with mock.patch.dict(os.environ, {"OPENBOX_SAVE_ROOTS": "/"}):
            with self.assertRaises(ValueError):
                approved_save_path("/tmp")

    def test_env_entries_must_be_absolute(self):
        with mock.patch.dict(os.environ, {"OPENBOX_SAVE_ROOTS": "relative/root"}):
            with self.assertRaises(ValueError):
                approved_save_path(str(Path.home() / "saves"))

    def test_env_too_long_rejected(self):
        with mock.patch.dict(os.environ, {"OPENBOX_SAVE_ROOTS": "x" * (16 * 4096 + 1)}):
            with self.assertRaises(ValueError):
                approved_save_path(str(Path.home() / "saves"))

    def test_unresolvable_path_rejected(self):
        with mock.patch.object(Path, "resolve", side_effect=OSError("denied")):
            with self.assertRaises(ValueError):
                approved_save_path("/tmp/saves")

    def test_unresolvable_roots_are_rejected(self):
        original_resolve = Path.resolve
        failing = {Path.home(), Path("/approved"), Path("/tmp/approved")}

        def flaky_resolve(path_self, *args, **kwargs):
            if path_self in failing:
                raise OSError("denied")
            return original_resolve(path_self, *args, **kwargs)

        with mock.patch.object(Path, "resolve", flaky_resolve):
            with self.assertRaises(ValueError):
                approved_save_path("/tmp/outside")
            with mock.patch.dict(os.environ, {"OPENBOX_SAVE_ROOTS": "/approved"}):
                with self.assertRaises(ValueError):
                    approved_save_path("/tmp/outside")



class AddGameSavePathHandlerTests(unittest.TestCase):
    def setUp(self):
        self.handler = DataHandlers()
        self.responses = []
        self.handler.send_json = lambda status, payload: self.responses.append((status, payload))
        self.state = {"games": [{"game_id": "g1", "name": "G", "path": "/bin/true", "save_paths": []}]}

    def _transact(self, mutate):
        mutate(self.state)
        return self.state, None

    def test_rejects_etc_through_handler(self):
        with mock.patch.object(data_handlers, "transact_state", side_effect=self._transact):
            with self.assertRaises(ValueError):
                self.handler.add_game_save_path({"id": 0, "path": "/etc"})

    def test_accepts_home_temp_dir_through_handler(self):
        target = Path.home() / f".openbox-save-test-{os.getpid()}"
        target.mkdir(exist_ok=True)
        try:
            with mock.patch.object(data_handlers, "transact_state", side_effect=self._transact):
                self.handler.add_game_save_path({"id": 0, "path": str(target)})
            self.assertEqual(self.responses[-1][0], 200)
            self.assertIn(str(target.resolve()), self.state["games"][0]["save_paths"])
        finally:
            target.rmdir()


if __name__ == "__main__":
    unittest.main()
