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
from webapp_state import LOGGER, load_state_view


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


# Game media field -> Steam grid filename suffix. Capsule art comes from the
# portrait cover (SteamGridDB grids 600x900), hero from the hero background,
# logo from the clear logo. v1 copies the original bytes as-is: no Python
# image resize exists in the codebase, and the plan forbids building one.
_GRID_ART_FIELDS = (("cover", "p"), ("background", "_hero"), ("clear_logo", "_logo"))


def _grid_dirs():
    """Steam per-account grid art dirs, mirroring _candidate_paths() account iteration."""
    grid_dirs = []
    for root in _steam_roots():
        userdata = root / "userdata"
        if not userdata.is_dir():
            continue
        try:
            accounts = sorted((item for item in userdata.iterdir() if item.is_dir()), key=lambda item: item.name)
        except OSError:
            continue
        grid_dirs.extend(account / "config" / "grid" for account in accounts)
    return grid_dirs


def _copy_grid_art(grid_dir, appid, game):
    """Copy cached artwork into the grid dir as Steam capsule/hero/logo files.

    Returns the number of files written. Bytes are copied as-is (no resize);
    the Steam-prescribed .png names are used even when the cached original
    has another extension, and that is logged.
    """
    written = 0
    for field, suffix in _GRID_ART_FIELDS:
        source = game.get(field)
        if not source:
            continue
        source_path = Path(str(source))
        if not source_path.is_file():
            LOGGER.warning("Steam grid art: %s file missing for %r (%s); skipping", field, game.get("name"), source_path)
            continue
        destination = grid_dir / f"{appid}{suffix}.png"
        try:
            shutil.copy2(source_path, destination)
        except OSError as error:
            LOGGER.warning("Steam grid art: cannot copy %s -> %s (%s); skipping", source_path, destination, error)
            continue
        if source_path.suffix.casefold() != ".png":
            LOGGER.info("Steam grid art: copied %s bytes as %s (cached original was %s)", field, destination.name, source_path.suffix)
        written += 1
    return written


def _apply_grid_art(path, body):
    """Copy cached SteamGridDB art for bridged games into the Steam grid dir.

    Runs after apply_shortcuts() succeeds. Only ever writes inside a detected
    Steam account dir; every skip is logged honestly.
    """
    games = _games(load_state_view(), body)
    if not games:
        LOGGER.info("Steam grid art: no bridged games in this plan; nothing to copy")
        return {"applied": 0, "skipped": 0}
    grid_dir = path.parent / "grid"
    if grid_dir not in _grid_dirs():
        LOGGER.warning("Steam grid art: %s is not inside a detected Steam account dir; skipping", grid_dir)
        return {"applied": 0, "skipped": len(games)}
    try:
        grid_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        LOGGER.warning("Steam grid art: cannot create %s (%s); skipping", grid_dir, error)
        return {"applied": 0, "skipped": len(games)}
    launcher = str(body.get("launcher_exe") or _launcher_executable()).strip()
    applied = 0
    skipped = 0
    for game in games:
        try:
            appid = shortcut_from_game(game, launcher_exe=launcher)["appid"]
        except SteamBridgeError as error:
            LOGGER.warning("Steam grid art: cannot map bridged appid for %r (%s); skipping", game.get("name"), error)
            skipped += 1
            continue
        written = _copy_grid_art(grid_dir, appid, game)
        if written:
            applied += 1
            LOGGER.info("Steam grid art: wrote %d file(s) for appid %s (%s)", written, appid, game.get("name"))
        else:
            LOGGER.info("Steam grid art: no cached artwork for %r (appid %s); skipping", game.get("name"), appid)
            skipped += 1
    return {"applied": applied, "skipped": skipped}


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
    # Deck Game Mode art: copy cached artwork into the Steam grid dir for the
    # account the shortcuts were just written to. Never raises: skips are
    # logged and reported, never fatal to the apply.
    try:
        result["grid_art"] = _apply_grid_art(path, body)
    except Exception:
        LOGGER.exception("Steam grid art copy failed; shortcuts were still applied")
        result["grid_art"] = {"applied": 0, "skipped": 0, "error": "copy_failed"}
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
