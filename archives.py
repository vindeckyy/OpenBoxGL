"""Safe, cached extraction for compressed game files."""

import hashlib
import selectors
import shutil
import subprocess
import time
import zipfile
from pathlib import Path


MAX_ARCHIVE_MEMBERS = 25_000
MAX_ARCHIVE_MEMBER_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 8 * 1024 * 1024 * 1024
MAX_ARCHIVE_LISTING_BYTES = 16 * 1024 * 1024


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


def validate_7z_paths(extractor, archive):
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
    members = 0
    total_bytes = 0
    current_path = ""
    current_size = 0
    for line in bytes(output).decode("utf-8", errors="replace").splitlines():
        if line.startswith("Path = "):
            current_path = line[7:]
            candidate = Path(current_path.replace("\\", "/"))
            if candidate.is_absolute() or ".." in candidate.parts:
                raise ValueError(f"Unsafe archive path: {current_path}")
        elif line.startswith("Size = "):
            try:
                current_size = int(line[7:].strip())
            except ValueError:
                current_size = 0
            if current_size > MAX_ARCHIVE_MEMBER_BYTES:
                raise ValueError("Archive member is too large.")
        elif not line.strip() and current_path:
            members += 1
            total_bytes += current_size
            if members > MAX_ARCHIVE_MEMBERS or total_bytes > MAX_ARCHIVE_TOTAL_BYTES:
                raise ValueError("Archive expands beyond the allowed size.")
            current_path = ""
            current_size = 0


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
        staging = destination.with_name(f".{destination.name}.extracting")
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)
        try:
            if archive.suffix.lower() == ".zip":
                safe_zip_extract(archive, staging)
            else:
                extractor = shutil.which("7z") or shutil.which("7zz")
                if not extractor:
                    raise FileNotFoundError("7z or 7zz is required to extract this archive.")
                validate_7z_paths(extractor, archive)
                subprocess.run(
                    [extractor, "x", "-y", f"-o{staging}", str(archive)],
                    check=True, capture_output=True, timeout=300,
                )
            (staging / ".complete").touch()
            if destination.exists():
                shutil.rmtree(destination)
            staging.replace(destination)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    return choose_game_file(destination, member)
