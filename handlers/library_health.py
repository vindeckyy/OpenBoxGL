"""Library health v2 API (Flagship 8: H2 cached API, H3 fix queue with undo).

Thin handlers over ``pkg.parity.parity_library_health`` and the existing
repair flows (duplicate merge, Artwork Doctor, missing-file repair). The
score only measures; every fix runs behind an explicit dry-run preview,
records an inverse operation in the bounded fix journal, and refuses to
execute a stale preview (``base_token`` check, mirroring Time Machine).
"""

from __future__ import annotations

import uuid
from urllib.parse import parse_qs

from api_errors import BadRequest
from routes.registry import route

import openbox
from pkg.parity import parity_library_health as engine
from pkg.parity.parity_library_sync import state_token
from webapp_state import JOB_MANAGER, clear_file_probe_cache, transact_state

SCAN_JOB_NAME = "library-health-scan"


# ── Snapshot helpers ─────────────────────────────────────────────────────────

def _public_snapshot(snapshot: dict) -> dict:
    snapshot = snapshot or {}
    return {
        "score": snapshot.get("score"),
        "dimensions": snapshot.get("dimensions", {}),
        "computed_at": snapshot.get("computed_at", ""),
        "game_count": snapshot.get("game_count", 0),
        "dirty": bool(snapshot.get("dirty", True)),
        "full": bool(snapshot.get("full", True)),
        "scanned": snapshot.get("score") is not None,
    }


def _snapshot_or_compute() -> tuple[dict, bool]:
    """Serve the cached snapshot only. GET never scans: a missing or stale
    cache returns ``scanned: False`` and the client offers a re-scan."""
    state = openbox.load_state()
    games = state.get("games", []) or []
    cache = engine.get_health_cache(state)
    dirty = cache.get("dirty_ids") or cache.get("full_rescan_needed")
    if cache.get("score") is not None and not dirty and cache.get("game_count") == len(games):
        return _public_snapshot(cache), False
    return _public_snapshot(None), False


def _deductions(state=None) -> tuple[list[dict], bool]:
    """Serve cached deductions only; never scan on GET. Returns (items, scanned)."""
    state = state or openbox.load_state()
    cache = engine.get_health_cache(state)
    if cache.get("deductions") and cache.get("game_count") == len(state.get("games", []) or []):
        return [dict(item) for item in cache["deductions"]], True
    return [], False


def _select_issues(deductions: list[dict], dimension: str, issue_ids) -> list[dict]:
    selected = [item for item in deductions if item.get("dimension") == dimension]
    if issue_ids == "all" or issue_ids is None:
        return selected
    wanted = {str(item) for item in issue_ids} if isinstance(issue_ids, list) else set()
    return [
        item for item in selected
        if str(item.get("game_id")) in wanted or str(item.get("index")) in wanted
    ]


class _Shim:
    """Minimal handler stand-in for reusing canonical route functions."""

    def __init__(self):
        self.status = None
        self.payload = None

    def authorized(self):
        return True

    def handle_unauthorized(self):
        raise BadRequest("unauthorized")

    def send_json(self, status, payload):
        self.status = status
        self.payload = payload


# ── H2 routes ────────────────────────────────────────────────────────────────

@route("GET", "/api/v2/library/health", spec="handlers.library_health.health_snapshot")
def health_snapshot(handler, parsed):
    """Cached score snapshot: total, sub-scores, counts. Cheap; never scans."""
    snapshot, recomputed = _snapshot_or_compute()
    snapshot["recomputed"] = recomputed
    handler.send_json(200, snapshot)


def run_health_scan(_cancel_event=None):
    """Background full recompute; shared by the scan route and the scheduler."""
    state = openbox.load_state()
    snapshot = engine.score_library(state.get("games", []) or [], state)

    def mutate(current):
        engine.store_health_cache(current, snapshot)
        current.setdefault("settings", {})["last_health_rescan"] = snapshot["computed_at"]

    transact_state(mutate)
    clear_file_probe_cache()
    return {"score": snapshot["score"], "game_count": snapshot["game_count"]}


