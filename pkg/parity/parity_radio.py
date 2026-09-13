"""Backlog Radio — habit-aware playlist + abandonment radar (T4, OpenBox 1.11).

Pure functions over library state (games + history). No I/O, no runtime deps.

Habit model: windowed (``RADIO_WINDOW_DAYS``), half-life decayed
(``RADIO_HALF_LIFE_DAYS``) aggregates of real session history — weighted
seconds per genre/platform, median session length, completion-vs-abandonment
per genre, daypart histogram, and the recently-loved set.

Candidate score = similarity x habit-fit x freshness, each floored so a weak
factor dampens rather than vetoes a pick. Reasons ship as i18n keys + params
and must be true of the data — the test suite asserts param truth.

The weekly/on-demand playlist materializes into ``state["playlists"]`` as a
managed entry (``managed == "radio"``): a manual-membership collection is the
only form in the preset/playlist family that can honestly hold an explicit
pick list — filter presets apply rules, and a pick set is not a rule. This is
the 1.11 plan's "managed filter preset" contract realized as a managed saved
collection the sidebar can render and the picker can scope to.

The abandonment radar partitions started-then-stalled games into "still
winnable for you" and "consider parking" from observed-vs-estimated playtime
against the user's demonstrated completion budget. ``park_game`` records a
park so the radar stops resurfacing the title.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from pkg.parity.parity_constellation import KIND_WEIGHT, PLATFORM_FAMILIES
from pkg.parity.parity_picker import _estimated_minutes_for_genre

RADIO_PLAYLIST_NAME = "Backlog Radio"
RADIO_PICK_LIMIT = 5
RADIO_STALE_DAYS = 7
RADIO_WINDOW_DAYS = 90
RADIO_HALF_LIFE_DAYS = 30.0
MIN_SESSIONS_FOR_HABIT = 8
RADAR_STALL_DAYS = 14
RADAR_PARK_DAYS = 30
RADAR_MIN_OBSERVED_SECONDS = 900
RADAR_MIN_SESSIONS = 3
RECENTLY_LOVED_DAYS = 60
RECENTLY_LOVED_LIMIT = 5
LOVED_FALLBACK_LIMIT = 3
LOVED_FALLBACK_MIN_SECONDS = 3600
INVESTED_WINNABLE_RATIO = 0.25
PARK_MAX_INVESTED_RATIO = 0.15
PARK_MIN_ESTIMATE_FACTOR = 1.5
FRESH_UNPLAYED_DAYS = 14
DIVERSITY_SCORE_FRACTION = 0.6
COMPLETED_TOKENS = ("beaten", "completed", "mastered")
DAYPART_HOURS = {"morning": (5, 12), "afternoon": (12, 17), "evening": (17, 23)}


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _nonneg(value: Any) -> float:
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def _game_key(game: dict) -> str:
    key = game.get("game_id")
    if key not in (None, ""):
        return str(key)
    alt = game.get("id")
    return "" if alt is None else str(alt)


def _genres(game: dict) -> list[str]:
    return [part.strip() for part in str(game.get("genre") or "").split(",") if part.strip()]


def _primary_genre(game: dict) -> str:
    genres = _genres(game)
    return genres[0] if genres else ""


def _norm(value: Any) -> str:
    return str(value or "").strip().casefold()


def _daypart(hour: int) -> str:
    for name, (start, end) in DAYPART_HOURS.items():
        if start <= hour < end:
            return name
    return "night"


def _is_completed(game: dict) -> bool:
    progress = _norm(game.get("progress"))
    return any(token in progress for token in COMPLETED_TOKENS)


def _days_since_last_play(game: dict, days_for: dict[str, set], now: datetime) -> int | None:
    last = _parse_dt(game.get("last_played"))
    if last is not None:
        return max(0, (now - last).days)
    days = days_for.get(_game_key(game))
    if not days:
        return None
    return max(0, (now.date() - max(days)).days)


def _days_since_added(game: dict, now: datetime) -> int | None:
    added = _parse_dt(game.get("added_at") or game.get("added"))
    if added is None:
        return None
    return max(0, (now - added).days)


def _eligible(game: dict) -> bool:
    if not isinstance(game, dict) or not _game_key(game):
        return False
    if game.get("hidden") or game.get("hide_in_bigbox") or game.get("demo"):
        return False
    if game.get("path_exists") is False and game.get("store_installed") is False:
        return False
    return not _is_completed(game)


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    if n % 2:
        return ordered[n // 2]
    return (ordered[n // 2 - 1] + ordered[n // 2]) / 2


def build_habit_model(
    games: list[dict], history: list[dict], now: datetime | None = None
) -> dict[str, Any]:
    """Windowed, decayed habit model from real session history."""
    now = _now(now)
    games = games if isinstance(games, list) else []
    history = history if isinstance(history, list) else []
    by_key = {_game_key(g): g for g in games if isinstance(g, dict) and _game_key(g)}
    window_start = now - timedelta(days=RADIO_WINDOW_DAYS)

    genre_seconds: Counter[str] = Counter()
    platform_seconds: Counter[str] = Counter()
    dayparts: Counter[str] = Counter()
    genre_dayparts: dict[str, Counter[str]] = {}
    lengths: list[float] = []
    observed: Counter[str] = Counter()
    days_for: dict[str, set] = {}
    sessions = 0

    for entry in history:
        if not isinstance(entry, dict):
            continue
        started = _parse_dt(entry.get("started"))
        if started is None or started < window_start or started > now + timedelta(days=1):
            continue
        sessions += 1
        seconds = _nonneg(entry.get("seconds"))
        if seconds:
            lengths.append(seconds)
        age_days = max(0.0, (now - started).total_seconds() / 86400.0)
        weight = 0.5 ** (age_days / RADIO_HALF_LIFE_DAYS)
        part = _daypart(started.hour)
        dayparts[part] += weight
        key = str(entry.get("game_id") or "")
        if key:
            days_for.setdefault(key, set()).add(started.date())
        game = by_key.get(key)
        if game is None:
            continue
        observed[key] += seconds
        for genre in _genres(game):
            genre_seconds[genre] += seconds * weight
            genre_dayparts.setdefault(genre, Counter())[part] += weight
        platform = str(game.get("platform") or "").strip()
        if platform:
            platform_seconds[platform] += seconds * weight

    finished: Counter[str] = Counter()
    abandoned: Counter[str] = Counter()
    for game in games:
        if not isinstance(game, dict):
            continue
        playtime = _nonneg(game.get("playtime_seconds"))
        if playtime <= 0:
            continue
        estimate_seconds = _estimated_minutes_for_genre(_norm(game.get("genre"))) * 60
        days = _days_since_last_play(game, days_for, now)
        if _is_completed(game) or (estimate_seconds and playtime >= 0.8 * estimate_seconds):
            for genre in _genres(game):
                finished[genre] += 1
        elif days is not None and days >= 30:
            for genre in _genres(game):
                abandoned[genre] += 1
    completion = {
        genre: finished[genre] / (finished[genre] + abandoned[genre])
        for genre in set(finished) | set(abandoned)
        if finished[genre] + abandoned[genre] > 0
    }

    loved = _recently_loved(games, days_for, observed, now)

    top_genre = max(genre_seconds, key=genre_seconds.get) if genre_seconds else ""
    top_platform = max(platform_seconds, key=platform_seconds.get) if platform_seconds else ""
    return {
        "now": now,
        "sessions": sessions,
        "enough": sessions >= MIN_SESSIONS_FOR_HABIT,
        "genre_seconds": dict(genre_seconds),
        "platform_seconds": dict(platform_seconds),
        "dayparts": dict(dayparts),
        "genre_dayparts": {g: dict(c) for g, c in genre_dayparts.items()},
        "median_session_min": _median(lengths) / 60.0,
        "observed": dict(observed),
        "days_for": days_for,
        "completion": completion,
        "completion_budget_min": _completion_budget(games),
        "loved": loved,
        "top_genre": top_genre,
        "top_platform": top_platform,
    }


def _completion_budget(games: list[dict]) -> float:
    """Median total playtime (minutes) of games the user actually finished."""
    values = [
        _nonneg(g.get("playtime_seconds")) / 60.0
        for g in games
        if isinstance(g, dict) and _is_completed(g) and _nonneg(g.get("playtime_seconds")) > 0
    ]
    if values:
        return _median(values)
    return 0.0


def _recently_loved(
    games: list[dict], days_for: dict[str, set], observed: Counter, now: datetime
) -> list[str]:
    loved: list[tuple[int, str]] = []
    pool: list[tuple[float, str]] = []
    for game in games:
        if not isinstance(game, dict):
            continue
        key = _game_key(game)
        if not key:
            continue
        days = _days_since_last_play(game, days_for, now)
        if days is None or days > RECENTLY_LOVED_DAYS:
            continue
        rating = _nonneg(game.get("rating"))
        if game.get("favorite") or rating >= 4:
            loved.append((days, key))
        else:
            playtime = _nonneg(game.get("playtime_seconds"))
            if playtime >= LOVED_FALLBACK_MIN_SECONDS or observed.get(key, 0) >= LOVED_FALLBACK_MIN_SECONDS:
                pool.append((max(playtime, observed.get(key, 0)), key))
    loved.sort(key=lambda pair: (pair[0], pair[1]))
    if loved:
        return [key for _, key in loved[:RECENTLY_LOVED_LIMIT]]
    pool.sort(key=lambda pair: (-pair[0], pair[1]))
    return [key for _, key in pool[:LOVED_FALLBACK_LIMIT]]


def _shared_edge(a: dict, b: dict, days_for: dict[str, set]) -> tuple[float, str]:
    """Best constellation-style edge weight between two games."""
    best, kind = 0.0, ""
    series_a, series_b = _norm(a.get("series")), _norm(b.get("series"))
    if series_a and series_a == series_b:
        best, kind = KIND_WEIGHT["series"], "series"
    if _norm(a.get("developer")) and _norm(a.get("developer")) == _norm(b.get("developer")):
        if KIND_WEIGHT["developer"] > best:
            best, kind = KIND_WEIGHT["developer"], "developer"
    if _norm(a.get("publisher")) and _norm(a.get("publisher")) == _norm(b.get("publisher")):
        if KIND_WEIGHT["publisher"] > best:
            best, kind = KIND_WEIGHT["publisher"], "publisher"
    if set(_norm(g) for g in _genres(a)) & set(_norm(g) for g in _genres(b)):
        if KIND_WEIGHT["genre"] > best:
            best, kind = KIND_WEIGHT["genre"], "genre"
    fam_a = PLATFORM_FAMILIES.get(str(a.get("platform") or ""), "Other")
    fam_b = PLATFORM_FAMILIES.get(str(b.get("platform") or ""), "Other")
    if fam_a == fam_b and fam_a != "Other":
        if KIND_WEIGHT["platform_family"] > best:
            best, kind = KIND_WEIGHT["platform_family"], "platform_family"
    key_a, key_b = _game_key(a), _game_key(b)
    days_a, days_b = days_for.get(key_a, set()), days_for.get(key_b, set())
    if days_a and days_b:
        co_played = any(
            (day + timedelta(days=delta)) in days_b
            for day in days_a
            for delta in range(-7, 8)
        )
        if co_played and KIND_WEIGHT["co_played"] > best:
            best, kind = KIND_WEIGHT["co_played"], "co_played"
    return best, kind


def _effective_minutes(game: dict) -> float:
    """Remaining length estimate: observed-vs-estimated for played games."""
    estimate = _estimated_minutes_for_genre(_norm(game.get("genre")))
    playtime_min = _nonneg(game.get("playtime_seconds")) / 60.0
    if playtime_min > 0:
        return max(15.0, estimate - playtime_min)
    return estimate


def _freshness(game: dict, days_for: dict[str, set], now: datetime) -> float:
    if int(_nonneg(game.get("play_count"))) == 0:
        return 1.0
    days = _days_since_last_play(game, days_for, now)
    if days is None:
        return 1.0
    return 0.3 + 0.7 * min(days / 120.0, 1.0)


def _genre_daypart_fit(model: dict, game: dict, now: datetime) -> float:
    """Share of this game's genre sessions in the current daypart (0.5 neutral)."""
    part = _daypart(now.hour)
    shares = []
    for genre in _genres(game):
        counts = model.get("genre_dayparts", {}).get(genre) or {}
        total = sum(counts.values())
        if total > 0:
            shares.append(counts.get(part, 0.0) / total)
    return max(shares) if shares else 0.5


