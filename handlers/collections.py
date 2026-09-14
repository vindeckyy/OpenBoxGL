"""Smart collection routes (1.12.0): named Backlog Radio queries as living shelves."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pkg.parity  # noqa: F401,E402  # installs the flat parity_* import finder
from api_errors import BadRequest, NotFound  # noqa: E402
import openbox  # noqa: E402
from parity_collections import (  # noqa: E402
    collections_with_counts,
    delete_collection,
    save_collection,
)
from routes.registry import route  # noqa: E402
from webapp_state import transact_state  # noqa: E402


@route("GET", "/api/v2/collections", spec="handlers.collections.collections_list")
def collections_list(handler, parsed):
    """List saved collections with live match counts."""
    if not handler.authorized():
        handler.handle_unauthorized()
        return
    state = openbox.load_state()
    handler.send_json(200, {"items": collections_with_counts(state)})


@route("POST", "/api/v2/collections", spec="handlers.collections.collections_save")
def collections_save(handler, payload):
    """Create or replace a named collection from a Backlog Radio query."""
    if not handler.authorized():
        handler.handle_unauthorized()
        return
    body = payload if isinstance(payload, dict) else {}
    name = body.get("name")
    query = body.get("query")
    try:
        def mutate(state):
            return save_collection(state, name, query)
        _committed, saved = transact_state(mutate)
    except ValueError as exc:
        raise BadRequest(str(exc), code="COLLECTION_INVALID") from exc
    handler.send_json(200, {"ok": True, "saved": saved})


@route("POST", "/api/v2/collections/delete", spec="handlers.collections.collections_delete")
def collections_delete(handler, payload):
    """Remove a named collection; games themselves are never touched."""
    if not handler.authorized():
        handler.handle_unauthorized()
        return
    body = payload if isinstance(payload, dict) else {}
    name = body.get("name")

    def mutate(state):
        return delete_collection(state, name)

    _committed, deleted = transact_state(mutate)
    if not deleted:
        raise NotFound("Collection not found.", code="COLLECTION_NOT_FOUND")
    handler.send_json(200, {"ok": True, "deleted": str(name or "").strip()})
