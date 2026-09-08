"""Pure, causal catalog synchronization for OpenBox libraries.

The module deliberately has two small boundaries.  ``record_local_changes`` and
the merge functions operate on caller-owned dictionaries and perform no I/O.
The transport helpers read and write immutable, content-addressed event files
under ``openbox-library-v3``.  Paths, launch configuration, credentials,
statistics, and unknown fields never cross either boundary.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SYNC_METADATA_KEY = "library_sync"
SYNC_FORMAT = 3
SYNC_DIRECTORY = "openbox-library-v3"
EVENT_DIRECTORY = "events"
MAX_EVENT_BYTES = 1024 * 1024
MAX_EVENTS = 50_000
MAX_CATALOG_BYTES = 256 * 1024

# This is intentionally explicit.  Adding a field is a protocol change and
# should be reviewed with its ownership and privacy implications.
CATALOG_FIELDS = (
    "game_id", "name", "sort_title", "alternate_names", "platform", "genre",
    "year", "developer", "publisher", "series", "region", "esrb",
    "max_players", "description", "notes", "wikipedia_url", "source",
    "steam_app_id", "heroic_app_id", "lutris_id", "gameyfin_id", "igdb_id",
    "ra_game_id", "launchbox_db_id", "disc_count", "rom_name", "clone_of",
    "set_type", "manual_entry", "tags", "library_sync_id",
)
CATALOG_FIELD_SET = frozenset(CATALOG_FIELDS)
PROVIDER_ID_FIELDS = (
    "steam_app_id", "heroic_app_id", "lutris_id", "gameyfin_id", "igdb_id",
    "ra_game_id", "launchbox_db_id",
)
# This is deliberately local metadata.  A provider ID only becomes a
# cross-device identity after an explicit reconciliation step; imported
# provider fields by themselves are evidence, not permission to link records.
EXPLICIT_IDENTITY_FIELDS = ("library_sync_id", "sync_identity", "verified_sync_key", "verified_provider_identity")
LOCAL_ONLY_FIELDS = frozenset({
    "path", "launch", "command", "executable", "args", "arguments", "install_dir",
    "working_dir", "favorite", "hidden", "broken", "portable", "installed",
    "progress", "rating", "play_count", "playtime_seconds", "last_played",
    "credentials", "password", "token", "secret", "custom", "metadata",
})
EVENT_FIELDS = frozenset({
    "format", "event_id", "device_id", "sequence", "created_at", "sync_key",
    "parents", "catalog", "tombstone",
})


class LibrarySyncError(Exception):
    """Base class for safe library synchronization errors."""


class SyncValidationError(LibrarySyncError, ValueError):
    """An event or event set failed validation."""


class SyncFolderError(LibrarySyncError, OSError):
    """The configured transport folder is missing or unusable."""


class SyncStaleError(LibrarySyncError):
    """A preview was made against a state that has since changed."""


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise SyncValidationError("Sync data must be JSON serializable.") from error


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 80:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None


def _clean_id(value: Any, label: str = "identifier") -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise SyncValidationError(f"Sync {label} must be a non-empty string.")
    result = str(value).strip()
    if not result or len(result) > 256 or any(ord(char) < 32 for char in result):
        raise SyncValidationError(f"Sync {label} is invalid.")
    return result


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        raise SyncValidationError("Catalog values are nested too deeply.")
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and (value != value or value in {float("inf"), float("-inf")}):
            raise SyncValidationError("Catalog values must be finite.")
        return copy.deepcopy(value)
    if isinstance(value, list):
        if len(value) > 10_000:
            raise SyncValidationError("Catalog list is too large.")
        return [_json_safe(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > 1_000:
            raise SyncValidationError("Catalog object is too large.")
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 128:
                raise SyncValidationError("Catalog keys are invalid.")
            result[key] = _json_safe(item, depth=depth + 1)
        return result
    raise SyncValidationError("Catalog contains a non-JSON value.")


def _games(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = value.get("games", [])
    if not isinstance(value, list):
        raise SyncValidationError("Library games must be a list.")
    if any(not isinstance(item, dict) for item in value):
        raise SyncValidationError("Library games must contain objects.")
    return list(value)


def build_catalog(game: dict[str, Any]) -> dict[str, Any]:
    """Project a local game onto the explicit shared catalog allowlist."""
    if not isinstance(game, dict):
        raise SyncValidationError("A game must be an object.")
    catalog = {}
    for field in CATALOG_FIELDS:
        if field in game and game[field] is not None:
            catalog[field] = _json_safe(game[field])
    encoded = _canonical(catalog)
    if len(encoded) > MAX_CATALOG_BYTES:
        raise SyncValidationError("Catalog is too large.")
    return catalog


def stable_sync_key(game: dict[str, Any]) -> str:
    """Return an explicit verified identity, otherwise the stable game ID.

    Provider fields are intentionally not an automatic fallback.  Different
    imports can reuse a provider identifier, and linking those records would
    silently merge unrelated catalog entries.  The reconciliation UI may set
    one of ``EXPLICIT_IDENTITY_FIELDS`` after review.
    """
    if not isinstance(game, dict):
        raise SyncValidationError("A game must be an object.")
    for field in EXPLICIT_IDENTITY_FIELDS:
        value = game.get(field)
        if value not in (None, "") and not isinstance(value, (dict, list, bool)):
            return f"verified:{_clean_id(value, field)}"
    if game.get("provider_identity_verified") is True:
        for field in PROVIDER_ID_FIELDS:
            value = game.get(field)
            if value not in (None, "") and not isinstance(value, (dict, list, bool)):
                return f"verified:provider:{field}:{_clean_id(value, field)}"
    game_id = game.get("game_id")
    if game_id in (None, ""):
        raise SyncValidationError("A game needs a stable game_id or explicit verified sync identity.")
    return f"game:{_clean_id(game_id, 'game_id')}"


sync_key = stable_sync_key


def _event_without_id(event: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in event.items() if key != "event_id"}


def event_id(event: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(_event_without_id(event))).hexdigest()


def make_event(*, device_id: str, sequence: int, sync_key: str, parents: Iterable[str] = (),
               catalog: dict[str, Any] | None = None, tombstone: bool = False,
               created_at: str | None = None) -> dict[str, Any]:
    """Build and hash one immutable event."""
    device_id = _clean_id(device_id, "device_id")
    sync_key = _clean_id(sync_key, "sync_key")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise SyncValidationError("Event sequence must be a positive integer.")
    parent_ids = [_clean_id(parent, "parent event ID") for parent in parents]
    if len(set(parent_ids)) != len(parent_ids):
        raise SyncValidationError("Event parents must be unique.")
    if len(parent_ids) > 256:
        raise SyncValidationError("An event has too many parents.")
    when = created_at or _utc_now()
    if not _valid_timestamp(when):
        raise SyncValidationError("Event timestamp is invalid.")
    if tombstone and catalog is not None:
        raise SyncValidationError("An event cannot contain both catalog and tombstone.")
    if not tombstone and catalog is None:
        raise SyncValidationError("An event needs a catalog or tombstone.")
    catalog_value = None
    if not tombstone:
        catalog_value = build_catalog(catalog or {})
        if sync_key.startswith("verified:") and not catalog_value.get("library_sync_id"):
            catalog_value["library_sync_id"] = sync_key.split(":", 1)[1]
    result: dict[str, Any] = {
        "format": SYNC_FORMAT,
        "device_id": device_id,
        "sequence": sequence,
        "created_at": when,
        "sync_key": sync_key,
        "parents": sorted(parent_ids),
    }
    if tombstone:
        result["tombstone"] = True
    else:
        result["catalog"] = catalog_value
    result["event_id"] = event_id(result)
    validate_event(result)
    return result


def validate_event(event: Any) -> dict[str, Any]:
    """Validate and return a detached event copy, including its content hash."""
    if not isinstance(event, dict) or set(event) - EVENT_FIELDS:
        raise SyncValidationError("Event shape is invalid.")
    if set(event) - {"catalog", "tombstone"} != {"format", "event_id", "device_id", "sequence", "created_at", "sync_key", "parents"}:
        raise SyncValidationError("Event has missing or unexpected fields.")
    if event.get("format") != SYNC_FORMAT:
        raise SyncValidationError("Unsupported library sync format.")
    actual_id = _clean_id(event.get("event_id"), "event_id")
    if len(actual_id) != 64 or any(char not in "0123456789abcdef" for char in actual_id):
        raise SyncValidationError("Event ID is invalid.")
    if event_id(event) != actual_id:
        raise SyncValidationError("Event content hash does not match event_id.")
    _clean_id(event.get("device_id"), "device_id")
    if isinstance(event.get("sequence"), bool) or not isinstance(event.get("sequence"), int) or event["sequence"] < 1:
        raise SyncValidationError("Event sequence is invalid.")
    if not _valid_timestamp(event.get("created_at")):
        raise SyncValidationError("Event timestamp is invalid.")
    sync_key = _clean_id(event.get("sync_key"), "sync_key")
    if sync_key.startswith("game:"):
        key_value = sync_key.split(":", 1)[1]
        if not key_value:
            raise SyncValidationError("Game sync key is invalid.")
    elif sync_key.startswith("verified:"):
        if not sync_key.split(":", 1)[1]:
            raise SyncValidationError("Verified sync key is invalid.")
    else:
        raise SyncValidationError("Unsupported sync key namespace.")
    parents = event.get("parents")
    if not isinstance(parents, list) or len(parents) > 256:
        raise SyncValidationError("Event parents are invalid.")
    for parent in parents:
        parent_id = _clean_id(parent, "parent event ID")
        if len(parent_id) != 64 or any(char not in "0123456789abcdef" for char in parent_id):
            raise SyncValidationError("Event parent ID is invalid.")
    if len(set(parents)) != len(parents) or parents != sorted(parents):
        raise SyncValidationError("Event parents must be unique and sorted.")
    has_catalog = "catalog" in event
    has_tombstone = "tombstone" in event
    if has_catalog == has_tombstone or has_tombstone and event.get("tombstone") is not True:
        raise SyncValidationError("Event must contain exactly one valid catalog or tombstone.")
    if has_catalog:
        if not isinstance(event["catalog"], dict):
            raise SyncValidationError("Event catalog is invalid.")
        if set(event["catalog"]) - CATALOG_FIELD_SET:
            raise SyncValidationError("Event catalog contains a non-catalog field.")
        sync_key = str(event.get("sync_key") or "")
        if sync_key.startswith("game:"):
            game_id = str(event["catalog"].get("game_id") or "")
            if game_id != sync_key.split(":", 1)[1]:
                raise SyncValidationError("Game sync identity does not match event key.")
        if sync_key.startswith("verified:"):
            identity = str(event["catalog"].get("library_sync_id") or "")
            if identity and identity != sync_key.split(":", 1)[1]:
                raise SyncValidationError("Verified sync identity does not match event key.")
        build_catalog(event["catalog"])
    if len(_canonical(event)) > MAX_EVENT_BYTES:
        raise SyncValidationError("Event is too large.")
    return copy.deepcopy(event)


def validate_event_set(events: Iterable[dict[str, Any]], known_events: Iterable[dict[str, Any]] = ()) -> list[dict[str, Any]]:
    """Validate event objects, hashes, duplicate identities, and their DAG."""
    values = list(events or [])
    if len(values) > MAX_EVENTS:
        raise SyncValidationError("Too many sync events.")
    known = {}
    for item in known_events or ():
        checked = validate_event(item)
        known[checked["event_id"]] = checked
    result = {}
    for item in values:
        checked = validate_event(item)
        ident = checked["event_id"]
        if ident in result:
            raise SyncValidationError("Duplicate event identity.")
        # A replicated folder is commonly read repeatedly.  An event already
        # present in local history is the same immutable object, so accepting
        # that exact duplicate is safe; two values with one identity cannot be
        # represented because the identity is a content hash.
        if ident in known:
            if known[ident] != checked:
                raise SyncValidationError("Duplicate event identity.")
            continue
        result[ident] = checked
    combined = {**known, **result}
    for checked in result.values():
        for parent in checked["parents"]:
            if parent not in combined:
                raise SyncValidationError("Event DAG has a missing parent.")
            if combined[parent]["sync_key"] != checked["sync_key"]:
                raise SyncValidationError("Event parent belongs to another sync key.")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(ident: str) -> None:
        if ident in visiting:
            raise SyncValidationError("Event DAG contains a cycle.")
        if ident in visited:
            return
        visiting.add(ident)
        for parent in combined[ident]["parents"]:
            visit(parent)
        visiting.remove(ident)
        visited.add(ident)

    for ident in combined:
        visit(ident)
    return list(result.values())


def _transport_root(folder: str | os.PathLike[str]) -> Path:
    root = Path(folder).expanduser()
    if not root.is_dir():
        raise SyncFolderError(f"Sync folder does not exist: {root}")
    return root / SYNC_DIRECTORY


def sync_folder(folder: str | os.PathLike[str], *, create: bool = False) -> Path:
    target = _transport_root(folder)
    if create:
        try:
            (target / EVENT_DIRECTORY).mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise SyncFolderError(f"Unable to create sync folder: {target}") from error
    return target


def event_path(folder: str | os.PathLike[str], ident: str) -> Path:
    ident = _clean_id(ident, "event_id")
    if len(ident) != 64 or any(char not in "0123456789abcdef" for char in ident):
        raise SyncValidationError("Event ID is invalid.")
    return sync_folder(folder, create=True) / EVENT_DIRECTORY / f"{ident}.json"


def write_event(folder: str | os.PathLike[str], event: dict[str, Any]) -> Path:
    """Atomically publish one immutable event; never overwrite a different value."""
    checked = validate_event(event)
    target = event_path(folder, checked["event_id"])
    encoded = _canonical(checked) + b"\n"
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
            if validate_event(existing) == checked:
                return target
        except (OSError, ValueError, SyncValidationError) as error:
            raise SyncValidationError(f"Existing event cannot be replaced: {target}") from error
        raise SyncValidationError(f"Immutable event differs from existing file: {target}")
    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        temporary = None
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    except OSError as error:
        raise SyncFolderError(f"Unable to publish sync event: {target}") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


def read_events(folder: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Read and validate all event files, leaving invalid source files intact."""
    target = sync_folder(folder)
    events_dir = target / EVENT_DIRECTORY
    if not events_dir.exists():
        return []
    if not events_dir.is_dir():
        raise SyncFolderError(f"Sync event directory is not a directory: {events_dir}")
    paths = sorted(events_dir.glob("*.json"))
    if len(paths) > MAX_EVENTS:
        raise SyncValidationError("Too many sync event files.")
    events = []
    for path in paths:
        try:
            if path.stat().st_size > MAX_EVENT_BYTES:
                raise SyncValidationError(f"Sync event is too large: {path.name}")
            payload = json.loads(path.read_text(encoding="utf-8"))
        except SyncValidationError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SyncValidationError(f"Invalid sync event file: {path.name}") from error
        checked = validate_event(payload)
        if path.stem != checked["event_id"]:
            raise SyncValidationError(f"Sync event filename does not match its hash: {path.name}")
        events.append(checked)
    validate_event_set(events)
    return events


