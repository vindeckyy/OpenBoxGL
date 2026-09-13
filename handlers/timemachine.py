"""Time Machine v2 routes for the local journal (T2-core)."""

from __future__ import annotations

from urllib.parse import parse_qs

from api_errors import BadRequest, Conflict
from openbox import load_state
from pkg.parity import parity_time_machine as time_machine
from pkg.parity.parity_library_sync import SyncStaleError, SyncValidationError
from routes.registry import route
from webapp_state import transact_state


def _first(params: dict[str, list[str]], key: str, default: str = "") -> str:
    values = params.get(key)
    return values[0] if values else default


def _integer(params: dict[str, list[str]], key: str, default: int) -> int:
    raw = _first(params, key)
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError) as error:
        raise BadRequest(f"{key} must be an integer", code="TM_INVALID_REQUEST") from error


def _translate_validation(error: SyncValidationError) -> BadRequest:
    return BadRequest(str(error), code="TM_INVALID_REQUEST")


def _revert_plan(state, payload):
    if not isinstance(payload, dict):
        raise BadRequest("body must be an object", code="TM_INVALID_REQUEST")
    event_id = str(payload.get("event_id") or "").strip()
    if not event_id:
        raise BadRequest("event_id is required", code="TM_INVALID_REQUEST")
    try:
        plan = time_machine.plan_revert(
            state,
            event_id=event_id,
            game_id=payload.get("game_id"),
            fields=payload.get("fields"),
            undo=bool(payload.get("undo")),
        )
    except SyncValidationError as error:
        raise _translate_validation(error) from error
    if "base_token" in payload:
        plan["base_token"] = payload["base_token"]
    return plan


class TimeMachineHandlers:
    @route("GET", "/api/v2/library/time-machine/events")
    def _api_get_api_v2_library_time_machine_events(self, parsed):
        params = parse_qs(getattr(parsed, "query", "") or "", keep_blank_values=True)
        try:
            result = time_machine.list_events(
                load_state(),
                days=_first(params, "days") or None,
                kind=_first(params, "kind") or None,
                game_id=_first(params, "game_id") or _first(params, "game") or None,
                offset=_integer(params, "offset", 0),
                limit=_integer(params, "limit", time_machine.EVENT_PAGE_DEFAULT),
            )
        except (SyncValidationError, TypeError, ValueError) as error:
            raise _translate_validation(error) if isinstance(error, SyncValidationError) else BadRequest(
                str(error), code="TM_INVALID_REQUEST"
            ) from error
        self.send_json(200, result)

    @route("GET", "/api/v2/library/time-machine/as-of")
    def _api_get_api_v2_library_time_machine_as_of(self, parsed):
        params = parse_qs(getattr(parsed, "query", "") or "", keep_blank_values=True)
        raw_date = _first(params, "date")
        if not raw_date:
            raise BadRequest("date is required", code="TM_INVALID_DATE")
        try:
            when = time_machine.parse_as_of_date(raw_date)
            result = time_machine.materialize_as_of(load_state(), when)
        except SyncValidationError as error:
            raise BadRequest(str(error), code="TM_INVALID_DATE") from error
        self.send_json(200, result)

    @route("POST", "/api/v2/library/time-machine/revert")
    def _api_post_api_v2_library_time_machine_revert(self, payload):
        state = load_state()
        if isinstance(payload, dict) and payload.get("apply") and not payload.get("base_token"):
            raise BadRequest(
                "base_token is required when applying a revert preview",
                code="TM_INVALID_REQUEST",
            )
        plan = _revert_plan(state, payload)
        if not bool(payload.get("apply")):
            self.send_json(200, {"preview": True, "plan": plan})
            return

        try:
            _committed, result = transact_state(
                lambda current: time_machine.apply_revert(current, plan)
            )
        except SyncStaleError as error:
            raise Conflict(str(error), code="TM_REVERT_STALE") from error
        except SyncValidationError as error:
            raise _translate_validation(error) from error
        self.send_json(200, {"preview": False, **result})


__all__ = ["TimeMachineHandlers"]
