//! Steam Bridge v2 routes for safe `shortcuts.vdf` preview/apply/remove (`handlers/steambridge.py`).

use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

use axum::http::StatusCode;
use axum::response::Response;
use openbox_core::ApiError;
use openbox_integrations::steam_bridge::{
    SteamBridgeError, candidate_shortcuts_paths, commit_plan, copy_grid_art, is_bridge_shortcut,
    parse_shortcuts_file, preview_remove, preview_shortcuts, record_appid, shortcut_from_game,
    steam_grid_dirs, steam_roots,
};
use openbox_platform::{expand_user, which};
use serde_json::{Map, Value, json};

use crate::handlers::shared::{RequestCtx, str_field};
use crate::middleware::{api_error_response, json_response, state_error_response};

const MAX_BRIDGE_GAMES: usize = 500;
const DEFAULT_LAUNCHER: &str = "openbox";

fn launcher_executable() -> String {
    if let Ok(cfg) = std::env::var("OPENBOX_EXECUTABLE")
        && !cfg.trim().is_empty()
    {
        return cfg.trim().to_owned();
    }
    which("openbox")
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_else(|| DEFAULT_LAUNCHER.to_owned())
}

fn resolve_shortcuts_path(supplied: &str) -> Result<PathBuf, ApiError> {
    let trimmed = supplied.trim();
    if !trimmed.is_empty() {
        let p = expand_user(Path::new(trimmed));
        if !p.is_absolute() {
            return Err(ApiError::new(
                400,
                "STEAMBRIDGE_PATH_INVALID",
                "Steam Bridge path must be absolute.",
            ));
        }
        return Ok(p);
    }
    let existing: Vec<PathBuf> = candidate_shortcuts_paths()
        .into_iter()
        .filter(|p| p.is_file())
        .collect();
    if let Some(best) = existing.into_iter().max_by_key(|p| {
        fs::metadata(p)
            .ok()
            .and_then(|m| m.modified().ok())
            .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    }) {
        return Ok(best);
    }
    Ok(steam_roots()[0].join("userdata/0/config/shortcuts.vdf"))
}

fn select_games(
    state: &Value,
    payload: &Map<String, Value>,
) -> Result<Vec<Map<String, Value>>, ApiError> {
    let source = state
        .get("games")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let Some(req) = payload.get("game_ids") else {
        return Ok(source
            .into_iter()
            .filter_map(|g| g.as_object().cloned())
            .take(MAX_BRIDGE_GAMES)
            .collect());
    };
    if req.is_null() {
        return Ok(source
            .into_iter()
            .filter_map(|g| g.as_object().cloned())
            .take(MAX_BRIDGE_GAMES)
            .collect());
    }
    let Some(arr) = req.as_array() else {
        return Err(ApiError::new(
            400,
            "STEAMBRIDGE_INVALID_REQUEST",
            format!("game_ids must be a list of at most {MAX_BRIDGE_GAMES} items."),
        ));
    };
    if arr.len() > MAX_BRIDGE_GAMES {
        return Err(ApiError::new(
            400,
            "STEAMBRIDGE_INVALID_REQUEST",
            format!("game_ids must be a list of at most {MAX_BRIDGE_GAMES} items."),
        ));
    }
    let wanted: HashSet<String> = arr
        .iter()
        .filter_map(|v| match v {
            Value::String(s) if !s.trim().is_empty() => Some(s.trim().to_owned()),
            Value::Number(n) => Some(n.to_string()),
            _ => None,
        })
        .collect();
    Ok(source
        .into_iter()
        .filter_map(|g| g.as_object().cloned())
        .filter(|g| {
            let gid = g
                .get("game_id")
                .or_else(|| g.get("id"))
                .map(|v| match v {
                    Value::String(s) => s.clone(),
                    other => other.to_string(),
                })
                .unwrap_or_default();
            wanted.contains(&gid)
        })
        .collect())
}

fn map_bridge_error(err: SteamBridgeError) -> ApiError {
    if err.is_stale() {
        ApiError::new(409, "STEAMBRIDGE_PREVIEW_STALE", err.to_string())
    } else {
        ApiError::new(400, "STEAMBRIDGE_INVALID_PLAN", err.to_string())
    }
}

