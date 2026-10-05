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
    payload = dict((fix_action or {}).get("payload") or {})
    for key in _PER_GAME_KEYS:
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
    return f"{check.get('code', '')}|{fix.get('kind', '')}|{_subject(check)}"


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
    if progress is not None:
        progress(phase="audit", current=total, total=total)
    ordered = sorted(
        groups.values(),
        key=lambda item: (_SEVERITY_RANK.get(item["severity"], 3), -len(item["games"]), item["key"]),
    )
    for group in ordered:
        group.pop("_cause", None)
        group["count"] = len(group["games"])
    return {
        "computed_at": _utcnow_iso(),
        "game_count": total,
        "deep": bool(deep),
        "totals": totals,
        "groups": ordered,
        "failed": failed,
        "probes": {"flatpak_spawns": probes.spawns, "which_misses": probes.which_misses},
    }


def audit_path(data_dir) -> Path:
    return Path(data_dir) / "cache" / AUDIT_FILE_NAME


def store_audit(data_dir, report) -> None:
    path = audit_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(report, ensure_ascii=False, separators=(",", ":")))


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
