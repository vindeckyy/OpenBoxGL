"""Pure helpers for OpenBox's persistent play queue."""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path

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
        game_path = str(game.get("path") or "") if game else ""
        result.append({
            **entry,
            "name": game.get("name", "Missing") if game else "Missing",
            "platform": game.get("platform", "") if game else "",
            "cover": game.get("cover", "") if game else "",
            "path_exists": bool(game) and bool(game_path) and Path(game_path).expanduser().exists(),
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
        state["queue"] = queue
        return item
    state["queue"] = queue
    return None


def main():
    # A queue with one valid entry must still persist the skips it recorded.
    state = {"games": [
        {"game_id": "g1", "path": ""},
        {"game_id": "g2", "path": "/bin/true"},
    ], "queue": [{"game_id": "g1", "skip": False}, {"game_id": "g2", "skip": False}]}
    next_item = advance(state)
    assert next_item["game_id"] == "g2"
    assert state["queue"][0]["skip"] is True, "skip state must be written back"
    assert advance(state, "g2") is None
    assert state["queue"][0]["skip"] is True
    print("play-queue skip persistence self-test: ok")


if __name__ == "__main__":
    main()
