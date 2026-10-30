"""Library format v7: the v6 copy, migration report, rollback, integrity check, and repair.

Every function here works on one data directory, the folder that holds
``library.json``. Files change only through atomic writers, and migration runs
under the store's exclusive file lock. Standard library only.

Files written beside ``library.json``:

- ``library.v6.backup.json``: byte-identical copy of the primary as it was
  before the migration. A differing older copy is renamed, never overwritten.
- ``migration-in-progress.json``: written before the backup, removed once the
  report exists. Its presence after a crash means the report is still owed.
- ``migration-report.json``: what the last migration did.
- ``library.v7.kept-<stamp>.json``: the v7 primary that a rollback replaced.
- ``rollback-report.json``: what a rollback restored and what it discarded.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend_io import atomic_write_bytes
from notifications import add_notification
from pkg.platform_compat import try_file_lock
from pkg.state import media_manifest
from pkg.state import paths as library_paths
from state_store import (
    LIBRARY_ID_RE,
    STATE_FORMAT,
    STATE_SCHEMA_VERSION,
    JsonStateStore,
    StateCorruptError,
    StateFutureVersionError,
    StateBackupError,
    StateMigrationError,
    app_version,
    normalize_state,
    prune_trash,
    raw_schema_version,
    read_sessions_file,
)

LOGGER = logging.getLogger("openbox.state")

V6_BACKUP_NAME = "library.v6.backup.json"
PRIMARY_NAME = "library.json"
REPORT_NAME = "migration-report.json"
MARKER_NAME = "migration-in-progress.json"
ROLLBACK_REPORT_NAME = "rollback-report.json"
REPORT_FORMAT = 1
ROLLBACK_COMMAND = "openbox --rollback-library"
EXAMPLE_LIMIT = 20

Progress = Callable[[str, dict[str, Any]], None]


class RollbackRefused(RuntimeError):
    """A rollback or repair cannot run safely. The message says what to do first."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_bytes(path, (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _parse_primary(data: bytes, path: Path) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise StateCorruptError(
            f"Unable to read {path}. The original file was preserved; restore or inspect it before continuing."
        ) from error


def _size(value: Any) -> int:
    return len(value) if isinstance(value, (list, dict)) else 0


def counts_for(state: dict[str, Any]) -> dict[str, int]:
    """Counts recorded in the migration report. Paths are counted by the portable-path migration, not here."""
    return {
        "games": _size(state.get("games")),
        "profiles": _size(state.get("profiles")),
        "playlists": _size(state.get("playlists")),
        "sessions_moved": _size(state.get("active_sessions")),
        "paths_tokenized": 0,
        "paths_skipped": 0,
    }


