//! Library export HTTP handlers (`handlers/export.py`).

use std::sync::Arc;

use axum::http::StatusCode;
use axum::response::Response;
use openbox_core::ApiError;
use openbox_sync::export::{
    EXPORT_FORMATS, EXPORT_SCOPES, approved_export_file, build_export_games, export_rows,
    list_exports, write_export,
};
use serde_json::{Map, Value, json};

use crate::handlers::shared::{RequestCtx, bool_field, str_field};
use crate::middleware::{api_error_response, bytes_response, json_response};

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    match ctx.path {
        "/api/v2/library/export/exports" => Some(json_response(
            StatusCode::OK,
            &json!({"exports": list_exports(&ctx.app.data_dir)}),
            ctx.instance,
        )),
        "/api/v2/library/export/download" => {
            let name = ctx.query_first("file").unwrap_or_default();
            let name = name.trim();
            let Some(approved) = approved_export_file(&ctx.app.data_dir, name) else {
                return Some(json_response(
                    StatusCode::NOT_FOUND,
                    &json!({"error": "export not found"}),
                    ctx.instance,
                ));
            };
            let Ok(data) = std::fs::read(&approved) else {
                return Some(json_response(
                    StatusCode::NOT_FOUND,
                    &json!({"error": "export not found"}),
                    ctx.instance,
                ));
            };
            let ct = if approved.extension().and_then(|e| e.to_str()) == Some("json") {
                "application/json"
            } else {
                "text/csv; charset=utf-8"
            };
            let fname = approved
                .file_name()
                .and_then(|s| s.to_str())
                .unwrap_or("export");
            let disp = format!("attachment; filename=\"{fname}\"");
            Some(bytes_response(
                StatusCode::OK,
                data,
                ct,
                "no-store",
                false,
                &[("Content-Disposition", &disp)],
            ))
        }
        _ => None,
    }
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    if ctx.path != "/api/v2/library/export" {
        return None;
    }
    let fmt = {
        let f = str_field(payload, "format").to_lowercase();
        if f.is_empty() { "json".to_owned() } else { f }
    };
    if !EXPORT_FORMATS.contains(&fmt.as_str()) {
        return Some(api_error_response(
            &ApiError::bad_request("Export format must be json or csv."),
            ctx.request_id,
            ctx.instance,
        ));
    }
    let scope = {
        let s = str_field(payload, "scope").to_lowercase();
        if s.is_empty() { "all".to_owned() } else { s }
    };
    if !EXPORT_SCOPES.contains(&scope.as_str()) {
        return Some(api_error_response(
            &ApiError::bad_request("Export scope must be all, platform, or playlist."),
            ctx.request_id,
            ctx.instance,
        ));
    }
    let scope_name = str_field(payload, "scope_name");
    if scope != "all" && scope_name.is_empty() {
        return Some(api_error_response(
            &ApiError::bad_request(format!("Export scope {scope} requires a name.")),
            ctx.request_id,
            ctx.instance,
        ));
    }
    let include_media_paths = bool_field(payload, "include_media_paths");
    let scope_name = scope_name.to_owned();
    let app = Arc::clone(&ctx.app);
    let job = match ctx
        .app
        .jobs
        .submit("library-export", false, None, None, move |_jctx| {
            let state = app.load_state().map_err(|e| e.to_string())?;
            let games = build_export_games(&state, &scope, &scope_name)?;
            let rows = export_rows(&games, include_media_paths);
            let path = write_export(&app.data_dir, &rows, &fmt, include_media_paths)?;
            let fname = path
                .file_name()
                .and_then(|s| s.to_str())
                .unwrap_or("")
                .to_owned();
            Ok(json!({"file": fname, "count": rows.len()}))
        }) {
        Ok(j) => j,
        Err(e) => {
            return Some(api_error_response(
                &ApiError::job_state_conflict(e),
                ctx.request_id,
                ctx.instance,
            ));
        }
    };
    Some(json_response(
        StatusCode::ACCEPTED,
        &json!({"state": "queued", "job_id": job["job_id"]}),
        ctx.instance,
    ))
}
