"""Per-game save history: listing, verification, restore, and retention (F4).

The archives are the existing per-game ZIP backups written by
``saves.backup_saves`` (``<YYYYmmdd-HHMMSS-ffffff>-<label>.zip`` with a
``manifest.json`` member).  This module adds no new storage: a version exists
only while its archive exists, every listed field comes from the file itself,
and restores still go through ``saves.restore_saves`` so the manifest/root
identity checks stay in force.  Verification reads the archive back — every
member is streamed through a SHA-256 and the ZIP CRC check before it reports
``verified``.
"""

from __future__ import annotations

import hashlib
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from saves import (
    _load_save_manifest,
    _validate_save_archive_entries,
    game_backup_dir,
    list_backups,
)


HISTORY_FORMAT = 1
MAX_HISTORY = 200
MAX_VERIFY_MEMBER_BYTES = 4 * 1024 * 1024 * 1024
VERIFY_CHUNK_BYTES = 1024 * 1024
ARCHIVE_NAME_RE = re.compile(r"^(\d{8}-\d{6}-\d{6})-([A-Za-z0-9._-]{1,64})\.zip$")

SOURCE_MANUAL = "manual"
SOURCE_AUTO = "auto"
SOURCE_PRE_LAUNCH = "pre-launch"
SOURCE_SAFETY = "before-restore"
SOURCE_OTHER = "other"

SOURCE_ORDER = (SOURCE_MANUAL, SOURCE_PRE_LAUNCH, SOURCE_AUTO, SOURCE_SAFETY, SOURCE_OTHER)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def classify_label(label: Any) -> str:
    """Map an archive label to the honest source vocabulary."""
    cleaned = str(label or "").strip().casefold()
    if cleaned in ("", "manual"):
        return SOURCE_MANUAL
    if cleaned.startswith("on-close") or cleaned.startswith("on_close") or cleaned.startswith("auto"):
        return SOURCE_AUTO
    if cleaned.startswith("pre-launch") or cleaned.startswith("pre_launch") or cleaned.startswith("prelaunch"):
        return SOURCE_PRE_LAUNCH
    if cleaned.startswith("before-restore") or cleaned.startswith("before_restore") or cleaned.startswith("safety"):
        return SOURCE_SAFETY
    return SOURCE_OTHER


def parse_archive_name(name: Any) -> dict[str, Any] | None:
    """Parse ``<stamp>-<label>.zip``; None when the name does not match."""
    match = ARCHIVE_NAME_RE.fullmatch(str(name or ""))
    if match is None:
        return None
    try:
        created = datetime.strptime(match.group(1), "%Y%m%d-%H%M%S-%f").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    label = match.group(2)
    return {"label": label, "source": classify_label(label), "created_at": created}


def _archive_file(game: dict[str, Any], backup_root: Any, name: Any) -> Path:
    base_name = Path(str(name or "")).name
    directory = game_backup_dir(game, Path(str(backup_root)).expanduser())
    archive = directory / base_name
    if directory not in archive.parents or archive.is_symlink() or not archive.is_file():
        raise FileNotFoundError("Save backup not found.")
    return archive


def version_entry(path: Path, *, now: Any = None) -> dict[str, Any]:
    """Return one honest version row for an existing archive path."""
    moment = now if isinstance(now, datetime) else _utc_now()
    parsed = parse_archive_name(path.name)
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if parsed is None:
        try:
            created = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            created = moment
        parsed = {"label": path.stem, "source": SOURCE_OTHER, "created_at": created}
    age = max(0, int((moment - parsed["created_at"]).total_seconds()))
    return {
        "name": path.name,
        "size": size,
        "label": parsed["label"],
        "source": parsed["source"],
        "created_at": parsed["created_at"].astimezone(timezone.utc).isoformat(),
        "age_seconds": age,
    }


def save_history(game: dict[str, Any], backup_root: Any, *, now: Any = None) -> list[dict[str, Any]]:
    """List this game's save versions newest first, bounded and detached."""
    moment = now if isinstance(now, datetime) else _utc_now()
    versions = [version_entry(path, now=moment) for path in list_backups(game, backup_root)]
    versions.sort(key=lambda item: (item["created_at"], item["name"]), reverse=True)
    return versions[:MAX_HISTORY]


def latest_version(versions: Any) -> dict[str, Any] | None:
    """Return the newest version row or None for an empty/invalid list."""
    if not isinstance(versions, list) or not versions:
        return None
    return max(versions, key=lambda item: (str(item.get("created_at") or ""), str(item.get("name") or "")))


def retention_plan(versions: Any, keep: Any) -> list[str]:
    """Return the archive names to remove, oldest first, keeping the newest ``keep``.

    ``keep <= 0`` means keep everything, matching the existing backup-limit
    semantics (``saves.enforce_backup_limit``).
    """
    try:
        limit = int(keep)
    except (TypeError, ValueError):
        limit = 0
    if limit <= 0 or not isinstance(versions, list):
        return []
    ordered = sorted(
        (item for item in versions if isinstance(item, dict) and item.get("name")),
        key=lambda item: (str(item.get("created_at") or ""), str(item.get("name") or "")),
        reverse=True,
    )
    return [str(item["name"]) for item in ordered[limit:]][::-1]