fn apply_grid_art(path: &Path, state: &Value, payload: &Map<String, Value>) -> Value {
    let Ok(games) = select_games(state, payload) else {
        return json!({"applied": 0, "skipped": 0});
    };
    if games.is_empty() {
        return json!({"applied": 0, "skipped": 0});
    }
    let Some(grid_dir) = path.parent().map(|p| p.join("grid")) else {
        return json!({"applied": 0, "skipped": games.len()});
    };
    if !steam_grid_dirs().contains(&grid_dir) {
        return json!({"applied": 0, "skipped": games.len()});
    }
    if fs::create_dir_all(&grid_dir).is_err() {
        return json!({"applied": 0, "skipped": games.len()});
    }
    let launcher = {
        let s = str_field(payload, "launcher_exe").trim();
        if s.is_empty() {
            launcher_executable()
        } else {
            s.to_owned()
        }
    };
    let mut applied = 0usize;
    let mut skipped = 0usize;
    for game in &games {
        let Ok(rec) = shortcut_from_game(game, Some(&launcher)) else {
            skipped += 1;
            continue;
        };
        let Some(appid) = record_appid(&rec) else {
            skipped += 1;
            continue;
        };
        if copy_grid_art(&grid_dir, appid, game) > 0 {
            applied += 1;
        } else {
            skipped += 1;
        }
    }
    json!({"applied": applied, "skipped": skipped})
}

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    if ctx.path != "/api/v2/steambridge/status" {
        return None;
    }
    let path = match resolve_shortcuts_path("") {
        Ok(p) => p,
        Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
    };
    let exists = path.is_file();
    let available = path.parent().is_some_and(Path::is_dir);
    let mut result = json!({
        "path": path.to_string_lossy(),
        "exists": exists,
        "available": available,
    });
    if exists {
        match parse_shortcuts_file(&path) {
            Ok(doc) => {
                let openbox_count = doc.records.iter().filter(|r| is_bridge_shortcut(r)).count();
                let foreign_count = doc.records.len().saturating_sub(openbox_count);
                if let Some(obj) = result.as_object_mut() {
                    obj.insert("count".into(), json!(doc.records.len()));
                    obj.insert("openbox_count".into(), json!(openbox_count));
                    obj.insert("foreign_count".into(), json!(foreign_count));
                }
            }
            Err(err) => {
                if let Some(obj) = result.as_object_mut() {
                    obj.insert("error".into(), json!(err.to_string()));
                    obj.insert("corrupt".into(), json!(true));
                }
            }
        }
    }
    Some(json_response(StatusCode::OK, &result, ctx.instance))
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/steambridge/preview" => {
            let path = match resolve_shortcuts_path(str_field(payload, "path")) {
                Ok(p) => p,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let games = match select_games(&state, payload) {
                Ok(g) => g,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let launcher = {
                let s = str_field(payload, "launcher_exe").trim();
                if s.is_empty() {
                    launcher_executable()
                } else {
                    s.to_owned()
                }
            };
            if launcher.is_empty() {
                return Some(api_error_response(
                    &ApiError::new(
                        400,
                        "STEAMBRIDGE_INVALID_REQUEST",
                        "Steam Bridge launcher executable is empty.",
                    ),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let mut shortcuts = Vec::with_capacity(games.len());
            for g in &games {
                match shortcut_from_game(g, Some(&launcher)) {
                    Ok(rec) => shortcuts.push(rec),
                    Err(err) => {
                        return Some(api_error_response(
                            &map_bridge_error(err),
                            ctx.request_id,
                            ctx.instance,
                        ));
                    }
                }
            }
            match preview_shortcuts(&path, &shortcuts) {
                Ok(plan) => Some(json_response(StatusCode::OK, &plan, ctx.instance)),
                Err(err) => Some(api_error_response(
                    &map_bridge_error(err),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/steambridge/apply" => {
            let Some(plan) = payload.get("plan").filter(|v| v.is_object()) else {
                return Some(api_error_response(
                    &ApiError::new(
                        400,
                        "STEAMBRIDGE_PLAN_REQUIRED",
                        "A reviewed Steam Bridge plan is required.",
                    ),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let supplied_path = {
                let p = str_field(payload, "path");
                if !p.is_empty() {
                    p
                } else {
                    plan.get("path").and_then(Value::as_str).unwrap_or("")
                }
            };
            let path = match resolve_shortcuts_path(supplied_path) {
                Ok(p) => p,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let mut result = match commit_plan(&path, plan, false) {
                Ok(r) => r,
                Err(err) => {
                    return Some(api_error_response(
                        &map_bridge_error(err),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let grid_art = match ctx.app.load_state_view() {
                Ok(st) => apply_grid_art(&path, &st, payload),
                Err(_) => json!({"applied": 0, "skipped": 0}),
            };
            if let Some(obj) = result.as_object_mut() {
                obj.insert("grid_art".into(), grid_art);
            }
            Some(json_response(StatusCode::OK, &result, ctx.instance))
        }
        "/api/v2/steambridge/remove/preview" => {
            let targets: Vec<Value> = match payload.get("targets") {
                None | Some(Value::Null) => Vec::new(),
                Some(Value::String(s)) if s.is_empty() => Vec::new(),
                Some(Value::Array(arr)) => arr.clone(),
                Some(other) => vec![other.clone()],
            };
            if targets.is_empty() {
                return Some(api_error_response(
                    &ApiError::new(
                        400,
                        "STEAMBRIDGE_TARGET_REQUIRED",
                        "At least one Steam shortcut target is required.",
                    ),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let path = match resolve_shortcuts_path(str_field(payload, "path")) {
                Ok(p) => p,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            match preview_remove(&path, &targets) {
                Ok(plan) => Some(json_response(StatusCode::OK, &plan, ctx.instance)),
                Err(err) => Some(api_error_response(
                    &map_bridge_error(err),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/steambridge/remove" => {
            let Some(plan) = payload.get("plan").filter(|v| {
                v.is_object() && v.get("operation").and_then(Value::as_str) == Some("remove")
            }) else {
                return Some(api_error_response(
                    &ApiError::new(
                        400,
                        "STEAMBRIDGE_PLAN_REQUIRED",
                        "A reviewed Steam Bridge removal plan is required.",
                    ),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let supplied_path = {
                let p = str_field(payload, "path");
                if !p.is_empty() {
                    p
                } else {
                    plan.get("path").and_then(Value::as_str).unwrap_or("")
                }
            };
            let path = match resolve_shortcuts_path(supplied_path) {
                Ok(p) => p,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            match commit_plan(&path, plan, false) {
                Ok(res) => Some(json_response(StatusCode::OK, &res, ctx.instance)),
                Err(err) => Some(api_error_response(
                    &map_bridge_error(err),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        _ => None,
    }
}