def server_running(data_dir: Path) -> bool:
    """True when a live OpenBox server answers on the loopback port recorded in ``server.port``."""
    try:
        port = int((Path(data_dir) / "server.port").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    if not 0 < port < 65536:
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _newest_snapshot(store: JsonStateStore) -> str | None:
    items = store.snapshots()
    return items[0]["name"] if items else None


def _build_report(
    *,
    started_at: str,
    state: dict[str, Any],
    from_schema: int,
    backup_sha: str,
    snapshot: str | None,
    counts: dict[str, int],
    warnings: list[str],
    reconstructed: bool,
) -> dict[str, Any]:
    return {
        "format": REPORT_FORMAT,
        "app_version": app_version(),
        "started_at": started_at,
        "completed_at": _utc_now(),
        "from_schema": from_schema,
        "to_schema": STATE_SCHEMA_VERSION,
        "library_id": str(state.get("library_id") or ""),
        "backup": V6_BACKUP_NAME,
        "backup_sha256": backup_sha,
        "pre_migration_snapshot": snapshot,
        "counts": counts,
        "warnings": warnings,
        "rollback": ROLLBACK_COMMAND,
        "reconstructed": reconstructed,
    }


def _keep_superseded_backup(data_dir: Path, new_sha: str) -> None:
    existing = data_dir / V6_BACKUP_NAME
    if existing.is_file() and _sha256(existing.read_bytes()) != new_sha:
        os.replace(existing, data_dir / f"library.v6.backup.{_stamp()}.json")


def _finish_interrupted_migration(data_dir: Path, primary: Path) -> dict[str, Any] | None:
    """Write the report owed by a migration that crashed after the v7 primary was written."""
    marker = _read_json_file(data_dir / MARKER_NAME)
    if marker is None:
        return None
    raw = _parse_primary(primary.read_bytes(), primary)
    state = raw if isinstance(raw, dict) else {}
    report = _build_report(
        started_at=str(marker.get("started_at") or _utc_now()),
        state=state,
        from_schema=int(marker.get("from_schema") or 6),
        backup_sha=str(marker.get("backup_sha256") or ""),
        snapshot=None,
        counts=counts_for(state),
        warnings=["Written after an interrupted migration; counts describe the library as found."],
        reconstructed=True,
    )
    try:
        _write_json(data_dir / REPORT_NAME, report)
        (data_dir / MARKER_NAME).unlink(missing_ok=True)
    except OSError:
        LOGGER.warning("The migration report could not be written; the next start will retry.", exc_info=True)
    return report


def _silent(_phase: str, _detail: dict[str, Any]) -> None:
    return None


def migrate_locked(
    store: JsonStateStore, progress: Progress | None = None
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Upgrade the primary to schema v7. The caller must hold the store's locks.

    Order of effects: validate in memory, write the v6 copy and the marker,
    snapshot the v6 primary, replace the primary, write the report, remove the
    marker. A failure before the primary is replaced leaves the original file
    as it was. Returns ``(state, report)``; both are None when there is no
    primary, and ``state`` is None when the library was already current.
    """
    emit = progress or _silent
    primary = store.path
    data_dir = primary.parent
    if not primary.exists():
        return None, None
    raw_bytes = primary.read_bytes()
    raw = _parse_primary(raw_bytes, primary)
    version = raw_schema_version(raw)
    if version > STATE_SCHEMA_VERSION:
        raise StateFutureVersionError(
            f"This library uses schema {version}; this build supports {STATE_SCHEMA_VERSION}. Install a newer OpenBox."
        )
    if version == STATE_SCHEMA_VERSION:
        return None, _finish_interrupted_migration(data_dir, primary)

    started_at = _utc_now()
    try:
        state, _changed = normalize_state(raw)
    except StateCorruptError as error:
        raise StateMigrationError(
            f"The library could not be upgraded: {error} The original file was not changed."
        ) from error

    backup_sha = _sha256(raw_bytes)
    try:
        _keep_superseded_backup(data_dir, backup_sha)
        atomic_write_bytes(data_dir / V6_BACKUP_NAME, raw_bytes)
        _write_json(
            data_dir / MARKER_NAME,
            {
                "format": REPORT_FORMAT,
                "started_at": started_at,
                "from_schema": version,
                "backup": V6_BACKUP_NAME,
                "backup_sha256": backup_sha,
            },
        )
    except OSError as error:
        raise StateBackupError(
            f"The v{version} copy could not be written: {error}. The original file was not changed."
        ) from error
    emit("backup", {"backup": V6_BACKUP_NAME, "sha256": backup_sha, "bytes": len(raw_bytes)})

    store._rotate_snapshots(force=True)
    snapshot = _newest_snapshot(store)
    counts = counts_for(state)
    add_notification(
        state,
        kind="library.migrated",
        level="info",
        title=f"Library upgraded to format {STATE_SCHEMA_VERSION}",
        body=f"A v{version} copy is kept at {V6_BACKUP_NAME}. Settings > Library can roll it back.",
    )
    emit("migrate", {"from_schema": version, "to_schema": STATE_SCHEMA_VERSION, "counts": counts})

    try:
        store._write_unlocked(state, adopt=True)
    except OSError as error:
        raise StateMigrationError(
            f"The upgraded library could not be written: {error}. The original file was not replaced."
        ) from error
    emit("write", {"path": PRIMARY_NAME})

    report = _build_report(
        started_at=started_at,
        state=state,
        from_schema=version,
        backup_sha=backup_sha,
        snapshot=snapshot,
        counts=counts,
        warnings=[],
        reconstructed=False,
    )
    try:
        _write_json(data_dir / REPORT_NAME, report)
        (data_dir / MARKER_NAME).unlink(missing_ok=True)
    except OSError:
        # The v7 primary is already in place. The marker stays, so the next start writes the report.
        LOGGER.warning("The migration report could not be written; it will be written on the next start.", exc_info=True)
    emit("done", {"report": report})
    return state, report


def read_report(data_dir: Path) -> dict[str, Any] | None:
    return _read_json_file(Path(data_dir) / REPORT_NAME)


def _games_by_id(state: Any) -> dict[str, dict[str, Any]]:
    games = state.get("games") if isinstance(state, dict) else None
    result: dict[str, dict[str, Any]] = {}
    for game in games if isinstance(games, list) else []:
        if isinstance(game, dict) and str(game.get("game_id") or "").strip():
            result[str(game["game_id"])] = game
    return result


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def changes_since_migration(current: Any, v6: Any) -> dict[str, Any]:
    """Game-level differences a rollback would discard (added, edited) or restore (deleted after migration)."""
    now = _games_by_id(current)
    then = _games_by_id(v6)
    added = sorted(set(now) - set(then))
    restored = sorted(set(then) - set(now))
    edited = sorted(
        game_id for game_id in set(now) & set(then) if _canonical(now[game_id]) != _canonical(then[game_id])
    )
    return {
        "games_added": len(added),
        "games_edited": len(edited),
        "games_restored": len(restored),
        "examples": {
            "games_added": added[:EXAMPLE_LIMIT],
            "games_edited": edited[:EXAMPLE_LIMIT],
            "games_restored": restored[:EXAMPLE_LIMIT],
        },
    }


ACK_FILE = "migration-acknowledged.json"


def _read_report(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def migration_status(data_dir: Path) -> dict[str, Any]:
    """What the last migration did, and whether the user has seen it yet.

    A migration is pending until the user acknowledges it, so the pre-app page can
    show the report and offer a rollback before the library is used.
    """
    data_dir = Path(data_dir)
    report = _read_report(data_dir / REPORT_NAME)
    acknowledged = _read_report(data_dir / ACK_FILE)
    completed_at = str(report.get("completed_at") or "")
    pending = bool(report) and str(acknowledged.get("completed_at") or "") != completed_at
    backup = data_dir / str(report.get("backup") or V6_BACKUP_NAME)
    return {
        "pending": pending,
        "report": report,
        "backup_path": str(backup) if backup.is_file() else "",
        "acknowledged_at": str(acknowledged.get("acknowledged_at") or ""),
        "rollback_command": ROLLBACK_COMMAND,
    }


def acknowledge_migration(data_dir: Path) -> dict[str, Any]:
    """Record that the user has seen the migration report."""
    data_dir = Path(data_dir)
    report = _read_report(data_dir / REPORT_NAME)
    if not report:
        return {"status": "no_migration"}
    payload = {"completed_at": str(report.get("completed_at") or ""), "acknowledged_at": _utc_now()}
    _write_json(data_dir / ACK_FILE, payload)
    return {"status": "acknowledged", **payload}


def rollback(data_dir: Path, *, confirmed: bool, allow_running: bool = False) -> dict[str, Any]:
    """Restore the v6 copy as the primary library.

    Without ``confirmed`` this only reports what would be discarded. Refuses
    while OpenBox is running, while sessions are recorded, or when the copy
    does not validate as v6. The replaced v7 file is kept, never deleted.

    ``allow_running`` is for the in-app rollback only: the caller is the running
    server, and it freezes the library writes and stops right after the restore.
    """
    data_dir = Path(data_dir)
    primary = data_dir / PRIMARY_NAME
    backup = data_dir / V6_BACKUP_NAME
    if not allow_running and server_running(data_dir):
        raise RollbackRefused("OpenBox is running. Quit OpenBox before rolling back the library.")
    if read_sessions_file(data_dir):
        raise RollbackRefused(
            "Sessions are still recorded in sessions.json. End the session before rolling back."
        )
    if not backup.is_file():
        raise RollbackRefused(f"{V6_BACKUP_NAME} is missing, so there is no v6 copy to restore.")
    backup_bytes = backup.read_bytes()
    v6_raw = _parse_primary(backup_bytes, backup)
    if raw_schema_version(v6_raw) >= STATE_SCHEMA_VERSION:
        raise RollbackRefused(f"{V6_BACKUP_NAME} is not a v6 library, so it cannot be restored.")
    try:
        normalize_state(v6_raw)
    except StateCorruptError as error:
        raise RollbackRefused(f"{V6_BACKUP_NAME} does not validate: {error}") from error

    current_bytes = primary.read_bytes() if primary.is_file() else b""
    current_raw = _parse_primary(current_bytes, primary) if current_bytes else {}
    lost = changes_since_migration(current_raw, v6_raw)
    result: dict[str, Any] = {
        "status": "rolled_back" if confirmed else "confirmation_required",
        "backup": V6_BACKUP_NAME,
        "lost": lost,
    }
    if not confirmed:
        return result

    with try_file_lock(data_dir / f".{PRIMARY_NAME}.lock") as held:
        if not held:
            raise RollbackRefused("Another OpenBox process is writing the library. Try again when it has exited.")
        kept: Path | None = None
        if current_bytes:
            kept = data_dir / f"library.v7.kept-{_stamp()}.json"
            atomic_write_bytes(kept, current_bytes)
        atomic_write_bytes(primary, backup_bytes)
        atomic_write_bytes(data_dir / f"{PRIMARY_NAME}.bak", backup_bytes)
        (data_dir / REPORT_NAME).unlink(missing_ok=True)
        (data_dir / MARKER_NAME).unlink(missing_ok=True)
        report = {
            "format": REPORT_FORMAT,
            "app_version": app_version(),
            "rolled_back_at": _utc_now(),
            "restored": V6_BACKUP_NAME,
            "restored_sha256": _sha256(backup_bytes),
            "kept": kept.name if kept else None,
            "lost": lost,
            "warning": "Changes made after the migration are not in the restored library.",
        }
        _write_json(data_dir / ROLLBACK_REPORT_NAME, report)
    result["kept"] = report["kept"]
    result["report"] = report
    return result


_SEVERITY = {
    "primary_unreadable": "error",
    "schema_future": "error",
    "game_duplicate_id": "error",
    "path_token_escape": "error",
    "path_token_invalid": "error",
    "library_root_missing": "error",
    "schema_outdated": "warning",
    "envelope_incomplete": "warning",
    "game_path_missing": "warning",
    "game_path_unreadable": "warning",
    "game_path_outside_root": "warning",
    "media_missing": "warning",
    "media_changed": "warning",
    "media_untracked": "warning",
}


class _Findings:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.examples: dict[str, list[str]] = {}

    def add(self, category: str, example: str) -> None:
        self.counts[category] = self.counts.get(category, 0) + 1
        bucket = self.examples.setdefault(category, [])
        if len(bucket) < EXAMPLE_LIMIT:
            bucket.append(example)

    def finish(self, report: dict[str, Any]) -> dict[str, Any]:
        categories = {
            category: {
                "severity": _SEVERITY.get(category, "warning"),
                "count": count,
                "examples": self.examples.get(category, []),
            }
            for category, count in sorted(self.counts.items())
        }
        if any(item["severity"] == "error" for item in categories.values()):
            status = "errors"
        elif categories:
            status = "warnings"
        else:
            status = "healthy"
        report.update(
            status=status,
            exit_code={"healthy": 0, "warnings": 1, "errors": 2}[status],
            categories=categories,
        )
        return report


def _platform_rules() -> tuple[bool, bool]:
    """(windows, case_insensitive) for the running host. macOS volumes are case-insensitive by default."""
    windows = sys.platform.startswith("win")
    return windows, windows or sys.platform == "darwin"


def _check_game_path(
    findings: _Findings, game_id: str, path: str, library_root: str, windows: bool, folded: bool
) -> None:
    kind = library_paths.classify(path, library_root, windows=windows, case_insensitive=folded)
    if kind == "token":
        try:
            resolved = library_paths.decode_path(path, library_root, windows=windows, case_insensitive=folded)
        except library_paths.PathTokenError as error:
            category = {
                library_paths.CODE_TOKEN_ESCAPE: "path_token_escape",
                library_paths.CODE_ROOT_MISSING: "library_root_missing",
            }.get(error.code, "path_token_invalid")
            findings.add(category, f"{game_id}: {path}")
            return
    else:
        resolved = path
        if kind == "absolute" and library_root:
            findings.add("game_path_outside_root", f"{game_id}: {path}")
    if not os.path.exists(resolved):
        findings.add("game_path_missing", f"{game_id}: {resolved}")
    elif not os.access(resolved, os.R_OK):
        findings.add("game_path_unreadable", f"{game_id}: {resolved}")


def check_library(data_dir: Path) -> dict[str, Any]:
    """Read-only integrity report. Exit codes: 0 healthy, 1 warnings, 2 errors."""
    data_dir = Path(data_dir)
    primary = data_dir / PRIMARY_NAME
    report: dict[str, Any] = {
        "format": REPORT_FORMAT,
        "library_path": str(primary),
        "checked_at": _utc_now(),
        "schema_version": None,
        "library_id": None,
        "path_mode": None,
        "library_root": None,
        "games": 0,
    }
    findings = _Findings()
    if not primary.is_file():
        report.update(status="healthy", exit_code=0, note="No library.json yet; nothing to check.", categories={})
        return report
    try:
        raw = _parse_primary(primary.read_bytes(), primary)
    except StateCorruptError as error:
        findings.add("primary_unreadable", str(error))
        return findings.finish(report)
    version = raw_schema_version(raw)
    report["schema_version"] = version
    if version > STATE_SCHEMA_VERSION:
        findings.add("schema_future", f"schema {version}; this build supports {STATE_SCHEMA_VERSION}")
    elif version < STATE_SCHEMA_VERSION:
        findings.add("schema_outdated", f"schema {version}; run --migrate-library")
    if not isinstance(raw, dict):
        findings.add("primary_unreadable", "library.json is not a JSON object")
        return findings.finish(report)
    if version == STATE_SCHEMA_VERSION and (
        raw.get("format") != STATE_FORMAT or not LIBRARY_ID_RE.match(str(raw.get("library_id") or ""))
    ):
        findings.add("envelope_incomplete", "format or library_id is missing or invalid")

    windows, folded = _platform_rules()
    path_mode = raw.get("path_mode") or "absolute"
    library_root = str(raw.get("library_root") or "")
    report.update(library_id=raw.get("library_id"), path_mode=path_mode, library_root=library_root)
    if library_root and not os.path.isdir(library_root):
        findings.add("library_root_missing", library_root)

    games = raw.get("games") if isinstance(raw.get("games"), list) else []
    report["games"] = len(games)
    seen: set[str] = set()
    for game in games:
        if not isinstance(game, dict):
            continue
        game_id = str(game.get("game_id") or "")
        if game_id in seen:
            findings.add("game_duplicate_id", game_id)
        seen.add(game_id)
        path = game.get("path")
        if isinstance(path, str) and path.strip():
            _check_game_path(findings, game_id, path, library_root, windows, folded)
    media = media_manifest.verify(data_dir / "media")
    for relative in media["missing"]:
        findings.add("media_missing", relative)
    for relative in media["changed"]:
        findings.add("media_changed", relative)
    for relative in media["untracked"]:
        findings.add("media_untracked", relative)
    return findings.finish(report)


def repair_library(data_dir: Path, *, refresh_backup: bool = False) -> dict[str, Any]:
    """Narrow repair: prune expired trash, drop the derived read model, optionally refresh ``.bak``.

    Never deletes games or media. Refuses a library that needs migration or
    does not validate, so the last-known-good copy is never replaced by a
    broken primary.
    """
    data_dir = Path(data_dir)
    primary = data_dir / PRIMARY_NAME
    if not primary.is_file():
        return {"status": "nothing_to_repair", "reason": "No library.json yet."}
    if server_running(data_dir):
        raise RollbackRefused("OpenBox is running. Quit OpenBox before repairing the library.")
    raw = _parse_primary(primary.read_bytes(), primary)
    if raw_schema_version(raw) < STATE_SCHEMA_VERSION:
        raise RollbackRefused("The library needs migration first. Run --migrate-library.")
    raw_trash = raw.get("trash") if isinstance(raw.get("trash"), list) else []
    trash_removed = len(raw_trash) - len(prune_trash(raw_trash))
    store = JsonStateStore(primary)
    state = store.load()
    pruned = prune_trash(state.get("trash") or [])
    if len(pruned) != len(state.get("trash") or []):
        state["trash"] = pruned
        store.save(state)
    changed = trash_removed > 0
    read_model = data_dir / "sqlite_readmodel.db"
    read_model_removed = read_model.exists()
    read_model.unlink(missing_ok=True)
    if refresh_backup:
        atomic_write_bytes(data_dir / f"{PRIMARY_NAME}.bak", primary.read_bytes())
    media = media_manifest.rebuild(data_dir / "media")
    media_changed = bool(media["added"] or media["refreshed"] or media["dropped"])
    status = "repaired" if (changed or read_model_removed or refresh_backup or media_changed) else "nothing_to_repair"
    return {
        "status": status,
        "trash_entries_removed": trash_removed,
        "read_model_removed": read_model_removed,
        "backup_refreshed": refresh_backup,
        "media_manifest": media,
    }


# ---------------------------------------------------------------------------
# Portable paths (ADR 0090). Tokens are expanded on load and encoded on write,
# so every consumer sees absolute paths while library.json stores tokens.
# ---------------------------------------------------------------------------

MOVE_PLAN_FORMAT = 1


_EXTRA_LISTS = ("applications", "versions", "documents")


def _path_slots(game: dict):
    """Yield ``(slot, value)`` for each path a game record carries: ``path`` and each extra's ``path``."""
    yield "path", game.get("path")
    for name in _EXTRA_LISTS:
        extras = game.get(name)
        if isinstance(extras, list):
            for index, item in enumerate(extras):
                if isinstance(item, dict):
                    yield f"{name}.{index}", item.get("path")


def _set_slot(game: dict, slot: str, value: str) -> None:
    if slot == "path":
        game["path"] = value
        return
    name, index = slot.split(".")
    game[name][int(index)]["path"] = value


def _rewrite_game_paths(game: dict, rewrite) -> tuple[dict, list[str]]:
    """Copy ``game`` with ``rewrite(value)`` applied to each path it changes. The input is never mutated.

    ``rewrite`` may raise PathTokenError: that path keeps its value and its code is returned.
    The same object comes back when nothing changed.
    """
    problems: list[str] = []
    updates: dict[str, str] = {}
    for slot, value in _path_slots(game):
        if not isinstance(value, str) or not value:
            continue
        try:
            new_value = rewrite(value)
        except library_paths.PathTokenError as error:
            problems.append(error.code)
            continue
        if new_value != value:
            updates[slot] = new_value
    if not updates:
        return game, problems
    copied = dict(game)
    for name in _EXTRA_LISTS:
        if name in game and isinstance(game[name], list) and any(slot.startswith(name + ".") for slot in updates):
            copied[name] = [dict(item) if isinstance(item, dict) else item for item in game[name]]
    for slot, new_value in updates.items():
        _set_slot(copied, slot, new_value)
    return copied, problems


def expand_games(games: Any, root: str) -> tuple[Any, list[str]]:
    """Resolve @library tokens in game and extra-app paths against ``root``. Returns ``(games, problems)``.

    A token that cannot resolve (no root, an escape) is kept as written and reported, so a missing
    drive appears as a path problem instead of a silent loss.
    """
    if not isinstance(games, list) or not root:
        return games, []
    windows, folded = _platform_rules()

    def decode(value: str) -> str:
        if not library_paths.is_token(value):
            return value
        return library_paths.decode_path(value, root, windows=windows, case_insensitive=folded)

    problems: list[str] = []
    expanded = games
    for index, game in enumerate(games):
        if not isinstance(game, dict):
            continue
        updated, game_problems = _rewrite_game_paths(game, decode)
        problems += [f"{game.get('game_id', '')}: {code}" for code in game_problems]
        if updated is not game:
            if expanded is games:
                expanded = list(games)
            expanded[index] = updated
    return expanded, problems


def encode_games(games: Any, root: str) -> Any:
    """Store game and extra-app paths under ``root`` as tokens. Returns the same list when nothing changes."""
    if not isinstance(games, list) or not root:
        return games
    windows, folded = _platform_rules()

    def encode(value: str) -> str:
        return library_paths.encode_path(value, root, windows=windows, case_insensitive=folded)

    encoded = games
    for index, game in enumerate(games):
        if not isinstance(game, dict):
            continue
        updated, _ = _rewrite_game_paths(game, encode)
        if updated is not game:
            if encoded is games:
                encoded = list(games)
            encoded[index] = updated
    return encoded


def plan_move(games: Any, old_root: str, new_root: str) -> dict[str, Any]:
    """Describe which game paths follow the folder from ``old_root`` to ``new_root``.

    Nothing is copied or moved: the user moves the folder, and this rebases stored paths.
    ``plan_id`` is a digest of the rebased paths, so apply can refuse a stale plan.
    """
    windows, folded = _platform_rules()
    old = library_paths.normalize_root(old_root, windows=windows)
    new = library_paths.normalize_root(new_root, windows=windows)
    if not old or not new:
        raise library_paths.PathTokenError(library_paths.CODE_ROOT_MISSING, "Both library folders are required.")
    counts = {"tokenized": 0, "absolute_inside_old": 0, "missing": 0, "outside": 0, "symlink_outside": 0}
    examples: dict[str, list[str]] = {"missing": [], "outside": []}
    rebased: dict[str, str] = {}
    for game in games if isinstance(games, list) else []:
        if not isinstance(game, dict):
            continue
        game_id = str(game.get("game_id") or "")
        for slot, path in _path_slots(game):
            if not isinstance(path, str) or not path:
                continue
            kind = library_paths.classify(path, old, windows=windows, case_insensitive=folded)
            if kind == "token":
                counts["tokenized"] += 1
                continue
            if kind != "inside_root":
                counts["outside"] += 1
                if len(examples["outside"]) < EXAMPLE_LIMIT:
                    examples["outside"].append(f"{game_id}: {path}")
                continue
            counts["absolute_inside_old"] += 1
            relative = library_paths.encode_path(path, old, windows=windows, case_insensitive=folded)
            target = library_paths.decode_path(relative, new, windows=windows, case_insensitive=folded)
            rebased[f"{game_id}|{slot}"] = target
            if not os.path.exists(target):
                counts["missing"] += 1
                if len(examples["missing"]) < EXAMPLE_LIMIT:
                    examples["missing"].append(target)
    digest = hashlib.sha256(
        json.dumps({"old": old, "new": new, "rebased": sorted(rebased.items())}, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "format": MOVE_PLAN_FORMAT,
        "plan_id": f"mv-{digest[:24]}",
        "old_root": old,
        "new_root": new,
        "counts": counts,
        "examples": examples,
        "rebased": rebased,
    }


def apply_move(state: dict[str, Any], plan_id: str, old_root: str, new_root: str) -> dict[str, Any]:
    """Rebase the stored paths and switch the library to portable mode. Caller holds the store lock.

    Recomputes the plan from the state being committed and refuses unless its id matches
    ``plan_id`` (MOVE_PLAN_STALE), so the wizard's preview and the applied change agree.
    """
    from api_errors import MovePlanStale

    plan = plan_move(state.get("games"), old_root, new_root)
    if plan["plan_id"] != plan_id:
        raise MovePlanStale("The library changed since the preview. Run the preview again.")
    rebased = plan["rebased"]
    moved = 0
    for game in state.get("games") or []:
        if not isinstance(game, dict):
            continue
        game_id = str(game.get("game_id") or "")
        for slot, _path in list(_path_slots(game)):
            key = f"{game_id}|{slot}"
            if key in rebased:
                _set_slot(game, slot, rebased[key])
                moved += 1
    settings = state.setdefault("settings", {})
    settings["library_root"] = plan["new_root"]
    settings["path_mode"] = "portable"
    return {"moved": moved, "counts": plan["counts"], "library_root": plan["new_root"], "path_mode": "portable"}
