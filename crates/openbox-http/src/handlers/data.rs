//! Saves, save tools, documents (frameable), platform videos, netplay lobby, highscores table,
//! and Steam store URI action handlers (`handlers/data.py`).

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::{Arc, LazyLock};

use axum::http::StatusCode;
use axum::http::header::{CONTENT_DISPOSITION, HeaderValue};
use axum::response::Response;
use openbox_core::ApiError;
use openbox_emulators::highscores::table_for_game;
use openbox_emulators::netplay::lobby as netplay_lobby;
use openbox_integrations::gameyfin::{
    catalog_gameyfin, default_install_root, gameyfin_settings, install_gameyfin_game,
    test_gameyfin_connection, uninstall_gameyfin_game, validate_gameyfin_id,
};
use openbox_integrations::misc::{export_highscores, import_highscores};
use openbox_platform::{open_path, user_symlink_component};
use openbox_sync::save_tools::{run_hoard, run_ludusavi, save_tool_status};
use openbox_sync::saves::{
    SaveError, backup_saves, discover_save_paths, enforce_backup_limit, expand_user_path,
    extra_save_candidates, inspect_save_backup, list_backup_metadata, preview_restore,
    restore_saves, scan_all_saves,
};
use parking_lot::Mutex;
use regex::Regex;
use serde_json::{Map, Value, json};

use crate::handlers::projection::find_game_index_in_state;
use crate::handlers::shared::{RequestCtx, bool_field, str_field};
use crate::middleware::{api_error_response, json_response, state_error_response};
use crate::static_files::send_file;

const VIDEO_EXTS: &[&str] = &[".mp4", ".webm", ".ogv", ".ogg", ".mkv"];
static GAMEYFIN_INSTALLS: LazyLock<Mutex<HashMap<String, Value>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));
static HS_SLUG_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[^a-z0-9]+").unwrap());

fn save_management_enabled(state: &Value) -> bool {
    state
        .get("settings")
        .and_then(|s| s.get("save_management_enabled"))
        .and_then(Value::as_bool)
        .unwrap_or(true)
}

fn game_from_query(state: &Value, ctx: &RequestCtx<'_>) -> Option<(usize, Value)> {
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
    let idx = find_game_index_in_state(state, &payload).ok()?;
    let g = state.get("games")?.as_array()?.get(idx)?.clone();
    Some((idx, g))
}

fn game_from_payload(state: &Value, payload: &Map<String, Value>) -> Option<(usize, Value)> {
    let idx = find_game_index_in_state(state, payload).ok()?;
    let g = state.get("games")?.as_array()?.get(idx)?.clone();
    Some((idx, g))
}

fn safe_document_file(raw_path: &str) -> Result<PathBuf, ApiError> {
    let trimmed = raw_path.trim();
    if trimmed.is_empty() {
        return Err(ApiError::document_not_found("Document not found"));
    }
    let p = expand_user_path(trimmed);
    if user_symlink_component(&p).is_some() || !p.is_file() {
        return Err(ApiError::document_not_found("Document not found"));
    }
    Ok(p)
}

fn sanitize_document_records(items: Option<&Value>) -> Vec<Value> {
    let Some(arr) = items.and_then(Value::as_array) else {
        return Vec::new();
    };
    arr.iter()
        .filter_map(|item| {
            let obj = item.as_object()?;
            let path = obj
                .get("path")
                .and_then(Value::as_str)
                .map(str::trim)
                .unwrap_or("");
            if path.is_empty() {
                return None;
            }
            let name = obj
                .get("name")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .map(str::to_owned)
                .unwrap_or_else(|| {
                    Path::new(path)
                        .file_name()
                        .and_then(|s| s.to_str())
                        .unwrap_or("Document")
                        .to_owned()
                });
            Some(json!({ "name": name, "path": path }))
        })
        .collect()
}

fn clean_document_extras(raw: Option<&Value>) -> Result<Vec<Value>, ApiError> {
    let Some(arr) = raw.and_then(Value::as_array) else {
        return Ok(Vec::new());
    };
    let mut out = Vec::new();
    for item in arr.iter().take(200) {
        let Some(obj) = item.as_object() else {
            continue;
        };
        let path_raw = obj
            .get("path")
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or("");
        if path_raw.is_empty() {
            continue;
        }
        let expanded = expand_user_path(path_raw);
        if user_symlink_component(&expanded).is_some() {
            return Err(ApiError::bad_request(
                "Document path may not contain symlinks.",
            ));
        }
        let name = obj
            .get("name")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .unwrap_or_else(|| {
                expanded
                    .file_name()
                    .and_then(|s| s.to_str())
                    .unwrap_or("Document")
                    .to_owned()
            });
        out.push(json!({
            "name": name.chars().take(120).collect::<String>(),
            "path": expanded.to_string_lossy(),
        }));
    }
    Ok(out)
}

