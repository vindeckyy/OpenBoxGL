"""Steam Bridge v2 routes for safe shortcuts.vdf preview/apply/remove."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from api_errors import BadRequest, Conflict
from pkg.parity.parity_steam_bridge import (
    ShortcutsStaleError,
    SteamBridgeError,
    apply_shortcuts,
    is_bridge_shortcut,
    parse_shortcuts,
    preview_remove,
    preview_shortcuts,
    shortcut_from_game,
)
from routes.registry import route
from webapp_state import load_state_view


MAX_BRIDGE_GAMES = 500
DEFAULT_LAUNCHER = "openbox"


def _body(payload):
    if not isinstance(payload, dict):
        raise BadRequest("Steam Bridge request must be an object.", code="STEAMBRIDGE_INVALID_REQUEST")
    return payload


def _launcher_executable():
    configured = str(os.environ.get("OPENBOX_EXECUTABLE", "")).strip()
    if configured:
        return configured
    return shutil.which("openbox") or DEFAULT_LAUNCHER


def _steam_roots():
    home = Path.home()
    return (
        home / ".local/share/Steam",
        home / ".steam/steam",
        home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
    )


def _candidate_paths():
    candidates = []
    for root in _steam_roots():
        userdata = root / "userdata"
        if not userdata.is_dir():
            continue
        try:
            accounts = sorted((item for item in userdata.iterdir() if item.is_dir()), key=lambda item: item.name)
        except OSError:
            continue
        candidates.extend(account / "config" / "shortcuts.vdf" for account in accounts)
    return candidates


def _path(payload):
    supplied = str(payload.get("path", "") or "").strip()
    if supplied:
        path = Path(supplied).expanduser()
        if not path.is_absolute():
            raise BadRequest("Steam Bridge path must be absolute.", code="STEAMBRIDGE_PATH_INVALID")
        return path
    existing = [path for path in _candidate_paths() if path.is_file()]
    if existing:
        try:
            return max(existing, key=lambda value: value.stat().st_mtime_ns)
        except OSError:
            return existing[0]
    roots = _steam_roots()
    return roots[0] / "userdata" / "0" / "config" / "shortcuts.vdf"


def _games(state, payload):
    source = state.get("games", []) if isinstance(state, dict) else []
    if not isinstance(source, list):
        source = []
    requested = payload.get("game_ids")
    if requested is None:
        return [game for game in source if isinstance(game, dict)][:MAX_BRIDGE_GAMES]
    if not isinstance(requested, list) or len(requested) > MAX_BRIDGE_GAMES:
        raise BadRequest(f"game_ids must be a list of at most {MAX_BRIDGE_GAMES} items.", code="STEAMBRIDGE_INVALID_REQUEST")
    wanted = {str(value).strip() for value in requested if str(value).strip()}
    return [
        game for game in source
        if isinstance(game, dict) and str(game.get("game_id") or game.get("id") or "") in wanted
    ]


def _shortcuts(state, payload):
    launcher = str(payload.get("launcher_exe") or _launcher_executable()).strip()
    if not launcher:
        raise BadRequest("Steam Bridge launcher executable is empty.", code="STEAMBRIDGE_INVALID_REQUEST")
    return [shortcut_from_game(game, launcher_exe=launcher) for game in _games(state, payload)]


def _plan_error(error):
    if isinstance(error, ShortcutsStaleError):
        raise Conflict(str(error), code="STEAMBRIDGE_PREVIEW_STALE") from error
    raise BadRequest(str(error), code="STEAMBRIDGE_INVALID_PLAN") from error


@route("GET", "/api/v2/steambridge/status", spec="handlers.steambridge.steambridge_status")
def steambridge_status(handler, parsed):
    path = _path({})
    result = {"path": str(path), "exists": path.is_file(), "available": bool(path.parent.is_dir())}
    if path.is_file():
        try:
            document = parse_shortcuts(path)
            result.update({
                "count": len(document.records),
                "openbox_count": sum(1 for record in document.records if is_bridge_shortcut(record)),
                "foreign_count": sum(1 for record in document.records if not is_bridge_shortcut(record)),
            })
        except (OSError, ValueError, SteamBridgeError) as error:
            result.update({"error": str(error), "corrupt": True})
    handler.send_json(200, result)


@route("POST", "/api/v2/steambridge/preview", spec="handlers.steambridge.steambridge_preview")
def steambridge_preview(handler, payload):
    body = _body(payload)
    path = _path(body)
    try:
        plan = preview_shortcuts(path, _shortcuts(load_state_view(), body))
    except (OSError, ValueError, SteamBridgeError) as error:
        _plan_error(error)
    handler.send_json(200, plan)


@route("POST", "/api/v2/steambridge/apply", spec="handlers.steambridge.steambridge_apply")
def steambridge_apply(handler, payload):
    body = _body(payload)
    plan = body.get("plan")
    if not isinstance(plan, dict):
        raise BadRequest("A reviewed Steam Bridge plan is required.", code="STEAMBRIDGE_PLAN_REQUIRED")
    path = _path({"path": body.get("path") or plan.get("path")})
    try:
        result = apply_shortcuts(path, plan=plan)
    except (OSError, ValueError, SteamBridgeError) as error:
        _plan_error(error)
    handler.send_json(200, result)


@route("POST", "/api/v2/steambridge/remove/preview", spec="handlers.steambridge.steambridge_remove_preview")
def steambridge_remove_preview(handler, payload):
    body = _body(payload)
    targets = body.get("targets")
    if targets in (None, [], ""):
        raise BadRequest("At least one Steam shortcut target is required.", code="STEAMBRIDGE_TARGET_REQUIRED")
    try:
        plan = preview_remove(_path(body), targets)
    except (OSError, ValueError, SteamBridgeError) as error:
        _plan_error(error)
    handler.send_json(200, plan)


@route("POST", "/api/v2/steambridge/remove", spec="handlers.steambridge.steambridge_remove")
def steambridge_remove(handler, payload):
    body = _body(payload)
    plan = body.get("plan")
    if not isinstance(plan, dict) or plan.get("operation") != "remove":
        raise BadRequest("A reviewed Steam Bridge removal plan is required.", code="STEAMBRIDGE_PLAN_REQUIRED")
    path = _path({"path": body.get("path") or plan.get("path")})
    try:
        result = apply_shortcuts(path, plan=plan)
    except (OSError, ValueError, SteamBridgeError) as error:
        _plan_error(error)
    handler.send_json(200, result)


__all__ = [
    "steambridge_status",
    "steambridge_preview",
    "steambridge_apply",
    "steambridge_remove_preview",
    "steambridge_remove",
]
