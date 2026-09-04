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


def build_party_queue(
    games: list[dict[str, Any]],
    *,
    players: int = 2,
    minutes: int = 0,
    limit: int = PARTY_QUEUE_LIMIT,
) -> list[str]:
    """Build a party queue of game ids, best first.

    Filters to couch-eligible games, excludes titles whose typical session
    runs longer than 3x the session budget (same factor as the picker), sorts
    rating desc with a random tiebreak, and caps at `limit`.
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

    scored = [(-rating_of(game), random.random(), str(game.get("game_id") or game.get("id") or "")) for game in candidates]
    scored.sort(key=lambda item: (item[0], item[1]))
    return [game_id for _, _, game_id in scored[:limit] if game_id]
