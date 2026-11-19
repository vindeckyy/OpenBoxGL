//! Time Machine HTTP handlers (`handlers/timemachine.py`).

use axum::http::StatusCode;
use axum::response::Response;
use openbox_core::ApiError;
use openbox_sync::library_sync::SyncError;
use openbox_sync::time_machine::{
    COMPARE_PAGE_DEFAULT, EVENT_PAGE_DEFAULT, apply_revert, compare, list_events,
    materialize_as_of, parse_as_of_date, plan_revert,
};
use serde_json::{Map, Value, json};

use crate::handlers::shared::{RequestCtx, bool_field, str_field};
use crate::middleware::{api_error_response, json_response, state_error_response};

fn parse_query_usize(ctx: &RequestCtx<'_>, key: &str, default: usize) -> Result<usize, ApiError> {
    let Some(raw) = ctx.query_first(key).filter(|s| !s.is_empty()) else {
        return Ok(default);
    };
    let n: i64 = raw.parse().map_err(|_| {
        ApiError::bad_request(format!("{key} must be an integer")).with_code("TM_INVALID_REQUEST")
    })?;
    Ok(n.max(0) as usize)
}

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    match ctx.path {
        "/api/v2/library/time-machine/events" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let days = match ctx.query_first("days").filter(|s| !s.trim().is_empty()) {
                None => None,
                Some(raw) => match raw.trim().parse::<f64>() {
                    Ok(v) => Some(v),
                    Err(_) => {
                        return Some(api_error_response(
                            &ApiError::bad_request("days must be a positive number")
                                .with_code("TM_INVALID_REQUEST"),
                            ctx.request_id,
                            ctx.instance,
                        ));
                    }
                },
            };
            let offset = match parse_query_usize(ctx, "offset", 0) {
                Ok(v) => v,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let limit = match parse_query_usize(ctx, "limit", EVENT_PAGE_DEFAULT) {
                Ok(v) => v,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let kind = ctx.query_first("kind").filter(|s| !s.is_empty());
            let game_id = ctx
                .query_first("game_id")
                .filter(|s| !s.is_empty())
                .or_else(|| ctx.query_first("game").filter(|s| !s.is_empty()));
            match list_events(
                &state,
                days,
                kind.as_deref(),
                game_id.as_deref(),
                offset,
                Some(limit),
            ) {
                Ok(res) => Some(json_response(StatusCode::OK, &res, ctx.instance)),
                Err(err) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()).with_code("TM_INVALID_REQUEST"),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/library/time-machine/as-of" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let raw_date = ctx.query_first("date").unwrap_or_default();
            if raw_date.trim().is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("date is required").with_code("TM_INVALID_DATE"),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let when = match parse_as_of_date(&raw_date) {
                Ok(ts) => ts,
                Err(err) => {
                    return Some(api_error_response(
                        &ApiError::bad_request(err.to_string()).with_code("TM_INVALID_DATE"),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            match materialize_as_of(&state, Some(when)) {
                Ok(res) => Some(json_response(StatusCode::OK, &res, ctx.instance)),
                Err(err) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()).with_code("TM_INVALID_DATE"),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/library/time-machine/compare" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let after = ctx
                .query_first("after")
                .filter(|s| !s.trim().is_empty())
                .or_else(|| ctx.query_first("date").filter(|s| !s.trim().is_empty()))
                .unwrap_or_default();
            if after.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("after is required").with_code("TM_INVALID_DATE"),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let before = ctx.query_first("before").filter(|s| !s.trim().is_empty());
            let fields: Option<Vec<String>> = ctx
                .query_first("fields")
                .map(|raw| {
                    raw.split(',')
                        .map(str::trim)
                        .filter(|s| !s.is_empty())
                        .map(str::to_owned)
                        .collect::<Vec<_>>()
                })
                .filter(|v| !v.is_empty());
            let limit = match parse_query_usize(ctx, "limit", COMPARE_PAGE_DEFAULT) {
                Ok(v) => v,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            match compare(
                &state,
                before.as_deref(),
                &after,
                fields.as_deref(),
                Some(limit),
            ) {
                Ok(res) => Some(json_response(StatusCode::OK, &res, ctx.instance)),
                Err(err) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()).with_code("TM_INVALID_DATE"),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        _ => None,
    }
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    if ctx.path != "/api/v2/library/time-machine/revert" {
        return None;
    }
    let state = match ctx.app.load_state_view() {
        Ok(s) => s,
        Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
    };
    let apply = bool_field(payload, "apply");
    if apply && str_field(payload, "base_token").is_empty() {
        return Some(api_error_response(
            &ApiError::bad_request("base_token is required when applying a revert preview")
                .with_code("TM_INVALID_REQUEST"),
            ctx.request_id,
            ctx.instance,
        ));
    }
    let event_id = str_field(payload, "event_id");
    if event_id.is_empty() {
        return Some(api_error_response(
            &ApiError::bad_request("event_id is required").with_code("TM_INVALID_REQUEST"),
            ctx.request_id,
            ctx.instance,
        ));
    }
    let fields: Option<Vec<String>> = match payload.get("fields") {
        None | Some(Value::Null) => None,
        Some(Value::Array(arr)) => {
            if !arr.iter().all(Value::is_string) {
                return Some(api_error_response(
                    &ApiError::bad_request("fields must be a list of catalog field names")
                        .with_code("TM_INVALID_REQUEST"),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            Some(
                arr.iter()
                    .filter_map(Value::as_str)
                    .map(str::to_owned)
                    .collect(),
            )
        }
        _ => {
            return Some(api_error_response(
                &ApiError::bad_request("fields must be a list of catalog field names")
                    .with_code("TM_INVALID_REQUEST"),
                ctx.request_id,
                ctx.instance,
            ));
        }
    };
    let game_id = payload
        .get("game_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty());
    let undo = bool_field(payload, "undo");
    let mut plan = match plan_revert(&state, event_id, game_id, fields.as_deref(), undo) {
        Ok(p) => p,
        Err(err) => {
            return Some(api_error_response(
                &ApiError::bad_request(err.to_string()).with_code("TM_INVALID_REQUEST"),
                ctx.request_id,
                ctx.instance,
            ));
        }
    };
    if let Some(bt) = payload.get("base_token")
        && let Some(m) = plan.as_object_mut()
    {
        m.insert("base_token".into(), bt.clone());
    }
    if !apply {
        return Some(json_response(
            StatusCode::OK,
            &json!({"preview": true, "plan": plan}),
            ctx.instance,
        ));
    }
    let res = ctx.app.transact_state(|cur| Ok(apply_revert(cur, &plan)));
    match res {
        Ok((_, Ok(mut out))) => {
            if let Some(m) = out.as_object_mut() {
                m.insert("preview".into(), Value::Bool(false));
            }
            Some(json_response(StatusCode::OK, &out, ctx.instance))
        }
        Ok((_, Err(SyncError::Stale(msg)))) => Some(api_error_response(
            &ApiError::conflict(msg).with_code("TM_REVERT_STALE"),
            ctx.request_id,
            ctx.instance,
        )),
        Ok((_, Err(err))) => Some(api_error_response(
            &ApiError::bad_request(err.to_string()).with_code("TM_INVALID_REQUEST"),
            ctx.request_id,
            ctx.instance,
        )),
        Err(e) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
    }
}
