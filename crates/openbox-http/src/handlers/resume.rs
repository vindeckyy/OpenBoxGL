//! Quick Resume v2 + host routes (`handlers/resume.py`).

use axum::http::StatusCode;
use axum::response::Response;
use openbox_core::ApiError;
use openbox_emulators::{load_adapters, start_game_with_extra_args};
use openbox_integrations::resume::{
    discard_resume_state, inject_resume_args, resume_status, validate_resume_slot,
};
use serde_json::{Map, Value, json};

use crate::handlers::projection::find_game_index_in_state;
use crate::handlers::shared::{RequestCtx, bool_field};
use crate::middleware::{api_error_response, json_response, state_error_response};

fn game_from_query(state: &Value, ctx: &RequestCtx<'_>) -> Result<(usize, Value), ApiError> {
    let mut payload = Map::new();
    if let Some(gid) = ctx.query_first("game_id").filter(|s| !s.is_empty()) {
        payload.insert("game_id".into(), Value::String(gid));
    }
    if let Some(id) = ctx.query_first("id").filter(|s| !s.is_empty()) {
        if let Ok(n) = id.parse::<i64>() {
            payload.insert("id".into(), json!(n));
        } else {
            payload.insert("id".into(), Value::String(id));
        }
    }
    let idx = find_game_index_in_state(state, &payload)
        .map_err(|_| ApiError::new(400, "GAME_NOT_FOUND", "Game not found."))?;
    let game = state
        .get("games")
        .and_then(Value::as_array)
        .and_then(|a| a.get(idx))
        .cloned()
        .ok_or_else(|| ApiError::new(400, "GAME_NOT_FOUND", "Game not found."))?;
    Ok((idx, game))
}

