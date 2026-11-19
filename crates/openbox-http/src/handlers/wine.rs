//! Wine prefix & Proton discovery HTTP handlers. Mirrors `handlers/wine.py`.

use axum::http::StatusCode;
use axum::response::Response;
use openbox_emulators::{resolve_game_from_payload, wine};
use openbox_platform::platform_features;
use serde_json::{Map, Value, json};

use crate::handlers::shared::RequestCtx;
use crate::middleware::{api_error_response, json_response, state_error_response};

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    let has_wine = platform_features().wine;
    match ctx.path {
        "/api/v2/wine/prefixes" => {
            if !has_wine {
                return Some(json_response(
                    StatusCode::OK,
                    &json!({"prefixes": [], "available": false}),
                    ctx.instance,
                ));
            }
            Some(json_response(
                StatusCode::OK,
                &json!({"prefixes": wine::list_wine_prefixes(None), "available": true}),
                ctx.instance,
            ))
        }
        "/api/v2/wine/protons" => {
            if !has_wine {
                return Some(json_response(
                    StatusCode::OK,
                    &json!({"protons": [], "available": false}),
                    ctx.instance,
                ));
            }
            Some(json_response(
                StatusCode::OK,
                &json!({"protons": wine::list_proton_versions(None), "available": true}),
                ctx.instance,
            ))
        }
        "/api/v2/wine/prefix-for-game" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let mut req_map = Map::new();
            if let Some(gid) = ctx.query_first("game_id") {
                req_map.insert("game_id".into(), Value::String(gid));
            }
            if let Some(id) = ctx.query_first("id") {
                req_map.insert("id".into(), Value::String(id));
            }
            let (_, game) = match resolve_game_from_payload(&state, &req_map) {
                Ok(pair) => pair,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            if !has_wine {
                return Some(json_response(
                    StatusCode::OK,
                    &json!({"prefix": "", "available": false}),
                    ctx.instance,
                ));
            }
            let prefix = wine::get_prefix_for_game(&game);
            let avail = !prefix.is_empty();
            Some(json_response(
                StatusCode::OK,
                &json!({"prefix": prefix, "available": avail}),
                ctx.instance,
            ))
        }
        _ => None,
    }
}
