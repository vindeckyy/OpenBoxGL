//! Save Vault HTTP handlers (`handlers/savevault.py`, ADR 0109).

use axum::http::StatusCode;
use axum::response::Response;
use openbox_core::ApiError;
use openbox_sync::save_vault::{
    DEFAULT_KEEP, clamp_keep, create_snapshot, delete_snapshot, diff_snapshot, list_snapshots,
    resolve_game_save_roots, restore_snapshot, vault_status,
};
use serde_json::{Map, Value, json};

use crate::handlers::projection::find_game_index_in_state;
use crate::handlers::shared::{RequestCtx, bool_field, str_field};
use crate::middleware::{api_error_response, json_response, state_error_response};

fn save_mgmt_enabled(state: &Value) -> bool {
    state
        .get("settings")
        .and_then(|s| s.get("save_management_enabled"))
        .and_then(Value::as_bool)
        .unwrap_or(true)
}

fn game_from_query(state: &Value, ctx: &RequestCtx<'_>) -> Result<Value, ApiError> {
    let mut payload = Map::new();
    if let Some(gid) = ctx.query_first("game_id") {
        payload.insert("game_id".into(), Value::String(gid));
    }
    if let Some(id_s) = ctx.query_first("id") {
        if let Ok(n) = id_s.parse::<i64>() {
            payload.insert("id".into(), json!(n));
        } else {
            payload.insert("id".into(), Value::String(id_s));
        }
    }
    let idx = find_game_index_in_state(state, &payload)
        .map_err(|_| ApiError::game_not_found("Game not found."))?;
    state
        .get("games")
        .and_then(Value::as_array)
        .and_then(|a| a.get(idx))
        .cloned()
        .ok_or_else(|| ApiError::game_not_found("Game not found."))
}

