"""Media manifest for the data folder's ``media/`` tree (plan W3-07, W3-08).

``media/manifest.json`` records each file that a download or capture wrote: its size, mtime,
SHA-256, and the source that produced it. Verification runs on demand. A file whose size and
mtime still match its entry is not hashed again, so a report over a large media folder stays
cheap. ``rebuild`` repairs the manifest: it adds untracked files, refreshes changed ones, and
drops entries whose file is gone.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from backend_io import atomic_write_text

MANIFEST_NAME = "manifest.json"
MANIFEST_FORMAT = "openbox-media-manifest"
MANIFEST_VERSION = 1
UNTRACKED_SOURCE = "untracked"


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _entry(path: Path, source: str) -> dict[str, Any]:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": _digest(path), "source": source}


def _relative(media_root: Path, path: Path) -> str | None:
    try:
        return path.resolve().relative_to(media_root.resolve()).as_posix()
    except ValueError:
        return None


def _tree_files(media_root: Path) -> list[str]:
    found = []
    for directory, _dirs, names in os.walk(media_root):
        for name in names:
            if name == MANIFEST_NAME or name.endswith(".tmp"):
                continue
            path = Path(directory) / name
            if path.is_file() and not path.is_symlink():
                found.append(path.relative_to(media_root).as_posix())
    return sorted(found)


def load(media_root: Path) -> dict[str, dict[str, Any]]:
    """Entries keyed by relative path. A missing or unreadable manifest is an empty one."""
    try:
        payload = json.loads((Path(media_root) / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("format") != MANIFEST_FORMAT:
        return {}
    files = payload.get("files")
    return {str(rel): entry for rel, entry in files.items() if isinstance(entry, dict)} if isinstance(files, dict) else {}


def _save(media_root: Path, entries: dict[str, dict[str, Any]]) -> None:
    media_root.mkdir(parents=True, exist_ok=True)
    payload = {"format": MANIFEST_FORMAT, "version": MANIFEST_VERSION, "files": dict(sorted(entries.items()))}
    atomic_write_text(media_root / MANIFEST_NAME, json.dumps(payload, indent=2, sort_keys=True) + "\n", mode=0o600)


def record(media_root: Path, path: Path, source: str) -> str | None:
    """Add or refresh the entry for a file a download or capture just wrote. Files outside the tree are ignored."""
    media_root = Path(media_root)
    path = Path(path)
    relative = _relative(media_root, path)
    if relative is None or not path.is_file():
        return None
    entries = load(media_root)
    entries[relative] = _entry(path, source)
    _save(media_root, entries)
    return relative


def verify(media_root: Path) -> dict[str, Any]:
    """Compare the manifest with the tree. Hashes only files whose size or mtime moved."""
    media_root = Path(media_root)
    entries = load(media_root)
    missing: list[str] = []
    changed: list[str] = []
    for relative, entry in sorted(entries.items()):
        path = media_root / relative
        if not path.is_file():
            missing.append(relative)
            continue
        stat = path.stat()
        if stat.st_size == entry.get("size") and stat.st_mtime_ns == entry.get("mtime_ns"):
            continue
        if _digest(path) != entry.get("sha256"):
            changed.append(relative)
    untracked = [relative for relative in _tree_files(media_root) if relative not in entries] if media_root.is_dir() else []
    return {"tracked": len(entries), "missing": missing, "changed": changed, "untracked": untracked}


def rebuild(media_root: Path) -> dict[str, int]:
    """Repair the manifest from the tree and return what changed."""
    media_root = Path(media_root)
    if not media_root.is_dir():
        return {"added": 0, "refreshed": 0, "dropped": 0}
    report = verify(media_root)
    entries = load(media_root)
    for relative in report["missing"]:
        entries.pop(relative, None)
    for relative in report["changed"]:
        entries[relative] = _entry(media_root / relative, entries[relative].get("source", UNTRACKED_SOURCE))
    for relative in report["untracked"]:
        entries[relative] = _entry(media_root / relative, UNTRACKED_SOURCE)
    if report["missing"] or report["changed"] or report["untracked"]:
        _save(media_root, entries)
    return {"added": len(report["untracked"]), "refreshed": len(report["changed"]), "dropped": len(report["missing"])}
