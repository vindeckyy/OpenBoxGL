//! Moments timeline routes (`handlers/moments.py`).

use std::fs;
use std::path::{Path, PathBuf};

use axum::http::StatusCode;
use axum::response::Response;
use jiff::Timestamp;
use openbox_core::ApiError;
use openbox_emulators::{load_adapters, start_game_with_extra_args};
use openbox_integrations::resume::{
    adapter_for_launch, emulator_fingerprint, inject_resume_args, resume_status, state_dir_for,
};
use openbox_metadata::media::capture_screenshot;
use openbox_runtime::AppState;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use uuid::Uuid;

use crate::handlers::projection::find_game_index_in_state;
use crate::handlers::shared::{RequestCtx, str_field};
use crate::middleware::{api_error_response, json_response, state_error_response};

const MAX_MOMENTS_PER_GAME: usize = 500;
const MAX_NOTE_LENGTH: usize = 2000;
const MAX_TITLE_LENGTH: usize = 120;

const AUTO_TRIGGERS: &[&str] = &["first_boot", "ra_unlock", "progress", "milestone"];
const ALL_TRIGGERS: &[&str] = &[
    "first_boot",
    "ra_unlock",
    "progress",
    "milestone",
    "manual",
    "pause",
    "hotkey",
    "recap",
    "session_end",
];

fn trigger_title(trigger: &str) -> &'static str {
    match trigger {
        "pause" => "Paused moment",
        "hotkey" => "Quick moment",
        "recap" => "Session recap",
        "session_end" => "Session moment",
        "first_boot" => "First steps",
        "ra_unlock" => "Achievement unlocked",
        "progress" => "Progress update",
        "milestone" => "Milestone",
        _ => "Moment",
    }
}

fn utc_now() -> String {
    Timestamp::now()
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
        .to_string()
}

fn clean_note(val: Option<&Value>) -> Result<String, ApiError> {
    let Some(v) = val else {
        return Ok(String::new());
    };
    if v.is_null() {
        return Ok(String::new());
    }
    let Some(s) = v.as_str() else {
        return Err(ApiError::new(
            400,
            "MOMENT_NOTE_INVALID",
            "note must be text.",
        ));
    };
    let trimmed = s.trim();
    if trimmed.chars().count() > MAX_NOTE_LENGTH {
        return Err(ApiError::new(
            400,
            "MOMENT_NOTE_TOO_LONG",
            format!("note must be at most {MAX_NOTE_LENGTH} characters."),
        ));
    }
    Ok(trimmed.to_owned())
}

fn clean_title(val: Option<&Value>, trigger: &str) -> Result<String, ApiError> {
    let Some(v) = val else {
        return Ok(trigger_title(trigger).to_owned());
    };
    if v.is_null() {
        return Ok(trigger_title(trigger).to_owned());
    }
    let Some(s) = v.as_str() else {
        return Err(ApiError::new(
            400,
            "MOMENT_TITLE_INVALID",
            "title must be text.",
        ));
    };
    let trimmed = s.trim();
    if trimmed.chars().count() > MAX_TITLE_LENGTH {
        return Err(ApiError::new(
            400,
            "MOMENT_TITLE_TOO_LONG",
            format!("title must be at most {MAX_TITLE_LENGTH} characters."),
        ));
    }
    if trimmed.is_empty() {
        Ok(trigger_title(trigger).to_owned())
    } else {
        Ok(trimmed.to_owned())
    }
}

fn clean_trigger(val: Option<&Value>) -> Result<String, ApiError> {
    let raw = val
        .and_then(Value::as_str)
        .filter(|s| !s.trim().is_empty())
        .unwrap_or("manual")
        .trim()
        .to_lowercase()
        .replace('-', "_");
    if !ALL_TRIGGERS.contains(&raw.as_str()) {
        return Err(ApiError::new(
            400,
            "MOMENT_TRIGGER_INVALID",
            "Unknown moment trigger.",
        ));
    }
    Ok(raw)
}

