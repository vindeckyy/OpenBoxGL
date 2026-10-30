"""Library storage endpoints: the integrity report and the move/rebase API (ADR 0090).

The integrity report is read-only. A move is two calls: preview computes the plan and
its id, and apply recomputes the plan inside the store's lock and refuses (MOVE_PLAN_STALE)
when the library no longer matches the preview. Nothing here copies or moves files.
"""

from __future__ import annotations

from pathlib import Path

from api_errors import BadRequest
from pkg.state import library_v7
from pkg.state.paths import PathTokenError
from routes.registry import route
from webapp_state import DATA, load_state_view, update_state_with_result


def _roots(payload) -> tuple[str, str]:
    if not isinstance(payload, dict):
        raise BadRequest("A move needs old_root and new_root.")
    old_root = str(payload.get("old_root") or "").strip()
    new_root = str(payload.get("new_root") or "").strip()
    if not new_root or not Path(new_root).is_dir():
        raise BadRequest("The new library folder must exist.")
    return old_root, new_root


@route("GET", "/api/v2/library/integrity", spec="handlers.library_storage.integrity")
def integrity(handler, parsed):
    """Read-only report: missing, unreadable, out-of-root, and token problems with counts and examples."""
    handler.send_json(200, library_v7.check_library(DATA.parent))


@route("POST", "/api/v2/library/move/preview", spec="handlers.library_storage.move_preview")
def move_preview(handler, payload):
    old_root, new_root = _roots(payload)
    try:
        plan = library_v7.plan_move(load_state_view().get("games"), old_root, new_root)
    except PathTokenError as error:
        raise BadRequest(str(error)) from error
    handler.send_json(200, plan)


@route("POST", "/api/v2/library/move/apply", spec="handlers.library_storage.move_apply")
def move_apply(handler, payload):
    old_root, new_root = _roots(payload)
    plan_id = str(payload.get("plan_id") or "")

    def mutate(state):
        try:
            return library_v7.apply_move(state, plan_id, old_root, new_root)
        except PathTokenError as error:
            raise BadRequest(str(error)) from error

    _state, result = update_state_with_result(mutate)
    handler.send_json(200, result)
