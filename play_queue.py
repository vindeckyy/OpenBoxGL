"""Pure helpers for OpenBox's persistent play queue."""
from __future__ import annotations
from datetime import datetime, timezone

CAP = 500
NOTE_LIMIT = 200


def normalize_queue(value):
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:CAP]:
        if not isinstance(item, dict):
            continue
        game_id = str(item.get("game_id") or "").strip()
        if not game_id:
            continue
        result.append({
            "game_id": game_id,
            "added_at": str(item.get("added_at") or datetime.now(timezone.utc).isoformat()),
            "note": str(item.get("note") or "")[:NOTE_LIMIT],
            "skip": bool(item.get("skip")),
        })
    return result


def resolve_queue(state):
    games = {str(game.get("game_id")): game for game in state.get("games", []) if isinstance(game, dict)}
    result = []
    for entry in normalize_queue(state.get("queue")):
        game = games.get(entry["game_id"])
        result.append({
            **entry,
            "name": game.get("name", "Missing") if game else "Missing",
            "platform": game.get("platform", "") if game else "",
            "cover": game.get("cover", "") if game else "",
            "path_exists": bool(game and game.get("path")),
            "missing": game is None,
        })
    return result


def enqueue(state, game_ids, position=None, note=""):
    if not isinstance(game_ids, list) or not all(isinstance(item, str) and item.strip() for item in game_ids):
        raise ValueError("game_ids must be a list of strings.")
    known = {str(game.get("game_id")) for game in state.get("games", [])}
    unknown = [item for item in game_ids if item not in known]
    if unknown:
        raise ValueError(f"Game not found: {unknown[0]}")
    queue = normalize_queue(state.get("queue"))
    entries = [{"game_id": item, "added_at": datetime.now(timezone.utc).isoformat(), "note": str(note or "")[:NOTE_LIMIT], "skip": False} for item in game_ids]
    if position is None:
        queue.extend(entries)
    else:
        position = max(0, min(int(position), len(queue)))
        queue[position:position] = entries
    state["queue"] = queue[:CAP]
    return game_ids


def remove(state, game_ids):
    ids = set(game_ids or [])
    before = normalize_queue(state.get("queue"))
    state["queue"] = [item for item in before if item["game_id"] not in ids]
    return len(before) - len(state["queue"])


def reorder(state, ordered_game_ids):
    current = normalize_queue(state.get("queue"))
    if len(ordered_game_ids or []) != len(current) or sorted(ordered_game_ids) != sorted(item["game_id"] for item in current):
        raise ValueError("ordered_game_ids must contain the current queue entries.")
    buckets = {}
    for item in current:
        buckets.setdefault(item["game_id"], []).append(item)
    state["queue"] = [buckets[item].pop(0) for item in ordered_game_ids]
    return state["queue"]


def advance(state, current_game_id=None):
    queue = normalize_queue(state.get("queue"))
    start = -1
    if current_game_id:
        for index, item in enumerate(queue):
            if item["game_id"] == current_game_id:
                start = index
                break
    games = {str(game.get("game_id")): game for game in state.get("games", [])}
    for index in range(start + 1, len(queue)):
        item = queue[index]
        if item.get("skip"):
            continue
        game = games.get(item["game_id"])
        if not game or not game.get("path"):
            item["skip"] = True
            continue
        return item
    state["queue"] = queue
    return None