def apply_retention(game: dict[str, Any], backup_root: Any, keep: Any) -> dict[str, Any]:
    """Delete archives beyond the retention limit; report what was removed."""
    versions = save_history(game, backup_root)
    names = retention_plan(versions, keep)
    removed = 0
    for name in names:
        try:
            archive = _archive_file(game, backup_root, name)
            archive.unlink(missing_ok=True)
            removed += 1
        except (FileNotFoundError, OSError):
            continue
    return {"removed": removed, "kept": max(0, len(versions) - removed)}


def verify_version(game: dict[str, Any], backup_root: Any, name: Any) -> dict[str, Any]:
    """Read every member back (CRC + SHA-256) and report the verified manifest."""
    archive = _archive_file(game, backup_root, name)
    try:
        with zipfile.ZipFile(archive) as package:
            infos = _validate_save_archive_entries(package)
            manifest = _load_save_manifest(package)
            digest = hashlib.sha256()
            total = 0
            members = 0
            for info in infos:
                if info.is_dir():
                    continue
                if info.file_size > MAX_VERIFY_MEMBER_BYTES:
                    raise ValueError("Save backup member is too large to verify.")
                with package.open(info) as source:
                    while True:
                        chunk = source.read(VERIFY_CHUNK_BYTES)
                        if not chunk:
                            break
                        digest.update(chunk)
                        total += len(chunk)
                members += 1
    except zipfile.BadZipFile as error:
        raise ValueError("Save backup is not a readable ZIP archive.") from error
    roots = manifest.get("roots") if isinstance(manifest, dict) else None
    return {
        "name": archive.name,
        "format": HISTORY_FORMAT,
        "verified": True,
        "members": members,
        "bytes": total,
        "sha256": digest.hexdigest(),
        "game": (manifest.get("game") if isinstance(manifest, dict) else "") or "",
        "roots": len(roots) if isinstance(roots, list) else 0,
    }


def test_restore(game: dict[str, Any], backup_root: Any, name: Any) -> dict[str, Any]:
    """Restore one backup into a temp dir and verify it; live saves untouched.

    Every archive member under ``roots/`` is extracted into a throwaway
    directory, its length compared with the ZIP metadata and its bytes folded
    into one SHA-256. Any CRC failure surfaces as a ``ValueError``. The
    temporary tree is removed before returning, so the drill never writes to a
    configured save path.
    """
    archive = _archive_file(game, backup_root, name)
    files = 0
    members = 0
    total = 0
    digest = hashlib.sha256()
    failures: list[str] = []
    try:
        with zipfile.ZipFile(archive) as package:
            infos = _validate_save_archive_entries(package)
            manifest = _load_save_manifest(package)
            with tempfile.TemporaryDirectory(prefix="openbox-save-drill-") as temp:
                root = Path(temp)
                for info in infos:
                    if info.is_dir():
                        continue
                    member = info.filename.replace("\\", "/")
                    if not member.startswith("roots/"):
                        continue
                    members += 1
                    if info.file_size > MAX_VERIFY_MEMBER_BYTES:
                        failures.append(member)
                        continue
                    target = root / member
                    target.parent.mkdir(parents=True, exist_ok=True)
                    extracted = 0
                    with package.open(info) as source, target.open("wb") as destination:
                        while True:
                            chunk = source.read(VERIFY_CHUNK_BYTES)
                            if not chunk:
                                break
                            destination.write(chunk)
                            digest.update(chunk)
                            extracted += len(chunk)
                            total += len(chunk)
                    if extracted != info.file_size:
                        failures.append(member)
                    files += 1
    except zipfile.BadZipFile as error:
        raise ValueError("Save backup is not a readable ZIP archive.") from error
    roots = manifest.get("roots") if isinstance(manifest, dict) else None
    return {
        "name": archive.name,
        "format": HISTORY_FORMAT,
        "ok": not failures and files == members,
        "verified": not failures,
        "files": files,
        "members": members,
        "bytes": total,
        "sha256": digest.hexdigest(),
        "mismatches": failures[:20],
        "game": (manifest.get("game") if isinstance(manifest, dict) else "") or "",
        "roots": len(roots) if isinstance(roots, list) else 0,
    }


def history_response(game: dict[str, Any], backup_root: Any, *, keep: Any = 0, now: Any = None) -> dict[str, Any]:
    """Build the HTTP payload: versions, counts, source totals, and the plan."""
    versions = save_history(game, backup_root, now=now)
    sources: dict[str, int] = {}
    for version in versions:
        sources[version["source"]] = sources.get(version["source"], 0) + 1
    return {
        "format": HISTORY_FORMAT,
        "versions": versions,
        "count": len(versions),
        "total_bytes": sum(int(item.get("size") or 0) for item in versions),
        "sources": sources,
        "latest": latest_version(versions),
        "retention_plan": retention_plan(versions, keep),
    }


__all__ = [
    "HISTORY_FORMAT",
    "MAX_HISTORY",
    "SOURCE_AUTO",
    "SOURCE_MANUAL",
    "SOURCE_ORDER",
    "SOURCE_OTHER",
    "SOURCE_PRE_LAUNCH",
    "SOURCE_SAFETY",
    "apply_retention",
    "classify_label",
    "history_response",
    "latest_version",
    "parse_archive_name",
    "retention_plan",
    "save_history",
    "test_restore",
    "verify_version",
    "version_entry",
]
