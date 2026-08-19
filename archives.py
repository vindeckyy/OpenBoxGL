"""Safe, cached extraction for compressed game files."""

import hashlib
import os
import selectors
import stat
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path


MAX_ARCHIVE_MEMBERS = 25_000
MAX_ARCHIVE_MEMBER_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 8 * 1024 * 1024 * 1024
MAX_ARCHIVE_LISTING_BYTES = 16 * 1024 * 1024


def _snapshot_archive(archive, directory):
    """Copy one securely opened archive to a private temporary pathname.

    7z has to receive a pathname, so validating the source and then invoking
    7z on the original pathname leaves a replacement race.  The extractor
    only ever sees this owner-only snapshot, whose descriptor was opened with
    ``O_NOFOLLOW``.
    """
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(archive, flags)
    snapshot_fd = -1
    snapshot_name = None
    source = None
    output = None
    try:
        source_info = os.fstat(source_fd)
        if not stat.S_ISREG(source_info.st_mode):
            raise ValueError("Archive source must be a regular file.")
        if source_info.st_size > MAX_ARCHIVE_TOTAL_BYTES:
            raise ValueError("Archive source is too large.")
        snapshot_fd, snapshot_name = tempfile.mkstemp(
            prefix=".openbox-archive-",
            suffix=Path(archive).suffix,
            dir=directory,
        )
        source = os.fdopen(source_fd, "rb")
        source_fd = -1
        output = os.fdopen(snapshot_fd, "wb")
        snapshot_fd = -1
        copied = 0
        with source, output:
            while True:
                chunk = source.read(min(1024 * 1024, MAX_ARCHIVE_TOTAL_BYTES + 1 - copied))
                if not chunk:
                    break
                copied += len(chunk)
                if copied > MAX_ARCHIVE_TOTAL_BYTES:
                    raise ValueError("Archive source is too large.")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        return Path(snapshot_name)
    except Exception:
        if source is not None:
            source.close()
        if output is not None:
            output.close()
        if source_fd >= 0:
            os.close(source_fd)
        if snapshot_fd >= 0:
            os.close(snapshot_fd)
        if snapshot_name:
            Path(snapshot_name).unlink(missing_ok=True)
        raise


def _validate_extracted_tree(destination):
    """Reject links and special files before an extracted tree is promoted."""
    root = Path(destination).resolve()
    for path in Path(destination).rglob("*"):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"Archive links are not supported: {path.name}")
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError(f"Archive special files are not supported: {path.name}")
        resolved = path.resolve(strict=False)
        if resolved != root and root not in resolved.parents:
            raise ValueError(f"Archive extraction escaped its destination: {path.name}")


