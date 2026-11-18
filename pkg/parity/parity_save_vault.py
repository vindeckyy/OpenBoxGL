"""Save Vault — indexed, retained, SHA-256-verified save-file snapshots (ADR 0107)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from api_errors import (
    VaultGameMismatch,
    VaultLimitExceeded,
    VaultNoSaves,
    VaultPathEscape,
    VaultSafetySnapshotFailed,
    VaultSnapshotMissing,
)
from backend_io import atomic_write_text, fsync_directory
from pkg.parity.parity_saves import extra_save_candidates
from pkg.platform_compat import user_symlink_component
from saves import SAVE_SYNC_LOCK, _reject_symlink_components, discover_save_paths, file_sha256, save_source_for

LOGGER = logging.getLogger("openbox.save_vault")

VAULT_DIR_NAME = "save-vault"
INDEX_FILE_NAME = "index.json"
SNAPSHOT_FILE_NAME = "snapshot.json"
INDEX_FORMAT = 1
SNAPSHOT_FORMAT = 1
DEFAULT_KEEP = 5
MAX_KEEP = 50
MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
MAX_SNAPSHOT_FILES = 5_000
SIZE_WARNING_BYTES = 5 * 1024 * 1024 * 1024
PREVIEW_BUCKET_LIMIT = 200
VALID_TRIGGERS = frozenset({"manual", "session_close", "auto", "pre-restore"})

_VAULT_LOCK = threading.RLock()
_SNAPSHOT_SEQ = 0


def vault_dir_for(data_dir: Path | str) -> Path:
    """Return the Save Vault directory under *data_dir*."""
    return Path(data_dir) / VAULT_DIR_NAME


def game_vault_key(game: dict[str, Any]) -> str:
    """Stable filesystem-safe folder key for one game."""
    gid = str((game or {}).get("game_id") or "").strip()
    if gid:
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", gid).strip("._")
        if cleaned:
            return cleaned[:80]
    seed = f"{(game or {}).get('name', '')}:{(game or {}).get('platform', '')}".encode("utf-8")
    return f"g-{hashlib.sha256(seed).hexdigest()[:16]}"


def _utc_now(now: datetime | None = None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _utc_iso(now: datetime | None = None) -> str:
    return _utc_now(now).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_snapshot_id() -> str:
    return f"snap-{secrets.token_hex(8)}"


def _empty_index() -> dict[str, Any]:
    return {"format": INDEX_FORMAT, "last_auto_at": "", "snapshots": []}


def _clamp_keep(value: Any, default: int = DEFAULT_KEEP) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(MAX_KEEP, num))


def _reject_symlinks(path: Path) -> None:
    try:
        _reject_symlink_components(path)
    except ValueError as error:
        raise VaultPathEscape(str(error)) from error
    component = user_symlink_component(path)
    if component is not None:
        raise VaultPathEscape(f"Save path may not contain symlinks: {component}")


def _validate_relpath(relpath: str) -> Path:
    text = str(relpath or "")
    if not text or "\x00" in text:
        raise VaultPathEscape("Save relative path is invalid.")
    if text.startswith(("/", "\\")) or "\\" in text:
        raise VaultPathEscape(f"Save relative path must be relative POSIX: {text}")
    parts = Path(text).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise VaultPathEscape(f"Save relative path escapes root: {text}")
    if ":" in parts[0]:
        raise VaultPathEscape(f"Save relative path may not contain a drive prefix: {text}")
    return Path(*parts)


def _resolve_contained_target(root: Path, relpath: str, *, is_file_root: bool) -> Path:
    rel = _validate_relpath(relpath)
    _reject_symlinks(root)
    resolved_root = root.resolve(strict=False)
    if is_file_root:
        if rel.name != relpath or rel.name != resolved_root.name:
            raise VaultPathEscape(f"Save restore target does not match file root: {relpath}")
        _reject_symlinks(resolved_root)
        return resolved_root
    target = (resolved_root / rel).resolve(strict=False)
    if target != resolved_root and not target.is_relative_to(resolved_root):
        raise VaultPathEscape(f"Save restore path escapes save root: {relpath}")
    # Check symlink components along the un-resolved path between root and target
    cursor = resolved_root
    for part in rel.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise VaultPathEscape(f"Save restore target contains symlink: {cursor}")
    return target


def _safe_snapshot_dir(vault_dir: Path, game_key: str, folder_name: str) -> Path:
    if not folder_name or "/" in folder_name or "\\" in folder_name or folder_name in (".", ".."):
        raise VaultPathEscape("Invalid snapshot folder name.")
    base = (vault_dir / game_key).resolve(strict=False)
    candidate = (base / folder_name).resolve(strict=False)
    if not candidate.is_relative_to(base) or candidate == base:
        raise VaultPathEscape("Snapshot path escapes vault directory.")
    return candidate


def _shared_paths_for_game(game: dict[str, Any], *, home: Path | None = None) -> set[str]:
    shared: set[str] = set()
    sources = game.get("save_sources") if isinstance(game, dict) else None
    if isinstance(sources, dict):
        for path, meta in sources.items():
            if isinstance(meta, dict) and meta.get("shared"):
                shared.add(str(path))
    for item in discover_save_paths(game, home=home) + extra_save_candidates(game, home=home):
        if item.get("shared") and item.get("path"):
            shared.add(str(item["path"]))
    return shared


def resolve_game_save_roots(
    game: dict[str, Any],
    *,
    include_shared: bool = False,
    home: Path | None = None,
) -> list[dict[str, Any]]:
    """Resolve and containment-check save roots for *game*.

    Automatic triggers pass ``include_shared=False`` so shared emulator folders
    (such as PCSX2 memcards or Dolphin Wii NAND) are not copied per game on
    every close. Manual snapshots include explicit ``save_paths`` entries.
    """
    if not isinstance(game, dict):
        raise VaultNoSaves("No save paths discovered for this game.")
    shared_set = _shared_paths_for_game(game, home=home)
    candidates: list[tuple[str, bool]] = []
    seen_raw: set[str] = set()

    configured = game.get("save_paths") or []
    if isinstance(configured, list):
        for raw in configured:
            text = str(raw or "").strip()
            if text and text not in seen_raw:
                seen_raw.add(text)
                candidates.append((text, text in shared_set))

    for item in discover_save_paths(game, home=home) + extra_save_candidates(game, home=home):
        text = str(item.get("path") or "").strip()
        is_shared = bool(item.get("shared")) or text in shared_set
        if not text or is_shared or text in seen_raw:
            continue
        seen_raw.add(text)
        candidates.append((text, False))

    roots: list[dict[str, Any]] = []
    seen_resolved: set[Path] = set()
    for raw_text, is_shared in candidates:
        if not include_shared and is_shared:
            continue
        raw_path = Path(raw_text).expanduser()
        _reject_symlinks(raw_path)
        if not raw_path.exists():
            continue
        resolved = raw_path.resolve(strict=True)
        if resolved in seen_resolved:
            continue
        seen_resolved.add(resolved)
        roots.append({
            "index": len(roots),
            "path": str(resolved),
            "is_file": resolved.is_file(),
            "shared": is_shared,
            "source": save_source_for(game, raw_text),
        })
    if not roots:
        raise VaultNoSaves("No existing save files were discovered for this game.")
    return roots


def _collect_root_files(roots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Enumerate all regular files under *roots*, enforcing symlink & size bounds."""
    collected: list[dict[str, Any]] = []
    total_bytes = 0
    for root_info in roots:
        root_idx = int(root_info["index"])
        root_path = Path(root_info["path"])
        _reject_symlinks(root_path)
        if root_info["is_file"]:
            pair_list = [(root_path, root_path.name)]
        else:
            pair_list = []
            for entry in sorted(root_path.rglob("*")):
                if entry.is_symlink():
                    raise VaultPathEscape(f"Save path contains a symlink: {entry}")
                if entry.is_file():
                    rel = entry.relative_to(root_path).as_posix()
                    _validate_relpath(rel)
                    pair_list.append((entry, rel))
        for file_path, rel in pair_list:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(file_path, flags)
            except OSError as error:
                raise VaultPathEscape(f"Save file cannot be opened safely: {file_path}") from error
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise VaultPathEscape(f"Save path is not a regular file: {file_path}")
                size = int(info.st_size)
                mtime = int(info.st_mtime)
                mode = stat.S_IMODE(info.st_mode)
            finally:
                os.close(fd)
            total_bytes += size
            if len(collected) >= MAX_SNAPSHOT_FILES:
                raise VaultLimitExceeded(f"Save Vault snapshot exceeds {MAX_SNAPSHOT_FILES} files.")
            if total_bytes > MAX_SNAPSHOT_BYTES:
                raise VaultLimitExceeded("Save Vault snapshot exceeds the 512 MiB per-snapshot limit.")
            collected.append({
                "root": root_idx,
                "source_path": file_path,
                "relpath": rel,
                "archive_rel": f"roots/{root_idx}/{rel}",
                "size": size,
                "mtime": mtime,
                "mode": mode,
            })
    if not collected:
        raise VaultNoSaves("Save directories exist, but contain no regular save files.")
    return collected


