"""Bulk literal find/replace over selected metadata fields.

Only fields the ordinary game editor exposes are editable. Replacement is
literal (no regex) so a stray ``$`` or ``\\`` can never rewrite more than the
user typed.
"""
from __future__ import annotations

FIELDS = {
    "name",
    "platform",
    "genre",
    "developer",
    "publisher",
    "region",
    "source",
    "notes",
    "progress",
    "esrb",
}
MAX_SELECTION = 50_000


def _field_value(game, field):
    value = game.get(field, "")
    return value if isinstance(value, str) else ""


def find_replace(games, ids, field, find, replacement, dry_run=True):
    field = str(field or "").strip()
    if field not in FIELDS:
        raise ValueError("That field cannot be edited in bulk.")
    find = str(find or "")
    replacement = str(replacement or "")
    if not find:
        raise ValueError("Find text is required.")
    if len(find) > 500 or len(replacement) > 500:
        raise ValueError("Find and replace text must be 500 characters or fewer.")
    selected = {str(item) for item in (ids or [])}
    if not selected or len(selected) > MAX_SELECTION:
        raise ValueError("Select between 1 and 50,000 games.")
    matches = []
    changed = 0
    for game in games:
        identity = str(game.get("game_id") or game.get("id") or "")
        if identity not in selected:
            continue
        before = _field_value(game, field)
        if find not in before:
            continue
        after = before.replace(find, replacement)
        matches.append({"game_id": identity, "name": game.get("name", ""), "before": before, "after": after})
        changed += 1
        if not dry_run:
            game[field] = after
    return {
        "field": field,
        "matched": changed,
        "games": matches[:100],
        "truncated": max(0, changed - 100),
        "applied": not dry_run,
    }
