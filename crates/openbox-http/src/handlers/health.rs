//! Health, logs, diagnostics, portable redacted backup export, Backup Format v2 create/restore/diff,
//! cloud statistics sync, causal library sync v3, and shutdown (`handlers/health.py`).

use std::collections::HashSet;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use axum::http::StatusCode;
use axum::response::Response;
use openbox_core::{ApiError, redact_state_for_export};
use openbox_integrations::updates::{check_update, install_desktop_entry, install_update};
use openbox_io::{build_preview, read_diagnostic_log};
use openbox_platform::user_symlink_component;
use openbox_sync::backup::{
    DIFF_PREVIEW_LIMIT, WRITABLE_BACKUP_FORMATS, create_backup, diff_manifests, restore_backup,
};
use openbox_sync::cloud_sync::{CloudSyncError, sync_statistics};
use openbox_sync::library_sync::{
    SyncError, apply_sync, preview_sync, publish_outbox, read_events, state_token, sync_enabled,
};
use serde_json::{Map, Value, json};

use crate::handlers::shared::{RequestCtx, bool_field, str_field};
use crate::middleware::{api_error_response, bytes_response, json_response, state_error_response};

pub const BACKUP_ITEMS: &[&str] = &[
    "extension_data",
    "library",
    "media",
    "plugins",
    "settings",
    "themes",
];

fn approved_backup_file(data_dir: &Path, raw: &str) -> Result<PathBuf, (u16, String)> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Err((400, "Backup path is required.".to_owned()));
    }
    let backups_dir = data_dir.join("backups");
    let raw_path = Path::new(trimmed);
    let candidate = if raw_path.is_absolute() {
        raw_path.to_path_buf()
    } else {
        let Some(fname) = raw_path.file_name().and_then(|s| s.to_str()) else {
            return Err((
                400,
                "backup archive is not an approved OpenBox backup".to_owned(),
            ));
        };
        if fname != trimmed || trimmed.contains('/') || trimmed.contains('\\') {
            return Err((
                400,
                "backup archive is not an approved OpenBox backup".to_owned(),
            ));
        }
        backups_dir.join(fname)
    };
    if user_symlink_component(&candidate).is_some() {
        return Err((
            400,
            "backup archive is not an approved OpenBox backup".to_owned(),
        ));
    }
    let fname = candidate.file_name().and_then(|s| s.to_str()).unwrap_or("");
    if !fname.starts_with("OpenBoxBackup-") || !fname.ends_with(".zip") {
        return Err((
            400,
            "backup archive is not an approved OpenBox backup".to_owned(),
        ));
    }
    if !candidate.is_file() {
        return Err((404, "backup archive not found".to_owned()));
    }
    let Ok(resolved_dir) = std::fs::canonicalize(&backups_dir) else {
        return Err((404, "backup archive not found".to_owned()));
    };
    let Ok(resolved_file) = std::fs::canonicalize(&candidate) else {
        return Err((404, "backup archive not found".to_owned()));
    };
    if !resolved_file.starts_with(&resolved_dir) {
        return Err((
            400,
            "backup archive is not an approved OpenBox backup".to_owned(),
        ));
    }
    Ok(resolved_file)
}

