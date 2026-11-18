"""Player's Log — Time Capsules & Deterministic Milestone Cards (ADR 0110)."""

from __future__ import annotations

from bisect import bisect_left
import calendar
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re
import secrets
from typing import Any

from api_errors import (
    BadRequest,
    CapsuleBodyTooLarge,
    CapsuleInvalidDate,
    CapsuleLocked,
    CapsuleNotFound,
)
from catalog import total_playtime_seconds
from notifications import add_notification
from pkg.parity.parity_insights import compute_streak
from pkg.parity.parity_radio import _is_completed

CAPSULE_TITLE_MAX = 120
CAPSULE_BODY_MAX = 4000
CAPSULE_KINDS = frozenset({"note", "question"})

GAME_PLAYTIME_HOURS = (10, 25, 50, 100, 250, 500, 1000)
TOTAL_PLAYTIME_HOURS = (100, 500, 1000, 2500, 5000, 10000)
SESSION_MILESTONES = (100, 500, 1000)
RA_POINTS_MILESTONES = (100, 200, 500, 1000, 2500)
STREAK_MILESTONES = (7, 30, 100, 365)
LIBRARY_AGE_YEARS = (1, 2, 5, 10)
COLLECTION_MILESTONES = (10, 25, 50, 100)

_STRICT_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def _today(today: date | None = None) -> date:
    if isinstance(today, datetime):
        return today.date()
    return today if isinstance(today, date) else datetime.now().date()


def _utc_iso(now_iso: str | None = None) -> str:
    if now_iso:
        return str(now_iso)
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_unlock_date(raw: Any) -> date:
    text = str(raw or "").strip()
    if not _STRICT_DATE_RE.match(text):
        raise CapsuleInvalidDate("unlock_at must be a valid YYYY-MM-DD local date.")
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        raise CapsuleInvalidDate("unlock_at is not a valid calendar date.") from error


def _parse_date_prefix(raw: Any) -> date | None:
    text = str(raw or "").strip()
    match = _DATE_PREFIX_RE.match(text)
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _add_years(anchor: date, years: int) -> date:
    target_year = anchor.year + years
    day = anchor.day
    if anchor.month == 2 and day == 29 and not calendar.isleap(target_year):
        day = 28
    return date(target_year, anchor.month, day)


def _trashed_game_ids(state: dict[str, Any]) -> set[str]:
    trashed: set[str] = set()
    for entry in state.get("trash") or []:
        if isinstance(entry, str) and entry.strip():
            trashed.add(entry.strip())
        elif isinstance(entry, dict):
            gid = str(entry.get("game_id") or "").strip()
            if not gid and isinstance(entry.get("game"), dict):
                gid = str(entry["game"].get("game_id") or "").strip()
            if gid:
                trashed.add(gid)
    return trashed


def _public_capsule(cap: dict[str, Any], *, today: date | None = None) -> dict[str, Any]:
    """Return a strict server-redacted projection of a time capsule.

    When ``opened_at`` is not set, ``body`` is omitted completely rather than
    sent empty or flagged client-side.
    """
    today_val = _today(today)
    opened_at = cap.get("opened_at") or None
    unlock_str = str(cap.get("unlock_at") or "")
    try:
        due = bool(unlock_str) and _parse_unlock_date(unlock_str) <= today_val
    except CapsuleInvalidDate:
        due = False
    view: dict[str, Any] = {
        "id": str(cap.get("id") or ""),
        "game_id": str(cap.get("game_id") or ""),
        "game_name": str(cap.get("game_name") or ""),
        "title": str(cap.get("title") or ""),
        "kind": str(cap.get("kind") or "note"),
        "unlock_at": unlock_str,
        "created_at": str(cap.get("created_at") or ""),
        "opened_at": opened_at,
        "locked": opened_at is None,
        "due": due,
    }
    if opened_at:
        view["body"] = str(cap.get("body") or "")
    return view