fn timestamp_key(val: Option<&Value>) -> (i64, String) {
    let text = val.and_then(Value::as_str).unwrap_or("").to_owned();
    let ts = text
        .parse::<Timestamp>()
        .map(|t| t.as_second())
        .unwrap_or(0);
    (ts, text)
}

fn screenshot_index(game: &Value, path: &str) -> Value {
    if path.is_empty() {
        return Value::Null;
    }
    if let Some(arr) = game.get("screenshots").and_then(Value::as_array)
        && let Some(pos) = arr.iter().position(|v| v.as_str() == Some(path))
    {
        return json!(pos);
    }
    Value::Null
}

fn moment_public(app: &AppState, moment: &Value, game: &Value) -> Value {
    let raw_shot = moment
        .get("screenshot")
        .and_then(Value::as_str)
        .unwrap_or("");
    let screenshot = if !raw_shot.is_empty() {
        app.media_probe
            .approved_media_path(raw_shot, true)
            .map(|p| p.to_string_lossy().into_owned())
            .unwrap_or_default()
    } else {
        String::new()
    };
    let mut payload = json!({
        "moment_id": moment.get("moment_id").and_then(Value::as_str).unwrap_or(""),
        "game_id": moment.get("game_id").and_then(Value::as_str).filter(|s| !s.is_empty())
            .or_else(|| game.get("game_id").and_then(Value::as_str)).unwrap_or(""),
        "created_at": moment.get("created_at").and_then(Value::as_str).unwrap_or(""),
        "updated_at": moment.get("updated_at").and_then(Value::as_str).unwrap_or(""),
        "launch_id": moment.get("launch_id").and_then(Value::as_str).unwrap_or(""),
        "title": moment.get("title").and_then(Value::as_str).filter(|s| !s.is_empty()).unwrap_or("Moment"),
        "note": moment.get("note").and_then(Value::as_str).unwrap_or(""),
        "trigger": moment.get("trigger").and_then(Value::as_str).filter(|s| !s.is_empty()).unwrap_or("manual"),
        "screenshot": screenshot,
        "screenshot_index": screenshot_index(game, &screenshot),
        "resume_state": moment.get("resume_state").filter(|v| v.is_object()).cloned().unwrap_or(Value::Null),
    });
    if let Some(err) = moment
        .get("capture_error")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        payload
            .as_object_mut()
            .unwrap()
            .insert("capture_error".into(), Value::String(err.to_owned()));
    }
    payload
}

