"""Launch Audit (F1): the Launch Doctor run across the whole library, grouped by cause.

A user who imported 2,000 ROMs had no way to ask "how many of these launch, and
why not the rest?" short of trying them. The Doctor already answers that for one
game; this module answers it for all of them and groups the answers by root
cause -- "RetroArch is not installed: 412 games" rather than 412 rows.

Two rules keep it honest:

- **One taxonomy.** Every check comes from ``run_preflight_checks`` unchanged,
  so the audit's codes are the Doctor's codes by construction and the two can
  never disagree about a game.
- **Memoize the probes, not the answers.** The Doctor spawns ``flatpak info``
  (and ``flatpak info --show-permissions``) per game. ``ProbeCache`` caches the
  *subprocess result* per argv, so each distinct app id costs at most two
  spawns however many games use it, and the per-ROM filesystem-grant decision is
  still made per ROM from the cached permission text. ``which`` is cached the
  same way. A 20,000-game audit goes from up to 40,000 spawns to at most 48.

The audit never mutates the library. Results live in ``cache/launch_audit.json``
beside the data directory, not in ``library.json``, so a large report does not
grow every state write.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from backend_io import atomic_write_text
from pkg.parity.parity_launch_doctor import _derive_status, run_preflight_checks

LOGGER = logging.getLogger(__name__)

AUDIT_FILE_NAME = "launch_audit.json"
PROGRESS_EVERY = 50
#: Members recorded per failed-entry sample; the count is always exact.
ERROR_SAMPLE = 20
#: Payload keys that name one game rather than the cause. A group's fix action
#: is only offered when it is identical for every member once these are ignored.
_PER_GAME_KEYS = ("path", "template", "name")
_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}


class ProbeCache:
    """Memoized ``which`` and ``run`` for one audit pass, with real-call counters.

    ``run`` caches the completed process (or the exception) per argv, so the
    Doctor's own parsing of the permission text still runs per ROM.
    """

    def __init__(self, which=None, run=None):
        self._which = which or shutil.which
        self._run = run or subprocess.run
        self._which_results: dict[str, object] = {}
        self._run_results: dict[tuple, object] = {}
        self.which_misses = 0
        self.spawns = 0

    def which(self, name):
        key = str(name)
        if key not in self._which_results:
            self.which_misses += 1
            self._which_results[key] = self._which(key)
        return self._which_results[key]

    def run(self, argv, **kwargs):
        key = tuple(str(part) for part in argv)
        if key not in self._run_results:
            self.spawns += 1
            try:
                self._run_results[key] = self._run(argv, **kwargs)
            except (subprocess.TimeoutExpired, OSError) as error:
                self._run_results[key] = error
        result = self._run_results[key]
        if isinstance(result, BaseException):
            raise result
        return result


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cause_payload(fix_action) -> dict:
    fix = fix_action or {}
    payload = dict(fix.get("payload") or {})
    # A folder grant is one permission shared by every game in that folder, so its path
    # is part of the cause: games in two folders get two groups, and a group's one button knows its folder.
    keep_path = fix.get("kind") == "flatpak_grant"
    for key in _PER_GAME_KEYS:
        if key == "path" and keep_path:
            continue
        payload.pop(key, None)
    return payload


def _subject(check) -> str:
    """What the cause is about: the app to install, the core, the extension."""
    payload = (check.get("fix_action") or {}).get("payload") or {}
    return str(
        payload.get("app_id") or payload.get("core") or payload.get("adapter_id")
        or payload.get("extension") or ""
    )


def _group_key(check) -> str:
    fix = check.get("fix_action") or {}
    key = f"{check.get('code', '')}|{fix.get('kind', '')}|{_subject(check)}"
    # A folder grant is per folder: games in two folders are two causes, each with its own grant button.
    if fix.get("kind") == "flatpak_grant" and (fix.get("payload") or {}).get("path"):
        key += f"|{fix['payload']['path']}"
    return key


def audit_library(games, profiles, data_dir, *, which=None, run=None, deep=False,
                  progress=None, is_cancelled=None):
    """Audit every game. Returns the report dict, or ``None`` when cancelled.

    ``deep`` turns on the checks that open files (archive listing, BIOS hashing);
    it is off by default because it is the only part of the audit whose cost
    grows with the size of the ROMs rather than the number of games.
    """
    probes = ProbeCache(which, run)
    groups: dict[str, dict] = {}
    totals = {"ready": 0, "warning": 0, "blocked": 0}
    failed = {"count": 0, "sample": []}
    total = len(games)
    for index, game in enumerate(games):
        if is_cancelled is not None and is_cancelled():
            return None
        if progress is not None and index % PROGRESS_EVERY == 0:
            progress(phase="audit", current=index, total=total)
        if not isinstance(game, dict):
            continue
        member = {"game_id": str(game.get("game_id") or ""), "index": index, "name": str(game.get("name") or "")}
        try:
            checks = run_preflight_checks(game, profiles, data_dir, which=probes.which, run=probes.run, deep=deep)
        except Exception:  # one malformed entry must not sink a 20k-game audit
            LOGGER.exception("Launch audit failed for game %s", member["game_id"] or index)
            failed["count"] += 1
            if len(failed["sample"]) < ERROR_SAMPLE:
                failed["sample"].append(member)
            continue
        totals[_derive_status(checks)] += 1
        _record_checks(groups, member, checks)
    if progress is not None:
        progress(phase="audit", current=total, total=total)
    return {
        "computed_at": _utcnow_iso(),
        "game_count": total,
        "deep": bool(deep),
        "totals": totals,
        "groups": _ordered_groups(groups),
        "failed": failed,
        "probes": {"flatpak_spawns": probes.spawns, "which_misses": probes.which_misses},
    }


def _record_checks(groups: dict, member: dict, checks) -> None:
    """Add one game's checks to the group map. The full audit and a refresh share this rule."""
    seen = set()
    for check in checks:
        key = _group_key(check)
        if key in seen:
            continue
        seen.add(key)
        cause = _cause_payload(check.get("fix_action"))
        group = groups.get(key)
        if group is None:
            fix = check.get("fix_action")
            groups[key] = group = {
                "key": key,
                "code": check.get("code", ""),
                "subject": _subject(check),
                "severity": check.get("severity", "info"),
                "message": check.get("message", ""),
                "fix_action": {**fix, "payload": cause} if fix else None,
                "_cause": cause,
                "games": [],
            }
        elif group["fix_action"] is not None and cause != group["_cause"]:
            group["fix_action"] = None
        group["games"].append(member)


