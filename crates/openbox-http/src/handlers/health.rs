//! Health, logs, diagnostics, portable redacted backup export, backup listing, and shutdown.
//! Mirrors `handlers/health.py` (Phase 3 native slice; cloud/backup/sync jobs expand in Phase 7).

use std::io::Read;

use axum::http::StatusCode;
use axum::response::Response;
use openbox_core::redact_state_for_export;
use openbox_io::{build_preview, read_diagnostic_log};
use serde_json::{Map, Value, json};

use crate::handlers::shared::RequestCtx;
use crate::middleware::{bytes_response, json_response, state_error_response};

pub const BACKUP_ITEMS: &[&str] = &["library", "media", "plugins", "saves", "settings", "themes"];

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    match ctx.path {
        "/api/v2/log" => Some(json_response(
            StatusCode::OK,
            &json!({"log": read_diagnostic_log(&ctx.app.data_dir, 65_536)}),
            ctx.instance,
        )),
        "/api/v2/diagnostic" => {
            let mut recent_ids: Vec<String> = Vec::new();
            for job in ctx.app.jobs.history(10) {
                if let Some(jid) = job.get("job_id").and_then(Value::as_str)
                    && !jid.is_empty()
                {
                    recent_ids.push(jid.to_owned());
                }
            }
            for job in ctx.app.jobs.snapshots().values() {
                if let Some(jid) = job.get("job_id").and_then(Value::as_str)
                    && !jid.is_empty()
                    && !recent_ids.iter().any(|x| x == jid)
                {
                    recent_ids.push(jid.to_owned());
                }
            }
            recent_ids.truncate(10);
            let preview = build_preview(&ctx.app.data_dir, &recent_ids, None);
            let report = serde_json::to_string_pretty(&preview).unwrap_or_else(|_| "{}".to_owned());
            Some(json_response(
                StatusCode::OK,
                &json!({"report": report}),
                ctx.instance,
            ))
        }
        "/api/v2/backup" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let export = redact_state_for_export(&state);
            let data = serde_json::to_vec_pretty(&export).unwrap_or_else(|_| b"{}".to_vec());
            Some(bytes_response(
                StatusCode::OK,
                data,
                "application/json",
                "no-store",
                false,
                &[(
                    "Content-Disposition",
                    "attachment; filename=\"openbox-portable-redacted-export.json\"",
                )],
            ))
        }
        "/api/v2/backup/manifest" => Some(json_response(
            StatusCode::OK,
            &json!({"items": BACKUP_ITEMS}),
            ctx.instance,
        )),
        "/api/v2/backups" => {
            let folder = ctx.app.data_dir.join("backups");
            let mut entries: Vec<(std::time::SystemTime, std::path::PathBuf)> = Vec::new();
            if let Ok(rd) = std::fs::read_dir(&folder) {
                for ent in rd.flatten() {
                    let path = ent.path();
                    let name = ent.file_name().to_string_lossy().into_owned();
                    if name.starts_with("OpenBoxBackup-")
                        && name.ends_with(".zip")
                        && let Ok(meta) = ent.metadata()
                        && meta.is_file()
                    {
                        let mtime = meta.modified().unwrap_or(std::time::UNIX_EPOCH);
                        entries.push((mtime, path));
                    }
                }
            }
            entries.sort_by_key(|a| std::cmp::Reverse(a.0));
            let mut backups = Vec::with_capacity(entries.len());
            for (_, path) in entries {
                let size = std::fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
                let name = path
                    .file_name()
                    .map(|n| n.to_string_lossy().into_owned())
                    .unwrap_or_default();
                let manifest = read_zip_manifest(&path);
                let invalid = manifest
                    .get("invalid")
                    .and_then(Value::as_bool)
                    .unwrap_or(false);
                backups.push(json!({
                    "name": name,
                    "path": path.to_string_lossy(),
                    "size": size,
                    "created": manifest.get("created").and_then(Value::as_str).unwrap_or(""),
                    "items": manifest.get("items").cloned().unwrap_or_else(|| json!([])),
                    "invalid": invalid,
                }));
            }
            Some(json_response(
                StatusCode::OK,
                &json!({"backups": backups}),
                ctx.instance,
            ))
        }
        _ => None,
    }
}

pub fn handle_post(ctx: &RequestCtx<'_>, _payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/shutdown" | "/api/host/shutdown" => {
            ctx.app.request_shutdown();
            Some(json_response(
                StatusCode::OK,
                &json!({"ok": true, "shutting_down": true}),
                ctx.instance,
            ))
        }
        _ => None,
    }
}

fn read_zip_manifest(path: &std::path::Path) -> Value {
    let Ok(file) = std::fs::File::open(path) else {
        return json!({"items": [], "invalid": true});
    };
    let Ok(mut archive) = zip::ZipArchive::new(file) else {
        return json!({"items": [], "invalid": true});
    };
    let Ok(mut entry) = archive.by_name("manifest.json") else {
        return json!({});
    };
    let mut text = String::new();
    if entry.read_to_string(&mut text).is_err() {
        return json!({"items": [], "invalid": true});
    }
    serde_json::from_str::<Value>(&text).unwrap_or_else(|_| json!({"items": [], "invalid": true}))
}