fn game_from_query(state: &Value, ctx: &RequestCtx<'_>) -> Result<(usize, Value), ApiError> {
    let mut payload = Map::new();
    let gid = ctx
        .query_first("game_id")
        .filter(|s| !s.is_empty())
        .or_else(|| ctx.query_first("game").filter(|s| !s.is_empty()));
    if let Some(g) = gid {
        payload.insert("game_id".into(), Value::String(g));
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

fn find_moment(
    state: &Value,
    payload: &Map<String, Value>,
) -> Result<(usize, Value, Value), ApiError> {
    let moment_id = str_field(payload, "moment_id").trim();
    if moment_id.is_empty() {
        return Err(ApiError::new(
            400,
            "MOMENT_ID_REQUIRED",
            "moment_id is required.",
        ));
    }
    let games = state
        .get("games")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if payload.get("game_id").is_some_and(|v| !v.is_null())
        || payload.get("id").is_some_and(|v| !v.is_null())
    {
        let (idx, game) = game_from_payload(state, payload)?;
        let found = game
            .get("moments")
            .and_then(Value::as_array)
            .and_then(|arr| {
                arr.iter()
                    .find(|item| item.get("moment_id").and_then(Value::as_str) == Some(moment_id))
                    .cloned()
            });
        if let Some(item) = found {
            return Ok((idx, game, item));
        }
        return Err(ApiError::new(404, "MOMENT_NOT_FOUND", "Moment not found."));
    }
    for (idx, game) in games.into_iter().enumerate() {
        let found = game
            .get("moments")
            .and_then(Value::as_array)
            .and_then(|arr| {
                arr.iter()
                    .find(|item| item.get("moment_id").and_then(Value::as_str) == Some(moment_id))
                    .cloned()
            });
        if let Some(item) = found {
            return Ok((idx, game, item));
        }
    }
    Err(ApiError::new(404, "MOMENT_NOT_FOUND", "Moment not found."))
}

fn resume_link_snapshot(
    app: &AppState,
    game: &Value,
    state: &Value,
    requested: Option<&Value>,
    snapshot_id: &str,
) -> Option<Value> {
    if requested == Some(&Value::Bool(false)) {
        return None;
    }
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
    let adapters = load_adapters(&app.root_dir, Some(&app.data_dir));
    let status = resume_status(game, &settings, &profiles, &adapters, &app.data_dir);
    if !status
        .get("enabled")
        .and_then(Value::as_bool)
        .unwrap_or(false)
        || !status
            .get("capable")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        || !status
            .get("available")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        || status
            .get("stale")
            .and_then(Value::as_bool)
            .unwrap_or(false)
    {
        return None;
    }
    let cur = status.get("state")?.as_object()?;
    let cur_file = cur
        .get("file")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())?;
    if let Some(req_obj) = requested.and_then(Value::as_object) {
        if let Some(rf) = req_obj
            .get("file")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            && rf != cur_file
        {
            return None;
        }
        if let Some(rc) = req_obj
            .get("capture_id")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            && rc != cur.get("capture_id").and_then(Value::as_str).unwrap_or("")
        {
            return None;
        }
    }
    let sdir = state_dir_for(game, &app.data_dir).ok()?;
    let source = sdir.join(cur_file);
    let source_meta = fs::symlink_metadata(&source).ok()?;
    if source_meta.file_type().is_symlink() {
        return None;
    }
    let source_resolved = fs::canonicalize(&source).ok()?;
    let state_root = fs::canonicalize(&sdir).ok()?;
    if !source_resolved.is_file() || !source_resolved.starts_with(&state_root) {
        return None;
    }
    let suffix = source_resolved
        .extension()
        .and_then(|e| e.to_str())
        .map(|e| format!(".{e}"))
        .unwrap_or_else(|| ".state".to_owned());
    let snap_dir = sdir.join("moments");
    fs::create_dir_all(&snap_dir).ok()?;
    let snapshot = snap_dir.join(format!("{snapshot_id}{suffix}"));
    fs::copy(&source_resolved, &snapshot).ok()?;
    let bytes = fs::read(&snapshot).ok()?;
    let digest = hex::encode(Sha256::digest(&bytes));
    let rel = snapshot
        .strip_prefix(&sdir)
        .ok()?
        .to_string_lossy()
        .replace('\\', "/");
    Some(json!({
        "file": rel,
        "capture_id": cur.get("capture_id").and_then(Value::as_str).unwrap_or(""),
        "snapshot_id": snapshot_id,
        "immutable": true,
        "sha256": digest,
        "size": bytes.len(),
        "captured_at": cur.get("captured_at").and_then(Value::as_str).unwrap_or(""),
        "adapter_id": cur.get("adapter_id").and_then(Value::as_str).unwrap_or(""),
        "emulator_version": cur.get("emulator_version").and_then(Value::as_str).unwrap_or(""),
    }))
}

fn capture_for_moment(
    app: &AppState,
    game: &Value,
    payload: &Map<String, Value>,
) -> Result<(String, String), ApiError> {
    let supplied = str_field(payload, "screenshot");
    let supplied = if !supplied.is_empty() {
        supplied
    } else {
        str_field(payload, "screenshot_path")
    };
    if !supplied.is_empty() {
        let approved = app
            .media_probe
            .approved_media_path(supplied, true)
            .map_err(|_| {
                ApiError::new(
                    400,
                    "MOMENT_MEDIA_INVALID",
                    "Screenshot is not in an approved media directory.",
                )
            })?;
        return Ok((approved.to_string_lossy().into_owned(), String::new()));
    }
    if payload.get("capture") == Some(&Value::Bool(false)) {
        return Ok((String::new(), String::new()));
    }
    let stable_id = game
        .get("game_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .unwrap_or("game")
        .replace(['/', '\\'], "_");
    let stamp = Timestamp::now().strftime("%Y%m%d-%H%M%S").to_string();
    let suffix = &Uuid::new_v4().simple().to_string()[..8];
    let dest: PathBuf = app
        .data_dir
        .join("media")
        .join("moments")
        .join(stable_id)
        .join(format!("{stamp}-{suffix}.png"));
    if let Ok(approved_dest) = app
        .media_probe
        .approved_media_path(&dest.to_string_lossy(), false)
        && let Ok(captured) = capture_screenshot(&approved_dest, "")
        && let Ok(verified) = app.media_probe.approved_media_path(&captured, true)
    {
        return Ok((verified.to_string_lossy().into_owned(), String::new()));
    }
    Ok((String::new(), "SCREENSHOT_UNAVAILABLE".to_owned()))
}

fn validate_snapshot_path(
    data_dir: &Path,
    game: &Value,
    link: Option<&Value>,
) -> Result<String, ApiError> {
    let Some(obj) = link.and_then(Value::as_object) else {
        return Err(ApiError::new(
            404,
            "MOMENT_RESUME_MISSING",
            "This moment has no immutable resume snapshot.",
        ));
    };
    if obj.get("immutable") != Some(&Value::Bool(true)) {
        return Err(ApiError::new(
            404,
            "MOMENT_RESUME_MISSING",
            "This moment has no immutable resume snapshot.",
        ));
    }
    let rel = obj.get("file").and_then(Value::as_str).unwrap_or("");
    if rel.is_empty() || !rel.replace('\\', "/").starts_with("moments/") {
        return Err(ApiError::new(
            400,
            "MOMENT_RESUME_INVALID",
            "Moment resume reference is invalid.",
        ));
    }
    let sdir = state_dir_for(game, data_dir)
        .map_err(|e| ApiError::new(400, "MOMENT_RESUME_INVALID", e))?;
    let cand = sdir.join(rel);
    if fs::symlink_metadata(&cand)
        .ok()
        .is_some_and(|m| m.file_type().is_symlink())
    {
        return Err(ApiError::new(
            404,
            "MOMENT_RESUME_MISSING",
            "This moment's resume snapshot is missing.",
        ));
    }
    let Ok(resolved) = fs::canonicalize(&cand) else {
        return Err(ApiError::new(
            404,
            "MOMENT_RESUME_MISSING",
            "This moment's resume snapshot is missing.",
        ));
    };
    let Ok(root) = fs::canonicalize(&sdir) else {
        return Err(ApiError::new(
            404,
            "MOMENT_RESUME_MISSING",
            "This moment's resume snapshot is missing.",
        ));
    };
    if !resolved.starts_with(&root) || !resolved.is_file() {
        return Err(ApiError::new(
            404,
            "MOMENT_RESUME_MISSING",
            "This moment's resume snapshot is missing.",
        ));
    }
    let expected = obj.get("sha256").and_then(Value::as_str).unwrap_or("");
    if !expected.is_empty() {
        let bytes = fs::read(&resolved).map_err(|_| {
            ApiError::new(
                404,
                "MOMENT_RESUME_MISSING",
                "This moment's resume snapshot is missing.",
            )
        })?;
        let actual = hex::encode(Sha256::digest(&bytes));
        if actual != expected {
            return Err(ApiError::new(
                400,
                "MOMENT_RESUME_INVALID",
                "Moment resume snapshot failed its integrity check.",
            ));
        }
    }
    Ok(rel.to_owned())
}

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    if ctx.path != "/api/v2/moments" {
        return None;
    }
    let state = match ctx.app.load_state() {
        Ok(s) => s,
        Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
    };
    if let Some(mid) = ctx
        .query_first("moment_id")
        .filter(|s| !s.trim().is_empty())
    {
        let mut p = Map::new();
        p.insert("moment_id".into(), Value::String(mid.trim().to_owned()));
        return match find_moment(&state, &p) {
            Ok((_, game, item)) => Some(json_response(
                StatusCode::OK,
                &json!({
                    "game_id": game.get("game_id").and_then(Value::as_str).unwrap_or(""),
                    "item": moment_public(&ctx.app, &item, &game),
                }),
                ctx.instance,
            )),
            Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
        };
    }
    let (_, game) = match game_from_query(&state, ctx) {
        Ok(pair) => pair,
        Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
    };
    let limit = match ctx.query_first("limit").filter(|s| !s.is_empty()) {
        None => 100usize,
        Some(raw) => match raw.parse::<i64>() {
            Ok(n) if (1..=MAX_MOMENTS_PER_GAME as i64).contains(&n) => n as usize,
            Ok(_) => {
                return Some(api_error_response(
                    &ApiError::new(
                        400,
                        "MOMENT_LIMIT_INVALID",
                        "limit must be between 1 and 500.",
                    ),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            Err(_) => {
                return Some(api_error_response(
                    &ApiError::new(400, "MOMENT_LIMIT_INVALID", "limit must be an integer."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
        },
    };
    let mut indexed: Vec<(usize, Value)> = game
        .get("moments")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .filter(|v| v.is_object())
        .enumerate()
        .collect();
    indexed.sort_by(|(ia, a), (ib, b)| {
        let ka = timestamp_key(a.get("created_at"));
        let kb = timestamp_key(b.get("created_at"));
        (kb.0, kb.1, *ib).cmp(&(ka.0, ka.1, *ia))
    });
    let total = indexed.len();
    let items: Vec<Value> = indexed
        .into_iter()
        .take(limit)
        .map(|(_, item)| moment_public(&ctx.app, &item, &game))
        .collect();
    Some(json_response(
        StatusCode::OK,
        &json!({
            "game_id": game.get("game_id").and_then(Value::as_str).unwrap_or(""),
            "items": items,
            "total": total,
        }),
        ctx.instance,
    ))
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/moments" => {
            let state = match ctx.app.load_state() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let (_, game) = match game_from_payload(&state, payload) {
                Ok(pair) => pair,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let stable_id = game
                .get("game_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            let trigger = match clean_trigger(payload.get("trigger")) {
                Ok(t) => t,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let auto_enabled = state
                .get("settings")
                .and_then(|s| s.get("moments_autocapture"))
                .and_then(Value::as_bool)
                .unwrap_or(false);
            if AUTO_TRIGGERS.contains(&trigger.as_str()) && !auto_enabled {
                return Some(api_error_response(
                    &ApiError::new(
                        400,
                        "MOMENTS_AUTO_DISABLED",
                        "Automatic moments are disabled in Settings.",
                    ),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let note = match clean_note(payload.get("note")) {
                Ok(n) => n,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let title = match clean_title(payload.get("title"), &trigger) {
                Ok(t) => t,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let (screenshot, capture_error) = match capture_for_moment(&ctx.app, &game, payload) {
                Ok(pair) => pair,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let now = {
                let c = str_field(payload, "created_at");
                if !c.is_empty() {
                    c.to_owned()
                } else {
                    utc_now()
                }
            };
            let moment_id = format!("moment-{}", &Uuid::new_v4().simple().to_string()[..24]);
            let resume = resume_link_snapshot(
                &ctx.app,
                &game,
                &state,
                payload.get("resume_state"),
                &moment_id,
            );
            let launch_id = {
                let a = str_field(payload, "launch_id");
                if !a.is_empty() {
                    a
                } else {
                    str_field(payload, "launchId")
                }
            };
            let mut item = json!({
                "moment_id": moment_id,
                "game_id": stable_id,
                "created_at": now,
                "updated_at": now,
                "launch_id": launch_id,
                "title": title,
                "note": note,
                "trigger": trigger,
                "screenshot": screenshot,
                "resume_state": resume.unwrap_or(Value::Null),
            });
            if !capture_error.is_empty() {
                item.as_object_mut()
                    .unwrap()
                    .insert("capture_error".into(), Value::String(capture_error));
            }
            let sid_c = stable_id.clone();
            let item_c = item.clone();
            let shot_c = screenshot.clone();
            let (committed, saved) = match ctx.app.transact_state(move |st| {
                let mut lookup = Map::new();
                lookup.insert("game_id".into(), Value::String(sid_c));
                let idx = find_game_index_in_state(st, &lookup)
                    .map_err(|_| openbox_store::StateError::Corrupt("Game not found.".into()))?;
                if let Some(target) = st
                    .get_mut("games")
                    .and_then(Value::as_array_mut)
                    .and_then(|a| a.get_mut(idx))
                    .and_then(Value::as_object_mut)
                {
                    if !shot_c.is_empty() {
                        let shots = target
                            .entry("screenshots")
                            .or_insert_with(|| Value::Array(Vec::new()));
                        if !shots.is_array() {
                            *shots = Value::Array(Vec::new());
                        }
                        let sarr = shots.as_array_mut().unwrap();
                        if !sarr.iter().any(|v| v.as_str() == Some(shot_c.as_str())) {
                            sarr.push(Value::String(shot_c));
                        }
                    }
                    let moments = target
                        .entry("moments")
                        .or_insert_with(|| Value::Array(Vec::new()));
                    if !moments.is_array() {
                        *moments = Value::Array(Vec::new());
                    }
                    let marr = moments.as_array_mut().unwrap();
                    marr.push(item_c.clone());
                    if marr.len() > MAX_MOMENTS_PER_GAME {
                        let drop_n = marr.len() - MAX_MOMENTS_PER_GAME;
                        marr.drain(..drop_n);
                    }
                }
                Ok(item_c)
            }) {
                Ok(res) => res,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            if !screenshot.is_empty() {
                ctx.app.bump_media_epoch();
            }
            let mut lookup = Map::new();
            lookup.insert("game_id".into(), Value::String(stable_id));
            let committed_game = game_from_payload(&committed, &lookup)
                .map(|(_, g)| g)
                .unwrap_or(game);
            let public = moment_public(&ctx.app, &saved, &committed_game);
            ctx.app.sse.broadcast_event("moment.created", &public);
            Some(json_response(
                StatusCode::CREATED,
                &json!({"item": public}),
                ctx.instance,
            ))
        }
        "/api/v2/moments/update" => {
            let state = match ctx.app.load_state() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let (_, _, existing) = match find_moment(&state, payload) {
                Ok(triple) => triple,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let existing_trigger = existing
                .get("trigger")
                .and_then(Value::as_str)
                .unwrap_or("manual");
            let note = if payload.contains_key("note") {
                match clean_note(payload.get("note")) {
                    Ok(n) => n,
                    Err(err) => {
                        return Some(api_error_response(&err, ctx.request_id, ctx.instance));
                    }
                }
            } else {
                existing
                    .get("note")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned()
            };
            let title = if payload.contains_key("title") {
                match clean_title(payload.get("title"), existing_trigger) {
                    Ok(t) => t,
                    Err(err) => {
                        return Some(api_error_response(&err, ctx.request_id, ctx.instance));
                    }
                }
            } else {
                existing
                    .get("title")
                    .and_then(Value::as_str)
                    .unwrap_or("Moment")
                    .to_owned()
            };
            let moment_id = existing
                .get("moment_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            let gid_opt = payload
                .get("game_id")
                .and_then(Value::as_str)
                .map(str::to_owned);
            let (_, (saved, saved_game)) = match ctx.app.transact_state(move |st| {
                let mut lookup = Map::new();
                lookup.insert("moment_id".into(), Value::String(moment_id.clone()));
                if let Some(ref g) = gid_opt {
                    lookup.insert("game_id".into(), Value::String(g.clone()));
                }
                let (idx, _, _) = find_moment(st, &lookup)
                    .map_err(|e| openbox_store::StateError::Corrupt(e.message))?;
                let mut out_item = Value::Null;
                let mut out_game = Value::Null;
                if let Some(game_val) = st
                    .get_mut("games")
                    .and_then(Value::as_array_mut)
                    .and_then(|a| a.get_mut(idx))
                {
                    if let Some(marr) = game_val.get_mut("moments").and_then(Value::as_array_mut) {
                        for m in marr.iter_mut() {
                            if m.get("moment_id").and_then(Value::as_str)
                                == Some(moment_id.as_str())
                                && let Some(mobj) = m.as_object_mut()
                            {
                                mobj.insert("note".into(), Value::String(note.clone()));
                                mobj.insert("title".into(), Value::String(title.clone()));
                                mobj.insert("updated_at".into(), Value::String(utc_now()));
                                out_item = m.clone();
                                break;
                            }
                        }
                    }
                    out_game = game_val.clone();
                }
                Ok((out_item, out_game))
            }) {
                Ok(res) => res,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            Some(json_response(
                StatusCode::OK,
                &json!({"item": moment_public(&ctx.app, &saved, &saved_game)}),
                ctx.instance,
            ))
        }
        "/api/v2/moments/delete" => {
            let state = match ctx.app.load_state() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let (_, _, existing) = match find_moment(&state, payload) {
                Ok(triple) => triple,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let moment_id = existing
                .get("moment_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            let mid_c = moment_id.clone();
            let gid_opt = payload
                .get("game_id")
                .and_then(Value::as_str)
                .map(str::to_owned);
            if let Err(e) = ctx.app.transact_state(move |st| {
                let mut lookup = Map::new();
                lookup.insert("moment_id".into(), Value::String(mid_c.clone()));
                if let Some(ref g) = gid_opt {
                    lookup.insert("game_id".into(), Value::String(g.clone()));
                }
                let (idx, _, _) = find_moment(st, &lookup)
                    .map_err(|e| openbox_store::StateError::Corrupt(e.message))?;
                if let Some(marr) = st
                    .get_mut("games")
                    .and_then(Value::as_array_mut)
                    .and_then(|a| a.get_mut(idx))
                    .and_then(|g| g.get_mut("moments"))
                    .and_then(Value::as_array_mut)
                {
                    marr.retain(|m| {
                        m.get("moment_id").and_then(Value::as_str) != Some(mid_c.as_str())
                    });
                }
                Ok(())
            }) {
                return Some(state_error_response(&e, ctx.request_id, ctx.instance));
            }
            Some(json_response(
                StatusCode::OK,
                &json!({"ok": true, "deleted": true, "moment_id": moment_id}),
                ctx.instance,
            ))
        }
        "/api/v2/moments/resume" => {
            let state = match ctx.app.load_state() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let (idx, game, item) = match find_moment(&state, payload) {
                Ok(triple) => triple,
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
            let (Some(adapter), _) = adapter_for_launch(&game, &profiles, &adapters) else {
                return Some(api_error_response(
                    &ApiError::new(
                        400,
                        "RESUME_UNSUPPORTED",
                        "This launch path cannot resume from a saved state.",
                    ),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let link = item.get("resume_state");
            if let Some(lobj) = link.and_then(Value::as_object) {
                if lobj.get("adapter_id").and_then(Value::as_str).unwrap_or("")
                    != adapter.adapter_id
                {
                    return Some(api_error_response(
                        &ApiError::new(
                            400,
                            "MOMENT_RESUME_STALE",
                            "The moment was captured by a different adapter.",
                        ),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
                if lobj
                    .get("emulator_version")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    != emulator_fingerprint(Some(&adapter))
                {
                    return Some(api_error_response(
                        &ApiError::new(
                            400,
                            "MOMENT_RESUME_STALE",
                            "The moment was captured by a different emulator build.",
                        ),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            }
            let relative = match validate_snapshot_path(&ctx.app.data_dir, &game, link) {
                Ok(r) => r,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let extra_args = match inject_resume_args(
                &game,
                &[],
                &profiles,
                &settings,
                &adapters,
                Some(&relative),
                &ctx.app.data_dir,
                true,
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
                        "moment_id": item.get("moment_id").and_then(Value::as_str).unwrap_or(""),
                        "launch_id": entry.get("launch_id").and_then(Value::as_str).unwrap_or(""),
                        "state_file": relative,
                    }),
                    ctx.instance,
                )),
                Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            }
        }
        _ => None,
    }
}
