"""LaunchHandlers capability handlers. Launch Doctor preflight and Launch Audit routes."""

import secrets
import threading
from urllib.parse import parse_qs

import openbox
from api_errors import BadRequest
from openbox import load_state
from parity_launch_doctor import preflight_batch, preflight_single
from pkg.parity import parity_launch_audit as audit
from routes.registry import route

# The cached report is read, merged and written in one step by the refresh and the full audit;
# this lock keeps two of them from overwriting each other's results.
_AUDIT_FILE_LOCK = threading.Lock()

AUDIT_JOB_NAME = "launch-audit"
AUDIT_PAGE_MAX = 500


def run_launch_audit(ctx=None, *, deep=False):
    """Background audit job: every game through the Doctor, grouped by cause.

    Reads the library once and writes only the audit cache file; it never
    mutates library.json, so cancelling it at any point leaves nothing behind
    except the previous report.
    """
    state = load_state()
    data_dir = str(openbox.DATA.parent)
    report = audit.audit_library(
        state.get("games", []) or [],
        state.get("profiles", {}) or {},
        data_dir,
        deep=deep,
        progress=ctx.progress if ctx is not None else None,
        is_cancelled=ctx.is_cancelled if ctx is not None else None,
    )
    if report is None:
        return {}
    with _AUDIT_FILE_LOCK:
        previous = audit.load_audit(data_dir)
        audit.store_audit(data_dir, report)
    notice = audit.totals_change_notice(previous, report)
    if notice is not None:
        _record_audit_notice(notice)
    return {"game_count": report["game_count"], "totals": report["totals"]}


def _record_audit_notice(notice):
    """Add a change in the audit's totals to the notification feed."""
    from notifications import add_notification
    from webapp_state import transact_state

    def mutate(state):
        add_notification(state, kind="launch_audit", source="launch-audit", **notice)

    transact_state(mutate)


def _audit_job():
    from webapp_state import JOB_MANAGER

    job = JOB_MANAGER.snapshots().get(AUDIT_JOB_NAME)
    if not job:
        return None
    return {key: job.get(key) for key in ("job_id", "state", "current", "total", "error") if key in job}


