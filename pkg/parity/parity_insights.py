"""Play Insights aggregation for OpenBox 1.7.1.

Local-first aggregation over library.json sessions/history + games.
No new storage, no telemetry. Pure functions for heatmap, streaks,
top platforms/genres, momentum.
"""

from __future__ import annotations

import datetime
from collections import Counter
from typing import Any


def _parse_date(value: str) -> datetime.date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        if "T" in text:
            dt = datetime.datetime.fromisoformat(text)
            return dt.date()
        dt = datetime.datetime.strptime(text[:10], "%Y-%m-%d")
        return dt.date()
    except (ValueError, TypeError):
        return None


def _history_daily_buckets(history: list[dict[str, Any]]) -> dict[datetime.date, dict[str, int]]:
    buckets: dict[datetime.date, dict[str, int]] = {}
    for entry in history:
        if not isinstance(entry, dict):
            continue
        started = entry.get("started") or entry.get("date") or ""
        date = _parse_date(str(started))
        if date is None:
            continue
        try:
            seconds = int(entry.get("seconds", 0) or 0)
        except (TypeError, ValueError):
            seconds = 0
        seconds = max(0, seconds)
        bucket = buckets.setdefault(date, {"count": 0, "seconds": 0})
        bucket["count"] += 1
        bucket["seconds"] += seconds
    return buckets


def _level_for_seconds(seconds: int, counts: list[int]) -> int:
    if seconds <= 0:
        return 0
    if not counts:
        return 1
    sorted_counts = sorted(counts)
    n = len(sorted_counts)
    if n < 4:
        if seconds < 600:
            return 1
        if seconds < 1800:
            return 2
        if seconds < 3600:
            return 3
        return 4
    q1 = sorted_counts[n // 4]
    q2 = sorted_counts[n // 2]
    q3 = sorted_counts[3 * n // 4]
    q1 = max(600, q1)
    q2 = max(1800, q2)
    q3 = max(3600, q3)
    if seconds < q1:
        return 1
    if seconds < q2:
        return 2
    if seconds < q3:
        return 3
    return 4


def compute_heatmap(
    history: list[dict[str, Any]],
    days: int = 366,
    end_date: datetime.date | None = None,
) -> list[dict[str, Any]]:
    if end_date is None:
        end_date = datetime.date.today()
    buckets = _history_daily_buckets(history)
    non_zero_seconds = [bucket["seconds"] for bucket in buckets.values() if bucket["seconds"] > 0]
    start_date = end_date - datetime.timedelta(days=days - 1)
    cells = []
    current = start_date
    while current <= end_date:
        bucket = buckets.get(current, {"count": 0, "seconds": 0})
        seconds = bucket["seconds"]
        count = bucket["count"]
        level = _level_for_seconds(seconds, non_zero_seconds)
        cells.append(
            {
                "date": current.isoformat(),
                "count": count,
                "seconds": seconds,
                "level": level,
            }
        )
        current += datetime.timedelta(days=1)
    return cells


def compute_streak(heatmap: list[dict[str, Any]]) -> dict[str, Any]:
    longest = 0
    running = 0
    last_played = ""
    for cell in heatmap:
        if cell["count"] > 0:
            running += 1
            last_played = cell["date"]
            if running > longest:
                longest = running
        else:
            running = 0
    trailing = 0
    for cell in reversed(heatmap):
        if cell["count"] > 0:
            trailing += 1
        else:
            break
    return {"current": trailing, "longest": longest, "last_played": last_played}


def compute_totals(games: list[dict[str, Any]], history: list[dict[str, Any]]) -> dict[str, Any]:
    total_games = len(games) if isinstance(games, list) else 0
    played = 0
    total_playtime = 0
    for game in games if isinstance(games, list) else []:
        if not isinstance(game, dict):
            continue
        try:
            pt = int(game.get("playtime_seconds", 0) or 0)
        except (TypeError, ValueError):
            pt = 0
        total_playtime += max(0, pt)
        if pt > 0 or game.get("last_played"):
            if game.get("last_played") or pt > 0:
                played += 1
    total_sessions = len([h for h in history if isinstance(h, dict)])
    return {
        "games": total_games,
        "played": played,
        "total_playtime_seconds": total_playtime,
        "total_sessions": total_sessions,
    }


def compute_top_platforms(
    games: list[dict[str, Any]], limit: int = 8
) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    playtime: Counter[str] = Counter()
    for game in games if isinstance(games, list) else []:
        if not isinstance(game, dict):
            continue
        platform = str(game.get("platform", "") or "Unspecified").strip() or "Unspecified"
        counter[platform] += 1
        try:
            pt = int(game.get("playtime_seconds", 0) or 0)
        except (TypeError, ValueError):
            pt = 0
        playtime[platform] += max(0, pt)
    items = []
    for platform, count in counter.most_common(limit):
        items.append({"platform": platform, "count": count, "playtime_seconds": playtime[platform]})
    items.sort(key=lambda x: (-x["count"], -x["playtime_seconds"], x["platform"]))
    return items[:limit]


def compute_top_genres(games: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for game in games if isinstance(games, list) else []:
        if not isinstance(game, dict):
            continue
        genre_field = str(game.get("genre", "") or "").strip()
        if not genre_field:
            continue
        for part in genre_field.split(","):
            label = part.strip()
            if label:
                counter[label] += 1
    items = [{"genre": genre, "count": count} for genre, count in counter.most_common(limit)]
    items.sort(key=lambda x: (-x["count"], x["genre"]))
    return items[:limit]


def compute_momentum(
    heatmap: list[dict[str, Any]],
) -> dict[str, int]:
    if len(heatmap) < 60:
        last_30 = heatmap[-30:] if len(heatmap) >= 30 else heatmap
        prev_30 = []
    else:
        last_30 = heatmap[-30:]
        prev_30 = heatmap[-60:-30]
    last_seconds = sum(c["seconds"] for c in last_30)
    prev_seconds = sum(c["seconds"] for c in prev_30)
    delta = last_seconds - prev_seconds
    return {
        "last_30_days_seconds": last_seconds,
        "previous_30_days_seconds": prev_seconds,
        "delta_seconds": delta,
    }


def summarize(state: dict[str, Any], end_date: datetime.date | None = None) -> dict[str, Any]:
    games = state.get("games", []) if isinstance(state, dict) else []
    history = state.get("history", []) if isinstance(state, dict) else []
    if not isinstance(games, list):
        games = []
    if not isinstance(history, list):
        history = []
    heatmap = compute_heatmap(history, days=366, end_date=end_date)
    streak = compute_streak(heatmap)
    totals = compute_totals(games, history)
    top_platforms = compute_top_platforms(games, limit=8)
    top_genres = compute_top_genres(games, limit=8)
    momentum = compute_momentum(heatmap)
    last_30_days = heatmap[-30:]
    return {
        "heatmap": heatmap,
        "streak": streak,
        "totals": totals,
        "top_platforms": top_platforms,
        "top_genres": top_genres,
        "momentum": momentum,
        "last_30_days": last_30_days,
    }


def heatmap_for_range(
    history: list[dict[str, Any]],
    days: int = 366,
    end_date: datetime.date | None = None,
) -> list[dict[str, Any]]:
    return compute_heatmap(history, days=days, end_date=end_date)
