"""parity_picker.py — smart "what should I play?" suggestions.

No runtime deps. Pure functions operating on library state and history.
"""
from __future__ import annotations

import heapq
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
    return _estimated_minutes_for_genre(genre)


def _estimated_minutes_for_genre(genre: str) -> float:
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


def _picker_history_indexes(history: list[dict]) -> tuple[dict[Any, list[float]], dict[Any, datetime]]:
    """Build the two history lookups used by a picker pass.

    The picker evaluates every game, so scanning the complete history from
    each game turns a small history into an avoidable O(games * history) hot
    path.  Keep the public helper behavior unchanged while indexing once for
    the optimized request path.
    """
    sessions: dict[Any, list[float]] = {}
    recent: dict[Any, datetime] = {}
    for entry in history:
        if not isinstance(entry, dict):
            continue
        game_id = entry.get("game_id")
        try:
            hash(game_id)
        except TypeError:
            continue
        seconds = entry.get("seconds")
        if seconds:
            try:
                sessions.setdefault(game_id, []).append(float(seconds))
            except (TypeError, ValueError):
                pass
        started = entry.get("started")
        if started:
            try:
                dt = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            # Treat legacy naive timestamps as UTC so a malformed entry cannot
            # make an otherwise unrelated picker request fail when compared
            # with an aware timestamp.
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            if recent.get(game_id) is None or dt > recent[game_id]:
                recent[game_id] = dt
    return sessions, recent


def _indexed_days_since_last_play(game: dict, recent: dict[Any, datetime], now: datetime) -> int | None:
    """Match ``_days_since_last_play`` without rescanning history."""
    last = game.get("last_played")
    if last:
        try:
            dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            return max(0, (now - dt).days)
        except (TypeError, ValueError):
            pass
    dt = recent.get(game.get("id"))
    if dt is None:
        return None
    return max(0, (now - dt).days)


def _indexed_fits_minutes(
    game: dict,
    sessions: dict[Any, list[float]],
    minutes: int,
    genre: str,
    estimate_cache: dict[str, float] | None = None,
) -> bool:
    if not minutes:
        return True
    target = minutes * 60
    values = sessions.get(game.get("id"))
    if values:
        return _median(values) <= target * 1.5
    if estimate_cache is not None:
        estimate = estimate_cache.get(genre)
        if estimate is None:
            estimate = _estimated_minutes_for_genre(genre)
            estimate_cache[genre] = estimate
    else:
        estimate = _estimated_minutes_for_genre(genre)
    return estimate <= minutes * 1.5


