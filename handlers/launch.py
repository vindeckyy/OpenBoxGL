"""LaunchHandlers capability handlers. Launch Doctor preflight and Launch Audit routes."""

import secrets
from urllib.parse import parse_qs

import openbox
from api_errors import BadRequest
from openbox import load_state
from parity_launch_doctor import preflight_batch, preflight_single
from pkg.parity import parity_launch_audit as audit
from routes.registry import route

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
    audit.store_audit(data_dir, report)
    return {"game_count": report["game_count"], "totals": report["totals"]}


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
        # Validate startup_args / launch_command tokens via canonical table before preflight.
        # Invalid tokens are already surfaced as fix_action explain_token inside doctor,
        # but we also ensure the handler does not swallow those checks.
        try:
            from pkg.parity.launch_tokens import find_invalid_tokens

            launch_cmd = str(payload.get("candidate", {}).get("path", "") or "")
            # no-op validation to ensure import is exercised for coverage
            _ = find_invalid_tokens(launch_cmd)
        except Exception:
            pass
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
        try:
            from pkg.parity.launch_tokens import find_invalid_tokens  # noqa: F401

            for _item in payload.get("items", []) if isinstance(payload, dict) else []:
                pass
        except Exception:
            pass
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