def _query_int(query, name, default, *, minimum, maximum):
    raw = (query.get(name, [""])[0] or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise BadRequest(f"{name} must be an integer.") from None
    return max(minimum, min(value, maximum))


class LaunchHandlers:
    @route("GET", "/api/v2/launch/audit")
    def _api_get_api_v2_launch_audit(self, parsed):
        """Cached Launch Audit: totals and one row per root cause. Never scans.

        ``?group=<key>`` adds that cause's games, paginated with ``offset`` and
        ``limit`` (at most 500) -- the 20k-library guard: never unbounded.
        """
        query = parse_qs(parsed.query or "")
        report = audit.load_audit(str(openbox.DATA.parent))
        game_count = len(load_state().get("games", []) or [])
        payload = audit.public_summary(report, game_count=game_count)
        payload["job"] = _audit_job()
        group = (query.get("group", [""])[0] or "").strip()
        if group:
            offset = _query_int(query, "offset", 0, minimum=0, maximum=10**9)
            limit = _query_int(query, "limit", 50, minimum=1, maximum=AUDIT_PAGE_MAX)
            payload["members"] = audit.group_members(report, group, offset=offset, limit=limit)
        self.send_json(200, payload)

    @route("GET", "/api/v2/launch/audit/status")
    def _api_get_api_v2_launch_audit_status(self, parsed):
        """Per-game readiness for the grid: ``blocked`` or ``warning`` only, never a scan.

        Ready games are absent. A report from an older library yields no statuses,
        so a badge is never drawn from data that no longer describes the library.
        """
        report = audit.load_audit(str(openbox.DATA.parent))
        game_count = len(load_state().get("games", []) or [])
        self.send_json(200, {"statuses": audit.readiness_statuses(report, game_count=game_count)})

    @route("POST", "/api/v2/launch/grant")
    def _api_post_api_v2_launch_grant(self, payload):
        """Give a Flatpak emulator read access to one folder (the Launch Readiness fix)."""
        from pkg.parity.parity_flatpak_grant import apply_grant
        self.send_json(200, apply_grant(payload.get("app_id"), payload.get("folder")))

    @route("POST", "/api/v2/launch/grant/undo")
    def _api_post_api_v2_launch_grant_undo(self, payload):
        """Remove a folder grant made by /api/v2/launch/grant."""
        from pkg.parity.parity_flatpak_grant import apply_grant
        self.send_json(200, apply_grant(payload.get("app_id"), payload.get("folder"), undo=True))

    @route("GET", "/api/v2/launch/cores")
    def _api_get_api_v2_launch_cores(self, parsed):
        """RetroArch cores installed here, for the Launch Doctor's core picker."""
        from pkg.parity.parity_emulator_defs import installed_retroarch_cores

        self.send_json(200, {"cores": installed_retroarch_cores()})

    @route("POST", "/api/v2/launch/core")
    def _api_post_api_v2_launch_core(self, payload):
        """Set the RetroArch core one game launches with; an empty core restores the default."""
        from openbox import local_only_mutation
        from pkg.parity.parity_emulator_defs import RETROARCH_CORE_CHOICE_KEY, valid_core_name
        from webapp_state import game_from_payload, transact_state

        game_id = str(payload.get("game_id") or "").strip()
        core = str(payload.get("core") or "").strip()
        if not game_id:
            raise BadRequest("game_id is required.")
        if core and not valid_core_name(core):
            raise BadRequest("That is not a RetroArch core file name.")

        def mutate(state):
            game = game_from_payload(state, {"game_id": game_id})
            if core:
                game[RETROARCH_CORE_CHOICE_KEY] = core
            else:
                game.pop(RETROARCH_CORE_CHOICE_KEY, None)
            return str(game.get("name") or "")

        _, name = transact_state(local_only_mutation(mutate))
        self.send_json(200, {"ok": True, "game_id": game_id, "core": core, "name": name})

    @route("POST", "/api/v2/launch/audit/refresh")
    def _api_post_api_v2_launch_audit_refresh(self, payload):
        """Check again only the games of one launch-check group, and merge them into the cached report.

        A group is one cause, so it is usually a handful of games; the check runs in the request. The
        report must describe the library as it is now, or the totals would be wrong, so the caller checks
        every game first when the library has changed.
        """
        key = str(payload.get("group") or "").strip()
        if not key:
            raise BadRequest("group is required.")
        data_dir = str(openbox.DATA.parent)
        report = audit.load_audit(data_dir)
        state = load_state()
        games = state.get("games", []) or []
        if report is None or int(report.get("game_count", -1)) != len(games):
            raise BadRequest("The library changed since the last check. Check every game first.")
        group = next((item for item in report.get("groups", []) if item.get("key") == key), None)
        if group is None:
            raise BadRequest("That group is no longer in the check. Check every game first.")
        ids = [str(member.get("game_id") or "") for member in group.get("games", [])]
        with _AUDIT_FILE_LOCK:
            # Read the report under the lock, so a refresh that finished meanwhile is not lost.
            report = audit.load_audit(data_dir)
            refreshed = audit.refresh_report(report, games, state.get("profiles", {}) or {}, data_dir, game_ids=ids)
            audit.store_audit(data_dir, refreshed)
        self.send_json(200, {
            "ok": True,
            "rechecked": len([item for item in ids if item]),
            "totals": refreshed["totals"],
            "computed_at": refreshed["computed_at"],
        })

    @route("POST", "/api/v2/launch/audit/scan")
    def _api_post_api_v2_launch_audit_scan(self, payload):
        """Queue a full audit (replace=True collapses a double click into one run)."""
        from webapp_state import JOB_MANAGER

        deep = bool((payload or {}).get("deep", False))
        job = JOB_MANAGER.submit(AUDIT_JOB_NAME, lambda ctx: run_launch_audit(ctx, deep=deep), replace=True)
        self.send_json(202, {"state": "queued", "job_id": job["job_id"], "deep": deep})

    @route("POST", "/api/v2/launch/preflight")
    def _api_post_api_v2_launch_preflight(self, payload):
        self.launch_preflight(payload)

    @route("POST", "/api/v2/launch/preflight/batch")
    def _api_post_api_v2_launch_preflight_batch(self, payload):
        self.launch_preflight_batch(payload)

    def launch_preflight(self, payload, *, request_id=None):
        # Token validation lives in the Doctor (explain_token fix actions), so
        # this handler only checks its input and delegates.
        if not isinstance(payload, dict):
            raise BadRequest("Request body must be a JSON object.")
        fail_on_blocked = bool(payload.get("fail_on_blocked", False))
        result = preflight_single(payload, state=load_state())
        if fail_on_blocked and result["status"] == "blocked":
            self.send_json(409, {
                "code": "LAUNCH_PREFLIGHT_BLOCKED",
                "request_id": request_id or secrets.token_hex(4),
                "status": "blocked",
                "game_id": result["game_id"],
                "candidate_id": result["candidate_id"],
                "resolved": result["resolved"],
                "checks": result["checks"],
            })
            return
        self.send_json(200, result)

    def launch_preflight_batch(self, payload, *, request_id=None):
        if not isinstance(payload, dict):
            raise BadRequest("Request body must be a JSON object.")
        fail_on_blocked = bool(payload.get("fail_on_blocked", False))
        result = preflight_batch(payload, state=load_state())
        if fail_on_blocked and result["totals"]["blocked"] > 0:
            self.send_json(409, {
                "code": "LAUNCH_PREFLIGHT_BLOCKED",
                "request_id": request_id or secrets.token_hex(4),
                "status": "blocked",
                "totals": result["totals"],
                "by_platform": result["by_platform"],
                "results": result["results"],
            })
            return
        self.send_json(200, result)
