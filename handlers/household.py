"""Local-first household records and game-night routes (T6)."""

from __future__ import annotations

import copy
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
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
    materialize_records,
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
from pkg.parity.parity_presence import (
    make_heartbeat,
    new_device_id,
    presence_opted_in,
    presence_settings,
    project_activity,
    read_heartbeats,
    set_presence_opt_in,
    write_heartbeat,
)
from pkg.parity.parity_library_sync import SyncValidationError
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
        "presence": presence_settings(state),
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


def _presence_folder(state):
    settings = state.get("settings", {}) if isinstance(state, dict) else {}
    folder = settings.get("cloud_folder") if isinstance(settings, dict) else None
    if not isinstance(folder, str) or not folder.strip():
        return None
    return folder.strip()


def _known_member_ids(state) -> set[str]:
    try:
        current = materialize_records(household_records(state))
    except (HouseholdValidationError, SyncValidationError):
        return set()
    members = set()
    for sync_key, payload in current.items():
        if not str(sync_key).startswith("member:") or not isinstance(payload, dict):
            continue
        member_id = payload.get("member_id")
        if isinstance(member_id, str) and member_id:
            members.add(member_id)
    return members


def _local_device_id(state) -> str:
    household = state.get("household") if isinstance(state, dict) else None
    device = household.get("device_id") if isinstance(household, dict) else None
    if not isinstance(device, str) or not device.strip():
        library = state.get("library_sync") if isinstance(state, dict) else None
        device = library.get("device_id") if isinstance(library, dict) else None
    if isinstance(device, str) and device.strip():
        return device.strip()
    return new_device_id()


@route("GET", "/api/v2/household/activity", spec="handlers.household.household_activity")
def household_activity(handler, parsed):
    """Project unexpired Now Playing heartbeats from the shared Household folder."""
    state = load_state_view()
    presence = presence_settings(state)
    folder = _presence_folder(state)
    base = {
        "format": 1,
        "available": False,
        "reason": None,
        "members": [],
        "count": 0,
        "presence": presence,
    }
    if folder is None:
        base["reason"] = "sync_folder"
        handler.send_json(200, base)
        return
    try:
        events = read_heartbeats(folder)
    except (SyncFolderError, OSError, SyncValidationError, ValueError):
        base["reason"] = "unavailable"
        handler.send_json(200, base)
        return
    base.update(project_activity(events))
    base["available"] = True
    base["presence"] = presence
    handler.send_json(200, base)


@route("POST", "/api/v2/household/presence", spec="handlers.household.household_presence")
def household_presence(handler, payload):
    """Toggle one member's local presence opt-in; off unless explicitly enabled."""
    body = _body(payload)
    member_id = _required_text(body, "member_id")
    if "enabled" not in body:
        raise BadRequest("enabled is required.", code="HOUSEHOLD_INVALID_FIELD")
    enabled = bool(body.get("enabled"))
    toast = body.get("toast")
    state = load_state_view()
    if enabled and member_id not in _known_member_ids(state):
        raise BadRequest("Add the member before opting into presence.", code="HOUSEHOLD_MEMBER_UNKNOWN")

    def mutate(live):
        return set_presence_opt_in(live, member_id, enabled, toast=toast)

    try:
        settings = transact_state(mutate)[1]
    except SyncValidationError as error:
        raise BadRequest(str(error), code="HOUSEHOLD_INVALID_FIELD") from error
    handler.send_json(200, {"presence": settings, "member_id": member_id})