def _ordered_groups(groups: dict) -> list:
    ordered = sorted(
        groups.values(),
        key=lambda item: (_SEVERITY_RANK.get(item["severity"], 3), -len(item["games"]), item["key"]),
    )
    for group in ordered:
        group.pop("_cause", None)
        group["count"] = len(group["games"])
    return ordered


_STATUS_RANK = {"blocked": 0, "warning": 1, "ready": 2}


def _statuses_from_report(report) -> dict:
    """Each game's status as the report records it. A game in no group is ready."""
    statuses: dict[str, str] = {}
    for group in report.get("groups", []):
        status = _STATUS_FOR_SEVERITY.get(group.get("severity"), "ready")
        for member in group.get("games", []):
            game_id = str(member.get("game_id") or "")
            if game_id and _STATUS_RANK[status] < _STATUS_RANK[statuses.get(game_id, "ready")]:
                statuses[game_id] = status
    return statuses


def _same_fix(old, new) -> bool:
    if old is None or new is None:
        return False
    return old.get("kind") == new.get("kind") and old.get("payload") == new.get("payload")


def refresh_report(report, games, profiles, data_dir, *, game_ids, which=None, run=None) -> dict:
    """Re-check only ``game_ids`` and merge the results into a report of this library.

    The caller must have checked that the report describes the library as it is now (same
    game count); otherwise the totals would be wrong. Each re-checked game's old groups are
    replaced by its new checks, groups left empty are dropped, and the totals move by the
    game's old and new status. A game whose check raises keeps its old result. Refreshing
    every game gives the same groups and totals as a full audit (tests pin this).
    """
    positions: dict[str, int] = {}
    for index, game in enumerate(games):
        if isinstance(game, dict) and str(game.get("game_id") or ""):
            positions.setdefault(str(game["game_id"]), index)
    wanted = [item for item in dict.fromkeys(str(value) for value in game_ids) if item in positions]
    old_status = _statuses_from_report(report)
    deep = bool(report.get("deep"))
    probes = ProbeCache(which, run)
    fresh: dict[str, dict] = {}
    new_status: dict[str, str] = {}
    refreshed: set[str] = set()
    for game_id in wanted:
        index = positions[game_id]
        game = games[index]
        member = {"game_id": game_id, "index": index, "name": str(game.get("name") or "")}
        try:
            checks = run_preflight_checks(game, profiles, data_dir, which=probes.which, run=probes.run, deep=deep)
        except Exception:  # keep the game's previous result rather than guessing a new one
            LOGGER.exception("Launch audit refresh failed for game %s", game_id)
            continue
        refreshed.add(game_id)
        new_status[game_id] = _derive_status(checks)
        _record_checks(fresh, member, checks)

    totals = dict(report.get("totals") or {"ready": 0, "warning": 0, "blocked": 0})
    for game_id in refreshed:
        totals[old_status.get(game_id, "ready")] -= 1
        totals[new_status[game_id]] += 1

    merged: dict[str, dict] = {}
    for group in report.get("groups", []):
        members = [m for m in group.get("games", []) if str(m.get("game_id") or "") not in refreshed]
        if members:
            merged[group["key"]] = {**group, "games": members}
    for key, group in fresh.items():
        current = merged.get(key)
        if current is None:
            merged[key] = group
            continue
        if not _same_fix(current.get("fix_action"), group.get("fix_action")):
            current["fix_action"] = None
        current["games"] = current["games"] + group["games"]

    return {
        "computed_at": _utcnow_iso(),
        "game_count": int(report.get("game_count", 0)),
        "deep": deep,
        "totals": totals,
        "groups": _ordered_groups(merged),
        "failed": dict(report.get("failed") or {"count": 0, "sample": []}),
        "probes": {"flatpak_spawns": probes.spawns, "which_misses": probes.which_misses},
    }


