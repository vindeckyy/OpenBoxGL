"""Transactional, process-safe JSON persistence for OpenBox user data."""

from __future__ import annotations

import copy
import hashlib
import json
import json as _stdlib_json
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import types
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend_io import fsync_directory
from pkg.platform_compat import lock_handle

# Persistence is stdlib-only, deliberately. An earlier revision swapped in
# ``orjson`` behind ``try: import orjson except ImportError``, which made the
# serializer -- and therefore the behaviour of the single most important write
# path in the app -- depend on whether a third-party package happened to be
# installed on the host. The two serializers do not agree: ``orjson.dumps``
# raises ``TypeError`` on a non-str dict key where the stdlib coerces it, and it
# writes ``NaN`` as ``null`` where the stdlib emits a bare ``NaN``. No CI job
# installed ``orjson``, so neither branch was ever exercised. ``scripts/
# check_dependencies.py`` now fails the gate if this ever comes back.
def _json_dumps(obj, **kwargs):
    return _stdlib_json.dumps(obj, **kwargs)


def _json_dumps_bytes(obj, **kwargs) -> bytes:
    return _stdlib_json.dumps(obj, **kwargs).encode("utf-8")


def _json_dump_file(obj, fp, **kwargs):
    _stdlib_json.dump(obj, fp, **kwargs)


def _json_load(fp):
    return _stdlib_json.load(fp)


_json_decode_error = _stdlib_json.JSONDecodeError

LOGGER = logging.getLogger("openbox.state")


STATE_SCHEMA_VERSION = 6

#: Snapshot filenames: <UTC timestamp>-<8-digit sequence>-<random>.json, where
#: the timestamp is %Y%m%dT%H%M%S%fZ -- 8 digits, T, 12 digits, Z. The
#: timestamp alone is not enough to order by (two writes can land in one tick)
#: and ordering by the file's mtime is worse, because snapshots are hard links
#: to the live file and so share its timestamps.
_SNAPSHOT_NAME_RE = re.compile(r"^(\d{8}T\d{12}Z)-(\d{8})-[0-9a-f]{8}\.json$")
COMPACT_JSON_THRESHOLD = 1024 * 1024
LEGACY_INDEXED_ID = re.compile(r"^game-[0-9a-f]{24}-\d+$")
QUEUE_CAP = 500
NOTIFICATIONS_CAP = 200
TRASH_CAP = 200
TRASH_MAX_AGE_DAYS = 30
SNAPSHOT_DEBOUNCE_DEFAULT = float(os.environ.get("OPENBOX_SNAPSHOT_DEBOUNCE", "0.0"))
WRITE_COALESCE_WINDOW = 0.05
WRITE_COALESCE_MS = 50


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
        "trash": [],
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


def _trash_entry_expired(entry: dict[str, Any], cutoff: float) -> bool:
    """Return True when a trash entry's ``trashed_at`` is older than the retention window."""
    raw = entry.get("trashed_at")
    if not isinstance(raw, str):
        return False
    try:
        return datetime.fromisoformat(raw).timestamp() < cutoff
    except (ValueError, OverflowError, OSError):
        return False


def prune_trash(trash: list[Any], *, now: float | None = None) -> list[Any]:
    """Return the bounded trash list: dict entries only, within retention, newest TRASH_CAP kept."""
    cutoff = (now if now is not None else time.time()) - TRASH_MAX_AGE_DAYS * 86400
    return [
        entry for entry in trash
        if isinstance(entry, dict) and not _trash_entry_expired(entry, cutoff)
    ][-TRASH_CAP:]


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
    trash = state.get("trash")
    if not isinstance(trash, list):
        state["trash"] = []
        changed = True
    else:
        kept = prune_trash(trash)
        if len(kept) != len(trash):
            state["trash"] = kept
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
        if not isinstance(game, dict):
            continue
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


