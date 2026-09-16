"""Expiring, signed Now Playing heartbeats (Household 2.0 — F1).

Presence is deliberately non-durable.  Each opted-in member rewrites exactly
one slot file in the shared Household folder (nothing accumulates), every
heartbeat carries a content hash that readers re-check, and reads only project
unexpired events.  The module never carries paths, commands, or credentials —
only a display name, the game label, and an optional Game Night hint.

State helpers keep the per-member opt-in inside ``state["household"]["presence"]``
so no settings-schema change is required and the transport stays off by default.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend_io import atomic_write_text
from pkg.parity.parity_library_sync import SyncFolderError, SyncValidationError, _canonical
from pkg.parity.parity_household import _household_transport_lock, transport_subdir


PRESENCE_FORMAT = 1
PRESENCE_KIND = "heartbeat"
PRESENCE_DIRECTORY = "presence"
PRESENCE_STATE_KEY = "presence"

HEARTBEAT_TTL_SECONDS = 600
MAX_HEARTBEAT_BYTES = 32 * 1024
MAX_PRESENCE_FILES = 256
MAX_ELAPSED_SECONDS = 7 * 24 * 3600
MAX_ID = 128
MAX_NAME = 120
MAX_GAME_NAME = 200
MAX_PLATFORM = 80

ACTIVITIES = ("playing", "lobby")
GAME_NIGHT_FIELDS = ("seed", "players", "queue_id")

PRESENCE_FIELDS = frozenset({
    "format", "event_id", "kind", "member_id", "display_name", "avatar_color",
    "device_id", "game_id", "game_name", "platform", "activity",
    "started_at", "sent_at", "expires_at", "game_night",
})
_REQUIRED_FIELDS = frozenset({"format", "event_id", "kind", "member_id", "device_id", "sent_at", "expires_at"})
_SLOT_RE = re.compile(r"^[0-9a-f]{32}$")
_SEED_RE = re.compile(r"^[0-9a-f]{8,64}$")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def _text(value: Any, label: str, maximum: int, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise SyncValidationError(f"Presence {label} must be text.")
    cleaned = value.strip()
    if required and not cleaned:
        raise SyncValidationError(f"Presence {label} is required.")
    if len(cleaned) > maximum or any(ord(char) < 32 and char != "\t" for char in cleaned):
        raise SyncValidationError(f"Presence {label} is invalid.")
    return cleaned


def _sign(body: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(body)).hexdigest()


def heartbeat_id(event: dict[str, Any]) -> str:
    """Return the content hash for a heartbeat, independent of its id field."""
    body = {key: value for key, value in event.items() if key != "event_id"}
    return _sign(body)


def _clean_game_night(value: Any) -> dict[str, Any] | None:
    if value in (None, ""):
        return None
    if not isinstance(value, dict):
        raise SyncValidationError("Presence game_night must be an object.")
    cleaned: dict[str, Any] = {}
    for field in GAME_NIGHT_FIELDS:
        if value.get(field) in (None, ""):
            continue
        raw = value[field]
        if field == "players":
            if isinstance(raw, bool):
                raise SyncValidationError("Presence game_night players is invalid.")
            try:
                players = int(raw)
            except (TypeError, ValueError) as error:
                raise SyncValidationError("Presence game_night players is invalid.") from error
            if not 2 <= players <= 8:
                raise SyncValidationError("Presence game_night players is invalid.")
            cleaned["players"] = players
        else:
            cleaned[field] = _text(raw, f"game_night {field}", MAX_ID, required=True)
    return cleaned or None


def make_heartbeat(
    *,
    member_id: str,
    device_id: str,
    display_name: str = "",
    avatar_color: str = "",
    game_id: str = "",
    game_name: str = "",
    platform: str = "",
    activity: str = "playing",
    started_at: Any = None,
    sent_at: Any = None,
    ttl_seconds: int = HEARTBEAT_TTL_SECONDS,
    game_night: Any = None,
) -> dict[str, Any]:
    """Build one signed heartbeat and validate it before it can be published."""
    sent = _parse_dt(sent_at) or _utc_now()
    try:
        ttl = int(ttl_seconds)
    except (TypeError, ValueError) as error:
        raise SyncValidationError("Presence TTL must be a number.") from error
    if not 30 <= ttl <= HEARTBEAT_TTL_SECONDS:
        raise SyncValidationError("Presence TTL is out of range.")
    started = _parse_dt(started_at) if started_at else sent
    if started is None:
        raise SyncValidationError("Presence started_at is not a valid timestamp.")
    if started > sent + timedelta(seconds=60):
        raise SyncValidationError("Presence started_at cannot be in the future.")
    activity = _text(activity, "activity", 32) or "playing"
    if activity not in ACTIVITIES:
        raise SyncValidationError("Presence activity is unsupported.")
    event: dict[str, Any] = {
        "format": PRESENCE_FORMAT,
        "kind": PRESENCE_KIND,
        "member_id": _text(member_id, "member_id", MAX_ID, required=True),
        "device_id": _text(device_id, "device_id", MAX_ID, required=True),
        "display_name": _text(display_name, "display_name", MAX_NAME),
        "avatar_color": _text(avatar_color, "avatar_color", MAX_NAME),
        "game_id": _text(game_id, "game_id", MAX_ID),
        "game_name": _text(game_name, "game_name", MAX_GAME_NAME),
        "platform": _text(platform, "platform", MAX_PLATFORM),
        "activity": activity,
        "started_at": _iso(started),
        "sent_at": _iso(sent),
        "expires_at": _iso(sent + timedelta(seconds=ttl)),
    }
    cleaned_night = _clean_game_night(game_night)
    if cleaned_night is not None:
        event["game_night"] = cleaned_night
    event["event_id"] = heartbeat_id(event)
    return validate_heartbeat(event)


def validate_heartbeat(event: Any) -> dict[str, Any]:
    """Validate and return a detached heartbeat copy; tampering fails closed."""
    if not isinstance(event, dict):
        raise SyncValidationError("Presence heartbeat must be an object.")
    if set(event) - PRESENCE_FIELDS:
        raise SyncValidationError("Presence heartbeat shape is invalid.")
    if set(event) & _REQUIRED_FIELDS != _REQUIRED_FIELDS:
        raise SyncValidationError("Presence heartbeat has missing fields.")
    if event.get("format") != PRESENCE_FORMAT or event.get("kind") != PRESENCE_KIND:
        raise SyncValidationError("Unsupported presence heartbeat format.")
    event_id = _text(event.get("event_id"), "event_id", 64, required=True)
    if len(event_id) != 64 or any(char not in "0123456789abcdef" for char in event_id):
        raise SyncValidationError("Presence heartbeat ID is invalid.")
    if heartbeat_id(event) != event_id:
        raise SyncValidationError("Presence heartbeat content hash does not match its ID.")
    checked = dict(event)
    checked["member_id"] = _text(event.get("member_id"), "member_id", MAX_ID, required=True)
    checked["device_id"] = _text(event.get("device_id"), "device_id", MAX_ID, required=True)
    for field, maximum in (
        ("display_name", MAX_NAME), ("avatar_color", MAX_NAME), ("game_id", MAX_ID),
        ("game_name", MAX_GAME_NAME), ("platform", MAX_PLATFORM),
    ):
        checked[field] = _text(event.get(field, ""), field, maximum)
    activity = checked.get("activity") or "playing"
    if activity not in ACTIVITIES:
        raise SyncValidationError("Presence activity is unsupported.")
    checked["activity"] = activity
    sent = _parse_dt(checked.get("sent_at"))
    expires = _parse_dt(checked.get("expires_at"))
    if sent is None or expires is None:
        raise SyncValidationError("Presence heartbeat timestamps are invalid.")
    if expires <= sent or expires - sent > timedelta(seconds=HEARTBEAT_TTL_SECONDS):
        raise SyncValidationError("Presence heartbeat TTL is out of range.")
    started = _parse_dt(checked.get("started_at")) if checked.get("started_at") else sent
    if started is None or started > sent + timedelta(seconds=60):
        raise SyncValidationError("Presence heartbeat started_at is invalid.")
    if "game_night" in checked:
        night = _clean_game_night(checked.get("game_night"))
        if night is None:
            checked.pop("game_night")
        else:
            checked["game_night"] = night
    if len(_canonical(checked)) > MAX_HEARTBEAT_BYTES:
        raise SyncValidationError("Presence heartbeat is too large.")
    return checked


def heartbeat_expired(event: dict[str, Any], *, now: Any = None) -> bool:
    """Return True when a heartbeat is past its TTL (invalid input counts as expired)."""
    try:
        checked = validate_heartbeat(event)
    except SyncValidationError:
        return True
    moment = _parse_dt(now) or _utc_now()
    expires = _parse_dt(checked.get("expires_at"))
    if expires is None:
        return True
    return moment >= expires


def presence_slot(member_id: str, device_id: str) -> str:
    """Return the deterministic 32-hex slot name for one member on one device."""
    member = _text(member_id, "member_id", MAX_ID, required=True)
    device = _text(device_id, "device_id", MAX_ID, required=True)
    return hashlib.sha256(f"{device}\x00{member}".encode("utf-8")).hexdigest()[:32]


def project_activity(events: Any, *, now: Any = None) -> dict[str, Any]:
    """Project validated, unexpired heartbeats into the 'who is playing what' view."""
    moment = _parse_dt(now) or _utc_now()
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    dropped = 0
    for raw in events if isinstance(events, (list, tuple)) else []:
        try:
            event = validate_heartbeat(raw)
        except SyncValidationError:
            dropped += 1
            continue
        if heartbeat_expired(event, now=moment):
            dropped += 1
            continue
        key = (event["device_id"], event["member_id"])
        previous = latest.get(key)
        if previous is None or str(previous.get("sent_at")) < str(event.get("sent_at")):
            latest[key] = event
    members = []
    for event in latest.values():
        sent = _parse_dt(event.get("sent_at")) or moment
        started = _parse_dt(event.get("started_at")) or sent
        elapsed = int(max(0.0, min(float(MAX_ELAPSED_SECONDS), (moment - started).total_seconds())))
        night = event.get("game_night") if isinstance(event.get("game_night"), dict) else None
        members.append({
            "member_id": event["member_id"],
            "display_name": event.get("display_name") or event["member_id"],
            "avatar_color": event.get("avatar_color") or "",
            "device_id": event["device_id"],
            "activity": event.get("activity") or "playing",
            "game_id": event.get("game_id") or "",
            "game_name": event.get("game_name") or "",
            "platform": event.get("platform") or "",
            "started_at": event.get("started_at"),
            "last_seen": event.get("sent_at"),
            "elapsed_seconds": elapsed,
            "join_hint": "game-night" if night else None,
            "game_night": night,
        })
    members.sort(key=lambda item: (item["display_name"].casefold(), item["member_id"]))
    return {
        "format": PRESENCE_FORMAT,
        "generated_at": _iso(moment),
        "ttl_seconds": HEARTBEAT_TTL_SECONDS,
        "count": len(members),
        "members": members,
        "dropped": dropped,
    }


def _presence_dir(root: Path, *, create: bool) -> Path:
    return transport_subdir(root, PRESENCE_DIRECTORY, create=create)


def _prune_expired(presence: Path, *, now: datetime) -> int:
    removed = 0
    for path in sorted(presence.glob("*.json")):
        if not _SLOT_RE.fullmatch(path.stem) or path.is_symlink() or not path.is_file():
            continue
        try:
            if path.stat().st_size > MAX_HEARTBEAT_BYTES + 1:
                continue
            event = json.loads(path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if heartbeat_expired(event, now=now):
            try:
                path.unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue
    return removed


def write_heartbeat(folder: str | Path, event: dict[str, Any]) -> Path:
    """Atomically publish one heartbeat, replacing the member/device slot."""
    checked = validate_heartbeat(event)
    with _household_transport_lock(folder, create=True) as root:
        presence = _presence_dir(root, create=True)
        _prune_expired(presence, now=_utc_now())
        target = presence / f"{presence_slot(checked['member_id'], checked['device_id'])}.json"
        encoded = _canonical(checked).decode("utf-8") + "\n"
        try:
            atomic_write_text(target, encoded, mode=0o600)
            target.chmod(0o600)
        except OSError as error:
            raise SyncFolderError("Unable to publish the presence heartbeat.") from error
        return target


def clear_heartbeat(folder: str | Path, *, member_id: str, device_id: str) -> bool:
    """Best-effort removal of a slot (used when a member opts out)."""
    slot = presence_slot(member_id, device_id)
    try:
        with _household_transport_lock(folder) as root:
            target = _presence_dir(root, create=False) / f"{slot}.json"
            if target.is_symlink() or not target.is_file():
                return False
            target.unlink()
            return True
    except (SyncFolderError, OSError):
        return False


def read_heartbeats(
    folder: str | Path, *, now: Any = None, prune_expired: bool = True,
) -> list[dict[str, Any]]:
    """Read only valid, unexpired heartbeats and drop expired slot files."""
    moment = _parse_dt(now) or _utc_now()
    with _household_transport_lock(folder) as root:
        presence = _presence_dir(root, create=False)
        if not presence.exists():
            return []
        if presence.is_symlink() or not presence.is_dir():
            raise SyncFolderError("Presence directory is not a directory.")
        paths = sorted(path for path in presence.glob("*.json") if not path.is_symlink())
        if len(paths) > MAX_PRESENCE_FILES:
            raise SyncValidationError("Too many presence heartbeat files.")
        events: list[dict[str, Any]] = []
        for path in paths:
            if not _SLOT_RE.fullmatch(path.stem) or not path.is_file():
                continue
            try:
                if path.stat().st_size > MAX_HEARTBEAT_BYTES + 1:
                    continue
                raw = path.read_bytes()
                if len(raw) > MAX_HEARTBEAT_BYTES + 1:
                    continue
                event = validate_heartbeat(json.loads(raw.decode("utf-8")))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, SyncValidationError):
                continue
            if heartbeat_expired(event, now=moment):
                if prune_expired:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                continue
            events.append(event)
        return events


def new_device_id() -> str:
    """Return a stable random device id for a state that has never recorded one."""
    return uuid.uuid4().hex


def presence_settings(state: Any) -> dict[str, Any]:
    """Return validated local presence settings; off unless a member opted in."""
    household = state.get("household") if isinstance(state, dict) else None
    raw = household.get(PRESENCE_STATE_KEY) if isinstance(household, dict) else None
    if not isinstance(raw, dict):
        return {"opted_in": {}, "toast": False}
    opted: dict[str, bool] = {}
    source = raw.get("opted_in")
    if isinstance(source, dict):
        for member_id, enabled in list(source.items())[:MAX_PRESENCE_FILES]:
            if enabled is True and isinstance(member_id, str) and 0 < len(member_id.strip()) <= MAX_ID:
                opted[member_id.strip()] = True
    return {"opted_in": opted, "toast": bool(raw.get("toast", False))}


def presence_opted_in(state: Any, member_id: str) -> bool:
    """Return True only for an explicit per-member opt-in on this device."""
    member = _text(member_id, "member_id", MAX_ID, required=True)
    return bool(presence_settings(state)["opted_in"].get(member))


def set_presence_opt_in(state: dict[str, Any], member_id: str, enabled: bool, *, toast: Any = None) -> dict[str, Any]:
    """Update one member's opt-in inside the household bucket and return the settings."""
    member = _text(member_id, "member_id", MAX_ID, required=True)
    bucket = state.get("household")
    if not isinstance(bucket, dict):
        bucket = {}
        state["household"] = bucket
    settings = presence_settings(state)
    opted = dict(settings["opted_in"])
    if enabled:
        if len(opted) >= MAX_PRESENCE_FILES:
            raise SyncValidationError("Too many presence opt-ins.")
        opted[member] = True
    else:
        opted.pop(member, None)
    if toast is not None:
        settings["toast"] = bool(toast)
    settings["opted_in"] = opted
    bucket[PRESENCE_STATE_KEY] = settings
    return settings


__all__ = [
    "ACTIVITIES",
    "HEARTBEAT_TTL_SECONDS",
    "MAX_HEARTBEAT_BYTES",
    "MAX_PRESENCE_FILES",
    "PRESENCE_FORMAT",
    "PRESENCE_KIND",
    "PRESENCE_STATE_KEY",
    "clear_heartbeat",
    "heartbeat_expired",
    "heartbeat_id",
    "make_heartbeat",
    "new_device_id",
    "presence_opted_in",
    "presence_settings",
    "presence_slot",
    "project_activity",
    "read_heartbeats",
    "set_presence_opt_in",
    "validate_heartbeat",
    "write_heartbeat",
]
