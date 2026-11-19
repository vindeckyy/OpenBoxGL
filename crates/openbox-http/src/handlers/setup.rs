//! Setup Center, migration gate, and compatibility report handlers (`handlers/setup.py`).

use std::sync::Arc;

use axum::http::StatusCode;
use axum::response::Response;
use openbox_core::{ApiError, job_result_state};
use openbox_imports::{
    DEFAULT_ITEMS_LIMIT, MAX_ITEMS_LIMIT, apply_decisions, commit_preview, compat_build_report,
    compute_summary, create_preview_record, list_preview_items, load_preview, preview_document,
    readiness_checks, revalidate_preview_record, run_scan_job, save_preview,
};
use openbox_store::library_v7::{ROLLBACK_REMOVED, acknowledge_migration, migration_status};
use serde_json::{Map, Value, json};

use crate::handlers::shared::{RequestCtx, parse_limit, str_field};
use crate::middleware::{api_error_response, json_response};

fn preview_state_for_job(ctx: &RequestCtx<'_>, job_id: Option<&str>) -> String {
    let Some(jid) = job_id.filter(|s| !s.is_empty()) else {
        return "ready".to_owned();
    };
    let Some(op) = ctx.app.operations.get(jid) else {
        return "ready".to_owned();
    };
    job_result_state(op.get("state").and_then(Value::as_str)).to_owned()
}

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    match ctx.path {
        "/api/v2/setup/migration/status" => {
            let mut status = migration_status(&ctx.app.data_dir);
            if let Some(m) = status.as_object_mut() {
                m.insert(
                    "readiness".into(),
                    Value::Array(readiness_checks(&ctx.app.data_dir, &ctx.app.root_dir)),
                );
            }
            Some(json_response(StatusCode::OK, &status, ctx.instance))
        }
        "/api/v2/compat/report" => {
            let report = compat_build_report(&ctx.app.data_dir, &ctx.app.root_dir);
            Some(json_response(StatusCode::OK, &report, ctx.instance))
        }
        "/api/v2/setup/summary" => match ctx.app.load_state_view() {
            Ok(st) => {
                let summary = compute_summary(&ctx.app, &st);
                Some(json_response(StatusCode::OK, &summary, ctx.instance))
            }
            Err(err) => Some(api_error_response(
                &ApiError::state_unavailable(err.to_string()),
                ctx.request_id,
                ctx.instance,
            )),
        },
        "/api/v2/setup/preview" => {
            let preview_id = ctx.query_first("preview_id").unwrap_or_default();
            let preview_id = preview_id.trim();
            if preview_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("preview_id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            match load_preview(&ctx.app, preview_id, false) {
                Ok(preview) => {
                    let mut doc = preview_document(&preview);
                    let job_id = preview.get("job_id").and_then(Value::as_str);
                    if preview.get("state").and_then(Value::as_str) != Some("ready") {
                        doc["state"] = Value::String(preview_state_for_job(ctx, job_id));
                    }
                    Some(json_response(StatusCode::OK, &doc, ctx.instance))
                }
                Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/setup/preview/items" => {
            let preview_id = ctx.query_first("preview_id").unwrap_or_default();
            let preview_id = preview_id.trim();
            if preview_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("preview_id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let cursor = ctx.query_first("cursor");
            let limit = match parse_limit(
                ctx.query_first("limit").as_deref(),
                DEFAULT_ITEMS_LIMIT,
                MAX_ITEMS_LIMIT,
            ) {
                Ok(l) => l,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            match list_preview_items(&ctx.app, preview_id, cursor.as_deref(), limit) {
                Ok(payload) => Some(json_response(StatusCode::OK, &payload, ctx.instance)),
                Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            }
        }
        _ => None,
    }
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/setup/migration/acknowledge" => {
            let res = acknowledge_migration(&ctx.app.data_dir);
            Some(json_response(StatusCode::OK, &res, ctx.instance))
        }
        "/api/v2/setup/migration/rollback" => Some(json_response(
            StatusCode::CONFLICT,
            &json!({"error": ROLLBACK_REMOVED, "code": "ROLLBACK_REMOVED"}),
            ctx.instance,
        )),
        "/api/v2/setup/preview" => {
            let Some(sources) = payload
                .get("sources")
                .and_then(Value::as_array)
                .filter(|a| !a.is_empty())
            else {
                return Some(api_error_response(
                    &ApiError::bad_request("sources must be a non-empty array."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let options = payload.get("options").cloned().unwrap_or_else(|| json!({}));
            if !options.is_object() {
                return Some(api_error_response(
                    &ApiError::bad_request("options must be an object."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let mut preview = match create_preview_record(&ctx.app, sources, &options) {
                Ok(p) => p,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let pid = preview["preview_id"]
                .as_str()
                .unwrap_or_default()
                .to_owned();
            let app = Arc::clone(&ctx.app);
            let pid_clone = pid.clone();
            let target = json!({"preview_id": pid});
            let job = match ctx.app.jobs.submit(
                &format!("setup-scan:{pid}"),
                true,
                Some("setup.scan"),
                Some(&target),
                move |_ctx| run_scan_job(&app, &pid_clone).map_err(|e| e.message),
            ) {
                Ok(j) => j,
                Err(e) => {
                    return Some(api_error_response(
                        &ApiError::job_state_conflict(e),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let job_id = job
                .get("job_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            if let Ok(mut reloaded) = load_preview(&ctx.app, &pid, true) {
                reloaded["job_id"] = json!(job_id);
                reloaded["state"] = Value::String(preview_state_for_job(ctx, Some(&job_id)));
                let _ = save_preview(&ctx.app, &reloaded);
                preview = reloaded;
            }
            Some(json_response(
                StatusCode::ACCEPTED,
                &json!({
                    "preview_id": pid,
                    "revision": preview["revision"],
                    "job_id": job_id,
                    "expires_at": preview["expires_at"],
                    "state": preview["state"],
                }),
                ctx.instance,
            ))
        }
        "/api/v2/setup/preview/decisions" => {
            let preview_id = str_field(payload, "preview_id");
            if preview_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("preview_id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let Some(items) = payload
                .get("items")
                .and_then(Value::as_array)
                .filter(|a| !a.is_empty())
            else {
                return Some(api_error_response(
                    &ApiError::bad_request("items must be a non-empty array."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            match apply_decisions(&ctx.app, preview_id, items) {
                Ok(mut res) => {
                    if let Some(m) = res.as_object_mut() {
                        m.insert("preview_id".into(), Value::String(preview_id.to_owned()));
                    }
                    Some(json_response(StatusCode::OK, &res, ctx.instance))
                }
                Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/setup/preview/revalidate" => {
            let preview_id = str_field(payload, "preview_id").to_owned();
            if preview_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("preview_id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if let Err(err) = load_preview(&ctx.app, &preview_id, false) {
                return Some(api_error_response(&err, ctx.request_id, ctx.instance));
            }
            let app = Arc::clone(&ctx.app);
            let pid = preview_id.clone();
            let target = json!({"preview_id": preview_id});
            let job = match ctx.app.jobs.submit(
                &format!("setup-revalidate:{preview_id}"),
                true,
                Some("setup.revalidate"),
                Some(&target),
                move |_ctx| {
                    revalidate_preview_record(&app, &pid)
                        .map(|_| json!({"revalidated": true}))
                        .map_err(|e| e.message)
                },
            ) {
                Ok(j) => j,
                Err(e) => {
                    return Some(api_error_response(
                        &ApiError::job_state_conflict(e),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let job_id = job
                .get("job_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            let mut revision = json!(1);
            if let Ok(mut updated) = load_preview(&ctx.app, &preview_id, true) {
                updated["job_id"] = json!(job_id);
                revision = updated["revision"].clone();
                let _ = save_preview(&ctx.app, &updated);
            }
            Some(json_response(
                StatusCode::ACCEPTED,
                &json!({
                    "preview_id": preview_id,
                    "revision": revision,
                    "job_id": job_id,
                    "state": preview_state_for_job(ctx, Some(&job_id)),
                }),
                ctx.instance,
            ))
        }
        "/api/v2/setup/commit" => {
            let preview_id = str_field(payload, "preview_id").to_owned();
            if preview_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("preview_id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let revision = payload.get("revision").and_then(Value::as_i64).unwrap_or(0);
            let Some(choices) = payload.get("emulator_choices") else {
                return Some(api_error_response(
                    &ApiError::bad_request("emulator_choices is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let Some(choices_arr) = choices.as_array().cloned() else {
                return Some(api_error_response(
                    &ApiError::bad_request("emulator_choices must be an array."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            if let Err(err) = load_preview(&ctx.app, &preview_id, false) {
                return Some(api_error_response(&err, ctx.request_id, ctx.instance));
            }
            let import_batch_id = uuid::Uuid::new_v4().simple().to_string();
            let app = Arc::clone(&ctx.app);
            let pid = preview_id.clone();
            let batch_clone = import_batch_id.clone();
            let target = json!({"preview_id": preview_id, "revision": revision});
            let job = match ctx.app.jobs.submit(
                &format!("setup-commit:{preview_id}"),
                true,
                Some("setup.commit"),
                Some(&target),
                move |_ctx| {
                    commit_preview(&app, &pid, revision, &choices_arr, &batch_clone)
                        .map_err(|e| e.message)
                },
            ) {
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
                &json!({
                    "job_id": job.get("job_id").cloned().unwrap_or(Value::Null),
                    "import_batch_id": import_batch_id,
                    "preview_id": preview_id,
                    "revision": revision,
                }),
                ctx.instance,
            ))
        }
        _ => None,
    }
}