def _state_digest(state: dict[str, Any]) -> bytes:
    """A stable content digest of a normalized state.

    Used to answer "did this mutation actually change anything?" cheaply. The
    auto-import driver runs every ten seconds on a default install and very
    often finds nothing new; before this, each of those ticks serialized the
    whole library, fsynced it, copied the entire file to the backup, renamed
    twice, fsynced the directory and rotated a snapshot -- roughly 8,640 full
    writes a day, each while holding the cross-process lock every other state
    read has to acquire.

    The serialization is the one cost this adds, and it is far cheaper than the
    disk work it replaces: normalization already walks every game on this path.
    ``sort_keys`` keeps the digest independent of dict ordering.
    """
    canonical = _stdlib_json.dumps(
        state, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return hashlib.blake2b(canonical.encode("utf-8"), digest_size=16).digest()


def _normalize_committed_state(state: dict[str, Any]) -> dict[str, Any]:
    """Validate and repair a state already loaded from the current schema.

    Transactions normally start from the store's validated in-memory state.
    Calling ``normalize_state`` here would deep-copy every game before every
    single-field write.  Keep the same validation and repair guarantees while
    doing the common current-schema path in place; legacy or malformed roots
    still use the full normalizer.
    """
    if not isinstance(state, dict):
        raise StateCorruptError("OpenBox state must be an object.")
    try:
        version = int(state.get("schema_version", 1))
    except (TypeError, ValueError):
        version = 1
    required = ("games", "profiles", "history", "settings", "playlists", "ui_state")
    if version != STATE_SCHEMA_VERSION or any(key not in state for key in required):
        return normalize_state(state)[0]
    state.setdefault("schema_version", STATE_SCHEMA_VERSION)
    if _normalize_feature_fields(state):
        # The repair is already applied in place.
        pass
    if not isinstance(state.get("games"), list):
        raise StateCorruptError("OpenBox library.json has an invalid games collection.")
    _normalize_game_ids(state["games"])
    _validate_state(state)
    return state


def _transaction_owned_containers(state: dict[str, Any]) -> tuple[dict[str, int], dict[int, set[int]]]:
    """Index shallow container ownership without copying the catalog."""
    roots: dict[str, int] = {}
    children: dict[int, set[int]] = {}

    def register(value: Any) -> None:
        if not isinstance(value, (dict, list)) or id(value) in children:
            return
        values = value.values() if isinstance(value, dict) else value
        children[id(value)] = {
            id(child) for child in values if isinstance(child, (dict, list))
        }
        for child in values:
            register(child)

    for key, value in state.items():
        if not isinstance(value, (dict, list)):
            continue
        roots[key] = id(value)
        register(value)
    return roots, children


def _detach_new_mutables(
    state: dict[str, Any],
    owned: tuple[dict[str, int], dict[int, set[int]]],
) -> None:
    """Detach mutator-introduced records while retaining the warm write path."""
    roots, children = owned

    def detach(value: Any, seen: set[int] | None = None) -> None:
        known = children.get(id(value))
        if known is None:
            return
        if seen is None:
            seen = set()
        if id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, dict):
            items = list(value.items())
            for key, child in items:
                if not isinstance(child, (dict, list)):
                    continue
                if id(child) not in known:
                    value[key] = copy.deepcopy(child)
                else:
                    detach(child, seen)
        else:
            for index, child in enumerate(list(value)):
                if not isinstance(child, (dict, list)):
                    continue
                if id(child) not in known:
                    value[index] = copy.deepcopy(child)
                else:
                    detach(child, seen)

    for key, value in list(state.items()):
        if not isinstance(value, (dict, list)):
            continue
        if id(value) != roots.get(key):
            state[key] = copy.deepcopy(value)
            continue
        detach(value)