def create_capsule(
    state: dict[str, Any],
    payload: dict[str, Any],
    *,
    today: date | None = None,
    now_iso: str | None = None,
) -> dict[str, Any]:
    """Validate and append a sealed time capsule to *state*."""
    if not isinstance(payload, dict):
        raise BadRequest("Request body must be an object.")
    body = str(payload.get("body") or "")
    if len(body) > CAPSULE_BODY_MAX:
        raise CapsuleBodyTooLarge(f"Capsule body exceeds {CAPSULE_BODY_MAX} characters.")
    if not body.strip():
        raise BadRequest("Capsule body is required.")

    unlock_date = _parse_unlock_date(payload.get("unlock_at"))
    kind = str(payload.get("kind") or "note").strip().lower()
    if kind not in CAPSULE_KINDS:
        raise BadRequest("Capsule kind must be 'note' or 'question'.")

    game_id = str(payload.get("game_id") or "").strip()
    game_name = str(payload.get("game_name") or "").strip()
    if game_id and not game_name:
        for g in state.get("games") or []:
            if isinstance(g, dict) and str(g.get("game_id") or "") == game_id:
                game_name = str(g.get("name") or "")
                break

    raw_title = str(payload.get("title") or "").strip()
    if len(raw_title) > CAPSULE_TITLE_MAX:
        raise BadRequest(f"Capsule title exceeds {CAPSULE_TITLE_MAX} characters.")
    title = raw_title or (game_name[:CAPSULE_TITLE_MAX] if game_name else "Time Capsule")

    cap_id = f"cap-{secrets.token_hex(8)}"
    record = {
        "id": cap_id,
        "game_id": game_id,
        "game_name": game_name[:CAPSULE_TITLE_MAX],
        "title": title,
        "body": body,
        "kind": kind,
        "unlock_at": unlock_date.isoformat(),
        "created_at": _utc_iso(now_iso),
        "opened_at": None,
    }
    capsules = state.setdefault("time_capsules", [])
    if not isinstance(capsules, list):
        capsules = []
        state["time_capsules"] = capsules
    capsules.append(record)
    return _public_capsule(record, today=today)


def list_capsules(
    state: dict[str, Any],
    *,
    game_id: str = "",
    today: date | None = None,
) -> dict[str, Any]:
    """Return redacted capsule summaries sorted by unlock date."""
    today_val = _today(today)
    wanted = str(game_id or "").strip()
    raw = [c for c in state.get("time_capsules") or [] if isinstance(c, dict)]
    if wanted:
        raw = [c for c in raw if str(c.get("game_id") or "") == wanted]
    projected = [_public_capsule(c, today=today_val) for c in raw]
    projected.sort(key=lambda item: (item["unlock_at"], item["created_at"], item["id"]))
    locked_count = sum(1 for item in projected if item["locked"])
    return {
        "capsules": projected,
        "locked_count": locked_count,
        "unlocked_count": len(projected) - locked_count,
    }


def next_capsule(state: dict[str, Any], *, today: date | None = None) -> dict[str, Any]:
    """Return the earliest sealed capsule (unopened) and due count."""
    today_val = _today(today)
    sealed: list[tuple[date, dict[str, Any]]] = []
    due_count = 0
    for cap in state.get("time_capsules") or []:
        if not isinstance(cap, dict) or cap.get("opened_at"):
            continue
        try:
            u_date = _parse_unlock_date(cap.get("unlock_at"))
        except CapsuleInvalidDate:
            continue
        if u_date <= today_val:
            due_count += 1
        sealed.append((u_date, cap))
    sealed.sort(key=lambda pair: (pair[0], str(pair[1].get("created_at") or ""), str(pair[1].get("id") or "")))
    if not sealed:
        return {"capsule": None, "days_until": None, "due_count": 0}
    soonest_date, soonest_cap = sealed[0]
    days_until = max(0, (soonest_date - today_val).days)
    return {
        "capsule": _public_capsule(soonest_cap, today=today_val),
        "days_until": days_until,
        "due_count": due_count,
    }