fn game_from_payload(state: &Value, payload: &Map<String, Value>) -> Result<Value, ApiError> {
    let idx = find_game_index_in_state(state, payload)
        .map_err(|_| ApiError::game_not_found("Game not found."))?;
    state
        .get("games")
        .and_then(Value::as_array)
        .and_then(|a| a.get(idx))
        .cloned()
        .ok_or_else(|| ApiError::game_not_found("Game not found."))
}

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    match ctx.path {
        "/api/v2/saves/vault" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            if !save_mgmt_enabled(&state) {
                return Some(json_response(
                    StatusCode::OK,
                    &json!({"enabled": false, "snapshots": []}),
                    ctx.instance,
                ));
            }
            let game_id = if ctx.query_first("id").is_some() || ctx.query_first("game_id").is_some()
            {
                match game_from_query(&state, ctx) {
                    Ok(g) => g
                        .get("game_id")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned(),
                    Err(err) => {
                        return Some(api_error_response(&err, ctx.request_id, ctx.instance));
                    }
                }
            } else {
                String::new()
            };
            let snapshots = list_snapshots(&ctx.app.data_dir, &game_id);
            let status = vault_status(&ctx.app.data_dir, &game_id, state.get("settings"));
            Some(json_response(
                StatusCode::OK,
                &json!({"enabled": true, "snapshots": snapshots, "status": status}),
                ctx.instance,
            ))
        }
        "/api/v2/saves/vault/status" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            if !save_mgmt_enabled(&state) {
                return Some(json_response(
                    StatusCode::OK,
                    &json!({"enabled": false, "snapshots": 0, "bytes": 0, "games": 0}),
                    ctx.instance,
                ));
            }
            let game_id = if ctx.query_first("id").is_some() || ctx.query_first("game_id").is_some()
            {
                match game_from_query(&state, ctx) {
                    Ok(g) => g
                        .get("game_id")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned(),
                    Err(err) => {
                        return Some(api_error_response(&err, ctx.request_id, ctx.instance));
                    }
                }
            } else {
                String::new()
            };
            let mut status = vault_status(&ctx.app.data_dir, &game_id, state.get("settings"));
            if let Some(m) = status.as_object_mut() {
                m.insert("enabled".into(), Value::Bool(true));
            }
            Some(json_response(StatusCode::OK, &status, ctx.instance))
        }
        "/api/v2/saves/vault/diff" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            if !save_mgmt_enabled(&state) {
                return Some(api_error_response(
                    &ApiError::bad_request("Save management is disabled."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let game = match game_from_query(&state, ctx) {
                Ok(g) => g,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let snapshot_id = ctx.query_first("snapshot_id").unwrap_or_default();
            let snapshot_id = snapshot_id.trim();
            if snapshot_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("snapshot_id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            match diff_snapshot(&ctx.app.data_dir, &game, snapshot_id) {
                Ok(diff) => Some(json_response(StatusCode::OK, &diff, ctx.instance)),
                Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            }
        }
        _ => None,
    }
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/saves/vault/snapshot" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            if !save_mgmt_enabled(&state) {
                return Some(api_error_response(
                    &ApiError::bad_request("Save management is disabled."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let game = match game_from_payload(&state, payload) {
                Ok(g) => g,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let game_id = game
                .get("game_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            let trigger = {
                let t = str_field(payload, "trigger");
                if t.is_empty() {
                    "manual".to_owned()
                } else {
                    t.to_owned()
                }
            };
            if !matches!(
                trigger.as_str(),
                "manual" | "session_close" | "auto" | "pre-restore"
            ) {
                return Some(api_error_response(
                    &ApiError::bad_request("Unsupported Save Vault trigger."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let vault_enabled = state
                .get("settings")
                .and_then(|s| s.get("save_vault_enabled"))
                .and_then(Value::as_bool)
                .unwrap_or(true);
            if matches!(trigger.as_str(), "session_close" | "auto") && !vault_enabled {
                return Some(api_error_response(
                    &ApiError::bad_request("Automatic Save Vault snapshots are disabled."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let keep = clamp_keep(
                state
                    .get("settings")
                    .and_then(|s| s.get("save_vault_keep"))
                    .and_then(Value::as_i64),
                DEFAULT_KEEP,
            );
            let include_shared = matches!(trigger.as_str(), "manual" | "pre-restore");
            if let Err(err) = resolve_game_save_roots(&game, include_shared, None) {
                return Some(api_error_response(&err, ctx.request_id, ctx.instance));
            }
            let data_dir = ctx.app.data_dir.clone();
            let name = format!(
                "save-vault-snapshot:{}",
                if game_id.is_empty() { "game" } else { &game_id }
            );
            let gid_ret = game_id.clone();
            let job = match ctx.app.jobs.submit(&name, false, None, None, move |jctx| {
                let mut cb = |cur: usize, total: usize| {
                    jctx.progress(json!({
                        "current": cur,
                        "total": total,
                        "message": "Snapshotting saves",
                    }));
                };
                create_snapshot(
                    &data_dir,
                    &game,
                    &trigger,
                    Some(include_shared),
                    None,
                    Some(keep),
                    Some(&mut cb),
                )
                .map_err(|e| e.message)
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
                &json!({"state": "queued", "job_id": job["job_id"], "game_id": gid_ret}),
                ctx.instance,
            ))
        }
        "/api/v2/saves/vault/restore" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            if !save_mgmt_enabled(&state) {
                return Some(api_error_response(
                    &ApiError::bad_request("Save management is disabled."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let game = match game_from_payload(&state, payload) {
                Ok(g) => g,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let game_id = game
                .get("game_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            let snapshot_id = str_field(payload, "snapshot_id").to_owned();
            if snapshot_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("snapshot_id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let dry_run = bool_field(payload, "dry_run");
            let skip_safety = bool_field(payload, "skip_safety");
            let include_removals = bool_field(payload, "include_removals");
            let data_dir = ctx.app.data_dir.clone();
            if dry_run {
                return match restore_snapshot(
                    &data_dir,
                    &game,
                    &snapshot_id,
                    true,
                    skip_safety,
                    include_removals,
                    None,
                ) {
                    Ok(res) => Some(json_response(StatusCode::OK, &res, ctx.instance)),
                    Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
                };
            }
            let name = format!(
                "save-vault-restore:{}",
                if game_id.is_empty() { "game" } else { &game_id }
            );
            let snap_ret = snapshot_id.clone();
            let job = match ctx.app.jobs.submit(&name, false, None, None, move |jctx| {
                let mut cb = |cur: usize, total: usize| {
                    jctx.progress(json!({
                        "current": cur,
                        "total": total,
                        "message": "Restoring saves",
                    }));
                };
                restore_snapshot(
                    &data_dir,
                    &game,
                    &snapshot_id,
                    false,
                    skip_safety,
                    include_removals,
                    Some(&mut cb),
                )
                .map_err(|e| e.message)
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
                &json!({"state": "queued", "job_id": job["job_id"], "snapshot_id": snap_ret}),
                ctx.instance,
            ))
        }
        "/api/v2/saves/vault/delete" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            if !save_mgmt_enabled(&state) {
                return Some(api_error_response(
                    &ApiError::bad_request("Save management is disabled."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let snapshot_id = str_field(payload, "snapshot_id");
            if snapshot_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("snapshot_id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let game = if payload.get("id").is_some_and(|v| !v.is_null())
                || !str_field(payload, "game_id").is_empty()
            {
                match game_from_payload(&state, payload) {
                    Ok(g) => Some(g),
                    Err(err) => {
                        return Some(api_error_response(&err, ctx.request_id, ctx.instance));
                    }
                }
            } else {
                None
            };
            match delete_snapshot(&ctx.app.data_dir, game.as_ref(), snapshot_id) {
                Ok(res) => Some(json_response(StatusCode::OK, &res, ctx.instance)),
                Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            }
        }
        _ => None,
    }
}
