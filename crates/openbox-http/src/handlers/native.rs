//! Native host IPC & contract version handlers. Mirrors `handlers/native.py`.

use std::path::Path;

use axum::http::StatusCode;
use axum::response::Response;
use openbox_core::{
    API_VERSION, APP_VERSION, ApiError, BACKUP_FORMAT, PLUGIN_API_VERSION, STATE_SCHEMA_VERSION,
    SYNC_FORMAT,
};
use openbox_platform::{battery_status, platform_features, platform_key};
use serde_json::{Map, Value, json};

use crate::handlers::shared::{RequestCtx, str_field};
use crate::middleware::{api_error_response, json_response};

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    match ctx.path {
        "/api/v2/native/capabilities" | "/api/host/capabilities" => {
            let host_attached = std::env::var("OPENBOX_NATIVE_HOST").is_ok_and(|v| !v.is_empty());
            let sys_platform = if cfg!(target_os = "windows") {
                "win32"
            } else if cfg!(target_os = "macos") {
                "darwin"
            } else {
                "linux"
            };
            Some(json_response(
                StatusCode::OK,
                &json!({
                    "webview": host_attached,
                    "dialogs": host_attached,
                    "tray": host_attached,
                    "single_instance": host_attached,
                    "gamepad": "webkit",
                    "fullscreen": true,
                    "clipboard": true,
                    "version": APP_VERSION,
                    "platform": sys_platform,
                    "data_dir": ctx.app.data_dir.to_string_lossy(),
                    "features": platform_features(),
                }),
                ctx.instance,
            ))
        }
        "/api/v2/native/battery" => Some(json_response(
            StatusCode::OK,
            &serde_json::to_value(battery_status(None)).unwrap_or_else(|_| json!({})),
            ctx.instance,
        )),
        "/api/v2/version" => Some(json_response(
            StatusCode::OK,
            &json!({
                "version": APP_VERSION,
                "api_version": API_VERSION,
                "state_schema": STATE_SCHEMA_VERSION,
                "plugin_api_version": PLUGIN_API_VERSION,
                "backup_format": BACKUP_FORMAT,
                "sync_format": SYNC_FORMAT,
                "platform": platform_key(),
                "python": "",
                "runtime": "rust",
            }),
            ctx.instance,
        )),
        _ => None,
    }
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/native/dialog" => {
            let kind = {
                let k = str_field(payload, "kind");
                if k.is_empty() { "folder" } else { k }
            };
            if !matches!(kind, "folder" | "file" | "save") {
                return Some(api_error_response(
                    &ApiError::bad_request("Native dialog kind must be folder, file, or save."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let start_path = str_field(payload, "start_path");
            if !start_path.is_empty() && !Path::new(start_path).is_absolute() {
                return Some(api_error_response(
                    &ApiError::bad_request("Native dialog start_path must be absolute."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            Some(json_response(
                StatusCode::OK,
                &json!({"path": null, "cancelled": true}),
                ctx.instance,
            ))
        }
        "/api/v2/native/open-external" => {
            let target = {
                let p = str_field(payload, "path");
                if !p.is_empty() {
                    p
                } else {
                    str_field(payload, "url")
                }
            };
            if target.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("Native open-external needs a path or url."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            Some(json_response(
                StatusCode::OK,
                &json!({"ok": false, "error": null}),
                ctx.instance,
            ))
        }
        "/api/v2/native/reveal" => {
            if str_field(payload, "path").is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("Native reveal needs a path."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            Some(json_response(
                StatusCode::OK,
                &json!({"ok": false, "error": null}),
                ctx.instance,
            ))
        }
        "/api/v2/native/window" => {
            let action = str_field(payload, "action");
            if !matches!(
                action,
                "minimize" | "toggle-maximize" | "close" | "set-fullscreen" | "unset-fullscreen"
            ) {
                return Some(api_error_response(
                    &ApiError::bad_request("Native window action is not supported."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            Some(json_response(
                StatusCode::OK,
                &json!({"ok": false}),
                ctx.instance,
            ))
        }
        _ => None,
    }
}