@route("POST", "/api/v2/household/presence/heartbeat", spec="handlers.household.household_heartbeat")
def household_heartbeat(handler, payload):
    """Write one signed, expiring heartbeat for an opted-in member."""
    body = _body(payload)
    member_id = _required_text(body, "member_id")
    state = load_state_view()
    if not presence_opted_in(state, member_id):
        raise BadRequest("Enable presence for this member first.", code="HOUSEHOLD_PRESENCE_DISABLED")
    folder = _presence_folder(state)
    if folder is None:
        raise BadRequest("Configure a mounted cloud sync folder first.", code="HOUSEHOLD_SYNC_FOLDER_REQUIRED")
    display_name = str(body.get("display_name") or "")
    avatar_color = str(body.get("avatar_color") or "")
    game_night = body.get("game_night") if isinstance(body.get("game_night"), dict) else None

    try:
        event = make_heartbeat(
            member_id=member_id,
            device_id=_local_device_id(state),
            display_name=display_name,
            avatar_color=avatar_color,
            game_id=str(body.get("game_id") or ""),
            game_name=str(body.get("game_name") or ""),
            platform=str(body.get("platform") or ""),
            started_at=body.get("started_at"),
            game_night=game_night,
        )
        path = write_heartbeat(folder, event)
    except SyncValidationError as error:
        raise BadRequest(str(error), code="HOUSEHOLD_INVALID_FIELD") from error
    except (SyncFolderError, OSError, ValueError) as error:
        raise BadRequest("Presence folder is invalid or unavailable.", code="HOUSEHOLD_SYNC_INVALID") from error
    handler.send_json(200, {
        "ok": True,
        "member_id": event["member_id"],
        "expires_at": event["expires_at"],
        "slot": path.stem,
        "presence": presence_settings(state),
    })


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
    "household_activity",
    "household_presence",
    "household_heartbeat",
    "household_weekly_challenge",
    "household_weekly_adopt",
    "household_wishlist",
    "household_wishlist_share",
]


# --- F15: weekly auto-challenge + shelf share as wishlist entries -----------

MAX_WISHLIST_MEMBERS = 32
MAX_WISHLIST_ITEMS = 200
MAX_WISHLIST_NAME = 160
MAX_WISHLIST_PLATFORM = 80
MAX_WISHLIST_NOTE = 280

# Deterministic week-seeded challenge pool. Every install derives the same
# challenge from the ISO week number alone, so members agree without a vote
# or a server.
WEEKLY_CHALLENGE_LIBRARY = (
    {"title": "Finish one game", "metric": "completions", "base": 1, "span": 1,
     "description": "Beat or complete any game in the library this week."},
    {"title": "Unlock three achievements", "metric": "achievements", "base": 3, "span": 3,
     "description": "Earn achievements across any supported games this week."},
    {"title": "Play for two hours", "metric": "playtime_seconds", "base": 7200, "span": 3600,
     "description": "Put in some proper couch time this week."},
    {"title": "Earn a RetroAchievements trophy", "metric": "ra_count", "base": 1, "span": 2,
     "description": "Unlock RetroAchievements trophies this week."},
    {"title": "Complete two games", "metric": "finished", "base": 2, "span": 2,
     "description": "Finish two games this week."},
)


def _week_bounds(now=None):
    current = now or datetime.now(timezone.utc)
    iso = current.isocalendar()
    start = datetime.fromisoformat(f"{iso.year}-W{iso.week:02d}-1").replace(tzinfo=timezone.utc)
    end = start + timedelta(days=7) - timedelta(seconds=1)
    return iso.year, iso.week, start, end


def weekly_challenge_definition(now=None):
    """Return the deterministic challenge for the current ISO week."""
    year, week, start, end = _week_bounds(now)
    week_key = f"{year}-W{week:02d}"
    seed = hashlib.sha256(f"openbox-weekly-challenge:{week_key}".encode("utf-8")).digest()
    template = WEEKLY_CHALLENGE_LIBRARY[seed[0] % len(WEEKLY_CHALLENGE_LIBRARY)]
    span = max(1, int(template["span"]))
    target = int(template["base"]) + ((seed[1] << 8) | seed[2]) % span
    return {
        "challenge_id": f"weekly-{year}w{week:02d}",
        "week": week_key,
        "title": template["title"],
        "metric": template["metric"],
        "target": target,
        "description": template["description"],
        "deadline": end.isoformat(timespec="seconds"),
        "starts_at": start.isoformat(timespec="seconds"),
    }


def _weekly_challenge_progress(state, definition):
    results = [item for item in household_records(state) if item.get("kind") == "challenge_result"]
    for item in household_records(state):
        if item.get("kind") != "challenge" or "payload" not in item:
            continue
        if str(item["payload"].get("challenge_id") or "") != definition["challenge_id"]:
            continue
        progress = challenge_progress(item, results)
        progress["record_id"] = item.get("event_id")
        return progress
    return None


@route("GET", "/api/v2/household/challenge/weekly", spec="handlers.household.household_weekly_challenge")
def household_weekly_challenge(handler, parsed):
    """Return this week's deterministic challenge and local progress."""
    definition = weekly_challenge_definition()
    state = load_state_view()
    challenge = _weekly_challenge_progress(state, definition)
    handler.send_json(200, {
        "definition": definition,
        "challenge": challenge,
        "adopted": bool(challenge),
        "week": definition["week"],
    })