def _write_index_locked(vault_dir: Path, index: dict[str, Any]) -> None:
    vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "format": INDEX_FORMAT,
        "last_auto_at": str(index.get("last_auto_at") or ""),
        "snapshots": list(index.get("snapshots") or []),
    }
    atomic_write_text(
        vault_dir / INDEX_FILE_NAME,
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        mode=0o600,
    )


def rebuild_index(data_dir: Path | str, *, last_auto_at: str = "") -> dict[str, Any]:
    """Reconstruct ``save-vault/index.json`` by scanning self-describing ``snapshot.json`` files."""
    vault_dir = vault_dir_for(data_dir)
    entries: list[dict[str, Any]] = []
    if vault_dir.is_dir():
        for game_dir in sorted(vault_dir.iterdir()):
            if not game_dir.is_dir() or game_dir.is_symlink() or game_dir.name.startswith("."):
                continue
            for snap_dir in sorted(game_dir.iterdir()):
                if not snap_dir.is_dir() or snap_dir.is_symlink() or snap_dir.name.startswith("."):
                    continue
                manifest_path = snap_dir / SNAPSHOT_FILE_NAME
                if not manifest_path.is_file():
                    continue
                try:
                    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(raw, dict) or not raw.get("id"):
                    continue
                digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                trigger = str(raw.get("trigger") or "manual")
                entries.append({
                    "id": str(raw["id"]),
                    "game_id": str(raw.get("game_id") or ""),
                    "game_key": game_dir.name,
                    "folder": snap_dir.name,
                    "game_title": str(raw.get("game_title") or ""),
                    "created_at": str(raw.get("created_at") or ""),
                    "trigger": trigger,
                    "files": len(raw.get("files") or []),
                    "bytes": int(raw.get("bytes") or 0),
                    "sha256": digest,
                    "exempt_from_prune": trigger == "pre-restore",
                })
    entries.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("folder") or "")), reverse=True)
    rebuilt = {"format": INDEX_FORMAT, "last_auto_at": last_auto_at, "snapshots": entries}
    with _VAULT_LOCK:
        _write_index_locked(vault_dir, rebuilt)
    return rebuilt


