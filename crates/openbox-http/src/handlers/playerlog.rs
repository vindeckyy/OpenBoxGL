//! Player's Log HTTP handlers — Time Capsules & Milestones (`handlers/playerlog.py`, ADR 0112).

use axum::http::StatusCode;
use axum::response::Response;
use openbox_discovery::playerlog::{
    compute_milestones, create_capsule, delete_capsule, list_capsules, next_capsule, open_capsule,
};
use serde_json::{Map, Value, json};

use crate::handlers::shared::{RequestCtx, str_field};
use crate::middleware::{api_error_response, json_response, state_error_response};

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    match ctx.path {
        "/api/v2/capsules" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let gid = ctx.query_first("game_id").unwrap_or_default();
            Some(json_response(
                StatusCode::OK,
                &list_capsules(&state, gid.trim(), None),
                ctx.instance,
            ))
        }
        "/api/v2/capsules/next" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            Some(json_response(
                StatusCode::OK,
                &next_capsule(&state, None),
                ctx.instance,
            ))
        }
        "/api/v2/insights/milestones" => {
            let mark_seen = matches!(
                ctx.query_first("mark_seen")
                    .as_deref()
                    .unwrap_or("")
                    .trim()
                    .to_lowercase()
                    .as_str(),
                "1" | "true" | "yes"
            );
            let limit: usize = ctx
                .query_first("limit")
                .and_then(|s| s.trim().parse::<i64>().ok())
                .unwrap_or(100)
                .clamp(1, 500) as usize;
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let report = compute_milestones(&state, Some(&ctx.app.data_dir), None, Some(limit));
            if mark_seen
                && let Some(unlocked) = report.get("unlocked").and_then(Value::as_array)
                && !unlocked.is_empty()
            {
                let seen_ids: Vec<String> = unlocked
                    .iter()
                    .filter_map(|it| it.get("id").and_then(Value::as_str).map(str::to_owned))
                    .collect();
                let _ = ctx.app.transact_state(|cur| {
                    if let Some(root) = cur.as_object_mut() {
                        let ui = root
                            .entry("ui_state".to_owned())
                            .or_insert_with(|| Value::Object(Map::new()));
                        if !ui.is_object() {
                            *ui = Value::Object(Map::new());
                        }
                        if let Some(um) = ui.as_object_mut() {
                            let mut existing: Vec<String> = um
                                .get("milestones_seen")
                                .and_then(Value::as_array)
                                .map(|a| {
                                    a.iter()
                                        .filter_map(Value::as_str)
                                        .filter(|s| !s.is_empty())
                                        .map(str::to_owned)
                                        .collect()
                                })
                                .unwrap_or_default();
                            for id in &seen_ids {
                                if !existing.contains(id) {
                                    existing.push(id.clone());
                                }
                            }
                            um.insert("milestones_seen".into(), json!(existing));
                        }
                    }
                    Ok(())
                });
            }
            Some(json_response(StatusCode::OK, &report, ctx.instance))
        }
        _ => None,
    }
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/capsules" => {
            let body = payload.clone();
            match ctx
                .app
                .transact_state(|cur| Ok(create_capsule(cur, &body, None, None)))
            {
                Ok((_, Ok(created))) => Some(json_response(
                    StatusCode::CREATED,
                    &json!({"capsule": created}),
                    ctx.instance,
                )),
                Ok((_, Err(err))) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
                Err(e) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/capsules/open" => {
            let cid = str_field(payload, "id").trim().to_owned();
            match ctx
                .app
                .transact_state(|cur| Ok(open_capsule(cur, &cid, None, None)))
            {
                Ok((_, Ok(opened))) => Some(json_response(
                    StatusCode::OK,
                    &json!({"capsule": opened}),
                    ctx.instance,
                )),
                Ok((_, Err(err))) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
                Err(e) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/capsules/delete" => {
            let cid = str_field(payload, "id").trim().to_owned();
            match ctx.app.transact_state(|cur| Ok(delete_capsule(cur, &cid))) {
                Ok((_, Ok(deleted))) => Some(json_response(StatusCode::OK, &deleted, ctx.instance)),
                Ok((_, Err(err))) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
                Err(e) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            }
        }
        _ => None,
    }
}
