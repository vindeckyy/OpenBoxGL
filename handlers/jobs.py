"""JobsHandlers capability handlers. v2 jobs API and legacy /api/jobs adapter."""

from urllib.parse import parse_qs

from api_errors import BadRequest, JobNotCancellable, JobNotResumable, JobStateConflict, NotFound
from pkg.state.operations import (
    JobNotCancellableError,
    JobNotResumableError,
    JobStateConflictError,
    get_operation_service,
)
from routes.registry import route
from webapp_state import JOB_MANAGER


def _parse_limit(raw, *, default: int, maximum: int) -> int:
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise BadRequest("limit must be an integer.") from error
    return max(1, min(value, maximum))


def _operation_error(error: Exception):
    code = getattr(error, "code", "INTERNAL_ERROR")
    if code == "JOB_NOT_CANCELLABLE":
        raise JobNotCancellable(str(error)) from error
    if code == "JOB_NOT_RESUMABLE":
        raise JobNotResumable(str(error)) from error
    if code == "JOB_STATE_CONFLICT":
        raise JobStateConflict(str(error)) from error
    raise JobStateConflict(str(error)) from error


class JobsHandlers:
    @route("GET", ["/api/jobs", "/api/v1/jobs"], v1=True)
    def _api_get_api_jobs(self, parsed):
        self.send_json(200, {
            "jobs": JOB_MANAGER.snapshots(),
            "history": JOB_MANAGER.history(),
        })

    @route("GET", "/api/v2/jobs")
    def _api_get_api_v2_jobs(self, parsed):
        query = parse_qs(parsed.query)
        cursor = query.get("cursor", [None])[0]
        limit = _parse_limit(query.get("limit", [None])[0], default=50, maximum=100)
        type_filter = (query.get("type", [None])[0] or query.get("type_filter", [None])[0])
        state_filter = (query.get("state", [None])[0] or query.get("state_filter", [None])[0])
        root_job_id = query.get("root_job_id", [None])[0]
        if type_filter == "":
            type_filter = None
        if state_filter == "":
            state_filter = None
        if root_job_id == "":
            root_job_id = None
        payload = get_operation_service().list_jobs(
            cursor=cursor,
            limit=limit,
            type_filter=type_filter,
            state_filter=state_filter,
            root_job_id=root_job_id,
        )
        # Expose grouping helper for clients that request it
        if query.get("group_by", [None])[0] == "root_job_id":
            grouped = get_operation_service().group_jobs_by_root(payload.get("jobs") or [])
            payload["grouped_by_root"] = grouped
        self.send_json(200, payload)

    @route("GET", "/api/v2/jobs/items")
    def _api_get_api_v2_jobs_items(self, parsed):
        query = parse_qs(parsed.query)
        job_id = str(query.get("job_id", [""])[0]).strip()
        if not job_id:
            raise BadRequest("job_id is required.")
        if get_operation_service().get(job_id) is None:
            raise NotFound("Job not found.")
        cursor = query.get("cursor", [None])[0]
        limit = _parse_limit(query.get("limit", [None])[0], default=50, maximum=200)
        payload = get_operation_service().list_items(job_id, cursor=cursor, limit=limit)
        self.send_json(200, payload)

    @route("POST", "/api/v2/jobs/cancel")
    def _api_post_api_v2_jobs_cancel(self, payload):
        job_id = str((payload or {}).get("job_id") or "").strip()
        if not job_id:
            raise BadRequest("job_id is required.")
        service = get_operation_service()
        try:
            result = service.request_cancel(job_id)
        except JobNotCancellableError as error:
            raise JobNotCancellable(str(error)) from error
        except JobStateConflictError as error:
            raise NotFound(str(error)) from error
        JOB_MANAGER.cancel_by_id(job_id)
        self.send_json(202, {"job_id": result["job_id"], "state": result["state"]})

    @route("POST", "/api/v2/jobs/retry")
    def _api_post_api_v2_jobs_retry(self, payload):
        payload = payload or {}
        job_id = str(payload.get("job_id") or "").strip()
        if not job_id:
            raise BadRequest("job_id is required.")
        service = get_operation_service()
        try:
            if payload.get("input") is not None:
                original = service.get(job_id)
                if original is None:
                    raise NotFound("Job not found.")
                retry = service.create(
                    operation_type=original["type"],
                    title=original["title"],
                    input_data=payload.get("input") or {},
                    retry_of=original["job_id"],
                    root_job_id=original["root_job_id"],
                )
            else:
                retry = service.retry(job_id)
        except JobStateConflictError as error:
            _operation_error(error)
        self.send_json(202, {
            "job_id": retry["job_id"],
            "root_job_id": retry["root_job_id"],
            "retry_of": retry["retry_of"],
            "state": retry["state"],
        })

    @route("POST", "/api/v2/jobs/resume")
    def _api_post_api_v2_jobs_resume(self, payload):
        job_id = str((payload or {}).get("job_id") or "").strip()
        if not job_id:
            raise BadRequest("job_id is required.")
        service = get_operation_service()
        try:
            resumed = service.resume(job_id)
        except JobNotResumableError as error:
            raise JobNotResumable(str(error)) from error
        except JobStateConflictError as error:
            raise NotFound(str(error)) from error
        self.send_json(202, {
            "job_id": resumed["job_id"],
            "resume_of": resumed["resume_of"],
            "state": resumed["state"],
        })
