//! Durable operations & jobs v2 HTTP handlers. Mirrors `handlers/jobs.py`.

use axum::http::StatusCode;
use axum::response::Response;
use openbox_core::ApiError;
use serde_json::{Map, Value, json};

use crate::handlers::shared::{RequestCtx, parse_limit, str_field};
use crate::middleware::{api_error_response, json_response};

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    match ctx.path {
        "/api/v2/jobs" => {
            let limit = match parse_limit(ctx.query_first("limit").as_deref(), 50, 100) {
                Ok(n) => n,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let cursor = ctx.query_first("cursor");
            let type_filter = ctx
                .query_first("type")
                .or_else(|| ctx.query_first("type_filter"))
                .filter(|s| !s.is_empty());
            let state_filter = ctx
                .query_first("state")
                .or_else(|| ctx.query_first("state_filter"))
                .filter(|s| !s.is_empty());
            let root_job_id = ctx.query_first("root_job_id").filter(|s| !s.is_empty());

            let mut payload = ctx.app.operations.list_jobs(
                cursor.as_deref(),
                limit,
                type_filter.as_deref(),
                state_filter.as_deref(),
                root_job_id.as_deref(),
            );
            if ctx.query_first("group_by").as_deref() == Some("root_job_id")
                && let Some(obj) = payload.as_object_mut()
            {
                let jobs = obj
                    .get("jobs")
                    .and_then(Value::as_array)
                    .cloned()
                    .unwrap_or_default();
                let mut grouped: Map<String, Value> = Map::new();
                for job in jobs {
                    let root = job
                        .get("root_job_id")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned();
                    grouped
                        .entry(root)
                        .or_insert_with(|| Value::Array(Vec::new()))
                        .as_array_mut()
                        .unwrap()
                        .push(job);
                }
                obj.insert("grouped_by_root".into(), Value::Object(grouped));
            }
            Some(json_response(StatusCode::OK, &payload, ctx.instance))
        }
        "/api/v2/jobs/items" => {
            let job_id = ctx.query_first("job_id").unwrap_or_default();
            let job_id = job_id.trim();
            if job_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("job_id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if ctx.app.operations.get(job_id).is_none() {
                return Some(api_error_response(
                    &ApiError::not_found("Job not found."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let limit = match parse_limit(ctx.query_first("limit").as_deref(), 50, 200) {
                Ok(n) => n,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let cursor = ctx.query_first("cursor");
            let payload = ctx
                .app
                .operations
                .list_items(job_id, cursor.as_deref(), limit);
            Some(json_response(StatusCode::OK, &payload, ctx.instance))
        }
        _ => None,
    }
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/jobs/cancel" => {
            let job_id = str_field(payload, "job_id");
            if job_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("job_id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if ctx.app.operations.get(job_id).is_none() {
                return Some(api_error_response(
                    &ApiError::not_found("Job not found."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            match ctx.app.operations.request_cancel(job_id) {
                Ok(res) => {
                    ctx.app.jobs.cancel_by_id(job_id);
                    Some(json_response(
                        StatusCode::ACCEPTED,
                        &json!({"job_id": res["job_id"], "state": res["state"]}),
                        ctx.instance,
                    ))
                }
                Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/jobs/retry" => {
            let job_id = str_field(payload, "job_id");
            if job_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("job_id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let Some(original) = ctx.app.operations.get(job_id) else {
                return Some(api_error_response(
                    &ApiError::not_found("Job not found."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            if let Some(custom_input) = payload.get("input").filter(|v| !v.is_null()) {
                if !original
                    .get("can_retry")
                    .and_then(Value::as_bool)
                    .unwrap_or(false)
                {
                    return Some(api_error_response(
                        &ApiError::job_state_conflict(format!(
                            "Operation {job_id} cannot be retried."
                        )),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
                let created = ctx.app.operations.create_linked(
                    original["type"].as_str().unwrap_or("setup.scan"),
                    original["title"].as_str().unwrap_or("Retry"),
                    Some(custom_input),
                    original["root_job_id"].as_str(),
                    Some(job_id),
                    None,
                );
                return Some(json_response(
                    StatusCode::ACCEPTED,
                    &json!({
                        "job_id": created["job_id"],
                        "root_job_id": created["root_job_id"],
                        "retry_of": created["retry_of"],
                        "state": created["state"],
                    }),
                    ctx.instance,
                ));
            }
            match ctx.app.operations.retry(job_id) {
                Ok(retried) => Some(json_response(
                    StatusCode::ACCEPTED,
                    &json!({
                        "job_id": retried["job_id"],
                        "root_job_id": retried["root_job_id"],
                        "retry_of": retried["retry_of"],
                        "state": retried["state"],
                    }),
                    ctx.instance,
                )),
                Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/jobs/resume" => {
            let job_id = str_field(payload, "job_id");
            if job_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("job_id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if ctx.app.operations.get(job_id).is_none() {
                return Some(api_error_response(
                    &ApiError::not_found("Job not found."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            match ctx.app.operations.resume(job_id) {
                Ok(resumed) => Some(json_response(
                    StatusCode::ACCEPTED,
                    &json!({
                        "job_id": resumed["job_id"],
                        "resume_of": resumed["resume_of"],
                        "state": resumed["state"],
                    }),
                    ctx.instance,
                )),
                Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            }
        }
        _ => None,
    }
}
