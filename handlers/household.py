"""Local-first household records and game-night routes (T6)."""

from __future__ import annotations

import copy
import uuid
from urllib.parse import parse_qs

from api_errors import BadRequest, Conflict
from pkg.parity.parity_library_sync import SyncStaleError
from pkg.parity.parity_household import (
    HOUSEHOLD_SYNC_FORMAT,
    HouseholdValidationError,
    SyncFolderError,
    acknowledge_outbox,
    append_record,
    challenge_progress,
    compute_leaderboard,
    household_records,
    merge_records,
    publish_household_outbox,
    read_household_records,
    record_challenge,
    record_challenge_result,
    record_member,
    record_share,
    record_stats_share,
    state_token,
    stats_sharing_enabled,
)
from routes.registry import route
from webapp_state import broadcast_event, load_state_view, transact_state


def _body(payload):
    if not isinstance(payload, dict):
        raise BadRequest("Household request must be an object.", code="HOUSEHOLD_INVALID_REQUEST")
    return payload


def _required_text(payload, key, *, maximum=256):
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise BadRequest(f"{key} must be non-empty text.", code="HOUSEHOLD_INVALID_FIELD")
    return value.strip()


def _optional_text(payload, key, *, maximum=256):
    value = payload.get(key)
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value.strip()) > maximum:
        raise BadRequest(f"{key} must be text.", code="HOUSEHOLD_INVALID_FIELD")
    return value.strip() or None


def _integer(payload, key, *, default=0):
    value = payload.get(key, default)
    if isinstance(value, bool):
        raise BadRequest(f"{key} must be an integer.", code="HOUSEHOLD_INVALID_FIELD")
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise BadRequest(f"{key} must be an integer.", code="HOUSEHOLD_INVALID_FIELD") from error


def _period(payload):
    period = str(payload.get("period", "all_time") or "all_time").strip().casefold().replace("-", "_")
    if period not in {"daily", "weekly", "monthly", "all_time"}:
        raise BadRequest("period is unsupported.", code="HOUSEHOLD_INVALID_FIELD")
    return period


def _sync_protocol(payload):
    protocol = payload.get("protocol")
    if protocol not in (None, "v1", HOUSEHOLD_SYNC_FORMAT, f"household-v{HOUSEHOLD_SYNC_FORMAT}"):
        raise BadRequest("Unsupported household sync protocol.", code="HOUSEHOLD_SYNC_PROTOCOL")


def _sync_folder(state):
    settings = state.get("settings", {}) if isinstance(state, dict) else {}
    folder = settings.get("cloud_folder") if isinstance(settings, dict) else None
    if not isinstance(folder, str) or not folder.strip():
        raise BadRequest("Configure a mounted cloud sync folder first.", code="HOUSEHOLD_SYNC_FOLDER_REQUIRED")
    return folder.strip()


def _sync_failure(error):
    # Transport errors can contain the configured absolute folder.  The web
    # layer also sanitizes errors, but this boundary must be safe for direct
    # route callers and tests as well.
    return BadRequest("Household sync data is invalid or unavailable.", code="HOUSEHOLD_SYNC_INVALID")


def _snapshot(state, *, period=None):
    records = household_records(state)
    visible = [item for item in records if "payload" in item]
    challenges = []
    results = [item for item in visible if item.get("kind") == "challenge_result"]
    for item in visible:
        if item.get("kind") != "challenge":
            continue
        progress = challenge_progress(item, results)
        progress["record_id"] = item.get("event_id")
        challenges.append(progress)
    settings = state.get("settings", {}) if isinstance(state, dict) else {}
    sync_folder_configured = bool(settings.get("cloud_folder")) if isinstance(settings, dict) else False
    return {
        "format": 1,
        "records": records,
        "challenges": challenges,
        "leaderboard": compute_leaderboard(
            state,
            stats_sharing=stats_sharing_enabled(state),
            period=period,
        ),
        "stats_sharing": stats_sharing_enabled(state),
        "outbox_count": len((state.get("household") or {}).get("outbox", [])) if isinstance(state, dict) else 0,
        "device_id": (state.get("household") or {}).get("device_id") if isinstance(state, dict) else None,
        "settings": {"household_stats_sharing": bool(settings.get("household_stats_sharing", False))},
        "household_sync": {"configured": sync_folder_configured, "format": HOUSEHOLD_SYNC_FORMAT},
        "state_token": state_token(state),
    }


