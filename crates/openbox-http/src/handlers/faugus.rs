//! Faugus Launcher status, scan, and import HTTP handlers. Mirrors `handlers/faugus.py`.

use std::collections::HashSet;

use axum::http::StatusCode;
use axum::response::Response;
use openbox_emulators::faugus;
use serde_json::{Map, Value, json};

use crate::handlers::shared::RequestCtx;
use crate::middleware::{json_response, state_error_response};

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    match ctx.path {
        "/api/v2/faugus/status" => {
            let dirs = faugus::find_faugus_data_dirs();
            Some(json_response(
                StatusCode::OK,
                &json!({"installed": !dirs.is_empty(), "data_dirs": dirs}),
                ctx.instance,
            ))
        }
        "/api/v2/faugus/scan" => {
            let games = faugus::scan_faugus_games(None);
            let count = games.len();
            Some(json_response(
                StatusCode::OK,
                &json!({"games": games, "count": count}),
                ctx.instance,
            ))
        }
        _ => None,
    }
}

pub fn handle_post(ctx: &RequestCtx<'_>, _payload: &Map<String, Value>) -> Option<Response> {
    if ctx.path != "/api/v2/faugus/import" {
        return None;
    }
    let candidates = faugus::scan_faugus_games(None);
    let found = candidates.len();
    let incoming: Vec<Value> = candidates
        .into_iter()
        .map(|cand| {
            let path = cand["path"].as_str().unwrap_or("");
            let launch = if path.is_empty() {
                ""
            } else {
                "umu-run {path}"
            };
            json!({
                "name": cand["name"],
                "platform": "PC",
                "path": path,
                "source": "Faugus",
                "faugus_id": cand["faugus_id"],
                "launch": launch,
                "wine_prefix": cand["prefix"],
                "source_identity": cand["source_identity"],
            })
        })
        .collect();
    match ctx.app.transact_state(move |st| {
        let games = st.get_mut("games").and_then(Value::as_array_mut).unwrap();
        let mut seen: HashSet<String> = games
            .iter()
            .filter_map(|g| {
                g.get("faugus_id")
                    .or_else(|| g.get("source_identity"))
                    .and_then(Value::as_str)
                    .map(str::to_owned)
            })
            .collect();
        let mut added_names = Vec::new();
        for g in incoming {
            let key = g["faugus_id"].as_str().unwrap_or("").to_owned();
            if seen.insert(key) {
                added_names.push(g["name"].as_str().unwrap_or("").to_owned());
                games.push(g);
            }
        }
        Ok(added_names)
    }) {
        Ok((_, added_names)) => {
            let added = added_names.len();
            Some(json_response(
                StatusCode::OK,
                &json!({
                    "added": added,
                    "found": found,
                    "imported": added_names,
                    "count": added,
                }),
                ctx.instance,
            ))
        }
        Err(e) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
    }
}
