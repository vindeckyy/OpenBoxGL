"""Backlog Itinerary — dated multi-week backlog planner (ADR 0111)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
import secrets
from statistics import median
from typing import Any

from api_errors import BadRequest, ItineraryInvalidStrategy, ItineraryNoGames
from backend_io import atomic_write_text
from catalog import total_playtime_seconds
from pkg.parity import parity_igdb
from pkg.parity.parity_radio import _eligible, _is_completed, _norm
from pkg.platform_compat import parse_timestamp

VALID_STRATEGIES = ("quick-wins", "comeback", "raided")
DEFAULT_EST_HOURS = 8.0
MIN_EST_HOURS = 0.5
MIN_OWN_HISTORY_SAMPLES = 3
IGDB_TTL_SECONDS = 30 * 86400
IGDB_CONCURRENCY = 2
HORIZON_DAYS_MIN = 7
HORIZON_DAYS_MAX = 365
MINUTES_PER_DAY_MIN = 5
MINUTES_PER_DAY_MAX = 960
DONE_PLAYTIME_RATIO = 1.15


def _utc_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _today(now: datetime | None = None) -> date:
    return _utc_now(now).date()


def clamp_horizon_days(raw: Any, default: int = 30) -> int:
    if raw is None or raw == "":
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return max(HORIZON_DAYS_MIN, min(HORIZON_DAYS_MAX, val))


def clamp_minutes_per_day(raw: Any, default: int = 60) -> int:
    if raw is None or raw == "":
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return max(MINUTES_PER_DAY_MIN, min(MINUTES_PER_DAY_MAX, val))


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


def is_eligible_for_itinerary(
    game: dict[str, Any],
    state: dict[str, Any],
    *,
    platforms: list[str] | None = None,
    trashed: set[str] | None = None,
) -> bool:
    """Honor parity_radio eligibility, ``progress_values``, ``Abandoned``, and ``trash``."""
    if not _eligible(game):
        return False
    gid = str(game.get("game_id") or "").strip()
    trashed_set = trashed if trashed is not None else _trashed_ids(state)
    if not gid or gid in trashed_set:
        return False
    prog = _norm(game.get("progress"))
    if "abandoned" in prog:
        return False
    settings = state.get("settings") if isinstance(state.get("settings"), dict) else {}
    raw_allowed = settings.get("progress_values")
    if isinstance(raw_allowed, list) and raw_allowed and prog:
        allowed_set = {_norm(val) for val in raw_allowed if str(val or "").strip()}
        if allowed_set and prog not in allowed_set:
            return False
    if platforms:
        scope = {str(p or "").strip().casefold() for p in platforms if str(p or "").strip()}
        if scope and str(game.get("platform") or "").strip().casefold() not in scope:
            return False
    return True


def build_own_history_medians(games: list[dict[str, Any]]) -> tuple[dict[str, float], float | None]:
    """Compute per-platform (>=3 samples) and global (>=3 samples) completed-game medians."""
    by_plat: dict[str, list[float]] = {}
    all_hours: list[float] = []
    for g in games:
        if not isinstance(g, dict) or g.get("hidden") or not _is_completed(g):
            continue
        secs = total_playtime_seconds(g)
        if secs <= 0:
            continue
        hours = secs / 3600.0
        plat = str(g.get("platform") or "").strip().casefold()
        all_hours.append(hours)
        if plat:
            by_plat.setdefault(plat, []).append(hours)

    plat_medians = {
        plat: float(median(vals))
        for plat, vals in by_plat.items()
        if len(vals) >= MIN_OWN_HISTORY_SAMPLES
    }
    global_med = float(median(all_hours)) if len(all_hours) >= MIN_OWN_HISTORY_SAMPLES else None
    return plat_medians, global_med


def load_ra_progress_map(data_dir: Path | str | None) -> dict[str, dict[str, Any]]:
    if not data_dir:
        return {}
    ra_dir = Path(data_dir) / "cache" / "retroachievements"
    if not ra_dir.is_dir():
        return {}
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(ra_dir.glob("*.json")):
        if not path.is_file() or path.name == "systems.json" or path.name.startswith("system-"):
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(raw, dict) and int(raw.get("total") or 0) > 0:
            total = int(raw.get("total") or 0)
            earned = int(raw.get("earned") or 0)
            pct = round(earned * 100.0 / total, 1)
            result[path.stem] = {
                "earned": earned,
                "total": total,
                "progress_pct": pct,
                "mastered": bool(raw.get("mastered")),
            }
    return result


def _igdb_cache_path(data_dir: Path, game_id: str) -> Path:
    return data_dir / "cache" / "igdb" / f"{game_id}.json"


def read_igdb_cached_hours(
    data_dir: Path | str | None,
    game_id: str,
    *,
    now: datetime | None = None,
) -> tuple[bool, float | None]:
    """Return ``(cache_hit, hours_or_none)`` within the 30-day TTL."""
    if not data_dir or not game_id:
        return False, None
    path = _igdb_cache_path(Path(data_dir), game_id)
    if not path.is_file():
        return False, None
    try:
        age = _utc_now(now).timestamp() - path.stat().st_mtime
        if age >= IGDB_TTL_SECONDS:
            return False, None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, None
    if not isinstance(raw, dict):
        return False, None
    hours = raw.get("time_to_beat_hours")
    if isinstance(hours, (int, float)) and not isinstance(hours, bool) and hours > 0:
        return True, float(hours)
    return True, None


def _write_igdb_cache(data_dir: Path, game_id: str, hours: float | None, *, now: datetime | None = None) -> None:
    path = _igdb_cache_path(data_dir, game_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "game_id": game_id,
        "time_to_beat_hours": float(hours) if isinstance(hours, (int, float)) and hours > 0 else None,
        "cached_at": _utc_now(now).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False), mode=0o600)


def missing_igdb_games(
    data_dir: Path | str,
    games: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    base = Path(data_dir)
    need: list[dict[str, Any]] = []
    for g in games:
        stored = g.get("time_to_beat_hours")
        if isinstance(stored, (int, float)) and not isinstance(stored, bool) and stored > 0:
            continue
        gid = str(g.get("game_id") or "").strip()
        hit, _ = read_igdb_cached_hours(base, gid, now=now)
        if not hit:
            need.append(g)
    return need


def fetch_missing_igdb_estimates(
    data_dir: Path | str,
    games: list[dict[str, Any]],
    *,
    cancel_event=None,
    progress_cb=None,
    search_fn=None,
    fetch_ttb_fn=None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fetch missing time-to-beat estimates via IGDB with concurrency <= 2."""
    creds = parity_igdb.credentials()
    if not creds:
        return {"fetched": 0, "skipped_no_creds": True, "errors": []}

    base = Path(data_dir)
    targets = missing_igdb_games(base, games, now=now)
    if not targets:
        return {"fetched": 0, "skipped_no_creds": False, "errors": []}

    search = search_fn if search_fn is not None else parity_igdb.search_games
    fetch_ttb = fetch_ttb_fn if fetch_ttb_fn is not None else parity_igdb._fetch_time_to_beat

    def _one(game: dict[str, Any]) -> tuple[str, float | None, str | None]:
        gid = str(game.get("game_id") or "")
        if cancel_event is not None and cancel_event.is_set():
            return gid, None, None
        try:
            igdb_id = game.get("igdb_id")
            if not igdb_id:
                hits = search(str(game.get("name") or ""), str(game.get("platform") or ""), limit=1)
                if hits:
                    igdb_id = hits[0].get("igdb_id")
            hours = fetch_ttb(int(igdb_id)) if igdb_id else None
            _write_igdb_cache(base, gid, hours, now=now)
            return gid, hours, None
        except Exception as error:
            return gid, None, str(error)[:160]

    fetched = 0
    errors: list[str] = []
    total = len(targets)
    with ThreadPoolExecutor(max_workers=IGDB_CONCURRENCY) as pool:
        for idx, (_gid, hours, err) in enumerate(pool.map(_one, targets), start=1):
            if err:
                errors.append(err)
            elif isinstance(hours, (int, float)) and hours > 0:
                fetched += 1
            if progress_cb is not None:
                progress_cb(idx, total)

    return {"fetched": fetched, "skipped_no_creds": False, "errors": errors[:20]}


