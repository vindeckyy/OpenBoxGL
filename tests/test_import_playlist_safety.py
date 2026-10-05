#!/usr/bin/env python3
"""S8 -- a stale sheet parse deleted a user playlist and hid a ROM.

Two defects in `parity_import.import_multi_platform`, both from the 1.15.0
"parse each disc sheet once" optimisation:

1. **Stale cache.** The exclusion pass cached ``sheet_targets[path] = refs``
   for every sheet it read. ``_write_m3u_playlist`` then wrote
   ``<romdir>/<base>.m3u``, and ``_sheet_platform`` looked *that path* up in the
   cache -- getting the pre-write parse. The platform was resolved from what the
   file used to point at.
2. **Clobber.** The write went to the ROM directory under a name derived from
   the disc group, with no check for an existing file. A hand-made or stale
   ``Game.m3u`` next to ``Game (Disc 1).cue`` and ``Game (Disc 2).cue`` was
   silently overwritten. The user's file was gone and the ROM it referenced was
   excluded from the scan, so it ended up in no row at all.

The invariant: **an import must never destroy or rewrite a file it did not
create, and a sheet's platform must be resolved from what the file contains at
the time it is read.**
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pkg.parity import parity_import
from pkg.parity.parity_import import parse_m3u

#: ".iso" deliberately maps to a dead-end platform, exactly as it does in
#: production -- a disc image recommends no emulator.
PLATFORM_MAP = {
    ".cue": "Sega Saturn",
    ".iso": "Disc image",
    ".bin": "Sega Saturn",
    ".chd": "Sega Saturn",
    ".m3u": "Disc image",
    ".zip": "Arcade",
}

USER_PLAYLIST = "Game.iso\n"


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


class UserPlaylistIsNotDestroyedTests(unittest.TestCase):
    """The import must not touch a .m3u it did not create."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.romdir = self.root / "roms"
        self.romdir.mkdir()
        self.generated = self.root / "generated"
        self.generated.mkdir()
        self.patch = patch.object(
            parity_import, "generated_m3u_dir", return_value=self.generated
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def _scenario(self):
        """The exact trigger: a user playlist plus a two-disc set."""
        iso = _write(self.romdir / "Game.iso", "ISO")
        _write(self.romdir / "Game.m3u", USER_PLAYLIST)
        cue1 = _write(self.romdir / "Game (Disc 1).cue", 'FILE "Game (Disc 1).bin" BINARY\n')
        _write(self.romdir / "Game (Disc 1).bin", "DISC1")
        cue2 = _write(self.romdir / "Game (Disc 2).cue", 'FILE "Game (Disc 2).bin" BINARY\n')
        _write(self.romdir / "Game (Disc 2).bin", "DISC2")
        return iso, cue1, cue2

    def _import(self):
        return parity_import.import_multi_platform(
            self.romdir,
            {".cue", ".bin", ".iso", ".m3u"},
            PLATFORM_MAP,
        )

    def test_the_users_playlist_is_byte_identical_after_the_import(self):
        self._scenario()
        self._import()
        self.assertEqual(
            (self.romdir / "Game.m3u").read_text(encoding="utf-8"),
            USER_PLAYLIST,
            "the import overwrote a .m3u it did not create -- the user's file is gone",
        )

    def test_the_users_playlist_is_not_rewritten_into_the_generated_dir_either(self):
        """Skipping must not mean "write the same name somewhere else"."""
        self._scenario()
        self._import()
        self.assertFalse(
            (self.generated / "Game.m3u").exists(),
            "a generated Game.m3u was written even though the user's was preserved",
        )

    def test_the_referenced_rom_is_represented_exactly_once(self):
        """Excluding a referenced file is only safe if its sheet is a row.

        The old flow excluded ``Game.iso`` from the scan (the user's playlist
        referenced it) and *then* replaced that playlist, so the ROM was in no
        row at all. With the playlist preserved, the exclusion is correct: the
        ROM is covered by the playlist's own row rather than imported twice.
        """
        self._scenario()
        rows = self._import()
        paths = [row["path"] for row in rows]
        self.assertIn(
            str(self.romdir / "Game.m3u"), paths,
            f"the preserved playlist is not a row, so the ROM it names is lost: {paths}",
        )
        # And it must not *also* appear standalone, which would double-import it.
        self.assertNotIn(
            str(self.romdir / "Game.iso"), paths,
            f"the referenced ROM was imported both standalone and via its sheet: {paths}",
        )

    def test_the_preserved_playlist_row_describes_what_the_file_actually_holds(self):
        """The row must reflect the file that survived, not a parse of a
        different one. The user's playlist points at a .iso, so "Disc image" is
        the accurate label here -- what must not happen is a label derived from
        the cues the clobbered file used to reference.
        """
        self._scenario()
        rows = self._import()
        playlist_rows = [row for row in rows if row["path"].endswith("Game.m3u")]
        self.assertEqual(len(playlist_rows), 1, rows)
        self.assertEqual(
            [str(item) for item in parse_m3u(self.romdir / "Game.m3u")],
            [str(self.romdir / "Game.iso")],
            "the playlist's contents changed",
        )
        self.assertEqual(playlist_rows[0]["platform"], PLATFORM_MAP[".iso"])

    def test_a_playlist_that_is_only_a_sheet_still_imports_with_its_platform(self):
        """Skipping the write must not lose the row entirely."""
        self._scenario()
        rows = self._import()
        self.assertTrue(rows, "the import produced no rows at all")

    def test_two_imports_are_idempotent(self):
        """A second run must not start rewriting what the first one created."""
        self._scenario()
        first = self._import()
        second = self._import()
        self.assertEqual(
            [row["path"] for row in first],
            [row["path"] for row in second],
            "a repeated import produced a different row set",
        )
        self.assertEqual((self.romdir / "Game.m3u").read_text(encoding="utf-8"), USER_PLAYLIST)


class NoUserPlaylistStillWritesTests(unittest.TestCase):
    """The clobber guard must not disable the feature it protects."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.romdir = self.root / "roms"
        self.romdir.mkdir()
        self.generated = self.root / "generated"
        self.generated.mkdir()
        self.patch = patch.object(parity_import, "generated_m3u_dir", return_value=self.generated)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        _write(self.romdir / "Game (Disc 1).cue", 'FILE "Game (Disc 1).bin" BINARY\n')
        _write(self.romdir / "Game (Disc 1).bin", "DISC1")
        _write(self.romdir / "Game (Disc 2).cue", 'FILE "Game (Disc 2).bin" BINARY\n')
        _write(self.romdir / "Game (Disc 2).bin", "DISC2")

    def test_a_clean_directory_still_gets_a_generated_playlist(self):
        rows = parity_import.import_multi_platform(
            self.romdir, {".cue", ".bin"}, PLATFORM_MAP
        )
        written = self.romdir / "Game.m3u"
        self.assertTrue(
            written.exists() or (self.generated / "Game.m3u").exists(),
            "no playlist was generated for a clean multi-disc set",
        )
        self.assertTrue(rows)
        self.assertEqual(len(rows), 1, "a two-disc set is one game, not two")

    def test_the_generated_playlist_resolves_the_real_platform(self):
        """The cache invalidation: the row's platform must follow the .bin."""
        rows = parity_import.import_multi_platform(
            self.romdir, {".cue", ".bin"}, PLATFORM_MAP
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0]["platform"], "Sega Saturn",
            "the freshly written playlist was resolved from a stale or missing parse",
        )

    def test_write_m3u_false_still_imports_the_first_disc(self):
        rows = parity_import.import_multi_platform(
            self.romdir, {".cue", ".bin"}, PLATFORM_MAP, write_m3u=False
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["path"], str(self.romdir / "Game (Disc 1).cue"))


