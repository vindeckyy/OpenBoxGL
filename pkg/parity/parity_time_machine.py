"""Local library Time Machine engine (journal mode) — T2-core.

The journal reuses the causal, content-addressed catalog event machinery in
``parity_library_sync`` with no transport folder: events are validated,
immutable, and recorded inside the same ``openbox.update_state`` transaction
that commits the library change.  This module reads that journal three ways —
a paginated timeline with field-level diffs, a snapshot-fold materializer for
"what did my library look like on <date>", and a field-level revert planner
whose apply emits a NEW journal event (history is never rewritten).

Retention/compaction is honest: events beyond the retention window or the
storage cap are dropped, never guessed; the ``journal_horizon`` marker tells
readers where the retained record begins.  While sync is enabled the causal
ancestry of every head is protected so publish still produces a complete DAG;
a journal-only library compacts freely and reports the horizon.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any

from pkg.parity.parity_library_sync import (
    CATALOG_FIELDS,
    SYNC_METADATA_KEY,
    SyncStaleError,
    SyncValidationError,
    _ancestors,
    _append_event,
    _catalog_for_sync_key,
    _common_base_many,
    _metadata,
    _next_sequence,
    _replace_catalog,
    _state_event_map,
    ensure_device_id,
    journal_enabled,
    make_event,
    stable_sync_key,
    state_token,
    sync_enabled,
    validate_event,
)

JOURNAL_HORIZON_KEY = "journal_horizon"
JOURNAL_REVERTS_KEY = "journal_reverts"
JOURNAL_COMPACT_CHECK_KEY = "journal_compact_checked_at"
JOURNAL_COMPACT_TOTAL_KEY = "journal_compacted_total"
JOURNAL_COMPACT_ERROR_KEY = "journal_compact_error"
JOURNAL_OVERFLOW_KEY = "journal_overflow"

JOURNAL_RETENTION_DAYS = 400
JOURNAL_MAX_EVENTS = 25000
JOURNAL_COMPACT_CHECK_HOURS = 24
AS_OF_CACHE_SIZE = 16
EVENT_PAGE_DEFAULT = 200
EVENT_PAGE_MAX = 1000
REVERT_FORMAT = "time-machine-revert-v1"
AMBIGUOUS_BASE_MARKER = "__sync_ambiguous_bases__"

_VALIDATED_CACHE: dict[str, Any] = {"fingerprint": None, "events": {}, "corrupt": []}
_AS_OF_CACHE: "OrderedDict[tuple[str, str], dict[str, Any]]" = OrderedDict()


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


def parse_as_of_date(value: Any) -> datetime:
    """Parse a route ``date`` parameter: ISO-8601, date-only means end of day UTC."""
    text = str(value or "").strip()
    if not text:
        raise SyncValidationError("date parameter is required")
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        text = f"{text}T23:59:59.999999"
    parsed = _parse_dt(text)
    if parsed is None:
        raise SyncValidationError(f"date must be ISO-8601, got: {value!r}")
    return parsed


def _journal_meta(state: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(state, dict):
        return None
    metadata = state.get(SYNC_METADATA_KEY)
    return metadata if isinstance(metadata, dict) else None


def _raw_events(metadata: dict[str, Any]) -> dict[str, Any]:
    raw = metadata.get("events", {})
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        return {
            item["event_id"]: item
            for item in raw
            if isinstance(item, dict) and isinstance(item.get("event_id"), str)
        }
    return {}


def _fingerprint(metadata: dict[str, Any], raw: dict[str, Any]) -> str:
    # Include event content, not only event ids.  The state object is mutable
    # in-process and a caller can corrupt an event without changing its key;
    # cache validation must notice that mutation on the next read.
    marker = json.dumps(
        {
            "events": raw,
            "horizon": metadata.get(JOURNAL_HORIZON_KEY),
            "next_sequence": metadata.get("next_sequence"),
            "reverts": metadata.get(JOURNAL_REVERTS_KEY) or {},
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(marker.encode("utf-8")).hexdigest()[:16]


def _validated_events(state: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Return (validated event map, corrupt row list, metadata); cached per state generation."""
    metadata = _journal_meta(state) or {}
    raw = _raw_events(metadata)
    fingerprint = _fingerprint(metadata, raw)
    if _VALIDATED_CACHE["fingerprint"] != fingerprint:
        events: dict[str, dict[str, Any]] = {}
        corrupt: list[dict[str, Any]] = []
        for ident, item in raw.items():
            try:
                checked = validate_event(item)
                if ident != checked["event_id"]:
                    raise SyncValidationError("event key mismatch")
                events[ident] = checked
            except SyncValidationError:
                corrupt.append(
                    {
                        "event_id": str(ident)[:64],
                        "sync_key": item.get("sync_key") if isinstance(item, dict) else None,
                    }
                )
        _VALIDATED_CACHE["fingerprint"] = fingerprint
        _VALIDATED_CACHE["events"] = events
        _VALIDATED_CACHE["corrupt"] = corrupt
    return _VALIDATED_CACHE["events"], _VALIDATED_CACHE["corrupt"], metadata