def extraction_dir(archive, cache_root):
    stat = archive.stat()
    digest = hashlib.sha256(f"{archive.resolve()}:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()[:20]
    return cache_root / digest


def safe_zip_extract(
    archive,
    destination,
    *,
    max_members=MAX_ARCHIVE_MEMBERS,
    max_member_bytes=MAX_ARCHIVE_MEMBER_BYTES,
    max_total_bytes=MAX_ARCHIVE_TOTAL_BYTES,
):
    destination = Path(destination)
    if destination.is_symlink() or any(parent.is_symlink() for parent in destination.parents):
        raise ValueError("Archive destination contains a symlink.")
    root = destination.resolve()
    with zipfile.ZipFile(archive) as package:
        infos = package.infolist()
        if len(infos) > max_members:
            raise ValueError("Archive contains too many entries.")
        total_bytes = 0
        names = set()
        for info in infos:
            name = info.filename.replace("\\", "/")
            relative = Path(name)
            if not name or "\x00" in name or relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"Unsafe archive path: {info.filename}")
            normalized = "/".join(part for part in relative.parts if part not in {"."})
            if normalized in names:
                raise ValueError(f"Archive contains a duplicate entry: {info.filename}")
            names.add(normalized)
            if info.file_size > max_member_bytes:
                raise ValueError("Archive member is too large.")
            total_bytes += info.file_size
            if total_bytes > max_total_bytes:
                raise ValueError("Archive expands beyond the allowed size.")
            target = (destination / relative).resolve(strict=False)
            if root != target and root not in target.parents:
                raise ValueError(f"Unsafe archive path: {info.filename}")
            mode = (info.external_attr >> 16) & 0o170000
            if mode in {0o120000, 0o060000}:
                raise ValueError(f"Archive links are not supported: {info.filename}")
        destination.mkdir(parents=True, exist_ok=True)
        for info in infos:
            relative = Path(info.filename.replace("\\", "/"))
            target = destination / relative
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if any(parent.is_symlink() for parent in [target, *target.parents] if parent != root and parent.exists()):
                raise ValueError(f"Archive destination contains a symlink: {info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with package.open(info) as source, target.open("xb") as output:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)


def _read_7z_listing(extractor, archive):
    """Run 7z -slt and stream its listing back under strict bounds."""
    process = subprocess.Popen(
        [extractor, "l", "-slt", str(archive)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    output = bytearray()
    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + 60
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, 60)
            events = selector.select(remaining)
            if not events:
                raise subprocess.TimeoutExpired(process.args, 60)
            chunk = process.stdout.read1(64 * 1024)
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > MAX_ARCHIVE_LISTING_BYTES:
                raise ValueError("Archive listing is too large.")
        return_code = process.wait(timeout=60)
    except Exception:
        if process.poll() is None:
            process.kill()
        process.wait()
        raise
    finally:
        selector.close()
    if return_code != 0:
        raise ValueError("7z could not inspect the archive.")
    return bytes(output).decode("utf-8", errors="replace")


def _parse_7z_records(text):
    """Split 7z -slt output into one dict per blank-line-separated record."""
    records = []
    record = {}
    for line in text.splitlines():
        if not line.strip():
            if record:
                records.append(record)
                record = {}
            continue
        key, separator, value = line.partition(" = ")
        if separator:
            record[key] = value
    if record:
        records.append(record)
    return records


def _validate_7z_member_record(record):
    """Validate one member record; return its size or None to skip it."""
    # The listing starts with an archive-level record that has
    # ``Physical Size`` but no member ``Size``.  Only member records are
    # subject to path, link, and expansion checks.
    current_path = record.get("Path", "")
    if "Size" not in record or not current_path:
        return None
    candidate = Path(current_path.replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"Unsafe archive path: {current_path}")
    attributes = record.get("Attributes", "").casefold()
    attribute_tokens = attributes.split()
    link_fields = {"Symbolic Link", "Hard Link", "Reparse Point"}
    if any(field in record for field in link_fields) or any(
        token.startswith("l") for token in attribute_tokens
    ):
        raise ValueError(f"Archive links are not supported: {current_path}")
    try:
        current_size = int(record.get("Size", "0").strip())
    except ValueError:
        raise ValueError("Archive member size is invalid.") from None
    if current_size < 0 or current_size > MAX_ARCHIVE_MEMBER_BYTES:
        raise ValueError("Archive member is too large.")
    return current_size


def validate_7z_paths(extractor, archive):
    records = _parse_7z_records(_read_7z_listing(extractor, archive))
    members = 0
    total_bytes = 0
    for record in records:
        current_size = _validate_7z_member_record(record)
        if current_size is None:
            continue
        members += 1
        total_bytes += current_size
        if members > MAX_ARCHIVE_MEMBERS or total_bytes > MAX_ARCHIVE_TOTAL_BYTES:
            raise ValueError("Archive expands beyond the allowed size.")


def choose_game_file(destination, member=""):
    if member:
        raw = destination / member
        if raw.is_symlink():
            raise ValueError("Archive member is a symlink.")
        selected = raw.resolve()
        if destination.resolve() not in selected.parents or not selected.is_file():
            raise FileNotFoundError(f"Archive member not found: {member}")
        return selected
    ignored = {".txt", ".nfo", ".diz", ".jpg", ".jpeg", ".png", ".gif", ".pdf"}
    files = [path for path in destination.rglob("*") if not path.is_symlink() and path.is_file() and path.name != ".complete" and path.suffix.lower() not in ignored]
    if not files:
        raise FileNotFoundError("No launchable file was found in the archive.")
    return max(files, key=lambda path: path.stat().st_size)


def extract_game(archive_path, cache_root, member=""):
    archive = Path(archive_path)
    if archive.is_symlink():
        raise ValueError("Archive source is a symlink.")
    destination = extraction_dir(archive, Path(cache_root))
    complete = destination / ".complete"
    if destination.is_symlink():
        raise ValueError("Archive cache destination is a symlink.")
    if not complete.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.extracting-", dir=destination.parent))
        snapshot = None
        try:
            if archive.suffix.lower() == ".zip":
                flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                source_fd = os.open(archive, flags)
                try:
                    source_info = os.fstat(source_fd)
                    if not stat.S_ISREG(source_info.st_mode):
                        raise ValueError("Archive source must be a regular file.")
                    if source_info.st_size > MAX_ARCHIVE_TOTAL_BYTES:
                        raise ValueError("Archive source is too large.")
                    with os.fdopen(source_fd, "rb") as source_file:
                        source_fd = -1
                        safe_zip_extract(source_file, staging)
                finally:
                    if source_fd >= 0:
                        os.close(source_fd)
            else:
                snapshot = _snapshot_archive(archive, destination.parent)
                extractor = shutil.which("7z") or shutil.which("7zz")
                if not extractor:
                    raise FileNotFoundError("7z or 7zz is required to extract this archive.")
                validate_7z_paths(extractor, snapshot)
                subprocess.run(
                    [extractor, "x", "-y", "-snl-", "-snh-", f"-o{staging}", str(snapshot)],
                    check=True, capture_output=True, timeout=300,
                )
            _validate_extracted_tree(staging)
            (staging / ".complete").touch()
            if destination.exists():
                shutil.rmtree(destination)
            staging.replace(destination)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        finally:
            if snapshot is not None:
                snapshot.unlink(missing_ok=True)
    return choose_game_file(destination, member)
