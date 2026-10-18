"""parity_party.py — Game Night party queue builder.

No runtime deps. Pure functions operating on library state.
"""
from __future__ import annotations

import random
from typing import Any

# ponytail: static couch-platform set. Consoles, handhelds, and arcade cabinets
# are couch-multiplayer by default; computer platforms qualify only through
# explicit per-game controller_support. Upgrade path: derive from a
# user-editable platform taxonomy if one ever exists.
COUCH_PLATFORMS = frozenset({
    # Nintendo
    "NES", "SNES", "Nintendo 64", "GameCube", "Wii", "Wii U", "Switch",
    "Nintendo Switch", "Game Boy", "Game Boy Color", "Game Boy Advance",
    "Nintendo DS", "Nintendo 3DS",
    # Sega
    "Sega Genesis", "Sega Mega Drive", "Sega Master System", "Sega Game Gear",
    "Sega Saturn", "Sega Dreamcast", "Sega CD",
    # Sony
    "PlayStation", "PlayStation 2", "PlayStation 3", "PlayStation 4",
    "PlayStation 5", "PSP", "PlayStation Vita",
    # Microsoft
    "Xbox", "Xbox 360", "Xbox One", "Xbox Series X", "Xbox Series S",
    # Arcade
    "Arcade", "MAME", "Neo Geo", "Neo Geo AES",
    # Atari / NEC
    "Atari 2600", "Atari 5200", "Atari 7800", "Atari Jaguar", "Atari Lynx",
    "Turbografx-16", "TurboGrafx-16", "PC Engine",
})

PARTY_QUEUE_LIMIT = 50
MAX_DECKS = 20
MAX_DECK_NAME = 60
MAX_DECK_QUERY = 500
MAX_DECK_MINUTES = 1440


def _stable_id(game: dict[str, Any]) -> str:
    return str(game.get("game_id") or game.get("id") or "")


def _clean_deck_name(name) -> str:
    text = str(name or "").strip()
    if not text:
        raise ValueError("Deck name is required.")
    if len(text) > MAX_DECK_NAME:
        raise ValueError(f"Deck name must be at most {MAX_DECK_NAME} characters.")
    return text


def _clean_deck_query(query) -> str:
    if query is not None and not isinstance(query, str):
        raise ValueError("Deck filter must be text.")
    text = " ".join(str(query or "").split())
    if len(text) > MAX_DECK_QUERY:
        raise ValueError(f"Deck filter must be at most {MAX_DECK_QUERY} characters.")
    return text


def _clean_deck_players(players) -> int:
    try:
        value = int(players)
    except (TypeError, ValueError) as error:
        raise ValueError("Deck players must be an integer.") from error
    if not 2 <= value <= 8:
        raise ValueError("Deck players must be between 2 and 8.")
    return value


def _clean_deck_minutes(minutes) -> int:
    try:
        value = int(minutes or 0)
    except (TypeError, ValueError) as error:
        raise ValueError("Deck minutes must be an integer.") from error
    if not 0 <= value <= MAX_DECK_MINUTES:
        raise ValueError(f"Deck minutes must be between 0 and {MAX_DECK_MINUTES}.")
    return value


def normalize_decks(state) -> bool:
    """Repair state["party_decks"] in place; returns True when it changed."""
    items = state.get("party_decks")
    if not isinstance(items, list):
        state["party_decks"] = []
        return True
    clean = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            name = _clean_deck_name(item.get("name"))
            query = _clean_deck_query(item.get("query"))
            players = _clean_deck_players(item.get("players", 2))
            minutes = _clean_deck_minutes(item.get("minutes", 0))
        except ValueError:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        clean.append({"name": name, "query": query, "players": players, "minutes": minutes})
        if len(clean) >= MAX_DECKS:
            break
    if clean != items:
        state["party_decks"] = clean
        return True
    return False


