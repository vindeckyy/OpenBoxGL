"""Launcher-level trophies (S6): deterministic rules over library + history.

Trophies are OpenBox-level achievements (Decade Tourist, Deep Diver, Curator,
…).  Every rule is a pure function of ``state["games"]`` + ``state["history"]``
— no telemetry, no randomness, no clocks beyond the recorded timestamps, so a
fixture library always awards exactly the expected set.

Earned trophies are persisted under ``state["trophies"]["awarded"]`` (id ->
ISO timestamp) purely so the case can show an earn date and the toast fires
once.  Awards are never revoked: ``awarded`` means "the rule was met at least
once", while ``progress`` always reports the live count.
"""

from __future__ import annotations

import datetime
from typing import Any

STATE_KEY = "trophies"
AWARDS_KEY = "awarded"

# Night Owl fires on a session started between 00:00 and 04:59 local time.
NIGHT_OWL_LAST_HOUR = 4
# Old School requires the played game's release year to be before this one.
OLD_SCHOOL_YEAR_LIMIT = 1990
# Sane lower bound for treating a game's release year as real metadata.
MIN_VALID_YEAR = 1950

MARATHON_SECONDS = 4 * 3600
DEEP_DIVER_SECONDS = 20 * 3600
CENTURY_CLUB_SECONDS = 100 * 3600

FINISHED_MARKERS = ("beaten", "completed", "mastered")

# Rule table: declaration order is the case order.  Each rule awards when the
# collected stat reaches ``target``; ``progress`` reports ``min(stat, target)``.
TROPHY_RULES = (
    {"id": "century_club", "stat": "total_playtime", "target": CENTURY_CLUB_SECONDS},
    {"id": "collector", "stat": "games", "target": 100},
    {"id": "completionist", "stat": "finished_games", "target": 5},
    {"id": "curator", "stat": "cover_games", "target": 25},
    {"id": "decade_tourist", "stat": "decades_played", "target": 4},
    {"id": "deep_diver", "stat": "max_game_playtime", "target": DEEP_DIVER_SECONDS},
    {"id": "first_launch", "stat": "sessions", "target": 1},
    {"id": "marathon", "stat": "max_session_seconds", "target": MARATHON_SECONDS},
    {"id": "night_owl", "stat": "night_sessions", "target": 1},
    {"id": "old_school", "stat": "pre_limit_played", "target": 1},
    {"id": "platform_hopper", "stat": "platforms_played", "target": 4},
    {"id": "week_streak", "stat": "longest_streak", "target": 7},
)

