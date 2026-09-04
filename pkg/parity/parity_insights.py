"""Play Insights aggregation for OpenBox 1.7.1.

Local-first aggregation over library.json sessions/history + games.
No new storage, no telemetry. Pure functions for heatmap, streaks,
top platforms/genres, momentum.
"""

from __future__ import annotations

import datetime
import json
from collections import Counter
from pathlib import Path
from typing import Any


def _basename(value: Any) -> str:
    if not value:
        return ""
    # Basenames only (privacy): handle posix + windows separators explicitly
    # since Path().name on linux keeps backslashes intact.
    text = str(value).replace("\\", "/")
    return text.split("/")[-1] if text else ""


def _in_year(date_value: Any, year: int) -> bool:
    d = _parse_date(date_value)
    return d is not None and d.year == year


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


def compute_top_games(games: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    items = []
    for game in games if isinstance(games, list) else []:
        if not isinstance(game, dict):
            continue
        try:
            pt = int(game.get("playtime_seconds", 0) or 0)
        except (TypeError, ValueError):
            pt = 0
        pt = max(0, pt)
        if pt <= 0:
            continue
        try:
            plays = int(game.get("play_count", 0) or 0)
        except (TypeError, ValueError):
            plays = 0
        items.append(
            {
                "game_id": str(game.get("game_id", "") or ""),
                "name": str(game.get("name", "") or ""),
                "platform": str(game.get("platform", "") or ""),
                "playtime_seconds": pt,
                "play_count": plays,
                "last_played": str(game.get("last_played", "") or ""),
            }
        )
    items.sort(key=lambda x: (-x["playtime_seconds"], -x["play_count"], x["name"]))
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


def summarize(
    state: dict[str, Any],
    end_date: datetime.date | None = None,
    days: int = 366,
) -> dict[str, Any]:
    games = state.get("games", []) if isinstance(state, dict) else []
    history = state.get("history", []) if isinstance(state, dict) else []
    if not isinstance(games, list):
        games = []
    if not isinstance(history, list):
        history = []
    if not isinstance(days, int) or not 1 <= days <= 366:
        days = 366
    heatmap = compute_heatmap(history, days=days, end_date=end_date)
    streak = compute_streak(heatmap)
    totals = compute_totals(games, history)
    top_platforms = compute_top_platforms(games, limit=8)
    top_genres = compute_top_genres(games, limit=8)
    top_games = compute_top_games(games, limit=10)
    momentum = compute_momentum(heatmap)
    last_30_days = heatmap[-30:]
    return {
        "heatmap": heatmap,
        "days": days,
        "streak": streak,
        "totals": totals,
        "top_platforms": top_platforms,
        "top_genres": top_genres,
        "top_games": top_games,
        "momentum": momentum,
        "last_30_days": last_30_days,
    }


def heatmap_for_range(
    history: list[dict[str, Any]],
    days: int = 366,
    end_date: datetime.date | None = None,
) -> list[dict[str, Any]]:
    return compute_heatmap(history, days=days, end_date=end_date)


def _load_ra_cache(ra_cache_dir: str) -> dict[str, dict[str, Any]]:
    """Load RA game caches keyed by game_id. Returns empty on missing dir."""
    by_game: dict[str, dict[str, Any]] = {}
    if not ra_cache_dir:
        return by_game
    base = Path(ra_cache_dir)
    if not base.is_dir():
        return by_game
    for path in base.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        game_id = str(data.get("game_id") or "").strip()
        if game_id:
            by_game[game_id] = data
    return by_game


def mastery_summary(games, ra_cache_dir=None):
    """Platform and overall completionist breakdown.

    Uses local `progress` and optional RetroAchievements cache only.
    """
    states = ("never", "played", "beaten", "completed", "mastered")
    ra_cache = _load_ra_cache(ra_cache_dir or "")

    def bucket(items):
        total = len(items)
        counts: dict[str, int] = {s: 0 for s in states}
        ra_tracked = 0
        ra_mastered = 0
        ra_progress_total = 0
        for g in items:
            prog = str(g.get("progress", "") or "").lower()
            if "mastered" in prog:
                counts["mastered"] += 1
            elif "completed" in prog:
                counts["completed"] += 1
            elif "beaten" in prog:
                counts["beaten"] += 1
            elif int(g.get("playtime_seconds") or 0) or int(g.get("play_count") or 0):
                counts["played"] += 1
            else:
                counts["never"] += 1
            game_id = str(g.get("game_id") or "")
            ra = ra_cache.get(game_id) or {}
            if ra.get("game_id"):
                ra_tracked += 1
                if ra.get("mastered"):
                    ra_mastered += 1
                ra_progress_total += float(ra.get("progress_pct", 0) or 0)
        return {
            **{s: counts[s] for s in states},
            "ra_tracked": ra_tracked,
            "ra_mastered": ra_mastered,
            "ra_avg_progress": round(ra_progress_total / ra_tracked, 2) if ra_tracked else 0.0,
            "total": total,
        }

    # Per platform
    by_platform: dict[str, list[dict[str, Any]]] = {}
    for g in games:
        if not isinstance(g, dict):
            continue
        platform = str(g.get("platform", "") or "Unspecified").strip() or "Unspecified"
        by_platform.setdefault(platform, []).append(g)

    platforms = {p: bucket(items) for p, items in by_platform.items()}
    overall = bucket([g for g in games if isinstance(g, dict)])
    ra_available = bool(ra_cache)
    overall["ra_available"] = ra_available

    # Decades
    decade_buckets: dict[str, list[dict[str, Any]]] = {}
    for g in games:
        if not isinstance(g, dict):
            continue
        try:
            year = int(g.get("year", 0) or 0)
        except (TypeError, ValueError):
            year = 0
        if year < 1980:
            key = "pre-1980"
        else:
            key = f"{year // 10 * 10}s"
        decade_buckets.setdefault(key, []).append(g)
    decades = {d: bucket(items) for d, items in decade_buckets.items()}

    return {"platforms": platforms, "overall": overall, "decades": decades, "ra_available": ra_available}


def _wrapped_year_range(year: int) -> tuple[datetime.date, datetime.date]:
    """Inclusive start/end for a year; handles leap years."""
    start = datetime.date(year, 1, 1)
    end = datetime.date(year, 12, 31)
    return start, end


def wrapped_summary(state: dict[str, Any], year: int) -> dict[str, Any]:
    """Generate an annual gaming summary from local state.

    Returns only game names, covers, and aggregates — no paths, settings,
    or credentials. Privacy invariant is enforced by construction.
    """
    games = state.get("games", []) if isinstance(state, dict) else []
    history = state.get("history", []) if isinstance(state, dict) else []
    if not isinstance(games, list):
        games = []
    if not isinstance(history, list):
        history = []

    game_by_id = {str(g.get("game_id", "")): g for g in games if isinstance(g, dict)}
    in_year = [e for e in history if isinstance(e, dict) and _in_year(e.get("started"), year)]

    played_ids: set[str] = set()
    new_games = 0
    playtime = 0
    sessions = 0
    month_seconds = [0] * 12
    weekday_seconds: Counter[int] = Counter()
    first: dict[str, Any] | None = None
    oldest_year: int | None = None
    oldest_game: dict[str, Any] | None = None

    progress_fields = ("beaten", "completed", "mastered")
    progress: dict[str, int] = {k: 0 for k in progress_fields}

    for entry in in_year:
        game_id = str(entry.get("game_id", ""))
        game = game_by_id.get(game_id) or {}
        started = entry.get("started", "")
        d = _parse_date(started)
        if d is None:
            continue
        seconds = max(0, int(entry.get("seconds", 0) or 0))
        playtime += seconds
        sessions += 1
        played_ids.add(game_id)
        month_seconds[d.month - 1] += seconds
        weekday_seconds[d.weekday()] += seconds

        if first is None or (d < _parse_date(first["date"])):
            first = {"date": started, "game_id": game_id, "name": str(game.get("name", ""))}

        added = _parse_date(game.get("added_at") or "")
        if added and added.year == year:
            new_games += 1

        gy = game.get("year")
        if gy:
            try:
                gy_int = int(gy)
            except (TypeError, ValueError):
                gy_int = None
            if gy_int and (oldest_year is None or gy_int < oldest_year):
                oldest_year = gy_int
                oldest_game = {"game_id": game_id, "name": str(game.get("name", "")), "year": gy_int}

        note = str(entry.get("note", "") or "").lower()
        for field in progress_fields:
            if field in note:
                progress[field] += 1

    if not in_year:
        for game in games:
            if not isinstance(game, dict):
                continue
            p = str(game.get("progress", "") or "").lower()
            for field in progress_fields:
                if field in p:
                    progress[field] += 1

    year_games = [game_by_id[gid] for gid in played_ids if game_by_id.get(gid)]
    top_game = compute_top_games(year_games, limit=1)
    top_platform = compute_top_platforms(year_games, limit=1)
    top_genre = compute_top_genres(year_games, limit=1)

    start, end = _wrapped_year_range(year)
    in_year_heatmap = compute_heatmap(in_year, days=(end - start).days + 1, end_date=end)
    streak = compute_streak(in_year_heatmap)

    by_day: dict[datetime.date, list[str]] = {}
    for entry in in_year:
        d = _parse_date(entry.get("started"))
        if d is None:
            continue
        gid = str(entry.get("game_id", ""))
        by_day.setdefault(d, []).append(gid)
    pairs = set()
    for gids in by_day.values():
        local = sorted(set(gids))
        for i in range(len(local)):
            for j in range(i + 1, len(local)):
                pairs.add((local[i], local[j]))

    busiest_month = None
    if any(month_seconds):
        m_idx = max(range(12), key=lambda i: month_seconds[i])
        busiest_month = {"month": m_idx + 1, "seconds": month_seconds[m_idx]}

    busiest_weekday = None
    if weekday_seconds:
        wd = weekday_seconds.most_common(1)[0][0]
        names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        busiest_weekday = {"weekday": names[wd], "seconds": weekday_seconds[wd]}

    return {
        "year": year,
        "totals": {
            "playtime_seconds": playtime,
            "sessions": sessions,
            "games_played": len(played_ids),
            "new_games": new_games,
        },
        "progress": progress,
        "streak": {"longest": streak["longest"]},
        "top": {
            "game": top_game[0] if top_game else None,
            "platform": top_platform[0] if top_platform else None,
            "genre": top_genre[0] if top_genre else None,
        },
        "oldest_played": oldest_game,
        "first_play": first,
        "busiest_month": busiest_month,
        "busiest_weekday": busiest_weekday,
        "per_month": month_seconds,
        "co_play_pairs": len(pairs),
    }


def timeline_groups(state: dict[str, Any], days: int = 90) -> dict[str, Any]:
    """Group history entries by date desc for a timeline UI.

    Exposes only names, cover bool, durations, and basenames for recordings.
    Never exposes filesystem paths.
    """
    games = state.get("games", []) if isinstance(state, dict) else []
    history = state.get("history", []) if isinstance(state, dict) else []
    if not isinstance(games, list):
        games = []
    if not isinstance(history, list):
        history = []

    game_by_id = {str(g.get("game_id", "")): g for g in games if isinstance(g, dict)}
    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=days - 1)

    by_date: dict[datetime.date, list[dict[str, Any]]] = {}
    for entry in history:
        if not isinstance(entry, dict):
            continue
        d = _parse_date(entry.get("started"))
        if d is None or d < start_date or d > end_date:
            continue
        game_id = str(entry.get("game_id", ""))
        game = game_by_id.get(game_id) or {}
        seconds = max(0, int(entry.get("seconds", 0) or 0))
        recording = _basename(entry.get("recording"))
        started = str(entry.get("started", "") or "")
        ended = str(entry.get("ended", "") or "")
        by_date.setdefault(d, []).append({
            "game_id": game_id,
            "name": str(game.get("name", "")),
            "cover": bool(game.get("has_cover")),
            "seconds": seconds,
            "started": started,
            "ended": ended,
            "recording": recording,
        })

    groups = []
    for d in sorted(by_date, reverse=True):
        groups.append({
            "date": d.isoformat(),
            "entries": sorted(by_date[d], key=lambda x: x.get("started", ""), reverse=True),
        })

    return {"days": days, "groups": groups}