read_event_files = read_events


def write_events(folder: str | os.PathLike[str], events: Iterable[dict[str, Any]]) -> list[Path]:
    checked = validate_event_set(list(events))
    return [write_event(folder, item) for item in checked]


def _metadata(state: dict[str, Any], *, create: bool = True) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise SyncValidationError("Sync state must be an object.")
    value = state.get(SYNC_METADATA_KEY)
    if value is None and create:
        value = {}
        state[SYNC_METADATA_KEY] = value
    if not isinstance(value, dict):
        raise SyncValidationError("Library sync metadata is invalid.")
    value.setdefault("heads", {})
    value.setdefault("events", {})
    value.setdefault("outbox", [])
    return value


def ensure_device_id(state: dict[str, Any]) -> str:
    metadata = _metadata(state)
    value = metadata.get("device_id")
    try:
        parsed = uuid.UUID(str(value)) if value else None
    except (ValueError, AttributeError):
        parsed = None
    if parsed is None:
        value = str(uuid.uuid4())
        metadata["device_id"] = value
    return str(value)


device_id = ensure_device_id


def sync_enabled(state: dict[str, Any]) -> bool:
    """Return whether the caller explicitly opted into catalog synchronization."""
    if not isinstance(state, dict):
        return False
    metadata = state.get(SYNC_METADATA_KEY)
    if isinstance(metadata, dict) and metadata.get("enabled") is True:
        return True
    settings = state.get("settings")
    return isinstance(settings, dict) and settings.get("library_sync_enabled") is True


