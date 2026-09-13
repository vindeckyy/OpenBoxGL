"""Bounded, local-first household records and shared-folder transport (T6).

Household data is deliberately a small protocol layered beside the catalog
sync journal.  Records are immutable, content addressed, and contain no
paths, commands, credentials, or provider metadata.  A mounted sync folder
or another local transport can exchange the detached records.  The optional
filesystem transport below uses a separate versioned event namespace and does
not change the record or manual-merge APIs.

The state helpers use ``state["household"]`` as a local record cache and
outbox.  They validate a complete candidate before assigning it, so malformed
or oversized remote data cannot leave a half-applied local state behind.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend_io import atomic_write_text
from pkg.parity.parity_library_sync import (
    SyncValidationError,
    SyncFolderError,
    _canonical,
    _clean_id,
    _json_safe,
    _valid_timestamp,
)


HOUSEHOLD_FORMAT = 1
HOUSEHOLD_FORMAT_VERSION = HOUSEHOLD_FORMAT
HOUSEHOLD_SYNC_FORMAT = HOUSEHOLD_FORMAT
HOUSEHOLD_STATE_KEY = "household"
HOUSEHOLD_SYNC_DIRECTORY = "openbox-household-v1"
HOUSEHOLD_EVENT_DIRECTORY = "events"
HOUSEHOLD_LOCK_FILENAME = ".household.lock"

RECORD_KINDS = ("member", "challenge", "challenge_result", "share")
RECORD_KIND_SET = frozenset(RECORD_KINDS)

# These are protocol limits, rather than UI limits.  Keeping them here makes
# every ingestion path enforce the same finite boundary.
MAX_RECORD_BYTES = 128 * 1024
MAX_RECORDS = 10_000
# Familiar names for callers that share the library-sync transport helpers.
MAX_EVENT_BYTES = MAX_RECORD_BYTES
MAX_EVENTS = MAX_RECORDS
MAX_HOUSEHOLD_RECORD_BYTES = MAX_RECORD_BYTES
MAX_HOUSEHOLD_RECORDS = MAX_RECORDS
MAX_PARENTS = 256
MAX_MEMBER_NAME = 80
MAX_AVATAR_COLOR = 48
MAX_CHALLENGE_TITLE = 160
MAX_CHALLENGE_DESCRIPTION = 2_000
MAX_PARTICIPANTS = 128
MAX_RESULT_NOTE = 500
MAX_STATS_FIELDS = 32
MAX_STAT_VALUE = 2_000_000_000
MAX_CHALLENGE_TARGET = 2_000_000_000
MAX_SHARE_ID = 256

METRICS = frozenset({
    "completions",
    "finished",
    "playtime_seconds",
    "ra_count",
    "achievements",
    "wins",
})
CHALLENGE_STATUSES = frozenset({"open", "active", "closed", "cancelled", "completed"})
LOCAL_RESULT_STATUSES = frozenset({"open", "active"})
SHARE_PERIODS = frozenset({"weekly", "all_time", "monthly", "daily"})
SHARE_STAT_FIELDS = frozenset({
    "playtime_seconds",
    "completions",
    "ra_count",
    "games_played",
    "sessions",
    "wins",
})
COMPLETION_MARKERS = ("beaten", "completed", "mastered", "finished")

RECORD_FIELDS = frozenset({
    "format",
    "event_id",
    "device_id",
    "sequence",
    "created_at",
    "sync_key",
    "kind",
    "parents",
    "payload",
    "tombstone",
})
MEMBER_FIELDS = frozenset({"member_id", "display_name", "avatar_color", "stats_shared"})
CHALLENGE_FIELDS = frozenset({
    "challenge_id",
    "title",
    "description",
    "game_id",
    "metric",
    "target",
    "deadline",
    "created_by",
    "participant_ids",
    "status",
})
CHALLENGE_RESULT_FIELDS = frozenset({
    "challenge_id",
    "member_id",
    "value",
    "completed",
    "completed_at",
    "game_id",
    "note",
})
SHARE_FIELDS = frozenset({
    "share_id",
    "member_id",
    "period",
    "stats",
    "window_start",
    "window_end",
})


class HouseholdError(Exception):
    """Base class for household protocol errors."""


# Keep the library-sync exception as the public validation type.  Callers
# already catch this type for catalog events and can use one fail-closed path.
HouseholdValidationError = SyncValidationError


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, label: str, maximum: int, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise SyncValidationError(f"Household {label} must be text.")
    value = value.strip()
    if not value and required:
        raise SyncValidationError(f"Household {label} is required.")
    if len(value) > maximum or any(ord(char) < 32 for char in value):
        raise SyncValidationError(f"Household {label} is invalid.")
    return value


def _optional_text(value: Any, label: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _text(value, label, maximum)


def _number(value: Any, label: str, maximum: int = MAX_STAT_VALUE, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SyncValidationError(f"Household {label} must be an integer.")
    lower = 1 if positive else 0
    if value < lower or value > maximum:
        raise SyncValidationError(f"Household {label} is outside its bound.")
    return value


def _timestamp(value: Any, label: str) -> str:
    if not _valid_timestamp(value):
        raise SyncValidationError(f"Household {label} timestamp is invalid.")
    return str(value)


def _period_bound(value: Any, label: str) -> str:
    if isinstance(value, str) and len(value.strip()) == 10:
        try:
            datetime.strptime(value.strip(), "%Y-%m-%d")
        except ValueError:
            pass
        else:
            return value.strip()
    return _timestamp(value, label)


def _normalise_share_period(value: Any) -> str:
    period = _text(value, "share period", 20).casefold().replace("-", "_")
    if period not in SHARE_PERIODS:
        raise SyncValidationError("Household share period is unsupported.")
    return period


def _id_list(value: Any, label: str, maximum: int = MAX_PARTICIPANTS) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise SyncValidationError(f"Household {label} must be a bounded list.")
    result = [_clean_id(item, f"{label} identifier") for item in value]
    if len(set(result)) != len(result):
        raise SyncValidationError(f"Household {label} must be unique.")
    return result


def _bounded_iterable(value: Iterable[Any], label: str, maximum: int) -> list[Any]:
    """Materialize a caller iterable without allowing an unbounded source."""
    if isinstance(value, (str, bytes, bytearray, dict)):
        raise SyncValidationError(f"Household {label} must be an iterable of values.")
    try:
        iterator = iter(value)
    except TypeError as error:
        raise SyncValidationError(f"Household {label} must be iterable.") from error
    result = []
    for index, item in enumerate(iterator):
        if index >= maximum:
            raise SyncValidationError(f"Household {label} is too large.")
        result.append(item)
    return result


def _namespace_key(sync_key: Any, kind: str) -> str:
    value = _clean_id(sync_key, "sync_key")
    prefix = f"{kind}:"
    if not value.startswith(prefix) or not value[len(prefix):]:
        raise SyncValidationError("Household sync key has the wrong namespace.")
    return value


def _stats(value: Any) -> dict[str, int]:
    if not isinstance(value, dict) or len(value) > MAX_STATS_FIELDS:
        raise SyncValidationError("Household shared stats must be a bounded object.")
    if set(value) - SHARE_STAT_FIELDS:
        raise SyncValidationError("Household shared stats contain a private or unknown field.")
    result = {}
    for key, item in value.items():
        result[key] = _number(item, f"shared stat {key}")
    return result


def _validate_payload(kind: str, payload: Any, sync_key: str) -> dict[str, Any]:
    """Validate one detached kind payload without changing the source."""
    safe = _json_safe(payload)
    if not isinstance(safe, dict):
        raise SyncValidationError("Household record payload must be an object.")

    if kind == "member":
        if set(safe) - MEMBER_FIELDS or not {"member_id", "display_name", "avatar_color"} <= set(safe):
            raise SyncValidationError("Household member payload is invalid.")
        member_id = _clean_id(safe["member_id"], "member_id")
        expected = _namespace_key(sync_key, kind)
        if expected != f"member:{member_id}":
            raise SyncValidationError("Household member identity does not match its key.")
        _text(safe["display_name"], "display_name", MAX_MEMBER_NAME)
        _text(safe["avatar_color"], "avatar_color", MAX_AVATAR_COLOR)
        if "stats_shared" in safe and not isinstance(safe["stats_shared"], bool):
            raise SyncValidationError("Household stats_shared must be boolean.")
        return safe

    if kind == "challenge":
        required = {"challenge_id", "title", "metric", "target", "created_by"}
        if set(safe) - CHALLENGE_FIELDS or not required <= set(safe):
            raise SyncValidationError("Household challenge payload is invalid.")
        challenge_id = _clean_id(safe["challenge_id"], "challenge_id")
        if _namespace_key(sync_key, kind) != f"challenge:{challenge_id}":
            raise SyncValidationError("Household challenge identity does not match its key.")
        _text(safe["title"], "challenge title", MAX_CHALLENGE_TITLE)
        if "description" in safe:
            _text(safe["description"], "challenge description", MAX_CHALLENGE_DESCRIPTION, required=False)
        if "game_id" in safe:
            _clean_id(safe["game_id"], "game_id")
        metric = _text(safe["metric"], "challenge metric", 40)
        if metric not in METRICS:
            raise SyncValidationError("Household challenge metric is unsupported.")
        _number(safe["target"], "challenge target", MAX_CHALLENGE_TARGET, positive=True)
        if "deadline" in safe:
            _timestamp(safe["deadline"], "challenge deadline")
        _clean_id(safe["created_by"], "created_by")
        if "participant_ids" in safe:
            _id_list(safe["participant_ids"], "challenge participants")
        if "status" in safe:
            status = _text(safe["status"], "challenge status", 20)
            if status not in CHALLENGE_STATUSES:
                raise SyncValidationError("Household challenge status is unsupported.")
        return safe

    if kind == "challenge_result":
        required = {"challenge_id", "member_id", "value"}
        if set(safe) - CHALLENGE_RESULT_FIELDS or not required <= set(safe):
            raise SyncValidationError("Household challenge result payload is invalid.")
        challenge_id = _clean_id(safe["challenge_id"], "challenge_id")
        member_id = _clean_id(safe["member_id"], "member_id")
        expected = f"challenge_result:{challenge_id}:{member_id}"
        if _namespace_key(sync_key, kind) != expected:
            raise SyncValidationError("Household challenge result identity does not match its key.")
        _number(safe["value"], "challenge result value")
        if "completed" in safe and not isinstance(safe["completed"], bool):
            raise SyncValidationError("Household challenge result completed must be boolean.")
        if "completed_at" in safe and safe["completed_at"] is not None:
            _timestamp(safe["completed_at"], "challenge result completion")
        if "game_id" in safe:
            _clean_id(safe["game_id"], "game_id")
        if "note" in safe:
            _text(safe["note"], "challenge result note", MAX_RESULT_NOTE, required=False)
        return safe

    if kind == "share":
        required = {"share_id", "member_id", "period", "stats"}
        if set(safe) - SHARE_FIELDS or not required <= set(safe):
            raise SyncValidationError("Household share payload is invalid.")
        share_id = _clean_id(safe["share_id"], "share_id")
        if len(share_id) > MAX_SHARE_ID:
            raise SyncValidationError("Household share_id is too long.")
        member_id = _clean_id(safe["member_id"], "member_id")
        if _namespace_key(sync_key, kind) != f"share:{share_id}":
            raise SyncValidationError("Household share identity does not match its key.")
        _normalise_share_period(safe["period"])
        _stats(safe["stats"])
        window_start = window_end = None
        if "window_start" in safe:
            window_start = _period_bound(safe["window_start"], "share window start")
        if "window_end" in safe:
            window_end = _period_bound(safe["window_end"], "share window end")
        if window_start is not None and window_end is not None:
            if _timestamp_key(window_start) >= _timestamp_key(window_end):
                raise SyncValidationError("Household share window is invalid.")
        return safe

    raise SyncValidationError("Household record kind is unsupported.")


def _without_id(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key not in {"event_id", "record_id"}}


def record_id(record: dict[str, Any]) -> str:
    """Return the content hash for a record, independent of its id alias."""
    return hashlib.sha256(_canonical(_without_id(record))).hexdigest()


event_id = record_id


def make_record(*, kind: str, device_id: str, sequence: int, sync_key: str | None = None,
                payload: dict[str, Any] | None = None, parents: Iterable[str] = (),
                tombstone: bool = False, created_at: str | None = None,
                record_key: str | None = None) -> dict[str, Any]:
    """Build one immutable household record and validate its content hash."""
    kind = _text(kind, "record kind", 32)
    if kind not in RECORD_KIND_SET:
        raise SyncValidationError("Household record kind is unsupported.")
    device_id = _clean_id(device_id, "device_id")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise SyncValidationError("Household record sequence must be positive.")
    if sync_key is None:
        if record_key is None:
            raise SyncValidationError("Household sync key is required.")
        raw_key = _clean_id(record_key, "record_key")
        sync_key = raw_key if raw_key.startswith(f"{kind}:") else f"{kind}:{raw_key}"
    elif record_key is not None and sync_key != record_key:
        raise SyncValidationError("Household record has conflicting sync keys.")
    sync_key = _namespace_key(sync_key, kind)
    parent_ids = [_clean_id(parent, "parent record ID")
                  for parent in _bounded_iterable(parents, "record parents", MAX_PARENTS)]
    if len(set(parent_ids)) != len(parent_ids):
        raise SyncValidationError("Household record parents must be unique.")
    when = _utc_now() if created_at is None else _timestamp(created_at, "record")
    if tombstone and payload is not None:
        raise SyncValidationError("A household tombstone cannot contain a payload.")
    if not tombstone and payload is None:
        raise SyncValidationError("A household record needs a payload or tombstone.")
    result: dict[str, Any] = {
        "format": HOUSEHOLD_FORMAT,
        "device_id": device_id,
        "sequence": sequence,
        "created_at": when,
        "sync_key": sync_key,
        "kind": kind,
        "parents": sorted(parent_ids),
    }
    if tombstone:
        result["tombstone"] = True
    else:
        result["payload"] = copy.deepcopy(_validate_payload(kind, payload, sync_key))
    result["event_id"] = record_id(result)
    return validate_record(result)


def _normalise_aliases(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise SyncValidationError("Household record must be an object.")
    try:
        result = copy.deepcopy(record)
    except (TypeError, ValueError) as error:
        raise SyncValidationError("Household record cannot be copied safely.") from error
    if "record_id" in result:
        if "event_id" in result and result["event_id"] != result["record_id"]:
            raise SyncValidationError("Household record has conflicting identities.")
        result["event_id"] = result.pop("record_id")
    if "record_key" in result:
        if "sync_key" in result and result["sync_key"] != result["record_key"]:
            raise SyncValidationError("Household record has conflicting sync keys.")
        result["sync_key"] = result.pop("record_key")
    return result


def validate_record(record: Any) -> dict[str, Any]:
    """Validate and return a detached household record copy."""
    checked = _normalise_aliases(record)
    if set(checked) - RECORD_FIELDS:
        raise SyncValidationError("Household record shape is invalid.")
    required = {"format", "event_id", "device_id", "sequence", "created_at", "sync_key", "kind", "parents"}
    if set(checked) - {"payload", "tombstone"} != required:
        raise SyncValidationError("Household record has missing or unexpected fields.")
    if checked.get("format") != HOUSEHOLD_FORMAT:
        raise SyncValidationError("Unsupported household record format.")
    actual_id = _clean_id(checked.get("event_id"), "record_id")
    if len(actual_id) != 64 or any(char not in "0123456789abcdef" for char in actual_id):
        raise SyncValidationError("Household record ID is invalid.")
    if record_id(checked) != actual_id:
        raise SyncValidationError("Household record content hash does not match its ID.")
    _clean_id(checked.get("device_id"), "device_id")
    if isinstance(checked.get("sequence"), bool) or not isinstance(checked.get("sequence"), int) or checked["sequence"] < 1:
        raise SyncValidationError("Household record sequence is invalid.")
    _timestamp(checked.get("created_at"), "record")
    kind = checked.get("kind")
    if not isinstance(kind, str) or kind not in RECORD_KIND_SET:
        raise SyncValidationError("Household record kind is unsupported.")
    sync_key = _namespace_key(checked.get("sync_key"), kind)
    parents = checked.get("parents")
    if not isinstance(parents, list) or len(parents) > MAX_PARENTS:
        raise SyncValidationError("Household record parents are invalid.")
    for parent in parents:
        parent_id = _clean_id(parent, "parent record ID")
        if len(parent_id) != 64 or any(char not in "0123456789abcdef" for char in parent_id):
            raise SyncValidationError("Household parent record ID is invalid.")
    if len(set(parents)) != len(parents) or parents != sorted(parents):
        raise SyncValidationError("Household record parents must be unique and sorted.")
    has_payload = "payload" in checked
    has_tombstone = "tombstone" in checked
    if has_payload == has_tombstone or has_tombstone and checked.get("tombstone") is not True:
        raise SyncValidationError("Household record needs exactly one payload or tombstone.")
    if has_payload:
        _validate_payload(kind, checked["payload"], sync_key)
    if len(_canonical(checked)) > MAX_RECORD_BYTES:
        raise SyncValidationError("Household record is too large.")
    return copy.deepcopy(checked)


def validate_record_set(records: Iterable[dict[str, Any]], known_records: Iterable[dict[str, Any]] = ()) -> list[dict[str, Any]]:
    """Validate a bounded record set, duplicate identities, and its DAG."""
    values = _bounded_records(records, "Household records")
    known_values = _bounded_records(known_records, "Known household records")
    if len(values) + len(known_values) > MAX_RECORDS:
        raise SyncValidationError("Too many household records.")
    known: dict[str, dict[str, Any]] = {}
    for item in known_values:
        checked = validate_record(item)
        ident = checked["event_id"]
        if ident in known and known[ident] != checked:
            raise SyncValidationError("Duplicate household record identity.")
        known[ident] = checked
    result: dict[str, dict[str, Any]] = {}
    for item in values:
        checked = validate_record(item)
        ident = checked["event_id"]
        if ident in result:
            raise SyncValidationError("Duplicate household record identity.")
        if ident in known:
            if known[ident] != checked:
                raise SyncValidationError("Duplicate household record identity.")
            continue
        result[ident] = checked
    combined = {**known, **result}
    for checked in result.values():
        for parent in checked["parents"]:
            if parent not in combined:
                raise SyncValidationError("Household record DAG has a missing parent.")
            if combined[parent]["sync_key"] != checked["sync_key"]:
                raise SyncValidationError("Household parent belongs to another record key.")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(ident: str) -> None:
        if ident in visiting:
            raise SyncValidationError("Household record DAG contains a cycle.")
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


def _bounded_records(records: Iterable[dict[str, Any]], label: str) -> list[Any]:
    if records is None:
        return []
    try:
        iterator = iter(records)
    except TypeError as error:
        raise SyncValidationError(f"{label} must be iterable.") from error
    result = []
    for index, item in enumerate(iterator):
        if index >= MAX_RECORDS:
            raise SyncValidationError("Too many household records.")
        result.append(item)
    return result


def household_sync_folder(folder: str | os.PathLike[str], *, create: bool = False) -> Path:
    """Return the versioned Household transport namespace below ``folder``."""
    root = Path(folder).expanduser()
    if not root.is_dir():
        raise SyncFolderError(f"Sync folder does not exist: {root}")
    target = root / HOUSEHOLD_SYNC_DIRECTORY
    if target.is_symlink() or target.exists() and not target.is_dir():
        raise SyncFolderError("Household sync namespace is not a directory.")
    if create:
        try:
            events = target / HOUSEHOLD_EVENT_DIRECTORY
            if events.is_symlink() or events.exists() and not events.is_dir():
                raise SyncFolderError("Household sync event directory is not a directory.")
            events.mkdir(parents=True, exist_ok=True)
            os.chmod(target, 0o700)
            os.chmod(events, 0o700)
        except OSError as error:
            raise SyncFolderError(f"Unable to create household sync folder: {target}") from error
    return target


def household_event_path(folder: str | os.PathLike[str], ident: str) -> Path:
    """Return the private path for one content-addressed Household record."""
    ident = _clean_id(ident, "record_id")
    if len(ident) != 64 or any(char not in "0123456789abcdef" for char in ident):
        raise SyncValidationError("Household record ID is invalid.")
    return household_sync_folder(folder, create=True) / HOUSEHOLD_EVENT_DIRECTORY / f"{ident}.json"


@contextmanager
def _household_transport_lock(folder: str | os.PathLike[str], *, create: bool = False):
    """Serialize local shared-folder readers/writers without holding state locks."""
    target = household_sync_folder(folder, create=create)
    if not target.exists():
        yield target
        return
    lock_path = target / HOUSEHOLD_LOCK_FILENAME
    try:
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            os.chmod(lock_path, 0o600)
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            except OSError as error:
                raise SyncFolderError("Unable to lock household sync folder.") from error
            try:
                yield target
            finally:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
    except SyncFolderError:
        raise
    except OSError as error:
        raise SyncFolderError("Unable to access household sync folder.") from error


def _read_household_record_file(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise SyncValidationError(f"Household record file is not a regular file: {path.name}")
    try:
        if path.stat().st_size > MAX_RECORD_BYTES + 1:
            raise SyncValidationError(f"Household record is too large: {path.name}")
        with path.open("rb") as source:
            raw = source.read(MAX_RECORD_BYTES + 2)
        if len(raw) > MAX_RECORD_BYTES + 1:
            raise SyncValidationError(f"Household record is too large: {path.name}")
        payload = json.loads(raw.decode("utf-8"))
    except SyncValidationError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise SyncValidationError(f"Invalid household record file: {path.name}") from error
    checked = validate_record(payload)
    if path.stem != checked["event_id"]:
        raise SyncValidationError(f"Household record filename does not match its hash: {path.name}")
    return checked


def _read_household_records_at_root(target: Path) -> list[dict[str, Any]]:
    events_dir = target / HOUSEHOLD_EVENT_DIRECTORY
    if not target.exists():
        return []
    if target.is_symlink() or not target.is_dir():
        raise SyncFolderError("Household sync namespace is not a directory.")
    if not events_dir.exists():
        return []
    if events_dir.is_symlink() or not events_dir.is_dir():
        raise SyncFolderError("Household sync event directory is not a directory.")
    paths = sorted(events_dir.glob("*.json"))
    if len(paths) > MAX_HOUSEHOLD_RECORDS:
        raise SyncValidationError("Too many household sync records.")
    records = [_read_household_record_file(path) for path in paths]
    validate_record_set(records)
    return records


def read_household_records(folder: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Read a complete, bounded Household event set without changing its files."""
    with _household_transport_lock(folder) as target:
        return _read_household_records_at_root(target)