fn is_video_file(p: &Path) -> bool {
    let ext = p
        .extension()
        .and_then(|e| e.to_str())
        .map(|s| format!(".{}", s.to_lowercase()))
        .unwrap_or_default();
    VIDEO_EXTS.contains(&ext.as_str()) && p.is_file()
}

fn inline_document_response(ctx: &RequestCtx<'_>, path: &Path) -> Response {
    let mut resp = send_file(
        ctx.headers,
        path,
        None,
        None,
        true,
        ctx.request_id,
        ctx.instance,
    );
    let fname = path
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("document");
    let safe_name: String = fname
        .chars()
        .map(|c| {
            if matches!(c, '\r' | '\n' | '"') {
                '_'
            } else {
                c
            }
        })
        .collect();
    if let Ok(hv) = HeaderValue::from_str(&format!("inline; filename=\"{safe_name}\"")) {
        resp.headers_mut().insert(CONTENT_DISPOSITION, hv);
    }
    resp
}

fn save_err_to_api(err: SaveError) -> ApiError {
    ApiError::bad_request(err.to_string())
}

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    match ctx.path {
        "/api/v2/saves" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            if !save_management_enabled(&state) {
                return Some(json_response(
                    StatusCode::OK,
                    &json!({"enabled": false}),
                    ctx.instance,
                ));
            }
            let Some((_, game)) = game_from_query(&state, ctx) else {
                return Some(api_error_response(
                    &ApiError::game_not_found("Game not found"),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let backups = list_backup_metadata(&game, &ctx.app.data_dir.join("save-backups"))
                .unwrap_or_default()
                .into_iter()
                .take(200)
                .collect::<Vec<_>>();
            Some(json_response(
                StatusCode::OK,
                &json!({"backups": backups}),
                ctx.instance,
            ))
        }
        "/api/v2/saves/discover" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            if !save_management_enabled(&state) {
                return Some(json_response(
                    StatusCode::OK,
                    &json!({"enabled": false}),
                    ctx.instance,
                ));
            }
            let Some((_, game)) = game_from_query(&state, ctx) else {
                return Some(api_error_response(
                    &ApiError::game_not_found("Game not found"),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let configured: HashSet<String> = game
                .get("save_paths")
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .filter_map(Value::as_str)
                        .map(str::to_owned)
                        .collect()
                })
                .unwrap_or_default();
            let mut combined = discover_save_paths(&game, None);
            combined.extend(extra_save_candidates(&game, None));
            let candidates: Vec<Value> = combined
                .into_iter()
                .filter(|item| {
                    item.get("path")
                        .and_then(Value::as_str)
                        .is_some_and(|p| !configured.contains(p))
                })
                .collect();
            Some(json_response(
                StatusCode::OK,
                &json!({"candidates": candidates}),
                ctx.instance,
            ))
        }
        "/api/v2/saves/scan" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            if !save_management_enabled(&state) {
                return Some(json_response(
                    StatusCode::OK,
                    &json!({"enabled": false}),
                    ctx.instance,
                ));
            }
            let mut games = state
                .get("games")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            let found = scan_all_saves(&mut games, None);
            let mut map = Map::new();
            for (idx, paths) in &found {
                map.insert(
                    idx.to_string(),
                    Value::Array(paths.iter().cloned().map(Value::String).collect()),
                );
            }
            Some(json_response(
                StatusCode::OK,
                &json!({"games": Value::Object(map), "count": found.len()}),
                ctx.instance,
            ))
        }
        "/api/v2/saves/manifest" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            if !save_management_enabled(&state) {
                return Some(json_response(
                    StatusCode::OK,
                    &json!({"enabled": false}),
                    ctx.instance,
                ));
            }
            let Some((_, game)) = game_from_query(&state, ctx) else {
                return Some(api_error_response(
                    &ApiError::game_not_found("Game not found"),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let backup_name = ctx.query_first("backup").unwrap_or_default();
            let backup_name = backup_name.trim();
            if backup_name.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("A backup name is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            match inspect_save_backup(&game, &ctx.app.data_dir.join("save-backups"), backup_name) {
                Ok(res) => Some(json_response(StatusCode::OK, &res, ctx.instance)),
                Err(err) => Some(api_error_response(
                    &save_err_to_api(err),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/save-tools/status" => Some(json_response(
            StatusCode::OK,
            &save_tool_status(),
            ctx.instance,
        )),
        "/api/v2/document" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let Some((_, game)) = game_from_query(&state, ctx) else {
                return Some(api_error_response(
                    &ApiError::document_not_found("Document not found"),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let idx = ctx
                .query_first("index")
                .and_then(|s| s.parse::<usize>().ok());
            let doc_path = idx
                .and_then(|i| game.get("documents")?.as_array()?.get(i))
                .and_then(|d| d.get("path"))
                .and_then(Value::as_str)
                .unwrap_or("");
            match safe_document_file(doc_path) {
                Ok(p) => Some(inline_document_response(ctx, &p)),
                Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/platform/documents" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let platform = ctx.query_first("platform").unwrap_or_default();
            let docs = state
                .get("settings")
                .and_then(|s| s.get("platform_documents"))
                .and_then(Value::as_object);
            let result = if !platform.is_empty() {
                Value::Array(sanitize_document_records(
                    docs.and_then(|d| d.get(&platform)),
                ))
            } else {
                let mut m = Map::new();
                if let Some(d) = docs {
                    for (name, items) in d {
                        m.insert(
                            name.clone(),
                            Value::Array(sanitize_document_records(Some(items))),
                        );
                    }
                }
                Value::Object(m)
            };
            Some(json_response(
                StatusCode::OK,
                &json!({"documents": result}),
                ctx.instance,
            ))
        }
        "/api/v2/platform/document" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let platform = ctx.query_first("platform").unwrap_or_default();
            let idx = ctx
                .query_first("index")
                .and_then(|s| s.parse::<usize>().ok());
            let doc_path = idx
                .and_then(|i| {
                    state
                        .get("settings")?
                        .get("platform_documents")?
                        .get(&platform)?
                        .as_array()?
                        .get(i)
                })
                .and_then(|d| d.get("path"))
                .and_then(Value::as_str)
                .unwrap_or("");
            match safe_document_file(doc_path) {
                Ok(p) => Some(inline_document_response(ctx, &p)),
                Err(_) => Some(api_error_response(
                    &ApiError::platform_document_not_found("Platform document not found"),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/bigbox/startup-video" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let raw = state
                .get("settings")
                .and_then(|s| s.get("bigbox_startup_video"))
                .and_then(Value::as_str)
                .map(str::trim)
                .unwrap_or("");
            let p = expand_user_path(raw);
            if !is_video_file(&p) {
                return Some(api_error_response(
                    &ApiError::platform_document_not_found("Startup video not found"),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            Some(send_file(
                ctx.headers,
                &p,
                None,
                None,
                false,
                ctx.request_id,
                ctx.instance,
            ))
        }
        "/api/v2/platform/videos" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let videos = state
                .get("settings")
                .and_then(|s| s.get("platform_videos"))
                .filter(|v| v.is_object())
                .cloned()
                .unwrap_or_else(|| json!({}));
            Some(json_response(
                StatusCode::OK,
                &json!({"videos": videos}),
                ctx.instance,
            ))
        }
        "/api/v2/platform/video" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let platform = ctx.query_first("platform").unwrap_or_default();
            let platform = platform.trim();
            let raw = state
                .get("settings")
                .and_then(|s| s.get("platform_videos"))
                .and_then(|v| v.get(platform))
                .and_then(Value::as_str)
                .map(str::trim)
                .unwrap_or("");
            if raw.is_empty() {
                return Some(api_error_response(
                    &ApiError::platform_document_not_found("Platform video not found"),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let p = expand_user_path(raw);
            if !is_video_file(&p) {
                return Some(api_error_response(
                    &ApiError::platform_document_not_found("Platform video not found"),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            Some(send_file(
                ctx.headers,
                &p,
                None,
                None,
                false,
                ctx.request_id,
                ctx.instance,
            ))
        }
        "/api/v2/netplay/lobby" => {
            let res = match tokio::runtime::Handle::try_current() {
                Ok(h) => tokio::task::block_in_place(|| h.block_on(netplay_lobby())),
                Err(_) => {
                    json!({"available": false, "error": "RetroArch netplay lobby is unreachable."})
                }
            };
            Some(json_response(StatusCode::OK, &res, ctx.instance))
        }
        "/api/v2/highscores/table" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let Some((_, game)) = game_from_query(&state, ctx) else {
                return Some(api_error_response(
                    &ApiError::game_not_found("Game not found"),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            Some(json_response(
                StatusCode::OK,
                &table_for_game(&game, None),
                ctx.instance,
            ))
        }
        "/api/v2/gameyfin/install/status" => {
            let raw_id = ctx
                .query_first("gameyfin_id")
                .unwrap_or_default()
                .trim()
                .to_owned();
            if raw_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("gameyfin_id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let gid = match validate_gameyfin_id(&raw_id) {
                Ok(g) => g,
                Err(e) => {
                    return Some(api_error_response(
                        &ApiError::bad_request(e),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let job = GAMEYFIN_INSTALLS
                .lock()
                .get(&format!("gameyfin:{gid}"))
                .cloned()
                .unwrap_or_else(|| json!({"state": "idle"}));
            Some(json_response(StatusCode::OK, &job, ctx.instance))
        }
        "/api/v2/gameyfin/providers" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let settings = state
                .get("settings")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            match catalog_gameyfin(&settings) {
                Ok((_, providers)) => Some(json_response(
                    StatusCode::OK,
                    &json!({"providers": providers}),
                    ctx.instance,
                )),
                Err(e) => Some(api_error_response(
                    &ApiError::bad_request(e),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        _ => None,
    }
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/saves/backup" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            if !save_management_enabled(&state) {
                return Some(api_error_response(
                    &ApiError::bad_request("Save management is disabled."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let Some((_, game)) = game_from_payload(&state, payload) else {
                return Some(api_error_response(
                    &ApiError::game_not_found("Game not found"),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let game_id = game
                .get("game_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            let app = Arc::clone(&ctx.app);
            let name = format!(
                "saves-backup:{}",
                if game_id.is_empty() { "game" } else { &game_id }
            );
            let job = match ctx.app.jobs.submit(&name, false, None, None, move |_jctx| {
                let root = app.data_dir.join("save-backups");
                let archive = backup_saves(&game, &root, "manual", false, None, None, None)
                    .map_err(|e| e.to_string())?
                    .ok_or_else(|| "No configured save paths currently exist.".to_owned())?;
                let limit = app
                    .load_state_view()
                    .ok()
                    .and_then(|s| s.get("settings")?.get("save_backup_limit")?.as_u64())
                    .unwrap_or(10) as usize;
                let removed = enforce_backup_limit(&game, &root, limit);
                Ok(json!({
                    "backup": archive.file_name().and_then(|s| s.to_str()).unwrap_or(""),
                    "trimmed": removed,
                    "game_id": game_id,
                }))
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
        "/api/v2/saves/restore/preview" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            if !save_management_enabled(&state) {
                return Some(api_error_response(
                    &ApiError::bad_request("Save management is disabled."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let Some((_, game)) = game_from_payload(&state, payload) else {
                return Some(api_error_response(
                    &ApiError::game_not_found("Game not found"),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let backup_name = str_field(payload, "backup");
            if backup_name.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("A backup name is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let selected = match payload.get("selected") {
                None | Some(Value::Null) => None,
                Some(Value::Array(arr)) => Some(
                    arr.iter()
                        .filter_map(Value::as_str)
                        .map(str::to_owned)
                        .collect::<Vec<_>>(),
                ),
                _ => {
                    return Some(api_error_response(
                        &ApiError::bad_request("selected must be a list of archive paths."),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            match preview_restore(
                &game,
                &ctx.app.data_dir.join("save-backups"),
                backup_name,
                selected.as_deref(),
            ) {
                Ok(prev) => Some(json_response(StatusCode::OK, &prev, ctx.instance)),
                Err(err) => Some(api_error_response(
                    &save_err_to_api(err),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/saves/restore" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            if !save_management_enabled(&state) {
                return Some(api_error_response(
                    &ApiError::bad_request("Save management is disabled."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let Some((_, game)) = game_from_payload(&state, payload) else {
                return Some(api_error_response(
                    &ApiError::game_not_found("Game not found"),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let game_id = game
                .get("game_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            let backup_name = str_field(payload, "backup").to_owned();
            if backup_name.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("A backup name is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let dry_run = bool_field(payload, "dry_run");
            let selected = match payload.get("selected") {
                None | Some(Value::Null) => None,
                Some(Value::Array(arr)) => Some(
                    arr.iter()
                        .filter_map(Value::as_str)
                        .map(str::to_owned)
                        .collect::<Vec<_>>(),
                ),
                _ => {
                    return Some(api_error_response(
                        &ApiError::bad_request("selected must be a list of archive paths."),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let root = ctx.app.data_dir.join("save-backups");
            if !dry_run {
                let preview = match preview_restore(&game, &root, &backup_name, selected.as_deref())
                {
                    Ok(p) => p,
                    Err(err) => {
                        return Some(api_error_response(
                            &save_err_to_api(err),
                            ctx.request_id,
                            ctx.instance,
                        ));
                    }
                };
                if preview
                    .get("conflict")
                    .and_then(Value::as_bool)
                    .unwrap_or(false)
                    && !bool_field(payload, "confirm_conflict")
                {
                    return Some(json_response(
                        StatusCode::CONFLICT,
                        &json!({
                            "error": "Restoring this backup would overwrite local save files.",
                            "preview": preview,
                        }),
                        ctx.instance,
                    ));
                }
            }
            let name = format!(
                "saves-restore:{}",
                if game_id.is_empty() { "game" } else { &game_id }
            );
            let job = match ctx.app.jobs.submit(&name, false, None, None, move |_jctx| {
                let res = restore_saves(&game, &root, &backup_name, selected.as_deref(), dry_run)
                    .map_err(|e| e.to_string())?;
                if dry_run {
                    Ok(res)
                } else {
                    Ok(json!({
                        "restored": res.get("archive").and_then(Value::as_str).unwrap_or(""),
                        "game_id": game_id,
                    }))
                }
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
        "/api/v2/saves/add" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            if !save_management_enabled(&state) {
                return Some(api_error_response(
                    &ApiError::bad_request("Save management is disabled."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let path = expand_user_path(str_field(payload, "path"));
            if !path.exists() {
                return Some(api_error_response(
                    &ApiError::not_found("Save path does not exist."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let path_str = path.to_string_lossy().into_owned();
            let payload_cloned = payload.clone();
            let path_for_mut = path_str.clone();
            let res = ctx.app.transact_state(|st| {
                let Ok(idx) = find_game_index_in_state(st, &payload_cloned) else {
                    return Ok(false);
                };
                if let Some(g) = st
                    .get_mut("games")
                    .and_then(Value::as_array_mut)
                    .and_then(|a| a.get_mut(idx))
                    .and_then(Value::as_object_mut)
                {
                    let sp = g
                        .entry("save_paths".to_owned())
                        .or_insert_with(|| Value::Array(Vec::new()));
                    if let Some(arr) = sp.as_array_mut()
                        && !arr
                            .iter()
                            .any(|x| x.as_str() == Some(path_for_mut.as_str()))
                    {
                        arr.push(Value::String(path_for_mut));
                    }
                }
                Ok(true)
            });
            match res {
                Ok((_, true)) => Some(json_response(
                    StatusCode::OK,
                    &json!({"path": path_str}),
                    ctx.instance,
                )),
                Ok((_, false)) => Some(api_error_response(
                    &ApiError::game_not_found("Game not found"),
                    ctx.request_id,
                    ctx.instance,
                )),
                Err(e) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/saves/scan/apply" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            if !save_management_enabled(&state) {
                return Some(api_error_response(
                    &ApiError::bad_request("Save management is disabled."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let app = Arc::clone(&ctx.app);
            let job = match ctx
                .app
                .jobs
                .submit("saves-scan", false, None, None, move |_jctx| {
                    let st = app.load_state().map_err(|e| e.to_string())?;
                    let mut games = st
                        .get("games")
                        .and_then(Value::as_array)
                        .cloned()
                        .unwrap_or_default();
                    let found = scan_all_saves(&mut games, None);
                    let mut found_by_id: Vec<(String, Vec<String>, Map<String, Value>)> =
                        Vec::new();
                    for (idx, paths) in &found {
                        if let Some(g) = games.get(*idx) {
                            let gid = g
                                .get("game_id")
                                .and_then(Value::as_str)
                                .unwrap_or("")
                                .to_owned();
                            let srcs = g
                                .get("save_sources")
                                .and_then(Value::as_object)
                                .cloned()
                                .unwrap_or_default();
                            if !gid.is_empty() {
                                found_by_id.push((gid, paths.clone(), srcs));
                            }
                        }
                    }
                    let games_found = found.len();
                    let (_, updated) = app
                        .transact_state(|cur| {
                            let mut upd = 0usize;
                            if let Some(arr) = cur.get_mut("games").and_then(Value::as_array_mut) {
                                for (gid, paths, srcs) in &found_by_id {
                                    if let Some(g) = arr
                                        .iter_mut()
                                        .find(|x| {
                                            x.get("game_id").and_then(Value::as_str)
                                                == Some(gid.as_str())
                                        })
                                        .and_then(Value::as_object_mut)
                                    {
                                        let sp = g
                                            .entry("save_paths".to_owned())
                                            .or_insert_with(|| Value::Array(Vec::new()));
                                        if let Some(sp_arr) = sp.as_array_mut() {
                                            for p in paths {
                                                if !sp_arr
                                                    .iter()
                                                    .any(|v| v.as_str() == Some(p.as_str()))
                                                {
                                                    sp_arr.push(Value::String(p.clone()));
                                                    upd += 1;
                                                }
                                            }
                                        }
                                        let ss = g
                                            .entry("save_sources".to_owned())
                                            .or_insert_with(|| Value::Object(Map::new()));
                                        if let Some(sm) = ss.as_object_mut() {
                                            for (k, v) in srcs {
                                                sm.insert(k.clone(), v.clone());
                                            }
                                        }
                                    }
                                }
                            }
                            Ok(upd)
                        })
                        .map_err(|e| e.to_string())?;
                    Ok(json!({"updated": updated, "games": games_found}))
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
        "/api/v2/save-tools/ludusavi" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let mut game_name = str_field(payload, "name").to_owned();
            if payload.contains_key("id")
                && game_name.is_empty()
                && let Some((_, g)) = game_from_payload(&state, payload)
            {
                game_name = g
                    .get("name")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned();
            }
            let action = {
                let a = str_field(payload, "action");
                if a.is_empty() { "backup" } else { a }
            };
            let path = {
                let p = str_field(payload, "path");
                if !p.is_empty() {
                    p.to_owned()
                } else {
                    state
                        .get("settings")
                        .and_then(|s| s.get("ludusavi_backup_path"))
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned()
                }
            };
            match run_ludusavi(action, &game_name, &path, true) {
                Ok(res) => Some(json_response(StatusCode::OK, &res, ctx.instance)),
                Err((404, msg)) => Some(api_error_response(
                    &ApiError::not_found(msg),
                    ctx.request_id,
                    ctx.instance,
                )),
                Err((_, msg)) => Some(api_error_response(
                    &ApiError::bad_request(msg),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/save-tools/hoard" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let mut game_name = str_field(payload, "name").to_owned();
            if payload.contains_key("id")
                && game_name.is_empty()
                && let Some((_, g)) = game_from_payload(&state, payload)
            {
                game_name = g
                    .get("name")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned();
            }
            let action = {
                let a = str_field(payload, "action");
                if a.is_empty() { "backup" } else { a }
            };
            match run_hoard(action, &game_name) {
                Ok(res) => Some(json_response(StatusCode::OK, &res, ctx.instance)),
                Err((404, msg)) => Some(api_error_response(
                    &ApiError::not_found(msg),
                    ctx.request_id,
                    ctx.instance,
                )),
                Err((_, msg)) => Some(api_error_response(
                    &ApiError::bad_request(msg),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/platform/documents" => {
            let platform = str_field(payload, "platform").to_owned();
            if platform.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("Platform is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let docs = match clean_document_extras(payload.get("documents")) {
                Ok(d) => d,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            if bool_field(payload, "reorder") {
                let state = match ctx.app.load_state_view() {
                    Ok(s) => s,
                    Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
                };
                let mut cur_paths: Vec<String> = state
                    .get("settings")
                    .and_then(|s| s.get("platform_documents"))
                    .and_then(|pd| pd.get(&platform))
                    .and_then(Value::as_array)
                    .map(|a| {
                        a.iter()
                            .filter_map(|x| {
                                x.get("path").and_then(Value::as_str).map(str::to_owned)
                            })
                            .collect()
                    })
                    .unwrap_or_default();
                let mut new_paths: Vec<String> = docs
                    .iter()
                    .filter_map(|x| x.get("path").and_then(Value::as_str).map(str::to_owned))
                    .collect();
                cur_paths.sort();
                new_paths.sort();
                if cur_paths != new_paths {
                    return Some(api_error_response(
                        &ApiError::bad_request(
                            "Reordering cannot add or remove platform documents.",
                        ),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            }
            let count = docs.len();
            let plat_c = platform.clone();
            if let Err(e) = ctx.app.transact_state(|st| {
                if let Some(root) = st.as_object_mut() {
                    let set = root
                        .entry("settings".to_owned())
                        .or_insert_with(|| Value::Object(Map::new()));
                    if let Some(sm) = set.as_object_mut() {
                        let pd = sm
                            .entry("platform_documents".to_owned())
                            .or_insert_with(|| Value::Object(Map::new()));
                        if let Some(pm) = pd.as_object_mut() {
                            pm.insert(plat_c, Value::Array(docs));
                        }
                    }
                }
                Ok(())
            }) {
                return Some(state_error_response(&e, ctx.request_id, ctx.instance));
            }
            Some(json_response(
                StatusCode::OK,
                &json!({"saved": platform, "count": count}),
                ctx.instance,
            ))
        }
        "/api/v2/platform/videos" => {
            let platform = str_field(payload, "platform").to_owned();
            if platform.is_empty() || platform.chars().count() > 80 {
                return Some(api_error_response(
                    &ApiError::bad_request("Platform is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if bool_field(payload, "clear") {
                let plat_c = platform.clone();
                if let Err(e) = ctx.app.transact_state(|st| {
                    if let Some(pv) = st
                        .get_mut("settings")
                        .and_then(|s| s.get_mut("platform_videos"))
                        .and_then(Value::as_object_mut)
                    {
                        pv.remove(&plat_c);
                    }
                    Ok(())
                }) {
                    return Some(state_error_response(&e, ctx.request_id, ctx.instance));
                }
                return Some(json_response(
                    StatusCode::OK,
                    &json!({"platform": platform, "path": ""}),
                    ctx.instance,
                ));
            }
            let p = expand_user_path(str_field(payload, "path"));
            if !p.is_absolute() || !is_video_file(&p) {
                return Some(api_error_response(
                    &ApiError::bad_request("Platform video must be an existing video file."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let p_str = p.to_string_lossy().into_owned();
            let plat_c = platform.clone();
            let ps_c = p_str.clone();
            if let Err(e) = ctx.app.transact_state(|st| {
                if let Some(root) = st.as_object_mut() {
                    let set = root
                        .entry("settings".to_owned())
                        .or_insert_with(|| Value::Object(Map::new()));
                    if let Some(sm) = set.as_object_mut() {
                        let pv = sm
                            .entry("platform_videos".to_owned())
                            .or_insert_with(|| Value::Object(Map::new()));
                        if let Some(pm) = pv.as_object_mut() {
                            pm.insert(plat_c, Value::String(ps_c));
                        }
                    }
                }
                Ok(())
            }) {
                return Some(state_error_response(&e, ctx.request_id, ctx.instance));
            }
            Some(json_response(
                StatusCode::OK,
                &json!({"platform": platform, "path": p_str}),
                ctx.instance,
            ))
        }
        "/api/v2/steam/action" => {
            let app_id = str_field(payload, "app_id");
            let parsed = app_id.parse::<u64>().ok();
            if app_id.is_empty()
                || !app_id.chars().all(|c| c.is_ascii_digit())
                || parsed.is_none_or(|n| n == 0 || n >= 100_000_000)
            {
                return Some(api_error_response(
                    &ApiError::bad_request("A numeric Steam app id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let action = {
                let a = str_field(payload, "action");
                if a.is_empty() { "install" } else { a }
            };
            if action != "install" && action != "repair" {
                return Some(api_error_response(
                    &ApiError::bad_request("Steam action must be install or repair."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let verb = if action == "repair" {
                "validate"
            } else {
                "install"
            };
            if let Err(err) = open_path(&format!("steam://{verb}/{app_id}")) {
                return Some(api_error_response(
                    &ApiError::bad_request(format!("Steam did not open: {err}")),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            Some(json_response(
                StatusCode::OK,
                &json!({"app_id": app_id, "action": action}),
                ctx.instance,
            ))
        }
        "/api/v2/gameyfin/test" => {
            let state = match ctx.app.load_state() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let mut settings = state
                .get("settings")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            for (k, v) in payload {
                if k == "gameyfin_password" && v.as_str().is_none_or(|s| s.trim().is_empty()) {
                    continue;
                }
                settings.insert(k.clone(), v.clone());
            }
            match test_gameyfin_connection(&settings) {
                Ok(res) => Some(json_response(StatusCode::OK, &res, ctx.instance)),
                Err(e) => Some(api_error_response(
                    &ApiError::bad_request(e),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/gameyfin/install" => {
            let raw_id = {
                let a = str_field(payload, "gameyfin_id").trim();
                if !a.is_empty() {
                    a.to_owned()
                } else {
                    match payload.get("id") {
                        Some(Value::String(s)) => s.trim().to_owned(),
                        Some(Value::Number(n)) => n.to_string(),
                        _ => String::new(),
                    }
                }
            };
            if raw_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("gameyfin_id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let gid = match validate_gameyfin_id(&raw_id) {
                Ok(g) => g,
                Err(e) => {
                    return Some(api_error_response(
                        &ApiError::bad_request(e),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let library_id = payload.get("library_id").cloned();
            let job_key = format!("gameyfin:{gid}");
            {
                let mut map = GAMEYFIN_INSTALLS.lock();
                if map
                    .get(&job_key)
                    .and_then(|j| j.get("state"))
                    .and_then(Value::as_str)
                    == Some("installing")
                {
                    return Some(json_response(
                        StatusCode::OK,
                        &json!({"state": "installing", "gameyfin_id": gid}),
                        ctx.instance,
                    ));
                }
                map.insert(
                    job_key.clone(),
                    json!({"state": "installing", "gameyfin_id": gid}),
                );
            }
            let app = Arc::clone(&ctx.app);
            let gid_c = gid.clone();
            let jkey_c = job_key.clone();
            let job = match ctx.app.jobs.submit(
                &job_key,
                true,
                Some("gameyfin_install"),
                None,
                move |_jctx| {
                    let settings = app
                        .load_state()
                        .ok()
                        .and_then(|s| s.get("settings").and_then(Value::as_object).cloned())
                        .unwrap_or_default();
                    let res = match install_gameyfin_game(&settings, &gid_c) {
                        Ok(installed) => {
                            let inst_c = installed.clone();
                            let gid_m = gid_c.clone();
                            let lib_m = library_id.clone();
                            let _ = app.transact_state(move |st| {
                                if let Some(games) =
                                    st.get_mut("games").and_then(Value::as_array_mut)
                                {
                                    let mut found_idx = games.iter().position(|g| {
                                        g.get("gameyfin_id").and_then(Value::as_str)
                                            == Some(gid_m.as_str())
                                    });
                                    if found_idx.is_none()
                                        && let Some(ref lid) = lib_m
                                    {
                                        if let Some(n) = lid.as_u64().map(|u| u as usize)
                                            && n < games.len()
                                        {
                                            found_idx = Some(n);
                                        } else if let Some(s) = lid.as_str() {
                                            found_idx = games.iter().position(|g| {
                                                g.get("game_id").and_then(Value::as_str) == Some(s)
                                            });
                                        }
                                    }
                                    if let Some(idx) = found_idx
                                        && let Some(target) = games[idx].as_object_mut()
                                    {
                                        for (k, v) in inst_c {
                                            target.insert(k, v);
                                        }
                                    } else {
                                        games.push(Value::Object(inst_c));
                                    }
                                }
                                Ok(())
                            });
                            json!({"state": "done", "gameyfin_id": gid_c, "game": Value::Object(installed)})
                        }
                        Err(err) => {
                            json!({"state": "error", "gameyfin_id": gid_c, "error": err})
                        }
                    };
                    GAMEYFIN_INSTALLS.lock().insert(jkey_c.clone(), res.clone());
                    Ok(res)
                },
            ) {
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
                &json!({"state": "installing", "gameyfin_id": gid, "job_id": job["job_id"]}),
                ctx.instance,
            ))
        }
        "/api/v2/gameyfin/uninstall" => {
            let state = match ctx.app.load_state() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let Some((_, original)) = game_from_payload(&state, payload) else {
                return Some(api_error_response(
                    &ApiError::game_not_found("Game not found."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let mut target = original.as_object().cloned().unwrap_or_default();
            if target
                .get("gameyfin_id")
                .and_then(Value::as_str)
                .is_none_or(str::is_empty)
            {
                return Some(api_error_response(
                    &ApiError::bad_request("This game is not a Gameyfin entry."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let settings = state
                .get("settings")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            let conf = gameyfin_settings(&settings);
            let install_root = default_install_root(&conf);
            let result = match uninstall_gameyfin_game(&mut target, &install_root) {
                Ok(r) => r,
                Err(e) => {
                    return Some(api_error_response(
                        &ApiError::bad_request(e),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let gid = target
                .get("game_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            let target_c = target.clone();
            if let Err(e) = ctx.app.transact_state(move |st| {
                let mut lookup = Map::new();
                lookup.insert("game_id".into(), Value::String(gid));
                if let Ok(idx) = find_game_index_in_state(st, &lookup)
                    && let Some(g) = st
                        .get_mut("games")
                        .and_then(Value::as_array_mut)
                        .and_then(|a| a.get_mut(idx))
                        .and_then(Value::as_object_mut)
                {
                    for (k, v) in target_c {
                        g.insert(k, v);
                    }
                }
                Ok(())
            }) {
                return Some(state_error_response(&e, ctx.request_id, ctx.instance));
            }
            Some(json_response(StatusCode::OK, &result, ctx.instance))
        }
        "/api/v2/highscores/export" => {
            let state = match ctx.app.load_state() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let Some((_, game)) = game_from_payload(&state, payload) else {
                return Some(api_error_response(
                    &ApiError::game_not_found("Game not found."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let name = game
                .get("name")
                .and_then(Value::as_str)
                .unwrap_or("game")
                .to_lowercase();
            let slug = HS_SLUG_RE.replace_all(&name, "-");
            let slug = slug.trim_matches('-');
            let folder_name = if slug.is_empty() { "game" } else { slug };
            let export_dir = ctx.app.data_dir.join("highscores").join(folder_name);
            match export_highscores(&game, &export_dir, None) {
                Ok(res) => Some(json_response(StatusCode::OK, &res, ctx.instance)),
                Err(e) => Some(api_error_response(
                    &ApiError::bad_request(e),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/highscores/import" => {
            let import_dir = str_field(payload, "path").trim();
            let state = match ctx.app.load_state() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let Some((_, game)) = game_from_payload(&state, payload) else {
                return Some(api_error_response(
                    &ApiError::game_not_found("Game not found."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            match import_highscores(&game, Path::new(import_dir), None) {
                Ok(restored) => Some(json_response(
                    StatusCode::OK,
                    &json!({"restored": restored}),
                    ctx.instance,
                )),
                Err(e) => {
                    let err = if e.contains("No high score files") {
                        ApiError::not_found(e)
                    } else {
                        ApiError::bad_request(e)
                    };
                    Some(api_error_response(&err, ctx.request_id, ctx.instance))
                }
            }
        }
        _ => None,
    }
}
