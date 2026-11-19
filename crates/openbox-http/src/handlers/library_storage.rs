//! Library storage endpoints: integrity report and move/rebase API (`handlers/library_storage.py`).

use std::path::Path;

use axum::http::StatusCode;
use axum::response::Response;
use openbox_core::ApiError;
use openbox_store::library_v7::{apply_move, check_library, plan_move};
use serde_json::{Map, Value};

use crate::handlers::shared::{RequestCtx, str_field};
use crate::middleware::{api_error_response, json_response};

fn extract_roots(payload: &Map<String, Value>) -> Result<(String, String), ApiError> {
    let old_root = str_field(payload, "old_root").to_owned();
    let new_root = str_field(payload, "new_root").to_owned();
    if new_root.is_empty() || !Path::new(&new_root).is_dir() {
        return Err(ApiError::bad_request("The new library folder must exist."));
    }
    Ok((old_root, new_root))
}

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    if ctx.path != "/api/v2/library/integrity" {
        return None;
    }
    let report = check_library(&ctx.app.data_dir);
    Some(json_response(StatusCode::OK, &report, ctx.instance))
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/library/move/preview" => {
            let (old_root, new_root) = match extract_roots(payload) {
                Ok(r) => r,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(err) => {
                    return Some(api_error_response(
                        &ApiError::state_unavailable(err.to_string()),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let games = state
                .get("games")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            match plan_move(&games, &old_root, &new_root) {
                Ok(plan) => Some(json_response(StatusCode::OK, &plan, ctx.instance)),
                Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/library/move/apply" => {
            let (old_root, new_root) = match extract_roots(payload) {
                Ok(r) => r,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let plan_id = str_field(payload, "plan_id").to_owned();
            let mut move_res: Result<Value, ApiError> = Ok(Value::Null);
            let tx = ctx.app.transact_state(|state| {
                move_res = apply_move(state, &plan_id, &old_root, &new_root);
                if let Err(ref err) = move_res {
                    return Err(openbox_store::StateError::Corrupt(err.message.clone()));
                }
                Ok(())
            });
            match (tx, move_res) {
                (_, Err(err)) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
                (Err(err), _) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
                (Ok(_), Ok(result)) => Some(json_response(StatusCode::OK, &result, ctx.instance)),
            }
        }
        _ => None,
    }
}
