"""Smart collections — named Backlog Radio queries pinned as living shelves (1.12.0).

A collection stores only ``{name, query}`` in ``state["smart_collections"]``;
membership is evaluated at read time through the canonical query pipeline
(``parity_query.parse_query`` + ``apply_query``), so a saved filter can never
drift from what the interpretation chips showed when it was saved.

Bounds: at most ``MAX_COLLECTIONS`` entries, names capped, queries capped —
the settings blob and the public payload stay small.
"""

from __future__ import annotations

from pkg.parity.parity_query import filter_games_by_query, parse_query

MAX_COLLECTIONS = 50
MAX_NAME_LENGTH = 80
MAX_QUERY_LENGTH = 500


def list_collections(state):
    """Return stored collections as ``[{name, query}]`` in insertion order."""
    items = state.get("smart_collections", []) if isinstance(state, dict) else []
    if not isinstance(items, list):
        return []
    return [
        {"name": str(item.get("name") or ""), "query": str(item.get("query") or "")}
        for item in items
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]


def _clean_name(name):
    text = str(name or "").strip()
    if not text:
        raise ValueError("Collection name is required.")
    if len(text) > MAX_NAME_LENGTH:
        raise ValueError(f"Collection name must be at most {MAX_NAME_LENGTH} characters.")
    return text


def _clean_query(query):
    text = " ".join(str(query or "").split())
    if not text:
        raise ValueError("Collection query is required.")
    if len(text) > MAX_QUERY_LENGTH:
        raise ValueError(f"Collection query must be at most {MAX_QUERY_LENGTH} characters.")
    return text


def save_collection(state, name, query):
    """Upsert ``{name, query}`` into ``state["smart_collections"]``; returns name."""
    name = _clean_name(name)
    query = _clean_query(query)
    items = state.setdefault("smart_collections", [])
    if not isinstance(items, list):
        items = []
        state["smart_collections"] = items
    entry = {"name": name, "query": query}
    for index, item in enumerate(items):
        if isinstance(item, dict) and str(item.get("name") or "") == name:
            items[index] = entry
            return name
    if len(items) >= MAX_COLLECTIONS:
        raise ValueError(f"At most {MAX_COLLECTIONS} smart collections can be saved.")
    items.append(entry)
    return name


def delete_collection(state, name):
    """Remove a collection by name; returns True when one existed."""
    name = str(name or "").strip()
    items = state.get("smart_collections", [])
    if not isinstance(items, list):
        return False
    kept = [item for item in items if not isinstance(item, dict) or str(item.get("name") or "") != name]
    if len(kept) == len(items):
        return False
    state["smart_collections"] = kept
    return True


def evaluate_collection(state, query, now=None):
    """Count games matching a stored query. Parse failures count as zero.

    A saved query that no longer parses (grammar drift across versions) shows
    as empty rather than erroring the whole list — the user can re-save it.
    """
    try:
        parsed = parse_query(query, now=now)
    except (ValueError, TypeError):
        return 0
    games = state.get("games", []) if isinstance(state, dict) else []
    try:
        return len(filter_games_by_query(games, parsed, now=now))
    except (ValueError, TypeError):
        return 0


def collections_with_counts(state, now=None):
    """List collections each annotated with a live ``count``."""
    return [
        {**item, "count": evaluate_collection(state, item["query"], now=now)}
        for item in list_collections(state)
    ]
