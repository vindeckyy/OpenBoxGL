"""Achievement Trail — offline RetroAchievements analytics & derived cache (ADR 0110)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import time
from typing import Any

from api_errors import RaNotLinked, TrailGameUnknown
from backend_io import atomic_write_text
from pkg.platform_compat import parse_timestamp
import retroachievements
from retroachievements import cached as ra_cached, game_progress, match_game

TRAIL_CACHE_NAME = "trail.json"
TRAIL_CACHE_FORMAT = 1
INLINE_REBUILD_THRESHOLD = 250
MAX_TRAIL_ITEMS = 50
DEFAULT_SPACING_SECONDS = 0.5
VALID_SORTS = frozenset({"closest", "active", "longest", "points"})


def _utc_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def parse_ra_timestamp(raw: Any) -> datetime | None:
    """Parse RA ``YYYY-MM-DD HH:MM:SS`` or ISO-8601 timestamps into aware UTC."""
    text = str(raw or "").strip()
    if not text:
        return None
    if len(text) == 19 and text[10] in (" ", "T"):
        try:
            return datetime.strptime(text.replace("T", " "), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    parsed = parse_timestamp(text)
    if parsed is None:
        return None
    if text.endswith(("Z", "z")) or ("+" in text[10:] or "-" in text[10:]):
        return parsed.astimezone(timezone.utc)
    return parsed.replace(tzinfo=timezone.utc)


def _id_sort_key(raw: Any) -> tuple[int, int, str]:
    text = str(raw or "").strip()
    if text.isdigit():
        return (0, int(text), "")
    return (1, 0, text.casefold())


def _trashed_ids(state: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for entry in state.get("trash") or []:
        if isinstance(entry, str) and entry.strip():
            out.add(entry.strip())
        elif isinstance(entry, dict):
            gid = str(entry.get("game_id") or "").strip()
            if not gid and isinstance(entry.get("game"), dict):
                gid = str(entry["game"].get("game_id") or "").strip()
            if gid:
                out.add(gid)
    return out


def _trailing_12_weeks(now_dt: datetime) -> list[tuple[int, int, str]]:
    """Return 12 chronological ISO week tuples + labels ending on *now_dt* week."""
    monday = (now_dt - timedelta(days=now_dt.weekday())).date()
    weeks: list[tuple[int, int, str]] = []
    for offset in range(11, -1, -1):
        wk_day = monday - timedelta(weeks=offset)
        iso = wk_day.isocalendar()
        weeks.append((iso.year, iso.week, f"{iso.year}-W{iso.week:02d}"))
    return weeks


def _week_key_to_ordinal(year: int, week: int) -> int:
    return int((datetime.fromisocalendar(year, week, 1).date() - datetime(1970, 1, 1).date()).days // 7)


def _compute_week_streak(unlock_dts: list[datetime], now_dt: datetime) -> int:
    if not unlock_dts:
        return 0
    week_ordinals = {_week_key_to_ordinal(dt.isocalendar().year, dt.isocalendar().week) for dt in unlock_dts}
    cur_iso = now_dt.isocalendar()
    cur_ord = _week_key_to_ordinal(cur_iso.year, cur_iso.week)
    anchor = cur_ord if cur_ord in week_ordinals else (cur_ord - 1 if (cur_ord - 1) in week_ordinals else None)
    if anchor is None:
        return 0
    streak = 0
    while anchor in week_ordinals:
        streak += 1
        anchor -= 1
    return streak


def _velocity_and_eta(
    unlock_events: list[tuple[datetime, int]],
    *,
    earned: int,
    total: int,
    has_dates: bool,
    now_dt: datetime,
) -> dict[str, Any]:
    remaining = max(0, total - earned)
    weeks_12 = _trailing_12_weeks(now_dt)
    buckets: dict[tuple[int, int], dict[str, int]] = {(yr, wk): {"count": 0, "points": 0} for yr, wk, _ in weeks_12}

    if has_dates:
        for dt, pts in unlock_events:
            iso = dt.isocalendar()
            key = (iso.year, iso.week)
            if key in buckets:
                buckets[key]["count"] += 1
                buckets[key]["points"] += max(0, int(pts or 0))

    trail_12 = [
        {"week": label, "count": buckets[(yr, wk)]["count"], "points": buckets[(yr, wk)]["points"]}
        for yr, wk, label in weeks_12
    ]
    last_8 = trail_12[-8:] if has_dates else []
    active_weeks = sum(1 for row in trail_12 if row["count"] > 0) if has_dates else 0

    if total > 0 and remaining == 0:
        return {
            "weekly_velocity": last_8,
            "active_weeks": active_weeks,
            "eta_days": 0,
            "eta_confidence": "complete",
            "eta_reason": "complete",
        }
    if not has_dates:
        return {
            "weekly_velocity": [],
            "active_weeks": 0,
            "eta_days": None,
            "eta_confidence": "low",
            "eta_reason": "no_dates",
        }

    nonzero_8 = [row["count"] for row in trail_12[-8:] if row["count"] > 0]
    nonzero_12 = [row["count"] for row in trail_12 if row["count"] > 0]
    sample = nonzero_8 if len(nonzero_8) >= 4 else nonzero_12
    weekly_rate = (sum(sample) / len(sample)) if sample else 0.0

    if active_weeks >= 8:
        confidence = "high"
    elif active_weeks >= 4:
        confidence = "medium"
    else:
        confidence = "low"

    if confidence in ("high", "medium") and weekly_rate > 0 and remaining > 0:
        eta_days: int | None = min(3650, math.ceil((remaining / weekly_rate) * 7))
        reason = "forecast"
    else:
        eta_days = None
        reason = "zero_rate" if weekly_rate <= 0 else "low_activity"

    return {
        "weekly_velocity": last_8,
        "active_weeks": active_weeks,
        "eta_days": eta_days,
        "eta_confidence": confidence,
        "eta_reason": reason,
    }


def compute_game_trail(
    game: dict[str, Any],
    ra_cache: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compute pure Achievement Trail analytics for one cached game."""
    now_dt = _utc_now(now)
    achievements = [a for a in (ra_cache.get("achievements") or []) if isinstance(a, dict)]
    earned_count = int(ra_cache.get("earned") or sum(1 for a in achievements if a.get("earned")))
    earned_hardcore = int(ra_cache.get("earned_hardcore") or sum(1 for a in achievements if a.get("hardcore")))
    total_count = int(ra_cache.get("total") or len(achievements))
    mastered = bool(ra_cache.get("mastered")) or (total_count > 0 and earned_hardcore >= total_count)
    remaining = max(0, total_count - earned_count)

    unlock_events: list[tuple[datetime, int]] = []
    earned_points = 0
    total_points = 0
    unearned: list[dict[str, Any]] = []

    for ach in achievements:
        pts_raw = ach.get("points")
        pts = int(pts_raw) if isinstance(pts_raw, (int, float)) and not isinstance(pts_raw, bool) else 0
        total_points += max(0, pts)
        if ach.get("earned"):
            earned_points += max(0, pts)
            dt = parse_ra_timestamp(ach.get("date_earned_hardcore")) or parse_ra_timestamp(ach.get("date_earned"))
            if dt is not None:
                unlock_events.append((dt, max(0, pts)))
        else:
            unearned.append({
                "id": ach.get("id"),
                "title": str(ach.get("title") or ""),
                "description": str(ach.get("description") or ""),
                "points": pts_raw if isinstance(pts_raw, int) and pts_raw > 0 else 0,
                "badge": str(ach.get("badge") or ""),
            })

    has_dates = earned_count == 0 or len(unlock_events) > 0
    vel = _velocity_and_eta(
        unlock_events,
        earned=earned_count,
        total=total_count,
        has_dates=has_dates,
        now_dt=now_dt,
    )
    streak_weeks = _compute_week_streak([dt for dt, _ in unlock_events], now_dt) if has_dates else 0
    last_unlock_at = max((dt for dt, _ in unlock_events), default=None)

    unearned.sort(
        key=lambda item: (
            item["points"] <= 0,
            item["points"],
            _id_sort_key(item.get("id")),
            item["title"].casefold(),
        )
    )
    next_target_pts = ((earned_points // 100) + 1) * 100
    progress_pct = round(earned_count * 100.0 / total_count, 1) if total_count > 0 else 0.0

    return {
        "game_id": str(game.get("game_id") or ""),
        "name": str(game.get("name") or ""),
        "platform": str(game.get("platform") or ""),
        "ra_game_id": str(game.get("ra_game_id") or ra_cache.get("game_id") or ""),
        "earned": earned_count,
        "earned_hardcore": earned_hardcore,
        "total": total_count,
        "remaining": remaining,
        "progress_pct": progress_pct,
        "mastered": mastered,
        "earned_points": earned_points,
        "total_points": total_points,
        "has_dates": has_dates,
        "weekly_velocity": vel["weekly_velocity"],
        "active_weeks": vel["active_weeks"],
        "eta_days": vel["eta_days"],
        "eta_confidence": vel["eta_confidence"],
        "eta_reason": vel["eta_reason"],
        "streak_weeks": streak_weeks,
        "last_unlock_at": last_unlock_at.strftime("%Y-%m-%dT%H:%M:%SZ") if last_unlock_at else "",
        "next_up": unearned[:10],
        "next_milestone": {
            "points": next_target_pts,
            "remaining": max(0, next_target_pts - earned_points),
        },
    }


def _iter_ra_cache_files(data_dir: Path) -> list[Path]:
    ra_dir = data_dir / "cache" / "retroachievements"
    if not ra_dir.is_dir():
        return []
    files: list[Path] = []
    for path in sorted(ra_dir.glob("*.json")):
        if not path.is_file():
            continue
        if path.name == "systems.json" or path.name.startswith("system-"):
            continue
        files.append(path)
    return files


def ra_cache_signature(data_dir: Path | str, state_sig: str = "") -> list[Any]:
    """Composite invalidation signature matching ADR 0101 + ADR 0110."""
    base = Path(data_dir)
    ra_dir = base / "cache" / "retroachievements"
    try:
        dir_mtime = ra_dir.stat().st_mtime_ns if ra_dir.is_dir() else 0
    except OSError:
        dir_mtime = 0
    files = _iter_ra_cache_files(base)
    max_mtime = 0
    for f in files:
        try:
            mt = f.stat().st_mtime_ns
            if mt > max_mtime:
                max_mtime = mt
        except OSError:
            continue
    return [str(state_sig or ""), int(dir_mtime), len(files), int(max_mtime)]


def compute_trail_index(
    state: dict[str, Any],
    data_dir: Path | str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build library-wide and per-game Achievement Trail analytics from disk caches."""
    now_dt = _utc_now(now)
    base = Path(data_dir)
    trashed = _trashed_ids(state)
    games = state.get("games") or []
    game_by_id: dict[str, dict[str, Any]] = {}
    for g in games:
        if not isinstance(g, dict):
            continue
        gid = str(g.get("game_id") or "").strip()
        if not gid or g.get("hidden") or gid in trashed:
            continue
        game_by_id[gid] = g

    items: dict[str, dict[str, Any]] = {}
    all_events: list[tuple[datetime, int]] = []
    total_earned = 0
    total_ach = 0
    total_pts = 0
    mastered_games = 0
    any_earned_missing_dates = False
    any_has_dates = False

    for cache_path in _iter_ra_cache_files(base):
        gid = cache_path.stem
        game = game_by_id.get(gid)
        if game is None:
            continue
        try:
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        entry = compute_game_trail(game, raw, now=now_dt)
        items[gid] = entry
        total_earned += entry["earned"]
        total_ach += entry["total"]
        total_pts += entry["earned_points"]
        if entry["mastered"]:
            mastered_games += 1
        if entry["earned"] > 0 and not entry["has_dates"]:
            any_earned_missing_dates = True
        if entry["has_dates"] and entry["earned"] > 0:
            any_has_dates = True
        for ach in raw.get("achievements") or []:
            if isinstance(ach, dict) and ach.get("earned"):
                dt = parse_ra_timestamp(ach.get("date_earned_hardcore")) or parse_ra_timestamp(ach.get("date_earned"))
                if dt is not None:
                    pts = int(ach.get("points") or 0) if isinstance(ach.get("points"), (int, float)) else 0
                    all_events.append((dt, max(0, pts)))

    lib_has_dates = (total_earned == 0) or any_has_dates
    vel = _velocity_and_eta(
        all_events,
        earned=total_earned,
        total=total_ach,
        has_dates=lib_has_dates and not (total_earned > 0 and not any_has_dates),
        now_dt=now_dt,
    )
    streak_weeks = _compute_week_streak([dt for dt, _ in all_events], now_dt) if lib_has_dates else 0
    next_target_pts = ((total_pts // 100) + 1) * 100

    return {
        "format": TRAIL_CACHE_FORMAT,
        "summary": {
            "games_count": len(items),
            "mastered_games": mastered_games,
            "earned_achievements": total_earned,
            "total_achievements": total_ach,
            "earned_points": total_pts,
            "has_dates": lib_has_dates and not (any_earned_missing_dates and not any_has_dates),
            "stale_dates_notice": any_earned_missing_dates,
            "weekly_velocity": vel["weekly_velocity"],
            "streak_weeks": streak_weeks,
            "next_milestone": {
                "points": next_target_pts,
                "remaining": max(0, next_target_pts - total_pts),
            },
        },
        "games": items,
    }


def _trail_cache_path(data_dir: Path) -> Path:
    return data_dir / "cache" / TRAIL_CACHE_NAME


def _write_trail_cache(path: Path, sig: list[Any], index: dict[str, Any]) -> dict[str, Any]:
    payload = {"format": TRAIL_CACHE_FORMAT, "signature": sig, **index}
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False), mode=0o600)
    return payload


def _read_trail_cache(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    if not isinstance(raw, dict) or raw.get("format") != TRAIL_CACHE_FORMAT or not isinstance(raw.get("games"), dict):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return raw


def rebuild_trail_cache(
    state: dict[str, Any],
    data_dir: Path | str,
    state_sig: str = "",
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Recompute and atomically persist ``cache/trail.json``."""
    base = Path(data_dir)
    sig = ra_cache_signature(base, state_sig)
    index = compute_trail_index(state, base, now=now)
    return _write_trail_cache(_trail_cache_path(base), sig, index)


def _sort_items(items: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    key = sort if sort in VALID_SORTS else "closest"
    if key == "active":
        return sorted(items, key=lambda x: (x.get("last_unlock_at") or "", x.get("earned", 0), x.get("name", "")), reverse=True)
    if key == "longest":
        return sorted(items, key=lambda x: (-x.get("remaining", 0), -x.get("total_points", 0), x.get("name", "").casefold()))
    if key == "points":
        return sorted(items, key=lambda x: (-x.get("earned_points", 0), -x.get("total_points", 0), x.get("name", "").casefold()))
    return sorted(
        items,
        key=lambda x: (
            1 if x.get("remaining", 0) == 0 else 0,
            -x.get("progress_pct", 0.0),
            x.get("remaining", 0),
            x.get("name", "").casefold(),
        ),
    )


def get_trail(
    state: dict[str, Any],
    data_dir: Path | str,
    state_sig: str = "",
    *,
    sort: str = "closest",
    offset: int = 0,
    limit: int = MAX_TRAIL_ITEMS,
    now: datetime | None = None,
    rebuild_cb=None,
) -> dict[str, Any]:
    """Serve Achievement Trail cache offline with ADR 0101 stale-while-rebuild."""
    base = Path(data_dir)
    sig = ra_cache_signature(base, state_sig)
    file_count = int(sig[2])
    cache_path = _trail_cache_path(base)
    cached_data = _read_trail_cache(cache_path)
    stale = False

    if cached_data is None or cached_data.get("signature") != sig:
        if cached_data is not None and file_count > INLINE_REBUILD_THRESHOLD and rebuild_cb is not None:
            stale = True
            rebuild_cb()
        else:
            cached_data = rebuild_trail_cache(state, base, state_sig, now=now)

    rows = list((cached_data.get("games") or {}).values())
    sorted_rows = _sort_items(rows, sort)
    safe_offset = max(0, int(offset))
    safe_limit = max(1, min(200, int(limit)))
    return {
        "available": True,
        "summary": cached_data.get("summary") or {},
        "items": sorted_rows[safe_offset : safe_offset + safe_limit],
        "total": len(sorted_rows),
        "offset": safe_offset,
        "limit": safe_limit,
        "sort": sort if sort in VALID_SORTS else "closest",
        "stale": stale,
    }


def get_trail_game(
    state: dict[str, Any],
    data_dir: Path | str,
    game_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return Achievement Trail metrics for one game, or raise unknown/not-linked errors."""
    gid = str(game_id or "").strip()
    trashed = _trashed_ids(state)
    game = next(
        (
            g for g in state.get("games") or []
            if isinstance(g, dict) and str(g.get("game_id") or "") == gid and gid not in trashed
        ),
        None,
    )
    if game is None:
        raise TrailGameUnknown(f"Game not found in library: {gid}")

    base = Path(data_dir)
    cache_path = base / "cache" / "retroachievements" / f"{gid}.json"
    has_ra_id = bool(str(game.get("ra_game_id") or "").strip())
    if not cache_path.is_file() and not has_ra_id:
        raise RaNotLinked("This game is not linked to RetroAchievements yet.")

    raw: dict[str, Any] = {}
    if cache_path.is_file():
        try:
            loaded = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except (OSError, ValueError):
            raw = {}

    return {"available": True, "game": compute_game_trail(game, raw, now=now)}


def refresh_trail_progress(
    data_dir: Path | str,
    state: dict[str, Any],
    credentials: dict[str, Any],
    *,
    game_ids: list[str] | None = None,
    fetch=None,
    cancel_event=None,
    progress_cb=None,
    spacing_seconds: float = DEFAULT_SPACING_SECONDS,
    max_age: int = 0,
) -> dict[str, Any]:
    """Bounded explicit job refreshing RA progress caches via ``retroachievements.cached``."""
    fetch_fn = fetch if fetch is not None else retroachievements.api_get
    base = Path(data_dir)
    ra_dir = base / "cache" / "retroachievements"
    ra_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    trashed = _trashed_ids(state)
    wanted = {str(x).strip() for x in (game_ids or []) if str(x).strip()}

    targets: list[dict[str, Any]] = []
    for g in state.get("games") or []:
        if not isinstance(g, dict) or g.get("hidden"):
            continue
        gid = str(g.get("game_id") or "").strip()
        if not gid or gid in trashed:
            continue
        if wanted and gid not in wanted:
            continue
        cache_exists = (ra_dir / f"{gid}.json").is_file()
        if g.get("ra_game_id") or cache_exists:
            targets.append(g)

    refreshed = 0
    errors: list[str] = []
    total = len(targets)

    for idx, game in enumerate(targets, start=1):
        if cancel_event is not None and cancel_event.is_set():
            break
        gid = str(game.get("game_id") or "")
        cache_path = ra_dir / f"{gid}.json"
        try:
            if str(game.get("ra_game_id") or "").isdigit():
                ra_id = int(game["ra_game_id"])
            else:
                ra_id, _digest = match_game(game, credentials, ra_dir, fetch=fetch_fn)

            def _fetch_one(rid=ra_id, creds=credentials):
                progress = game_progress(rid, creds, fetch=fetch_fn)
                earned = int(progress.get("earned") or 0)
                total_ach = int(progress.get("total") or 0)
                return {
                    "game_id": rid,
                    "earned": earned,
                    "earned_hardcore": int(progress.get("earned_hardcore") or 0),
                    "total": total_ach,
                    "progress_pct": round(earned * 100.0 / total_ach, 1) if total_ach > 0 else 0.0,
                    "mastered": bool(progress.get("mastered", False)),
                    "beaten": int(progress.get("beaten") or 0),
                    "achievements": (progress.get("achievements") or [])[:500],
                }

            ra_cached(cache_path, _fetch_one, max_age=max_age)
            refreshed += 1
        except Exception as error:
            errors.append(f"{game.get('name', gid)}: {error}")
        if progress_cb is not None:
            progress_cb(idx, total)
        if idx < total and spacing_seconds > 0:
            if cancel_event is not None:
                if cancel_event.wait(spacing_seconds):
                    break
            else:
                time.sleep(spacing_seconds)

    return {"refreshed": refreshed, "total": total, "errors": errors[:20]}


__all__ = [
    "INLINE_REBUILD_THRESHOLD",
    "compute_game_trail",
    "compute_trail_index",
    "get_trail",
    "get_trail_game",
    "parse_ra_timestamp",
    "ra_cache_signature",
    "rebuild_trail_cache",
    "refresh_trail_progress",
]
