"""ExportHandlers — library export (1.8.0)."""

from urllib.parse import parse_qs

from openbox import DATA, load_state
from pkg.parity.parity_export import (
    EXPORT_FORMATS,
    EXPORT_SCOPES,
    approved_export_file,
    build_export_games,
    export_rows,
    list_exports,
    write_export,
)
from routes.registry import route
from webapp_state import JOB_MANAGER


class ExportHandlers:
    @route("POST", "/api/v2/library/export")
    def _api_post_api_v2_library_export(self, payload):
        self.create_library_export(payload)

    @route("GET", "/api/v2/library/export/exports")
    def _api_get_api_v2_library_export_exports(self, parsed):
        self.send_json(200, {"exports": list_exports(DATA.parent)})
        return

    @route("GET", "/api/v2/library/export/download")
    def _api_get_api_v2_library_export_download(self, parsed):
        qs = parse_qs(parsed.query or "")
        name = (qs.get("file", [""])[0] or "").strip()
        approved = approved_export_file(DATA.parent, name)
        if not approved:
            self.send_json(404, {"error": "export not found"})
            return
        data = approved.read_bytes()
        content_type = "application/json" if approved.suffix == ".json" else "text/csv; charset=utf-8"
        # send_bytes owns Content-Length and the single-write path, so this
        # inherits the same partial-write discipline as every other response.
        self.send_bytes(200, data, content_type, extra_headers={
            "Content-Disposition": f'attachment; filename="{approved.name}"',
        })
        return

    def create_library_export(self, payload):
        fmt = str(payload.get("format", "json")).strip().lower()
        if fmt not in EXPORT_FORMATS:
            raise ValueError("Export format must be json or csv.")
        scope = str(payload.get("scope", "all")).strip().lower()
        if scope not in EXPORT_SCOPES:
            raise ValueError("Export scope must be all, platform, or playlist.")
        scope_name = str(payload.get("scope_name", "")).strip()
        if scope != "all" and not scope_name:
            raise ValueError(f"Export scope {scope} requires a name.")
        include_media_paths = bool(payload.get("include_media_paths"))

        def worker(_cancel_event):
            state = load_state()
            games = build_export_games(state, scope, scope_name)
            rows = export_rows(games, include_media_paths=include_media_paths)
            path = write_export(DATA.parent, rows, fmt=fmt, include_media_paths=include_media_paths)
            return {"file": path.name, "count": len(rows)}

        job = JOB_MANAGER.submit("library-export", worker)
        self.send_json(202, {"state": "queued", "job_id": job["job_id"]})