def score_candidates(
    games: list[dict],
    model: dict,
    now: datetime | None = None,
    exclude_ids: Any = (),
) -> list[dict]:
    """Score = similarity x habit-fit x freshness (floored factors)."""
    now = _now(now or model.get("now"))
    exclude = {str(item) for item in (exclude_ids or ())}
    games = [g for g in games if isinstance(g, dict)] if isinstance(games, list) else []
    by_key = {_game_key(g): g for g in games if _game_key(g)}
    loved_games = [by_key[key] for key in model.get("loved", []) if key in by_key]
    top_genre_seconds = max(model["genre_seconds"].values(), default=0.0) or 1.0
    top_platform_seconds = max(model["platform_seconds"].values(), default=0.0) or 1.0
    days_for = model.get("days_for", {})
    out = []
    for game in games:
        key = _game_key(game)
        if key in exclude or not _eligible(game):
            continue
        best_sim, source = 0.0, None
        for loved in loved_games:
            if _game_key(loved) == key:
                continue
            weight, _kind = _shared_edge(game, loved, days_for)
            if weight > best_sim:
                best_sim, source = weight, loved
        genres = _genres(game)
        genre_aff = max(
            (model["genre_seconds"].get(g, 0.0) / top_genre_seconds for g in genres),
            default=0.0,
        )
        platform = str(game.get("platform") or "").strip()
        plat_aff = model["platform_seconds"].get(platform, 0.0) / top_platform_seconds if platform else 0.0
        completion = model["completion"].get(_primary_genre(game), 0.5)
        minutes = _effective_minutes(game)
        median = model.get("median_session_min") or 0.0
        session_fit = min(1.0, 4.0 * median / minutes) if median and minutes else 0.7
        daypart_fit = _genre_daypart_fit(model, game, now)
        habit_fit = (
            0.30 * genre_aff
            + 0.15 * plat_aff
            + 0.25 * completion
            + 0.20 * session_fit
            + 0.10 * daypart_fit
        )
        freshness = _freshness(game, days_for, now)
        score = (0.15 + 0.85 * best_sim) * (0.25 + 0.75 * habit_fit) * (0.25 + 0.75 * freshness)
        out.append(
            {
                "game": game,
                "key": key,
                "score": round(score, 4),
                "similarity": best_sim,
                "similar_to": source,
                "habit_fit": habit_fit,
                "genre_aff": genre_aff,
                "platform_aff": plat_aff,
                "completion": completion,
                "session_fit": session_fit,
                "effective_minutes": minutes,
                "freshness": freshness,
            }
        )
    out.sort(key=lambda c: (-c["score"], c["key"]))
    return out