def load_index(data_dir: Path | str) -> dict[str, Any]:
    """Read ``save-vault/index.json``, self-healing from snapshot manifests if corrupt."""
    vault_dir = vault_dir_for(data_dir)
    index_path = vault_dir / INDEX_FILE_NAME
    with _VAULT_LOCK:
        if not index_path.is_file():
            if vault_dir.is_dir() and any(p.is_dir() and not p.name.startswith(".") for p in vault_dir.iterdir()):
                return rebuild_index(data_dir)
            return _empty_index()
        try:
            raw = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            LOGGER.warning("Save Vault index.json is corrupt; rebuilding from snapshot manifests.")
            return rebuild_index(data_dir)
        if not isinstance(raw, dict) or not isinstance(raw.get("snapshots"), list):
            LOGGER.warning("Save Vault index.json has invalid structure; rebuilding from snapshot manifests.")
            return rebuild_index(data_dir)
        return {
            "format": int(raw.get("format") or INDEX_FORMAT),
            "last_auto_at": str(raw.get("last_auto_at") or ""),
            "snapshots": [dict(item) for item in raw["snapshots"] if isinstance(item, dict) and item.get("id")],
        }


def _copy_with_sha256(src: Path, dest: Path) -> str:
    """Stream *src* (opened O_NOFOLLOW) to *dest* (0600) and return SHA-256 hex digest."""
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    digest = hashlib.sha256()
    fd = os.open(src, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise VaultPathEscape(f"Save path is not a regular file: {src}")
        with os.fdopen(fd, "rb") as reader:
            fd = -1
            out_fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(out_fd, "wb") as writer:
                    out_fd = -1
                    while True:
                        chunk = reader.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        writer.write(chunk)
                    writer.flush()
                    os.fsync(writer.fileno())
            finally:
                if out_fd >= 0:
                    os.close(out_fd)
    finally:
        if fd >= 0:
            os.close(fd)
    return digest.hexdigest()


def _prune_game_snapshots_locked(vault_dir: Path, index: dict[str, Any], game_key: str, keep: int) -> int:
    keep = _clamp_keep(keep)
    all_rows = list(index.get("snapshots") or [])
    game_prunable = [
        row for row in all_rows
        if row.get("game_key") == game_key and not row.get("exempt_from_prune") and row.get("trigger") != "pre-restore"
    ]
    game_prunable.sort(
        key=lambda item: (str(item.get("created_at") or ""), str(item.get("folder") or "")),
        reverse=True,
    )
    to_drop = game_prunable[keep:]
    if not to_drop:
        return 0
    drop_ids = {row["id"] for row in to_drop}
    for row in to_drop:
        folder = str(row.get("folder") or "")
        if folder:
            try:
                snap_dir = _safe_snapshot_dir(vault_dir, game_key, folder)
                shutil.rmtree(snap_dir, ignore_errors=True)
            except VaultPathEscape:
                pass
    index["snapshots"] = [row for row in all_rows if row.get("id") not in drop_ids]
    return len(to_drop)


def create_snapshot(
    data_dir: Path | str,
    game: dict[str, Any],
    *,
    trigger: str = "manual",
    include_shared: bool | None = None,
    home: Path | None = None,
    now: datetime | None = None,
    keep: int | None = DEFAULT_KEEP,
    progress_cb=None,
) -> dict[str, Any]:
    """Capture an indexed, SHA-256-verified snapshot of *game*'s saves."""
    if trigger not in VALID_TRIGGERS:
        trigger = "manual"
    if include_shared is None:
        include_shared = trigger in ("manual", "pre-restore")

    global _SNAPSHOT_SEQ
    vault_dir = vault_dir_for(data_dir)
    game_key = game_vault_key(game)
    snap_id = _new_snapshot_id()
    moment = _utc_now(now)
    created_at = moment.strftime("%Y-%m-%dT%H:%M:%SZ")

    with SAVE_SYNC_LOCK, _VAULT_LOCK:
        roots = resolve_game_save_roots(game, include_shared=include_shared, home=home)
        items = _collect_root_files(roots)

        _SNAPSHOT_SEQ += 1
        folder_name = f"{moment.strftime('%Y%m%dT%H%M%SZ')}_{_SNAPSHOT_SEQ:04d}_{snap_id[-4:]}"
        game_dir = vault_dir / game_key
        game_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging_dir = game_dir / f".staging-{snap_id}"
        final_dir = _safe_snapshot_dir(vault_dir, game_key, folder_name)

        try:
            staging_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
            manifest_files: list[dict[str, Any]] = []
            total_bytes = 0
            total_items = len(items)
            for pos, item in enumerate(items, start=1):
                dest = staging_dir / item["archive_rel"]
                digest = _copy_with_sha256(item["source_path"], dest)
                total_bytes += item["size"]
                manifest_files.append({
                    "root": item["root"],
                    "relpath": item["relpath"],
                    "archive_rel": item["archive_rel"],
                    "size": item["size"],
                    "mtime": item["mtime"],
                    "mode": item["mode"],
                    "sha256": digest,
                })
                if progress_cb is not None:
                    progress_cb(pos, total_items)

            manifest = {
                "format": SNAPSHOT_FORMAT,
                "id": snap_id,
                "game_id": str(game.get("game_id") or ""),
                "game_title": str(game.get("name") or "Untitled"),
                "created_at": created_at,
                "trigger": trigger,
                "roots": roots,
                "files": manifest_files,
                "bytes": total_bytes,
            }
            manifest_bytes = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
            manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
            atomic_write_text(staging_dir / SNAPSHOT_FILE_NAME, manifest_bytes.decode("utf-8"), mode=0o600)
            fsync_directory(staging_dir)
            os.replace(staging_dir, final_dir)
            fsync_directory(game_dir)

            index = load_index(data_dir)
            entry = {
                "id": snap_id,
                "game_id": manifest["game_id"],
                "game_key": game_key,
                "folder": folder_name,
                "game_title": manifest["game_title"],
                "created_at": created_at,
                "trigger": trigger,
                "files": len(manifest_files),
                "bytes": total_bytes,
                "sha256": manifest_sha,
                "exempt_from_prune": trigger == "pre-restore",
            }
            index["snapshots"] = [entry, *[row for row in index["snapshots"] if row.get("id") != snap_id]]
            pruned = 0
            if keep is not None and trigger != "pre-restore":
                pruned = _prune_game_snapshots_locked(vault_dir, index, game_key, _clamp_keep(keep))
            _write_index_locked(vault_dir, index)
            return {
                "snapshot_id": snap_id,
                "game_id": manifest["game_id"],
                "game_key": game_key,
                "created_at": created_at,
                "trigger": trigger,
                "files": len(manifest_files),
                "bytes": total_bytes,
                "pruned": pruned,
                "snapshot": entry,
            }
        finally:
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)