def _send_snapshot(handler, state, *, status=200, period=None, **extra):
    result = _snapshot(state, period=period)
    result.update(extra)
    handler.send_json(status, result)


def _commit_record(handler, callback, *, event="recorded"):
    try:
        record = transact_state(callback)[1]
    except HouseholdValidationError as error:
        raise BadRequest(str(error), code="HOUSEHOLD_INVALID_RECORD") from error
    state = load_state_view()
    broadcast_event("household.changed", {"event": event, "record_id": record.get("event_id") if isinstance(record, dict) else None})
    _send_snapshot(handler, state, record=record)


@route("GET", "/api/v2/household", spec="handlers.household.household_status")
def household_status(handler, parsed):
    """Return local household records and derived challenge/leaderboard views."""
    query = parse_qs(parsed.query or "")
    period = (query.get("period", [None])[0] or None)
    if period is not None:
        period = _period({"period": period})
    _send_snapshot(handler, load_state_view(), period=period)


@route("GET", "/api/v2/household/leaderboard", spec="handlers.household.household_leaderboard")
def household_leaderboard(handler, parsed):
    query = parse_qs(parsed.query or "")
    period = (query.get("period", ["all_time"])[0] or "all_time")
    period = _period({"period": period})
    state = load_state_view()
    _send_snapshot(handler, state, period=period, leaderboard_only=True)


@route("POST", "/api/v2/household/member", spec="handlers.household.household_member")
def household_member(handler, payload):
    body = _body(payload)
    member_id = _required_text(body, "member_id")
    display_name = _required_text(body, "display_name", maximum=80)
    avatar_color = str(body.get("avatar_color", "blue") or "blue").strip()
    stats_shared = bool(body.get("stats_shared", False))

    def mutate(state):
        return record_member(state, member_id, display_name, avatar_color, stats_shared=stats_shared)

    _commit_record(handler, mutate, event="member")


@route("POST", "/api/v2/household/challenge", spec="handlers.household.household_challenge")
def household_challenge(handler, payload):
    body = _body(payload)
    challenge_id = _optional_text(body, "challenge_id") or uuid.uuid4().hex
    title = _required_text(body, "title", maximum=160)
    metric = _optional_text(body, "metric", maximum=32) or "completions"
    target = _integer(body, "target", default=1)
    if target < 1:
        raise BadRequest("target must be positive.", code="HOUSEHOLD_INVALID_FIELD")
    created_by = _optional_text(body, "created_by")
    description = _optional_text(body, "description", maximum=2000) or ""
    game_id = _optional_text(body, "game_id")
    deadline = _optional_text(body, "deadline", maximum=80)
    participants = body.get("participant_ids", [])
    if not isinstance(participants, list):
        raise BadRequest("participant_ids must be a list.", code="HOUSEHOLD_INVALID_FIELD")

    def mutate(state):
        return record_challenge(
            state,
            challenge_id,
            title,
            created_by=created_by,
            metric=metric,
            target=target,
            description=description,
            game_id=game_id,
            deadline=deadline,
            participant_ids=participants,
        )

    _commit_record(handler, mutate, event="challenge")


@route("POST", "/api/v2/household/challenge/result", spec="handlers.household.household_result")
def household_result(handler, payload):
    body = _body(payload)
    challenge_id = _required_text(body, "challenge_id")
    member_id = _required_text(body, "member_id")
    value = _integer(body, "value", default=0)
    if value < 0:
        raise BadRequest("value must not be negative.", code="HOUSEHOLD_INVALID_FIELD")
    completed = body.get("completed")
    if completed is not None and not isinstance(completed, bool):
        raise BadRequest("completed must be boolean.", code="HOUSEHOLD_INVALID_FIELD")
    game_id = _optional_text(body, "game_id")
    note = _optional_text(body, "note", maximum=500)

    def mutate(state):
        return record_challenge_result(
            state,
            challenge_id,
            member_id,
            value,
            completed=completed,
            game_id=game_id,
            note=note,
        )

    _commit_record(handler, mutate, event="challenge_result")


