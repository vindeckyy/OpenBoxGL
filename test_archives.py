import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import archives
from archives import extract_game, safe_zip_extract, validate_7z_paths


def _fake_7z_script(root, *members):
    """An executable stand-in for 7z that prints a -slt listing.

    Real 7z separates records with blank lines; only the final trailing
    separator is omitted, which is the case the flush must handle.
    """
    records = []
    for path, size in members:
        records.append(f"Path = {path}\nSize = {size}")
    listing = "\n\n".join(records)
    script = root / "fake-7z"
    script.write_text(f"#!/bin/sh\nprintf '%b' {listing!r}\n")
    script.chmod(0o755)
    return str(script)


def test():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        archive = root / "game.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("readme.txt", "notes")
            package.writestr("game.rom", b"real game data")
        selected = extract_game(archive, root / "cache")
        assert selected.name == "game.rom"
        unsafe = root / "unsafe.zip"
        with zipfile.ZipFile(unsafe, "w") as package:
            package.writestr("../escape.rom", b"bad")
        try:
            safe_zip_extract(unsafe, root / "unsafe")
        except ValueError:
            pass
        else:
            raise AssertionError("path traversal must be rejected")

        # A listing without a trailing blank separator must still count its
        # final member against the archive safety limits.
        fake = _fake_7z_script(root, ("last.rom", 5))
        with mock.patch.object(archives, "MAX_ARCHIVE_MEMBERS", 1):
            validate_7z_paths(fake, root / "listing.7z")
        # Two members cross MAX_ARCHIVE_MEMBERS=1; the second must trip the
        # limit even though the listing has no trailing blank line.
        fake = _fake_7z_script(root, ("last.rom", 5), ("last2.rom", 5))
        with mock.patch.object(archives, "MAX_ARCHIVE_MEMBERS", 1):
            try:
                validate_7z_paths(fake, root / "listing.7z")
            except ValueError as error:
                assert "beyond" in str(error)
            else:
                raise AssertionError("final members must not dodge the limits")
    print("archive self-test: ok")


if __name__ == "__main__":
    test()
