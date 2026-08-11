"""Persistent notification feed helpers for OpenBox."""
from __future__ import annotations

from datetime import datetime, timezone
import uuid

CAP = 200
_DEDUPE_SECONDS = 600


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(value: str) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return 0.0


def add_notification(state, *, kind, level, title, body, source="", correlation_id="", dedupe_key="", now=None):
    items = state.setdefault("notifications", [])
    if not isinstance(items, list):
        items = []
        state["notifications"] = items
    created_at = now or _now()
    for item in items:
        same_key = dedupe_key and item.get("dedupe_key") == dedupe_key
        same_recent = (item.get("kind"), item.get("title")) == (kind, title) and _parse(created_at) - _parse(item.get("created_at")) < _DEDUPE_SECONDS
        if same_key or same_recent:
            return item
    item = {
        "id": f"nt-{uuid.uuid4().hex[:12]}", "kind": str(kind),
        "level": str(level), "title": str(title)[:200],
        "body": str(body)[:2000], "created_at": created_at, "read": False,
    }
    for key, value in (("source", source), ("correlation_id", correlation_id), ("dedupe_key", dedupe_key)):
        if value:
            item[key] = str(value)[:200]
    state["notifications"] = [item, *items][:CAP]
    return item


def unread_count(state):
    return sum(1 for item in state.get("notifications", []) if isinstance(item, dict) and not item.get("read"))


def mark_read(state, ids=None):
    wanted = set(ids or [])
    for item in state.setdefault("notifications", []):
        if not wanted or item.get("id") in wanted:
            item["read"] = True


def clear(state, ids=None):
    wanted = set(ids or [])
    if not wanted:
        state["notifications"] = []
    else:
        state["notifications"] = [item for item in state.get("notifications", []) if item.get("id") not in wanted]