def _reasons(cand: dict, model: dict, now: datetime) -> list[dict]:
    """Ordered TRUE reason chips (max 3). Every param comes from the model."""
    game = cand["game"]
    reasons: list[dict] = []
    median = model.get("median_session_min") or 0.0
    if cand["session_fit"] >= 0.9 and median and cand["effective_minutes"] <= 2 * median and cand["effective_minutes"] <= 90:
        reasons.append({"key": "radio.reason.short", "params": {"minutes": int(round(cand["effective_minutes"]))}})
    genre = _primary_genre(game)
    if genre and cand["genre_aff"] >= 0.3:
        if cand["completion"] >= 0.5 and genre in model.get("completion", {}):
            reasons.append({"key": "radio.reason.genre_finish", "params": {"genre": genre}})
        else:
            reasons.append({"key": "radio.reason.genre_habit", "params": {"genre": genre}})
    if cand["similarity"] >= 0.3 and cand["similar_to"] is not None:
        source = cand["similar_to"]
        reasons.append(
            {
                "key": "radio.reason.similar_to",
                "params": {"name": str(source.get("name") or ""), "source_id": _game_key(source)},
            }
        )
    platform = str(game.get("platform") or "").strip()
    if platform and cand["platform_aff"] >= 0.25:
        reasons.append({"key": "radio.reason.platform_habit", "params": {"platform": platform}})
    if int(_nonneg(game.get("play_count"))) == 0:
        days = _days_since_added(game, now)
        if days is not None and days >= FRESH_UNPLAYED_DAYS:
            reasons.append({"key": "radio.reason.fresh", "params": {"days": days}})
    if not reasons:
        if genre:
            reasons.append({"key": "radio.reason.genre_habit", "params": {"genre": genre}})
        else:
            reasons.append({"key": "radio.reason.fresh", "params": {"days": _days_since_added(game, now) or 0}})
    return reasons[:3]