def _order_key(event: dict[str, Any]) -> tuple:
    return (
        _parse_dt(event.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc),
        int(event.get("sequence") or 0),
        str(event.get("event_id") or ""),
    )


def _event_kind(event: dict[str, Any], events: dict[str, dict[str, Any]]) -> str:
    if event.get("tombstone") is True:
        return "delete"
    parents = event.get("parents") or []
    if not parents:
        return "add"
    known = [events[pid] for pid in parents if pid in events]
    if known and all(item.get("tombstone") is True for item in known):
        return "restore"
    return "edit"


def _base_catalog(
    event: dict[str, Any], events: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any], bool, bool]:
    """Return (base catalog, base_missing, base_ambiguous) for a non-add event."""
    parents = event.get("parents") or []
    if not parents:
        return {}, False, False
    missing = any(pid not in events for pid in parents)
    if len(parents) == 1:
        parent = events.get(parents[0])
        base = {} if parent is None or parent.get("tombstone") else (parent.get("catalog") or {})
        return base, missing, False
    base = _common_base_many(list(parents), events)
    ambiguous = AMBIGUOUS_BASE_MARKER in base
    if ambiguous:
        base = dict(base[AMBIGUOUS_BASE_MARKER][0])
    return base, missing or ambiguous, ambiguous


