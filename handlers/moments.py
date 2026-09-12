"""Moments timeline routes (T1-ui).

Moments are small, durable memories attached to a library game.  A capture
is deliberately best-effort: if the host has no screenshot tool, the note and
its trigger still commit.  Resume links are projected only when the current
Quick Resume state is both capable and loadable.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import secrets
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pkg.parity  # noqa: F401,E402  # installs the flat parity_* import finder
from api_errors import BadRequest, NotFound  # noqa: E402
import openbox  # noqa: E402
from parity_integrations import capture_screenshot  # noqa: E402
from parity_resume import (  # noqa: E402
    adapter_for_launch,
    emulator_fingerprint,
    resume_status,
    state_dir_for,
)
from pkg.state.launch import game_from_payload, game_from_query, start_game  # noqa: E402
from routes.registry import route  # noqa: E402
from webapp_state import (  # noqa: E402
    approved_media_path,
    broadcast_event,
    bump_media_epoch,
    transact_state,
)

LOGGER = logging.getLogger("openbox")

MAX_MOMENTS_PER_GAME = 500
MAX_NOTE_LENGTH = 2000
MAX_TITLE_LENGTH = 120
MILESTONE_SECONDS = (3600, 5 * 3600, 10 * 3600, 25 * 3600, 50 * 3600, 100 * 3600)
AUTO_TRIGGERS = frozenset({"first_boot", "ra_unlock", "progress", "milestone"})
TRIGGERS = AUTO_TRIGGERS | frozenset({"manual", "pause", "hotkey", "recap", "session_end"})
TRIGGER_TITLES = {
    "manual": "Moment",
    "pause": "Paused moment",
    "hotkey": "Quick moment",
    "recap": "Session recap",
    "session_end": "Session moment",
    "first_boot": "First steps",
    "ra_unlock": "Achievement unlocked",
    "progress": "Progress update",
    "milestone": "Milestone",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _data_parent() -> Path:
    """Resolve the current data root for embedders and test stores."""
    return openbox.DATA.parent


def _game_or_400(state, payload):
    try:
        return game_from_payload(state, payload if isinstance(payload, dict) else {})
    except (IndexError, ValueError, TypeError) as exc:
        raise BadRequest("Game not found.", code="GAME_NOT_FOUND") from exc


def _game_from_query_or_400(state, query):
    try:
        return game_from_query(state, query)
    except (IndexError, ValueError, TypeError) as exc:
        raise BadRequest("Game not found.", code="GAME_NOT_FOUND") from exc


def _clean_note(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise BadRequest("note must be text.", code="MOMENT_NOTE_INVALID")
    note = value.strip()
    if len(note) > MAX_NOTE_LENGTH:
        raise BadRequest(
            f"note must be at most {MAX_NOTE_LENGTH} characters.",
            code="MOMENT_NOTE_TOO_LONG",
        )
    return note


def _clean_title(value, trigger: str) -> str:
    if value is None:
        return TRIGGER_TITLES.get(trigger, "Moment")
    if not isinstance(value, str):
        raise BadRequest("title must be text.", code="MOMENT_TITLE_INVALID")
    title = value.strip()
    if len(title) > MAX_TITLE_LENGTH:
        raise BadRequest(
            f"title must be at most {MAX_TITLE_LENGTH} characters.",
            code="MOMENT_TITLE_TOO_LONG",
        )
    return title or TRIGGER_TITLES.get(trigger, "Moment")


def _clean_trigger(value) -> str:
    trigger = str(value or "manual").strip().casefold().replace("-", "_")
    if trigger not in TRIGGERS:
        raise BadRequest("Unknown moment trigger.", code="MOMENT_TRIGGER_INVALID")
    return trigger


def _timestamp_key(value) -> tuple[int, str]:
    text = str(value or "")
    try:
        return (int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()), text)
    except (TypeError, ValueError, OverflowError, OSError):
        return (0, text)


def _moment_sort_key(item, index: int):
    stamp, text = _timestamp_key(item.get("created_at"))
    return (stamp, text, index)


def _safe_screenshot(path) -> str:
    if not path:
        return ""
    try:
        return str(approved_media_path(path, must_exist=True))
    except (OSError, ValueError, TypeError):
        return ""


def _screenshot_index(game, path: str):
    if not path:
        return None
    screenshots = game.get("screenshots")
    if not isinstance(screenshots, list):
        return None
    try:
        return screenshots.index(path)
    except ValueError:
        return None


def _moment_public(moment, game=None):
    """Project one stored item without exposing stale or unapproved paths."""
    item = moment if isinstance(moment, dict) else {}
    screenshot = _safe_screenshot(item.get("screenshot"))
    payload = {
        "moment_id": str(item.get("moment_id") or ""),
        "game_id": str(item.get("game_id") or (game or {}).get("game_id") or ""),
        "created_at": str(item.get("created_at") or ""),
        "updated_at": str(item.get("updated_at") or ""),
        "launch_id": str(item.get("launch_id") or ""),
        "title": str(item.get("title") or "Moment"),
        "note": str(item.get("note") or ""),
        "trigger": str(item.get("trigger") or "manual"),
        "screenshot": screenshot,
        "screenshot_index": _screenshot_index(game or {}, screenshot),
        "resume_state": copy.deepcopy(item.get("resume_state")) if isinstance(item.get("resume_state"), dict) else None,
    }
    error = str(item.get("capture_error") or "")
    if error:
        payload["capture_error"] = error
    return payload


def _resume_link(game, state, requested, *, snapshot_id=""):
    """Copy the current state into an immutable moment snapshot."""
    if requested is False:
        return None
    try:
        status = resume_status(
            game,
            state.get("settings", {}),
            profiles=state.get("profiles", {}),
            data_parent=_data_parent(),
        )
    except Exception:  # noqa: BLE001 - a capability probe must never block a note
        LOGGER.debug("Moments: resume capability probe failed", exc_info=True)
        return None
    if not status.get("enabled") or not status.get("capable") or not status.get("available") or status.get("stale"):
        return None
    current = status.get("state")
    if not isinstance(current, dict) or not current.get("file"):
        return None
    if isinstance(requested, dict):
        requested_file = str(requested.get("file") or "")
        if requested_file and requested_file != str(current.get("file")):
            return None
        requested_capture = str(requested.get("capture_id") or "")
        current_capture = str(current.get("capture_id") or "")
        if requested_capture and requested_capture != current_capture:
            return None
    try:
        state_dir = state_dir_for(game, _data_parent())
        source = state_dir / str(current["file"])
        source_resolved = source.resolve()
        state_root = state_dir.resolve()
        if source.is_symlink() or not source_resolved.is_file() or state_root not in source_resolved.parents:
            return None
        moment_id = str(snapshot_id or f"moment-{secrets.token_hex(12)}")
        suffix = "".join(source_resolved.suffixes) or ".state"
        snapshot_dir = state_dir / "moments"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        snapshot = snapshot_dir / f"{moment_id}{suffix}"
        shutil.copyfile(source_resolved, snapshot)
        digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        size = snapshot.stat().st_size
    except (OSError, ValueError, RuntimeError):
        LOGGER.debug("Moments: immutable resume snapshot failed", exc_info=True)
        return None
    return {
        "file": str(snapshot.relative_to(state_dir)),
        "capture_id": str(current.get("capture_id") or ""),
        "snapshot_id": str(snapshot_id or ""),
        "immutable": True,
        "sha256": digest,
        "size": size,
        "captured_at": str(current.get("captured_at") or ""),
        "adapter_id": str(current.get("adapter_id") or ""),
        "emulator_version": str(current.get("emulator_version") or ""),
    }


def _capture_destination(game) -> Path:
    stable_id = str(game.get("game_id") or "game").replace("/", "_").replace("\\", "_")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return _data_parent() / "media" / "moments" / stable_id / f"{stamp}-{secrets.token_hex(4)}.png"


def _capture_for_moment(game, payload):
    """Capture or validate a screenshot, returning ``(path, error_code)``."""
    supplied = payload.get("screenshot") or payload.get("screenshot_path")
    if supplied:
        try:
            return str(approved_media_path(supplied, must_exist=True)), ""
        except (OSError, ValueError, TypeError) as exc:
            raise BadRequest("Screenshot is not in an approved media directory.", code="MOMENT_MEDIA_INVALID") from exc
    if payload.get("capture", True) is False:
        return "", ""
    try:
        destination = approved_media_path(_capture_destination(game))
        path = capture_screenshot(destination)
        return str(approved_media_path(path, must_exist=True)), ""
    except (OSError, ValueError, TypeError, RuntimeError):
        LOGGER.info("Moments: screenshot capture unavailable", exc_info=True)
        return "", "SCREENSHOT_UNAVAILABLE"


def _new_moment(game, state, payload):
    trigger = _clean_trigger(payload.get("trigger"))
    if trigger in AUTO_TRIGGERS and not bool((state.get("settings") or {}).get("moments_autocapture")):
        raise BadRequest("Automatic moments are disabled in Settings.", code="MOMENTS_AUTO_DISABLED")
    note = _clean_note(payload.get("note"))
    screenshot, capture_error = _capture_for_moment(game, payload)
    now = str(payload.get("created_at") or _utc_now())
    moment_id = f"moment-{secrets.token_hex(12)}"
    resume = _resume_link(
        game, state, payload.get("resume_state"), snapshot_id=moment_id
    )
    item = {
        "moment_id": moment_id,
        "game_id": str(game.get("game_id") or ""),
        "created_at": now,
        "updated_at": now,
        "launch_id": str(payload.get("launch_id") or payload.get("launchId") or ""),
        "title": _clean_title(payload.get("title"), trigger),
        "note": note,
        "trigger": trigger,
        "screenshot": screenshot,
        "resume_state": resume,
    }
    if capture_error:
        item["capture_error"] = capture_error
    return item


def _snapshot_path(game, link):
    """Validate an immutable moment snapshot and return its absolute path."""
    if not isinstance(link, dict) or link.get("immutable") is not True:
        raise NotFound("This moment has no immutable resume snapshot.", code="MOMENT_RESUME_MISSING")
    relative = str(link.get("file") or "")
    if not relative or not relative.replace("\\", "/").startswith("moments/"):
        raise BadRequest("Moment resume reference is invalid.", code="MOMENT_RESUME_INVALID")
    state_dir = state_dir_for(game, _data_parent())
    candidate = state_dir / relative
    resolved = candidate.resolve()
    root = state_dir.resolve()
    if candidate.is_symlink() or root not in resolved.parents or not resolved.is_file():
        raise NotFound("This moment's resume snapshot is missing.", code="MOMENT_RESUME_MISSING")
    expected = str(link.get("sha256") or "")
    if expected:
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        if digest != expected:
            raise BadRequest("Moment resume snapshot failed its integrity check.", code="MOMENT_RESUME_INVALID")
    return relative


def auto_moment_trigger(previous, current):
    """Return the first eligible automatic trigger between two game snapshots."""
    before = previous if isinstance(previous, dict) else {}
    after = current if isinstance(current, dict) else {}

    def number(source, *keys):
        for key in keys:
            value = source.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
        return 0.0

    def progress_score(value):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        labels = {"playing": 0.1, "started": 0.1, "beaten": 1.0, "completed": 2.0, "mastered": 3.0}
        return labels.get(str(value or "").strip().casefold(), 0.0)

    if number(before, "play_count", "plays") < 1 <= number(after, "play_count", "plays"):
        return "first_boot"
    if number(after, "ra_achievements_earned", "achievements_earned") > number(before, "ra_achievements_earned", "achievements_earned"):
        return "ra_unlock"
    before_progress = progress_score(before.get("progress", before.get("completion")))
    after_progress = progress_score(after.get("progress", after.get("completion")))
    if before_progress < 1 <= after_progress:
        return "progress"
    before_playtime = number(before, "playtime_seconds", "total_playtime_seconds")
    after_playtime = number(after, "playtime_seconds", "total_playtime_seconds")
    if any(before_playtime < mark <= after_playtime for mark in MILESTONE_SECONDS):
        return "milestone"
    return ""


def _find_moment(state, payload):
    moment_id = str((payload or {}).get("moment_id") or "").strip()
    if not moment_id:
        raise BadRequest("moment_id is required.", code="MOMENT_ID_REQUIRED")
    game = None
    if (payload or {}).get("game_id") is not None or (payload or {}).get("id") is not None:
        game = _game_or_400(state, payload)
        games = [game]
    else:
        games = state.get("games") or []
    for candidate in games:
        for item in candidate.get("moments") or []:
            if isinstance(item, dict) and str(item.get("moment_id") or "") == moment_id:
                return candidate, item
    raise NotFound("Moment not found.", code="MOMENT_NOT_FOUND")


@route("GET", "/api/v2/moments", spec="handlers.moments.moments_list")
def moments_list(handler, parsed):
    """List a game's timeline, or fetch one moment by id for a deeplink."""
    if not handler.authorized():
        handler.handle_unauthorized()
        return
    state = openbox.load_state()
    query = parse_qs(parsed.query or "", keep_blank_values=True)
    requested_moment = str(query.get("moment_id", [""])[0] or "").strip()
    if requested_moment:
        game, item = _find_moment(state, {"moment_id": requested_moment})
        handler.send_json(200, {"game_id": str(game.get("game_id") or ""), "item": _moment_public(item, game)})
        return
    if query.get("game", [""])[0] and not query.get("game_id", [""])[0]:
        query["game_id"] = query["game"]
    game = _game_from_query_or_400(state, query)
    source = [item for item in (game.get("moments") or []) if isinstance(item, dict)]
    ordered = [item for _, item in sorted(enumerate(source), key=lambda pair: _moment_sort_key(pair[1], pair[0]), reverse=True)]
    try:
        limit = int(query.get("limit", ["100"])[0] or 100)
    except (TypeError, ValueError) as exc:
        raise BadRequest("limit must be an integer.", code="MOMENT_LIMIT_INVALID") from exc
    if not 1 <= limit <= MAX_MOMENTS_PER_GAME:
        raise BadRequest("limit must be between 1 and 500.", code="MOMENT_LIMIT_INVALID")
    handler.send_json(200, {
        "game_id": str(game.get("game_id") or ""),
        "items": [_moment_public(item, game) for item in ordered[:limit]],
        "total": len(ordered),
    })


