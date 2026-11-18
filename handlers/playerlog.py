"""HTTP handlers for Player's Log — Time Capsules & Milestone Cards (ADR 0110)."""

from __future__ import annotations

from urllib.parse import parse_qs

from api_errors import BadRequest
from openbox import DATA
from pkg.parity.parity_playerlog import (
    compute_milestones,
    create_capsule,
    delete_capsule,
    list_capsules,
    next_capsule,
    open_capsule,
)
from routes.registry import route
from webapp_state import load_state_view, transact_state


@route("GET", "/api/v2/capsules", spec="handlers.playerlog.capsules_list")
def capsules_list(handler, parsed):
    state = load_state_view()
    query = parse_qs(parsed.query or "")
    game_id = str((query.get("game_id") or [""])[0]).strip()
    handler.send_json(200, list_capsules(state, game_id=game_id))


@route("POST", "/api/v2/capsules", spec="handlers.playerlog.capsules_create")
def capsules_create(handler, payload):
    _, created = transact_state(lambda current: create_capsule(current, payload))
    handler.send_json(201, {"capsule": created})


@route("POST", "/api/v2/capsules/open", spec="handlers.playerlog.capsules_open")
def capsules_open(handler, payload):
    if not isinstance(payload, dict):
        raise BadRequest("Request body must be an object.")
    capsule_id = str(payload.get("id") or "").strip()
    _, opened = transact_state(lambda current: open_capsule(current, capsule_id))
    handler.send_json(200, {"capsule": opened})


@route("POST", "/api/v2/capsules/delete", spec="handlers.playerlog.capsules_delete")
def capsules_delete(handler, payload):
    if not isinstance(payload, dict):
        raise BadRequest("Request body must be an object.")
    capsule_id = str(payload.get("id") or "").strip()
    _, deleted = transact_state(lambda current: delete_capsule(current, capsule_id))
    handler.send_json(200, deleted)


@route("GET", "/api/v2/capsules/next", spec="handlers.playerlog.capsules_next")
def capsules_next(handler, parsed):
    state = load_state_view()
    handler.send_json(200, next_capsule(state))


@route("GET", "/api/v2/insights/milestones", spec="handlers.playerlog.insights_milestones")
def insights_milestones(handler, parsed):
    query = parse_qs(parsed.query or "")
    mark_seen = str((query.get("mark_seen") or [""])[0]).strip().lower() in ("1", "true", "yes")
    state = load_state_view()
    report = compute_milestones(state, data_dir=DATA.parent)
    if mark_seen and report["unlocked"]:
        seen_ids = [item["id"] for item in report["unlocked"]]

        def _mark(current):
            ui = current.setdefault("ui_state", {})
            if not isinstance(ui, dict):
                ui = {}
                current["ui_state"] = ui
            existing = [str(x) for x in ui.get("milestones_seen") or [] if x]
            merged = list(dict.fromkeys([*existing, *seen_ids]))
            ui["milestones_seen"] = merged
            return merged

        transact_state(_mark)
    handler.send_json(200, report)


__all__ = [
    "capsules_create",
    "capsules_delete",
    "capsules_list",
    "capsules_next",
    "capsules_open",
    "insights_milestones",
]