def list_snapshots(data_dir: Path | str, *, game_id: str = "") -> list[dict[str, Any]]:
    index = load_index(data_dir)
    rows = index.get("snapshots", [])
    if game_id:
        wanted = str(game_id).strip()
        rows = [row for row in rows if str(row.get("game_id") or "") == wanted]
    rows.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("folder") or "")), reverse=True)
    return rows


def vault_summary_readonly(data_dir: Path | str) -> dict[str, int]:
    """Read-only Save Vault summary for ``compat_report`` and ``--check-library``."""
    index_path = vault_dir_for(data_dir) / INDEX_FILE_NAME
    if not index_path.is_file():
        return {"snapshots": 0, "bytes": 0, "games": 0}
    try:
        raw = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"snapshots": 0, "bytes": 0, "games": 0}
    rows = raw.get("snapshots") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return {"snapshots": 0, "bytes": 0, "games": 0}
    valid = [row for row in rows if isinstance(row, dict) and row.get("id")]
    return {
        "snapshots": len(valid),
        "bytes": sum(int(row.get("bytes") or 0) for row in valid),
        "games": len({str(row.get("game_key") or "") for row in valid if row.get("game_key")}),
    }


def vault_status(data_dir: Path | str, *, game_id: str = "", settings: dict[str, Any] | None = None) -> dict[str, Any]:
    rows = list_snapshots(data_dir)
    total_bytes = sum(int(row.get("bytes") or 0) for row in rows)
    games_count = len({str(row.get("game_key") or "") for row in rows if row.get("game_key")})
    game_rows = [row for row in rows if str(row.get("game_id") or "") == str(game_id).strip()] if game_id else []
    cfg = settings if isinstance(settings, dict) else {}
    return {
        "snapshots": len(rows),
        "bytes": total_bytes,
        "games": games_count,
        "over_5gb_warning": total_bytes >= SIZE_WARNING_BYTES,
        "warning_threshold_bytes": SIZE_WARNING_BYTES,
        "game_snapshots": len(game_rows),
        "game_bytes": sum(int(row.get("bytes") or 0) for row in game_rows),
        "settings": {
            "save_vault_enabled": bool(cfg.get("save_vault_enabled", True)),
            "save_vault_keep": _clamp_keep(cfg.get("save_vault_keep", DEFAULT_KEEP)),
            "save_vault_on_close": bool(cfg.get("save_vault_on_close", False)),
            "save_vault_auto_days": int(cfg.get("save_vault_auto_days", 0) or 0),
        },
    }