fn game_from_payload(
    state: &Value,
    payload: &Map<String, Value>,
) -> Result<(usize, Value), ApiError> {
    let idx = find_game_index_in_state(state, payload)
        .map_err(|_| ApiError::new(400, "GAME_NOT_FOUND", "Game not found."))?;
    let game = state
        .get("games")
        .and_then(Value::as_array)
        .and_then(|a| a.get(idx))
        .cloned()
        .ok_or_else(|| ApiError::new(400, "GAME_NOT_FOUND", "Game not found."))?;
    Ok((idx, game))
}

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    if ctx.path != "/api/v2/resume/status" {
        return None;
    }
    let state = match ctx.app.load_state() {
        Ok(s) => s,
        Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
    };
    let (_, game) = match game_from_query(&state, ctx) {
        Ok(pair) => pair,
        Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
    };
    let settings = state
        .get("settings")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let profiles = state
        .get("profiles")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let adapters = load_adapters(&ctx.app.root_dir, Some(&ctx.app.data_dir));
    let status = resume_status(&game, &settings, &profiles, &adapters, &ctx.app.data_dir);
    Some(json_response(StatusCode::OK, &status, ctx.instance))
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/resume" | "/api/host/resume" => {
            let state = match ctx.app.load_state() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let (idx, game) = match game_from_payload(&state, payload) {
                Ok(pair) => pair,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let settings = state
                .get("settings")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            let profiles = state
                .get("profiles")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            let adapters = load_adapters(&ctx.app.root_dir, Some(&ctx.app.data_dir));
            let status = resume_status(&game, &settings, &profiles, &adapters, &ctx.app.data_dir);
            if !status
                .get("enabled")
                .and_then(Value::as_bool)
                .unwrap_or(false)
            {
                return Some(api_error_response(
                    &ApiError::new(
                        400,
                        "QUICK_RESUME_DISABLED",
                        "Quick Resume is disabled in Settings.",
                    ),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if !status
                .get("capable")
                .and_then(Value::as_bool)
                .unwrap_or(false)
            {
                return Some(api_error_response(
                    &ApiError::new(
                        400,
                        "RESUME_UNSUPPORTED",
                        "This launch path cannot resume from a saved state.",
                    ),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if !status
                .get("available")
                .and_then(Value::as_bool)
                .unwrap_or(false)
            {
                return Some(api_error_response(
                    &ApiError::new(
                        404,
                        "RESUME_STATE_MISSING",
                        "No captured state exists for this game.",
                    ),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let allow_stale = bool_field(payload, "allow_stale");
            if status
                .get("stale")
                .and_then(Value::as_bool)
                .unwrap_or(false)
                && !allow_stale
            {
                return Some(api_error_response(
                    &ApiError::new(
                        409,
                        "RESUME_STATE_STALE",
                        "The stored state was captured by a different adapter or emulator build. Start clean, or pass allow_stale to load it anyway.",
                    ),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let state_file = status
                .get("state")
                .and_then(|s| s.get("file"))
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            let extra_args = match inject_resume_args(
                &game,
                &[],
                &profiles,
                &settings,
                &adapters,
                Some(&state_file),
                &ctx.app.data_dir,
                allow_stale,
            ) {
                Ok(a) => a,
                Err(e) => {
                    return Some(api_error_response(
                        &ApiError::bad_request(e),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let stable_id = game.get("game_id").and_then(Value::as_str).unwrap_or("");
            match start_game_with_extra_args(&ctx.app, idx, stable_id, &extra_args) {
                Ok(entry) => Some(json_response(
                    StatusCode::OK,
                    &json!({
                        "ok": true,
                        "resumed_from_state": true,
                        "launch_id": entry.get("launch_id").and_then(Value::as_str).unwrap_or(""),
                        "state_file": state_file,
                    }),
                    ctx.instance,
                )),
                Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/resume/slot" => {
            let slot_raw = payload
                .get("slot")
                .map(|v| match v {
                    Value::Null => None,
                    Value::String(s) => Some(s.as_str()),
                    _ => Some(""),
                })
                .unwrap_or(None);
            let slot = match validate_resume_slot(slot_raw) {
                Ok(s) => s,
                Err(err) => {
                    return Some(api_error_response(
                        &ApiError::new(400, "RESUME_SLOT_INVALID", err),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let state = match ctx.app.load_state() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let (_, game) = match game_from_payload(&state, payload) {
                Ok(pair) => pair,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let gid = game
                .get("game_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            let payload_c = payload.clone();
            let slot_c = slot.clone();
            if let Err(e) = ctx.app.transact_state(move |st| {
                if let Ok(idx) = find_game_index_in_state(st, &payload_c)
                    && let Some(target) = st
                        .get_mut("games")
                        .and_then(Value::as_array_mut)
                        .and_then(|a| a.get_mut(idx))
                        .and_then(Value::as_object_mut)
                {
                    if slot_c.is_empty() {
                        target.remove("resume_slot");
                    } else {
                        target.insert("resume_slot".into(), Value::String(slot_c));
                    }
                }
                Ok(())
            }) {
                return Some(state_error_response(&e, ctx.request_id, ctx.instance));
            }
            ctx.app.invalidate_caches();
            Some(json_response(
                StatusCode::OK,
                &json!({"ok": true, "game_id": gid, "slot": slot}),
                ctx.instance,
            ))
        }
        "/api/v2/resume/discard" => {
            let state = match ctx.app.load_state() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let (_, game) = match game_from_payload(&state, payload) {
                Ok(pair) => pair,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            match discard_resume_state(&game, &ctx.app.data_dir) {
                Ok(true) => Some(json_response(
                    StatusCode::OK,
                    &json!({"ok": true, "discarded": true}),
                    ctx.instance,
                )),
                Ok(false) => Some(api_error_response(
                    &ApiError::new(
                        404,
                        "RESUME_STATE_MISSING",
                        "No captured state exists for this game.",
                    ),
                    ctx.request_id,
                    ctx.instance,
                )),
                Err(err) => Some(api_error_response(
                    &ApiError::bad_request(err),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        _ => None,
    }
}