def audit_path(data_dir) -> Path:
    return Path(data_dir) / "cache" / AUDIT_FILE_NAME


def store_audit(data_dir, report) -> None:
    path = audit_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(report, ensure_ascii=False, separators=(",", ":")))


def totals_change_notice(previous, current) -> dict | None:
    """The notification for a change in blocked or warning totals, or None.

    The first audit has nothing to compare with and says nothing. The key carries the
    totals, so the feed keeps one notice per distinct result.
    """
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return None
    before = previous.get("totals") or {}
    after = current.get("totals") or {}
    before_blocked, before_warning = int(before.get("blocked", 0)), int(before.get("warning", 0))
    blocked, warning = int(after.get("blocked", 0)), int(after.get("warning", 0))
    if (before_blocked, before_warning) == (blocked, warning):
        return None
    return {
        "level": "warning" if blocked > before_blocked else "info",
        "title": f"Launch check changed: {blocked} won't launch, {warning} need attention",
        "body": (
            f"Won't launch: {blocked} (was {before_blocked}). "
            f"Needs attention: {warning} (was {before_warning})."
        ),
        # One key per check, so a return to an earlier result is a new notice, not a suppressed one.
        "dedupe_key": f"launch-audit:{current.get('computed_at', '')}",
    }


def load_audit(data_dir) -> dict | None:
    try:
        report = json.loads(audit_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return report if isinstance(report, dict) and isinstance(report.get("groups"), list) else None


def public_summary(report, *, game_count) -> dict:
    """The cheap part of the report: totals and one row per cause, no member lists."""
    if not report:
        return {"scanned": False}
    return {
        "scanned": True,
        "computed_at": report.get("computed_at", ""),
        "game_count": report.get("game_count", 0),
        # The library changed since the audit ran; the counts are a snapshot.
        "stale": int(report.get("game_count", 0)) != int(game_count),
        "deep": bool(report.get("deep")),
        "totals": report.get("totals", {}),
        "failed": int((report.get("failed") or {}).get("count", 0)),
        "groups": [
            {key: group.get(key) for key in ("key", "code", "subject", "severity", "message", "count", "fix_action")}
            for group in report.get("groups", [])
        ],
    }


_STATUS_FOR_SEVERITY = {"error": "blocked", "warning": "warning"}


def readiness_statuses(report, *, game_count) -> dict:
    """Map each game_id the cached audit flags to ``blocked`` or ``warning``.

    A game missing from the map is ready. The map is empty when there is no
    report or the library has changed since the audit (the same ``stale`` rule
    ``public_summary`` uses), so a grid badge is never drawn from old data.
    Derived from the groups the report lists, so a badge and the audit agree.
    """
    if not report or int(report.get("game_count", 0)) != int(game_count):
        return {}
    statuses: dict[str, str] = {}
    for group in report.get("groups", []):
        status = _STATUS_FOR_SEVERITY.get(group.get("severity"))
        if status is None:
            continue
        for member in group.get("games", []):
            game_id = str(member.get("game_id") or "")
            if game_id and statuses.get(game_id) != "blocked":
                statuses[game_id] = status
    return statuses


def group_members(report, key, *, offset, limit) -> dict:
    group = next((item for item in (report or {}).get("groups", []) if item.get("key") == key), None)
    if group is None:
        return {"key": key, "games": [], "total": 0, "offset": offset, "limit": limit}
    games = group.get("games", [])
    return {"key": key, "games": games[offset:offset + limit], "total": len(games), "offset": offset, "limit": limit}


__all__ = [
    "AUDIT_FILE_NAME",
    "ProbeCache",
    "audit_library",
    "audit_path",
    "group_members",
    "load_audit",
    "public_summary",
    "store_audit",
]