def _diverse(cands: list[dict], limit: int) -> list[dict]:
    """Diversity guard: promote novel (genre, platform) signatures, but only
    while they stay competitive with the top score — habit-matched picks are
    never displaced by a much weaker candidate just for variety."""
    if len(cands) <= limit:
        return list(cands)
    floor = cands[0]["score"] * DIVERSITY_SCORE_FRACTION
    picked: list[dict] = []
    seen: set[tuple[str, str]] = set()
    rest: list[dict] = []
    for cand in cands:
        signature = (_primary_genre(cand["game"]), str(cand["game"].get("platform") or ""))
        if len(picked) < limit and cand["score"] >= floor and signature not in seen:
            seen.add(signature)
            picked.append(cand)
        else:
            rest.append(cand)
    for cand in rest:
        if len(picked) >= limit:
            break
        picked.append(cand)
    return picked[:limit]


def _public_pick(cand: dict, model: dict, now: datetime) -> dict:
    game = cand["game"]
    return {
        "game_id": cand["key"],
        "id": game.get("id"),
        "name": str(game.get("name") or ""),
        "platform": str(game.get("platform") or ""),
        "genre": str(game.get("genre") or ""),
        "has_cover": bool(game.get("has_cover")),
        "cover": game.get("cover", ""),
        "score": cand["score"],
        "estimated_minutes": int(round(cand["effective_minutes"])),
        "reasons": _reasons(cand, model, now),
    }


