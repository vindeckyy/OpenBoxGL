//! Deal Radar v2 handlers (`handlers/deals.py`, ADR 0111).

use std::sync::Arc;

use axum::http::StatusCode;
use axum::response::Response;
use openbox_core::ApiError;
use openbox_metadata::deals::{
    ALLOWED_HOST, DEFAULT_STORE_CC, list_deals, normalize_cc, record_manual_entry,
    refresh_deals_pass, search_steam, unwatch_game, watch_game,
};
use serde_json::{Map, Value, json};

use crate::handlers::shared::{RequestCtx, str_field};
use crate::middleware::{api_error_response, json_response, state_error_response};

fn deals_enabled(state: &Value) -> bool {
    state
        .get("settings")
        .and_then(|s| s.get("deals_enabled"))
        .and_then(Value::as_bool)
        .unwrap_or(false)
}

fn require_deals_enabled(state: &Value) -> Result<Value, ApiError> {
    if !deals_enabled(state) {
        return Err(ApiError::deals_disabled(
            "Deal Radar is turned off in Settings.",
        ));
    }
    Ok(state.get("settings").cloned().unwrap_or_else(|| json!({})))
}

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    match ctx.path {
        "/api/v2/deals" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            if !deals_enabled(&state) {
                return Some(json_response(
                    StatusCode::OK,
                    &json!({
                        "available": false,
                        "host_allowlist": [ALLOWED_HOST],
                        "watchlist": [],
                        "last_poll_at": "",
                    }),
                    ctx.instance,
                ));
            }
            Some(json_response(
                StatusCode::OK,
                &list_deals(&state),
                ctx.instance,
            ))
        }
        "/api/v2/deals/search" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let settings = match require_deals_enabled(&state) {
                Ok(s) => s,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let q = ctx.query_first("q").unwrap_or_default();
            let default_cc = settings
                .get("deals_store_cc")
                .and_then(Value::as_str)
                .unwrap_or(DEFAULT_STORE_CC);
            let cc = normalize_cc(
                ctx.query_first("cc")
                    .as_deref()
                    .filter(|s| !s.is_empty())
                    .unwrap_or(default_cc),
            );
            match search_steam(q.trim(), &cc) {
                Ok(results) => Some(json_response(
                    StatusCode::OK,
                    &json!({"available": true, "results": results, "cc": cc}),
                    ctx.instance,
                )),
                Err(e) => Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            }
        }
        _ => None,
    }
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/deals/watch" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            if let Err(e) = require_deals_enabled(&state) {
                return Some(api_error_response(&e, ctx.request_id, ctx.instance));
            }
            let mut res: Result<Value, ApiError> = Ok(Value::Null);
            let tx = ctx.app.transact_state(|st| {
                res = watch_game(st, payload);
                if res.is_err() {
                    return Err(openbox_store::StateError::Corrupt("watch".into()));
                }
                Ok(())
            });
            match (tx, res) {
                (_, Err(e)) => Some(api_error_response(&e, ctx.request_id, ctx.instance)),
                (Err(e), _) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
                (Ok(_), Ok(created)) => Some(json_response(
                    StatusCode::CREATED,
                    &json!({"entry": created}),
                    ctx.instance,
                )),
            }
        }
        "/api/v2/deals/unwatch" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            if let Err(e) = require_deals_enabled(&state) {
                return Some(api_error_response(&e, ctx.request_id, ctx.instance));
            }
            let item_id = str_field(payload, "id").to_owned();
            if item_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let mut res: Result<Value, ApiError> = Ok(Value::Null);
            let tx = ctx.app.transact_state(|st| {
                res = unwatch_game(st, &item_id);
                if res.is_err() {
                    return Err(openbox_store::StateError::Corrupt("unwatch".into()));
                }
                Ok(())
            });
            match (tx, res) {
                (_, Err(e)) => Some(api_error_response(&e, ctx.request_id, ctx.instance)),
                (Err(e), _) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
                (Ok(_), Ok(out)) => Some(json_response(StatusCode::OK, &out, ctx.instance)),
            }
        }
        "/api/v2/deals/entry" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            if let Err(e) = require_deals_enabled(&state) {
                return Some(api_error_response(&e, ctx.request_id, ctx.instance));
            }
            let mut res: Result<Value, ApiError> = Ok(Value::Null);
            let tx = ctx.app.transact_state(|st| {
                res = record_manual_entry(st, payload);
                if res.is_err() {
                    return Err(openbox_store::StateError::Corrupt("entry".into()));
                }
                Ok(())
            });
            match (tx, res) {
                (_, Err(e)) => Some(api_error_response(&e, ctx.request_id, ctx.instance)),
                (Err(e), _) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
                (Ok(_), Ok(updated)) => Some(json_response(
                    StatusCode::OK,
                    &json!({"entry": updated}),
                    ctx.instance,
                )),
            }
        }
        "/api/v2/deals/refresh" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let settings = match require_deals_enabled(&state) {
                Ok(s) => s,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let app = Arc::clone(&ctx.app);
            let st_c = Arc::clone(&state);
            let job = match ctx
                .app
                .jobs
                .submit("deals-refresh", true, None, None, move |jctx| {
                    let app_commit = Arc::clone(&app);
                    let res = refresh_deals_pass(
                        &app.data_dir,
                        &st_c,
                        &settings,
                        Some(jctx.cancel_flag()),
                        move |mutator| {
                            app_commit
                                .transact_state(|cur| Ok(mutator(cur)))
                                .map(|(_, out)| out)
                                .map_err(|e| e.to_string())
                        },
                    );
                    Ok(res)
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
                &json!({"state": "queued", "job_id": job.get("job_id").cloned().unwrap_or(Value::Null)}),
                ctx.instance,
            ))
        }
        _ => None,
    }
}
