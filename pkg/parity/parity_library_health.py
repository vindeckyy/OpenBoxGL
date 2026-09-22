"""Library health scoring engine (Flagship 8, H1).

Pure functions: the game list goes in, a 0-100 score, per-dimension
sub-scores, and a deduction ledger come out. Every deduction names its
dimension, the points lost, a human reason, and the offending game ids,
so the score is fully explainable by construction.

Dimension weights are documented in docs/adr/0056-library-health-score.md.
Detectors are extracted from ``LibraryHandlers.health()`` (handlers/library.py)
so the v1 audit route and the health score can never disagree about what
counts as an issue. The only I/O is cached path probes via
``pkg.state.media_probe.probe_path``.

Never auto-fix: this module only measures. Fix orchestration lives in
handlers/library_health.py and always goes through a dry-run preview.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable
from typing import Any

# Dimension weights. Sum must be 100. See ADR 0056.
DIMENSION_WEIGHTS = {
    "file_integrity": 35,
    "duplicates": 20,
    "artwork": 20,
    "metadata": 15,
    "launch_readiness": 10,
}

DIMENSIONS = tuple(DIMENSION_WEIGHTS)

# Fixed per-finding deductions, in dimension points (dimension clamped at 0).
DEDUCT_MISSING_GAME = 60
DEDUCT_MISSING_EXTRA = 10
DEDUCT_MISSING_EXTRA_CAP = 30
DEDUCT_MISSING_SAVE_PATH = 5
DEDUCT_MISSING_SAVE_PATH_CAP = 10
DEDUCT_DUPLICATE = 50
DEDUCT_METADATA_FIELD = 15
DEDUCT_NO_EMULATOR = 60
DEDUCT_BROKEN = 40

# Artwork weights by type priority (cover >> banner/icon; screenshots as a group).
ARTWORK_DEDUCTIONS = {
    "cover": 40,
    "banner": 15,
    "icon": 15,
    "clear_logo": 10,
    "fanart": 10,
    "background": 10,
    "screenshots": 10,
}
ARTWORK_DEDUCT_OTHER = 5

METADATA_FIELDS = ("name", "platform", "genre", "year", "developer", "description")

ROM_SUFFIXES = {".rom", ".nes", ".sfc", ".smc", ".gba", ".gb", ".gbc", ".iso"}

# Screenshot-ish list fields that count as a group rather than per file.
_GROUP_MEDIA_FIELDS = {"screenshots"}

_HEALTH_CACHE_KEY = "health_cache"
_FIX_JOURNAL_KEY = "health_fixes"
_FIX_JOURNAL_CAP = 50
_DIRTY_IDS_CAP = 5000

RESCAN_OPTIONS = ("daily", "weekly", "on_startup", "off")
DEFAULT_RESCAN = "weekly"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _game_key(game: dict[str, Any], index: int) -> str:
    return str(game.get("game_id") or game.get("id") or index)


def _default_probe(path: str, *, file_only: bool = False) -> bool:
    try:
        from pkg.state.media_probe import probe_path
    except ImportError:
        value = str(path or "")
        if not value:
            return False
        try:
            if file_only:
                return Path(value).is_file()
            return os.path.exists(value)
        except OSError:
            return False
    return bool(probe_path(path, file_only=file_only))


def _identity_helpers():
    """Mirror the identity imports LibraryHandlers.health() used."""
    try:
        from pkg.parity.parity_identity import (
            cross_source_identity,
            detect_duplicate_identities,
            normalize_identity,
        )
    except ImportError:
        return None
    try:
        from pkg.state.imports import game_identity as legacy_identity
    except ImportError:
        def legacy_identity(game):  # noqa: D103 - minimal fallback
            return ("path", str(Path(game.get("path", "")).expanduser()))
    return {
        "cross_source_identity": cross_source_identity,
        "detect_duplicate_identities": detect_duplicate_identities,
        "normalize_identity": normalize_identity,
        "legacy_identity": legacy_identity,
    }


def _media_types():
    try:
        from pkg.state.media_probe import MEDIA_TYPES_ALL
    except ImportError:
        MEDIA_TYPES_ALL = {"cover"}
    return [field for field in MEDIA_TYPES_ALL if field != "cover"]


def _issue(dimension, code, index, game, *, points, detail="", reason="", legacy=None):
    return {
        "dimension": dimension,
        "code": code,
        "index": index,
        "game_id": _game_key(game, index),
        "name": str(game.get("name", "")),
        "detail": str(detail),
        "points": int(points),
        "reason": reason or code.replace("_", " "),
        "legacy": legacy,  # {"type","detail"} for the v1 /api/health shape, else None
    }


def _detect_for_game(game, index, ctx) -> list[dict[str, Any]]:
    """Detect every health issue for one game. Order mirrors the old health()."""
    issues: list[dict[str, Any]] = []
    probe = ctx["probe"]
    seen = ctx["seen"]
    helpers = ctx["helpers"]
    state = ctx["state"]
    games = ctx["games"]

    # ── Duplicates (seen-set logic, byte-identical to the old health()) ──
    gid = game.get("game_id") or game.get("id")
    if helpers:
        identity = helpers["dup_index"].get(gid)
        if not identity:
            identity = (
                helpers["cross_source_identity"](game)
                or helpers["normalize_identity"](game)
                or helpers["legacy_identity"](game)
            )
    else:
        try:
            from pkg.state.imports import game_identity as legacy_identity
        except ImportError:
            def legacy_identity(game):  # noqa: D103 - minimal fallback
                return ("path", str(Path(game.get("path", "")).expanduser()))
        identity = legacy_identity(game)
    if identity in seen:
        match_name = games[seen[identity]].get("name", "")
        issues.append(_issue(
            "duplicates", "duplicate", index, game, points=DEDUCT_DUPLICATE,
            detail=f"Matches {match_name}; identity {identity}",
            reason="Duplicate entry",
            legacy={"type": "Duplicate", "detail": f"Matches {match_name}; identity {identity}"},
        ))
    else:
        seen[identity] = index

    # manual_entry games are exempt from every path check below.
    if game.get("manual_entry"):
        return issues

    path = Path(game.get("path", ""))
    if not game.get("path") or not probe(str(path)):
        issues.append(_issue(
            "file_integrity", "missing_game", index, game, points=DEDUCT_MISSING_GAME,
            detail=str(path), reason="Missing game file",
            legacy={"type": "Missing game", "detail": str(path)},
        ))
    if not probe(str(game.get("cover", "")), file_only=True):
        issues.append(_issue(
            "artwork", "missing_cover", index, game,
            points=ARTWORK_DEDUCTIONS["cover"], detail="No local cover image",
            reason="Missing box front",
            legacy={"type": "Missing box front", "detail": "No local cover image"},
        ))
    extra_missing = 0
    for kind in ("applications", "versions", "documents"):
        for extra in game.get(kind, []) or []:
            extra_path = str(extra.get("path", "")) if isinstance(extra, dict) else ""
            if not probe(extra_path):
                extra_missing += 1
                issues.append(_issue(
                    "file_integrity", "missing_extra", index, game,
                    points=min(DEDUCT_MISSING_EXTRA, max(0, DEDUCT_MISSING_EXTRA_CAP - (extra_missing - 1) * DEDUCT_MISSING_EXTRA)),
                    detail=extra_path, reason="Missing extra file",
                    legacy={"type": "Missing extra", "detail": extra_path},
                ))
    save_missing = 0
    for save_path in game.get("save_paths", []) or []:
        if not probe(str(save_path)):
            save_missing += 1
            issues.append(_issue(
                "file_integrity", "missing_save_path", index, game,
                points=min(DEDUCT_MISSING_SAVE_PATH, max(0, DEDUCT_MISSING_SAVE_PATH_CAP - (save_missing - 1) * DEDUCT_MISSING_SAVE_PATH)),
                detail=str(save_path), reason="Missing save path",
                legacy={"type": "Missing save path", "detail": str(save_path)},
            ))
    suffix = Path(game.get("path", "")).suffix.casefold()
    if suffix in ROM_SUFFIXES and not game.get("launch") and not (state.get("profiles") or {}).get(game.get("platform", "")):
        platform = game.get("platform", "Unspecified")
        issues.append(_issue(
            "launch_readiness", "no_emulator", index, game, points=DEDUCT_NO_EMULATOR,
            detail=platform, reason="No emulator profile",
            legacy={"type": "No emulator", "detail": platform},
        ))

    # ── Artwork coverage beyond the cover (engine-only; no v1 legacy) ──
    for field in _media_types():
        if field in _GROUP_MEDIA_FIELDS:
            continue
        value = game.get(field, "")
        present = any(probe(str(item), file_only=True) for item in value) if isinstance(value, list) else probe(str(value), file_only=True)
        if not present:
            issues.append(_issue(
                "artwork", "missing_artwork", index, game,
                points=ARTWORK_DEDUCTIONS.get(field, ARTWORK_DEDUCT_OTHER),
                detail=field, reason=f"Missing {field.replace('_', ' ')}",
            ))
    shots = game.get("screenshots") or []
    if not any(probe(str(item), file_only=True) for item in shots):
        issues.append(_issue(
            "artwork", "missing_artwork", index, game,
            points=ARTWORK_DEDUCTIONS["screenshots"], detail="screenshots",
            reason="Missing screenshots",
        ))

    # ── Metadata completeness (presence-only, no quality judgment) ──
    missing_fields = [field for field in METADATA_FIELDS if not str(game.get(field, "") or "").strip()]
    for field in missing_fields:
        issues.append(_issue(
            "metadata", "missing_metadata", index, game,
            points=DEDUCT_METADATA_FIELD, detail=field,
            reason=f"Missing {field}",
        ))

    # ── Launch readiness: broken flag ──
    if game.get("broken"):
        issues.append(_issue(
            "launch_readiness", "broken", index, game, points=DEDUCT_BROKEN,
            detail="Flagged broken", reason="Flagged broken",
        ))

    return issues


def detect_issues(games, state, *, probe: Callable | None = None) -> list[dict[str, Any]]:
    """Full-library issue detection. Pure except for cached path probes."""
    games = list(games or [])
    helpers = _identity_helpers()
    dup_index: dict[Any, Any] = {}
    if helpers:
        try:
            groups = helpers["detect_duplicate_identities"](games, include_cross_source=True)
        except Exception:
            groups = []
        for group in groups:
            for member in group.get("games", []):
                dup_index[member] = group.get("identity")
        helpers = {**helpers, "dup_index": dup_index}
    ctx = {
        "probe": probe or _default_probe,
        "seen": {},
        "helpers": helpers,
        "state": state or {},
        "games": games,
    }
    issues: list[dict[str, Any]] = []
    for index, game in enumerate(games):
        if not isinstance(game, dict):
            continue
        issues.extend(_detect_for_game(game, index, ctx))
    return issues


def _rescore(deductions: list[dict[str, Any]], game_count: int) -> dict[str, Any]:
    dim_points = {dim: 0 for dim in DIMENSIONS}
    dim_counts = {dim: 0 for dim in DIMENSIONS}
    for deduction in deductions:
        dim = deduction.get("dimension")
        if dim in dim_points:
            dim_points[dim] += int(deduction.get("points", 0) or 0)
            dim_counts[dim] += 1
    dimensions = {}
    for dim in DIMENSIONS:
        sub = max(0, 100 - dim_points[dim])
        dimensions[dim] = {
            "score": sub,
            "weight": DIMENSION_WEIGHTS[dim],
            "issues": dim_counts[dim],
        }
    total = round(sum(dimensions[dim]["score"] * DIMENSION_WEIGHTS[dim] for dim in DIMENSIONS) / 100)
    return {
        "score": max(0, min(100, total)),
        "dimensions": dimensions,
        "deductions": deductions,
        "game_count": int(game_count or 0),
    }


def score_library(games, state, *, probe: Callable | None = None) -> dict[str, Any]:
    """Full recompute: detect everything, then score. Returns a ScoreSnapshot."""
    games = list(games or [])
    deductions = [
        {
            "dimension": issue["dimension"],
            "points": issue["points"],
            "reason": issue["reason"],
            "game_id": issue["game_id"],
            "game_ids": [issue["game_id"]],
            "code": issue["code"],
            "detail": issue["detail"],
            "index": issue["index"],
            "name": issue["name"],
        }
        for issue in detect_issues(games, state, probe=probe)
    ]
    snapshot = _rescore(deductions, len(games))
    snapshot.update({
        "computed_at": _utcnow_iso(),
        "dirty": False,
        "full": True,
    })
    return snapshot


def merge_incremental(snapshot, games, state, dirty_ids, *, probe: Callable | None = None) -> dict[str, Any]:
    """Recompute only dirty games and merge into a cached snapshot.

    ``dirty_ids`` is an iterable of game ids (str). Deductions for those games
    are dropped and recomputed; everything else is kept. Returns a new snapshot.
    """
    games = list(games or [])
    dirty = {str(item) for item in dirty_ids or []}
    if not dirty:
        merged = dict(snapshot or {})
        merged["dirty"] = False
        return merged
    kept = [
        deduction for deduction in (snapshot or {}).get("deductions", [])
        if not (set(map(str, deduction.get("game_ids", []))) & dirty)
    ]
    by_id = {}
    for index, game in enumerate(games):
        if isinstance(game, dict):
            by_id[_game_key(game, index)] = (index, game)
    helpers = _identity_helpers()
    dup_index: dict[Any, Any] = {}
    if helpers:
        try:
            groups = helpers["detect_duplicate_identities"](games, include_cross_source=True)
        except Exception:
            groups = []
        for group in groups:
            for member in group.get("games", []):
                dup_index[member] = group.get("identity")
        helpers = {**helpers, "dup_index": dup_index}
    ctx = {
        "probe": probe or _default_probe,
        "seen": {},
        "helpers": helpers,
        "state": state or {},
        "games": games,
    }
    # Rebuild the seen-set from clean games so duplicate flags stay consistent.
    for index, game in enumerate(games):
        if not isinstance(game, dict) or _game_key(game, index) in dirty:
            continue
        _detect_for_game(game, index, ctx)
    fresh: list[dict[str, Any]] = []
    for gid in dirty:
        entry = by_id.get(gid)
        if entry is None:
            continue
        index, game = entry
        # Remove this game's own contribution from seen before re-detecting it,
        # so a dirty game never flags itself as its own duplicate.
        fresh.extend(_detect_for_game(game, index, ctx))
    deductions = kept + [
        {
            "dimension": issue["dimension"],
            "points": issue["points"],
            "reason": issue["reason"],
            "game_id": issue["game_id"],
            "game_ids": [issue["game_id"]],
            "code": issue["code"],
            "detail": issue["detail"],
            "index": issue["index"],
            "name": issue["name"],
        }
        for issue in fresh
    ]
    snapshot_out = _rescore(deductions, len(games))
    snapshot_out.update({
        "computed_at": _utcnow_iso(),
        "dirty": False,
        "full": bool((snapshot or {}).get("full", True)),
    })
    return snapshot_out


# ── Health cache (additive state key) ────────────────────────────────────────

def get_health_cache(state: dict[str, Any]) -> dict[str, Any]:
    cache = (state or {}).get(_HEALTH_CACHE_KEY)
    return dict(cache) if isinstance(cache, dict) else {}


def store_health_cache(state: dict[str, Any], snapshot: dict[str, Any]) -> None:
    state[_HEALTH_CACHE_KEY] = dict(snapshot or {})


def mark_dirty_ids(state: dict[str, Any], game_ids) -> None:
    """Record changed game ids; overflow forces a full rescan on next read."""
    cache = state.setdefault(_HEALTH_CACHE_KEY, {})
    dirty = cache.setdefault("dirty_ids", [])
    if not isinstance(dirty, list):
        dirty = cache["dirty_ids"] = []
    for gid in game_ids or []:
        gid = str(gid)
        if gid and gid not in dirty:
            dirty.append(gid)
    if len(dirty) > _DIRTY_IDS_CAP:
        cache["dirty_ids"] = []
        cache["full_rescan_needed"] = True


def take_dirty_ids(state: dict[str, Any]) -> list[str]:
    cache = state.get(_HEALTH_CACHE_KEY)
    if not isinstance(cache, dict):
        return []
    dirty = cache.pop("dirty_ids", []) or []
    cache.pop("full_rescan_needed", None)
    return [str(item) for item in dirty if str(item)]


# ── Fix journal (bounded, pruned like trash) ─────────────────────────────────

def record_fix(state: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    journal = state.setdefault(_FIX_JOURNAL_KEY, [])
    if not isinstance(journal, list):
        journal = state[_FIX_JOURNAL_KEY] = []
    record = dict(entry or {})
    record.setdefault("at", _utcnow_iso())
    record.setdefault("undone", False)
    journal.append(record)
    while len(journal) > _FIX_JOURNAL_CAP:
        journal.pop(0)
    return record


def get_fix_journal(state: dict[str, Any]) -> list[dict[str, Any]]:
    journal = (state or {}).get(_FIX_JOURNAL_KEY)
    return [dict(item) for item in journal] if isinstance(journal, list) else []


def mark_fix_undone(state: dict[str, Any], fix_id: str) -> bool:
    journal = (state or {}).get(_FIX_JOURNAL_KEY)
    if not isinstance(journal, list):
        return False
    for item in journal:
        if isinstance(item, dict) and str(item.get("fix_id")) == str(fix_id):
            item["undone"] = True
            return True
    return False


# ── Scheduled rescan ─────────────────────────────────────────────────────────

def normalize_rescan_setting(value: Any) -> str:
    value = str(value or "").strip().lower()
    return value if value in RESCAN_OPTIONS else DEFAULT_RESCAN


def rescan_due(settings: dict[str, Any], now: datetime | None = None) -> bool:
    """True when the scheduled health rescan should run now."""
    mode = normalize_rescan_setting((settings or {}).get("health_rescan", DEFAULT_RESCAN))
    if mode in {"off", "on_startup"}:
        return False
    now = now or datetime.now(timezone.utc)
    last = str((settings or {}).get("last_health_rescan") or "").strip()
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    interval_days = 1 if mode == "daily" else 7
    return (now - last_dt).total_seconds() >= interval_days * 86400


__all__ = [
    "ARTWORK_DEDUCTIONS",
    "DEFAULT_RESCAN",
    "DIMENSION_WEIGHTS",
    "DIMENSIONS",
    "METADATA_FIELDS",
    "RESCAN_OPTIONS",
    "ROM_SUFFIXES",
    "detect_issues",
    "get_fix_journal",
    "get_health_cache",
    "mark_dirty_ids",
    "mark_fix_undone",
    "merge_incremental",
    "normalize_rescan_setting",
    "record_fix",
    "rescan_due",
    "score_library",
    "store_health_cache",
    "take_dirty_ids",
]
