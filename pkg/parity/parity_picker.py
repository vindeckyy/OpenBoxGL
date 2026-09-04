"""parity_picker.py — smart "what should I play?" suggestions.

No runtime deps. Pure functions operating on library state and history.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timezone
from typing import Any

# ponytail: simple keyword heuristics; upgrade to a hand-curated genre taxonomy
# if the keyword maps stop matching how users actually describe their games.
MOOD_GENRES = {
    "action": {"action", "shooter", "fps", "fighting", "platform", "beat", "up", "brawler"},
    "chill": {"puzzle", "simulation", "sandbox", "strategy", "turn", "card", "board"},
    "story": {"rpg", "adventure", "visual novel", "point and click", "narrative"},
    "retro": set(),  # year < 2001 is the signal, not a keyword
    "party": set(),  # max_players > 1 is the signal
}


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).lower()


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    if n % 2:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2


def _game_history_seconds(game_id: int, history: list[dict]) -> list[float]:
    out = []
    for entry in history:
        if entry.get("game_id") == game_id:
            seconds = entry.get("seconds")
            if seconds:
                try:
                    out.append(float(seconds))
                except (TypeError, ValueError):
                    pass
    return out


def _days_since_last_play(game: dict, history: list[dict], now: datetime) -> int | None:
    last = game.get("last_played")
    if last:
        try:
            dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            return max(0, (now - dt).days)
        except (TypeError, ValueError):
            pass
    # Fallback to most recent history entry for this game.
    recent = None
    for entry in history:
        if entry.get("game_id") == game.get("id"):
            started = entry.get("started")
            if started:
                try:
                    dt = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
                    if recent is None or dt > recent:
                        recent = dt
                except (TypeError, ValueError):
                    pass
    if recent is None:
        return None
    return max(0, (now - recent).days)


def _genre_matches_mood(genre: str, mood: str) -> bool:
    if mood == "any":
        return True
    if mood == "retro":
        return False  # handled by year
    if mood == "party":
        return False  # handled by max_players
    g = _normalize_text(genre)
    return any(k in g for k in MOOD_GENRES.get(mood, set()))


def _estimated_minutes_for_unplayed(game: dict) -> float | None:
    # ponytail: naive genre-based estimate when no play history exists.
    # If a how-long-to-beat source is added later, prefer that.
    genre = _normalize_text(game.get("genre"))
    if any(k in genre for k in ("rpg", "strategy", "simulation")):
        return 120.0
    if any(k in genre for k in ("adventure", "action", "shooter", "platform", "fighting")):
        return 60.0
    if any(k in genre for k in ("puzzle", "card", "board")):
        return 30.0
    return 45.0


def _fits_minutes(game: dict, history: list[dict], minutes: int) -> bool:
    if not minutes:
        return True
    target = minutes * 60
    sessions = _game_history_seconds(game.get("id"), history)
    if sessions:
        median = _median(sessions)
        # Allow games whose median session is at most 1.5x the requested time,
        # so a 20-minute game is still fine for a 30-minute slot.
        return median <= target * 1.5
    estimate = _estimated_minutes_for_unplayed(game)
    if estimate is None:
        return True
    return estimate <= minutes * 1.5


def _eligibility(game: dict, history: list[dict], criteria: dict, now: datetime) -> tuple[bool, str | None, dict]:
    """Return (eligible, reason_key, reason_params) for a single game.

    The reason here is just one candidate; `pick_games` selects the dominant
    factor across all eligible games and rewrites per-pick reasons.
    """
    players = criteria.get("players") or 1
    mood = criteria.get("mood") or "any"
    familiarity = criteria.get("familiarity") or "any"
    minutes = criteria.get("minutes") or 0

    if game.get("hidden") or game.get("hide_in_bigbox"):
        return False, None, {}
    if game.get("path_exists") is False and game.get("store_installed") is False:
        return False, None, {}

    # Scope is applied at the games list level before calling _eligibility.

    max_players = int(game.get("max_players") or 1)
    if players > 1 and max_players < players:
        return False, None, {}

    genre = _normalize_text(game.get("genre"))
    if mood == "party" and max_players <= 1:
        return False, None, {}
    if mood == "retro":
        try:
            year = int(game.get("year") or 0)
        except (TypeError, ValueError):
            year = 0
        if year >= 2001:
            return False, None, {}
    elif mood not in ("any", "party") and not _genre_matches_mood(genre, mood):
        return False, None, {}

    play_count = int(game.get("play_count") or 0)
    if familiarity == "new" and play_count > 0:
        return False, None, {}
    if familiarity == "favorite" and not (game.get("favorite") or float(game.get("rating") or 0) >= 4):
        return False, None, {}

    if not _fits_minutes(game, history, minutes):
        return False, None, {}

    reason_key = None
    reason_params = {"name": str(game.get("name") or "game")}
    if play_count == 0:
        reason_key = "picker.reason.never_played"
    elif game.get("favorite"):
        reason_key = "picker.reason.favorite"
        days = _days_since_last_play(game, history, now)
        if days is not None:
            reason_params["days"] = days
    else:
        days = _days_since_last_play(game, history, now)
        if days is not None and days > 30:
            reason_key = "picker.reason.long_time"
            reason_params["days"] = days
        elif _genre_matches_mood(genre, mood) and mood != "any":
            reason_key = "picker.reason.mood"
            reason_params["mood"] = mood
    if reason_key is None:
        rating = float(game.get("rating") or 0)
        if rating >= 4:
            reason_key = "picker.reason.rated"
            reason_params["rating"] = rating
    if reason_key is None and minutes:
        reason_key = "picker.reason.fits_session"
        reason_params["minutes"] = minutes

    return True, reason_key, reason_params


def _score(game: dict, history: list[dict], criteria: dict, now: datetime) -> float:
    """Additive score; higher is a stronger recommendation."""
    score = 0.0
    play_count = int(game.get("play_count") or 0)

    # Never played is a strong novelty signal.
    if play_count == 0:
        score += 12.0
    else:
        score += math.log1p(play_count) * 2.5

    # Favorite or high rating.
    if game.get("favorite"):
        score += 10.0
    rating = float(game.get("rating") or 0)
    if rating:
        score += rating * 3.0

    # Recency: older last play gets more points, capped at one year.
    days = _days_since_last_play(game, history, now)
    if days is not None:
        score += min(days, 365) / 365.0 * 8.0
    else:
        score += 8.0  # never played / no history, same as capped max

    # Mood keyword match.
    mood = criteria.get("mood") or "any"
    if mood != "any" and _genre_matches_mood(_normalize_text(game.get("genre")), mood):
        score += 6.0

    # Time fit.
    minutes = criteria.get("minutes") or 0
    if minutes and _fits_minutes(game, history, minutes):
        score += 4.0

    # Player fit.
    players = criteria.get("players") or 1
    max_players = int(game.get("max_players") or 1)
    if players > 1 and max_players >= players:
        score += 3.0

    return score


def pick_games(
    games: list[dict],
    history: list[dict],
    criteria: dict,
) -> list[dict]:
    """Return up to 3 picked games with scores and reasons.

    The games list is expected to already be scoped by the caller (all, one
    platform, or one playlist). criteria fields:
      - minutes: int (0 = any)
      - mood: one of any/action/chill/story/retro/party
      - familiarity: one of any/new/favorite
      - players: int >= 1
      - limit: int (default 3)
    """
    now = datetime.now(timezone.utc)
    eligible = []
    for game in games:
        ok, reason_key, reason_params = _eligibility(game, history, criteria, now)
        if not ok:
            continue
        s = _score(game, history, criteria, now)
        eligible.append({
            "game": game,
            "score": s,
            "reason_key": reason_key,
            "reason_params": reason_params,
        })

    if not eligible:
        return []

    # Deterministic sort by score desc, then stable by game id.
    eligible.sort(key=lambda x: (-x["score"], x["game"].get("id", 0)))
    top = eligible[:12]

    # Weighted random selection: higher-scored games are more likely to be picked.
    picks = []
    pool = list(top)
    limit = min(criteria.get("limit") or 3, 3)
    rng = random.Random()  # uses system entropy; no seed for variety
    while pool and len(picks) < limit:
        total = sum(item["score"] for item in pool)
        if total <= 0:
            pick = rng.choice(pool)
        else:
            r = rng.uniform(0, total)
            cumulative = 0.0
            pick = pool[-1]
            for item in pool:
                cumulative += item["score"]
                if cumulative >= r:
                    pick = item
                    break
        picks.append(pick)
        pool.remove(pick)

    # Build final reason from the dominant scoring factor if the initial reason
    # would be weak for the winner.
    result = []
    for item in picks:
        game = item["game"]
        g_name = str(game.get("name") or "game")
        reason_params = {"name": g_name}
        reason_key = item["reason_key"]

        days = _days_since_last_play(game, history, now)
        play_count = int(game.get("play_count") or 0)
        mood = criteria.get("mood") or "any"
        minutes = criteria.get("minutes") or 0

        # Rewrite reason to the strongest signal, in priority order.
        if play_count == 0:
            reason_key = "picker.reason.never_played"
        elif game.get("favorite") and (days is None or days > 14):
            reason_key = "picker.reason.favorite"
            reason_params["days"] = days or 0
        elif days is not None and days > 30:
            reason_key = "picker.reason.long_time"
            reason_params["days"] = days
        elif mood != "any" and _genre_matches_mood(_normalize_text(game.get("genre")), mood):
            reason_key = "picker.reason.mood"
            reason_params["mood"] = mood
        elif minutes and _fits_minutes(game, history, minutes):
            reason_key = "picker.reason.fits_session"
            reason_params["minutes"] = minutes
        elif float(game.get("rating") or 0) >= 4:
            reason_key = "picker.reason.rated"
            reason_params["rating"] = float(game.get("rating"))
        elif reason_key is None:
            reason_key = "picker.reason.never_played"

        result.append({
            "id": game.get("id"),
            "game_id": game.get("game_id") or str(game.get("id", "")),
            "name": g_name,
            "has_cover": bool(game.get("has_cover")),
            "cover": game.get("cover", ""),
            "cover_kind": "cover",
            "score": round(item["score"], 2),
            "reason_key": reason_key,
            "reason_params": reason_params,
        })
    return result
