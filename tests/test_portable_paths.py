"""Portable library paths: encode/decode rules for POSIX and Windows, escapes, and roots."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pkg.state import paths  # noqa: E402
from pkg.state.paths import PathTokenError  # noqa: E402


class PosixPathTests(unittest.TestCase):
    def test_paths_under_the_root_are_stored_as_tokens(self):
        self.assertEqual(paths.encode_path("/games/roms/x.iso", "/games", windows=False), "@library/roms/x.iso")
        self.assertEqual(paths.encode_path("/games", "/games", windows=False), "@library")

    def test_sibling_prefixes_are_not_inside_the_root(self):
        self.assertEqual(paths.encode_path("/games2/x.iso", "/games", windows=False), "/games2/x.iso")
        self.assertEqual(paths.classify("/games2/x.iso", "/games", windows=False), "absolute")

    def test_dot_dot_that_leaves_the_root_is_not_tokenized(self):
        self.assertEqual(paths.encode_path("/games/../etc/passwd", "/games", windows=False), "/etc/passwd")

    def test_moving_the_root_resolves_the_same_token(self):
        self.assertEqual(paths.decode_path("@library/roms/x.iso", "/mnt/games", windows=False), "/mnt/games/roms/x.iso")

    def test_no_root_leaves_paths_alone(self):
        self.assertEqual(paths.encode_path("/games/roms/x.iso", "", windows=False), "/games/roms/x.iso")
        with self.assertRaises(PathTokenError) as raised:
            paths.decode_path("@library/roms/x.iso", "", windows=False)
        self.assertEqual(raised.exception.code, "LIBRARY_ROOT_MISSING")

    def test_escape_attempts_are_rejected(self):
        for token in ("@library/../../etc/passwd", "@library//etc/passwd", "@library/a/../../b"):
            with self.subTest(token=token), self.assertRaises(PathTokenError) as raised:
                paths.decode_path(token, "/games", windows=False)
            self.assertEqual(raised.exception.code, "PATH_TOKEN_ESCAPE")

    def test_nul_bytes_are_invalid(self):
        with self.assertRaises(PathTokenError) as raised:
            paths.decode_path("@library/roms\x00x", "/games", windows=False)
        self.assertEqual(raised.exception.code, "PATH_TOKEN_INVALID")

    def test_non_token_look_alikes_are_plain_paths(self):
        self.assertEqual(paths.decode_path("@libraryish/x.iso", "/games", windows=False), "@libraryish/x.iso")

    def test_symlink_free_containment_is_lexical_and_case_sensitive_on_posix(self):
        self.assertFalse(paths.is_within("/Games/x", "/games", windows=False, case_insensitive=False))
        self.assertTrue(paths.is_within("/Games/x", "/games", windows=False, case_insensitive=True))


class WindowsPathTests(unittest.TestCase):
    def test_drive_paths_are_stored_with_forward_slashes(self):
        self.assertEqual(paths.encode_path("D:\\Roms\\x.iso", "D:\\Roms", windows=True), "@library/x.iso")
        self.assertEqual(paths.encode_path("D:\\Other\\x.iso", "D:\\Roms", windows=True), "D:/Other/x.iso")

    def test_drive_letter_and_folder_case_fold_for_containment_but_not_for_storage(self):
        token = paths.encode_path("d:/roms/Game.ISO", "D:/Roms", windows=True, case_insensitive=True)
        self.assertEqual(token, "@library/Game.ISO")
        self.assertEqual(paths.decode_path(token, "D:/Roms", windows=True, case_insensitive=True), "D:\\Roms\\Game.ISO")

    def test_decode_refuses_drive_relative_and_drive_prefixed_tokens(self):
        for token in ("@library/C:foo.iso", "@library/C:/Windows/system32", "@library/\\\\server\\share"):
            with self.subTest(token=token), self.assertRaises(PathTokenError) as raised:
                paths.decode_path(token, "D:\\Roms", windows=True)
            self.assertEqual(raised.exception.code, "PATH_TOKEN_ESCAPE")

    def test_unc_paths_are_never_tokenized_and_unc_roots_are_refused(self):
        self.assertEqual(paths.classify("\\\\server\\share\\x.iso", "D:\\Roms", windows=True), "unc")
        self.assertEqual(paths.encode_path("\\\\server\\share\\x.iso", "D:\\Roms", windows=True), "\\\\server\\share\\x.iso")
        with self.assertRaises(PathTokenError) as raised:
            paths.normalize_root("\\\\server\\share", windows=True)
        self.assertEqual(raised.exception.code, "LIBRARY_ROOT_INVALID")

    def test_windows_escape_via_backslash_dot_dot_is_rejected(self):
        with self.assertRaises(PathTokenError) as raised:
            paths.decode_path("@library\\..\\secret.txt", "D:\\Roms", windows=True)
        self.assertEqual(raised.exception.code, "PATH_TOKEN_ESCAPE")


class UnicodeAndRootTests(unittest.TestCase):
    def test_nfd_and_nfc_spellings_are_the_same_location(self):
        nfc_root = "/games/Pok\u00e9mon"
        nfd_child = "/games/Poke\u0301mon/x.iso"
        self.assertTrue(paths.is_within(nfd_child, nfc_root, windows=False))
        self.assertEqual(paths.encode_path(nfd_child, nfc_root, windows=False), "@library/x.iso")

    def test_stored_spelling_is_preserved_on_round_trip(self):
        stored = "@library/Poke\u0301mon/x.iso"
        resolved = paths.decode_path(stored, "/games", windows=False)
        self.assertEqual(paths.encode_path(resolved, "/games", windows=False), stored)

    def test_roots_are_validated_and_normalized_for_storage(self):
        self.assertEqual(paths.normalize_root("", windows=False), "")
        self.assertEqual(paths.normalize_root("/games/", windows=False), "/games")
        self.assertEqual(paths.normalize_root("C:\\Games\\", windows=True), "C:/Games")
        with self.assertRaises(PathTokenError):
            paths.normalize_root("relative/games", windows=False)


if __name__ == "__main__":
    unittest.main()