def _locate_snapshot(data_dir: Path | str, game: dict[str, Any] | None, snapshot_id: str) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    vault_dir = vault_dir_for(data_dir)
    index = load_index(data_dir)
    entry = next((row for row in index.get("snapshots", []) if row.get("id") == snapshot_id), None)
    if entry is None:
        raise VaultSnapshotMissing(f"Save Vault snapshot not found: {snapshot_id}")
    if game is not None:
        gid = str(game.get("game_id") or "").strip()
        if gid and str(entry.get("game_id") or "") != gid:
            raise VaultGameMismatch("Save Vault snapshot belongs to a different game.")
    snap_dir = _safe_snapshot_dir(vault_dir, str(entry["game_key"]), str(entry["folder"]))
    manifest_path = snap_dir / SNAPSHOT_FILE_NAME
    if not manifest_path.is_file():
        raise VaultSnapshotMissing(f"Snapshot manifest missing on disk: {snapshot_id}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise VaultSnapshotMissing(f"Snapshot manifest unreadable: {snapshot_id}") from error
    return entry, snap_dir, manifest


def diff_snapshot(data_dir: Path | str, game: dict[str, Any], snapshot_id: str) -> dict[str, Any]:
    """Compare *snapshot_id* against current live save files under its declared roots."""
    _entry, _snap_dir, manifest = _locate_snapshot(data_dir, game, snapshot_id)
    roots = {int(r["index"]): r for r in manifest.get("roots") or [] if isinstance(r, dict)}

    added: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []

    expected_targets: set[Path] = set()
    for item in manifest.get("files") or []:
        root_idx = int(item.get("root", 0))
        root_info = roots.get(root_idx)
        if not root_info:
            continue
        root_path = Path(str(root_info["path"]))
        target = _resolve_contained_target(root_path, str(item["relpath"]), is_file_root=bool(root_info.get("is_file")))
        expected_targets.add(target)
        row_info = {"path": str(target), "relpath": str(item["relpath"]), "size": int(item.get("size") or 0)}
        if not target.exists():
            added.append(row_info)
            continue
        live_hash = file_sha256(target)
        if live_hash == item.get("sha256"):
            unchanged.append(row_info)
        else:
            changed.append(row_info)

    for root_info in roots.values():
        root_path = Path(str(root_info["path"]))
        if not root_path.exists():
            continue
        _reject_symlinks(root_path)
        if root_info.get("is_file"):
            if root_path.resolve(strict=False) not in expected_targets and root_path.is_file():
                removed.append({"path": str(root_path), "relpath": root_path.name, "size": root_path.stat().st_size})
        else:
            for live in sorted(root_path.rglob("*")):
                if live.is_symlink():
                    raise VaultPathEscape(f"Live save directory contains symlink: {live}")
                if live.is_file() and live.resolve(strict=False) not in expected_targets:
                    removed.append({
                        "path": str(live),
                        "relpath": live.relative_to(root_path).as_posix(),
                        "size": live.stat().st_size,
                    })

    return {
        "snapshot_id": snapshot_id,
        "counts": {
            "added": len(added),
            "changed": len(changed),
            "removed": len(removed),
            "unchanged": len(unchanged),
        },
        "added": added[:PREVIEW_BUCKET_LIMIT],
        "changed": changed[:PREVIEW_BUCKET_LIMIT],
        "removed": removed[:PREVIEW_BUCKET_LIMIT],
        "unchanged": unchanged[:PREVIEW_BUCKET_LIMIT],
    }


def _any_live_files(roots: dict[int, dict[str, Any]]) -> bool:
    for root_info in roots.values():
        root_path = Path(str(root_info.get("path") or ""))
        if not root_path.exists():
            continue
        if root_info.get("is_file") and root_path.is_file():
            return True
        if root_path.is_dir() and any(item.is_file() for item in root_path.rglob("*")):
            return True
    return False


def restore_snapshot(
    data_dir: Path | str,
    game: dict[str, Any],
    snapshot_id: str,
    *,
    dry_run: bool = False,
    skip_safety: bool = False,
    include_removals: bool = False,
    now: datetime | None = None,
    progress_cb=None,
) -> dict[str, Any]:
    """Restore a Save Vault snapshot with mandatory safety snapshot and per-file verification."""
    with SAVE_SYNC_LOCK, _VAULT_LOCK:
        _entry, snap_dir, manifest = _locate_snapshot(data_dir, game, snapshot_id)
        roots = {int(r["index"]): r for r in manifest.get("roots") or [] if isinstance(r, dict)}
        # Pre-validate every target path before mutating anything on disk.
        planned: list[tuple[dict[str, Any], Path, Path]] = []
        for item in manifest.get("files") or []:
            root_idx = int(item.get("root", 0))
            root_info = roots.get(root_idx)
            if not root_info:
                raise VaultPathEscape(f"Snapshot references unknown root index: {root_idx}")
            root_path = Path(str(root_info["path"]))
            dest = _resolve_contained_target(root_path, str(item["relpath"]), is_file_root=bool(root_info.get("is_file")))
            archive_rel = str(item.get("archive_rel") or "")
            _validate_relpath(archive_rel)
            src = (snap_dir / archive_rel).resolve(strict=False)
            if not src.is_relative_to(snap_dir.resolve(strict=False)):
                raise VaultPathEscape(f"Snapshot source path escapes vault: {archive_rel}")
            planned.append((item, src, dest))

        diff = diff_snapshot(data_dir, game, snapshot_id)
        if dry_run:
            return {
                "snapshot_id": snapshot_id,
                "safety_snapshot_id": None,
                "dry_run": True,
                "diff": diff,
                "files": [],
                "counts": {"restored": 0, "unchanged": diff["counts"]["unchanged"], "removed": 0, "error": 0},
            }

        safety_snapshot_id: str | None = None
        if not skip_safety and _any_live_files(roots):
            try:
                safety = create_snapshot(
                    data_dir,
                    game,
                    trigger="pre-restore",
                    include_shared=True,
                    now=now,
                    keep=None,
                )
                safety_snapshot_id = safety["snapshot_id"]
            except VaultNoSaves:
                safety_snapshot_id = None
            except Exception as error:
                raise VaultSafetySnapshotFailed(
                    f"Pre-restore safety snapshot failed; live saves were left untouched: {error}"
                ) from error

        results: list[dict[str, Any]] = []
        counts = {"restored": 0, "unchanged": 0, "removed": 0, "error": 0}
        total = len(planned)

        for pos, (item, src, dest) in enumerate(planned, start=1):
            if progress_cb is not None:
                progress_cb(pos, total)
            if not src.is_file() or src.is_symlink():
                counts["error"] += 1
                results.append({"path": str(dest), "status": "error", "error": "Snapshot file missing or invalid."})
                continue
            try:
                src_sha = file_sha256(src)
                if src_sha != item.get("sha256"):
                    counts["error"] += 1
                    results.append({"path": str(dest), "status": "error", "error": "Snapshot file checksum mismatch."})
                    continue
                if dest.exists() and not dest.is_symlink() and dest.is_file() and file_sha256(dest) == src_sha:
                    counts["unchanged"] += 1
                    results.append({"path": str(dest), "status": "unchanged"})
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp_name = tempfile.mkstemp(prefix=".vault-restore-", suffix=".tmp", dir=dest.parent)
                os.close(fd)
                tmp_path = Path(tmp_name)
                try:
                    _copy_with_sha256(src, tmp_path)
                    mode = int(item.get("mode") or 0o600) & 0o777
                    os.chmod(tmp_path, mode or 0o600)
                    os.replace(tmp_path, dest)
                    fsync_directory(dest.parent)
                finally:
                    tmp_path.unlink(missing_ok=True)
                counts["restored"] += 1
                results.append({"path": str(dest), "status": "restored"})
            except (OSError, ValueError) as error:
                counts["error"] += 1
                results.append({"path": str(dest), "status": "error", "error": str(error)})

        if include_removals:
            for item in diff.get("removed") or []:
                target = Path(str(item["path"]))
                try:
                    if target.is_file() and not target.is_symlink():
                        target.unlink()
                        counts["removed"] += 1
                        results.append({"path": str(target), "status": "removed"})
                except OSError as error:
                    counts["error"] += 1
                    results.append({"path": str(target), "status": "error", "error": str(error)})

        return {
            "snapshot_id": snapshot_id,
            "safety_snapshot_id": safety_snapshot_id,
            "dry_run": False,
            "files": results,
            "counts": counts,
        }


def delete_snapshot(data_dir: Path | str, game: dict[str, Any] | None, snapshot_id: str) -> dict[str, Any]:
    vault_dir = vault_dir_for(data_dir)
    with _VAULT_LOCK:
        entry, snap_dir, _manifest = _locate_snapshot(data_dir, game, snapshot_id)
        shutil.rmtree(snap_dir, ignore_errors=False)
        index = load_index(data_dir)
        index["snapshots"] = [row for row in index["snapshots"] if row.get("id") != snapshot_id]
        _write_index_locked(vault_dir, index)
        return {"deleted": True, "snapshot_id": snapshot_id, "game_id": entry.get("game_id", "")}


def auto_vault_due(data_dir: Path | str, settings: dict[str, Any], *, now: datetime | None = None) -> bool:
    cfg = settings if isinstance(settings, dict) else {}
    if not cfg.get("save_management_enabled", True) or not cfg.get("save_vault_enabled", True):
        return False
    try:
        auto_days = int(cfg.get("save_vault_auto_days", 0) or 0)
    except (TypeError, ValueError):
        return False
    if auto_days <= 0:
        return False
    index = load_index(data_dir)
    raw = str(index.get("last_auto_at") or "").strip()
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (_utc_now(now) - last).total_seconds() >= auto_days * 86400


def run_auto_vault_pass(
    data_dir: Path | str,
    state: dict[str, Any],
    *,
    now: datetime | None = None,
    cancel_event=None,
) -> dict[str, Any]:
    settings = state.get("settings", {}) if isinstance(state, dict) else {}
    keep = _clamp_keep(settings.get("save_vault_keep", DEFAULT_KEEP))
    games = state.get("games", []) if isinstance(state, dict) else []
    snapshotted = 0
    skipped = 0
    errors: list[str] = []

    for game in games if isinstance(games, list) else []:
        if cancel_event is not None and cancel_event.is_set():
            break
        if not isinstance(game, dict) or game.get("hidden"):
            continue
        try:
            create_snapshot(
                data_dir,
                game,
                trigger="auto",
                include_shared=False,
                now=now,
                keep=keep,
            )
            snapshotted += 1
        except VaultNoSaves:
            skipped += 1
        except (OSError, ValueError, VaultPathEscape, VaultLimitExceeded) as error:
            errors.append(f"{game.get('name', 'game')}: {error}")

    with _VAULT_LOCK:
        index = load_index(data_dir)
        index["last_auto_at"] = _utc_iso(now)
        _write_index_locked(vault_dir_for(data_dir), index)

    return {"snapshotted": snapshotted, "skipped": skipped, "errors": errors[:20]}
