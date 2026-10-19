"""Publish recent games for the native tray menu.

The native host owns the UI; this module only writes a small owner-only file.
Each line is ``display name<TAB>game id``. Names have tabs and line breaks
removed because the native side splits on a single tab.
"""
from __future__ import annotations

from pathlib import Path

from state_store import secure_text_write

RECENT_LIMIT = 8


def _clean_name(value):
    return " ".join(str(value or "").replace("\t", " ").split())[:120] or "Untitled"


def recent_games(state, limit=RECENT_LIMIT):
    """Return ``(name, game_id)`` pairs, most recent first, without duplicates."""
    entries = []
    seen = set()
    games = {
        str(game.get("game_id") or game.get("id") or ""): game
        for game in (state.get("games") or [])
        if isinstance(game, dict)
    }
    history = state.get("history") if isinstance(state.get("history"), list) else []
    for session in reversed(history):
        if not isinstance(session, dict):
            continue
        game_id = str(session.get("game_id") or "")
        if not game_id or game_id in seen:
            continue
        name = session.get("game") or games.get(game_id, {}).get("name")
        seen.add(game_id)
        entries.append((_clean_name(name), game_id))
        if len(entries) >= limit:
            return entries
    played = [
        (str(game.get("last_played") or ""), str(game.get("game_id") or ""), game.get("name"))
        for game in games.values()
        if game.get("last_played") and str(game.get("game_id") or "") not in seen
    ]
    for _stamp, game_id, name in sorted(played, reverse=True):
        if not game_id or game_id in seen:
            continue
        seen.add(game_id)
        entries.append((_clean_name(name), game_id))
        if len(entries) >= limit:
            break
    return entries


def write_recent_games(data_parent, state, limit=RECENT_LIMIT):
    path = Path(data_parent) / "native-host-recent"
    lines = [f"{name}\t{game_id}\n" for name, game_id in recent_games(state, limit)]
    secure_text_write(path, "".join(lines))
    return path
