"""Transactional, process-safe JSON persistence for OpenBox user data."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json as _stdlib_json

try:
    import orjson as _orjson

    def _json_dumps(obj, **kwargs):
        options = 0
        if kwargs.get("sort_keys"):
            options |= _orjson.OPT_SORT_KEYS
        if kwargs.get("indent") == 2:
            options |= _orjson.OPT_INDENT_2
        # orjson always uses compact separators without indent
        return _orjson.dumps(obj, option=options or None).decode("utf-8")

    def _json_dump_file(obj, fp, **kwargs):
        fp.write(_json_dumps(obj, **kwargs))

    def _json_load(fp):
        return _orjson.loads(fp.read())

    _json_decode_error = _orjson.JSONDecodeError

except ImportError:
    _orjson = None

    def _json_dumps(obj, **kwargs):
        return _stdlib_json.dumps(obj, **kwargs)

    def _json_dump_file(obj, fp, **kwargs):
        _stdlib_json.dump(obj, fp, **kwargs)

    def _json_load(fp):
        return _stdlib_json.load(fp)

    _json_decode_error = _stdlib_json.JSONDecodeError

import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from collections.abc import Callable

LOGGER = logging.getLogger("openbox.state")


STATE_SCHEMA_VERSION = 6
COMPACT_JSON_THRESHOLD = 1024 * 1024
LEGACY_INDEXED_ID = re.compile(r"^game-[0-9a-f]{24}-\d+$")
QUEUE_CAP = 500
NOTIFICATIONS_CAP = 200


class StateCorruptError(RuntimeError):
    """Raised when the primary state file cannot be decoded safely."""


def default_state() -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "games": [],
        "profiles": {},
        "history": [],
        "settings": {},
        "playlists": [],
        "queue": [],
        "notifications": [],
        "ui_state": {},
        "active_sessions": [],
    }


def _identity_payload(game: dict[str, Any]) -> dict[str, str]:
    identity = {
        key: str(game.get(key) or "").strip()
        for key in (
            "path", "platform", "steam_app_id", "heroic_app_id",
            "lutris_id", "gameyfin_id", "launchbox_db_id",
        )
    }
    if identity["path"]:
        identity["path"] = os.path.normcase(os.path.normpath(os.path.expanduser(identity["path"])))
    if not identity["path"] and not any(
        identity[key] for key in ("steam_app_id", "heroic_app_id", "lutris_id", "gameyfin_id", "launchbox_db_id")
    ):
        identity["name"] = str(game.get("name") or "").strip()
    return identity


def _stable_game_id(game: dict[str, Any]) -> str:
    """Return a durable id derived from game identity, never list position."""
    raw = json.dumps(_identity_payload(game), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"game-{digest}"


def _is_legacy_indexed_id(value: str) -> bool:
    return bool(LEGACY_INDEXED_ID.fullmatch(value))


def _migrate_v1_to_v2(state: dict[str, Any]) -> None:
    state.setdefault("profiles", {})
    state.setdefault("history", [])
    state.setdefault("settings", {})
    state.setdefault("playlists", [])
    state["schema_version"] = 2


def _migrate_v2_to_v3(state: dict[str, Any]) -> None:
    """Replace the old index-suffixed IDs while retaining aliases."""
    used: set[str] = set()
    for game in state.get("games", []):
        if not isinstance(game, dict):
            continue
        old_id = str(game.get("game_id") or "").strip()
        candidate = _stable_game_id(game)
        if old_id and _is_legacy_indexed_id(old_id):
            aliases = game.setdefault("legacy_game_ids", [])
            if not isinstance(aliases, list):
                aliases = []
                game["legacy_game_ids"] = aliases
            if old_id not in aliases:
                aliases.append(old_id)
        if candidate in used:
            suffix = 2
            while f"{candidate}-{suffix}" in used:
                suffix += 1
            candidate = f"{candidate}-{suffix}"
        game["game_id"] = candidate
        used.add(candidate)
    state["schema_version"] = 3


def _migrate_v3_to_v4(state: dict[str, Any]) -> None:
    """Add the play queue and notification center while preserving unknown fields."""
    if not isinstance(state.get("queue"), list):
        state["queue"] = []
    else:
        state["queue"] = state["queue"][:QUEUE_CAP]
    if not isinstance(state.get("notifications"), list):
        state["notifications"] = []
    else:
        state["notifications"] = state["notifications"][:NOTIFICATIONS_CAP]
    for game in state.get("games", []):
        if isinstance(game, dict) and "tags" in game and not isinstance(game.get("tags"), list):
            game["tags"] = []
    state["schema_version"] = 4


def _migrate_v4_to_v5(state: dict[str, Any]) -> None:
    """Add the host-owned ui_state block while preserving every existing field."""
    if not isinstance(state.get("ui_state"), dict):
        state["ui_state"] = {}
    state["schema_version"] = 5


def _migrate_v5_to_v6(state: dict[str, Any]) -> None:
    """Add active_sessions collection."""
    if not isinstance(state.get("active_sessions"), list):
        state["active_sessions"] = []
    state["schema_version"] = 6


MIGRATIONS: dict[int, Callable[[dict[str, Any]], None]] = {
    1: _migrate_v1_to_v2,
    2: _migrate_v2_to_v3,
    3: _migrate_v3_to_v4,
    4: _migrate_v4_to_v5,
    5: _migrate_v5_to_v6,
}


def _normalize_feature_fields(state: dict[str, Any]) -> bool:
    """Repair feature collections, sessions, and per-game tags; returns True if changed."""
    changed = False
    for key, cap in (("queue", QUEUE_CAP), ("notifications", NOTIFICATIONS_CAP)):
        value = state.get(key)
        if not isinstance(value, list):
            state[key] = []
            changed = True
        elif len(value) > cap:
            state[key] = value[:cap]
            changed = True
    sessions = state.get("active_sessions")
    if not isinstance(sessions, list):
        state["active_sessions"] = []
        changed = True
    else:
        valid_sessions = [session for session in sessions if isinstance(session, dict)]
        if len(valid_sessions) != len(sessions):
            state["active_sessions"] = valid_sessions
            changed = True
    for game in state.get("games", []):
        if isinstance(game, dict) and "tags" in game and not isinstance(game.get("tags"), list):
            game["tags"] = []
            changed = True
    return changed


def _normalize_game_ids(games: list[dict[str, Any]]) -> bool:
    changed = False
    used: set[str] = set()
    for game in games:
        if "legacy_game_ids" in game and not isinstance(game.get("legacy_game_ids"), list):
            game["legacy_game_ids"] = []
            changed = True
        existing = str(game.get("game_id") or "").strip()
        if not existing or _is_legacy_indexed_id(existing):
            candidate = _stable_game_id(game)
            if existing:
                aliases = game.setdefault("legacy_game_ids", [])
                if not isinstance(aliases, list):
                    aliases = []
                    game["legacy_game_ids"] = aliases
                if existing not in aliases:
                    aliases.append(existing)
                    changed = True
        else:
            candidate = existing
        if candidate in used:
            suffix = 2
            base = candidate
            while f"{base}-{suffix}" in used:
                suffix += 1
            candidate = f"{base}-{suffix}"
        if game.get("game_id") != candidate:
            game["game_id"] = candidate
            changed = True
        used.add(candidate)
    return changed


def _apply_migrations(state: dict[str, Any], version: int) -> bool:
    changed = False
    while version < STATE_SCHEMA_VERSION:
        migration = MIGRATIONS.get(version)
        if migration is None:
            raise StateCorruptError(f"No migration is available for schema version {version}.")
        migration(state)
        version = int(state["schema_version"])
        changed = True
    return changed


def _ensure_defaults(state: dict[str, Any]) -> bool:
    changed = False
    defaults = default_state()
    for key, value in defaults.items():
        if key not in state:
            state[key] = copy.deepcopy(value)
            changed = True
    return changed


def _validate_state(state: dict[str, Any]) -> None:
    if not isinstance(state["games"], list):
        raise StateCorruptError("OpenBox library.json has an invalid games collection.")
    for index, game in enumerate(state["games"]):
        if not isinstance(game, dict):
            raise StateCorruptError(f"OpenBox library.json has an invalid game at index {index}.")
        if not str(game.get("game_id") or "").strip():
            raise StateCorruptError(f"OpenBox library.json has a game without an identity at index {index}.")


def normalize_state(raw: Any) -> tuple[dict[str, Any], bool]:
    """Normalize legacy state through explicit migrations while retaining unknown fields."""
    changed = False
    if isinstance(raw, list):
        state: dict[str, Any] = {"games": raw, "schema_version": 1}
        changed = True
    elif isinstance(raw, dict):
        state = copy.deepcopy(raw)
    else:
        raise StateCorruptError("OpenBox library.json must contain an object or legacy game list.")

    try:
        version = int(state.get("schema_version", 1))
    except (TypeError, ValueError):
        version = 1
        changed = True
    if version > STATE_SCHEMA_VERSION or version < 1:
        raise StateCorruptError(f"OpenBox library.json uses unsupported schema version {version}.")
    if "games" in state and not isinstance(state.get("games"), list):
        raise StateCorruptError("OpenBox library.json has an invalid games collection.")
    changed |= _apply_migrations(state, version)
    changed |= _ensure_defaults(state)
    state["schema_version"] = STATE_SCHEMA_VERSION
    if not isinstance(state.get("games"), list):
        raise StateCorruptError("OpenBox library.json has an invalid games collection.")
    if _normalize_feature_fields(state):
        changed = True
    if _normalize_game_ids(state["games"]):
        changed = True
    _validate_state(state)
    return state, changed


class JsonStateStore:
    """A small JSON store with atomic commits and a sidecar last-known-good copy."""

    def __init__(self, path: Path, snapshot_limit: int = 5):
        self.path = Path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        self.backup_path = self.path.with_name(f"{self.path.name}.bak")
        self.snapshots_dir = self.path.with_name(f"{self.path.name}.snapshots")
        self.snapshot_limit = max(0, int(snapshot_limit))
        self._thread_lock = threading.RLock()
        self._cached_state: dict[str, Any] | None = None
        self._cached_signature: tuple[int, int, int] | None = None

    def _signature(self) -> tuple[int, int, int] | None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        return (stat.st_mtime_ns, stat.st_size, stat.st_ino)

    def signature(self) -> tuple[int, int, int] | None:
        """Return a cheap content signature; None when the primary file is absent."""
        return self._signature()

    def _remember(self, state: dict[str, Any], adopt: bool = False) -> None:
        self._cached_state = state if adopt else copy.deepcopy(state)
        self._cached_signature = self._signature()

    @contextmanager
    def _file_lock(self, exclusive: bool):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_unlocked(self, path: Path) -> Any:
        with path.open("r", encoding="utf-8") as source:
            return _json_load(source)

    def _load_unlocked(self) -> tuple[dict[str, Any], bool]:
        if not self.path.exists():
            return default_state(), False
        try:
            raw = self._read_unlocked(self.path)
        except (OSError, _json_decode_error, UnicodeDecodeError) as error:
            raise StateCorruptError(
                f"Unable to read {self.path}. The original file was preserved; restore or inspect it before continuing."
            ) from error
        if (
            isinstance(raw, dict)
            and raw.get("schema_version") == STATE_SCHEMA_VERSION
            and all(key in raw for key in ("games", "profiles", "history", "settings", "playlists", "ui_state"))
            and isinstance(raw.get("games"), list)
            and isinstance(raw.get("queue"), list)
            and len(raw["queue"]) <= QUEUE_CAP
            and isinstance(raw.get("notifications"), list)
            and len(raw["notifications"]) <= NOTIFICATIONS_CAP
            and isinstance(raw.get("ui_state"), dict)
            and isinstance(raw.get("active_sessions"), list)
            and all(isinstance(session, dict) for session in raw["active_sessions"])
            and all(
                isinstance(game, dict)
                and str(game.get("game_id") or "").strip()
                and not _is_legacy_indexed_id(str(game.get("game_id") or ""))
                and ("tags" not in game or isinstance(game.get("tags"), list))
                for game in raw["games"]
            )
        ):
            return raw, False
        return normalize_state(raw)

    def load(self) -> dict[str, Any]:
        with self._thread_lock:
            signature = self._signature()
            if self._cached_state is not None and signature == self._cached_signature:
                return copy.deepcopy(self._cached_state)
            with self._file_lock(True):
                state, changed = self._load_unlocked()
                if changed:
                    self._write_unlocked(state)
                self._remember(state)
                return state

    def load_readonly(self) -> dict[str, Any]:
        """Return the cached state without copying. Callers must not mutate the result."""
        with self._thread_lock:
            signature = self._signature()
            if self._cached_state is not None and signature == self._cached_signature:
                return self._cached_state
            with self._file_lock(True):
                state, changed = self._load_unlocked()
                if changed:
                    self._write_unlocked(state)
                self._remember(state)
                return self._cached_state

    def recover(self) -> dict[str, Any]:
        with self._thread_lock, self._file_lock(True):
            if not self.backup_path.is_file():
                raise StateCorruptError(f"No last-known-good state exists at {self.backup_path}.")
            try:
                state, _ = normalize_state(self._read_unlocked(self.backup_path))
            except (OSError, _json_decode_error, UnicodeDecodeError, StateCorruptError) as error:
                raise StateCorruptError(f"The last-known-good state is also unusable: {self.backup_path}") from error
            self._write_unlocked(state)
            self._remember(state)
            return state

    def _write_unlocked(self, state: dict[str, Any], adopt: bool = False) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        compact = _json_dumps(state, separators=(",", ":"), ensure_ascii=False).encode()
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                if len(compact) > COMPACT_JSON_THRESHOLD:
                    output.write(compact.decode("utf-8"))
                    output.write("\n")
                else:
                    _json_dump_file(state, output, indent=2, ensure_ascii=False)
                    output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o600)
            # Backup first: a failure must not pair a fresh primary with a
            # stale backup. The backup mirrors the latest committed state
            # (previous versions live in snapshots); stage it atomically so
            # an interrupted backup copy can never leave a corrupt file.
            backup_tmp = self.backup_path.with_name(self.backup_path.name + ".tmp")
            shutil.copy2(temporary, backup_tmp)
            os.chmod(backup_tmp, 0o600)
            os.replace(backup_tmp, self.backup_path)
            os.chmod(self.backup_path, 0o600)
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            self._remember(state, adopt=adopt)
            self._rotate_snapshots()
        finally:
            if temporary.exists():
                temporary.unlink()

    def _rotate_snapshots(self) -> None:
        """Keep the last N committed states as timestamped recovery copies; best-effort."""
        if not self.snapshot_limit:
            return
        try:
            self.snapshots_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            target = self.snapshots_dir / f"{stamp}-{secrets.token_hex(4)}.json"
            shutil.copy2(self.path, target)
            os.chmod(target, 0o600)
            existing = []
            for path in self.snapshots_dir.glob("*.json"):
                try:
                    existing.append((path.stat().st_mtime, path))
                except OSError:
                    continue
            existing.sort(key=lambda item: item[0])
            for _, stale in existing[: max(0, len(existing) - self.snapshot_limit)]:
                stale.unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("Snapshot rotation failed; the committed state is unaffected.")

    def snapshots(self) -> list[dict[str, Any]]:
        """Return available state snapshots, newest first."""
        items = []
        try:
            paths = list(self.snapshots_dir.glob("*.json"))
        except OSError:
            return items
        for path in paths:
            try:
                st = path.stat()
                items.append({"name": path.name, "size": st.st_size, "modified": st.st_mtime})
            except OSError:
                continue
        items.sort(key=lambda item: item["modified"], reverse=True)
        return items

    def restore_snapshot(self, name: str) -> dict[str, Any]:
        """Restore a named snapshot over the primary state file."""
        candidate = self.snapshots_dir / Path(name).name
        if not candidate.is_file() or candidate.parent != self.snapshots_dir:
            raise StateCorruptError(f"Unknown snapshot: {name}")
        with self._thread_lock, self._file_lock(True):
            state, _ = normalize_state(self._read_unlocked(candidate))
            self._write_unlocked(state)
            self._remember(state)
            return state

    def save(self, state: dict[str, Any]) -> dict[str, Any]:
        with self._thread_lock, self._file_lock(True):
            normalized, _ = normalize_state(state)
            self._write_unlocked(normalized, adopt=True)
            return normalized

    def update(self, mutator: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
        """Apply a mutation and return a detached snapshot of the committed state."""
        state, _ = self.update_with_result(mutator)
        return copy.deepcopy(state)

    def update_with_result(self, mutator: Callable[[dict[str, Any]], Any]) -> tuple[dict[str, Any], Any]:
        """Apply a mutation under the process lock; the returned state is cache-owned, read-only for the caller."""
        with self._thread_lock, self._file_lock(True):
            state, _ = self._load_unlocked()
            result = mutator(state)
            self._write_unlocked(state, adopt=True)
            return state, result


def secure_text_write(path: Path, value: str) -> None:
    """Write a credential or token file atomically with owner-only permissions."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()