def _fallback_picks(games: list[dict], exclude: set[str], now: datetime, limit: int) -> list[dict]:
    """Honest thin-history picks: rating first, then fresh finds."""
    pool = [g for g in games if isinstance(g, dict) and _eligible(g) and _game_key(g) not in exclude]

    def rank(game: dict) -> tuple:
        rating = _nonneg(game.get("rating"))
        days = _days_since_added(game, now)
        return (-rating, int(_nonneg(game.get("play_count"))) > 0, -(days or -1), _game_key(game))

    pool.sort(key=rank)
    picks = []
    for game in pool[:limit]:
        rating = _nonneg(game.get("rating"))
        if rating >= 4:
            reasons = [{"key": "radio.reason.fallback_rating", "params": {"rating": rating}}]
        else:
            reasons = [{"key": "radio.reason.fallback_fresh", "params": {}}]
        picks.append(
            {
                "game_id": _game_key(game),
                "id": game.get("id"),
                "name": str(game.get("name") or ""),
                "platform": str(game.get("platform") or ""),
                "genre": str(game.get("genre") or ""),
                "has_cover": bool(game.get("has_cover")),
                "cover": game.get("cover", ""),
                "score": 0.0,
                "estimated_minutes": int(round(_effective_minutes(game))),
                "reasons": reasons,
            }
        )
    return picks


