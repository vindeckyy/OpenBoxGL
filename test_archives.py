#!/usr/bin/env python3
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from archives import extract_game, safe_zip_extract


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
    print("archive self-test: ok")


if __name__ == "__main__":
    test()