def _write_household_record_at_root(target: Path, record: dict[str, Any]) -> Path:
    checked = validate_record(record)
    events_dir = target / HOUSEHOLD_EVENT_DIRECTORY
    target_path = events_dir / f"{checked['event_id']}.json"
    encoded = _canonical(checked).decode("utf-8") + "\n"
    if target_path.exists():
        existing = _read_household_record_file(target_path)
        if existing == checked:
            return target_path
        raise SyncValidationError(f"Immutable household record differs from existing file: {target_path.name}")
    try:
        atomic_write_text(target_path, encoded, mode=0o600)
        os.chmod(target_path, 0o600)
    except OSError as error:
        raise SyncFolderError("Unable to publish household record.") from error
    return target_path


def write_household_record(folder: str | os.PathLike[str], record: dict[str, Any]) -> Path:
    """Atomically publish one immutable Household record with owner-only mode."""
    checked = validate_record(record)
    with _household_transport_lock(folder, create=True) as target:
        return _write_household_record_at_root(target, checked)


def write_household_records(folder: str | os.PathLike[str], records: Iterable[dict[str, Any]]) -> list[Path]:
    """Validate a complete set before atomically publishing its record files."""
    checked = validate_record_set(records)
    with _household_transport_lock(folder, create=True) as target:
        return [_write_household_record_at_root(target, item) for item in checked]