def build_playlist(
    games: list[dict],
    history: list[dict],
    previous_ids: Any = (),
    now: datetime | None = None,
    limit: int = RADIO_PICK_LIMIT,
) -> dict[str, Any]:
    """Build the radio playlist; deterministic for a fixed ``now``."""
    now = _now(now)
    games = games if isinstance(games, list) else []
    history = history if isinstance(history, list) else []
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = RADIO_PICK_LIMIT
    exclude = {str(item) for item in (previous_ids or ())}
    model = build_habit_model(games, history, now=now)
    if model["enough"]:
        cands = score_candidates(games, model, now=now, exclude_ids=exclude)
        picks = [_public_pick(c, model, now) for c in _diverse(cands, limit)]
        fallback = False
        notice = "radio.notice.ok" if picks else "radio.notice.empty"
    else:
        picks = _fallback_picks(games, exclude, now, limit)
        fallback = True
        notice = "radio.notice.not_enough_history" if picks else "radio.notice.empty"
    return {
        "name": RADIO_PLAYLIST_NAME,
        "generated_at": now.isoformat(),
        "picks": picks,
        "fallback": fallback,
        "enough_history": model["enough"],
        "notice_key": notice,
    }


def _radar_entry(game: dict, days: int, observed: float, est_min: float, key: str) -> dict:
    return {
        "game_id": _game_key(game),
        "id": game.get("id"),
        "name": str(game.get("name") or ""),
        "platform": str(game.get("platform") or ""),
        "has_cover": bool(game.get("has_cover")),
        "days_stalled": days,
        "observed_seconds": int(observed),
        "estimated_minutes": int(round(est_min)),
        "reasons": [{"key": key, "params": {"days": days}}],
    }


