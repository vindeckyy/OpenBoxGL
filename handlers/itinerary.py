"""HTTP handlers for Backlog Itinerary (ADR 0111)."""

from __future__ import annotations

from urllib.parse import parse_qs

from api_errors import BadRequest, GameNotFound, ItineraryInvalidStrategy, ItineraryNoGames
from openbox import DATA
from pkg.parity import parity_igdb
from pkg.parity.parity_itinerary import (
    VALID_STRATEGIES,
    build_itinerary,
    build_own_history_medians,
    clamp_horizon_days,
    clamp_minutes_per_day,
    estimate_game_hours,
    fetch_missing_igdb_estimates,
    is_eligible_for_itinerary,
    load_ra_progress_map,
    missing_igdb_games,
    project_live_itinerary,
    replan_itinerary,
)
from routes.registry import route
from webapp_state import JOB_MANAGER, game_from_query, load_state_readonly, transact_state


def _validate_plan_inputs(payload: dict) -> tuple[int, int, str, list[str] | None, bool]:
    if not isinstance(payload, dict):
        raise BadRequest("Request body must be an object.")
    strategy = str(payload.get("strategy") or "quick-wins").strip()
    if strategy not in VALID_STRATEGIES:
        raise ItineraryInvalidStrategy(f"Unsupported strategy '{strategy}'.")
    horizon = clamp_horizon_days(payload.get("horizon_days"), 30)
    minutes = clamp_minutes_per_day(payload.get("minutes_per_day"), 60)
    platforms = payload.get("platforms")
    if platforms is not None and not isinstance(platforms, list):
        raise BadRequest("platforms must be a list of strings.")
    clean_plats = [str(p).strip() for p in (platforms or []) if str(p).strip()] or None
    fetch_igdb = bool(payload.get("fetch_igdb", False))
    return horizon, minutes, strategy, clean_plats, fetch_igdb


@route("GET", "/api/v2/itinerary", spec="handlers.itinerary.itinerary_get")
def itinerary_get(handler, parsed):
    state = load_state_readonly()
    handler.send_json(200, project_live_itinerary(state))


@route("GET", "/api/v2/itinerary/estimate", spec="handlers.itinerary.itinerary_estimate")
def itinerary_estimate(handler, parsed):
    state = load_state_readonly()
    query = parse_qs(parsed.query or "")
    try:
        game = game_from_query(state, query)
    except (KeyError, IndexError, ValueError, TypeError) as error:
        raise GameNotFound("Game not found.") from error
    games = [g for g in (state.get("games") or []) if isinstance(g, dict)]
    plat_meds, global_med = build_own_history_medians(games)
    ra_map = load_ra_progress_map(DATA.parent)
    gid = str(game.get("game_id") or "")
    est = estimate_game_hours(
        game,
        data_dir=DATA.parent,
        platform_medians=plat_meds,
        global_median=global_med,
        ra_info=ra_map.get(gid),
    )
    handler.send_json(200, {"estimate": est})


@route("POST", "/api/v2/itinerary/plan", spec="handlers.itinerary.itinerary_plan")
def itinerary_plan(handler, payload):
    horizon, minutes, strategy, platforms, fetch_igdb = _validate_plan_inputs(payload if payload is not None else {})
    state = load_state_readonly()
    data_dir = DATA.parent
    eligible = [
        g for g in (state.get("games") or [])
        if isinstance(g, dict) and is_eligible_for_itinerary(g, state, platforms=platforms)
    ]
    if not eligible:
        raise ItineraryNoGames("No eligible unfinished games match your Backlog Itinerary filters.")

    need_igdb = fetch_igdb and bool(parity_igdb.credentials()) and bool(missing_igdb_games(data_dir, eligible))
    if need_igdb:
        def worker(ctx):
            fetch_missing_igdb_estimates(
                data_dir,
                eligible,
                cancel_event=ctx,
                progress_cb=lambda current, total: ctx.progress(current=current, total=total),
            )
            _, result = transact_state(lambda current: _store_plan(
                current, horizon, minutes, strategy, platforms, data_dir,
            ))
            return result

        job = JOB_MANAGER.submit("itinerary-plan", worker, replace=True)
        handler.send_json(202, {"state": "queued", "job_id": job["job_id"]})
        return

    _, res = transact_state(lambda current: _store_plan(current, horizon, minutes, strategy, platforms, data_dir))
    handler.send_json(200, res)


def _store_plan(state, horizon, minutes, strategy, platforms, data_dir):
    plan = build_itinerary(
        state,
        horizon_days=horizon,
        minutes_per_day=minutes,
        strategy=strategy,
        platforms=platforms,
        data_dir=data_dir,
    )
    state["itinerary"] = plan
    return project_live_itinerary(state)


@route("POST", "/api/v2/itinerary/replan", spec="handlers.itinerary.itinerary_replan")
def itinerary_replan(handler, payload):
    if payload is not None and not isinstance(payload, dict):
        raise BadRequest("Request body must be an object.")
    body = payload or {}
    if "strategy" in body and str(body["strategy"]).strip() not in VALID_STRATEGIES:
        raise ItineraryInvalidStrategy(f"Unsupported strategy '{body['strategy']}'.")
    data_dir = DATA.parent
    _, res = transact_state(lambda current: replan_itinerary(current, body, data_dir=data_dir))
    handler.send_json(200, res)


@route("POST", "/api/v2/itinerary/abandon", spec="handlers.itinerary.itinerary_abandon")
def itinerary_abandon(handler, payload):
    def _clear(current):
        current["itinerary"] = None
        return {"abandoned": True}

    _, res = transact_state(_clear)
    handler.send_json(200, res)


__all__ = [
    "itinerary_abandon",
    "itinerary_estimate",
    "itinerary_get",
    "itinerary_plan",
    "itinerary_replan",
]
