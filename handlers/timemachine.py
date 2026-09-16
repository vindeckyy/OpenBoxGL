"""Time Machine v2 routes for the local journal (T2-core)."""

from __future__ import annotations

from urllib.parse import parse_qs

from api_errors import BadRequest, Conflict
from openbox import load_state
from pkg.parity import parity_time_machine as time_machine
from pkg.parity.parity_library_sync import SyncStaleError, SyncValidationError
from routes.registry import route
from webapp_state import transact_state


COMPARE_FORMAT = "time-machine-compare-v1"

# Revert apply is deliberately narrower than the catalog protocol: identity and
# launch-routing fields (game_id, provider ids, library_sync_id, path, launch
# command) are never writable through Time Machine.  The catalog protocol
# already excludes paths/commands; this whitelist makes the metadata-only
# boundary explicit at the HTTP edge.
REVERT_METADATA_WHITELIST = frozenset({
    "name", "sort_title", "alternate_names", "platform", "genre", "year",
    "developer", "publisher", "series", "region", "esrb", "max_players",
    "description", "notes", "wikipedia_url", "tags", "disc_count", "clone_of",
    "set_type", "manual_entry", "source",
})


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


def _validated_revert_fields(fields):
    """Return whitelist-checked fields; a missing list means the whole whitelist."""
    if fields is None:
        return sorted(REVERT_METADATA_WHITELIST)
    if not isinstance(fields, list) or not all(isinstance(field, str) for field in fields):
        raise BadRequest("fields must be a list of field names", code="TM_INVALID_REQUEST")
    invalid = sorted({field for field in fields if field not in REVERT_METADATA_WHITELIST})
    if invalid:
        raise BadRequest(
            f"fields outside the metadata whitelist cannot be reverted: {invalid}",
            code="TM_FIELD_NOT_ALLOWED",
        )
    return fields


def _revert_plan(state, payload):
    if not isinstance(payload, dict):
        raise BadRequest("body must be an object", code="TM_INVALID_REQUEST")
    event_id = str(payload.get("event_id") or "").strip()
    if not event_id:
        raise BadRequest("event_id is required", code="TM_INVALID_REQUEST")
    fields = _validated_revert_fields(payload.get("fields"))
    try:
        plan = time_machine.plan_revert(
            state,
            event_id=event_id,
            game_id=payload.get("game_id"),
            fields=fields,
            undo=bool(payload.get("undo")),
        )
    except SyncValidationError as error:
        raise _translate_validation(error) from error
    if "base_token" in payload:
        plan["base_token"] = payload["base_token"]
    return plan


def compare_snapshots(before: dict, after: dict, *, from_date: str, to_date: str) -> dict:
    """Diff two as-of snapshots: added, removed, and re-edited rows with fields."""
    before_rows = {
        str(row.get("sync_key")): row
        for row in (before.get("games") or [])
        if isinstance(row, dict) and row.get("sync_key")
    }
    after_rows = {
        str(row.get("sync_key")): row
        for row in (after.get("games") or [])
        if isinstance(row, dict) and row.get("sync_key")
    }

    def identity(row):
        return {
            "sync_key": row.get("sync_key"),
            "game_id": row.get("game_id"),
            "name": row.get("name"),
            "platform": row.get("platform"),
        }

    added = [identity(after_rows[key]) for key in sorted(after_rows) if key not in before_rows]
    removed = [identity(before_rows[key]) for key in sorted(before_rows) if key not in after_rows]
    edited = []
    for key in sorted(set(before_rows) & set(after_rows)):
        old, new = before_rows[key], after_rows[key]
        fields = [
            {"field": field, "from": old.get(field), "to": new.get(field)}
            for field in sorted(set(old) | set(new))
            if field != "sync_key" and old.get(field) != new.get(field)
        ]
        if fields:
            edited.append({**identity(new), "fields": fields})
    corrupt = list(before.get("corrupt") or []) + list(after.get("corrupt") or [])
    return {
        "format": COMPARE_FORMAT,
        "from": from_date,
        "to": to_date,
        "from_as_of": before.get("as_of"),
        "to_as_of": after.get("as_of"),
        "added": added,
        "removed": removed,
        "edited": edited,
        "summary": {
            "added": len(added),
            "removed": len(removed),
            "edited": len(edited),
            "re_edited": len(edited),
        },
        "truncated": bool(before.get("truncated")) or bool(after.get("truncated")),
        "corrupt": corrupt,
    }


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

    @route("GET", "/api/v2/timemachine/compare")
    def _api_get_api_v2_timemachine_compare(self, parsed):
        params = parse_qs(getattr(parsed, "query", "") or "", keep_blank_values=True)
        raw_a = _first(params, "a")
        raw_b = _first(params, "b")
        if not raw_a or not raw_b:
            raise BadRequest("a and b are required", code="TM_INVALID_DATE")
        state = load_state()
        try:
            when_a = time_machine.parse_as_of_date(raw_a)
            when_b = time_machine.parse_as_of_date(raw_b)
            if when_a > when_b:
                raise BadRequest("a must be before b", code="TM_INVALID_REQUEST")
            before = time_machine.materialize_as_of(state, when_a)
            after = time_machine.materialize_as_of(state, when_b)
        except SyncValidationError as error:
            raise BadRequest(str(error), code="TM_INVALID_DATE") from error
        self.send_json(200, compare_snapshots(before, after, from_date=raw_a, to_date=raw_b))

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