def list_decks(state) -> list[dict[str, Any]]:
    items = state.get("party_decks", []) if isinstance(state, dict) else []
    return [dict(item) for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def save_deck(state, *, name, query="", players=2, minutes=0) -> dict[str, Any]:
    from pkg.parity.parity_query import parse_query

    name = _clean_deck_name(name)
    query = _clean_deck_query(query)
    if query:
        try:
            parse_query(query)
        except (ValueError, TypeError) as error:
            raise ValueError(f"Deck filter is invalid: {error}") from error
    entry = {
        "name": name,
        "query": query,
        "players": _clean_deck_players(players),
        "minutes": _clean_deck_minutes(minutes),
    }
    items = state.setdefault("party_decks", [])
    if not isinstance(items, list):
        items = []
        state["party_decks"] = items
    for index, item in enumerate(items):
        if isinstance(item, dict) and str(item.get("name") or "").strip().casefold() == name.casefold():
            items[index] = entry
            return dict(entry)
    if len(items) >= MAX_DECKS:
        raise ValueError(f"At most {MAX_DECKS} decks can be saved.")
    items.append(entry)
    return dict(entry)


def delete_deck(state, name) -> bool:
    name = str(name or "").strip()
    items = state.get("party_decks", [])
    if not isinstance(items, list):
        return False
    kept = [item for item in items if not isinstance(item, dict) or str(item.get("name") or "") != name]
    if len(kept) == len(items):
        return False
    state["party_decks"] = kept
    return True


def _max_players(game: dict[str, Any]) -> int:
    try:
        return max(1, int(game.get("max_players") or 1))
    except (TypeError, ValueError):
        return 1


def _path_usable(game: dict[str, Any]) -> bool:
    # Same default as the picker: only a known-missing path without a store
    # install disqualifies; unknown counts as usable.
    if game.get("path_exists") is False and not game.get("store_installed"):
        return False
    return True


def _avg_session_seconds(game: dict[str, Any]) -> float | None:
    try:
        plays = int(game.get("play_count") or 0)
        total = float(game.get("playtime_seconds") or 0)
    except (TypeError, ValueError):
        return None
    if plays > 0 and total > 0:
        return total / plays
    return None


def eligible_party_games(games: list[dict[str, Any]], *, players: int = 2) -> list[dict[str, Any]]:
    """Games suitable for a couch session with `players` participants."""
    eligible = []
    for game in games:
        if not isinstance(game, dict):
            continue
        if game.get("hidden") or game.get("hide_in_bigbox"):
            continue
        if not _path_usable(game):
            continue
        if _max_players(game) < players:
            continue
        platform = str(game.get("platform") or "").strip()
        has_controller = bool(str(game.get("controller_support") or "").strip())
        if not has_controller and platform not in COUCH_PLATFORMS:
            continue
        eligible.append(game)
    return eligible


def queue_exclusion_breakdown(
    games: list[dict[str, Any]],
    *,
    players: int = 2,
    minutes: int = 0,
) -> dict[str, int]:
    """Count why games were excluded from a party queue (same rules as build).

    Keys: total, hidden, unusable_path, too_few_players,
    no_controller_or_platform, over_budget. Used to explain empty queues.
    """
    try:
        players = max(2, min(8, int(players)))
    except (TypeError, ValueError):
        players = 2
    try:
        minutes = int(minutes or 0)
    except (TypeError, ValueError):
        minutes = 0
    counts = {
        "total": 0, "hidden": 0, "unusable_path": 0, "too_few_players": 0,
        "no_controller_or_platform": 0, "over_budget": 0,
    }
    budget = minutes * 60 * 3 if minutes > 0 else None
    for game in games or []:
        if not isinstance(game, dict):
            continue
        counts["total"] += 1
        if game.get("hidden") or game.get("hide_in_bigbox"):
            counts["hidden"] += 1
            continue
        if not _path_usable(game):
            counts["unusable_path"] += 1
            continue
        if _max_players(game) < players:
            counts["too_few_players"] += 1
            continue
        platform = str(game.get("platform") or "").strip()
        if not str(game.get("controller_support") or "").strip() and platform not in COUCH_PLATFORMS:
            counts["no_controller_or_platform"] += 1
            continue
        if budget is not None:
            avg = _avg_session_seconds(game)
            if avg is not None and avg > budget:
                counts["over_budget"] += 1
    return counts


def empty_queue_reason(breakdown: dict[str, int], players: int = 2) -> str:
    """Human-readable explanation for an empty party queue."""
    total = breakdown.get("total", 0)
    if not total:
        return "Your library is empty — import games to build a party queue."
    ranked = sorted(
        (("too_few_players", f"No games support {players} players — lower the player count or add Max players metadata."),
         ("no_controller_or_platform", "No couch-ready games found — add controller support or console platforms."),
         ("unusable_path", "Every candidate has missing game files — fix paths or reinstall."),
         ("over_budget", "Every candidate runs longer than the session budget — raise the minutes."),
         ("hidden", "Every game in the library is hidden.")),
        key=lambda item: breakdown.get(item[0], 0),
        reverse=True,
    )
    for key, message in ranked:
        if breakdown.get(key, 0):
            return message
    return "No eligible games for this setup."


def build_party_queue(
    games: list[dict[str, Any]],
    *,
    players: int = 2,
    minutes: int = 0,
    limit: int = PARTY_QUEUE_LIMIT,
    seed: int | None = None,
) -> list[str]:
    """Build a party queue of game ids, best first.

    Filters to couch-eligible games, excludes titles whose typical session
    runs longer than 3x the session budget (same factor as the picker), sorts
    rating desc with a random tiebreak, and caps at `limit`. With `seed`, the
    candidate order is normalized by stable id first, so the same library and
    seed produce the same draw on another device.
    """
    try:
        players = int(players)
    except (TypeError, ValueError):
        players = 2
    players = max(2, min(8, players))
    try:
        minutes = int(minutes or 0)
    except (TypeError, ValueError):
        minutes = 0
    try:
        limit = int(limit or PARTY_QUEUE_LIMIT)
    except (TypeError, ValueError):
        limit = PARTY_QUEUE_LIMIT
    limit = max(1, min(PARTY_QUEUE_LIMIT, limit))

    candidates = eligible_party_games(games, players=players)
    if minutes > 0:
        budget = minutes * 60 * 3
        kept = []
        for game in candidates:
            avg = _avg_session_seconds(game)
            if avg is None or avg <= budget:
                kept.append(game)
        candidates = kept

    def rating_of(game: dict[str, Any]) -> float:
        try:
            return float(game.get("rating") or 0)
        except (TypeError, ValueError):
            return 0.0

    rng = random.Random(seed) if seed is not None else random
    scored = [
        (-rating_of(game), rng.random(), _stable_id(game))
        for game in (sorted(candidates, key=_stable_id) if seed is not None else candidates)
    ]
    scored.sort(key=lambda item: (item[0], item[1]))
    return [game_id for _, _, game_id in scored[:limit] if game_id]