@route("POST", "/api/v2/moments/resume", spec="handlers.moments.moments_resume")
def moments_resume(handler, payload):
    """Launch the exact state snapshot captured with one moment."""
    if not handler.authorized():
        handler.handle_unauthorized()
        return
    body = payload if isinstance(payload, dict) else {}
    state = openbox.load_state()
    game, item = _find_moment(state, body)
    link = item.get("resume_state")
    status = resume_status(
        game,
        state.get("settings", {}),
        profiles=state.get("profiles", {}),
        data_parent=_data_parent(),
    )
    if not status.get("enabled"):
        raise BadRequest("Quick Resume is disabled in Settings.", code="QUICK_RESUME_DISABLED")
    if not status.get("capable"):
        raise BadRequest("This launch path cannot resume from a saved state.", code="RESUME_UNSUPPORTED")
    adapter, _precedence = adapter_for_launch(
        game, state.get("profiles", {}),
    )
    if not adapter:
        raise BadRequest("This launch path cannot resume from a saved state.", code="RESUME_UNSUPPORTED")
    if str(link.get("adapter_id") or "") != str(adapter.get("adapter_id") or ""):
        raise BadRequest("The moment was captured by a different adapter.", code="MOMENT_RESUME_STALE")
    if str(link.get("emulator_version") or "") != emulator_fingerprint(adapter):
        raise BadRequest("The moment was captured by a different emulator build.", code="MOMENT_RESUME_STALE")
    relative = _snapshot_path(game, link)
    stable_id = str(game.get("game_id") or "")
    games = state.get("games") or []
    entry = (
        start_game(stable_game_id=stable_id, resume=relative)
        if stable_id else start_game(games.index(game), resume=relative)
    )
    handler.send_json(200, {
        "ok": True,
        "resumed_from_state": True,
        "moment_id": str(item.get("moment_id") or ""),
        "launch_id": entry.get("launch_id", ""),
        "state_file": relative,
    })


