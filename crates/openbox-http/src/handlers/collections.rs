//! Smart collections handlers (`handlers/collections.py`).

use axum::http::StatusCode;
use axum::response::Response;
use openbox_core::ApiError;
use openbox_discovery::collections::{collections_with_counts, delete_collection, save_collection};
use serde_json::{Map, Value, json};

use crate::handlers::shared::{RequestCtx, str_field};
use crate::middleware::{api_error_response, json_response, state_error_response};

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    if ctx.path != "/api/v2/collections" {
        return None;
    }
    let state = match ctx.app.load_state_view() {
        Ok(s) => s,
        Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
    };
    Some(json_response(
        StatusCode::OK,
        &json!({"items": collections_with_counts(&state, None)}),
        ctx.instance,
    ))
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/collections" => {
            let name = str_field(payload, "name").to_owned();
            let query = str_field(payload, "query").to_owned();
            match ctx
                .app
                .transact_state(|cur| Ok(save_collection(cur, &name, &query)))
            {
                Ok((_, Ok(saved))) => Some(json_response(
                    StatusCode::OK,
                    &json!({"ok": true, "saved": saved}),
                    ctx.instance,
                )),
                Ok((_, Err(msg))) => Some(api_error_response(
                    &ApiError::bad_request(msg).with_code("COLLECTION_INVALID"),
                    ctx.request_id,
                    ctx.instance,
                )),
                Err(e) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/collections/delete" => {
            let name = str_field(payload, "name").trim().to_owned();
            let name_mut = name.clone();
            match ctx
                .app
                .transact_state(|cur| Ok(delete_collection(cur, &name_mut)))
            {
                Ok((_, true)) => Some(json_response(
                    StatusCode::OK,
                    &json!({"ok": true, "deleted": name}),
                    ctx.instance,
                )),
                Ok((_, false)) => Some(api_error_response(
                    &ApiError::not_found("Collection not found.").with_code("COLLECTION_NOT_FOUND"),
                    ctx.request_id,
                    ctx.instance,
                )),
                Err(e) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            }
        }
        _ => None,
    }
}