_RULE_BY_ID = {rule["id"]: rule for rule in TROPHY_RULES}


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _parse_started(value: Any) -> datetime.datetime | None:
    """Parse a history ``started`` timestamp; date-only strings get hour None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        if "T" in text or " " in text:
            return datetime.datetime.fromisoformat(text.replace(" ", "T", 1))
        datetime.datetime.strptime(text[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    return None


def _parse_day(value: Any) -> datetime.date | None:
    parsed = _parse_started(value)
    if parsed is not None:
        return parsed.date()
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _game_year(game: dict[str, Any]) -> int | None:
    try:
        year = int(game.get("year") or 0)
    except (TypeError, ValueError):
        return None
    return year if year >= MIN_VALID_YEAR else None


def _longest_day_run(days: set[datetime.date]) -> int:
    longest = run = 0
    previous = None
    for day in sorted(days):
        run = run + 1 if previous is not None and (day - previous).days == 1 else 1
        longest = max(longest, run)
        previous = day
    return longest


def collect_stats(games: Any, history: Any) -> dict[str, int]:
    """Linear pass over history then library producing one counter per rule stat."""
    stats = {
        "games": 0,
        "cover_games": 0,
        "finished_games": 0,
        "decades_played": 0,
        "platforms_played": 0,
        "genres_played": 0,
        "pre_limit_played": 0,
        "max_game_playtime": 0,
        "total_playtime": 0,
        "sessions": 0,
        "max_session_seconds": 0,
        "night_sessions": 0,
        "longest_streak": 0,
    }
    history_ids, played_days = _history_stats(history, stats)
    stats["longest_streak"] = _longest_day_run(played_days)
    _game_stats(games, history_ids, stats)
    return stats


def _history_stats(history: Any, stats: dict[str, int]) -> tuple[set[str], set[datetime.date]]:
    """Session counters + the played-game id set + played-day set for streaks."""
    history_ids: set[str] = set()
    played_days: set[datetime.date] = set()
    for entry in history if isinstance(history, list) else []:
        if not isinstance(entry, dict):
            continue
        stats["sessions"] += 1
        game_id = str(entry.get("game_id") or "").strip()
        if game_id:
            history_ids.add(game_id)
        stats["max_session_seconds"] = max(stats["max_session_seconds"], _int(entry.get("seconds")))
        started = _parse_started(entry.get("started"))
        if started is not None:
            played_days.add(started.date())
            if started.hour <= NIGHT_OWL_LAST_HOUR:
                stats["night_sessions"] += 1
        else:
            day = _parse_day(entry.get("started"))
            if day is not None:
                played_days.add(day)
    return history_ids, played_days


def _played_game(game: dict[str, Any], playtime: int, history_ids: set[str]) -> bool:
    """A game counts as played when it has playtime, plays, a last_played
    stamp, or at least one recorded history row."""
    return (
        playtime > 0
        or _int(game.get("play_count")) > 0
        or bool(str(game.get("last_played") or "").strip())
        or str(game.get("game_id") or "").strip() in history_ids
    )


def _game_stats(games: Any, history_ids: set[str], stats: dict[str, int]) -> None:
    """Library counters; era/platform/genre breadth counts played games only."""
    decades: set[int] = set()
    platforms: set[str] = set()
    genres: set[str] = set()
    for game in games if isinstance(games, list) else []:
        if not isinstance(game, dict):
            continue
        stats["games"] += 1
        playtime = _int(game.get("playtime_seconds"))
        stats["total_playtime"] += playtime
        stats["max_game_playtime"] = max(stats["max_game_playtime"], playtime)
        if game.get("has_cover"):
            stats["cover_games"] += 1
        progress = str(game.get("progress") or "").lower()
        if any(marker in progress for marker in FINISHED_MARKERS):
            stats["finished_games"] += 1
        if not _played_game(game, playtime, history_ids):
            continue
        year = _game_year(game)
        if year is not None:
            decades.add(year // 10)
            if year < OLD_SCHOOL_YEAR_LIMIT:
                stats["pre_limit_played"] += 1
        platform = str(game.get("platform") or "").strip()
        if platform:
            platforms.add(platform)
        for part in str(game.get("genre") or "").split(","):
            label = part.strip()
            if label:
                genres.add(label)
    stats["decades_played"] = len(decades)
    stats["platforms_played"] = len(platforms)
    stats["genres_played"] = len(genres)


def evaluate(games: Any, history: Any) -> list[dict[str, Any]]:
    """Evaluate every rule; returns one entry per rule in declaration order."""
    stats = collect_stats(games, history)
    entries = []
    for rule in TROPHY_RULES:
        current = stats.get(rule["stat"], 0)
        entries.append({
            "id": rule["id"],
            "awarded": current >= rule["target"],
            "progress": {"current": min(current, rule["target"]), "target": rule["target"]},
        })
    return entries


def _stored_awards(state: Any) -> dict[str, str]:
    if not isinstance(state, dict):
        return {}
    bucket = state.get(STATE_KEY)
    if not isinstance(bucket, dict):
        return {}
    awarded = bucket.get(AWARDS_KEY)
    if not isinstance(awarded, dict):
        return {}
    return {str(k): str(v) for k, v in awarded.items() if str(k) in _RULE_BY_ID and v}


def trophy_case(state: Any) -> dict[str, Any]:
    """Read-only trophy case payload: live rule status merged with earn dates."""
    state = state if isinstance(state, dict) else {}
    evaluated = evaluate(state.get("games"), state.get("history"))
    stored = _stored_awards(state)
    trophies = []
    earned = 0
    for entry in evaluated:
        awarded_at = stored.get(entry["id"])
        awarded = bool(entry["awarded"]) or awarded_at is not None
        if awarded:
            earned += 1
        trophies.append({
            "id": entry["id"],
            "awarded": awarded,
            "awarded_at": awarded_at,
            "progress": entry["progress"],
        })
    return {"trophies": trophies, "earned": earned, "total": len(TROPHY_RULES)}


def unrecorded_awards(state: Any) -> list[str]:
    """Ids of rules currently met but not yet persisted — read-path check.

    Lets the evaluate route answer "nothing new" without opening a state
    transaction, so the refresh-driven checks stay write-free.
    """
    if not isinstance(state, dict):
        return []
    stored = _stored_awards(state)
    return [
        entry["id"]
        for entry in evaluate(state.get("games"), state.get("history"))
        if entry["awarded"] and entry["id"] not in stored
    ]


def record_new_awards(state: dict[str, Any], now: str | None = None) -> list[dict[str, Any]]:
    """Persist newly met rules under ``state["trophies"]["awarded"]``.

    Mutator-shaped helper: call inside ``update_state_with_result`` so the
    evaluation reads the same state snapshot being committed.  Returns the
    list of ``{"id", "awarded_at"}`` entries recorded this call — empty when
    nothing new, which is what keeps award toasts idempotent.
    """
    if not isinstance(state, dict):
        return []
    now = now or datetime.datetime.now().isoformat(timespec="seconds")
    evaluated = evaluate(state.get("games"), state.get("history"))
    stored = _stored_awards(state)
    newly = []
    for entry in evaluated:
        if not entry["awarded"] or entry["id"] in stored:
            continue
        newly.append({"id": entry["id"], "awarded_at": now})
    if newly:
        bucket = state.setdefault(STATE_KEY, {})
        if not isinstance(bucket, dict):
            bucket = state[STATE_KEY] = {}
        awarded = bucket.setdefault(AWARDS_KEY, {})
        if not isinstance(awarded, dict):
            awarded = bucket[AWARDS_KEY] = {}
        for award in newly:
            awarded[award["id"]] = award["awarded_at"]
    return newly


def trophy_rule_ids() -> tuple[str, ...]:
    return tuple(rule["id"] for rule in TROPHY_RULES)
