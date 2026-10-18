"""Persistent notification feed helpers for OpenBox."""
from __future__ import annotations

from datetime import datetime, timezone
import uuid

CAP = 200
_DEDUPE_SECONDS = 600
MAX_PARAMS = 10
MAX_PARAM_TEXT = 200

# Every translated server notice: key -> required placeholder names. The
# English title/body stays on the item as a fallback for old clients and
# webhook consumers.
SERVER_NOTICE_KEYS = {
    "notify.server.cloud_sync_ok_title": (),
    "notify.server.cloud_sync_ok_body": ("count",),
    "notify.server.cloud_sync_ok_body_one": ("count",),
    "notify.server.cloud_sync_failed_title": (),
    "notify.server.cloud_sync_failed_body": ("detail",),
    "notify.server.launch_audit_title": ("blocked", "warning"),
    "notify.server.launch_audit_body": ("blocked", "warning", "before_blocked", "before_warning"),
    "notify.server.defs_rejected_title": (),
    "notify.server.defs_rejected_body": ("detail",),
    "notify.server.webhook_failed_title": (),
    "notify.server.webhook_failed_body": ("event_id", "detail"),
}


def _clean_params(params):
    if not isinstance(params, dict):
        return {}
    clean = {}
    for key, value in list(params.items())[:MAX_PARAMS]:
        if not isinstance(key, str) or not key:
            continue
        if isinstance(value, bool) or isinstance(value, (int, float)):
            clean[key] = value
        elif isinstance(value, str):
            clean[key] = value[:MAX_PARAM_TEXT]
        else:
            clean[key] = str(value)[:MAX_PARAM_TEXT]
    return clean


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(value: str) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return 0.0


def add_notification(
    state, *, kind, level, title, body, source="", correlation_id="", dedupe_key="",
    title_key="", body_key="", params=None, now=None,
):
    items = state.setdefault("notifications", [])
    if not isinstance(items, list):
        items = []
        state["notifications"] = items
    created_at = now or _now()
    for item in items:
        if not isinstance(item, dict):
            continue
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
    if title_key:
        item["title_key"] = str(title_key)[:120]
    if body_key:
        item["body_key"] = str(body_key)[:120]
    clean_params = _clean_params(params)
    if clean_params:
        item["params"] = clean_params
    state["notifications"] = [item, *items][:CAP]
    return item


def unread_count(state):
    return sum(1 for item in state.get("notifications", []) if isinstance(item, dict) and not item.get("read"))


def mark_read(state, ids=None):
    wanted = set(ids or [])
    for item in state.setdefault("notifications", []):
        if isinstance(item, dict) and (not wanted or item.get("id") in wanted):
            item["read"] = True


def clear(state, ids=None):
    wanted = set(ids or [])
    if not wanted:
        state["notifications"] = []
    else:
        state["notifications"] = [
            item for item in state.get("notifications", [])
            if isinstance(item, dict) and item.get("id") not in wanted
        ]


CLOUD_SYNC_KIND = "cloud_sync"


def record_cloud_sync_outcome(state, *, success, body, now=None, params=None):
    """Keep only the latest cloud-sync success or failure notification."""
    items = state.setdefault("notifications", [])
    if not isinstance(items, list):
        items = []
        state["notifications"] = items
    state["notifications"] = [
        item for item in items
        if not (isinstance(item, dict) and item.get("kind") == CLOUD_SYNC_KIND)
    ]
    return add_notification(
        state,
        kind=CLOUD_SYNC_KIND,
        level="info" if success else "error",
        title="Cloud sync succeeded" if success else "Cloud sync failed",
        body=body,
        source="cloud_sync",
        dedupe_key="cloud_sync:last",
        title_key=(
            "notify.server.cloud_sync_ok_title" if success
            else "notify.server.cloud_sync_failed_title"
        ),
        body_key=(
            "notify.server.cloud_sync_ok_body" if success
            else "notify.server.cloud_sync_failed_body"
        ),
        params=params or {"detail": body},
        now=now,
    )