fn has_running_games(app: &openbox_runtime::AppState) -> bool {
    !app.registry.is_empty()
}

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
        "/api/v2/backup/diff" => {
            let archive_name = ctx.query_first("archive").unwrap_or_default();
            let archive_name = archive_name.trim();
            if archive_name.is_empty() {
                return Some(json_response(
                    StatusCode::BAD_REQUEST,
                    &json!({"error": "archive parameter required"}),
                    ctx.instance,
                ));
            }
            let archive = match approved_backup_file(&ctx.app.data_dir, archive_name) {
                Ok(p) => p,
                Err((404, msg)) => {
                    return Some(json_response(
                        StatusCode::NOT_FOUND,
                        &json!({"error": msg}),
                        ctx.instance,
                    ));
                }
                Err((_, msg)) => {
                    return Some(json_response(
                        StatusCode::BAD_REQUEST,
                        &json!({"error": msg}),
                        ctx.instance,
                    ));
                }
            };
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            match diff_manifests(&state, &archive, DIFF_PREVIEW_LIMIT) {
                Ok(res) => Some(json_response(StatusCode::OK, &res, ctx.instance)),
                Err(err) => Some(json_response(
                    StatusCode::BAD_REQUEST,
                    &json!({"error": err.message}),
                    ctx.instance,
                )),
            }
        }
        "/api/v2/update" => {
            let mut payload = match check_update() {
                Ok(v) => v,
                Err(err) => {
                    return Some(api_error_response(
                        &ApiError::bad_request(err),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let last_checked = ctx
                .app
                .load_state_view()
                .ok()
                .and_then(|s| {
                    s.get("settings")?
                        .get("last_update_check")?
                        .as_str()
                        .map(str::to_owned)
                })
                .unwrap_or_default();
            if let Some(obj) = payload.as_object_mut() {
                obj.insert("last_checked".into(), Value::String(last_checked));
            }
            Some(json_response(StatusCode::OK, &payload, ctx.instance))
        }
        _ => None,
    }
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/shutdown" | "/api/host/shutdown" => {
            ctx.app.request_shutdown();
            Some(json_response(
                StatusCode::OK,
                &json!({"ok": true, "shutting_down": true}),
                ctx.instance,
            ))
        }
        "/api/v2/backup/create" => {
            let items: Vec<String> = match payload.get("items") {
                Some(Value::Array(arr)) => arr
                    .iter()
                    .filter_map(Value::as_str)
                    .map(str::to_owned)
                    .collect(),
                Some(Value::String(s)) => vec![s.clone()],
                _ => vec!["library".to_owned(), "settings".to_owned()],
            };
            let keep = payload.get("keep").and_then(Value::as_u64).unwrap_or(0) as usize;
            let format_version = match payload.get("format") {
                None | Some(Value::Null) => 2i64,
                Some(Value::Number(n)) => match n.as_i64() {
                    Some(v) => v,
                    None => {
                        return Some(api_error_response(
                            &ApiError::bad_request("Backup format must be an integer."),
                            ctx.request_id,
                            ctx.instance,
                        ));
                    }
                },
                Some(Value::String(s)) => match s.trim().parse::<i64>() {
                    Ok(v) => v,
                    Err(_) => {
                        return Some(api_error_response(
                            &ApiError::bad_request("Backup format must be an integer."),
                            ctx.request_id,
                            ctx.instance,
                        ));
                    }
                },
                _ => {
                    return Some(api_error_response(
                        &ApiError::bad_request("Backup format must be an integer."),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            if !WRITABLE_BACKUP_FORMATS.contains(&format_version) {
                return Some(api_error_response(
                    &ApiError::backup_format_retired(
                        "OpenBox 2.1 writes backup format 2 only. Use a 2.0.x build for a 1.19-compatible export.",
                    )
                    .with_detail(json!({
                        "retired": format_version,
                        "writable": WRITABLE_BACKUP_FORMATS,
                        "since": "2.1.0",
                    })),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let app = Arc::clone(&ctx.app);
            let job = match ctx
                .app
                .jobs
                .submit("library-backup", false, None, None, move |_jctx| {
                    let st = app.load_state().map_err(|e| e.to_string())?;
                    let archive = create_backup(
                        &app.data_dir,
                        &st,
                        Some(&items),
                        keep,
                        has_running_games(&app),
                        format_version,
                    )
                    .map_err(|e| e.message)?;
                    let name = archive
                        .file_name()
                        .and_then(|s| s.to_str())
                        .unwrap_or("")
                        .to_owned();
                    Ok(json!({"archive": archive.to_string_lossy(), "name": name}))
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
                &json!({"state": "queued", "job_id": job["job_id"]}),
                ctx.instance,
            ))
        }
        "/api/v2/backup/restore" => {
            let raw_path = str_field(payload, "path");
            if raw_path.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("Backup path is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let archive = match approved_backup_file(&ctx.app.data_dir, raw_path) {
                Ok(p) => p,
                Err((_, msg)) => {
                    return Some(api_error_response(
                        &ApiError::bad_request(msg),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let items: Option<Vec<String>> = match payload.get("items") {
                None | Some(Value::Null) => None,
                Some(Value::Array(arr)) => Some(
                    arr.iter()
                        .filter_map(Value::as_str)
                        .map(str::to_owned)
                        .collect(),
                ),
                Some(Value::String(s)) => Some(vec![s.clone()]),
                _ => None,
            };
            let force = bool_field(payload, "force");
            let app = Arc::clone(&ctx.app);
            let job =
                match ctx
                    .app
                    .jobs
                    .submit("library-restore", false, None, None, move |_jctx| {
                        let res = restore_backup(
                            &archive,
                            &app.data_dir,
                            items.as_deref(),
                            has_running_games(&app),
                            force,
                        )
                        .map_err(|e| e.message)?;
                        let restored = res.get("items").cloned().unwrap_or_else(|| json!([]));
                        if restored
                            .as_array()
                            .is_some_and(|a| a.iter().any(|x| x.as_str() == Some("media")))
                        {
                            app.bump_media_epoch();
                        }
                        app.invalidate_caches();
                        Ok(json!({"restored": restored, "files": res["files"]}))
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
                &json!({"state": "queued", "job_id": job["job_id"]}),
                ctx.instance,
            ))
        }
        "/api/v2/cloud/sync" => {
            let app = Arc::clone(&ctx.app);
            let job = match ctx
                .app
                .jobs
                .submit("cloud-sync", false, None, None, move |_jctx| {
                    let state = app.load_state().map_err(|e| e.to_string())?;
                    let folder = state
                        .get("settings")
                        .and_then(|s| s.get("cloud_folder"))
                        .and_then(Value::as_str)
                        .map(str::trim)
                        .unwrap_or("")
                        .to_owned();
                    if folder.is_empty() {
                        return Err("Configure a cloud sync folder first.".to_owned());
                    }
                    let (_, res) = app
                        .transact_state(|cur| Ok(sync_statistics(cur, &folder, None)))
                        .map_err(|e| e.to_string())?;
                    res.map_err(|e| e.message)
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
                &json!({"state": "queued", "job_id": job["job_id"]}),
                ctx.instance,
            ))
        }
        "/api/v2/library/sync/publish" => {
            if payload.get("protocol").and_then(Value::as_str) != Some("v3") {
                let err = CloudSyncError::library_sync_unavailable();
                return Some(json_response(
                    StatusCode::SERVICE_UNAVAILABLE,
                    &json!({"error": err.message, "code": err.code}),
                    ctx.instance,
                ));
            }
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            if !sync_enabled(&state) {
                return Some(json_response(
                    StatusCode::BAD_REQUEST,
                    &json!({"error": "Enable library synchronization before publishing.", "code": "SYNC_NOT_ENABLED"}),
                    ctx.instance,
                ));
            }
            let folder = state
                .get("settings")
                .and_then(|s| s.get("cloud_folder"))
                .and_then(Value::as_str)
                .unwrap_or("");
            if folder.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("Configure a mounted cloud sync folder first."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let base_token = match state_token(&state) {
                Ok(t) => t,
                Err(e) => {
                    return Some(api_error_response(
                        &ApiError::bad_request(e.to_string()),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let pending_ids: HashSet<String> = state
                .get("library_sync")
                .and_then(|m| m.get("outbox"))
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .filter_map(|i| {
                            i.get("event_id").and_then(Value::as_str).map(str::to_owned)
                        })
                        .collect()
                })
                .unwrap_or_default();
            let mut detached = (*state).clone();
            let mut result = match publish_outbox(&mut detached, folder) {
                Ok(r) => r,
                Err(err) => {
                    return Some(api_error_response(
                        &ApiError::bad_request(err.to_string()),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let ack_res = ctx.app.transact_state(|cur| {
                if state_token(cur).unwrap_or_default() != base_token {
                    return Ok(Err("stale".to_owned()));
                }
                if let Some(root) = cur.as_object_mut() {
                    let meta = root
                        .entry("library_sync".to_owned())
                        .or_insert_with(|| Value::Object(Map::new()));
                    if let Some(ob) = meta.get_mut("outbox").and_then(Value::as_array_mut) {
                        ob.retain(|i| {
                            i.get("event_id")
                                .and_then(Value::as_str)
                                .is_none_or(|id| !pending_ids.contains(id))
                        });
                    }
                }
                Ok(Ok(pending_ids.len()))
            });
            match ack_res {
                Ok((_, Ok(ack))) => {
                    if let Some(m) = result.as_object_mut() {
                        m.insert("acknowledged".into(), json!(ack));
                        m.insert("protocol".into(), Value::String("v3".to_owned()));
                    }
                    Some(json_response(StatusCode::OK, &result, ctx.instance))
                }
                Ok((_, Err(_))) => Some(api_error_response(
                    &ApiError::conflict(
                        "Local library changed while sync was publishing; retry the operation.",
                    )
                    .with_code("SYNC_PUBLISH_STALE"),
                    ctx.request_id,
                    ctx.instance,
                )),
                Err(e) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/library/sync/pull" => {
            let err = CloudSyncError::library_sync_unavailable();
            Some(json_response(
                StatusCode::SERVICE_UNAVAILABLE,
                &json!({"error": err.message, "code": err.code}),
                ctx.instance,
            ))
        }
        "/api/v2/library/sync/preview" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            if !sync_enabled(&state) {
                return Some(json_response(
                    StatusCode::BAD_REQUEST,
                    &json!({"error": "Enable library synchronization before reviewing changes.", "code": "SYNC_NOT_ENABLED"}),
                    ctx.instance,
                ));
            }
            let folder = state
                .get("settings")
                .and_then(|s| s.get("cloud_folder"))
                .and_then(Value::as_str)
                .unwrap_or("");
            let incoming = if let Some(arr) = payload.get("events").and_then(Value::as_array) {
                arr.clone()
            } else {
                match read_events(folder) {
                    Ok(evs) => evs,
                    Err(err) => {
                        return Some(json_response(
                            StatusCode::BAD_REQUEST,
                            &json!({"error": err.to_string(), "code": "SYNC_INVALID"}),
                            ctx.instance,
                        ));
                    }
                }
            };
            match preview_sync(&state, &incoming, None) {
                Ok(plan) => Some(json_response(StatusCode::OK, &plan, ctx.instance)),
                Err(err) => Some(json_response(
                    StatusCode::BAD_REQUEST,
                    &json!({"error": err.to_string(), "code": "SYNC_INVALID"}),
                    ctx.instance,
                )),
            }
        }
        "/api/v2/library/sync/apply" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            if !sync_enabled(&state) {
                return Some(json_response(
                    StatusCode::BAD_REQUEST,
                    &json!({"error": "Enable library synchronization before applying changes.", "code": "SYNC_NOT_ENABLED"}),
                    ctx.instance,
                ));
            }
            let Some(plan) = payload.get("plan").filter(|v| v.is_object()) else {
                return Some(api_error_response(
                    &ApiError::bad_request("A reviewed sync plan is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let conflicts = payload.get("conflicts");
            let recovery = match create_backup(
                &ctx.app.data_dir,
                &state,
                Some(&["library".to_owned()]),
                3,
                has_running_games(&ctx.app),
                2,
            ) {
                Ok(p) => p,
                Err(err) => {
                    return Some(api_error_response(
                        &ApiError::bad_request(format!(
                            "Unable to create sync recovery backup: {}",
                            err.message
                        )),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let rec_name = recovery
                .file_name()
                .and_then(|s| s.to_str())
                .unwrap_or("")
                .to_owned();
            let res = ctx.app.transact_state(|cur| {
                if let Some(root) = cur.as_object_mut() {
                    let meta = root
                        .entry("library_sync".to_owned())
                        .or_insert_with(|| Value::Object(Map::new()));
                    if let Some(m) = meta.as_object_mut() {
                        m.insert("_suppress_local_recording".into(), Value::Bool(true));
                    }
                }
                Ok(apply_sync(cur, plan, conflicts, None))
            });
            match res {
                Ok((_, Ok(out))) => Some(json_response(
                    StatusCode::OK,
                    &json!({
                        "applied": out["applied"],
                        "conflicts": out["conflicts"],
                        "changed": out["changed"],
                        "recovery_backup": rec_name,
                    }),
                    ctx.instance,
                )),
                Ok((_, Err(SyncError::Stale(msg)))) => Some(api_error_response(
                    &ApiError::conflict(msg).with_code("SYNC_PREVIEW_STALE"),
                    ctx.request_id,
                    ctx.instance,
                )),
                Ok((_, Err(err))) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
                Err(e) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/update/install" => {
            let root_dir = ctx.app.root_dir.clone();
            match ctx.app.jobs.submit(
                "updater-install",
                false,
                Some("updater_install"),
                None,
                move |_jctx| {
                    let update = check_update()?;
                    install_update(&update, Some(&root_dir))
                },
            ) {
                Ok(job) => Some(json_response(
                    StatusCode::ACCEPTED,
                    &json!({"state": "queued", "job_id": job["job_id"]}),
                    ctx.instance,
                )),
                Err(e) => Some(api_error_response(
                    &ApiError::job_state_conflict(e),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/desktop/install" => match install_desktop_entry(&ctx.app.root_dir) {
            Ok(desktop) => Some(json_response(
                StatusCode::OK,
                &json!({"desktop": desktop}),
                ctx.instance,
            )),
            Err(err) => Some(api_error_response(
                &ApiError::bad_request(err),
                ctx.request_id,
                ctx.instance,
            )),
        },
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
