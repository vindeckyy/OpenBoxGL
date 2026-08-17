"""Discovery Center lists and richer related-game scoring."""

from __future__ import annotations

import random
import re

import threading
import time
from datetime import datetime

_WORD_RE = re.compile(r"\w+")


_DISCOVERY_CACHE = {"at": 0.0, "signature": None, "result": None}
_DISCOVERY_LOCK = threading.Lock()
_DISCOVERY_TTL = 30.0


def clear_discovery_cache():
    """Invalidate the discovery cache, called by transact_state."""
    with _DISCOVERY_LOCK:
        _DISCOVERY_CACHE.update({"at": 0.0, "signature": None, "result": None})


def discovery_lists(games, limit=12):
    # Sample first, middle, last games to detect content changes without hashing all games
    sample = []
    if games:
        for idx in [0, len(games) // 2, -1]:
            game = games[idx]
            sample.append((game.get("game_id", ""), game.get("rating", ""), game.get("favorite", False)))
    signature = (len(games), tuple(sample), limit)
    now = time.monotonic()
    with _DISCOVERY_LOCK:
        if _DISCOVERY_CACHE["signature"] == signature and now - _DISCOVERY_CACHE["at"] < _DISCOVERY_TTL:
            return _DISCOVERY_CACHE["result"]
    result = _discovery_lists_uncached(games, limit)
    with _DISCOVERY_LOCK:
        _DISCOVERY_CACHE.update({"at": now, "signature": signature, "result": result})
    return result


def _discovery_lists_uncached(games, limit=12):
    indexed = list(enumerate(games))
    visible = [(i, g) for i, g in indexed if not g.get("hidden")]

    def take(rows):
        return [index for index, _ in rows[:limit]]

    recently_added = sorted(
        visible,
        key=lambda item: str(item[1].get("added_at") or ""),
        reverse=True,
    )
    never_played = [(i, g) for i, g in visible if not g.get("play_count")]
    short_sessions = sorted(
        [(i, g) for i, g in visible if g.get("playtime_seconds")],
        key=lambda item: int(item[1].get("playtime_seconds") or 0),
    )
    highly_rated = sorted(
        [(i, g) for i, g in visible if item_rating(g) >= 4],
        key=lambda item: item_rating(item[1]),
        reverse=True,
    )
    continue_playing = sorted(
        [(i, g) for i, g in visible if g.get("progress") == "Playing" or g.get("last_played")],
        key=lambda item: str(item[1].get("last_played") or ""),
        reverse=True,
    )
    random_pool = list(visible)
    random.shuffle(random_pool)
    return {
        "recently_added": take(recently_added),
        "never_played": take(never_played),
        "short_sessions": take(short_sessions),
        "highly_rated": take(highly_rated),
        "continue_playing": take(continue_playing),
        "random_picks": take(random_pool),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def item_rating(game):
    try:
        return float(game.get("rating") or 0)
    except (TypeError, ValueError):
        return 0.0


def related_with_reasons(games, selected, limit=8):
    base = games[selected]
    base_genres = set(_WORD_RE.findall(str(base.get("genre", "")).lower()))
    ranked = []
    for index, game in enumerate(games):
        if index == selected or game.get("hidden"):
            continue
        reasons = []
        score = 0
        genres = set(_WORD_RE.findall(str(game.get("genre", "")).lower()))
        shared = base_genres & genres
        if shared:
            score += 2 * len(shared)
            reasons.append("shared genre")
        if base.get("series") and base.get("series") == game.get("series"):
            score += 8
            reasons.append("same series")
        if base.get("collection") and base.get("collection") == game.get("collection"):
            score += 5
            reasons.append("same collection")
        if base.get("developer") and base.get("developer") == game.get("developer"):
            score += 3
            reasons.append("same developer")
        if base.get("platform") and base.get("platform") == game.get("platform"):
            score += 2
            reasons.append("same platform")
        if base.get("publisher") and base.get("publisher") == game.get("publisher"):
            score += 1
            reasons.append("same publisher")
        if score:
            ranked.append((-score, str(game.get("sort_title") or game.get("name", "")).casefold(), index, reasons, score))
    return [
        {"id": index, "score": score, "reasons": reasons}
        for _, _, index, reasons, score in sorted(ranked)[:limit]
    ]