def _indexed_mood_match(genre: str, mood: str) -> bool:
    if mood in ("any", "retro", "party"):
        return mood == "any"
    return any(keyword in genre for keyword in MOOD_GENRES.get(mood, set()))


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
    players = criteria.get("players") or 1
    mood = criteria.get("mood") or "any"
    familiarity = criteria.get("familiarity") or "any"
    minutes = criteria.get("minutes") or 0
    history_sessions, history_recent = _picker_history_indexes(history)
    # Synthetic and imported libraries repeat the same genre/date values many
    # times. Cache their cheap normalization and date parsing for this pass so
    # scoring cost stays proportional to the number of games rather than the
    # number of duplicate metadata values.
    genre_cache: dict[Any, str] = {}
    estimate_cache: dict[str, float] = {}
    date_cache: dict[str, datetime | None] = {}
    eligible = []
    for game in games:
        if game.get("hidden") or game.get("hide_in_bigbox"):
            continue
        if game.get("path_exists") is False and game.get("store_installed") is False:
            continue
        max_players = int(game.get("max_players") or 1)
        if players > 1 and max_players < players:
            continue
        raw_genre = game.get("genre")
        try:
            genre = genre_cache[raw_genre]
        except (KeyError, TypeError):
            genre = raw_genre.lower() if isinstance(raw_genre, str) else _normalize_text(raw_genre)
            try:
                genre_cache[raw_genre] = genre
            except TypeError:
                pass
        mood_match = _indexed_mood_match(genre, mood)
        if mood == "party" and max_players <= 1:
            continue
        if mood == "retro":
            try:
                year = int(game.get("year") or 0)
            except (TypeError, ValueError):
                year = 0
            if year >= 2001:
                continue
        elif mood not in ("any", "party") and not mood_match:
            continue
        play_count = int(game.get("play_count") or 0)
        if familiarity == "new" and play_count > 0:
            continue
        rating = float(game.get("rating") or 0)
        if familiarity == "favorite" and not (game.get("favorite") or rating >= 4):
            continue
        fits_minutes = _indexed_fits_minutes(game, history_sessions, minutes, genre, estimate_cache)
        if not fits_minutes:
            continue
        last = game.get("last_played")
        days = None
        if last:
            date_key = str(last).replace("Z", "+00:00")
            if date_key not in date_cache:
                try:
                    dt = datetime.fromisoformat(date_key)
                    date_cache[date_key] = (
                        dt.replace(tzinfo=timezone.utc)
                        if dt.tzinfo is None
                        else dt.astimezone(timezone.utc)
                    )
                except (TypeError, ValueError):
                    date_cache[date_key] = None
            dt = date_cache[date_key]
            if dt is not None:
                try:
                    days = max(0, (now - dt).days)
                except TypeError:
                    # Preserve the legacy fallback for naive timestamps.
                    days = None
        if days is None:
            dt = history_recent.get(game.get("id"))
            if dt is not None:
                days = max(0, (now - dt).days)
        reason_key = None
        reason_params = {"name": str(game.get("name") or "game")}
        if play_count == 0:
            reason_key = "picker.reason.never_played"
        elif game.get("favorite"):
            reason_key = "picker.reason.favorite"
            if days is not None:
                reason_params["days"] = days
        elif days is not None and days > 30:
            reason_key = "picker.reason.long_time"
            reason_params["days"] = days
        elif mood_match and mood != "any":
            reason_key = "picker.reason.mood"
            reason_params["mood"] = mood
        if reason_key is None and rating >= 4:
            reason_key = "picker.reason.rated"
            reason_params["rating"] = rating
        if reason_key is None and minutes:
            reason_key = "picker.reason.fits_session"
            reason_params["minutes"] = minutes

        # Inline _score while the normalized genre, history-derived values,
        # and parsed numeric fields are already available.
        score = 12.0 if play_count == 0 else math.log1p(play_count) * 2.5
        if game.get("favorite"):
            score += 10.0
        if rating:
            score += rating * 3.0
        score += min(days, 365) / 365.0 * 8.0 if days is not None else 8.0
        if mood != "any" and mood_match:
            score += 6.0
        if minutes:
            score += 4.0
        if players > 1 and max_players >= players:
            score += 3.0
        eligible.append({
            "game": game,
            "score": score,
            "reason_key": reason_key,
            "reason_params": reason_params,
            "days": days,
            "mood_match": mood_match,
            "fits_minutes": fits_minutes,
            "play_count": play_count,
            "rating": rating,
        })

    if not eligible:
        return []

    # Only the top twelve can be selected.  Keep the deterministic ordering
    # while avoiding a full O(N log N) sort for very large libraries.
    top = heapq.nsmallest(12, eligible, key=lambda x: (-x["score"], x["game"].get("id", 0)))

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

        days = item["days"]
        play_count = item["play_count"]

        # Rewrite reason to the strongest signal, in priority order.
        if play_count == 0:
            reason_key = "picker.reason.never_played"
        elif game.get("favorite") and (days is None or days > 14):
            reason_key = "picker.reason.favorite"
            reason_params["days"] = days or 0
        elif days is not None and days > 30:
            reason_key = "picker.reason.long_time"
            reason_params["days"] = days
        elif mood != "any" and item["mood_match"]:
            reason_key = "picker.reason.mood"
            reason_params["mood"] = mood
        elif minutes and item["fits_minutes"]:
            reason_key = "picker.reason.fits_session"
            reason_params["minutes"] = minutes
        elif item["rating"] >= 4:
            reason_key = "picker.reason.rated"
            reason_params["rating"] = item["rating"]
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