def estimate_game_hours(
    game: dict[str, Any],
    *,
    data_dir: Path | str | None = None,
    platform_medians: dict[str, float] | None = None,
    global_median: float | None = None,
    ra_info: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compute honest time-to-beat estimate via IGDB -> own_history -> default + RA refinement."""
    gid = str(game.get("game_id") or "").strip()
    plat_key = str(game.get("platform") or "").strip().casefold()

    stored_ttb = game.get("time_to_beat_hours")
    base_hours: float | None = None
    est_source = "default"
    confidence = "low"

    if isinstance(stored_ttb, (int, float)) and not isinstance(stored_ttb, bool) and stored_ttb > 0:
        base_hours = float(stored_ttb)
        est_source = "igdb"
        confidence = "high"
    else:
        hit, cached_hours = read_igdb_cached_hours(data_dir, gid, now=now)
        if hit and cached_hours is not None:
            base_hours = cached_hours
            est_source = "igdb"
            confidence = "high"

    if base_hours is None:
        if platform_medians and plat_key in platform_medians:
            base_hours = float(platform_medians[plat_key])
            est_source = "own_history"
            confidence = "medium"
        elif global_median is not None:
            base_hours = float(global_median)
            est_source = "own_history"
            confidence = "medium"
        else:
            base_hours = DEFAULT_EST_HOURS
            est_source = "default"
            confidence = "low"

    est = max(MIN_EST_HOURS, round(base_hours, 1))
    refined_by_ra = False
    ra_pct = 0.0
    if isinstance(ra_info, dict) and int(ra_info.get("total") or 0) > 0:
        total = int(ra_info["total"])
        earned = int(ra_info.get("earned") or 0)
        ra_pct = round(earned * 100.0 / total, 1)
        if 0 < earned < total:
            est = max(MIN_EST_HOURS, round(base_hours * (1.0 - earned / total), 1))
            refined_by_ra = True

    return {
        "game_id": gid,
        "name": str(game.get("name") or gid),
        "platform": str(game.get("platform") or ""),
        "est_hours": est,
        "base_est_hours": round(base_hours, 1),
        "est_source": est_source,
        "confidence": confidence,
        "refined_by_ra": refined_by_ra,
        "ra_pct": ra_pct,
        "playtime_seconds": max(0, total_playtime_seconds(game)),
    }


def _order_candidates(cands: list[dict[str, Any]], strategy: str) -> tuple[list[dict[str, Any]], str | None]:
    if strategy not in VALID_STRATEGIES:
        raise ItineraryInvalidStrategy(f"Unsupported strategy '{strategy}'. Choose one of: {', '.join(VALID_STRATEGIES)}.")
    if strategy == "comeback":
        return sorted(cands, key=lambda c: (-c["playtime_seconds"], c["est_hours"], c["name"].casefold())), None
    if strategy == "raided":
        has_ra = any(c["ra_pct"] > 0 or c.get("_has_ra") for c in cands)
        if has_ra:
            return sorted(cands, key=lambda c: (-c["ra_pct"], c["est_hours"], c["name"].casefold())), None
        return sorted(cands, key=lambda c: (c["est_hours"], -c["playtime_seconds"], c["name"].casefold())), "quick-wins"
    return sorted(cands, key=lambda c: (c["est_hours"], -c["playtime_seconds"], c["name"].casefold())), None


def build_itinerary(
    state: dict[str, Any],
    *,
    horizon_days: int = 30,
    minutes_per_day: int = 60,
    strategy: str = "quick-wins",
    platforms: list[str] | None = None,
    data_dir: Path | str | None = None,
    pinned_done: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Generate and validate a capacity-bounded multi-week Backlog Itinerary."""
    if strategy not in VALID_STRATEGIES:
        raise ItineraryInvalidStrategy(f"Unsupported strategy '{strategy}'.")

    h_days = clamp_horizon_days(horizon_days)
    m_day = clamp_minutes_per_day(minutes_per_day)
    capacity_hours = round(h_days * m_day / 60.0, 2)
    weekly_hours = max(0.01, (m_day * 7) / 60.0)
    max_weeks = max(1, math.ceil(h_days / 7))

    all_games = [g for g in (state.get("games") or []) if isinstance(g, dict)]
    trashed = _trashed_ids(state)
    plat_medians, global_med = build_own_history_medians(all_games)
    ra_map = load_ra_progress_map(data_dir)

    pinned_list = [dict(e) for e in (pinned_done or [])]
    pinned_ids = {str(e.get("game_id") or "") for e in pinned_list}

    eligible_cands: list[dict[str, Any]] = []
    for g in all_games:
        gid = str(g.get("game_id") or "").strip()
        if gid in pinned_ids:
            continue
        if not is_eligible_for_itinerary(g, state, platforms=platforms, trashed=trashed):
            continue
        est = estimate_game_hours(
            g,
            data_dir=data_dir,
            platform_medians=plat_medians,
            global_median=global_med,
            ra_info=ra_map.get(gid),
            now=now,
        )
        est["_has_ra"] = gid in ra_map
        eligible_cands.append(est)

    if not eligible_cands and not pinned_list:
        raise ItineraryNoGames("No eligible unfinished games match your Backlog Itinerary filters.")

    ordered, fallback = _order_candidates(eligible_cands, strategy)
    entries: list[dict[str, Any]] = []
    later: list[str] = []
    later_hours = 0.0
    cum = 0.0
    breakdown = {"igdb": 0, "own_history": 0, "default": 0}

    for p_entry in pinned_list:
        est_h = float(p_entry.get("est_hours") or MIN_EST_HOURS)
        week = min(max_weeks, int(cum // weekly_hours) + 1)
        clean = {k: v for k, v in p_entry.items() if not k.startswith("_") and k != "live_status"}
        clean["week"] = week
        entries.append(clean)
        cum = round(cum + est_h, 2)
        src = clean.get("est_source", "default")
        if src in breakdown:
            breakdown[src] += 1

    for cand in ordered:
        cand.pop("_has_ra", None)
        est_h = float(cand["est_hours"])
        if cum + est_h <= capacity_hours + 1e-9:
            week = min(max_weeks, int(cum // weekly_hours) + 1)
            row = {k: v for k, v in cand.items() if k != "playtime_seconds"}
            row["week"] = week
            entries.append(row)
            cum = round(cum + est_h, 2)
            src = row["est_source"]
            if src in breakdown:
                breakdown[src] += 1
        else:
            later.append(cand["game_id"])
            later_hours = round(later_hours + est_h, 1)

    plan = {
        "id": f"itin-{secrets.token_hex(6)}",
        "created_at": _utc_now(now).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "horizon_days": h_days,
        "minutes_per_day": m_day,
        "strategy": strategy,
        "strategy_used": fallback or strategy,
        "strategy_fallback": fallback,
        "platforms": [str(p).strip() for p in (platforms or []) if str(p).strip()],
        "capacity_hours": capacity_hours,
        "weekly_hours": round(weekly_hours, 2),
        "planned_hours": cum,
        "entries": entries,
        "later": later,
        "later_hours": later_hours,
        "estimate_breakdown": breakdown,
    }
    return plan


def project_live_itinerary(
    state: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Derive live ``done`` / ``on_track`` / ``behind`` / ``stale`` statuses on read."""
    plan = state.get("itinerary")
    if not isinstance(plan, dict):
        return {"plan": None}

    trashed = _trashed_ids(state)
    game_by_id = {
        str(g.get("game_id") or ""): g
        for g in (state.get("games") or [])
        if isinstance(g, dict) and g.get("game_id")
    }
    now_dt = _utc_now(now)
    created_date = now_dt.date()
    raw_created = str(plan.get("created_at") or "").strip()
    if raw_created:
        parsed_created = parse_timestamp(raw_created)
        if parsed_created is not None:
            created_date = (
                parsed_created.astimezone(timezone.utc).date()
                if raw_created.endswith(("Z", "z")) or "+" in raw_created[10:]
                else parsed_created.date()
            )
    m_day = clamp_minutes_per_day(plan.get("minutes_per_day"), 60)
    elapsed_days = max(1, (now_dt.date() - created_date).days + 1)
    elapsed_capacity = round(elapsed_days * m_day / 60.0, 2)

    cum = 0.0
    projected_entries: list[dict[str, Any]] = []
    done_count = 0
    done_hours = 0.0
    stale_count = 0

    for entry in plan.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        gid = str(entry.get("game_id") or "")
        est_h = float(entry.get("est_hours") or MIN_EST_HOURS)
        cum = round(cum + est_h, 2)
        game = game_by_id.get(gid)
        if game is None or gid in trashed or game.get("hidden"):
            live_status = "stale"
            stale_count += 1
        else:
            played_h = total_playtime_seconds(game) / 3600.0
            abandoned = "abandoned" in _norm(game.get("progress"))
            if _is_completed(game) or abandoned or played_h >= est_h * DONE_PLAYTIME_RATIO:
                live_status = "done"
                done_count += 1
                done_hours = round(done_hours + est_h, 1)
            elif cum <= elapsed_capacity:
                live_status = "behind"
            else:
                live_status = "on_track"
        projected_entries.append({**entry, "live_status": live_status})

    planned_hours = float(plan.get("planned_hours") or cum)
    progress_pct = round(done_hours * 100.0 / planned_hours, 1) if planned_hours > 0 else 0.0
    remaining_hours = max(0.0, planned_hours - done_hours)
    weekly_h = max(0.01, float(plan.get("weekly_hours") or (m_day * 7 / 60.0)))
    projected_finish_week = math.ceil(remaining_hours / weekly_h) if remaining_hours > 0 else 0

    return {
        "plan": {
            **plan,
            "entries": projected_entries,
            "done_count": done_count,
            "done_hours": done_hours,
            "stale_count": stale_count,
            "progress_pct": progress_pct,
            "elapsed_capacity_hours": elapsed_capacity,
            "projected_finish_week": projected_finish_week,
        }
    }


def replan_itinerary(
    state: dict[str, Any],
    payload: dict[str, Any] | None = None,
    *,
    data_dir: Path | str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Pin ``done`` entries, report dropped ``stale`` ids, and rebuild remaining capacity."""
    body = payload if isinstance(payload, dict) else {}
    existing = state.get("itinerary") if isinstance(state.get("itinerary"), dict) else {}

    horizon_days = clamp_horizon_days(body.get("horizon_days"), clamp_horizon_days(existing.get("horizon_days"), 30))
    minutes_per_day = clamp_minutes_per_day(body.get("minutes_per_day"), clamp_minutes_per_day(existing.get("minutes_per_day"), 60))
    strategy = str(body.get("strategy") or existing.get("strategy") or "quick-wins").strip()
    platforms = body.get("platforms") if "platforms" in body else existing.get("platforms")
    if platforms is not None and not isinstance(platforms, list):
        raise BadRequest("platforms must be a list of platform names.")

    live = project_live_itinerary(state, now=now)["plan"]
    pinned_done: list[dict[str, Any]] = []
    dropped_ids: list[str] = []
    if live:
        for entry in live.get("entries") or []:
            if entry.get("live_status") == "done":
                pinned_done.append(entry)
            elif entry.get("live_status") == "stale":
                dropped_ids.append(str(entry.get("game_id") or ""))

    plan = build_itinerary(
        state,
        horizon_days=horizon_days,
        minutes_per_day=minutes_per_day,
        strategy=strategy,
        platforms=platforms,
        data_dir=data_dir,
        pinned_done=pinned_done,
        now=now,
    )
    state["itinerary"] = plan
    projected = project_live_itinerary(state, now=now)["plan"]
    return {"plan": projected, "dropped_ids": dropped_ids}


__all__ = [
    "DEFAULT_EST_HOURS",
    "DONE_PLAYTIME_RATIO",
    "HORIZON_DAYS_MAX",
    "HORIZON_DAYS_MIN",
    "IGDB_CONCURRENCY",
    "IGDB_TTL_SECONDS",
    "MINUTES_PER_DAY_MAX",
    "MINUTES_PER_DAY_MIN",
    "MIN_OWN_HISTORY_SAMPLES",
    "VALID_STRATEGIES",
    "build_itinerary",
    "build_own_history_medians",
    "clamp_horizon_days",
    "clamp_minutes_per_day",
    "estimate_game_hours",
    "fetch_missing_igdb_estimates",
    "is_eligible_for_itinerary",
    "load_ra_progress_map",
    "missing_igdb_games",
    "project_live_itinerary",
    "read_igdb_cached_hours",
    "replan_itinerary",
]
