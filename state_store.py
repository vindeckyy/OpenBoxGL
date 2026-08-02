"""Transactional, process-safe JSON persistence for OpenBox user data."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable


STATE_SCHEMA_VERSION = 3
LEGACY_INDEXED_ID = re.compile(r"^game-[0-9a-f]{24}-\d+$")


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


MIGRATIONS: dict[int, Callable[[dict[str, Any]], None]] = {
    1: _migrate_v1_to_v2,
    2: _migrate_v2_to_v3,
}


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
    if _normalize_game_ids(state["games"]):
        changed = True
    _validate_state(state)
    return state, changed


class JsonStateStore:
    """A small JSON store with atomic commits and a sidecar last-known-good copy."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        self.backup_path = self.path.with_name(f"{self.path.name}.bak")
        self._thread_lock = threading.RLock()
        self._cached_state: dict[str, Any] | None = None
        self._cached_signature: tuple[int, int, int] | None = None

    def _signature(self) -> tuple[int, int, int] | None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        return (stat.st_mtime_ns, stat.st_size, stat.st_ino)

    def _remember(self, state: dict[str, Any]) -> None:
        self._cached_state = copy.deepcopy(state)
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
            return json.load(source)

    def _load_unlocked(self) -> tuple[dict[str, Any], bool]:
        if not self.path.exists():
            return default_state(), False
        try:
            raw = self._read_unlocked(self.path)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
            raise StateCorruptError(
                f"Unable to read {self.path}. The original file was preserved; restore or inspect it before continuing."
            ) from error
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
                return copy.deepcopy(state)

    def recover(self) -> dict[str, Any]:
        with self._thread_lock, self._file_lock(True):
            if not self.backup_path.is_file():
                raise StateCorruptError(f"No last-known-good state exists at {self.backup_path}.")
            try:
                state, _ = normalize_state(self._read_unlocked(self.backup_path))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError, StateCorruptError) as error:
                raise StateCorruptError(f"The last-known-good state is also unusable: {self.backup_path}") from error
            self._write_unlocked(state)
            self._remember(state)
            return copy.deepcopy(state)

    def _write_unlocked(self, state: dict[str, Any]) -> None:
        normalized, _ = normalize_state(state)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_file():
            shutil.copy2(self.path, self.backup_path)
            os.chmod(self.backup_path, 0o600)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(normalized, output, indent=2, ensure_ascii=False)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o600)
            # Write the backup from the temporary first so a failure here
            # cannot leave a fresh primary paired with a stale backup.
            shutil.copy2(temporary, self.backup_path)
            os.chmod(self.backup_path, 0o600)
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            self._remember(normalized)
        finally:
            if temporary.exists():
                temporary.unlink()

    def save(self, state: dict[str, Any]) -> dict[str, Any]:
        with self._thread_lock, self._file_lock(True):
            normalized, _ = normalize_state(state)
            self._write_unlocked(normalized)
            return copy.deepcopy(normalized)

    def update(self, mutator: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
        state, _ = self.update_with_result(mutator)
        return state

    def update_with_result(self, mutator: Callable[[dict[str, Any]], Any]) -> tuple[dict[str, Any], Any]:
        with self._thread_lock, self._file_lock(True):
            state, _ = self._load_unlocked()
            result = mutator(state)
            normalized, _ = normalize_state(state)
            self._write_unlocked(normalized)
            return copy.deepcopy(normalized), result


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