class WriteM3uPlaylistUnitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.generated = self.root / "generated"
        self.generated.mkdir()
        self.patch = patch.object(parity_import, "generated_m3u_dir", return_value=self.generated)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.disc1 = _write(self.root / "Game (Disc 1).cue", "A")
        self.disc2 = _write(self.root / "Game (Disc 2).cue", "B")
        self.group = [self.disc1, self.disc2]

    def test_it_returns_the_written_path_when_the_name_is_free(self):
        written = parity_import._write_m3u_playlist(self.group, "Game", None)
        self.assertIsNotNone(written)
        self.assertTrue(written.is_file())

    def test_it_returns_none_when_the_name_is_taken_in_every_target(self):
        _write(self.root / "Game.m3u", "mine\n")
        _write(self.generated / "Game.m3u", "also mine\n")
        self.assertIsNone(parity_import._write_m3u_playlist(self.group, "Game", None))

    def test_a_name_taken_in_the_rom_dir_stops_the_write_entirely(self):
        """Falling through to the generated dir would create a duplicate row.

        The scan only reads the ROM folder, so a playlist written to the
        generated directory is never re-read -- it would become the row's path
        while the user's own playlist stayed a second row of the same game.
        """
        _write(self.root / "Game.m3u", "mine\n")
        self.assertIsNone(
            parity_import._write_m3u_playlist(self.group, "Game", None),
            "the write fell through to the generated dir, producing a duplicate row",
        )
        self.assertEqual((self.root / "Game.m3u").read_text(encoding="utf-8"), "mine\n")
        self.assertFalse((self.generated / "Game.m3u").exists())

    def test_a_name_free_in_both_targets_is_written_to_the_rom_dir(self):
        written = parity_import._write_m3u_playlist(self.group, "Game", None)
        self.assertEqual(written, self.root / "Game.m3u")

    def test_an_explicit_m3u_dir_is_also_protected(self):
        chosen = self.root / "chosen"
        chosen.mkdir()
        _write(chosen / "Game.m3u", "mine\n")
        self.assertIsNone(parity_import._write_m3u_playlist(self.group, "Game", chosen))
        self.assertEqual((chosen / "Game.m3u").read_text(encoding="utf-8"), "mine\n")


if __name__ == "__main__":
    unittest.main()