@route("POST", "/api/v2/moments", spec="handlers.moments.moments_create")
def moments_create(handler, payload):
    """Create a manual, pause, recap, or automatic moment."""
    if not handler.authorized():
        handler.handle_unauthorized()
        return
    state = openbox.load_state()
    game = _game_or_400(state, payload)
    stable_id = str(game.get("game_id") or "")
    item = _new_moment(game, state, payload if isinstance(payload, dict) else {})

    def mutate(current):
        target = _game_or_400(current, {"game_id": stable_id})
        moments = target.setdefault("moments", [])
        if not isinstance(moments, list):
            moments = []
            target["moments"] = moments
        screenshots = target.setdefault("screenshots", [])
        if not isinstance(screenshots, list):
            screenshots = []
            target["screenshots"] = screenshots
        if item["screenshot"] and item["screenshot"] not in screenshots:
            screenshots.append(item["screenshot"])
        moments.append(copy.deepcopy(item))
        del moments[:-MAX_MOMENTS_PER_GAME]
        return copy.deepcopy(item)

    committed, saved = transact_state(mutate)
    if item["screenshot"]:
        bump_media_epoch()
    committed_game = _game_or_400(committed, {"game_id": stable_id})
    public = _moment_public(saved, committed_game)
    try:
        broadcast_event("moment.created", public)
    except Exception:  # event delivery must not undo a durable moment
        LOGGER.debug("Moments: event publication failed", exc_info=True)
    handler.send_json(201, {"item": public})


