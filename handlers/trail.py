"""HTTP handlers for Achievement Trail (ADR 0108)."""

from __future__ import annotations

from urllib.parse import parse_qs

from api_errors import BadRequest, RaDisabled
from openbox import DATA, STATE_STORE
from pkg.parity.parity_trail import (
    get_trail,
    get_trail_game,
    rebuild_trail_cache,
    refresh_trail_progress,
)
from retroachievements import load_credentials
from routes.registry import route
from webapp_state import JOB_MANAGER, load_state_readonly


def _ra_ready(state) -> tuple[bool, dict | None]:
    settings = state.get("settings", {}) if isinstance(state, dict) else {}
    if not settings.get("retroachievements_enabled", True):
        return False, None
    creds = load_credentials(DATA.parent)
    if not creds:
        return False, None
    return True, creds


@route("GET", "/api/v2/ra/trail", spec="handlers.trail.trail_list")
def trail_list(handler, parsed):
    state = load_state_readonly()
    ready, _creds = _ra_ready(state)
    if not ready:
        handler.send_json(200, {
            "available": False,
            "reason": "ra_disabled",
            "items": [],
            "summary": {},
            "total": 0,
            "offset": 0,
            "limit": 50,
            "stale": False,
        })
        return
    query = parse_qs(parsed.query or "")
    sort = str((query.get("sort") or ["closest"])[0]).strip() or "closest"
    try:
        offset = int((query.get("offset") or ["0"])[0])
        limit = int((query.get("limit") or ["50"])[0])
    except (TypeError, ValueError) as error:
        raise BadRequest("offset and limit must be integers.") from error

    data_dir = DATA.parent
    sig = STATE_STORE.signature()

    def _schedule_rebuild():
        JOB_MANAGER.submit(
            "trail-rebuild",
            lambda _ctx, st=state, dp=data_dir, s=sig: rebuild_trail_cache(st, dp, s),
            replace=True,
        )

    result = get_trail(
        state,
        data_dir,
        sig,
        sort=sort,
        offset=offset,
        limit=limit,
        rebuild_cb=_schedule_rebuild,
    )
    handler.send_json(200, result)


@route("GET", "/api/v2/ra/trail/game", spec="handlers.trail.trail_game")
def trail_game(handler, parsed):
    state = load_state_readonly()
    ready, _creds = _ra_ready(state)
    if not ready:
        handler.send_json(200, {"available": False, "reason": "ra_disabled", "game": None})
        return
    query = parse_qs(parsed.query or "")
    game_id = str((query.get("game_id") or [""])[0]).strip()
    if not game_id:
        raise BadRequest("game_id is required.")
    handler.send_json(200, get_trail_game(state, DATA.parent, game_id))


@route("POST", "/api/v2/ra/trail/refresh", spec="handlers.trail.trail_refresh")
def trail_refresh(handler, payload):
    if payload is not None and not isinstance(payload, dict):
        raise BadRequest("Request body must be an object.")
    body = payload or {}
    raw_ids = body.get("game_ids")
    game_ids: list[str] | None = None
    if raw_ids is not None:
        if not isinstance(raw_ids, list):
            raise BadRequest("game_ids must be a list.")
        game_ids = [str(item).strip() for item in raw_ids if str(item).strip()]

    state = load_state_readonly()
    ready, creds = _ra_ready(state)
    if not ready or not creds:
        raise RaDisabled("RetroAchievements must be configured and enabled to refresh trail caches.")

    data_dir = DATA.parent
    sig = STATE_STORE.signature()

    def worker(ctx):
        res = refresh_trail_progress(
            data_dir,
            state,
            creds,
            game_ids=game_ids,
            cancel_event=ctx,
            progress_cb=lambda current, total: ctx.progress(current=current, total=total),
        )
        rebuild_trail_cache(state, data_dir, sig)
        return res

    job = JOB_MANAGER.submit("trail-refresh", worker, replace=True)
    handler.send_json(202, {"state": "queued", "job_id": job["job_id"]})


__all__ = ["trail_game", "trail_list", "trail_refresh"]
