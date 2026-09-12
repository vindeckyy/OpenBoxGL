"""Quick Resume v2 routes (T1-core).

``GET  /api/v2/resume/status`` reports per-game capability, availability and
stale-state flags. ``POST /api/v2/resume`` relaunches with the stored state
through the same atomic reservation path as ``/api/launch`` (conflicts stay
``LAUNCH_ALREADY_ACTIVE`` 409s). ``POST /api/v2/resume/discard`` clears a
stored state. All routes are additive v2 surface — v1 is untouched.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pkg.parity  # noqa: F401,E402  # installs the flat parity_* import finder
from api_errors import BadRequest, Conflict, NotFound  # noqa: E402
import openbox  # noqa: E402
from parity_resume import discard_resume_state, resume_status as _resume_status  # noqa: E402
from pkg.state.launch import game_from_payload, game_from_query, start_game  # noqa: E402
from routes.registry import route  # noqa: E402


def _data_parent():
    # Resolved per call so tests that rebind openbox.DATA see the new store.
    return openbox.DATA.parent


def _game_or_400(state, payload):
    try:
        return game_from_payload(state, payload if isinstance(payload, dict) else {})
    except IndexError as exc:
        raise BadRequest("Game not found.", code="GAME_NOT_FOUND") from exc


@route("GET", "/api/v2/resume/status", spec="handlers.resume.resume_status")
def resume_status(handler, parsed):
    """Report Quick Resume capability/availability for one game."""
    if not handler.authorized():
        handler.handle_unauthorized()
        return
    state = openbox.load_state()
    try:
        game = game_from_query(state, parse_qs(parsed.query))
    except IndexError as exc:
        raise BadRequest("Game not found.", code="GAME_NOT_FOUND") from exc
    status = _resume_status(
        game, state.get("settings", {}),
        profiles=state.get("profiles", {}), data_parent=_data_parent(),
    )
    handler.send_json(200, status)


@route("POST", "/api/v2/resume", spec="handlers.resume.resume_launch")
def resume_launch(handler, payload):
    """Relaunch a game into its stored state via the atomic reservation path."""
    if not handler.authorized():
        handler.handle_unauthorized()
        return
    state = openbox.load_state()
    game = _game_or_400(state, payload)
    status = _resume_status(
        game, state.get("settings", {}),
        profiles=state.get("profiles", {}), data_parent=_data_parent(),
    )
    if not status["enabled"]:
        raise BadRequest(
            "Quick Resume is disabled in Settings.", code="QUICK_RESUME_DISABLED"
        )
    if not status["capable"]:
        raise BadRequest(
            "This launch path cannot resume from a saved state.",
            code="RESUME_UNSUPPORTED",
        )
    if not status["available"]:
        raise NotFound(
            "No captured state exists for this game.", code="RESUME_STATE_MISSING"
        )
    allow_stale = bool(payload.get("allow_stale"))
    if status["stale"] and not allow_stale:
        raise Conflict(
            "The stored state was captured by a different adapter or emulator "
            "build. Start clean, or pass allow_stale to load it anyway.",
            code="RESUME_STATE_STALE",
        )
    stable_id = str(game.get("game_id") or "")
    games = state.get("games") or []
    entry = (
        start_game(stable_game_id=stable_id, resume=status["state"]["file"], allow_stale=allow_stale)
        if stable_id
        else start_game(games.index(game), resume=status["state"]["file"], allow_stale=allow_stale)
    )
    handler.send_json(200, {
        "ok": True,
        "resumed_from_state": True,
        "launch_id": entry.get("launch_id", ""),
        "state_file": status["state"]["file"],
    })


@route("POST", "/api/v2/resume/discard", spec="handlers.resume.resume_discard")
def resume_discard(handler, payload):
    """Delete the stored resume state and meta for one game."""
    if not handler.authorized():
        handler.handle_unauthorized()
        return
    state = openbox.load_state()
    game = _game_or_400(state, payload)
    if not discard_resume_state(game, _data_parent()):
        raise NotFound(
            "No captured state exists for this game.", code="RESUME_STATE_MISSING"
        )
    handler.send_json(200, {"ok": True, "discarded": True})