@route("POST", "/api/v2/household/share", spec="handlers.household.household_share")
def household_share(handler, payload):
    body = _body(payload)
    member_id = _required_text(body, "member_id")
    period = _period(body)
    stats = body.get("stats")

    def mutate(state):
        if stats is None:
            return record_stats_share(
                state,
                member_id,
                period,
                games=state.get("games", []),
                history=state.get("history", []),
            )
        return record_share(state, member_id, period, stats)

    try:
        record = transact_state(mutate)[1]
    except HouseholdValidationError as error:
        if "opt" in str(error).casefold() or "sharing" in str(error).casefold():
            raise BadRequest("Enable household stats sharing before publishing stats.", code="HOUSEHOLD_STATS_DISABLED") from error
        raise BadRequest(str(error), code="HOUSEHOLD_INVALID_RECORD") from error
    if record is None:
        raise BadRequest("Enable household stats sharing before publishing stats.", code="HOUSEHOLD_STATS_DISABLED")
    state = load_state_view()
    broadcast_event("household.changed", {"event": "share", "record_id": record.get("event_id")})
    _send_snapshot(handler, state, record=record, shared=True)


@route("POST", "/api/v2/household/sync/publish", spec="handlers.household.household_sync_publish")
def household_sync_publish(handler, payload):
    """Explicitly publish local Household outbox records to the shared folder."""
    body = _body(payload)
    _sync_protocol(body)
    state = load_state_view()
    folder = _sync_folder(state)
    base_token = state_token(state)
    pending = copy.deepcopy((state.get("household") or {}).get("outbox", []))
    detached = copy.deepcopy(state)
    try:
        result = publish_household_outbox(detached, folder)
    except (SyncFolderError, OSError, ValueError) as error:
        raise _sync_failure(error) from None

    published_ids = list(result.get("published_ids", []))
    acknowledged = 0
    if published_ids:
        def acknowledge(current):
            if state_token(current) != base_token:
                raise SyncStaleError("Household state changed while sync was publishing; retry the operation.")
            return acknowledge_outbox(current, published_ids)

        try:
            acknowledged = transact_state(acknowledge)[1]
        except SyncStaleError as error:
            raise Conflict(str(error), code="HOUSEHOLD_SYNC_STALE") from None
        except (SyncFolderError, OSError, ValueError) as error:
            raise _sync_failure(error) from None
    result["acknowledged"] = acknowledged
    result["pending"] = max(0, len(pending) - acknowledged)
    state = load_state_view()
    broadcast_event("household.changed", {"event": "sync_publish", "published": result.get("published", 0)})
    _send_snapshot(handler, state, sync=result)


@route("POST", "/api/v2/household/sync/pull", spec="handlers.household.household_sync_pull")
def household_sync_pull(handler, payload):
    """Explicitly pull and merge validated Household records from the folder."""
    body = _body(payload)
    _sync_protocol(body)
    state = load_state_view()
    folder = _sync_folder(state)
    try:
        incoming = read_household_records(folder)
    except (SyncFolderError, OSError, ValueError) as error:
        raise _sync_failure(error) from None

    def mutate(current):
        result = merge_records(current, incoming)
        result["acknowledged"] = acknowledge_outbox(current, [item["event_id"] for item in incoming])
        result["pulled"] = len(incoming)
        return result

    try:
        result = transact_state(mutate)[1]
    except (SyncFolderError, OSError, ValueError) as error:
        raise _sync_failure(error) from None
    state = load_state_view()
    broadcast_event("household.changed", {"event": "sync_pull", "pulled": result.get("pulled", 0)})
    _send_snapshot(handler, state, sync=result)


@route("POST", "/api/v2/household/merge", spec="handlers.household.household_merge")
def household_merge(handler, payload):
    body = _body(payload)
    incoming = body.get("records")
    if not isinstance(incoming, list):
        raise BadRequest("records must be a list.", code="HOUSEHOLD_INVALID_REQUEST")

    def mutate(state):
        return merge_records(state, incoming)

    try:
        result = transact_state(mutate)[1]
    except HouseholdValidationError as error:
        raise BadRequest(str(error), code="HOUSEHOLD_INVALID_RECORD") from error
    state = load_state_view()
    broadcast_event("household.changed", {"event": "merge", "applied": result.get("applied", 0)})
    _send_snapshot(handler, state, merge=result)


@route("POST", "/api/v2/household/record", spec="handlers.household.household_record")
def household_record(handler, payload):
    body = _body(payload)
    record = body.get("record")
    if not isinstance(record, dict):
        raise BadRequest("record must be an object.", code="HOUSEHOLD_INVALID_REQUEST")

    def mutate(state):
        return append_record(state, record, outbox=False)

    _commit_record(handler, mutate, event="record")


__all__ = [
    "household_status",
    "household_leaderboard",
    "household_member",
    "household_challenge",
    "household_result",
    "household_share",
    "household_sync_publish",
    "household_sync_pull",
    "household_merge",
    "household_record",
]