def radar(
    games: list[dict],
    history: list[dict],
    parked_ids: Any = (),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Started-then-stalled partition: still winnable vs consider parking."""
    now = _now(now)
    games = games if isinstance(games, list) else []
    history = history if isinstance(history, list) else []
    model = build_habit_model(games, history, now=now)
    finished_any = any(
        isinstance(g, dict) and _is_completed(g) for g in games
    )
    enough = model["sessions"] >= RADAR_MIN_SESSIONS or finished_any
    result = {"winnable": [], "park": [], "enough_history": enough}
    if not enough:
        return result
    parked = {str(item) for item in (parked_ids or ())}
    budget = model["completion_budget_min"] or max(4.0 * model["median_session_min"], 360.0)
    days_for = model["days_for"]
    for game in games:
        if not isinstance(game, dict) or game.get("hidden"):
            continue
        key = _game_key(game)
        if not key or key in parked:
            continue
        if _is_completed(game):
            continue
        observed = _nonneg(game.get("playtime_seconds"))
        if observed < RADAR_MIN_OBSERVED_SECONDS:
            continue
        days = _days_since_last_play(game, days_for, now)
        if days is None or days < RADAR_STALL_DAYS:
            continue
        est_min = _estimated_minutes_for_genre(_norm(game.get("genre")))
        invested = observed / (est_min * 60.0) if est_min else 0.0
        winnable = invested >= INVESTED_WINNABLE_RATIO or est_min <= budget
        deep_stall = (
            days >= RADAR_PARK_DAYS
            and invested < PARK_MAX_INVESTED_RATIO
            and est_min > budget * PARK_MIN_ESTIMATE_FACTOR
        )
        if deep_stall or not winnable:
            result["park"].append(_radar_entry(game, days, observed, est_min, "radio.reason.park"))
        else:
            result["winnable"].append(_radar_entry(game, days, observed, est_min, "radio.reason.winnable"))
    def order(item):
        return (-item["days_stalled"], item["game_id"])
    result["winnable"].sort(key=order)
    result["park"].sort(key=order)
    return result


def parked_ids(state: dict) -> set[str]:
    box = state.get("radio") if isinstance(state, dict) else None
    if not isinstance(box, dict):
        return set()
    return {str(item) for item in box.get("parked") or []}


def managed_playlist(state: dict) -> dict | None:
    playlists = state.get("playlists") if isinstance(state, dict) else None
    if not isinstance(playlists, list):
        return None
    for item in playlists:
        if isinstance(item, dict) and item.get("managed") == "radio":
            return item
    return None


def playlist_stale(entry: dict, now: datetime | None = None) -> bool:
    now = _now(now)
    generated = _parse_dt(entry.get("generated_at")) if isinstance(entry, dict) else None
    if generated is None:
        return True
    return (now - generated) >= timedelta(days=RADIO_STALE_DAYS)


def materialize_playlist(state: dict, playlist: dict, now: datetime | None = None) -> dict:
    """Persist the playlist as the managed saved collection (replaces itself)."""
    now = _now(now)
    playlists = state.setdefault("playlists", [])
    if not isinstance(playlists, list):
        playlists = []
        state["playlists"] = playlists
    entry = {
        "name": RADIO_PLAYLIST_NAME,
        "type": "manual",
        "managed": "radio",
        "generated_at": now.isoformat(),
        "members": [str(p["game_id"]) for p in playlist.get("picks", [])],
        "picks": [
            {"game_id": str(p["game_id"]), "score": p.get("score", 0.0), "reasons": p.get("reasons", [])}
            for p in playlist.get("picks", [])
        ],
        "fallback": bool(playlist.get("fallback")),
        "notice_key": str(playlist.get("notice_key") or "radio.notice.ok"),
        "rules": {},
    }
    for index, item in enumerate(playlists):
        if isinstance(item, dict) and item.get("managed") == "radio":
            playlists[index] = entry
            return entry
    playlists.append(entry)
    return entry


def entry_payload(entry: dict, state: dict) -> dict[str, Any]:
    """Hydrate a stored managed playlist with live game fields."""
    games = state.get("games") if isinstance(state, dict) else []
    by_key: dict[str, dict] = {}
    for game in games if isinstance(games, list) else []:
        if not isinstance(game, dict):
            continue
        key = _game_key(game)
        if key:
            by_key[key] = game
    picks = []
    for stored in entry.get("picks") or []:
        if not isinstance(stored, dict):
            continue
        game = by_key.get(str(stored.get("game_id") or ""))
        if game is None:
            continue
        picks.append(
            {
                "game_id": str(stored.get("game_id") or ""),
                "id": game.get("id"),
                "name": str(game.get("name") or ""),
                "platform": str(game.get("platform") or ""),
                "genre": str(game.get("genre") or ""),
                "has_cover": bool(game.get("has_cover")),
                "cover": game.get("cover", ""),
                "score": stored.get("score", 0.0),
                "reasons": stored.get("reasons") or [],
            }
        )
    return {
        "name": str(entry.get("name") or RADIO_PLAYLIST_NAME),
        "generated_at": str(entry.get("generated_at") or ""),
        "stale": playlist_stale(entry),
        "picks": picks,
        "fallback": bool(entry.get("fallback")),
        "enough_history": not entry.get("fallback"),
        "notice_key": str(entry.get("notice_key") or "radio.notice.ok"),
        "managed": True,
    }


def radio_payload(state: dict, now: datetime | None = None, force: bool = False) -> dict:
    """Return the current playlist payload; regenerate + persist when due.

    Safe to call inside a ``transact_state`` mutator — it writes only when it
    actually regenerates.
    """
    now = _now(now)
    entry = managed_playlist(state)
    if entry is not None and not force and not playlist_stale(entry, now):
        payload = entry_payload(entry, state)
        if payload["picks"] or not state.get("games"):
            return payload
    previous = [str(m) for m in (entry or {}).get("members") or []] if entry else []
    playlist = build_playlist(state.get("games"), state.get("history"), previous_ids=previous, now=now)
    entry = materialize_playlist(state, playlist, now=now)
    return entry_payload(entry, state)


def park_game(state: dict, game_id: Any, now: datetime | None = None) -> dict | None:
    """Park a stalled game: mark progress Paused and hide it from the radar."""
    target = str(game_id or "").strip()
    if not target:
        return None
    games = state.get("games") if isinstance(state, dict) else []
    for game in games if isinstance(games, list) else []:
        if not isinstance(game, dict):
            continue
        key = _game_key(game)
        if target not in (key, str(game.get("id") or "")):
            continue
        if _is_completed(game):
            return {"ok": False, "code": "already_completed", "game_id": key}
        game["progress"] = "Paused"
        box = state.setdefault("radio", {})
        parked = box.setdefault("parked", [])
        if key not in parked:
            parked.append(key)
        return {
            "ok": True,
            "game_id": key,
            "name": str(game.get("name") or ""),
            "progress": "Paused",
        }
    return None
