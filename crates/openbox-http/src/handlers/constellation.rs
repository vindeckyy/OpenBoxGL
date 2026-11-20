//! Constellation relationship graph handler (`handlers/constellation.py`).

use std::collections::HashSet;

use axum::http::StatusCode;
use axum::response::Response;
use openbox_core::ApiError;
use openbox_discovery::constellation::{KINDS, build_graph_typed};
use serde_json::Value;

use crate::handlers::shared::RequestCtx;
use crate::middleware::{api_error_response, serialized_json_response, state_error_response};

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    if ctx.path != "/api/v2/library/constellation" {
        return None;
    }
    let state = match ctx.app.load_state_view() {
        Ok(s) => s,
        Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
    };
    let raw_kinds = ctx.query_first("kinds").unwrap_or_default();
    let kinds: Option<HashSet<String>> = if raw_kinds.trim().is_empty() {
        None
    } else {
        let parsed: HashSet<String> = raw_kinds
            .split(',')
            .map(|k| k.trim().to_lowercase())
            .filter(|k| !k.is_empty())
            .collect();
        let valid: HashSet<&str> = KINDS.iter().copied().collect();
        let mut invalid: Vec<String> = parsed
            .iter()
            .filter(|k| !valid.contains(k.as_str()))
            .cloned()
            .collect();
        if !invalid.is_empty() {
            invalid.sort();
            let mut valid_sorted: Vec<&str> = KINDS.to_vec();
            valid_sorted.sort_unstable();
            return Some(api_error_response(
                &ApiError::bad_request(format!(
                    "unknown kinds: {invalid:?}; valid: {valid_sorted:?}"
                )),
                ctx.request_id,
                ctx.instance,
            ));
        }
        Some(parsed)
    };

    let limit: usize = match ctx.query_first("limit").filter(|s| !s.trim().is_empty()) {
        None => 400,
        Some(raw) => match raw.trim().parse::<i64>() {
            Ok(v) if (50..=1000).contains(&v) => v as usize,
            Ok(_) => {
                return Some(api_error_response(
                    &ApiError::bad_request("limit must be between 50 and 1000"),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            Err(_) => {
                return Some(api_error_response(
                    &ApiError::bad_request("limit must be an integer"),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
        },
    };

    let games = state
        .get("games")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let history = state
        .get("history")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let result = build_graph_typed(games, history, kinds.as_ref(), limit);
    Some(serialized_json_response(StatusCode::OK, &result))
}
