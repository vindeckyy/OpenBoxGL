import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import archives
from archives import extract_game, safe_zip_extract, validate_7z_paths


def _fake_7z_script(root, *members):
    """An executable stand-in for 7z that prints a -slt listing.

    Real 7z separates records with blank lines; only the final trailing
    separator is omitted, which is the case the flush must handle.
    """
    records = []
    for member in members:
        path, size, *attributes = member
        record = f"Path = {path}\nSize = {size}"
        if attributes:
            record += f"\nAttributes = {attributes[0]}"
        records.append(record)
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

        fake = _fake_7z_script(root, ("link.rom", 5, "A lrwxrwxrwx"))
        try:
            validate_7z_paths(fake, root / "listing.7z")
        except ValueError as error:
            assert "links" in str(error)
        else:
            raise AssertionError("7z symbolic links must be rejected")

        if shutil.which("7z"):
            source = root / "game.7z"
            (root / "game.rom").write_bytes(b"game")
            subprocess.run(
                ["7z", "a", "-t7z", str(source), "game.rom"],
                cwd=root,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            seen = []
            original_validate = archives.validate_7z_paths

            def capture_validate(extractor, archive_path):
                seen.append(Path(archive_path))
                return original_validate(extractor, archive_path)

            with mock.patch.object(archives, "validate_7z_paths", side_effect=capture_validate):
                selected = extract_game(source, root / "cache-7z")
            assert selected.read_bytes() == b"game"
            assert seen and seen[0] != source

            link_source = root / "symlink.7z"
            (root / "link.rom").symlink_to("game.rom")
            subprocess.run(
                ["7z", "a", "-t7z", "-snl", str(link_source), "link.rom"],
                cwd=root,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                listing = subprocess.run(
                    ["7z", "l", str(link_source)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except FileNotFoundError:
                print("  skipped real 7z symlink case (7z not installed)")
            else:
                has_symlink = " l " in listing.stdout or " L " in listing.stdout
                if not has_symlink:
                    print("  skipped real 7z symlink case (archive has no symlink entries)")
                else:
                    try:
                        extract_game(link_source, root / "cache-link")
                    except ValueError as error:
                        assert "links" in str(error)
                    else:
                        raise AssertionError("real 7z symbolic links must be rejected")
    print("archive self-test: ok")


def test_safe_streaming_extraction():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        archive = root / "stream_game.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("manual.txt", "Manual content")
            package.writestr("stream_game.rom", b"ROM payload data 12345")
        
        with mock.patch("archives._snapshot_archive") as mock_snapshot:
            selected = extract_game(archive, root / "cache-stream")
            # Snapshot must NOT be called for .zip extraction (direct safe streaming)
            mock_snapshot.assert_not_called()
        
        assert selected.name == "stream_game.rom"
        assert selected.read_bytes() == b"ROM payload data 12345"
        # Verify no snapshot files exist in cache parent
        snapshots = list((root / "cache-stream").parent.glob(".openbox-archive-*"))
        assert len(snapshots) == 0
    print("archive safe streaming extraction self-test: ok")


def test_zip_stream_fd_and_bounds():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        archive = root / "large_member.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("big.rom", b"1234567890")
        
        # Test max_member_bytes limit
        with open(archive, "rb") as source:
            try:
                safe_zip_extract(source, root / "dest_big", max_member_bytes=5)
                raise AssertionError("Expected member size limit ValueError")
            except ValueError as error:
                assert "large" in str(error)

        # Test max_members limit
        with open(archive, "rb") as source:
            try:
                safe_zip_extract(source, root / "dest_members", max_members=0)
                raise AssertionError("Expected max members limit ValueError")
            except ValueError as error:
                assert "entries" in str(error)

        # Test extract_game non-regular file and oversize bounds check
        from archives import extract_game
        from unittest.mock import patch

        dir_as_file = root / "dir_as_file.zip"
        dir_as_file.mkdir()
        try:
            extract_game(dir_as_file, root / "cache-dir")
            raise AssertionError("Expected regular file check failure")
        except ValueError as error:
            assert "regular file" in str(error)

        with patch("archives.MAX_ARCHIVE_TOTAL_BYTES", 5):
            try:
                extract_game(archive, root / "cache-oversize")
                raise AssertionError("Expected archive too large error")
            except ValueError as error:
                assert "too large" in str(error)

    print("archive zip stream fd and bounds self-test: ok")


if __name__ == "__main__":
    test()
    test_safe_streaming_extraction()
    test_zip_stream_fd_and_bounds()