# Familiar aliases keep the Household transport interoperable with callers that
# already use the library-sync event vocabulary.
sync_folder = household_sync_folder
event_path = household_event_path
read_events = read_household_records
read_event_files = read_household_records
read_shared_records = read_household_records
write_event = write_household_record
write_events = write_household_records


def _ancestors(ident: str, records: dict[str, dict[str, Any]]) -> set[str]:
    result: set[str] = set()
    todo = list(records.get(ident, {}).get("parents", []))
    while todo:
        current = todo.pop()
        if current in result:
            continue
        result.add(current)
        todo.extend(records.get(current, {}).get("parents", []))
    return result


def _is_ancestor(older: str, newer: str, records: dict[str, dict[str, Any]]) -> bool:
    return older == newer or older in _ancestors(newer, records)


def _timestamp_key(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _activity_datetime(value: Any) -> datetime | None:
    """Parse an activity timestamp, accepting legacy naive/date-only values."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _activity_timestamp(value: Any, fields: tuple[str, ...]) -> datetime | None:
    if not isinstance(value, dict):
        return None
    for field in fields:
        parsed = _activity_datetime(value.get(field))
        if parsed is not None:
            return parsed
    return None


def _stats_window(period: Any, now: Any = None) -> tuple[str, datetime | None, datetime]:
    """Return a UTC half-open calendar window for a stats share.

    ``now`` is injectable for deterministic callers/tests.  A date-only value
    denotes the whole named date, while a timestamp denotes the interval up to
    that instant.  All-time intentionally has no lower bound.
    """
    normalised = _normalise_share_period(period)
    date_only = isinstance(now, str) and len(now.strip()) == 10
    if now is None:
        end = datetime.now(timezone.utc)
    else:
        end = _activity_datetime(now)
        if end is None:
            raise SyncValidationError("Household stats window timestamp is invalid.")
        if date_only:
            end += timedelta(days=1)
    if normalised == "all_time":
        return normalised, None, end
    calendar_date = end.date() - timedelta(days=1) if date_only else end.date()
    if normalised == "daily":
        start = datetime.combine(calendar_date, datetime.min.time(), tzinfo=timezone.utc)
    elif normalised == "weekly":
        week_start = calendar_date - timedelta(days=calendar_date.weekday())
        start = datetime.combine(week_start, datetime.min.time(), tzinfo=timezone.utc)
    else:  # monthly
        start = datetime(calendar_date.year, calendar_date.month, 1, tzinfo=timezone.utc)
    return normalised, start, end


def _in_stats_window(value: Any, start: datetime | None, end: datetime) -> bool:
    parsed = _activity_datetime(value)
    return parsed is not None and (start is None or parsed >= start) and parsed < end


def _share_matches_window(payload: dict[str, Any], period: str, reference: Any = None) -> bool:
    """Reject an explicitly bounded share that belongs to an older period.

    Shares created by older clients have no bounds and remain valid for
    compatibility.  Once bounds are present, a finite-period leaderboard only
    accepts the current calendar period; all-time has no lower bound.
    """
    if period == "all_time" or "window_start" not in payload:
        return True
    _normalised, expected_start, _expected_end = _stats_window(period, reference)
    actual_start = _activity_datetime(payload.get("window_start"))
    if actual_start is None or expected_start is None or actual_start != expected_start:
        return False
    actual_end = payload.get("window_end")
    if actual_end is not None:
        parsed_end = _activity_datetime(actual_end)
        if parsed_end is None or parsed_end <= actual_start:
            return False
    return True


def _stat_number(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    try:
        return max(0, int(value))
    except (OverflowError, ValueError):
        return 0


def _game_identifier(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get("game_id") or value.get("id") or "").strip()


def _history_identifier(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get("game_id") or value.get("stable_game_id") or "").strip()


def _completion_marker(value: Any) -> bool:
    return any(marker in str(value or "").casefold() for marker in COMPLETION_MARKERS)


def _windowed_stats(games: list[Any], history: list[Any], start: datetime, end: datetime) -> dict[str, int]:
    """Derive bounded stats without copying lifetime counters into a period.

    Session history is authoritative for time/count metrics when present.  A
    game row's ``last_played`` is the existing timestamp fallback for libraries
    with history tracking disabled, and is also the only safe association for
    its current completion/RA counters.  No un-timestamped row is attributed to
    a finite window.
    """
    game_rows: dict[str, dict[str, Any]] = {}
    game_ids: set[str] = set()
    completion_game_ids: set[str] = set()
    ra_game_ids: set[str] = set()
    playtime = completions = ra_count = sessions = wins = 0

    for game in games:
        if not isinstance(game, dict):
            continue
        game_id = _game_identifier(game)
        if not game_id:
            continue
        activity_at = _activity_timestamp(game, ("last_played", "played_at"))
        completion_at = _activity_timestamp(game, ("completed_at", "finished_at", "completion_date"))
        ra_at = _activity_timestamp(game, ("ra_updated_at", "achievements_updated_at"))
        in_window = (
            activity_at is not None and start <= activity_at < end
        )
        if in_window:
            game_rows[game_id] = game
            game_ids.add(game_id)
            if _completion_marker(game.get("progress")):
                completion_game_ids.add(game_id)
            if ra_at is not None and start <= ra_at < end:
                ra_game_ids.add(game_id)
            elif ra_at is None:
                # ``last_played`` is the only timestamp available on most
                # imported game rows; use it for the current RA counter too.
                ra_game_ids.add(game_id)
        elif completion_at is not None and start <= completion_at < end and _completion_marker(game.get("progress")):
            completion_game_ids.add(game_id)

    history_game_ids: set[str] = set()
    history_completion_ids: set[str] = set()
    completions = len(completion_game_ids)
    for entry in history:
        if not isinstance(entry, dict):
            continue
        started_at = _activity_timestamp(
            entry, ("started_at", "started", "played_at", "played_on", "timestamp", "created_at")
        )
        if started_at is None or not (start <= started_at < end):
            continue
        sessions += 1
        seconds = _stat_number(entry.get("seconds", entry.get("playtime_seconds", 0)))
        playtime += seconds
        game_id = _history_identifier(entry)
        if game_id:
            game_ids.add(game_id)
            history_game_ids.add(game_id)
        result = str(entry.get("result") or "").casefold()
        completed = bool(entry.get("completed")) or _completion_marker(entry.get("progress"))
        won = result in {"win", "won", "complete", "completed"}
        if won:
            wins += 1
        if completed or won:
            if game_id:
                if game_id not in completion_game_ids and game_id not in history_completion_ids:
                    completions += 1
                history_completion_ids.add(game_id)
            else:
                completions += 1
        for field in ("ra_count_delta", "ra_delta", "achievements_delta", "ra_achievements_delta"):
            if field in entry:
                ra_count += _stat_number(entry.get(field))
                break

    for game_id, game in game_rows.items():
        if game_id not in history_game_ids:
            # No session row exists for this game in the period, so this is a
            # bounded-window fallback rather than an addition to session time.
            playtime += _stat_number(game.get("playtime_seconds", 0))
        if game_id in ra_game_ids:
            ra_count += _stat_number(game.get("ra_count", game.get("achievements", game.get("ra_achievements", 0))))

    return {
        "playtime_seconds": min(playtime, MAX_STAT_VALUE),
        "completions": min(completions, MAX_STAT_VALUE),
        "ra_count": min(ra_count, MAX_STAT_VALUE),
        "games_played": min(len(game_ids), MAX_STAT_VALUE),
        "sessions": min(sessions, MAX_STAT_VALUE),
        "wins": min(wins, MAX_STAT_VALUE),
    }


def _record_order_key(record: dict[str, Any]) -> tuple[Any, int, str, str]:
    return (
        _timestamp_key(record.get("created_at")),
        int(record.get("sequence") or 0),
        str(record.get("device_id") or ""),
        str(record.get("event_id") or ""),
    )


def _heads(records: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    by_key: dict[str, list[str]] = {}
    for ident, record in records.items():
        by_key.setdefault(record["sync_key"], []).append(ident)
    result = {}
    for key, identifiers in by_key.items():
        result[key] = sorted(
            ident for ident in identifiers
            if not any(ident != other and _is_ancestor(ident, other, records) for other in identifiers)
        )
    return result


def converge_records(records: Iterable[dict[str, Any]], known_records: Iterable[dict[str, Any]] = ()) -> list[dict[str, Any]]:
    """Return one deterministic latest record per logical key.

    Causal descendants win over their ancestors even when clocks disagree.
    Incomparable heads are selected by timestamp, sequence, device id, and
    content hash.  A tombstone is a normal record and therefore remains the
    selected value until a later non-tombstone record supersedes it.
    """
    known_values = _bounded_records(known_records, "Known household records")
    incoming = validate_record_set(records, known_values)
    known = [validate_record(item) for item in known_values]
    combined: dict[str, dict[str, Any]] = {item["event_id"]: item for item in known}
    combined.update({item["event_id"]: item for item in incoming})
    selected = []
    for _key, identifiers in _heads(combined).items():
        selected.append(copy.deepcopy(max((combined[ident] for ident in identifiers), key=_record_order_key)))
    selected.sort(key=lambda item: (item["sync_key"], _record_order_key(item)))
    return selected


def latest_valid(records: Iterable[dict[str, Any]], sync_key: str | None = None, *, strict: bool = False) -> dict[str, Any] | None:
    """Select the latest valid record, skipping bad candidates by default.

    Ingestion and convergence use strict validation.  This read helper is
    intentionally tolerant so one corrupt peer file does not hide a valid
    local record; pass ``strict=True`` when rejection is required.
    """
    values = []
    for item in _bounded_records(records, "Household records"):
        try:
            checked = validate_record(item)
        except SyncValidationError:
            if strict:
                raise
            continue
        if sync_key is None or checked["sync_key"] == sync_key:
            values.append(checked)
    if not values:
        return None
    by_id = {item["event_id"]: item for item in values}
    heads = _heads(by_id)
    candidates = [by_id[ident] for ident in heads.get(sync_key, [])] if sync_key else [
        by_id[ident] for ids in heads.values() for ident in ids
    ]
    return copy.deepcopy(max(candidates, key=_record_order_key)) if candidates else None


latest_valid_record = latest_valid
select_latest = latest_valid


def current_records(records: Iterable[dict[str, Any]], known_records: Iterable[dict[str, Any]] = ()) -> list[dict[str, Any]]:
    return converge_records(records, known_records)


def materialize_records(records: Iterable[dict[str, Any]], known_records: Iterable[dict[str, Any]] = ()) -> dict[str, dict[str, Any]]:
    """Return the current live payloads, omitting selected tombstones."""
    return {
        item["sync_key"]: copy.deepcopy(item["payload"])
        for item in converge_records(records, known_records)
        if "payload" in item
    }


def make_member_record(member_id: str, display_name: str = "", avatar_color: str = "blue", *,
                       device_id: str = "local", sequence: int = 1, stats_shared: bool = False,
                       parents: Iterable[str] = (), tombstone: bool = False,
                       created_at: str | None = None) -> dict[str, Any]:
    payload = None if tombstone else {
        "member_id": _clean_id(member_id, "member_id"),
        "display_name": display_name,
        "avatar_color": avatar_color,
        "stats_shared": stats_shared,
    }
    return make_record(
        kind="member", device_id=device_id, sequence=sequence,
        sync_key=f"member:{_clean_id(member_id, 'member_id')}", payload=payload,
        parents=parents, tombstone=tombstone, created_at=created_at,
    )


def make_challenge_record(challenge_id: str, title: str = "", *, device_id: str = "local", sequence: int = 1,
                          created_by: str | None = None, metric: str = "completions", target: int = 1,
                          description: str = "", game_id: str | None = None, deadline: str | None = None,
                          participant_ids: Iterable[str] = (), status: str = "open",
                          parents: Iterable[str] = (), tombstone: bool = False,
                          created_at: str | None = None) -> dict[str, Any]:
    challenge_id = _clean_id(challenge_id, "challenge_id")
    creator = _clean_id(created_by if created_by is not None else device_id, "created_by")
    payload = None if tombstone else {
        "challenge_id": challenge_id,
        "title": title,
        "metric": metric,
        "target": target,
        "created_by": creator,
        "status": status,
    }
    if not tombstone:
        if description:
            payload["description"] = description
        if game_id is not None:
            payload["game_id"] = _clean_id(game_id, "game_id")
        if deadline is not None:
            payload["deadline"] = deadline
        participants = _bounded_iterable(participant_ids, "challenge participants", MAX_PARTICIPANTS)
        if participants:
            payload["participant_ids"] = sorted(_clean_id(item, "participant_id") for item in participants)
    return make_record(
        kind="challenge", device_id=device_id, sequence=sequence,
        sync_key=f"challenge:{challenge_id}", payload=payload,
        parents=parents, tombstone=tombstone, created_at=created_at,
    )


def make_challenge_result_record(challenge_id: str, member_id: str, value: int = 0, *,
                                 device_id: str = "local", sequence: int = 1, completed: bool | None = None,
                                 completed_at: str | None = None, game_id: str | None = None,
                                 note: str | None = None, parents: Iterable[str] = (),
                                 tombstone: bool = False, created_at: str | None = None) -> dict[str, Any]:
    challenge_id = _clean_id(challenge_id, "challenge_id")
    member_id = _clean_id(member_id, "member_id")
    payload = None if tombstone else {
        "challenge_id": challenge_id,
        "member_id": member_id,
        "value": value,
        "completed": False if completed is None else completed,
    }
    if not tombstone:
        if completed_at is not None:
            payload["completed_at"] = completed_at
        if game_id is not None:
            payload["game_id"] = _clean_id(game_id, "game_id")
        if note is not None:
            payload["note"] = note
    return make_record(
        kind="challenge_result", device_id=device_id, sequence=sequence,
        sync_key=f"challenge_result:{challenge_id}:{member_id}", payload=payload,
        parents=parents, tombstone=tombstone, created_at=created_at,
    )


def make_share_record(member_id: str, period: str = "all_time", stats: dict[str, Any] | None = None, *, share_id: str | None = None,
                      device_id: str = "local", sequence: int = 1, window_start: str | None = None,
                      window_end: str | None = None, parents: Iterable[str] = (),
                      tombstone: bool = False, created_at: str | None = None,
                      enabled: bool | None = None, stats_shared: bool | None = None,
                      state: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Build an explicit stats share; explicit opt-out returns no record."""
    if enabled is False or stats_shared is False or state is not None and not stats_sharing_enabled(state):
        return None
    member_id = _clean_id(member_id, "member_id")
    period = _normalise_share_period(period)
    if share_id is None:
        share_id = f"{member_id}:{period}"
    share_id = _clean_id(share_id, "share_id")
    payload = None if tombstone else {
        "share_id": share_id,
        "member_id": member_id,
        "period": period,
        "stats": {} if stats is None else stats,
    }
    if not tombstone:
        if window_start is not None:
            payload["window_start"] = window_start
        if window_end is not None:
            payload["window_end"] = window_end
    return make_record(
        kind="share", device_id=device_id, sequence=sequence,
        sync_key=f"share:{share_id}", payload=payload,
        parents=parents, tombstone=tombstone, created_at=created_at,
    )


build_record = make_record
build_member_record = make_member_record
build_challenge_record = make_challenge_record
build_challenge_result_record = make_challenge_result_record
build_share_record = make_share_record
member_record = make_member_record
challenge_record = make_challenge_record
challenge_result_record = make_challenge_result_record
share_record = make_share_record


def make_tombstone_record(kind: str, sync_key: str, *, device_id: str = "local", sequence: int = 1,
                          parents: Iterable[str] = (), created_at: str | None = None) -> dict[str, Any]:
    return make_record(
        kind=kind, device_id=device_id, sequence=sequence, sync_key=sync_key,
        parents=parents, tombstone=True, created_at=created_at,
    )


def tombstone_record(record: dict[str, Any], *, device_id: str = "local", sequence: int = 1,
                     parents: Iterable[str] | None = None, created_at: str | None = None) -> dict[str, Any]:
    checked = validate_record(record)
    parent_ids = list(checked["parents"] if parents is None else parents)
    parent_ids.append(checked["event_id"])
    return make_tombstone_record(
        checked["kind"], checked["sync_key"], device_id=device_id, sequence=sequence,
        parents=parent_ids, created_at=created_at,
    )


delete_record = tombstone_record


def stats_sharing_enabled(value: Any) -> bool:
    """Return true only for an explicit per-device stats-sharing opt-in."""
    if isinstance(value, bool):
        return value
    if not isinstance(value, dict):
        return False
    candidates = [value]
    settings = value.get("settings")
    if isinstance(settings, dict):
        candidates.append(settings)
    household = value.get(HOUSEHOLD_STATE_KEY)
    if isinstance(household, dict):
        candidates.append(household)
    for candidate in candidates:
        for key in (
            "household_stats_sharing",
            "household_stats_enabled",
            "household_share_stats",
            "stats_sharing_enabled",
            "share_stats",
            "share_household_stats",
        ):
            if candidate.get(key) is True:
                return True
    return False


share_stats_enabled = stats_sharing_enabled
stats_opted_in = stats_sharing_enabled


def collect_stats(games: Any, history: Any = None, period: str = "all_time", *,
                  now: Any = None, as_of: Any = None) -> dict[str, int]:
    """Derive share-safe stats for an all-time or calendar activity window.

    All-time preserves the existing cumulative game counters.  Bounded
    periods use timestamped session history and ``last_played`` game rows, so
    a weekly/daily/monthly share cannot silently contain the lifetime totals.
    ``as_of`` is an alias for ``now`` for callers that prefer release-window
    terminology; supplying both is ambiguous and rejected.
    """
    if now is not None and as_of is not None:
        raise SyncValidationError("Household stats accepts only one window timestamp.")
    if isinstance(games, dict):
        state = games
        if history is None:
            history = state.get("history")
        games = state.get("games")
    games = games if isinstance(games, list) else []
    history = history if isinstance(history, list) else []
    reference = as_of if as_of is not None else now
    normalised, start, end = _stats_window(period, reference)
    if normalised != "all_time":
        return _windowed_stats(games, history, start, end)

    game_ids = set()
    playtime = completions = ra_count = games_played = wins = 0
    for game in games:
        if not isinstance(game, dict):
            continue
        game_id = _game_identifier(game)
        if game_id:
            game_ids.add(game_id)
        seconds = game.get("playtime_seconds", 0)
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            seconds = 0
        try:
            playtime += max(0, int(seconds))
        except (OverflowError, ValueError):
            pass
        if _completion_marker(game.get("progress")):
            completions += 1
        ra = game.get("ra_count", game.get("achievements", game.get("ra_achievements", 0)))
        if isinstance(ra, bool) or not isinstance(ra, (int, float)):
            ra = 0
        try:
            ra_count += max(0, int(ra))
        except (OverflowError, ValueError):
            pass
        plays = game.get("play_count", 0)
        if isinstance(plays, bool) or not isinstance(plays, (int, float)):
            plays = 0
        if plays > 0 or seconds > 0:
            games_played += 1
    sessions = 0
    for entry in history:
        if not isinstance(entry, dict):
            continue
        sessions += 1
        game_id = _history_identifier(entry)
        if game_id:
            game_ids.add(game_id)
        if str(entry.get("result") or "").casefold() in {"win", "won", "complete", "completed"}:
            wins += 1
    games_played = max(games_played, len(game_ids) if history else games_played)
    return {
        "playtime_seconds": min(playtime, MAX_STAT_VALUE),
        "completions": min(completions, MAX_STAT_VALUE),
        "ra_count": min(ra_count, MAX_STAT_VALUE),
        "games_played": min(games_played, MAX_STAT_VALUE),
        "sessions": min(sessions, MAX_STAT_VALUE),
        "wins": min(wins, MAX_STAT_VALUE),
    }


compute_stats = collect_stats


def shareable_stats(value: Any, *, state: Any = None) -> dict[str, int] | None:
    """Return only share-safe stats when the caller explicitly opted in."""
    if state is not None and not stats_sharing_enabled(state):
        return None
    return _stats(value)


def _state_bucket(state: Any) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    if not isinstance(state, dict):
        raise SyncValidationError("Household state must be an object.")
    raw = state.get(HOUSEHOLD_STATE_KEY)
    if raw is None:
        return {}, {}
    if not isinstance(raw, dict):
        raise SyncValidationError("Household state is invalid.")
    stored = raw.get("records", {})
    if isinstance(stored, dict):
        values = list(stored.values())
        if any(key != item.get("event_id") for key, item in stored.items() if isinstance(item, dict)):
            raise SyncValidationError("Household record map key is invalid.")
    elif isinstance(stored, list):
        values = list(stored)
    else:
        raise SyncValidationError("Household state records are invalid.")
    checked_values = validate_record_set(values)
    if len(checked_values) != len(values):
        raise SyncValidationError("Household state contains duplicate records.")
    records = {item["event_id"]: item for item in checked_values}
    bucket = copy.deepcopy(raw)
    bucket["records"] = copy.deepcopy(records)
    outbox = bucket.get("outbox", [])
    if not isinstance(outbox, list):
        raise SyncValidationError("Household outbox is invalid.")
    checked_outbox = []
    for item in outbox:
        checked = validate_record(item)
        if checked["event_id"] not in records:
            raise SyncValidationError("Household outbox record is not in history.")
        if checked["event_id"] not in {row["event_id"] for row in checked_outbox}:
            checked_outbox.append(checked)
    bucket["outbox"] = checked_outbox
    device = bucket.get("device_id")
    if device is not None:
        _clean_id(device, "device_id")
    sequence = bucket.get("next_sequence", 1)
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise SyncValidationError("Household next sequence is invalid.")
    bucket["heads"] = _heads(records)
    return bucket, records


def acknowledge_outbox(state: dict[str, Any], record_ids: Iterable[str]) -> int:
    """Acknowledge only records that were durably published to the folder."""
    bucket, _records = _state_bucket(state)
    identifiers = _bounded_iterable(record_ids, "Household acknowledgement IDs", MAX_RECORDS)
    checked_ids = set()
    for ident in identifiers:
        value = _clean_id(ident, "record_id")
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise SyncValidationError("Household acknowledgement ID is invalid.")
        checked_ids.add(value)
    before = len(bucket["outbox"])
    bucket["outbox"] = [item for item in bucket["outbox"] if item["event_id"] not in checked_ids]
    state[HOUSEHOLD_STATE_KEY] = bucket
    return before - len(bucket["outbox"])


acknowledge_household_outbox = acknowledge_outbox


def _household_record_can_publish(state: dict[str, Any], record: dict[str, Any]) -> bool:
    """Apply the existing per-device stats opt-in at the transport boundary."""
    return record.get("kind") != "share" or stats_sharing_enabled(state)


def publish_household_outbox(state: dict[str, Any], folder: str | os.PathLike[str]) -> dict[str, Any]:
    """Publish pending Household records and acknowledge successful writes.

    The complete causal ancestry of each pending record is retained so a fresh
    replica can validate the event graph.  Stats shares are skipped unless this
    device has the existing explicit ``household_stats_sharing`` opt-in.
    """
    bucket, records = _state_bucket(state)
    pending = [validate_record(item) for item in bucket.get("outbox", [])]
    publishable = [item for item in pending if _household_record_can_publish(state, item)]
    skipped = len(pending) - len(publishable)
    history = dict(records)
    needed: set[str] = set()
    for item in publishable:
        needed.add(item["event_id"])
        needed.update(_ancestors(item["event_id"], history))
    candidates = [history[ident] for ident in needed]
    published_ids = sorted(item["event_id"] for item in publishable)

    with _household_transport_lock(folder, create=True) as target:
        remote = _read_household_records_at_root(target)
        # Validate the local closure against the remote set before making any
        # writes.  Each record file is immutable, so existing remote records
        # are naturally preserved when another device has already published.
        validate_record_set(candidates, remote)
        combined = {item["event_id"]: item for item in remote}
        combined.update({item["event_id"]: item for item in candidates})
        ordered = sorted(
            candidates,
            key=lambda item: (len(_ancestors(item["event_id"], combined)), item["sequence"], item["event_id"]),
        )
        for item in ordered:
            _write_household_record_at_root(target, item)

    acknowledged = acknowledge_outbox(state, published_ids)
    return {
        "published": len(published_ids),
        "acknowledged": acknowledged,
        "pending": len((state.get(HOUSEHOLD_STATE_KEY) or {}).get("outbox", [])),
        "stats_skipped": skipped,
        "published_ids": published_ids,
    }


def pull_household_records(state: dict[str, Any], folder: str | os.PathLike[str]) -> dict[str, Any]:
    """Read and merge Household records from a shared folder idempotently."""
    incoming = read_household_records(folder)
    result = merge_records(state, incoming)
    acknowledged = acknowledge_outbox(state, [item["event_id"] for item in incoming])
    result["acknowledged"] = acknowledged
    return result


pull_household = pull_household_records
pull_shared_records = pull_household_records
publish_household = publish_household_outbox
publish_outbox = publish_household_outbox
pull_records = pull_household_records


def _device_for(state: dict[str, Any], bucket: dict[str, Any], device_id: str | None) -> str:
    if device_id is not None:
        return _clean_id(device_id, "device_id")
    if bucket.get("device_id"):
        return _clean_id(bucket["device_id"], "device_id")
    library = state.get("library_sync")
    if isinstance(library, dict) and library.get("device_id"):
        return _clean_id(library["device_id"], "device_id")
    return str(uuid.uuid4())


def _current_challenge(records: dict[str, dict[str, Any]], challenge_id: str) -> dict[str, Any]:
    """Return the current live challenge payload for a local result."""
    key = f"challenge:{challenge_id}"
    current = latest_valid(records.values(), key, strict=True)
    if current is None or "payload" not in current:
        raise SyncValidationError("Household challenge does not exist or was deleted.")
    return copy.deepcopy(current["payload"])


def _validate_result_against_challenge(challenge: dict[str, Any], member_id: str,
                                       game_id: Any, result_at: str,
                                       completed_at: str | None = None) -> None:
    """Apply semantic checks shared by local creation and derived progress."""
    status = str(challenge.get("status") or "open").strip().casefold()
    if status not in LOCAL_RESULT_STATUSES:
        raise SyncValidationError("Household challenge is not accepting results.")

    participants = challenge.get("participant_ids")
    if participants:
        allowed = {_clean_id(item, "challenge participant") for item in participants}
        if member_id not in allowed:
            raise SyncValidationError("Household result member is not a challenge participant.")

    challenge_game = challenge.get("game_id")
    if challenge_game is not None:
        if game_id is None or _clean_id(game_id, "game_id") != _clean_id(challenge_game, "game_id"):
            raise SyncValidationError("Household result game does not match the challenge.")

    deadline = challenge.get("deadline")
    if deadline is not None:
        completion_or_result = completed_at or result_at
        if _timestamp_key(completion_or_result) >= _timestamp_key(deadline):
            raise SyncValidationError("Household challenge deadline has passed.")


def _validate_local_challenge_result(records: dict[str, dict[str, Any]], challenge_id: str,
                                     member_id: str, game_id: Any, *,
                                     created_at: str | None = None,
                                     completed_at: str | None = None) -> tuple[str, str | None]:
    result_at = _utc_now() if created_at is None else _timestamp(created_at, "challenge result")
    completed_value = None if completed_at is None else _timestamp(completed_at, "challenge result completion")
    challenge = _current_challenge(records, challenge_id)
    _validate_result_against_challenge(
        challenge, member_id, game_id, result_at, completed_value,
    )
    return result_at, completed_value


def _store_record(state: dict[str, Any], bucket: dict[str, Any], records: dict[str, dict[str, Any]],
                  record: dict[str, Any], *, outbox: bool = True) -> dict[str, Any]:
    checked = validate_record(record)
    if checked["event_id"] in records:
        if records[checked["event_id"]] != checked:
            raise SyncValidationError("Household record identity differs from local history.")
        return copy.deepcopy(checked)
    candidate = dict(records)
    candidate[checked["event_id"]] = checked
    next_bucket = copy.deepcopy(bucket)
    next_bucket["records"] = copy.deepcopy(candidate)
    next_bucket["heads"] = _heads(candidate)
    pending = [validate_record(item) for item in next_bucket.get("outbox", [])]
    if outbox:
        pending.append(copy.deepcopy(checked))
    by_id = {}
    for item in pending:
        by_id[item["event_id"]] = item
    next_bucket["outbox"] = list(by_id.values())
    state[HOUSEHOLD_STATE_KEY] = next_bucket
    return copy.deepcopy(checked)


def append_record(state: dict[str, Any], record: dict[str, Any], *, outbox: bool = True) -> dict[str, Any]:
    """Append one validated record using a copy-on-write household bucket."""
    bucket, records = _state_bucket(state)
    checked = validate_record(record)
    # Validate the parent relationship before any assignment.
    validate_record_set([checked], records.values())
    if outbox and checked["kind"] == "challenge_result" and "payload" in checked:
        payload = checked["payload"]
        _validate_local_challenge_result(
            records,
            payload["challenge_id"],
            payload["member_id"],
            payload.get("game_id"),
            created_at=checked["created_at"],
            completed_at=payload.get("completed_at"),
        )
    return _store_record(state, bucket, records, checked, outbox=outbox)


def record_local(state: dict[str, Any], kind: str, payload: dict[str, Any] | None = None, *,
                 sync_key: str | None = None, device_id: str | None = None, sequence: int | None = None,
                 parents: Iterable[str] | None = None, tombstone: bool = False,
                 created_at: str | None = None) -> dict[str, Any]:
    """Create and append one local record, defaulting parents to current heads."""
    bucket, records = _state_bucket(state)
    kind = _text(kind, "record kind", 32)
    if kind not in RECORD_KIND_SET:
        raise SyncValidationError("Household record kind is unsupported.")
    if sync_key is None:
        if not isinstance(payload, dict):
            raise SyncValidationError("Household payload is required to derive its key.")
        if kind == "member":
            sync_key = f"member:{_clean_id(payload.get('member_id'), 'member_id')}"
        elif kind == "challenge":
            sync_key = f"challenge:{_clean_id(payload.get('challenge_id'), 'challenge_id')}"
        elif kind == "challenge_result":
            sync_key = (
                f"challenge_result:{_clean_id(payload.get('challenge_id'), 'challenge_id')}:"
                f"{_clean_id(payload.get('member_id'), 'member_id')}"
            )
        else:
            sync_key = f"share:{_clean_id(payload.get('share_id'), 'share_id')}"
    sync_key = _namespace_key(sync_key, kind)
    local_device = _device_for(state, bucket, device_id)
    if kind == "challenge_result" and not tombstone:
        if not isinstance(payload, dict):
            raise SyncValidationError("Household challenge result payload is required.")
        result_challenge_id = _clean_id(payload.get("challenge_id"), "challenge_id")
        result_member_id = _clean_id(payload.get("member_id"), "member_id")
        result_at, _completed_at = _validate_local_challenge_result(
            records,
            result_challenge_id,
            result_member_id,
            payload.get("game_id"),
            created_at=created_at,
            completed_at=payload.get("completed_at"),
        )
        # Use the validated timestamp in the immutable event as well.  This
        # avoids validating one instant and hashing a different one.
        created_at = result_at
    if sequence is None:
        sequence = bucket.get("next_sequence", 1)
    if parents is None:
        parents = bucket.get("heads", {}).get(sync_key, [])
    record = make_record(
        kind=kind, device_id=local_device, sequence=sequence, sync_key=sync_key,
        payload=payload, parents=parents, tombstone=tombstone, created_at=created_at,
    )
    bucket["device_id"] = local_device
    bucket["next_sequence"] = max(int(sequence) + 1, int(bucket.get("next_sequence", 1)) + 1)
    return _store_record(state, bucket, records, record, outbox=True)


def record_member(state: dict[str, Any], member_id: str, display_name: str, avatar_color: str = "blue", *,
                  stats_shared: bool = False, device_id: str | None = None, created_at: str | None = None) -> dict[str, Any]:
    bucket, _ = _state_bucket(state)
    local_device = _device_for(state, bucket, device_id)
    return record_local(
        state, "member", {
            "member_id": member_id, "display_name": display_name,
            "avatar_color": avatar_color, "stats_shared": stats_shared,
        }, device_id=local_device, created_at=created_at,
    )


def record_challenge(state: dict[str, Any], challenge_id: str, title: str, *,
                     created_by: str | None = None, metric: str = "completions", target: int = 1,
                     description: str = "", game_id: str | None = None, deadline: str | None = None,
                     participant_ids: Iterable[str] = (), status: str = "open",
                     device_id: str | None = None, created_at: str | None = None) -> dict[str, Any]:
    bucket, records = _state_bucket(state)
    local_device = _device_for(state, bucket, device_id)
    key = f"challenge:{_clean_id(challenge_id, 'challenge_id')}"
    parents = bucket.get("heads", {}).get(key, [])
    record = make_challenge_record(
        challenge_id, title, device_id=local_device, sequence=bucket.get("next_sequence", 1),
        created_by=created_by or local_device, metric=metric, target=target, description=description,
        game_id=game_id, deadline=deadline, participant_ids=participant_ids, status=status,
        parents=parents, created_at=created_at,
    )
    bucket["device_id"] = local_device
    bucket["next_sequence"] = int(bucket.get("next_sequence", 1)) + 1
    return _store_record(state, bucket, records, record)


def record_challenge_result(state: dict[str, Any], challenge_id: str, member_id: str, value: int, *,
                            completed: bool | None = None, completed_at: str | None = None,
                            game_id: str | None = None, note: str | None = None,
                            device_id: str | None = None, created_at: str | None = None) -> dict[str, Any]:
    bucket, records = _state_bucket(state)
    local_device = _device_for(state, bucket, device_id)
    challenge_id = _clean_id(challenge_id, "challenge_id")
    member_id = _clean_id(member_id, "member_id")
    result_at, completed_value = _validate_local_challenge_result(
        records,
        challenge_id,
        member_id,
        game_id,
        created_at=created_at,
        completed_at=completed_at,
    )
    key = f"challenge_result:{challenge_id}:{member_id}"
    record = make_challenge_result_record(
        challenge_id, member_id, value, device_id=local_device, sequence=bucket.get("next_sequence", 1),
        completed=completed, completed_at=completed_value, game_id=game_id, note=note,
        parents=bucket.get("heads", {}).get(key, []), created_at=result_at,
    )
    bucket["device_id"] = local_device
    bucket["next_sequence"] = int(bucket.get("next_sequence", 1)) + 1
    return _store_record(state, bucket, records, record)


def record_share(state: dict[str, Any], member_id: str, period: str, stats: dict[str, Any], *,
                 share_id: str | None = None, window_start: str | None = None, window_end: str | None = None,
                 device_id: str | None = None, created_at: str | None = None) -> dict[str, Any] | None:
    """Emit a share record only when this device has opted in."""
    if not stats_sharing_enabled(state):
        return None
    bucket, records = _state_bucket(state)
    local_device = _device_for(state, bucket, device_id)
    period = _normalise_share_period(period)
    sid = share_id or f"{_clean_id(member_id, 'member_id')}:{period}"
    key = f"share:{_clean_id(sid, 'share_id')}"
    record = make_share_record(
        member_id, period, stats, share_id=sid, device_id=local_device,
        sequence=bucket.get("next_sequence", 1), window_start=window_start, window_end=window_end,
        parents=bucket.get("heads", {}).get(key, []), created_at=created_at,
    )
    if record is None:
        return None
    bucket["device_id"] = local_device
    bucket["next_sequence"] = int(bucket.get("next_sequence", 1)) + 1
    return _store_record(state, bucket, records, record)


def record_stats_share(state: dict[str, Any], member_id: str, period: str, *,
                       games: Any = None, history: Any = None, stats: dict[str, Any] | None = None,
                       now: Any = None, as_of: Any = None, **kwargs: Any) -> dict[str, Any] | None:
    if now is not None and as_of is not None:
        raise SyncValidationError("Household stats accepts only one window timestamp.")
    period, start, end = _stats_window(period, as_of if as_of is not None else now)
    if stats is None:
        games_value = games
        history_value = history
        if games_value is None and history_value is None and isinstance(state, dict):
            games_value = state.get("games")
            history_value = state.get("history")
        if isinstance(games_value, dict) and history_value is None:
            history_value = games_value.get("history")
            games_value = games_value.get("games")
        games_value = games_value if isinstance(games_value, list) else []
        history_value = history_value if isinstance(history_value, list) else []
        values = (
            _windowed_stats(games_value, history_value, start, end)
            if period != "all_time"
            else collect_stats(games_value, history_value, period=period, now=as_of if as_of is not None else now)
        )
        if period != "all_time":
            kwargs.setdefault("window_start", start.isoformat())
            kwargs.setdefault("window_end", end.isoformat())
    else:
        values = stats
    return record_share(state, member_id, period, values, **kwargs)


emit_share_record = record_share
append_local_record = append_record


def household_records(state: dict[str, Any], *, include_tombstones: bool = True) -> list[dict[str, Any]]:
    bucket, records = _state_bucket(state)
    result = converge_records(records.values())
    if include_tombstones:
        return result
    return [item for item in result if "payload" in item]


current_household_records = household_records


def merge_records(state: dict[str, Any], incoming: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Ingest remote records atomically and return the converged view."""
    bucket, records = _state_bucket(state)
    checked = validate_record_set(incoming, records.values())
    candidate = dict(records)
    for item in checked:
        candidate[item["event_id"]] = item
    next_bucket = copy.deepcopy(bucket)
    next_bucket["records"] = copy.deepcopy(candidate)
    next_bucket["heads"] = _heads(candidate)
    state[HOUSEHOLD_STATE_KEY] = next_bucket
    selected = converge_records(candidate.values())
    return {
        "records": selected,
        "applied": len(checked),
        "changed": bool(checked),
        "heads": copy.deepcopy(next_bucket["heads"]),
    }


merge_household_records = merge_records
ingest_records = validate_record_set


def _payload(value: Any, kind: str | None = None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return (payload, validated record) for progress/result helpers."""
    if isinstance(value, dict) and ("event_id" in value or "record_id" in value):
        checked = validate_record(value)
        if kind is not None and checked["kind"] != kind:
            raise SyncValidationError("Household record kind is not suitable here.")
        return (None if "tombstone" in checked else copy.deepcopy(checked["payload"]), checked)
    if not isinstance(value, dict):
        raise SyncValidationError("Household helper input must be an object.")
    if kind is None:
        return copy.deepcopy(value), None
    if kind == "challenge":
        challenge_id = _clean_id(value.get("challenge_id"), "challenge_id")
        return _validate_payload(kind, value, f"challenge:{challenge_id}"), None
    if kind == "challenge_result":
        challenge_id = _clean_id(value.get("challenge_id"), "challenge_id")
        member_id = _clean_id(value.get("member_id"), "member_id")
        return _validate_payload(kind, value, f"challenge_result:{challenge_id}:{member_id}"), None
    raise SyncValidationError("Household helper kind is unsupported.")


def _result_is_eligible(challenge: dict[str, Any], payload: dict[str, Any],
                        checked: dict[str, Any] | None) -> bool:
    """Keep semantically invalid/out-of-window synced results out of views.

    Transport convergence remains intentionally order-independent: a result
    may arrive before its challenge.  Once both are available, derived views
    still fail closed for a mismatched participant/game or a late result.
    """
    participants = challenge.get("participant_ids")
    if participants is not None:
        allowed = {_clean_id(item, "challenge participant") for item in participants}
        if _clean_id(payload.get("member_id"), "member_id") not in allowed:
            return False
    challenge_game = challenge.get("game_id")
    if challenge_game is not None:
        if payload.get("game_id") is None:
            return False
        if str(payload.get("game_id")) != str(challenge_game):
            return False
    deadline = challenge.get("deadline")
    if deadline is not None:
        result_at = payload.get("completed_at") or (checked or {}).get("created_at")
        if result_at is None or _timestamp_key(result_at) >= _timestamp_key(deadline):
            return False
    return True


def _result_rows(results: Any, challenge_id: str, challenge: dict[str, Any] | None = None) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    if results is None:
        return []
    if isinstance(results, dict) and HOUSEHOLD_STATE_KEY in results:
        results = household_records(results)
    if isinstance(results, dict) and ("event_id" in results or "record_id" in results or "challenge_id" in results):
        values = [results]
    elif isinstance(results, dict):
        values = list(results.values())
    else:
        values = _bounded_records(results, "Challenge results")
    # Reduce immutable record candidates before dropping tombstones.  A
    # deleted result must not reveal an older progress value merely because
    # both the tombstone and its ancestor were supplied by a peer.
    record_values = [
        value for value in values
        if isinstance(value, dict) and ("event_id" in value or "record_id" in value)
    ]
    if record_values and len(record_values) == len(values):
        values = converge_records(record_values)
    rows = []
    for value in values:
        if isinstance(value, dict) and ("event_id" in value or "record_id" in value):
            checked_value = validate_record(value)
            if checked_value["kind"] != "challenge_result":
                continue
        payload, checked = _payload(value, "challenge_result")
        if payload is None:
            continue
        if payload["challenge_id"] != challenge_id:
            continue
        if challenge is not None and not _result_is_eligible(challenge, payload, checked):
            continue
        rows.append((payload, checked))
    return rows


def challenge_progress(challenge: Any, results: Any = None, *, member_id: str | None = None,
                       now: str | None = None) -> dict[str, Any]:
    """Compute bounded local progress from a challenge and result records."""
    challenge_payload, challenge_record_value = _payload(challenge, "challenge")
    challenge_id = challenge_payload["challenge_id"] if challenge_payload else ""
    target = int(challenge_payload.get("target", 0)) if challenge_payload else 0
    if challenge_record_value and "tombstone" in challenge_record_value:
        return {
            "challenge_id": challenge_id or challenge_record_value["sync_key"].split(":", 1)[1],
            "status": "deleted",
            "deleted": True,
            "current": 0,
            "target": target,
            "completed": False,
            "progress": 0.0,
            "members": [],
            "winner": None,
        }
    rows = _result_rows(results, challenge_id, challenge_payload)
    latest: dict[str, tuple[dict[str, Any], dict[str, Any] | None]] = {}
    for payload, checked in rows:
        member = payload["member_id"]
        if member not in latest:
            latest[member] = (payload, checked)
            continue
        old_payload, old_record = latest[member]
        if checked is not None and old_record is not None and _record_order_key(checked) > _record_order_key(old_record):
            latest[member] = (payload, checked)
        elif checked is None and old_record is None:
            latest[member] = (payload, checked)
    members = []
    for current_member, (payload, checked) in sorted(latest.items()):
        value = min(target, payload["value"]) if target else 0
        done = bool(payload.get("completed")) or payload["value"] >= target
        members.append({
            "member_id": current_member,
            "current": value,
            "target": target,
            "completed": done,
            "result": copy.deepcopy(payload),
            "record_id": checked["event_id"] if checked else None,
        })
    selected = next((row for row in members if row["member_id"] == member_id), None) if member_id else None
    current = selected["current"] if selected else max((row["current"] for row in members), default=0)
    completed = selected["completed"] if selected else any(row["completed"] for row in members)
    deadline = challenge_payload.get("deadline")
    expired = False
    if deadline is not None:
        check_at = _timestamp(now, "progress check") if now is not None else _utc_now()
        expired = _timestamp_key(check_at) >= _timestamp_key(deadline) and not completed
    status = "complete" if completed else "expired" if expired else str(challenge_payload.get("status") or "open")
    # Keep the validated records for winner selection.  The payload projection
    # used by the UI intentionally omits the record envelope, but its
    # ``created_at`` is the deterministic completion fallback when a synced
    # result has no explicit ``completed_at``.
    winner_inputs = [checked if checked is not None else payload for payload, checked in latest.values()]
    winner = challenge_winner(challenge_payload, winner_inputs, now=now)
    ratio = min(1.0, current / target) if target else 0.0
    return {
        "challenge_id": challenge_id,
        "member_id": member_id,
        "current": current,
        "target": target,
        "completed": completed,
        "complete": completed,
        "expired": expired,
        "status": status,
        "progress": ratio,
        "percent": round(ratio * 100, 2),
        "members": members,
        "winner": winner,
    }


compute_challenge_progress = challenge_progress
progress_for_challenge = challenge_progress


def challenge_winner(challenge: Any, results: Any = None, *, now: str | None = None) -> dict[str, Any] | None:
    challenge_payload, _ = _payload(challenge, "challenge")
    challenge_id = challenge_payload["challenge_id"] if challenge_payload else ""
    target = challenge_payload["target"] if challenge_payload else 0
    candidates = []
    for payload, checked in _result_rows(results, challenge_id, challenge_payload):
        if bool(payload.get("completed")) or payload["value"] >= target:
            completed_at = payload.get("completed_at") or (checked or {}).get("created_at") or ""
            candidates.append((
                _timestamp_key(completed_at),
                _record_order_key(checked) if checked else (datetime.min.replace(tzinfo=timezone.utc), 0, "", ""),
                payload["member_id"],
                payload,
            ))
    if not candidates:
        return None
    _, _, member, payload = min(candidates, key=lambda item: (item[0], item[1], item[2]))
    return {"member_id": member, "result": copy.deepcopy(payload)}


winner_for_challenge = challenge_winner
result_for_challenge = challenge_progress


def compute_leaderboard(records: Any, *, stats_sharing: bool = True, period: str | None = None,
                        limit: int = 50, now: Any = None, as_of: Any = None) -> dict[str, Any]:
    """Compute a local leaderboard from explicit, current-period shares only."""
    if now is not None and as_of is not None:
        raise SyncValidationError("Household leaderboard accepts only one window timestamp.")
    if period is not None:
        period = _normalise_share_period(period)
    reference = as_of if as_of is not None else now
    if isinstance(records, dict) and (HOUSEHOLD_STATE_KEY in records or "settings" in records):
        if not stats_sharing:
            return {"available": False, "reason": "stats_sharing_disabled", "entries": []}
        stats_sharing = stats_sharing_enabled(records)
        values = household_records(records)
    else:
        values = list(records or [])
    if not stats_sharing:
        return {"available": False, "reason": "stats_sharing_disabled", "entries": []}
    try:
        limit = max(1, min(100, int(limit)))
    except (TypeError, ValueError):
        limit = 50
    selected = converge_records(values)
    members = {
        item["payload"]["member_id"]: item["payload"]
        for item in selected if item["kind"] == "member" and "payload" in item
    }
    totals: dict[str, dict[str, int]] = {}
    for item in selected:
        if item["kind"] != "share" or "payload" not in item:
            continue
        payload = item["payload"]
        if period is not None and _normalise_share_period(payload["period"]) != period:
            continue
        if period is not None and not _share_matches_window(payload, period, reference):
            continue
        member = payload["member_id"]
        row = totals.setdefault(member, {field: 0 for field in SHARE_STAT_FIELDS})
        for field, value in payload["stats"].items():
            row[field] = min(MAX_STAT_VALUE, row.get(field, 0) + value)
    entries = []
    for member, total in totals.items():
        profile = members.get(member, {})
        entries.append({
            "member_id": member,
            "display_name": profile.get("display_name", member),
            "avatar_color": profile.get("avatar_color", ""),
            **total,
        })
    entries.sort(key=lambda row: (
        -row.get("completions", 0), -row.get("playtime_seconds", 0),
        -row.get("ra_count", 0), row["member_id"],
    ))
    for index, row in enumerate(entries[:limit], start=1):
        row["rank"] = index
    return {"available": True, "entries": entries[:limit], "period": period or "all_time"}


leaderboard = compute_leaderboard
build_leaderboard = compute_leaderboard

# Short aliases keep the pure core convenient for callers that use the
# record-oriented vocabulary already present in parity_library_sync.
make_member = make_member_record
make_challenge = make_challenge_record
make_challenge_result = make_challenge_result_record
make_share = make_share_record
validate_records = validate_record_set
converge = converge_records
latest_record = latest_valid
stats_enabled = stats_sharing_enabled
build_stats_share = make_share_record


def state_token(state: dict[str, Any]) -> str:
    """Return a detached token for household records and the opt-in flag."""
    _, records = _state_bucket(state)
    return hashlib.sha256(_canonical({
        "records": sorted(records),
        "stats_sharing": stats_sharing_enabled(state),
    })).hexdigest()


if __name__ == "__main__":
    member = make_member_record("demo", "Demo", created_at="2026-01-01T00:00:00+00:00")
    assert validate_record(member)["event_id"] == member["event_id"]
    print("household self-test: ok")
