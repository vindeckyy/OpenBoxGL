"""Small stdlib-only request/response contract helpers.

Handlers use these to validate payloads; a malformed request fails with a clear 400 and a stable code.
"""

from __future__ import annotations

from api_errors import BadRequest

# Documented response shapes for the top v1 routes; keys are v1 paths.
V1_SCHEMA = {
    "/api/v1/library": {
        "response": "games, playlists, settings, platforms, categories, tags, queue",
    },
    "/api/v1/settings": {
        "response": "the full public settings object",
    },
    "/api/v1/health": {
        "response": "health facts and job history",
    },
    "/api/v1/launch": {
        "response": "ok, launch_id, game",
    },
    "/api/v1/game": {
        "response": "saved game record",
    },
    "/api/v1/game/delete": {
        "response": "removed game name",
    },
    "/api/v1/queue": {
        "response": "queue and the advanced game",
    },
    "/api/v1/tags": {
        "response": "updated count and tag counts",
    },
    "/api/v1/notifications": {
        "response": "notifications and unread count",
    },
    "/api/v1/webhooks": {
        "response": "saved webhooks and event types",
    },
    "/api/v1/playlists": {
        "response": "saved playlist name",
    },
    "/api/v1/saves": {
        "response": "discovered save backups",
    },
    "/api/v1/media/bulk": {
        "response": "media job state",
    },
    "/api/v1/running": {
        "response": "running, events, last_event",
    },
    "/api/v1/history": {
        "response": "enabled, history",
    },
    "/api/v1/media": {
        "response": "media bytes or a JSON error",
    },
    "/api/v1/metadata/status": {
        "response": "ready, coverage, matched counts",
    },
    "/api/v1/metadata/apply": {
        "response": "applied field names and notes",
    },
    "/api/v1/import": {
        "response": "added, found, recommendations",
    },
    "/api/v1/profiles": {
        "response": "emulator profile map",
    },
    "/api/v1/themes": {
        "response": "installed themes and the active theme",
    },
    "/api/v1/update": {
        "response": "available, latest, current",
    },
    "/api/v1/jobs": {
        "response": "jobs, history",
    },
}


def require_fields(payload, *names):
    """Raise BadRequest when any named key is missing or blank."""
    if not isinstance(payload, dict):
        raise BadRequest("Request body must be a JSON object.")
    for name in names:
        if name not in payload or payload[name] in (None, ""):
            raise BadRequest(f"Missing required field: {name}")
    return payload


def optional_str(payload, key, default=""):
    """Coerce an optional value to a stripped string."""
    value = payload.get(key) if isinstance(payload, dict) else None
    if value is None:
        return default
    return str(value).strip()


def bounded_list(value, cap, default=None):
    """Return a list, truncating to ``cap`` entries; non-lists become empty."""
    if not isinstance(value, list):
        return [] if default is None else list(default)
    return value[:cap]


def one_of(value, choices, default):
    """Return ``value`` when it is a valid choice, otherwise ``default``."""
    if value in choices:
        return value
    return default