def open_capsule(
    state: dict[str, Any],
    capsule_id: str,
    *,
    today: date | None = None,
    now_iso: str | None = None,
) -> dict[str, Any]:
    """Open a due capsule idempotently; refuse early opens with ``CapsuleLocked``."""
    cid = str(capsule_id or "").strip()
    capsules = state.get("time_capsules") or []
    target = next((c for c in capsules if isinstance(c, dict) and str(c.get("id") or "") == cid), None)
    if target is None:
        raise CapsuleNotFound(f"Time capsule not found: {cid}")

    today_val = _today(today)
    # Durable guarantee: once opened_at is set, even a backward clock keeps it open.
    if target.get("opened_at"):
        return _public_capsule(target, today=today_val)

    unlock_date = _parse_unlock_date(target.get("unlock_at"))
    if unlock_date > today_val:
        raise CapsuleLocked(f"Capsule unlocks on {unlock_date.isoformat()} and cannot be opened early.")

    target["opened_at"] = _utc_iso(now_iso)
    return _public_capsule(target, today=today_val)


def delete_capsule(state: dict[str, Any], capsule_id: str) -> dict[str, Any]:
    """Delete any capsule (locked or opened) by ID."""
    cid = str(capsule_id or "").strip()
    capsules = state.get("time_capsules")
    if not isinstance(capsules, list):
        raise CapsuleNotFound(f"Time capsule not found: {cid}")
    before = len(capsules)
    kept = [c for c in capsules if not (isinstance(c, dict) and str(c.get("id") or "") == cid)]
    if len(kept) == before:
        raise CapsuleNotFound(f"Time capsule not found: {cid}")
    state["time_capsules"] = kept
    return {"deleted": True, "id": cid}


def unlock_due_capsules(
    state: dict[str, Any],
    *,
    today: date | None = None,
    now_iso: str | None = None,
) -> list[dict[str, Any]]:
    """Sweep due sealed capsules once, setting ``opened_at`` and emitting notices."""
    today_val = _today(today)
    stamp = _utc_iso(now_iso)
    unlocked: list[dict[str, Any]] = []
    for cap in state.get("time_capsules") or []:
        if not isinstance(cap, dict) or cap.get("opened_at"):
            continue
        try:
            u_date = _parse_unlock_date(cap.get("unlock_at"))
        except CapsuleInvalidDate:
            continue
        if u_date > today_val:
            continue
        cap["opened_at"] = stamp
        cap_title = str(cap.get("title") or "Time Capsule")
        add_notification(
            state,
            kind="capsule",
            level="info",
            title="A time capsule is ready to open",
            body=f"“{cap_title}” reached its unlock date.",
            source="playerlog",
            dedupe_key=f"cap:{cap.get('id')}",
            title_key="notify.server.capsule_unlocked_title",
            body_key="notify.server.capsule_unlocked_body",
            params={"title": cap_title},
            now=stamp,
        )
        unlocked.append(_public_capsule(cap, today=today_val))
    return unlocked


def _binary_search_crossing(
    events: list[tuple[date, int]],
    threshold: int,
    fallback_date: str,
) -> tuple[str, bool]:
    """Return ``(iso_date, approximate)`` when cumulative value reaches *threshold*."""
    if not events or threshold <= 0:
        return fallback_date, True
    ordered = sorted(events, key=lambda pair: pair[0])
    dates: list[date] = []
    prefix: list[int] = []
    running = 0
    for day, delta in ordered:
        val = max(0, int(delta))
        if val == 0:
            continue
        running += val
        dates.append(day)
        prefix.append(running)
    if not prefix or prefix[-1] < threshold:
        return fallback_date, True
    idx = bisect_left(prefix, threshold)
    return dates[idx].isoformat(), False


def _load_ra_cache_entries(data_dir: Path | str | None) -> list[dict[str, Any]]:
    if not data_dir:
        return []
    ra_dir = Path(data_dir) / "cache" / "retroachievements"
    if not ra_dir.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    for path in sorted(ra_dir.glob("*.json")):
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        entries.append({"game_id": path.stem, "path": path, "data": raw})
    return entries