def _diff(base: dict[str, Any] | None, catalog: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    before = base or {}
    after = catalog or {}
    return {
        field: {"from": before.get(field), "to": after.get(field)}
        for field in sorted(set(before) | set(after))
        if before.get(field) != after.get(field)
    }


def _event_game_id(event: dict[str, Any], events: dict[str, dict[str, Any]]) -> str | None:
    catalog = event.get("catalog") or {}
    if catalog.get("game_id"):
        return str(catalog["game_id"])
    base, _, _ = _base_catalog(event, events)
    if base.get("game_id"):
        return str(base["game_id"])
    key = str(event.get("sync_key") or "")
    return key.split(":", 1)[1] if key.startswith("game:") else None


def _event_row(
    event: dict[str, Any],
    events: dict[str, dict[str, Any]],
    reverts: dict[str, Any],
) -> dict[str, Any]:
    catalog = event.get("catalog") or {}
    base, base_missing, _ = _base_catalog(event, events)
    new_values = None if event.get("tombstone") is True else catalog
    row = {
        "event_id": event["event_id"],
        "created_at": event["created_at"],
        "sequence": event.get("sequence", 0),
        "sync_key": event.get("sync_key"),
        "device_id": event.get("device_id"),
        "kind": _event_kind(event, events),
        "game_id": _event_game_id(event, events),
        "name": catalog.get("name") or base.get("name"),
        "tombstone": event.get("tombstone") is True,
        "parents": list(event.get("parents") or []),
        "changes": _diff(base, new_values),
        "diff_base_missing": base_missing,
    }
    link = reverts.get(event["event_id"])
    if isinstance(link, dict):
        row["revert"] = dict(link)
    return row


def list_events(
    state: dict[str, Any],
    *,
    days: Any = None,
    kind: str | None = None,
    game_id: str | None = None,
    offset: int = 0,
    limit: int = EVENT_PAGE_DEFAULT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Paginated journal timeline, newest first, with field diffs and honest gap flags."""
    events, corrupt, metadata = _validated_events(state)
    reverts = metadata.get(JOURNAL_REVERTS_KEY)
    reverts = reverts if isinstance(reverts, dict) else {}
    if isinstance(days, str) and days.strip():
        try:
            days = float(days)
        except ValueError:
            raise SyncValidationError("days must be a positive number") from None
    if days is not None and float(days) <= 0:
        raise SyncValidationError("days must be a positive number")
    offset = max(0, int(offset or 0))
    limit = EVENT_PAGE_DEFAULT if limit is None else min(EVENT_PAGE_MAX, int(limit))
    cutoff = None
    if days is not None:
        cutoff = (now if isinstance(now, datetime) else _utc_now()) - timedelta(days=float(days))
    gap = any(
        any(pid not in events for pid in (event.get("parents") or []))
        for event in events.values()
    )
    matched: list[dict[str, Any]] = []
    for event in events.values():
        created = _parse_dt(event.get("created_at"))
        if cutoff is not None and (created is None or created < cutoff):
            continue
        if kind == "revert":
            if event["event_id"] not in reverts:
                continue
        elif kind and _event_kind(event, events) != kind:
            continue
        if game_id and _event_game_id(event, events) != game_id:
            continue
        matched.append(event)
    matched.sort(key=_order_key, reverse=True)
    page = matched[offset : offset + limit]
    stamps = [_parse_dt(event.get("created_at")) for event in events.values()]
    stamps = [item for item in stamps if item is not None]
    return {
        "events": [_event_row(event, events, reverts) for event in page],
        "total": len(matched),
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(page) < len(matched),
        "journal_enabled": journal_enabled(state),
        "earliest_at": _iso(min(stamps)) if stamps else None,
        "latest_at": _iso(max(stamps)) if stamps else None,
        "horizon": metadata.get(JOURNAL_HORIZON_KEY),
        "gap": gap,
        "corrupt": corrupt,
    }


def materialize_as_of(
    state: dict[str, Any], when: Any = None, *, bypass_cache: bool = False
) -> dict[str, Any]:
    """Snapshot-fold the journal to 'the library as of <when>' (bounded LRU cache)."""
    events, corrupt, metadata = _validated_events(state)
    when_dt = _parse_dt(when) if when is not None else None
    if when is not None and when_dt is None:
        raise SyncValidationError(f"as-of timestamp is not parseable: {when!r}")
    fingerprint = _VALIDATED_CACHE["fingerprint"]
    cache_key = (fingerprint, _iso(when_dt) if when_dt else "latest")
    if not bypass_cache and cache_key in _AS_OF_CACHE:
        return copy.deepcopy(_AS_OF_CACHE[cache_key])
    eligible = [
        event
        for event in events.values()
        if when_dt is None or ((_parse_dt(event.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)) <= when_dt)
    ]
    heads: dict[str, dict[str, Any]] = {}
    for event in eligible:
        key = str(event.get("sync_key") or "")
        if key and (key not in heads or _order_key(event) > _order_key(heads[key])):
            heads[key] = event
    corrupt_keys = {row.get("sync_key") for row in corrupt if row.get("sync_key")}
    rows = [
        {**(event.get("catalog") or {}), "sync_key": key}
        for key, event in heads.items()
        if key not in corrupt_keys and event.get("tombstone") is not True and event.get("catalog")
    ]
    rows.sort(key=lambda row: (str(row.get("name") or "").lower(), str(row.get("sync_key"))))
    stamps = [_parse_dt(event.get("created_at")) for event in events.values()]
    stamps = [item for item in stamps if item is not None]
    earliest = min(stamps) if stamps else None
    horizon = metadata.get(JOURNAL_HORIZON_KEY)
    horizon_dt = _parse_dt(horizon)
    result = {
        "as_of": _iso(when_dt) if when_dt else (_iso(max(stamps)) if stamps else _iso(_utc_now())),
        "requested": _iso(when_dt) if when_dt else None,
        "games": rows,
        "count": len(rows),
        "before_first_event": not eligible,
        "earliest_at": _iso(earliest) if earliest else None,
        "latest_at": _iso(max(stamps)) if stamps else None,
        "horizon": horizon,
        "truncated": bool(horizon_dt and when_dt and when_dt < horizon_dt),
        "corrupt": corrupt,
        "unknown_keys": sorted(corrupt_keys),
    }
    _AS_OF_CACHE[cache_key] = copy.deepcopy(result)
    while len(_AS_OF_CACHE) > AS_OF_CACHE_SIZE:
        _AS_OF_CACHE.popitem(last=False)
    return result


def _catalog_for_key(state: dict[str, Any], sync_key: str) -> dict[str, Any] | None:
    game = next(
        (
            item
            for item in state.get("games") or []
            if isinstance(item, dict) and stable_sync_key(item) == sync_key
        ),
        None,
    )
    return _catalog_for_sync_key(game, sync_key) if game is not None else None


def _validate_revert_fields(fields: Any) -> list[str] | None:
    if fields is None:
        return None
    if not isinstance(fields, list) or not all(isinstance(field, str) for field in fields):
        raise SyncValidationError("fields must be a list of catalog field names")
    invalid = [field for field in fields if field not in CATALOG_FIELDS or field == "game_id"]
    if invalid:
        raise SyncValidationError(f"non-catalog fields cannot be reverted: {sorted(invalid)}")
    return fields


def plan_revert(
    state: dict[str, Any],
    *,
    event_id: str,
    game_id: str | None = None,
    fields: list[str] | None = None,
    undo: bool = False,
) -> dict[str, Any]:
    """Build a previewable field-level revert plan pinned to the current state token."""
    events, _, _ = _validated_events(state)
    event = events.get(str(event_id))
    if event is None:
        raise SyncValidationError("event not in journal")
    wanted = _validate_revert_fields(fields)
    key = str(event.get("sync_key") or "")
    catalog = event.get("catalog") or {}
    base, base_missing, _ = _base_catalog(event, events)
    resolved_game_id = str(catalog.get("game_id") or base.get("game_id") or "")
    if game_id and resolved_game_id and str(game_id) != resolved_game_id:
        raise SyncValidationError("event does not belong to that game")
    current = _catalog_for_key(state, key)
    if undo:
        if base_missing:
            raise SyncValidationError("cannot undo: the event's base is unavailable")
        undo_fields = list(_diff(base, None if event.get("tombstone") else catalog))
        if wanted is not None:
            undo_fields = [field for field in undo_fields if field in wanted]
        target = dict(current or {})
        for field in undo_fields:
            if field in base:
                target[field] = base[field]
            else:
                target.pop(field, None)
        if event.get("tombstone") is True:
            target = dict(base)
        elif not event.get("parents") and not base:
            target = {}
    else:
        if event.get("tombstone") is True:
            target = {}
        elif wanted is not None:
            target = dict(current or {})
            for field in wanted:
                if field in catalog:
                    target[field] = catalog[field]
                else:
                    target.pop(field, None)
            if current is None:
                target = dict(catalog)
        else:
            target = dict(catalog)
    changes = [
        {"field": field, "action": ("remove" if values["to"] is None else "set"), **values}
        for field, values in _diff(current, target).items()
        if field != "game_id"
    ]
    return {
        "format": REVERT_FORMAT,
        "sync_key": key,
        "game_id": resolved_game_id or (current or {}).get("game_id"),
        "event_id": event["event_id"],
        "undo": bool(undo),
        "fields": wanted,
        "changes": changes,
        "target_catalog": target or None,
        "base_token": state_token(state),
        "noop": not changes,
    }


def apply_revert(state: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """Apply a previewed revert inside the transaction; appends a NEW journal event."""
    if not isinstance(plan, dict) or plan.get("format") != REVERT_FORMAT:
        raise SyncValidationError("invalid revert plan")
    if not journal_enabled(state):
        raise SyncValidationError("time-machine reverts require the library journal")
    if plan.get("base_token") != state_token(state):
        raise SyncStaleError("revert plan is stale: the library changed after the preview")
    replay = plan_revert(
        state,
        event_id=str(plan.get("event_id") or ""),
        game_id=plan.get("game_id"),
        fields=plan.get("fields"),
        undo=bool(plan.get("undo")),
    )
    if replay["noop"]:
        return {"changed": False, "applied": 0}
    key = replay["sync_key"]
    target = replay["target_catalog"]
    metadata = _metadata(state)
    heads = metadata.setdefault("heads", {})
    games = state.get("games")
    if not isinstance(games, list):
        state["games"] = games = []
    index = next(
        (
            i
            for i, item in enumerate(games)
            if isinstance(item, dict) and stable_sync_key(item) == key
        ),
        None,
    )
    if target is None and index is None:
        return {"changed": False, "applied": 0}
    if index is not None and target is not None:
        new_game = _replace_catalog(games[index], target)
    elif index is not None:
        new_game = None
    else:
        new_game = copy.deepcopy(target)
    event = make_event(
        device_id=ensure_device_id(state),
        sequence=_next_sequence(metadata),
        sync_key=key,
        parents=list(heads.get(key) or []),
        catalog=new_game,
        tombstone=target is None and new_game is None,
    )
    _append_event(metadata, event)
    heads[key] = [event["event_id"]]
    if index is not None:
        if new_game is None:
            games.pop(index)
        else:
            games[index] = new_game
    elif new_game is not None:
        games.append(new_game)
    reverts = metadata.setdefault(JOURNAL_REVERTS_KEY, {})
    if not isinstance(reverts, dict):
        metadata[JOURNAL_REVERTS_KEY] = reverts = {}
    reverts[event["event_id"]] = {
        "source_event": replay["event_id"],
        "fields": [row["field"] for row in replay["changes"]],
        "undo": bool(plan.get("undo")),
    }
    metadata["_suppress_local_recording"] = True
    return {
        "changed": True,
        "applied": len(replay["changes"]),
        "event_id": event["event_id"],
        "deleted": new_game is None and index is not None,
        "restored": index is None and new_game is not None,
    }


def _protected_ancestors(
    state: dict[str, Any], events: dict[str, dict[str, Any]], protected: set[str]
) -> set[str]:
    if not sync_enabled(state):
        return protected
    expanded = set(protected)
    for event_id in list(protected):
        expanded |= set(_ancestors(event_id, events))
    return expanded


def compact_journal(state: dict[str, Any], *, now: Any = None) -> dict[str, Any]:
    """Enforce retention + storage cap honestly: drop events, report the horizon."""
    metadata = _metadata(state)
    now_dt = _parse_dt(now) or _utc_now()
    events = _state_event_map(metadata)
    protected = {
        event_id
        for ids in (metadata.get("heads") or {}).values()
        if isinstance(ids, list)
        for event_id in ids
    }
    # Outbox rows are in-flight transport work: they matter only while sync is
    # enabled.  A journal-only library can compact them freely (the local
    # outbox never reaches a peer), and sync mode additionally protects the
    # full causal ancestry so publish still produces a complete DAG.
    if sync_enabled(state):
        protected |= {
            event.get("event_id")
            for event in metadata.get("outbox") or []
            if isinstance(event, dict) and event.get("event_id")
        }
        protected = _protected_ancestors(state, events, protected)
    cutoff = now_dt - timedelta(days=JOURNAL_RETENTION_DAYS)
    expired = {
        event["event_id"]
        for event in events.values()
        if event["event_id"] not in protected
        and (_parse_dt(event.get("created_at")) or now_dt) < cutoff
    }
    overflow = False
    if len(events) > JOURNAL_MAX_EVENTS and not sync_enabled(state):
        # Hard storage bound for a journal-only library: retain every head
        # plus the newest events that fit under the cap; the horizon records
        # exactly where the retained record begins.
        retained = set(protected)
        for event in sorted(events.values(), key=_order_key, reverse=True):
            if len(retained) >= JOURNAL_MAX_EVENTS:
                break
            retained.add(event["event_id"])
        drop = {event_id for event_id in events if event_id not in retained}
        overflow = len(events) - len(drop) > JOURNAL_MAX_EVENTS
    else:
        drop = expired
        overflow = len(events) - len(drop) > JOURNAL_MAX_EVENTS
    if drop:
        dropped_stamps = [
            stamp
            for stamp in (_parse_dt(events[event_id].get("created_at")) for event_id in drop)
            if stamp is not None
        ]
        for event_id in drop:
            events.pop(event_id, None)
        metadata["events"] = events
        metadata["outbox"] = [
            event
            for event in metadata.get("outbox") or []
            if not isinstance(event, dict) or event.get("event_id") not in drop
        ]
        prior = _parse_dt(metadata.get(JOURNAL_HORIZON_KEY))
        newest_drop = max(dropped_stamps) if dropped_stamps else now_dt
        metadata[JOURNAL_HORIZON_KEY] = _iso(max(prior, newest_drop) if prior else newest_drop)
        metadata[JOURNAL_COMPACT_TOTAL_KEY] = int(metadata.get(JOURNAL_COMPACT_TOTAL_KEY) or 0) + len(drop)
    if overflow:
        metadata[JOURNAL_OVERFLOW_KEY] = True
    else:
        metadata.pop(JOURNAL_OVERFLOW_KEY, None)
    metadata[JOURNAL_COMPACT_CHECK_KEY] = _iso(now_dt)
    return {
        "dropped": len(drop),
        "remaining": len(events),
        "horizon": metadata.get(JOURNAL_HORIZON_KEY),
        "overflow": overflow,
    }


def maybe_compact_journal(state: dict[str, Any], *, now: Any = None) -> dict[str, Any] | None:
    """Cheap gated compaction for the transaction wrapper; returns a report or None."""
    metadata = _journal_meta(state)
    if not isinstance(metadata, dict):
        return None
    raw = metadata.get("events")
    count = len(raw) if isinstance(raw, (dict, list)) else 0
    now_dt = _parse_dt(now) or _utc_now()
    checked = _parse_dt(metadata.get(JOURNAL_COMPACT_CHECK_KEY))
    due = (
        count >= JOURNAL_MAX_EVENTS
        or checked is None
        or checked + timedelta(hours=JOURNAL_COMPACT_CHECK_HOURS) <= now_dt
    )
    if not due:
        return None
    try:
        report = compact_journal(state, now=now_dt)
    except (SyncValidationError, TypeError, ValueError) as error:
        metadata[JOURNAL_COMPACT_ERROR_KEY] = str(error)[:300]
        metadata[JOURNAL_COMPACT_CHECK_KEY] = _iso(now_dt)
        return None
    metadata.pop(JOURNAL_COMPACT_ERROR_KEY, None)
    return report if report["dropped"] else None


__all__ = [
    "AS_OF_CACHE_SIZE",
    "EVENT_PAGE_DEFAULT",
    "EVENT_PAGE_MAX",
    "JOURNAL_COMPACT_CHECK_HOURS",
    "JOURNAL_MAX_EVENTS",
    "JOURNAL_RETENTION_DAYS",
    "REVERT_FORMAT",
    "apply_revert",
    "compact_journal",
    "list_events",
    "materialize_as_of",
    "maybe_compact_journal",
    "parse_as_of_date",
    "plan_revert",
]
