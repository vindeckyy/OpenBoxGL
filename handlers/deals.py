"""HTTP handlers for Deal Radar (ADR 0109)."""

from __future__ import annotations

from urllib.parse import parse_qs

from api_errors import BadRequest, DealsDisabled
from openbox import DATA
from pkg.parity.parity_deals import (
    DEFAULT_STORE_CC,
    list_deals,
    normalize_cc,
    record_manual_entry,
    refresh_deals_pass,
    search_steam,
    unwatch_game,
    watch_game,
)
from routes.registry import route
from webapp_state import JOB_MANAGER, load_state_view, transact_state


def _deals_enabled(state: dict) -> bool:
    settings = state.get("settings", {}) if isinstance(state, dict) else {}
    return bool(settings.get("deals_enabled", False))


def _require_deals_enabled(state: dict) -> dict:
    if not _deals_enabled(state):
        raise DealsDisabled("Deal Radar is turned off in Settings.")
    return state.get("settings", {}) if isinstance(state, dict) else {}


@route("GET", "/api/v2/deals", spec="handlers.deals.deals_list")
def deals_list(handler, parsed):
    state = load_state_view()
    if not _deals_enabled(state):
        handler.send_json(200, {
            "available": False,
            "host_allowlist": ["store.steampowered.com"],
            "watchlist": [],
            "last_poll_at": "",
        })
        return
    handler.send_json(200, list_deals(state))


@route("GET", "/api/v2/deals/search", spec="handlers.deals.deals_search")
def deals_search(handler, parsed):
    state = load_state_view()
    settings = _require_deals_enabled(state)
    query = parse_qs(parsed.query or "")
    q = str((query.get("q") or [""])[0]).strip()
    cc = normalize_cc((query.get("cc") or [settings.get("deals_store_cc", DEFAULT_STORE_CC)])[0])
    results = search_steam(q, cc=cc)
    handler.send_json(200, {"available": True, "results": results, "cc": cc})


@route("POST", "/api/v2/deals/watch", spec="handlers.deals.deals_watch")
def deals_watch(handler, payload):
    state = load_state_view()
    _require_deals_enabled(state)
    _, created = transact_state(lambda current: watch_game(current, payload))
    handler.send_json(201, {"entry": created})


@route("POST", "/api/v2/deals/unwatch", spec="handlers.deals.deals_unwatch")
def deals_unwatch(handler, payload):
    if not isinstance(payload, dict):
        raise BadRequest("Request body must be an object.")
    state = load_state_view()
    _require_deals_enabled(state)
    item_id = str(payload.get("id") or "").strip()
    if not item_id:
        raise BadRequest("id is required.")
    _, result = transact_state(lambda current: unwatch_game(current, item_id))
    handler.send_json(200, result)


@route("POST", "/api/v2/deals/entry", spec="handlers.deals.deals_entry")
def deals_entry(handler, payload):
    state = load_state_view()
    _require_deals_enabled(state)
    _, updated = transact_state(lambda current: record_manual_entry(current, payload))
    handler.send_json(200, {"entry": updated})


@route("POST", "/api/v2/deals/refresh", spec="handlers.deals.deals_refresh")
def deals_refresh(handler, payload):
    state = load_state_view()
    settings = _require_deals_enabled(state)
    data_dir = DATA.parent

    def _commit(fn):
        return transact_state(fn)[1]

    def worker(ctx):
        return refresh_deals_pass(
            data_dir,
            state,
            settings,
            commit_fn=_commit,
            cancel_event=ctx,
            progress_cb=lambda current, total: ctx.progress(current=current, total=total),
        )

    job = JOB_MANAGER.submit("deals-refresh", worker, replace=True)
    handler.send_json(202, {"state": "queued", "job_id": job["job_id"]})


__all__ = [
    "deals_entry",
    "deals_list",
    "deals_refresh",
    "deals_search",
    "deals_unwatch",
    "deals_watch",
]