def _collect_session_events(
    state: dict[str, Any],
    eligible_games: list[dict[str, Any]],
) -> tuple[dict[str, list[tuple[date, int]]], list[tuple[date, int]], list[tuple[date, int]]]:
    """Build per-game seconds, library seconds, and library count session timelines."""
    per_game: dict[str, list[tuple[date, int]]] = {}
    all_seconds: list[tuple[date, int]] = []
    all_counts: list[tuple[date, int]] = []

    eligible_ids = {str(g.get("game_id") or "") for g in eligible_games if g.get("game_id")}
    name_to_gid = {
        str(g.get("name") or ""): str(g.get("game_id") or "")
        for g in eligible_games
        if g.get("game_id") and g.get("name")
    }

    for entry in state.get("history") or []:
        if not isinstance(entry, dict):
            continue
        gid = str(entry.get("game_id") or "").strip()
        if not gid and entry.get("game"):
            gid = name_to_gid.get(str(entry["game"]), "")
        day = _parse_date_prefix(entry.get("started") or entry.get("ended") or entry.get("date"))
        try:
            secs = max(0, int(entry.get("seconds") or 0))
        except (TypeError, ValueError):
            secs = 0
        if day is None:
            continue
        all_counts.append((day, 1))
        if not eligible_ids or gid in eligible_ids:
            all_seconds.append((day, secs))
            if gid:
                per_game.setdefault(gid, []).append((day, secs))

    for game in eligible_games:
        gid = str(game.get("game_id") or "").strip()
        for ms in game.get("manual_sessions") or []:
            if not isinstance(ms, dict):
                continue
            day = _parse_date_prefix(ms.get("date"))
            try:
                secs = max(0, int(ms.get("seconds") or 0))
            except (TypeError, ValueError):
                secs = 0
            if day is None:
                continue
            all_counts.append((day, 1))
            all_seconds.append((day, secs))
            if gid:
                per_game.setdefault(gid, []).append((day, secs))

    return per_game, all_seconds, all_counts


def _fallback_date_for_game(game: dict[str, Any], today_str: str) -> str:
    for field in ("completed_at", "last_played", "added_at"):
        parsed = _parse_date_prefix(game.get(field))
        if parsed is not None:
            return parsed.isoformat()
    return today_str


def _latest_known_date(games: list[dict[str, Any]], all_seconds: list[tuple[date, int]], today_str: str) -> str:
    candidates: list[date] = [d for d, _ in all_seconds]
    for g in games:
        for field in ("last_played", "completed_at", "added_at"):
            parsed = _parse_date_prefix(g.get(field))
            if parsed is not None:
                candidates.append(parsed)
    return max(candidates).isoformat() if candidates else today_str


def _compute_full_history_streak(
    all_counts: list[tuple[date, int]],
    today_val: date,
) -> tuple[int, dict[int, str]]:
    """Compute longest streak across full history plus threshold crossing dates."""
    if not all_counts:
        return 0, {}
    by_day: dict[date, int] = {}
    for day, count in all_counts:
        by_day[day] = by_day.get(day, 0) + count
    start_day = min(by_day)
    end_day = max(max(by_day), today_val)
    span = (end_day - start_day).days + 1
    rows: list[dict[str, Any]] = []
    running = 0
    crossed: dict[int, str] = {}
    for offset in range(span):
        cur = start_day + timedelta(days=offset)
        cnt = by_day.get(cur, 0)
        iso = cur.isoformat()
        rows.append({"date": iso, "count": cnt})
        if cnt > 0:
            running += 1
            for target in STREAK_MILESTONES:
                if running >= target and target not in crossed:
                    crossed[target] = iso
        else:
            running = 0
    streak_info = compute_streak(rows)
    return int(streak_info.get("longest") or 0), crossed


