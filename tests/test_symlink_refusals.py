#!/usr/bin/env python3
"""A path that runs through a user-created symlink is refused at every entry point.

System symlinks that macOS installs (/var, /tmp, /etc) are not user symlinks; that
exception is covered in ``test_platform_compat.py``. These tests use links made here.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pkg.parity  # noqa: E402,F401  (flat-import finder for parity modules)
from handlers.extensions import ExtensionsHandlers  # noqa: E402
from handlers.library import LibraryHandlers  # noqa: E402
from pkg.parity import parity_backup, parity_gameyfin  # noqa: E402
from pkg.state import media_probe  # noqa: E402
import saves  # noqa: E402


@unittest.skipIf(os.name == "nt", "creating symlinks needs Windows privileges")
class SymlinkRefusalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.real = root / "real"
        self.real.mkdir()
        (self.real / "game.bin").write_bytes(b"rom")
        (self.real / "theme.css").write_text("body{}", encoding="utf-8")
        (self.real / "save.dat").write_bytes(b"save")
        (self.real / "cover.png").write_bytes(b"png")
        self.link = root / "link"
        os.symlink(self.real, self.link, target_is_directory=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_save_path_through_a_symlink_is_refused(self):
        with self.assertRaisesRegex(ValueError, "symlinks"):
            saves._reject_symlink_components(self.link / "save.dat")
        saves._reject_symlink_components(self.real / "save.dat")

    def test_a_backup_path_through_a_symlink_is_refused(self):
        with self.assertRaisesRegex(ValueError, "symlinks"):
            parity_backup._reject_symlink_components(self.link / "backup.zip")
        parity_backup._reject_symlink_components(self.real / "backup.zip")

    def test_a_gameyfin_path_through_a_symlink_is_refused(self):
        with self.assertRaisesRegex(parity_gameyfin.GameyfinError, "symlinks"):
            parity_gameyfin._reject_symlink_components(self.link / "install")
        parity_gameyfin._reject_symlink_components(self.real / "install")

    def test_a_gameyfin_path_that_cannot_be_inspected_is_refused_with_its_cause(self):
        with mock.patch.object(parity_gameyfin, "user_symlink_component", side_effect=OSError("denied")):
            with self.assertRaisesRegex(parity_gameyfin.GameyfinError, "Could not inspect"):
                parity_gameyfin._reject_symlink_components(self.real)

    def test_a_media_path_through_a_symlink_is_refused(self):
        with self.assertRaisesRegex(ValueError, "symlinks"):
            media_probe._reject_media_symlink_components(self.link / "cover.png")
        media_probe._reject_media_symlink_components(self.real / "cover.png")

    def test_a_media_path_that_cannot_be_inspected_is_refused_with_its_cause(self):
        with mock.patch.object(media_probe, "user_symlink_component", side_effect=OSError("denied")):
            with self.assertRaisesRegex(ValueError, "Could not inspect media path"):
                media_probe._reject_media_symlink_components(self.real / "cover.png")

    def test_a_theme_css_file_behind_a_symlink_is_refused(self):
        with self.assertRaisesRegex(ValueError, "symlinks"):
            # The refusal happens before the handler reads any instance state.
            ExtensionsHandlers.import_theme(None, {"path": str(self.link / "theme.css")})

    def test_a_game_path_behind_a_symlink_is_refused(self):
        handler = LibraryHandlers.__new__(LibraryHandlers)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            handler.save_game({"game": {"name": "Linked", "path": str(self.link / "game.bin")}})

    def test_a_game_path_that_cannot_be_inspected_is_refused_with_its_cause(self):
        handler = LibraryHandlers.__new__(LibraryHandlers)
        with mock.patch("handlers.library.user_symlink_component", side_effect=OSError("denied")):
            with self.assertRaisesRegex(ValueError, "Could not inspect game path"):
                handler.save_game({"game": {"name": "Plain", "path": str(self.real / "game.bin")}})


if __name__ == "__main__":
    unittest.main()