@route("POST", "/api/v2/library/health/scan", spec="handlers.library_health.health_scan")
def health_scan(handler, payload):
    """Enqueue a background full recompute (replace=True collapses duplicates)."""
    job = JOB_MANAGER.submit(SCAN_JOB_NAME, run_health_scan, replace=True)
    handler.send_json(202, {"state": "queued", "job_id": job["job_id"]})


@route("GET", "/api/v2/library/health/issues", spec="handlers.library_health.health_issues")
def health_issues(handler, parsed):
    """Paginated per-dimension issue list. The 20k-library guard: never unbounded."""
    query = parse_qs(parsed.query or "")
    dimension = (query.get("dimension", [""])[0] or "").strip()
    if dimension and dimension not in engine.DIMENSIONS:
        raise BadRequest(f"Unknown dimension: {dimension}.")
    try:
        limit = max(1, min(int(query.get("limit", ["50"])[0] or 50), 500))
    except (TypeError, ValueError):
        raise BadRequest("limit must be an integer.") from None
    try:
        offset = max(0, int(query.get("offset", ["0"])[0] or 0))
    except (TypeError, ValueError):
        raise BadRequest("offset must be an integer.") from None
    deductions, scanned = _deductions()
    if dimension:
        deductions = [item for item in deductions if item.get("dimension") == dimension]
    total = len(deductions)
    page = deductions[offset:offset + limit]
    handler.send_json(200, {
        "scanned": scanned,
        "issues": [
            {
                "game_id": str(item.get("game_id") or ""),
                "index": item.get("index"),
                "name": item.get("name", ""),
                "dimension": item.get("dimension", ""),
                "code": item.get("code", ""),
                "reason": item.get("reason", ""),
                "detail": item.get("detail", ""),
                "points": item.get("points", 0),
            }
            for item in page
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
        "dimension": dimension,
    })


# ── H3 fix queue ─────────────────────────────────────────────────────────────

def _duplicate_groups(state):
    from pkg.parity.parity_identity import detect_duplicate_identities
    return detect_duplicate_identities(state.get("games", []) or [], include_cross_source=True)


def _index_by_game_id(games):
    index_by_id = {}
    for index, game in enumerate(games):
        if isinstance(game, dict):
            for key in (game.get("game_id"), game.get("id")):
                if key:
                    index_by_id[str(key)] = index
    return index_by_id


def _plan_duplicates(state, issues):
    """Preview duplicate merges via the canonical merge_plan."""
    from pkg.parity.parity_duplicates import merge_plan
    games = state.get("games", []) or []
    index_by_id = _index_by_game_id(games)
    wanted = {str(item.get("game_id")) for item in issues}
    plans = []
    for group in _duplicate_groups(state):
        members = [index_by_id[str(gid)] for gid in group.get("games", []) if str(gid) in index_by_id]
        if len(members) < 2 or not (wanted & {str(gid) for gid in group.get("games", [])}):
            continue
        try:
            plans.append(merge_plan(state, members))
        except ValueError:
            continue
    return plans


def _execute_duplicates(state, issues):
    """Merge duplicate groups; absorbed games go to trash for undo."""
    from handlers.library import _trash_entry_for
    from pkg.parity.parity_duplicates import apply_merge
    wanted = {str(item.get("game_id")) for item in issues}
    before_ids = {
        str(entry.get("trash_id"))
        for entry in state.get("trash", []) or []
        if isinstance(entry, dict)
    }
    merged = 0
    for group in _duplicate_groups(state):
        index_by_id = _index_by_game_id(state.get("games", []) or [])
        members = [index_by_id[str(gid)] for gid in group.get("games", []) if str(gid) in index_by_id]
        if len(members) < 2 or not (wanted & {str(gid) for gid in group.get("games", [])}):
            continue
        result = apply_merge(state, members, trash_game=_trash_entry_for)
        merged += int(result.get("merged", 0))
    new_ids = [
        str(entry.get("trash_id"))
        for entry in state.get("trash", []) or []
        if isinstance(entry, dict) and str(entry.get("trash_id")) not in before_ids
    ]
    return {"merged": merged, "trash_ids": new_ids}


def _plan_artwork(state, issues):
    from pkg.parity.parity_artwork_hygiene import build_report, select_fixable
    media_root = openbox.DATA.parent / "media"
    report = build_report(state.get("games", []) or [], media_root=media_root)
    fields = sorted({str(item.get("detail") or "") for item in issues if item.get("code") == "missing_artwork"} | {"cover"})
    game_ids = [str(item.get("game_id")) for item in issues]
    targets = select_fixable(report, fields=fields, game_ids=game_ids)
    return {"targets": targets, "fields": fields}


def _execute_artwork(state, issues, plan):
    from handlers import steamgrid
    game_ids = sorted({str(item.get("game_id")) for item in issues if item.get("game_id")})
    fields = plan.get("fields") or ["cover"]
    shim = _Shim()
    steamgrid.steamgrid_hygiene_fix(shim, {"game_ids": game_ids, "fields": fields, "limit": max(len(game_ids), 1)})
    if shim.status != 202:
        raise BadRequest(f"Artwork Doctor refused the fix: {(shim.payload or {}).get('error', 'unknown')}.")
    return {"job_id": shim.payload.get("job_id"), "game_ids": game_ids, "fields": fields}


def _plan_file_integrity(state, issues, folder):
    from pkg.parity.parity_repair import plan_repair, resolve_folder, scan_candidates, scan_missing_paths
    if not folder:
        return {"needs_folder": True}
    folder_path = resolve_folder(folder)
    candidates = scan_candidates(folder_path)
    scan = scan_missing_paths(state, include_media=True)
    wanted_ids = {str(item.get("game_id")) for item in issues}
    wanted_indexes = {item.get("index") for item in issues}
    items = [
        item for item in scan["items"]
        if str(item.get("game_id") or "") in wanted_ids or item.get("id") in wanted_indexes
    ]
    plan = plan_repair(items, candidates)
    plan["folder"] = str(folder_path)
    return plan


def _execute_file_integrity(state, issues, folder, plan):
    from pkg.parity.parity_repair import apply_repair
    unambiguous = [row for row in plan.get("matches", [])]
    before = [
        {
            "index": row.get("id"),
            "game_id": str(row.get("game_id") or ""),
            "field": str(row.get("field") or ""),
            "before": str(row.get("from") or ""),
            "after": str(row.get("path") or ""),
        }
        for row in unambiguous
    ]
    result = apply_repair(state, unambiguous)
    applied = {(row.get("id"), row.get("field")) for row in result.get("applied", [])}
    inverse = [row for row in before if (row["index"], row["field"]) in applied]
    return {"updated": result.get("updated", 0), "skipped": result.get("skipped", []), "inverse": inverse}


def _build_fix_plan(state, dimension, issues, payload):
    """Dry-run preview. Never mutates."""
    if dimension == "duplicates":
        plans = _plan_duplicates(state, issues)
        return {
            "kind": "merge",
            "merges": [
                {
                    "primary": plan.get("primary"),
                    "absorbed": plan.get("absorbed"),
                    "changed_fields": plan.get("changed_fields"),
                }
                for plan in plans
            ],
            "undo": "trash",
        }
    if dimension == "artwork":
        plan = _plan_artwork(state, issues)
        return {"kind": "artwork_doctor", "targets": plan["targets"], "fields": plan["fields"], "undo": "artwork_doctor"}
    if dimension == "file_integrity":
        plan = _plan_file_integrity(state, issues, (payload or {}).get("folder"))
        if plan.get("needs_folder"):
            return {"kind": "repair_wizard", "needs_folder": True,
                    "detail": "Pick the folder the files moved to; only unambiguous matches are relinked."}
        return {
            "kind": "relink",
            "folder": plan.get("folder"),
            "matches": [{"id": row.get("id"), "game_id": row.get("game_id"), "field": row.get("field"),
                         "from": row.get("from"), "to": row.get("path")} for row in plan.get("matches", [])],
            "ambiguous": plan.get("ambiguous", []),
            "unmatched": plan.get("unmatched", []),
            "undo": "field_restore",
            "note": "Only unambiguous matches are applied; ambiguous rows need the repair wizard.",
        }
    if dimension == "metadata":
        return {"kind": "manual", "action": "open_editor",
                "game_ids": sorted({str(item.get("game_id")) for item in issues}),
                "detail": "Metadata is never auto-filled; open the affected games in the metadata editor."}
    if dimension == "launch_readiness":
        return {"kind": "manual", "action": "open_emulator_profiles",
                "platforms": sorted({str(item.get("detail") or "") for item in issues if item.get("code") == "no_emulator"}),
                "game_ids": sorted({str(item.get("game_id")) for item in issues}),
                "detail": "Assign an emulator profile for the affected platform."}
    raise BadRequest(f"Unknown dimension: {dimension}.")


@route("POST", "/api/v2/library/health/fix", spec="handlers.library_health.health_fix")
def health_fix(handler, payload):
    """Fix plan: {dimension, issue_ids[] | "all", dry_run}. Preview or execute + undo token."""
    payload = payload if isinstance(payload, dict) else {}
    dimension = str(payload.get("dimension") or "").strip()
    if dimension not in engine.DIMENSIONS:
        raise BadRequest(f"Unknown dimension: {dimension}.")
    issue_ids = payload.get("issue_ids", "all")
    if issue_ids != "all" and not isinstance(issue_ids, list):
        raise BadRequest('issue_ids must be a list of game ids or "all".')
    dry_run = bool(payload.get("dry_run", True))

    state = openbox.load_state()
    # Fix previews run on fresh detections: this is a user-initiated POST, so a
    # bounded recompute is acceptable (GET routes never recompute).
    deductions = engine.score_library(state.get("games", []) or [], state)["deductions"]
    issues = _select_issues(deductions, dimension, issue_ids)
    if not issues:
        raise BadRequest("No fixable issues match this selection.")
    token = state_token(state)

    if dry_run:
        plan = _build_fix_plan(state, dimension, issues, payload)
        handler.send_json(200, {
            "dry_run": True,
            "dimension": dimension,
            "issue_count": len(issues),
            "base_token": token,
            "plan": plan,
        })
        return

    # Execute: a preview is mandatory, and it must not be stale.
    preview_token = str(payload.get("base_token") or "").strip()
    if not preview_token:
        raise BadRequest("Refusing to fix without a dry-run preview: pass base_token from a preview.")
    if preview_token != token:
        raise BadRequest("Fix preview is stale: the library changed after the preview. Re-run the preview.")

    fix_id = f"healthfix-{uuid.uuid4().hex[:12]}"
    if dimension == "duplicates":
        def mutate(current):
            return _execute_duplicates(current, issues)
        _, exec_result = transact_state(mutate)
        inverse = {"kind": "trash_restore", "trash_ids": exec_result.get("trash_ids", [])}
        summary = {"merged": exec_result.get("merged", 0)}
        undo_kind = "trash"
    elif dimension == "artwork":
        plan = _plan_artwork(state, issues)
        exec_result = _execute_artwork(state, issues, plan)
        inverse = {"kind": "artwork_doctor", "job_id": exec_result.get("job_id")}
        summary = {"job_id": exec_result.get("job_id"), "games": len(exec_result.get("game_ids", []))}
        undo_kind = "artwork_doctor"
    elif dimension == "file_integrity":
        folder = (payload or {}).get("folder")
        plan = _plan_file_integrity(state, issues, folder)
        if plan.get("needs_folder"):
            raise BadRequest("file_integrity fixes need a folder: preview with one first.")
        def mutate(current):
            live_plan = _plan_file_integrity(current, issues, folder)
            return _execute_file_integrity(current, issues, folder, live_plan)
        _, exec_result = transact_state(mutate)
        inverse = {"kind": "field_restore", "changes": exec_result.get("inverse", [])}
        summary = {"updated": exec_result.get("updated", 0), "skipped": len(exec_result.get("skipped", []))}
        undo_kind = "field_restore"
    else:
        plan = _build_fix_plan(state, dimension, issues, payload)
        handler.send_json(200, {"dry_run": False, "dimension": dimension, "action": plan.get("action"),
                                "detail": plan.get("detail"), "executed": False})
        return

    clear_file_probe_cache()

    def record(current):
        engine.record_fix(current, {
            "fix_id": fix_id,
            "dimension": dimension,
            "game_ids": sorted({str(item.get("game_id")) for item in issues}),
            "issue_count": len(issues),
            "inverse": inverse,
            "summary": summary,
        })

    transact_state(record)
    handler.send_json(200, {
        "dry_run": False,
        "dimension": dimension,
        "fix_id": fix_id,
        "summary": summary,
        "undo": {"kind": undo_kind, "fix_id": fix_id},
    })


@route("POST", "/api/v2/library/health/undo", spec="handlers.library_health.health_undo")
def health_undo(handler, payload):
    """Undo a fix from the journal: trash restore for destructive fixes, field restore otherwise."""
    payload = payload if isinstance(payload, dict) else {}
    fix_id = str(payload.get("fix_id") or "").strip()
    if not fix_id:
        raise BadRequest("fix_id is required.")

    state = openbox.load_state()
    journal = engine.get_fix_journal(state)
    entry = next((item for item in journal if str(item.get("fix_id")) == fix_id), None)
    if entry is None:
        raise BadRequest("Fix not found in the journal.")
    if entry.get("undone"):
        raise BadRequest("Fix was already undone.")
    inverse = entry.get("inverse") or {}
    kind = inverse.get("kind")
    undone_count = 0

    if kind == "trash_restore":
        from handlers.library import LibraryHandlers
        restore = LibraryHandlers._api_post_api_v2_library_trash_restore
        for trash_id in inverse.get("trash_ids", []) or []:
            shim = _Shim()
            try:
                restore(shim, {"trash_id": str(trash_id)})
            except BadRequest:
                continue
            if shim.status == 200:
                undone_count += 1
    elif kind == "field_restore":
        def mutate(current):
            restored = 0
            games = current.get("games", []) or []
            by_id = {}
            for index, game in enumerate(games):
                if isinstance(game, dict):
                    by_id[str(game.get("game_id") or "")] = (index, game)
            for change in inverse.get("changes", []) or []:
                target = by_id.get(str(change.get("game_id") or ""))
                if target is None:
                    continue
                _, game = target
                field = str(change.get("field") or "")
                # Only revert when nothing else changed the field since the fix.
                if str(game.get(field) or "") == str(change.get("after") or ""):
                    game[field] = str(change.get("before") or "")
                    restored += 1
            return restored
        _, undone_count = transact_state(mutate)
    elif kind == "artwork_doctor":
        job_id = inverse.get("job_id")
        batch_id = _hygiene_batch_for_job(job_id)
        if not batch_id:
            handler.send_json(200, {"undone": False, "fix_id": fix_id,
                                    "action": "artwork_doctor",
                                    "detail": "The Artwork Doctor job has no undo batch yet; undo it from the Artwork Doctor batches list."})
            return
        from handlers import steamgrid
        shim = _Shim()
        steamgrid.steamgrid_hygiene_undo(shim, {"batch_id": batch_id})
        if shim.status != 200:
            raise BadRequest(f"Artwork undo failed: {(shim.payload or {}).get('error', 'unknown')}.")
        undone_count = int((shim.payload or {}).get("restored", 0) or 0)
    else:
        raise BadRequest(f"Unknown undo kind: {kind}.")

    def mark(current):
        engine.mark_fix_undone(current, fix_id)

    transact_state(mark)
    clear_file_probe_cache()
    handler.send_json(200, {"undone": True, "fix_id": fix_id, "restored": undone_count})


def _hygiene_batch_for_job(job_id):
    """Find the Artwork Doctor undo batch_id produced by a hygiene-fix job."""
    if not job_id:
        return None
    for job in JOB_MANAGER.snapshots().values():
        if job.get("job_id") == job_id:
            result = job.get("result") or {}
            return result.get("batch_id")
    for job in JOB_MANAGER.history():
        if job.get("job_id") == job_id:
            result = job.get("result") or {}
            return result.get("batch_id")
    return None


__all__ = [
    "SCAN_JOB_NAME",
    "health_fix",
    "health_issues",
    "health_scan",
    "health_snapshot",
    "health_undo",
    "run_health_scan",
]