class JsonStateStore:
    """A high-performance JSON store with in-memory caching, indexing, and atomic commits."""

    def __init__(self, path: Path, snapshot_limit: int = 5, snapshot_debounce: float = SNAPSHOT_DEBOUNCE_DEFAULT):
        self.path = Path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        self.backup_path = self.path.with_name(f"{self.path.name}.bak")
        self.snapshots_dir = self.path.with_name(f"{self.path.name}.snapshots")
        self.snapshot_limit = max(0, int(snapshot_limit))
        self.snapshot_debounce = max(0.0, float(snapshot_debounce))
        self._last_snapshot_time: float = 0.0
        # Monotonic counter embedded in snapshot filenames, so "newest" is a
        # property of the name rather than of the filesystem's mtime.
        self._snapshot_sequence: int = 0
        self._thread_lock = threading.RLock()
        self._cached_state: dict[str, Any] | None = None
        self._cached_signature: tuple[int, int, int] | None = None
        # Content digest of the state as last committed to disk. Compared after
        # a mutation so a mutator that changed nothing costs no fsync, no backup
        # copy and no snapshot rotation. See update_with_result.
        self._committed_digest: bytes | None = None
        self._games_by_id: dict[str, dict[str, Any]] = {}
        self._games_by_platform: dict[str, list[dict[str, Any]]] = {}
        # Write coalesce: 50ms micro-batch, single fsync per batch
        self._coalesce_window = WRITE_COALESCE_WINDOW
        self._coalesce_lock = threading.Lock()
        self._coalesce_pending_state: dict[str, Any] | None = None
        self._coalesce_timer: threading.Timer | None = None
        self._coalesce_last_flush: float = 0.0

    def _signature(self) -> tuple[int, int, int] | None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        return (stat.st_mtime_ns, stat.st_size, stat.st_ino)

    def signature(self) -> tuple[int, int, int] | None:
        """Return a cheap content signature; None when the primary file is absent."""
        return self._signature()

    def _reindex(self, state: dict[str, Any] | None) -> None:
        if state is None:
            self._games_by_id = {}
            self._games_by_platform = {}
            return
        games_by_id: dict[str, dict[str, Any]] = {}
        games_by_platform: dict[str, list[dict[str, Any]]] = {}
        games = state.get("games")
        if isinstance(games, list):
            for game in games:
                if isinstance(game, dict):
                    gid = str(game.get("game_id") or "").strip()
                    if gid:
                        games_by_id[gid] = game
                    platform = str(game.get("platform") or "").strip()
                    if platform:
                        games_by_platform.setdefault(platform, []).append(game)
        self._games_by_id = games_by_id
        self._games_by_platform = games_by_platform

    def _remember(self, state: dict[str, Any], adopt: bool = False) -> None:
        if adopt:
            games = state.get("games")
            if isinstance(games, list):
                self._cached_state = {
                    **state,
                    "games": [dict(g) if isinstance(g, dict) else g for g in games],
                    "profiles": dict(state.get("profiles", {})),
                    "settings": dict(state.get("settings", {})),
                    "history": list(state.get("history", [])),
                    "playlists": list(state.get("playlists", [])),
                    "queue": list(state.get("queue", [])),
                    "notifications": list(state.get("notifications", [])),
                    "ui_state": dict(state.get("ui_state", {})),
                    "active_sessions": list(state.get("active_sessions", [])),
                }
            else:
                self._cached_state = copy.deepcopy(state)
        else:
            self._cached_state = copy.deepcopy(state)
        self._cached_signature = self._signature()
        self._reindex(self._cached_state)

    def _clear_cache(self) -> None:
        self._cached_state = None
        self._cached_signature = None
        self._committed_digest = None
        self._reindex(None)

    @property
    def games_by_id(self) -> dict[str, dict[str, Any]]:
        """Return the primary index mapping game_id -> game dict."""
        with self._thread_lock:
            self._ensure_loaded()
            return self._games_by_id

    @property
    def games_by_platform(self) -> dict[str, list[dict[str, Any]]]:
        """Return the index mapping platform -> list of game dicts."""
        with self._thread_lock:
            self._ensure_loaded()
            return self._games_by_platform

    def get_game_by_id(self, game_id: str) -> dict[str, Any] | None:
        """O(1) lookup of a game by game_id."""
        with self._thread_lock:
            self._ensure_loaded()
            return self._games_by_id.get(str(game_id).strip())

    def get_games_by_platform(self, platform: str) -> list[dict[str, Any]]:
        """O(1) lookup of games by platform."""
        with self._thread_lock:
            self._ensure_loaded()
            return list(self._games_by_platform.get(str(platform).strip(), []))

    def _ensure_loaded(self) -> None:
        signature = self._signature()
        if self._cached_state is None or signature != self._cached_signature:
            with self._file_lock(True):
                state, changed = self._load_unlocked()
                if changed:
                    self._write_unlocked(state)
                self._remember(state)

    def _ensure_data_parent(self) -> None:
        parent = self.path.parent
        if parent.exists():
            return
        parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _file_lock(self, exclusive: bool):
        self._ensure_data_parent()
        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            with lock_handle(lock_file, exclusive=exclusive):
                yield

    def _read_unlocked(self, path: Path) -> Any:
        with path.open("r", encoding="utf-8") as source:
            return _stdlib_json.load(source)

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
                return copy.deepcopy(state)

    def load_readonly(self) -> dict[str, Any]:
        """Return a shallow-frozen view of cached state. Callers cannot mutate top-level keys."""
        with self._thread_lock:
            signature = self._signature()
            if self._cached_state is not None and signature == self._cached_signature:
                return types.MappingProxyType(self._cached_state)
            with self._file_lock(True):
                state, changed = self._load_unlocked()
                if changed:
                    self._write_unlocked(state)
                self._remember(state)
                return types.MappingProxyType(self._cached_state)

    def recover(self) -> dict[str, Any]:
        with self._thread_lock, self._file_lock(True):
            if not self.backup_path.is_file():
                raise StateCorruptError(f"No last-known-good state exists at {self.backup_path}.")
            try:
                state, _ = normalize_state(self._read_unlocked(self.backup_path))
            except (OSError, _json_decode_error, UnicodeDecodeError, StateCorruptError) as error:
                raise StateCorruptError(f"The last-known-good state is also unusable: {self.backup_path}") from error
            self._write_unlocked(state, adopt=True)
            return state

    def _write_unlocked(
        self,
        state: dict[str, Any],
        adopt: bool = False,
        *,
        reuse_cache: bool = False,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            games = state.get("games")
            games_count = len(games) if isinstance(games, list) else 0
            if games_count > 500:
                raw_bytes = _stdlib_json.dumps(state, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            else:
                pretty = _stdlib_json.dumps(state, indent=2, ensure_ascii=False).encode("utf-8")
                if len(pretty) > COMPACT_JSON_THRESHOLD:
                    raw_bytes = _stdlib_json.dumps(state, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                else:
                    raw_bytes = pretty

            with os.fdopen(fd, "wb") as output:
                output.write(raw_bytes)
                output.write(b"\n")
                output.flush()
                os.fsync(output.fileno())

            os.chmod(temporary, 0o600)
            # Backup first with a separate inode: a failure or corruption of primary must not affect backup.
            backup_tmp = self.backup_path.with_name(f".{self.backup_path.name}.{secrets.token_hex(4)}.tmp")
            try:
                shutil.copy2(temporary, backup_tmp)
                os.chmod(backup_tmp, 0o600)
                os.replace(backup_tmp, self.backup_path)
                os.chmod(self.backup_path, 0o600)
            finally:
                if backup_tmp.exists():
                    backup_tmp.unlink(missing_ok=True)

            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            fsync_directory(self.path.parent)
            self._rotate_snapshots()
            if reuse_cache:
                # New caller-owned containers were detached before the write;
                # retaining this validated transaction object avoids copying
                # every game dictionary on the warm path.
                self._cached_state = state
                self._cached_signature = self._signature()
                self._reindex(state)
            else:
                self._remember(state, adopt=adopt)
            # The file now holds exactly this state, whatever wrote it -- save,
            # update, snapshot restore or corrupt-state recovery. Recording it
            # here rather than at the update call site means the first no-op
            # after any of those is recognised as a no-op too.
            self._committed_digest = _state_digest(state)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _rotate_snapshots(self, force: bool = False) -> None:
        """Keep the last N committed states as recovery copies; debounced and zero-copy."""
        if not self.snapshot_limit:
            return
        now = time.monotonic()
        if not force and self.snapshot_debounce > 0 and self._last_snapshot_time > 0 and (now - self._last_snapshot_time) < self.snapshot_debounce:
            return
        try:
            self.snapshots_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            # The sequence number makes the ordering intrinsic to the name
            # instead of a filesystem property. Snapshots are hard links to the
            # live file, so two writes landing inside one mtime tick produced
            # two snapshots the sort could not distinguish -- `snapshots()`
            # ordered by st_mtime and "newest first" silently became arbitrary,
            # which made restoring "the snapshot from before the bad write"
            # restore the wrong one. See snapshots().
            sequence = self._snapshot_sequence = self._snapshot_sequence + 1
            target = self.snapshots_dir / f"{stamp}-{sequence:08d}-{secrets.token_hex(4)}.json"
            try:
                os.link(self.path, target)
            except OSError:
                shutil.copy2(self.path, target)
            os.chmod(target, 0o600)
            self._last_snapshot_time = now
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
        """Return available state snapshots, newest first.

        Ordered by the timestamp and sequence embedded in the filename, not by
        ``st_mtime``. A snapshot is a hard link to the live state file, so
        several can share an mtime and "newest" becomes ambiguous -- which is
        how a restore could land on the wrong recovery point. A name that does
        not parse (hand-placed, or from a build with the old format) falls back
        to mtime, so old snapshot directories still list correctly.
        """
        items = []
        try:
            paths = list(self.snapshots_dir.glob("*.json"))
        except OSError:
            return items
        for path in paths:
            try:
                st = path.stat()
            except OSError:
                continue
            match = _SNAPSHOT_NAME_RE.match(path.name)
            order = (match.group(1), match.group(2)) if match else None
            items.append({
                "name": path.name,
                "size": st.st_size,
                "modified": st.st_mtime,
                "_order": order,
            })
        named = [item for item in items if item["_order"] is not None]
        unnamed = [item for item in items if item["_order"] is None]
        named.sort(key=lambda item: item["_order"], reverse=True)
        unnamed.sort(key=lambda item: item["modified"], reverse=True)
        # Anything from the old format is older than anything from this one, and
        # its relative order among its peers is by mtime.
        ordered = named + unnamed
        for item in ordered:
            del item["_order"]
        return ordered

    def restore_snapshot(self, name: str) -> dict[str, Any]:
        """Restore a named snapshot over the primary state file."""
        candidate = self.snapshots_dir / Path(name).name
        if not candidate.is_file() or candidate.parent != self.snapshots_dir:
            raise StateCorruptError(f"Unknown snapshot: {name}")
        with self._thread_lock, self._file_lock(True):
            state, _ = normalize_state(self._read_unlocked(candidate))
            self._write_unlocked(state, adopt=True)
            return state

    def save(self, state: dict[str, Any]) -> dict[str, Any]:
        with self._thread_lock, self._file_lock(True):
            try:
                normalized, _ = normalize_state(state)
                self._write_unlocked(normalized, adopt=True)
                return normalized
            except Exception:
                self._clear_cache()
                raise

    def update(self, mutator: Callable[[dict[str, Any]], Any], *, isolate: bool = True) -> dict[str, Any]:
        """Apply a mutation and return a detached snapshot of the committed state."""
        state, _ = self.update_with_result(mutator, isolate=isolate)
        return copy.deepcopy(state)

    def _flush_coalesced(self) -> None:
        """Flush the pending coalesced state with a single fsync."""
        with self._coalesce_lock:
            state = self._coalesce_pending_state
            self._coalesce_pending_state = None
            self._coalesce_timer = None
            self._coalesce_last_flush = time.monotonic()
        if state is not None:
            with self._thread_lock, self._file_lock(True):
                self._write_unlocked(state, adopt=True)

    def flush_coalesced(self) -> None:
        """For testing: immediately flush any pending coalesced write."""
        if self._coalesce_pending_state is not None:
            if self._coalesce_timer is not None:
                try:
                    self._coalesce_timer.cancel()
                except Exception:
                    pass
            self._flush_coalesced()

    def coalesced_update(self, mutator: Callable[[dict[str, Any]], Any]) -> Any:
        """Apply mutation via 50ms micro-batch coalesce; single fsync per batch."""
        with self._thread_lock:
            self._ensure_loaded()
            # mutator works on the cached state directly (like update_with_result)
            result = mutator(self._cached_state)
            normalized, _ = normalize_state(self._cached_state)
            # keep in-memory state updated immediately for read-your-writes
            self._remember(normalized, adopt=True)
            pending = copy.deepcopy(normalized)
        with self._coalesce_lock:
            self._coalesce_pending_state = pending
            if self._coalesce_timer is None or not self._coalesce_timer.is_alive():
                # schedule flush after window
                self._coalesce_timer = threading.Timer(self._coalesce_window, self._flush_coalesced)
                self._coalesce_timer.daemon = True
                self._coalesce_timer.start()
        return result

    def coalesced_update_with_result(self, mutator: Callable[[dict[str, Any]], Any]) -> tuple[dict[str, Any], Any]:
        """Coalesced variant that returns state snapshot and mutator result."""
        with self._thread_lock:
            self._ensure_loaded()
            result = mutator(self._cached_state)
            normalized, _ = normalize_state(self._cached_state)
            self._remember(normalized, adopt=True)
            pending = copy.deepcopy(normalized)
        with self._coalesce_lock:
            self._coalesce_pending_state = pending
            if self._coalesce_timer is None or not self._coalesce_timer.is_alive():
                self._coalesce_timer = threading.Timer(self._coalesce_window, self._flush_coalesced)
                self._coalesce_timer.daemon = True
                self._coalesce_timer.start()
        return copy.deepcopy(normalized), result

    def update_with_result(
        self,
        mutator: Callable[[dict[str, Any]], Any],
        *,
        isolate: bool = True,
    ) -> tuple[dict[str, Any], Any]:
        """Apply a mutation under the process lock.

        Direct callers get the historical transaction isolation by default.
        The application-level wrapper uses ``isolate=False`` because its
        mutators are internal and the HTTP boundary cannot retain Python
        object aliases; this keeps large-library writes on the warm path.
        """
        with self._thread_lock, self._file_lock(True):
            signature = self._signature()
            if self._cached_state is not None and signature == self._cached_signature:
                state = self._cached_state
            else:
                state, _ = self._load_unlocked()
            owned_containers = _transaction_owned_containers(state) if isolate else None
            try:
                result = mutator(state)
                normalized = _normalize_committed_state(state)
                if owned_containers is not None:
                    _detach_new_mutables(normalized, owned_containers)
                if isinstance(result, (dict, list, tuple, set)):
                    result = copy.deepcopy(result)
                digest = _state_digest(normalized)
                if self._committed_digest == digest and self._cached_state is not None:
                    # The mutation was a no-op. Skip the write, the backup copy,
                    # the snapshot rotation and the signature churn -- the last
                    # of which matters beyond disk I/O, because the
                    # signature-keyed caches missed on every browser refresh and
                    # the plugin library hook re-ran per request.
                    #
                    # The file on disk is already exactly this state, so the
                    # in-memory cache stays valid; only the index needs to
                    # catch up with the normalized containers.
                    self._cached_state = normalized
                    self._cached_signature = self._signature()
                    self._reindex(normalized)
                else:
                    self._write_unlocked(normalized, adopt=True, reuse_cache=True)
                self._coalesce_last_flush = time.monotonic()
                return normalized, result
            except Exception:
                self._clear_cache()
                raise


def secure_text_write(path: Path, value: str) -> None:
    """Write a credential or token file atomically with owner-only permissions."""
    from backend_io import atomic_write_text
    atomic_write_text(path, value, mode=0o600)
