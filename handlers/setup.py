"""SetupHandlers capability handlers. Setup Center preview/commit v2 routes."""

from __future__ import annotations

from urllib.parse import parse_qs

from api_errors import BadRequest, PreviewExpired, PreviewNotFound
from openbox import load_state
from pkg.parity.parity_setup_preview import (
    DEFAULT_ITEMS_LIMIT,
    MAX_ITEMS_LIMIT,
    apply_decisions,
    commit_preview,
    compute_summary,
    create_preview_record,
    list_preview_items,
    load_preview,
    preview_document,
    revalidate_preview_record,
    run_scan_job,
    save_preview,
)
from pkg.state.operations import get_operation_service
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


def _preview_state_for_job(job_id: str | None) -> str:
    if not job_id:
        return "ready"
    operation = get_operation_service().get(job_id)
    if not operation:
        return "ready"
    state = str(operation.get("state") or "ready")
    if state in {"queued", "running", "cancelling"}:
        return state
    if state in {"done", "partial"}:
        return "ready"
    if state == "error":
        return "ready"
    return state


class SetupHandlers:
    @route("GET", "/api/v2/setup/summary")
    def _api_get_api_v2_setup_summary(self, parsed):
        self.send_json(200, compute_summary(state=load_state()))

    @route("POST", "/api/v2/setup/preview")
    def _api_post_api_v2_setup_preview(self, payload):
        payload = payload or {}
        sources = payload.get("sources")
        if not isinstance(sources, list) or not sources:
            raise BadRequest("sources must be a non-empty array.")
        options = payload.get("options") or {}
        if not isinstance(options, dict):
            raise BadRequest("options must be an object.")
        preview = create_preview_record(sources=sources, options=options)
        preview["_request_sources"] = sources
        preview["state"] = "queued"
        save_preview(preview)

        def worker(cancel_event):
            return run_scan_job(preview["preview_id"], cancel_event=cancel_event)

        job = JOB_MANAGER.submit(
            f"setup-scan:{preview['preview_id']}",
            worker,
            replace=True,
            operation_type="setup.scan",
            input_data={"preview_id": preview["preview_id"]},
        )
        preview = load_preview(preview["preview_id"], allow_expired=True)
        preview["job_id"] = job["job_id"]
        preview["state"] = _preview_state_for_job(job["job_id"])
        save_preview(preview)
        self.send_json(
            202,
            {
                "preview_id": preview["preview_id"],
                "revision": preview["revision"],
                "job_id": job["job_id"],
                "expires_at": preview["expires_at"],
                "state": preview["state"],
            },
        )

    @route("GET", "/api/v2/setup/preview")
    def _api_get_api_v2_setup_preview(self, parsed):
        query = parse_qs(parsed.query)
        preview_id = str(query.get("preview_id", [""])[0]).strip()
        if not preview_id:
            raise BadRequest("preview_id is required.")
        try:
            preview = load_preview(preview_id)
        except PreviewNotFound:
            raise
        except PreviewExpired:
            raise
        doc = preview_document(preview)
        doc["state"] = _preview_state_for_job(preview.get("job_id")) if preview.get("state") != "ready" else preview.get("state", "ready")
        if preview.get("job_id") and doc["state"] in {"queued", "running", "cancelling"}:
            pass
        elif preview.get("state") == "ready":
            doc["state"] = "ready"
        self.send_json(200, doc)

    @route("GET", "/api/v2/setup/preview/items")
    def _api_get_api_v2_setup_preview_items(self, parsed):
        query = parse_qs(parsed.query)
        preview_id = str(query.get("preview_id", [""])[0]).strip()
        if not preview_id:
            raise BadRequest("preview_id is required.")
        cursor = query.get("cursor", [None])[0]
        limit = _parse_limit(query.get("limit", [None])[0], default=DEFAULT_ITEMS_LIMIT, maximum=MAX_ITEMS_LIMIT)
        payload = list_preview_items(preview_id, cursor=cursor, limit=limit)
        self.send_json(200, payload)

    @route("POST", "/api/v2/setup/preview/decisions")
    def _api_post_api_v2_setup_preview_decisions(self, payload):
        payload = payload or {}
        preview_id = str(payload.get("preview_id") or "").strip()
        if not preview_id:
            raise BadRequest("preview_id is required.")
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            raise BadRequest("items must be a non-empty array.")
        result = apply_decisions(preview_id, items)
        self.send_json(200, {"preview_id": preview_id, **result})

    @route("POST", "/api/v2/setup/preview/revalidate")
    def _api_post_api_v2_setup_preview_revalidate(self, payload):
        preview_id = str((payload or {}).get("preview_id") or "").strip()
        if not preview_id:
            raise BadRequest("preview_id is required.")
        load_preview(preview_id)

        def worker(cancel_event):
            revalidate_preview_record(preview_id)
            return {"revalidated": True}

        job = JOB_MANAGER.submit(
            f"setup-revalidate:{preview_id}",
            worker,
            replace=True,
            operation_type="setup.revalidate",
            input_data={"preview_id": preview_id},
        )
        updated = load_preview(preview_id, allow_expired=True)
        updated["job_id"] = job["job_id"]
        save_preview(updated)
        self.send_json(
            202,
            {
                "preview_id": preview_id,
                "revision": updated["revision"],
                "job_id": job["job_id"],
                "state": _preview_state_for_job(job["job_id"]),
            },
        )

    @route("POST", "/api/v2/setup/commit")
    def _api_post_api_v2_setup_commit(self, payload):
        payload = payload or {}
        preview_id = str(payload.get("preview_id") or "").strip()
        if not preview_id:
            raise BadRequest("preview_id is required.")
        revision = int(payload.get("revision") or 0)
        options = payload.get("options") or {}
        emulator_choices = payload.get("emulator_choices")
        if emulator_choices is None:
            raise BadRequest("emulator_choices is required.")
        if not isinstance(emulator_choices, list):
            raise BadRequest("emulator_choices must be an array.")
        load_preview(preview_id)
        import_batch_id = __import__("uuid").uuid4().hex

        def worker(cancel_event):
            return commit_preview(
                preview_id,
                revision=revision,
                options=options,
                emulator_choices=emulator_choices,
                import_batch_id=import_batch_id,
            )

        job = JOB_MANAGER.submit(
            f"setup-commit:{preview_id}",
            worker,
            replace=True,
            operation_type="setup.commit",
            input_data={"preview_id": preview_id, "revision": revision},
        )
        self.send_json(
            202,
            {
                "job_id": job["job_id"],
                "import_batch_id": import_batch_id,
                "preview_id": preview_id,
                "revision": revision,
            },
        )