@route("POST", "/api/v2/moments/update", spec="handlers.moments.moments_update")
def moments_update(handler, payload):
    """Update the note/title while preserving the captured media and state link."""
    if not handler.authorized():
        handler.handle_unauthorized()
        return
    body = payload if isinstance(payload, dict) else {}
    state = openbox.load_state()
    _game, existing = _find_moment(state, body)
    note = _clean_note(body["note"]) if "note" in body else str(existing.get("note") or "")
    title = _clean_title(body.get("title"), str(existing.get("trigger") or "manual")) if "title" in body else str(existing.get("title") or "Moment")
    moment_id = str(existing.get("moment_id") or "")

    def mutate(current):
        game, item = _find_moment(current, {"moment_id": moment_id, **({"game_id": body["game_id"]} if body.get("game_id") is not None else {})})
        item["note"] = note
        item["title"] = title
        item["updated_at"] = _utc_now()
        return copy.deepcopy(item), copy.deepcopy(game)

    _committed, result = transact_state(mutate)
    saved, game = result
    handler.send_json(200, {"item": _moment_public(saved, game)})


@route("POST", "/api/v2/moments/delete", spec="handlers.moments.moments_delete")
def moments_delete(handler, payload):
    """Remove a timeline item; its screenshot remains managed media."""
    if not handler.authorized():
        handler.handle_unauthorized()
        return
    body = payload if isinstance(payload, dict) else {}
    state = openbox.load_state()
    _game, existing = _find_moment(state, body)
    moment_id = str(existing.get("moment_id") or "")

    def mutate(current):
        game, item = _find_moment(current, {"moment_id": moment_id, **({"game_id": body["game_id"]} if body.get("game_id") is not None else {})})
        moments = game.get("moments") or []
        game["moments"] = [
            candidate for candidate in moments
            if not isinstance(candidate, dict) or str(candidate.get("moment_id") or "") != moment_id
        ]
        return True

    _committed, deleted = transact_state(mutate)
    handler.send_json(200, {"ok": True, "deleted": bool(deleted), "moment_id": moment_id})