def compute_milestones(
    state: dict[str, Any],
    *,
    data_dir: Path | str | None = None,
    today: date | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Deterministically derive all 9 milestone kinds from *state* + RA cache."""
    today_val = _today(today)
    today_str = today_val.isoformat()
    trashed_ids = _trashed_game_ids(state)
    games = [
        g for g in state.get("games") or []
        if isinstance(g, dict) and not g.get("hidden") and str(g.get("game_id") or "") not in trashed_ids
    ]
    game_by_id = {str(g.get("game_id") or ""): g for g in games if g.get("game_id")}
    ui_state = state.get("ui_state") if isinstance(state.get("ui_state"), dict) else {}
    seen_ids = {str(x) for x in ui_state.get("milestones_seen") or [] if x}

    per_game_events, all_seconds, all_counts = _collect_session_events(state, games)
    ra_entries = _load_ra_cache_entries(data_dir)
    ra_by_gid = {entry["game_id"]: entry["data"] for entry in ra_entries}
    latest_date = _latest_known_date(games, all_seconds, today_str)

    unlocked: list[dict[str, Any]] = []
    upcoming: list[dict[str, Any]] = []

    def add_unlocked(
        mid: str,
        kind: str,
        title: str,
        subtitle: str,
        value: int,
        threshold: int,
        unlocked_at: str,
        approximate: bool,
        game: dict[str, Any] | None = None,
    ) -> None:
        item: dict[str, Any] = {
            "id": mid,
            "kind": kind,
            "title": title,
            "subtitle": subtitle,
            "value": value,
            "threshold": threshold,
            "unlocked_at": unlocked_at,
            "approximate": bool(approximate),
            "is_new": mid not in seen_ids,
        }
        if game is not None:
            item["game_id"] = str(game.get("game_id") or "")
            item["game_name"] = str(game.get("name") or "")
        unlocked.append(item)

    # 1. playtime_game
    for game in games:
        gid = str(game.get("game_id") or "").strip()
        if not gid:
            continue
        secs = max(0, total_playtime_seconds(game))
        if secs < 3600:
            continue
        hours = secs / 3600.0
        events = per_game_events.get(gid, [])
        fallback = _fallback_date_for_game(game, today_str) if secs >= GAME_PLAYTIME_HOURS[0] * 3600 else ""
        next_target = None
        for h in GAME_PLAYTIME_HOURS:
            if secs >= h * 3600:
                u_at, approx = _binary_search_crossing(events, h * 3600, fallback)
                add_unlocked(
                    f"m:playtime_game:{gid}:{h}h",
                    "playtime_game",
                    f"{h}h in {game.get('name', 'Game')}",
                    f"{round(hours, 1)} hours played",
                    int(hours),
                    h,
                    u_at,
                    approx,
                    game=game,
                )
            elif next_target is None and hours >= 1:
                next_target = h
        if next_target is not None:
            upcoming.append({
                "id": f"m:playtime_game:{gid}:{next_target}h",
                "kind": "playtime_game",
                "title": f"{next_target}h in {game.get('name', 'Game')}",
                "game_id": gid,
                "current": round(hours, 1),
                "target": next_target,
                "progress_pct": min(99, round(hours / next_target * 100)),
            })

    # 2. playtime_total
    total_secs = sum(max(0, total_playtime_seconds(g)) for g in games)
    total_hours = total_secs / 3600.0
    next_total = None
    for h in TOTAL_PLAYTIME_HOURS:
        if total_secs >= h * 3600:
            u_at, approx = _binary_search_crossing(all_seconds, h * 3600, latest_date)
            add_unlocked(
                f"m:playtime_total:{h}h",
                "playtime_total",
                f"{h:,} Library Hours",
                f"{round(total_hours, 1)} total hours across your collection",
                int(total_hours),
                h,
                u_at,
                approx,
            )
        elif next_total is None:
            next_total = h
    if next_total is not None:
        upcoming.append({
            "id": f"m:playtime_total:{next_total}h",
            "kind": "playtime_total",
            "title": f"{next_total:,} Library Hours",
            "current": round(total_hours, 1),
            "target": next_total,
            "progress_pct": min(99, round(total_hours / next_total * 100)),
        })

    # 3. first_clear
    for game in games:
        gid = str(game.get("game_id") or "").strip()
        if not gid:
            continue
        ra = ra_by_gid.get(gid) or {}
        cleared = _is_completed(game) or bool(ra.get("mastered")) or int(ra.get("beaten") or 0) > 0
        if not cleared:
            continue
        explicit = _parse_date_prefix(game.get("completed_at"))
        if explicit is not None:
            u_at, approx = explicit.isoformat(), False
        else:
            events = per_game_events.get(gid, [])
            if events:
                u_at, approx = max(d for d, _ in events).isoformat(), True
            else:
                u_at, approx = _fallback_date_for_game(game, today_str), True
        add_unlocked(
            f"m:first_clear:{gid}",
            "first_clear",
            f"Cleared {game.get('name', 'Game')}",
            str(game.get("progress") or "Completed"),
            1,
            1,
            u_at,
            approx,
            game=game,
        )

    # 4. sessions
    manual_total = sum(len(g.get("manual_sessions") or []) for g in games)
    history_total = len([e for e in state.get("history") or [] if isinstance(e, dict)])
    total_sessions = history_total + manual_total
    next_sessions = None
    for count in SESSION_MILESTONES:
        if total_sessions >= count:
            u_at, approx = _binary_search_crossing(all_counts, count, latest_date)
            add_unlocked(
                f"m:sessions:{count}",
                "sessions",
                f"{count:,} Play Sessions",
                f"{total_sessions:,} logged sessions",
                total_sessions,
                count,
                u_at,
                approx,
            )
        elif next_sessions is None:
            next_sessions = count
    if next_sessions is not None:
        upcoming.append({
            "id": f"m:sessions:{next_sessions}",
            "kind": "sessions",
            "title": f"{next_sessions:,} Play Sessions",
            "current": total_sessions,
            "target": next_sessions,
            "progress_pct": min(99, round(total_sessions / next_sessions * 100)),
        })

    # 5 & 6. ra_points & ra_complete
    ra_available = bool(ra_entries)
    ra_events: list[tuple[date, int]] = []
    total_ra_pts = 0
    latest_ra_mtime: date | None = None
    for entry in ra_entries:
        gid = entry["game_id"]
        data = entry["data"]
        try:
            file_date = datetime.fromtimestamp(entry["path"].stat().st_mtime, tz=timezone.utc).date()
            if latest_ra_mtime is None or file_date > latest_ra_mtime:
                latest_ra_mtime = file_date
        except OSError:
            file_date = today_val
        earned_dates: list[date] = []
        for ach in data.get("achievements") or []:
            if not isinstance(ach, dict) or not ach.get("earned"):
                continue
            pts = max(0, int(ach.get("points") or 0))
            total_ra_pts += pts
            ed = _parse_date_prefix(ach.get("date_earned_hardcore") or ach.get("date_earned"))
            if ed is not None:
                earned_dates.append(ed)
                ra_events.append((ed, pts))

        total_ach = int(data.get("total") or 0)
        earned_ach = int(data.get("earned") or 0)
        if total_ach > 0 and (bool(data.get("mastered")) or earned_ach >= total_ach):
            game = game_by_id.get(gid)
            gname = str(game.get("name") or gid) if game else gid
            if earned_dates:
                u_at, approx = max(earned_dates).isoformat(), False
            else:
                u_at, approx = file_date.isoformat(), True
            add_unlocked(
                f"m:ra_complete:{gid}",
                "ra_complete",
                f"Mastered {gname}",
                f"{earned_ach}/{total_ach} RetroAchievements",
                earned_ach,
                total_ach,
                u_at,
                approx,
                game=game,
            )

    if ra_available and total_ra_pts > 0:
        fallback_ra = (latest_ra_mtime or today_val).isoformat()
        next_pts = None
        for pts_target in RA_POINTS_MILESTONES:
            if total_ra_pts >= pts_target:
                u_at, approx = _binary_search_crossing(ra_events, pts_target, fallback_ra)
                add_unlocked(
                    f"m:ra_points:{pts_target}",
                    "ra_points",
                    f"{pts_target:,} RetroAchievements Points",
                    f"{total_ra_pts:,} total points earned",
                    total_ra_pts,
                    pts_target,
                    u_at,
                    approx,
                )
            elif next_pts is None:
                next_pts = pts_target
        if next_pts is not None:
            upcoming.append({
                "id": f"m:ra_points:{next_pts}",
                "kind": "ra_points",
                "title": f"{next_pts:,} RetroAchievements Points",
                "current": total_ra_pts,
                "target": next_pts,
                "progress_pct": min(99, round(total_ra_pts / next_pts * 100)),
            })

    # 7. streak
    longest_streak, streak_crossed = _compute_full_history_streak(all_counts, today_val)
    next_streak = None
    for days_target in STREAK_MILESTONES:
        if longest_streak >= days_target and days_target in streak_crossed:
            add_unlocked(
                f"m:streak:{days_target}d",
                "streak",
                f"{days_target}-Day Play Streak",
                f"Longest streak: {longest_streak} consecutive days",
                longest_streak,
                days_target,
                streak_crossed[days_target],
                False,
            )
        elif next_streak is None:
            next_streak = days_target
    if next_streak is not None:
        upcoming.append({
            "id": f"m:streak:{next_streak}d",
            "kind": "streak",
            "title": f"{next_streak}-Day Play Streak",
            "current": longest_streak,
            "target": next_streak,
            "progress_pct": min(99, round(longest_streak / next_streak * 100)) if next_streak else 0,
        })

    # 8. library_age
    added_dates = [d for g in games if (d := _parse_date_prefix(g.get("added_at"))) is not None]
    anchor_date: date | None = min(added_dates) if added_dates else (min(d for d, _ in all_counts) if all_counts else None)
    if anchor_date is not None:
        next_age = None
        for years_target in LIBRARY_AGE_YEARS:
            anniversary = _add_years(anchor_date, years_target)
            if anniversary <= today_val:
                add_unlocked(
                    f"m:library_age:{years_target}y",
                    "library_age",
                    f"{years_target}-Year Library Anniversary",
                    f"Collecting since {anchor_date.isoformat()}",
                    years_target,
                    years_target,
                    anniversary.isoformat(),
                    False,
                )
            elif next_age is None:
                next_age = years_target
        if next_age is not None:
            elapsed_days = max(0, (today_val - anchor_date).days)
            target_days = next_age * 365
            upcoming.append({
                "id": f"m:library_age:{next_age}y",
                "kind": "library_age",
                "title": f"{next_age}-Year Library Anniversary",
                "current": elapsed_days,
                "target": target_days,
                "progress_pct": min(99, round(elapsed_days / target_days * 100)),
            })

    # 9. collection
    collection_count = len(games)
    sorted_added = sorted(added_dates)
    next_coll = None
    for size_target in COLLECTION_MILESTONES:
        if collection_count >= size_target:
            if len(sorted_added) >= size_target:
                u_at, approx = sorted_added[size_target - 1].isoformat(), False
            else:
                u_at, approx = (sorted_added[-1].isoformat() if sorted_added else latest_date), True
            add_unlocked(
                f"m:collection:{size_target}",
                "collection",
                f"{size_target} Games Collected",
                f"{collection_count} active games in library",
                collection_count,
                size_target,
                u_at,
                approx,
            )
        elif next_coll is None:
            next_coll = size_target
    if next_coll is not None:
        upcoming.append({
            "id": f"m:collection:{next_coll}",
            "kind": "collection",
            "title": f"{next_coll} Games Collected",
            "current": collection_count,
            "target": next_coll,
            "progress_pct": min(99, round(collection_count / next_coll * 100)),
        })

    unlocked.sort(key=lambda item: (item["unlocked_at"], item["kind"], item["id"]), reverse=True)
    upcoming.sort(key=lambda item: (-item["progress_pct"], item["kind"], item["id"]))
    by_kind: dict[str, int] = {}
    for item in unlocked:
        k = item["kind"]
        by_kind[k] = by_kind.get(k, 0) + 1

    return {
        "unlocked": unlocked[:limit] if limit is not None else unlocked,
        "upcoming": upcoming[:8],
        "counts": {
            "unlocked": len(unlocked),
            "new": sum(1 for item in unlocked if item["is_new"]),
            "by_kind": by_kind,
        },
        "ra_available": ra_available,
    }


__all__ = [
    "CAPSULE_BODY_MAX",
    "CAPSULE_KINDS",
    "CAPSULE_TITLE_MAX",
    "compute_milestones",
    "create_capsule",
    "delete_capsule",
    "list_capsules",
    "next_capsule",
    "open_capsule",
    "unlock_due_capsules",
]
