//! Signed emulator definition pack update/status/rollback routes (ADR 0060).
//! Mirrors `handlers/defs.py`.

use axum::http::StatusCode;
use axum::response::Response;
use openbox_emulators::defs_update::{self, DefsUpdateError};
use openbox_runtime::NotificationSpec;
use serde_json::{Map, Value, json};

use crate::handlers::shared::RequestCtx;
use crate::middleware::json_response;

pub async fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    match ctx.path {
        "/api/v2/emulators/defs/status" => Some(json_response(
            StatusCode::OK,
            &defs_update::status(&ctx.app.root_dir, &ctx.app.data_dir),
            ctx.instance,
        )),
        "/api/v2/emulators/defs/update" => Some(json_response(
            StatusCode::OK,
            &defs_update::check(&ctx.app.data_dir).await,
            ctx.instance,
        )),
        _ => None,
    }
}

pub async fn handle_post(ctx: &RequestCtx<'_>, _payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/emulators/defs/update" => {
            match defs_update::install(&ctx.app.root_dir, &ctx.app.data_dir).await {
                Ok(res) => Some(json_response(StatusCode::OK, &res, ctx.instance)),
                Err(DefsUpdateError::Signature(msg)) => {
                    let mut params = Map::new();
                    params.insert("detail".into(), Value::String(msg.clone()));
                    let body = format!(
                        "A downloaded emulator definition pack failed signature verification \
                         and was discarded. Nothing was changed. Details: {msg}"
                    );
                    ctx.app.emit_notification(NotificationSpec {
                        kind: "security",
                        level: "error",
                        title: "Emulator definition update rejected",
                        body: &body,
                        source: "defs_update",
                        title_key: "notify.server.defs_rejected_title",
                        body_key: "notify.server.defs_rejected_body",
                        params: Some(&params),
                        ..Default::default()
                    });
                    Some(json_response(
                        StatusCode::BAD_REQUEST,
                        &json!({"error": "signature_verification_failed", "detail": msg}),
                        ctx.instance,
                    ))
                }
                Err(DefsUpdateError::Definition(msg)) => Some(json_response(
                    StatusCode::BAD_REQUEST,
                    &json!({"error": "invalid_definition_pack", "detail": msg}),
                    ctx.instance,
                )),
            }
        }
        "/api/v2/emulators/defs/rollback" => Some(json_response(
            StatusCode::OK,
            &defs_update::rollback(&ctx.app.data_dir),
            ctx.instance,
        )),
        _ => None,
    }
}
