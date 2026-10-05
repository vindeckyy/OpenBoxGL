#!/usr/bin/env python3
"""Request-supplied identifiers must not be able to walk out of their directory.

Three independent code paths took an identifier from an HTTP request and joined
it into a filesystem path without validation:

* ``parity_setup_preview.preview_path`` -- ``previews / f"{preview_id}.json"``,
  read back with ``json.loads``, so a traversal reached ``settings.json`` and
  therefore the stored provider credentials;
* ``parity_launch_doctor._preview_path`` / ``validate_preview`` -- the same
  shape, and the name ``validate_preview`` made it look guarded when it only
  checked existence and expiry;
* ``parity_integrations.import_highscores`` -- ``rom_name`` from library
  metadata reached the *destination* filename, making it a write primitive
  rather than a read.

These tests use the literal malicious inputs, not a description of them, and
assert both halves of the fix: the traversal is refused, and the directory
contents are unchanged.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pkg.platform_compat import (UnsafeIdentifier, contained_path, safe_filename_component, safe_identifier,)

# The literal inputs an attacker would send.
TRAVERSALS = [
    "../../../../home/user/.config/openbox-game-launcher/settings",
    "..\\..\\..\\Windows\\System32\\drivers\\etc\\hosts",
    "/etc/passwd",
    "..",
    ".",
    "a/b",
    "a\\b",
    "",
    "   ",
    "\x00evil",
    "x" * 200,
    ".hidden",
    "previews/../../../etc/passwd",
]


class SafeIdentifierTests(unittest.TestCase):
    def test_rejects_every_traversal_shape(self):
        for value in TRAVERSALS:
            with self.subTest(value=value):
                with self.assertRaises(UnsafeIdentifier):
                    safe_identifier(value, field="preview_id")

    def test_accepts_a_minted_id(self):
        # uuid4().hex, the form the server actually generates.
        self.assertEqual(safe_identifier("3f2504e04f8911ec43fb318f2a"), "3f2504e04f8911ec43fb318f2a")
        self.assertEqual(safe_identifier("preview-2024_v1.2"), "preview-2024_v1.2")

    def test_strips_surrounding_whitespace_then_validates(self):
        self.assertEqual(safe_identifier("  abc123  "), "abc123")

    def test_drive_letter_and_absolute_are_rejected(self):
        for value in ("C:", "C:\\Windows", "\\\\server\\share", "~root"):
            with self.subTest(value=value):
                with self.assertRaises(UnsafeIdentifier):
                    safe_identifier(value)


class ContainedPathTests(unittest.TestCase):
    def test_rejects_escape_via_dotdot(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "previews"
            base.mkdir()
            with self.assertRaises(UnsafeIdentifier):
                contained_path(base, "..", "..", "etc", "passwd")

    def test_allows_a_child(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "previews"
            base.mkdir()
            resolved = contained_path(base, "abc.json")
            self.assertEqual(resolved, base.resolve() / "abc.json")

    def test_rejects_escape_through_a_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            base = root / "previews"
            base.mkdir()
            link = base / "link"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation unavailable on this platform")
            with self.assertRaises(UnsafeIdentifier):
                contained_path(base, "link", "secret.json")


class PreviewPathTests(unittest.TestCase):
    """Both preview chokepoints must refuse a traversal and touch nothing."""

    def _make_previews(self, directory: Path) -> Path:
        previews = directory / "previews"
        previews.mkdir(parents=True, exist_ok=True)
        return previews

    def test_setup_preview_path_refuses_traversal(self):
        from pkg.parity import parity_setup_preview as setup_preview

        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            self._make_previews(data_dir)
            for value in TRAVERSALS:
                with self.subTest(value=value):
                    with self.assertRaises(UnsafeIdentifier):
                        setup_preview.preview_path(value, data_dir)

    def test_launch_doctor_validate_preview_refuses_traversal(self):
        from pkg.parity import parity_launch_doctor as doctor

        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            previews = self._make_previews(data_dir)
            # A real, readable JSON file *outside* previews: exactly what the
            # traversal used to reach.
            (data_dir / "settings.json").write_text(
                json.dumps({"screenscraper_token": "s3cret"}), encoding="utf-8"
            )
            for value in ("../settings", "../../../etc/passwd"):
                with self.subTest(value=value):
                    with self.assertRaises(Exception) as caught:
                        doctor.validate_preview(value, data_dir)
                    self.assertNotIsInstance(
                        caught.exception,
                        AssertionError,
                        "validate_preview must raise, not pass a traversal through",
                    )
            # And the legitimate id still works.
            good = previews / "abc123.json"
            good.write_text(json.dumps({"ok": True, "expires_at": ""}), encoding="utf-8")
            payload = doctor.validate_preview("abc123", data_dir)
            self.assertTrue(payload.get("ok"))

    def test_traversal_read_never_returns_another_directorys_json(self):
        """The concrete exfiltration: read settings.json as a preview."""
        from pkg.parity import parity_setup_preview as setup_preview

        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            self._make_previews(data_dir)
            (data_dir / "settings.json").write_text(
                json.dumps({"screenscraper_token": "s3cret"}), encoding="utf-8"
            )
            leaked = None
            for value in ("../settings", "..%2Fsettings", "../../settings"):
                try:
                    candidate = setup_preview.preview_path(value, data_dir)
                    leaked = candidate.read_text(encoding="utf-8")
                except (UnsafeIdentifier, OSError):
                    continue
            self.assertIsNone(leaked, "a preview read returned a file outside previews/")
            self.assertEqual(len(list((data_dir / "previews").iterdir())), 0)


class SafeFilenameComponentTests(unittest.TestCase):
    """Filename validation is deliberately looser than identifier validation.

    Real ROM names contain spaces, parentheses and ampersands -- ``Game (Disc
    1)``, ``Sonic & Knuckles`` -- so an identifier-grade character class would
    reject the very library this protects. Only things that can move a *write*
    are refused.
    """

    def test_accepts_real_rom_names(self):
        for name in (
            "Game (Disc 1)",
            "Sonic & Knuckles",
            "PokÃ©mon Red",
            "Mr. Do!",
            "Legend of Zelda, The",
            "F-Zero",
            "007 - The World is Not Enough",
        ):
            with self.subTest(name=name):
                self.assertEqual(safe_filename_component(name, field="rom_name"), name)

    def test_rejects_anything_that_moves_the_write(self):
        for name in (
            "../../../.config/openbox-game-launcher/evil",
            "..\\..\\evil",
            "/abs",
            "C:\\Windows\\evil",
            "..",
            ".",
            "sub/dir",
            "sub\\dir",
            "",
            "   ",
            "trailing.",
            ".leading",
            "nul",
            "COM1",
            "bad\x00name",
            "bad\nname",
            "x" * 300,
        ):
            with self.subTest(name=name):
                with self.assertRaises(UnsafeIdentifier):
                    safe_filename_component(name, field="rom_name")


class HighScoreRestorePathTests(unittest.TestCase):
    """rom_name reached the destination filename -- a write primitive."""

    def _bundle(self, directory: Path) -> tuple[Path, Path]:
        bundle = directory / "bundle"
        bundle.mkdir(parents=True, exist_ok=True)
        payload = bundle / "highscores.dat"
        payload.write_bytes(b"scores")
        return bundle, payload

    def test_crafted_rom_name_cannot_write_outside_the_highscore_directory(self):
        from pkg.parity import parity_integrations as integrations

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle, payload = self._bundle(root)
            home = root / "home"
            home.mkdir()
            integrations.mame_highscore_path(home)
            before = sorted(p.name for p in root.rglob("*"))

            for crafted in (
                "../../../../evil",
                "..\\..\\evil",
                "/tmp/evil",
            ):
                game = {"rom_name": crafted, "path": "/games/Game.zip"}
                with self.subTest(rom_name=crafted):
                    with self.assertRaises(UnsafeIdentifier):
                        integrations.import_highscores(game, bundle, home=home)

            after = sorted(p.name for p in root.rglob("*"))
            self.assertEqual(before, after, "a refused restore still created or removed a file")

    def test_a_legitimate_rom_name_still_restores(self):
        from pkg.parity import parity_integrations as integrations

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle, payload = self._bundle(root)
            home = root / "home"
            home.mkdir()
            game = {"rom_name": "Game (Disc 1)", "path": "/games/Game (Disc 1).zip"}
            restored = integrations.import_highscores(game, bundle, home=home)
            self.assertEqual(len(restored), 1)
            target = Path(restored[0])
            self.assertTrue(target.is_file())
            self.assertEqual(target.name, "Game (Disc 1)-highscores.dat")
            self.assertEqual(
                target.parent.resolve(),
                Path(integrations.mame_highscore_path(home)).resolve(),
                "the restore landed outside the high-score directory",
            )


if __name__ == "__main__":
    unittest.main()
