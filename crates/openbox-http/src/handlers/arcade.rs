//! Arcade Room museum kiosk PIN routes. Mirrors `handlers/arcade.py`.

use axum::http::StatusCode;
use axum::response::Response;
use openbox_core::ApiError;
use openbox_emulators::arcade;
use serde_json::{Map, Value, json};

use crate::handlers::shared::{RequestCtx, bool_field};
use crate::middleware::{api_error_response, json_response, state_error_response};

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    if ctx.path != "/api/v2/arcade/kiosk/status" {
        return None;
    }
    let state = match ctx.app.load_state_view() {
        Ok(s) => s,
        Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
    };
    Some(json_response(
        StatusCode::OK,
        &arcade::kiosk_status(&state),
        ctx.instance,
    ))
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/arcade/kiosk/pin" => {
            let supplied = payload
                .get("pin")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty());
            let clear = bool_field(payload, "clear");
            if supplied.is_some() && clear {
                return Some(api_error_response(
                    &ApiError::new(
                        400,
                        "ARCADE_INVALID_REQUEST",
                        "Choose a PIN or clear the existing PIN, not both.",
                    ),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let new_hash = match supplied {
                Some(pin) => match arcade::hash_kiosk_pin(pin, None, arcade::PIN_ITERATIONS) {
                    Ok(h) => Some(h),
                    Err(err) => {
                        return Some(api_error_response(&err, ctx.request_id, ctx.instance));
                    }
                },
                None => None,
            };
            let enabled = payload.get("enabled").and_then(Value::as_bool);
            match ctx.app.transact_state(move |st| {
                let settings = st
                    .as_object_mut()
                    .unwrap()
                    .entry("settings")
                    .or_insert_with(|| Value::Object(Map::new()))
                    .as_object_mut()
                    .unwrap();
                if let Some(h) = new_hash {
                    settings.insert("museum_kiosk_pin_hash".into(), Value::String(h));
                } else if clear {
                    settings.remove("museum_kiosk_pin_hash");
                }
                if let Some(en) = enabled {
                    settings.insert("museum_kiosk_enabled".into(), Value::Bool(en));
                }
                Ok(arcade::kiosk_status(st))
            }) {
                Ok((_, status)) => Some(json_response(StatusCode::OK, &status, ctx.instance)),
                Err(e) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/arcade/kiosk/verify" => {
            let Some(pin) = payload.get("pin").and_then(Value::as_str) else {
                return Some(api_error_response(
                    &ApiError::new(400, "ARCADE_INVALID_PIN", "Museum PIN must be text."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            if pin.len() > arcade::PIN_MAX_LENGTH {
                return Some(api_error_response(
                    &ApiError::new(400, "ARCADE_INVALID_PIN", "Museum PIN must be text."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let stored = state
                .get("settings")
                .and_then(|s| s.get("museum_kiosk_pin_hash"))
                .and_then(Value::as_str)
                .unwrap_or("");
            Some(json_response(
                StatusCode::OK,
                &json!({"ok": arcade::verify_kiosk_pin(stored, pin)}),
                ctx.instance,
            ))
        }
        _ => None,
    }
}
