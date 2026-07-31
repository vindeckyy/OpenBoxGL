"""Transactional, process-safe JSON persistence for OpenBox user data."""

from __future__ import annotations

import copy
import fcntl
import json
import os
import shutil
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable


STATE_SCHEMA_VERSION = 2


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


def _stable_game_id(game: dict[str, Any], index: int) -> str:
    """Return a durable id for a game, including legacy records without one."""
    for key in ("game_id", "id"):
        value = str(game.get(key) or "").strip()
        if value and key == "game_id":
            return value
    identity = {
        key: str(game.get(key) or "").strip()
        for key in (
            "path", "name", "platform", "steam_app_id", "heroic_app_id",
            "lutris_id", "gameyfin_id", "launchbox_db_id",
        )
    }
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    import hashlib

    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"game-{digest}-{index}"


def normalize_state(raw: Any) -> tuple[dict[str, Any], bool]:
    """Normalize legacy state while retaining every unknown field."""
    changed = False
    if isinstance(raw, list):
        state: dict[str, Any] = {"games": raw}
        changed = True
    elif isinstance(raw, dict):
        state = copy.deepcopy(raw)
    else:
        raise StateCorruptError("OpenBox library.json must contain an object or legacy game list.")

    defaults = default_state()
    for key, value in defaults.items():
        if key not in state:
            state[key] = copy.deepcopy(value)
            changed = True

    try:
        version = int(state.get("schema_version", 1))
    except (TypeError, ValueError):
        version = 1
        changed = True
    if version > STATE_SCHEMA_VERSION:
        raise StateCorruptError(
            f"OpenBox library.json uses unsupported schema version {version}."
        )
    if version != STATE_SCHEMA_VERSION:
        state["schema_version"] = STATE_SCHEMA_VERSION
        changed = True

    if not isinstance(state["games"], list):
        raise StateCorruptError("OpenBox library.json has an invalid games collection.")
    for index, game in enumerate(state["games"]):
        if not isinstance(game, dict):
            raise StateCorruptError(f"OpenBox library.json has an invalid game at index {index}.")
        game_id = _stable_game_id(game, index)
        if game.get("game_id") != game_id:
            game["game_id"] = game_id
            changed = True
    return state, changed


class JsonStateStore:
    """A small JSON store with atomic commits and a sidecar last-known-good copy."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        self.backup_path = self.path.with_name(f"{self.path.name}.bak")
        self._thread_lock = threading.RLock()

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
        with self._thread_lock, self._file_lock(True):
            state, changed = self._load_unlocked()
            if changed:
                self._write_unlocked(state)
            return state

    def recover(self) -> dict[str, Any]:
        with self._thread_lock, self._file_lock(True):
            if not self.backup_path.is_file():
                raise StateCorruptError(f"No last-known-good state exists at {self.backup_path}.")
            try:
                state, _ = normalize_state(self._read_unlocked(self.backup_path))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError, StateCorruptError) as error:
                raise StateCorruptError(f"The last-known-good state is also unusable: {self.backup_path}") from error
            self._write_unlocked(state)
            return state

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
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            shutil.copy2(self.path, self.backup_path)
            os.chmod(self.backup_path, 0o600)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary.exists():
                temporary.unlink()

    def save(self, state: dict[str, Any]) -> dict[str, Any]:
        with self._thread_lock, self._file_lock(True):
            normalized, _ = normalize_state(state)
            self._write_unlocked(normalized)
            return normalized

    def update(self, mutator: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
        with self._thread_lock, self._file_lock(True):
            state, _ = self._load_unlocked()
            mutator(state)
            normalized, _ = normalize_state(state)
            self._write_unlocked(normalized)
            return normalized


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
    finally:
        if temporary.exists():
            temporary.unlink()