def capture_sync_snapshot(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Capture a detached pre-mutation snapshot for the transactional hook."""
    return copy.deepcopy(_games(state))


sync_snapshot = capture_sync_snapshot


def set_sync_error(state: dict[str, Any], error: Exception, *, code: str = "SYNC_METADATA_INVALID") -> dict[str, Any]:
    """Persist an actionable local sync error without rejecting a state write."""
    metadata = state.get(SYNC_METADATA_KEY)
    if not isinstance(metadata, dict):
        metadata = {}
        state[SYNC_METADATA_KEY] = metadata
    metadata["error"] = {
        "code": str(code),
        "message": str(error)[:500] or "Library synchronization metadata is invalid.",
    }
    return copy.deepcopy(metadata["error"])


def bootstrap_local_catalog(state: dict[str, Any], *, now: str | None = None,
                            device: str | None = None) -> dict[str, Any]:
    """Create one initial revision for existing games when sync is enabled.

    The marker makes settings saves idempotent.  When a previously enabled
    library was edited while sync was disabled, reconcile its current catalog
    against the last recorded heads so those edits are not lost.
    Identity failures are recorded in metadata and returned to the caller; the
    surrounding settings transaction can still commit the user's choice.
    """
    metadata = _metadata(state)
    if metadata.get("catalog_bootstrapped") is True:
        try:
            events = _state_event_map(metadata)
            heads = metadata.get("heads", {})
            baseline = []
            if isinstance(heads, dict):
                for _key, identifiers in heads.items():
                    if not isinstance(identifiers, list) or not identifiers:
                        continue
                    event = events.get(identifiers[-1])
                    if event and not event.get("tombstone"):
                        catalog = copy.deepcopy(event.get("catalog", {}))
                        sync_key = str(event.get("sync_key") or "")
                        if sync_key.startswith("verified:") and not catalog.get("library_sync_id"):
                            catalog["library_sync_id"] = sync_key.split(":", 1)[1]
                        baseline.append(catalog)
            result = record_local_changes(state, baseline, _games(state), now=now, device=device)
            metadata.pop("error", None)
            return result
        except SyncValidationError as error:
            set_sync_error(state, error, code="SYNC_IDENTITY_AMBIGUOUS")
            return {"events": [], "outbox": copy.deepcopy(metadata.get("outbox", [])), "changed": 0, "error": str(error)}
    existing = metadata.get("events", {})
    heads = metadata.get("heads", {})
    if existing or heads:
        metadata["catalog_bootstrapped"] = True
        return {"events": [], "outbox": copy.deepcopy(metadata.get("outbox", [])), "changed": 0}
    try:
        result = record_local_changes(state, [], _games(state), now=now, device=device)
    except SyncValidationError as error:
        set_sync_error(state, error, code="SYNC_IDENTITY_AMBIGUOUS")
        return {"events": [], "outbox": [], "changed": 0, "error": str(error)}
    metadata["catalog_bootstrapped"] = True
    metadata.pop("error", None)
    return result


def _state_event_map(metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = metadata.get("events", {})
    if isinstance(raw, list):
        raw = {item.get("event_id"): item for item in raw if isinstance(item, dict) and item.get("event_id")}
        metadata["events"] = raw
    if not isinstance(raw, dict):
        raise SyncValidationError("Library sync event history is invalid.")
    result = {}
    for ident, item in raw.items():
        checked = validate_event(item)
        if ident != checked["event_id"]:
            raise SyncValidationError("Library sync event history key is invalid.")
        result[ident] = checked
    return result


def _append_event(metadata: dict[str, Any], event: dict[str, Any], *, outbox: bool = True) -> None:
    events = _state_event_map(metadata)
    events[event["event_id"]] = copy.deepcopy(event)
    metadata["events"] = events
    if outbox:
        pending = metadata.setdefault("outbox", [])
        if not isinstance(pending, list):
            raise SyncValidationError("Library sync outbox is invalid.")
        if not any(isinstance(item, dict) and item.get("event_id") == event["event_id"] for item in pending):
            pending.append(copy.deepcopy(event))


def _next_sequence(metadata: dict[str, Any]) -> int:
    value = metadata.get("next_sequence", 1)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        value = 1
    metadata["next_sequence"] = value + 1
    return value


def _catalog_for_sync_key(game: dict[str, Any], key: str) -> dict[str, Any]:
    catalog = build_catalog(game)
    if key.startswith("verified:") and not catalog.get("library_sync_id"):
        catalog["library_sync_id"] = key.split(":", 1)[1]
    return catalog


def _game_map(games: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result = {}
    for game in games:
        key = stable_sync_key(game)
        if key in result:
            raise SyncValidationError(f"Duplicate local sync identity: {key}")
        result[key] = game
    return result


def record_local_changes(state: dict[str, Any] | Any, before: Any = None, after: Any = None, *, now: str | None = None,
                         device: str | None = None) -> dict[str, Any]:
    """Record shared catalog additions, edits, and deletions in local outbox.

    ``before`` and ``after`` may be game lists or complete state dictionaries.
    The caller persists the mutated ``state`` in its existing transaction.
    """
    # The state-first form is the durable integration contract.  The two
    # snapshot-only forms are useful for callers that keep sync metadata in a
    # separate object: ``record_local_changes(before, after)`` and
    # ``record_local_changes(before, after, metadata)``.
    if isinstance(state, list):
        if isinstance(after, dict):
            state, before, after = after, state, before
        elif after is None:
            state, before, after = {"games": copy.deepcopy(before)}, state, before
        else:
            raise SyncValidationError("Record-local-changes arguments are invalid.")
    if not isinstance(state, dict) or before is None or after is None:
        raise SyncValidationError("Record-local-changes requires before and after snapshots.")
    metadata = _metadata(state)
    device = device or ensure_device_id(state)
    before_map = _game_map(_games(before))
    after_map = _game_map(_games(after))
    all_keys = sorted(set(before_map) | set(after_map))
    heads = metadata.setdefault("heads", {})
    if not isinstance(heads, dict):
        raise SyncValidationError("Library sync heads are invalid.")
    created = now or _utc_now()
    generated = []
    for key in all_keys:
        old = before_map.get(key)
        new = after_map.get(key)
        old_catalog = _catalog_for_sync_key(old, key) if old else None
        new_catalog = _catalog_for_sync_key(new, key) if new else None
        if old is not None and new is not None and old_catalog == new_catalog:
            continue
        prior = heads.get(key, [])
        if not isinstance(prior, list):
            raise SyncValidationError("Library sync head history is invalid.")
        event = make_event(
            device_id=device,
            sequence=_next_sequence(metadata),
            sync_key=key,
            parents=prior,
            catalog=new_catalog,
            tombstone=new is None,
            created_at=created,
        )
        _append_event(metadata, event)
        heads[key] = [event["event_id"]]
        generated.append(copy.deepcopy(event))
    return {"events": generated, "outbox": copy.deepcopy(metadata["outbox"]), "changed": len(generated)}


record_changes = record_local_changes


def publish_outbox(state: dict[str, Any], folder: str | os.PathLike[str]) -> dict[str, Any]:
    """Publish pending events and acknowledge each only after its atomic write."""
    metadata = _metadata(state)
    pending = metadata.get("outbox", [])
    if not isinstance(pending, list):
        raise SyncValidationError("Library sync outbox is invalid.")
    # Include retained ancestors when a device publishes to a fresh replica.
    # This keeps every independently copied folder a self-contained valid DAG.
    history = _state_event_map(metadata)
    pending_checked = validate_event_set(pending, history.values())
    for item in pending_checked:
        history[item["event_id"]] = item
    ordered = sorted(history.values(), key=lambda item: (len(_ancestors(item["event_id"], history)), item["sequence"], item["event_id"]))
    for item in ordered:
        write_event(folder, item)
    published = 0
    while published < len(pending):
        # The event was written above; this is the acknowledgement boundary.
        published += 1
    metadata["outbox"] = pending[published:]
    return {"published": published, "pending": len(metadata["outbox"])}


def state_token(state: dict[str, Any]) -> str:
    metadata = _metadata(state, create=False) if isinstance(state, dict) and SYNC_METADATA_KEY in state else {}
    heads = metadata.get("heads", {}) if isinstance(metadata, dict) else {}
    games = {}
    for key, game in sorted(_game_map(_games(state)).items()):
        games[key] = build_catalog(game)
    return hashlib.sha256(_canonical({"heads": heads, "games": games})).hexdigest()


base_token = state_token


def _ancestors(ident: str, events: dict[str, dict[str, Any]]) -> set[str]:
    result = set()
    todo = list(events.get(ident, {}).get("parents", [])) if ident in events else []
    while todo:
        current = todo.pop()
        if current in result:
            continue
        result.add(current)
        todo.extend(events.get(current, {}).get("parents", []))
    return result


def _is_ancestor(older: str, newer: str, events: dict[str, dict[str, Any]]) -> bool:
    return older == newer or older in _ancestors(newer, events)


def _event_catalog(event: dict[str, Any]) -> dict[str, Any] | None:
    return None if event.get("tombstone") else copy.deepcopy(event.get("catalog", {}))


def _common_base(left: str | None, right: str, events: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if left and left in events and right in events:
        left_anc = {left} | _ancestors(left, events)
        right_anc = {right} | _ancestors(right, events)
        common = left_anc & right_anc
        if common:
            # A merge base is selected by causal ancestry, never wall-clock
            # timestamps (devices can have skewed clocks). Remove every common
            # ancestor that is itself an ancestor of another common ancestor,
            # leaving only causal maxima. Multiple different maxima are kept
            # as alternatives; choosing one by event ID can lose an edit.
            maxima = [
                ident for ident in common
                if not any(ident != other and _is_ancestor(ident, other, events) for other in common)
            ]
            choices = sorted(maxima or common)
            catalogs = [_event_catalog(events[ident]) or {} for ident in choices]
            if len(catalogs) > 1 and any(catalog != catalogs[0] for catalog in catalogs[1:]):
                return {"__sync_ambiguous_bases__": catalogs}
            return catalogs[0] if catalogs else {}
    return {}


def _common_base_many(identifiers: list[str], events: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Return a causal merge base for a set of concurrent heads.

    A single arbitrary head is not a valid base for a multi-head reduction.
    Keep incomparable maxima as an explicit ambiguity so field reductions can
    require review instead of silently choosing one branch.
    """
    if not identifiers:
        return {}
    common: set[str] | None = None
    for ident in identifiers:
        ancestry = {ident} | _ancestors(ident, events)
        common = ancestry if common is None else common & ancestry
    if not common:
        return {}
    maxima = [
        ident for ident in common
        if not any(ident != other and _is_ancestor(ident, other, events) for other in common)
    ]
    choices = sorted(maxima or common)
    catalogs = [_event_catalog(events[ident]) or {} for ident in choices]
    if len(catalogs) > 1 and any(catalog != catalogs[0] for catalog in catalogs[1:]):
        return {"__sync_ambiguous_bases__": catalogs}
    return catalogs[0] if catalogs else {}


def _conflict_id(key: str, field: str, alternatives: list[dict[str, Any]]) -> str:
    """Build a stable opaque identifier for one review row."""
    identity = [
        {"choice": item.get("choice"), "event_ids": item.get("event_ids", []),
         "present": item.get("present", True), "value": item.get("value")}
        for item in alternatives
    ]
    digest = hashlib.sha256(_canonical({"sync_key": key, "field": field, "alternatives": identity})).hexdigest()
    return f"sync-conflict:{digest}"


def _alternative_values(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse equal values while retaining every source event identity."""
    grouped: dict[bytes, dict[str, Any]] = {}
    for item in items:
        value = copy.deepcopy(item.get("value"))
        present = bool(item.get("present", True))
        identity = _canonical({"present": present, "value": value})
        current = grouped.get(identity)
        if current is None:
            current = {
                "choice": item.get("choice"),
                "event_ids": [],
                "present": present,
                "value": value,
            }
            grouped[identity] = current
        event_id_value = item.get("event_id")
        if event_id_value and event_id_value not in current["event_ids"]:
            current["event_ids"].append(event_id_value)
    return list(grouped.values())


def _conflict_selection(conflicts: Any, key: str, field: str,
                        conflict_id: str | None = None) -> str | None:
    """Read a review selection, supporting row IDs and legacy field keys."""
    value = conflicts.get(key) if isinstance(conflicts, dict) else None
    if isinstance(value, dict):
        if conflict_id and value.get(conflict_id) is not None:
            selected = value.get(conflict_id)
        else:
            selected = value.get(field)
        if isinstance(selected, str):
            return selected
    if isinstance(value, str) and field in {"*", "__record__"}:
        return value
    return None


def _selected_alternative(conflicts: Any, key: str, row: dict[str, Any]) -> dict[str, Any] | None:
    selected = _conflict_selection(conflicts, key, str(row.get("field") or ""), row.get("conflict_id"))
    if selected is None:
        return None
    for alternative in row.get("alternatives", []):
        if not isinstance(alternative, dict):
            continue
        if selected == alternative.get("choice") or selected in alternative.get("event_ids", []):
            return copy.deepcopy(alternative)
    return None


def _make_conflict_row(key: str, field: str, alternatives: list[dict[str, Any]], *,
                       base: Any = None) -> dict[str, Any]:
    alternatives = _alternative_values(alternatives)
    if alternatives:
        alternatives[0]["choice"] = alternatives[0].get("choice") or "local"
    if len(alternatives) > 1:
        alternatives[1]["choice"] = alternatives[1].get("choice") or "remote"
    for index, alternative in enumerate(alternatives[2:], start=2):
        alternative["choice"] = alternative.get("choice") or f"alternative:{index}"
    row: dict[str, Any] = {
        "field": field,
        "alternatives": alternatives,
        "conflict_id": _conflict_id(key, field, alternatives),
    }
    if alternatives:
        row["local"] = copy.deepcopy(alternatives[0].get("value"))
    if len(alternatives) > 1:
        row["remote"] = copy.deepcopy(alternatives[1].get("value"))
    if base is not None:
        row["base"] = copy.deepcopy(base)
    return row


def _merge_remote_heads(key: str, head_ids: list[str], events: dict[str, dict[str, Any]],
                        choices: Any = None) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Merge every concurrent remote head, retaining each conflict alternative."""
    if not head_ids:
        return None, []
    head_ids = list(dict.fromkeys(head_ids))
    candidates = []
    for index, head_id in enumerate(head_ids):
        event = events[head_id]
        candidates.append({
            "event_id": head_id,
            "choice": "local" if index == 0 else ("remote" if index == 1 else f"alternative:{index}"),
            "value": _event_catalog(event),
            "present": not bool(event.get("tombstone")),
        })
    record_alternatives = _alternative_values(candidates)
    if len(record_alternatives) > 1 and any(not item.get("present", True) for item in record_alternatives):
        row = _make_conflict_row(key, "__record__", candidates)
        selected = _selected_alternative(choices, key, row)
        if selected is not None:
            return (copy.deepcopy(selected.get("value")) if selected.get("present", True) else None), []
        return copy.deepcopy(candidates[0].get("value")), [row]
    if len(record_alternatives) == 1:
        return (copy.deepcopy(record_alternatives[0].get("value"))
                if record_alternatives[0].get("present", True) else None), []

    base = _common_base_many(head_ids, events)
    ambiguous_bases = base.get("__sync_ambiguous_bases__") if isinstance(base, dict) else None
    merged: dict[str, Any] = {}
    conflict_rows: list[dict[str, Any]] = []
    all_fields = {
        field for field in (set(base) if isinstance(base, dict) else set())
        if field != "__sync_ambiguous_bases__"
    }
    for candidate in candidates:
        value = candidate.get("value")
        if isinstance(value, dict):
            all_fields.update(value)
    for field in sorted(all_fields):
        values = []
        for candidate in candidates:
            catalog = candidate.get("value")
            values.append({
                "event_id": candidate["event_id"],
                "choice": candidate["choice"],
                "present": isinstance(catalog, dict) and field in catalog,
                "value": catalog.get(field) if isinstance(catalog, dict) and field in catalog else None,
            })
        alternatives = _alternative_values(values)
        if len(alternatives) == 1:
            if alternatives[0].get("present", True):
                merged[field] = copy.deepcopy(alternatives[0].get("value"))
            continue
        base_values = []
        if isinstance(ambiguous_bases, list):
            base_values = [
                {"present": isinstance(item, dict) and field in item,
                 "value": item.get(field) if isinstance(item, dict) and field in item else None}
                for item in ambiguous_bases
            ]
        else:
            base_values = [{"present": field in base, "value": base.get(field)}]
        changed = [
            item for item in values
            if any(item["present"] != base_item["present"] or item["value"] != base_item["value"]
                   for base_item in base_values)
        ]
        if len(changed) == 1 and len(changed) < len(values):
            chosen = changed[0]
            if chosen["present"]:
                merged[field] = copy.deepcopy(chosen["value"])
            continue
        row = _make_conflict_row(key, field, values, base=(
            [item.get("value") if item.get("present") else None for item in base_values]
            if isinstance(ambiguous_bases, list) else base.get(field)
        ))
        selected = _selected_alternative(choices, key, row)
        if selected is not None:
            if selected.get("present", True):
                merged[field] = copy.deepcopy(selected.get("value"))
        else:
            first = alternatives[0]
            if first.get("present", True):
                merged[field] = copy.deepcopy(first.get("value"))
            conflict_rows.append(row)
    return merged, conflict_rows


def _conflict_choice(conflicts: Any, key: str, field: str) -> str | None:
    choice = _conflict_selection(conflicts, key, field)
    if choice in {"local", "remote"}:
        return choice
    return None


def _merge_catalogs(key: str, local: dict[str, Any] | None, remote: dict[str, Any] | None,
                    base: dict[str, Any], conflicts: Any = None, *, local_deleted: bool = False
                    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    # Resolve whole-record add/delete decisions before reconciling fields from
    # an ambiguous criss-cross base.  A remote tombstone must remain a
    # tombstone when the reviewer chooses it.
    if local is None and not local_deleted:
        return remote, []
    if local is None or remote is None:
        if local == remote:
            return local, []
        row = _make_conflict_row(key, "__record__", [
            {"choice": "local", "present": local is not None, "value": local},
            {"choice": "remote", "present": remote is not None, "value": remote},
        ], base=base)
        selected = _selected_alternative(conflicts, key, row)
        if selected is not None:
            return (copy.deepcopy(selected.get("value"))
                    if selected.get("present", True) else None), []
        return local, [row]
    ambiguous_bases = base.get("__sync_ambiguous_bases__") if isinstance(base, dict) else None
    if isinstance(ambiguous_bases, list):
        # A criss-cross DAG can have several incomparable common ancestors.
        # Fields whose base values disagree cannot be classified as a
        # disjoint edit; retain both alternatives for review instead of
        # selecting an arbitrary event hash.
        canonical_base = {}
        uncertain_fields = set()
        all_fields = set()
        for variant in ambiguous_bases:
            if isinstance(variant, dict):
                all_fields.update(variant)
        for field in all_fields:
            values = [variant.get(field) for variant in ambiguous_bases if isinstance(variant, dict)]
            if values and all(value == values[0] for value in values[1:]):
                if any(field in variant for variant in ambiguous_bases if isinstance(variant, dict)):
                    canonical_base[field] = values[0]
            else:
                uncertain_fields.add(field)
        merged, rows = _merge_catalogs(key, local, remote, canonical_base, conflicts, local_deleted=local_deleted)
        if merged is None:
            merged = copy.deepcopy(local)
        for field in sorted(uncertain_fields):
            local_value = local.get(field) if isinstance(local, dict) else None
            remote_value = remote.get(field) if isinstance(remote, dict) else None
            if local_value == remote_value:
                continue
            row = _make_conflict_row(key, field, [
                {"choice": "local", "present": isinstance(local, dict) and field in local, "value": local_value},
                {"choice": "remote", "present": isinstance(remote, dict) and field in remote, "value": remote_value},
            ], base=[variant.get(field) for variant in ambiguous_bases if isinstance(variant, dict)])
            selected = _selected_alternative(conflicts, key, row)
            if selected is not None:
                if selected.get("present", True):
                    if merged is None:
                        merged = {}
                    merged[field] = copy.deepcopy(selected.get("value"))
                elif merged is not None:
                    merged.pop(field, None)
            else:
                rows.append(row)
        return merged, rows
    merged = copy.deepcopy(local)
    conflict_rows = []
    for field in sorted(set(base) | set(local) | set(remote)):
        local_value = local.get(field)
        remote_value = remote.get(field)
        base_value = base.get(field)
        local_changed = local_value != base_value
        remote_changed = remote_value != base_value
        if local_changed and remote_changed and local_value != remote_value:
            row = _make_conflict_row(key, field, [
                {"choice": "local", "present": field in local, "value": local_value},
                {"choice": "remote", "present": field in remote, "value": remote_value},
            ], base=base_value)
            selected = _selected_alternative(conflicts, key, row)
            if selected is not None:
                if selected.get("present", True):
                    merged[field] = copy.deepcopy(selected.get("value"))
                else:
                    merged.pop(field, None)
            else:
                conflict_rows.append(row)
        elif remote_changed:
            if field in remote:
                merged[field] = copy.deepcopy(remote[field])
            else:
                merged.pop(field, None)
    return merged, conflict_rows


def _metadata_known_events(metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return _state_event_map(metadata) if metadata else {}


def _incoming_heads(events: dict[str, dict[str, Any]], key: str) -> list[str]:
    ids = {ident for ident, item in events.items() if item["sync_key"] == key}
    return sorted(ident for ident in ids if not any(ident in _ancestors(other, events) for other in ids if other != ident))


def preview_sync(state: dict[str, Any], incoming: Iterable[dict[str, Any]], *, conflicts: Any = None) -> dict[str, Any]:
    """Compute a detached merge plan without changing ``state``."""
    metadata = _metadata(state, create=False) if isinstance(state, dict) and SYNC_METADATA_KEY in state else {}
    known = _metadata_known_events(metadata)
    validated = validate_event_set(incoming, known.values())
    combined = {**known, **{item["event_id"]: item for item in validated}}
    local_games = _game_map(_games(state))
    heads = metadata.get("heads", {}) if isinstance(metadata, dict) else {}
    changes = []
    pending = metadata.get("pending_conflicts", {}) if isinstance(metadata, dict) else {}
    history_keys = set(heads) | (set(pending) if isinstance(pending, dict) else set())
    known_keys = {event["sync_key"] for event in combined.values() if event["sync_key"] in history_keys}
    keys = sorted(set(local_games) | {event["sync_key"] for event in validated} | known_keys | (set(pending) if isinstance(pending, dict) else set()))
    for key in keys:
        local_game = local_games.get(key)
        local_catalog = build_catalog(local_game) if local_game else None
        local_heads = heads.get(key, []) if isinstance(heads, dict) else []
        remote_ids = _incoming_heads(combined, key)
        remote_ids = [
            ident for ident in remote_ids
            if ident not in local_heads and not any(_is_ancestor(ident, local, combined) for local in local_heads)
        ]
        if not remote_ids:
            continue
        remote_ids = sorted(remote_ids)
        remote_id = remote_ids[-1]
        remote_catalog, remote_conflicts = _merge_remote_heads(key, remote_ids, combined, conflicts)
        local_id = local_heads[-1] if local_heads else None
        if local_id and all(_is_ancestor(local_id, ident, combined) for ident in remote_ids):
            merged, conflict_rows = remote_catalog, remote_conflicts
            mode = "conflict" if conflict_rows else ("delete" if remote_catalog is None else ("add" if local_game is None else "update"))
        elif local_id and all(_is_ancestor(ident, local_id, combined) for ident in remote_ids):
            continue
        elif local_id and len(remote_ids) > 1:
            # Reduce a divergent local head together with every concurrent
            # remote head in one pass.  Pairwise reduction would turn three
            # alternatives into changing local/remote pairs and make a UI
            # choice depend on which earlier choice happened to be applied.
            candidate_ids = list(dict.fromkeys([local_id, *remote_ids]))
            merged, conflict_rows = _merge_remote_heads(key, candidate_ids, combined, conflicts)
            mode = "conflict" if conflict_rows else ("delete" if merged is None else ("add" if local_game is None else "merge"))
        else:
            base = _common_base(local_id, remote_ids[0], combined) if local_id else {}
            merged, conflict_rows = _merge_catalogs(
                key, local_catalog, remote_catalog, base, conflicts,
                local_deleted=bool(local_id and combined.get(local_id, {}).get("tombstone")),
            )
            conflict_rows = list(remote_conflicts) + conflict_rows
            mode = "conflict" if conflict_rows else ("delete" if merged is None else ("add" if local_game is None else "merge"))
        changes.append({
            "sync_key": key, "event_id": remote_id, "event_ids": remote_ids, "action": mode,
            "local": copy.deepcopy(local_catalog), "remote": copy.deepcopy(remote_catalog),
            "merged": copy.deepcopy(merged), "conflicts": conflict_rows,
        })
    plan = {
        "format": SYNC_FORMAT,
        "source_token": state_token(state),
        "event_ids": [item["event_id"] for item in validated],
        "events": copy.deepcopy(validated),
        "changes": changes,
        "conflicts": [row for change in changes for row in change["conflicts"]],
        "pending_conflicts": copy.deepcopy(metadata.get("pending_conflicts", {})) if isinstance(metadata, dict) else {},
        "counts": {
            "additions": sum(change["action"] == "add" for change in changes),
            "updates": sum(change["action"] in {"update", "merge"} for change in changes),
            "deletions": sum(change["action"] == "delete" for change in changes),
            "conflicts": sum(bool(change["conflicts"]) for change in changes),
        },
    }
    plan["events_token"] = hashlib.sha256(_canonical(plan["events"])).hexdigest()
    return copy.deepcopy(plan)


preview = preview_sync
preview_incoming = preview_sync


def _replace_catalog(local: dict[str, Any] | None, catalog: dict[str, Any] | None) -> dict[str, Any] | None:
    if catalog is None:
        return None
    result = copy.deepcopy(local) if local else {}
    # Keep the local game_id when provider identity linked two device copies.
    local_id = result.get("game_id")
    for field in CATALOG_FIELDS:
        if field == "game_id" and local_id:
            continue
        if field in catalog:
            result[field] = copy.deepcopy(catalog[field])
        elif field in result and field in CATALOG_FIELD_SET:
            result.pop(field, None)
    return result


def apply_sync(state: dict[str, Any], plan: dict[str, Any], *, conflicts: Any = None,
               now: str | None = None) -> dict[str, Any]:
    """Apply a reviewed plan after recomputing it from validated events.

    The client may submit a catalog preview for display, but its ``changes``
    and ``merged`` values are advisory.  Applying always derives a fresh plan
    under the current state transaction so a tampered change catalog cannot
    overwrite server-side catalog data.
    """
    if not isinstance(plan, dict) or plan.get("format") != SYNC_FORMAT:
        raise SyncValidationError("Sync preview is invalid.")
    if plan.get("source_token") != state_token(state):
        raise SyncStaleError("Library sync preview is stale; review incoming changes again.")
    metadata = _metadata(state)
    known = _metadata_known_events(metadata)
    incoming = plan.get("events")
    if not isinstance(incoming, list):
        raise SyncValidationError("Sync preview events are invalid.")
    validated = validate_event_set(incoming, known.values())
    supplied = [validate_event(item) for item in incoming]
    event_ids = [item["event_id"] for item in supplied]
    if plan.get("event_ids") != event_ids:
        raise SyncValidationError("Sync preview event identities do not match its events.")
    events_token = hashlib.sha256(_canonical(supplied)).hexdigest()
    if plan.get("events_token", events_token) != events_token:
        raise SyncValidationError("Sync preview events were changed after review.")
    reviewed = preview_sync(state, supplied, conflicts=conflicts)
    for item in validated:
        _append_event(metadata, item, outbox=False)
    if not isinstance(state.get("games", []), list):
        raise SyncValidationError("Library games must be a list.")
    games = state["games"]
    local_map = _game_map(games)
    heads = metadata.setdefault("heads", {})
    pending_conflicts = metadata.setdefault("pending_conflicts", {})
    if not isinstance(pending_conflicts, dict):
        raise SyncValidationError("Library sync pending conflicts are invalid.")
    applied = []
    unresolved = []
    local_device = ensure_device_id(state)
    for change in reviewed.get("changes", []):
        key = change.get("sync_key")
        if not isinstance(key, str):
            continue
        local = local_map.get(key)
        local_head = (heads.get(key) or [None])[-1]
        merged, conflict_rows = copy.deepcopy(change.get("merged")), list(change.get("conflicts") or [])
        if conflict_rows:
            unresolved.extend({**row, "sync_key": key} for row in conflict_rows)
            pending_conflicts[key] = {
                "event_ids": list(change.get("event_ids") or [change.get("event_id")]),
                "conflicts": copy.deepcopy(conflict_rows),
            }
            continue
        remote_ids = [ident for ident in (change.get("event_ids") or [change.get("event_id")]) if ident]
        if key not in local_map and not remote_ids:
            continue
        parent_ids = []
        for ident in [local_head, *remote_ids]:
            if ident and ident not in parent_ids:
                parent_ids.append(ident)
        if merged is None:
            if local is not None:
                games.remove(local)
                local_map.pop(key, None)
            if len(parent_ids) > 1:
                resolution = make_event(
                    device_id=local_device, sequence=_next_sequence(metadata), sync_key=key,
                    parents=parent_ids, tombstone=True, created_at=now or _utc_now(),
                )
                _append_event(metadata, resolution)
                heads[key] = [resolution["event_id"]]
            else:
                heads[key] = [remote_ids[-1]] if remote_ids else []
            pending_conflicts.pop(key, None)
            applied.append(key)
            continue
        replacement = _replace_catalog(local, merged)
        if replacement is None:
            continue
        if key.startswith("verified:") and not replacement.get("library_sync_id"):
            replacement["library_sync_id"] = key.split(":", 1)[1]
        if local is None:
            games.append(replacement)
        else:
            index = games.index(local)
            games[index] = replacement
        local_map[key] = replacement
        if len(parent_ids) > 1:
            resolution = make_event(
                device_id=local_device, sequence=_next_sequence(metadata), sync_key=key,
                parents=parent_ids, catalog=merged, created_at=now or _utc_now(),
            )
            _append_event(metadata, resolution)
            heads[key] = [resolution["event_id"]]
        else:
            heads[key] = [remote_ids[-1]] if remote_ids else []
        pending_conflicts.pop(key, None)
        applied.append(key)
    result = {"games": games, "applied": len(applied), "conflicts": unresolved, "changed": bool(applied)}
    if unresolved:
        result["conflicts"] = copy.deepcopy(unresolved)
    return result


apply = apply_sync


def ingest_event_set(incoming: Iterable[dict[str, Any]], *, known_events: Iterable[dict[str, Any]] = ()) -> list[dict[str, Any]]:
    """Validate an externally supplied event set without mutating local state."""
    return validate_event_set(incoming, known_events)


ingest_events = ingest_event_set