@route("POST", "/api/v2/household/challenge/weekly", spec="handlers.household.household_weekly_adopt")
def household_weekly_adopt(handler, payload):
    """Adopt (record) this week's challenge so progress can be reported."""
    definition = weekly_challenge_definition()
    existing = _weekly_challenge_progress(load_state_view(), definition)
    if existing:
        _send_snapshot(handler, load_state_view(), weekly_definition=definition, challenge=existing, adopted=True)
        return

    def mutate(state):
        return record_challenge(
            state,
            definition["challenge_id"],
            definition["title"],
            created_by="openbox",
            metric=definition["metric"],
            target=definition["target"],
            description=definition["description"],
            deadline=definition["deadline"],
        )

    _commit_record(handler, mutate, event="challenge")


def _clean_wishlist_items(raw, games):
    if not isinstance(raw, list) or len(raw) > MAX_WISHLIST_ITEMS:
        raise BadRequest("Wishlist entries must be a bounded list.", code="HOUSEHOLD_INVALID_FIELD")
    by_id = {str(game.get("game_id") or ""): game for game in games if isinstance(game, dict)}
    items = []
    seen = set()
    for entry in raw:
        if isinstance(entry, str):
            game = by_id.get(entry)
            if game is None:
                continue
            item = {"game_id": entry, "name": str(game.get("name") or "")[:MAX_WISHLIST_NAME],
                    "platform": str(game.get("platform") or "")[:MAX_WISHLIST_PLATFORM], "note": ""}
        elif isinstance(entry, dict):
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            item = {
                "game_id": str(entry.get("game_id") or "")[:80],
                "name": name[:MAX_WISHLIST_NAME],
                "platform": str(entry.get("platform") or "")[:MAX_WISHLIST_PLATFORM],
                "note": str(entry.get("note") or "")[:MAX_WISHLIST_NOTE],
            }
        else:
            continue
        key = item["game_id"] or item["name"].casefold()
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
    return items


def _wishlist_view(state):
    wishlists = (state.get("household") or {}).get("wishlists") if isinstance(state, dict) else {}
    if not isinstance(wishlists, dict):
        wishlists = {}
    return {
        "members": {
            member_id: {
                "updated_at": str(entry.get("updated_at") or ""),
                "items": list(entry.get("items") or []),
            }
            for member_id, entry in wishlists.items()
            if isinstance(entry, dict)
        },
        "total": sum(len(entry.get("items") or []) for entry in wishlists.values() if isinstance(entry, dict)),
    }


@route("GET", "/api/v2/household/wishlist", spec="handlers.household.household_wishlist")
def household_wishlist(handler, parsed):
    """Project the local shelf shares as wishlist entries per member."""
    handler.send_json(200, _wishlist_view(load_state_view()))


@route("POST", "/api/v2/household/wishlist", spec="handlers.household.household_wishlist_share")
def household_wishlist_share(handler, payload):
    """Share a filtered shelf list as wishlist entries (names, not files)."""
    body = _body(payload)
    member_id = _required_text(body, "member_id")
    state = load_state_view()
    games = state.get("games") or []
    requested = body.get("game_ids")
    if requested is None:
        requested = [
            str(game.get("game_id") or "")
            for game in games
            if game.get("manual_entry") or game.get("shelf")
        ][:MAX_WISHLIST_ITEMS]
    if not isinstance(requested, list):
        raise BadRequest("game_ids must be a list.", code="HOUSEHOLD_INVALID_FIELD")
    items = _clean_wishlist_items(requested, games)

    def mutate(current):
        household = current.setdefault("household", {})
        wishlists = household.setdefault("wishlists", {})
        if not isinstance(wishlists, dict) or len(wishlists) > MAX_WISHLIST_MEMBERS:
            wishlists = {}
            household["wishlists"] = wishlists
        wishlists[member_id] = {
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "items": items,
        }
        return _wishlist_view(current)

    try:
        view = transact_state(mutate)[1]
    except SyncValidationError as error:
        raise BadRequest(str(error), code="HOUSEHOLD_INVALID_FIELD") from error
    broadcast_event("household.changed", {"event": "wishlist", "member_id": member_id})
    handler.send_json(200, view)
