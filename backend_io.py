"""Shared bounded network and filesystem helpers used by backend operations."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen


DEFAULT_MAX_DOWNLOAD = 64 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024


def contained_path(path: Path, roots, *, must_exist=False) -> Path:
    candidate = Path(path).expanduser().resolve(strict=False)
    allowed = [Path(root).expanduser().resolve(strict=False) for root in roots]
    if not any(candidate == root or root in candidate.parents for root in allowed):
        raise ValueError(f"Path is outside an approved OpenBox directory: {candidate}")
    if must_exist and not candidate.exists():
        raise FileNotFoundError(str(candidate))
    return candidate


def safe_media_path(path: Path, data_root: Path) -> Path:
    return contained_path(path, [Path(data_root)])


def remove_file_if_safe(path: Path, data_root: Path) -> bool:
    target = safe_media_path(path, data_root)
    if not target.is_file():
        return False
    target.unlink()
    return True


def download_file(
    url: str,
    destination: Path,
    *,
    expected_types=(),
    max_bytes=DEFAULT_MAX_DOWNLOAD,
    timeout=30,
    opener=urlopen,
    headers=None,
    sha256="",
) -> Path:
    request = Request(url, headers={"User-Agent": "OpenBox/1", **(headers or {})})
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    digest = hashlib.sha256()
    try:
        with opener(request, timeout=timeout) as response:
            if response.headers and hasattr(response.headers, "get_content_type"):
                content_type = response.headers.get_content_type()
            else:
                content_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0] if response.headers else ""
            if expected_types and not any(content_type.startswith(item) for item in expected_types):
                raise ValueError(f"The remote server returned an unsupported content type: {content_type or 'unknown'}")
            try:
                declared = int(response.headers.get("Content-Length", "0"))
            except (TypeError, ValueError):
                declared = 0
            if declared > max_bytes:
                raise ValueError("The download is too large.")
            fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
            with os.fdopen(fd, "wb") as output:
                total = 0
                while True:
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("The download is too large.")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        if sha256 and digest.hexdigest().casefold() != str(sha256).casefold():
            raise ValueError("The downloaded file failed checksum verification.")
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
    return destination


def safe_copytree(source: Path, destination: Path) -> None:
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)
