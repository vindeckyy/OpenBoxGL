//! Record That routes: OBS status/attach, replay-buffer clips, gallery listing, and reels
//! (`handlers/clips.py` + OBS routes from `handlers/media.py`).

use std::fs;
use std::path::{Path, PathBuf};

use axum::http::StatusCode;
use axum::response::Response;
use jiff::Timestamp;
use openbox_core::ApiError;
use openbox_integrations::misc::{attach_recording, obs_recording_directory, obs_recording_status};
use openbox_integrations::obs_bridge::{replay_enabled, save_replay_buffer};
use openbox_integrations::reels::{build_reel_manifest, create_reel};
use openbox_metadata::media::capture_screenshot;
use openbox_platform::expand_user;
use openbox_runtime::AppState;
use serde_json::{Map, Value, json};
use uuid::Uuid;

use crate::handlers::projection::find_game_index_in_state;
use crate::handlers::shared::{RequestCtx, str_field};
use crate::middleware::{api_error_response, json_response, state_error_response};

const MAX_CLIPS_PER_GAME: usize = 100;
const MAX_CLIP_BYTES: u64 = 2 * 1024 * 1024 * 1024;
const CLIP_EXTENSIONS: &[&str] = &[
    ".avi", ".flv", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".webm", ".png", ".jpg", ".jpeg",
    ".webp",
];

fn has_symlink_component(path: &Path) -> bool {
    let mut cur = path.to_path_buf();
    loop {
        if let Ok(meta) = fs::symlink_metadata(&cur)
            && meta.file_type().is_symlink()
        {
            return true;
        }
        if !cur.pop() {
            return false;
        }
    }
}

fn saved_filename(val: &Value, depth: usize) -> String {
    if depth > 4 {
        return String::new();
    }
    if let Some(s) = val.as_str() {
        return s.trim().to_owned();
    }
    let Some(obj) = val.as_object() else {
        return String::new();
    };
    for key in [
        "savedFilename",
        "saved_filename",
        "filename",
        "path",
        "file",
    ] {
        if let Some(s) = obj.get(key).and_then(Value::as_str)
            && !s.trim().is_empty()
        {
            return s.trim().to_owned();
        }
    }
    for v in obj.values() {
        let found = saved_filename(v, depth + 1);
        if !found.is_empty() {
            return found;
        }
    }
    String::new()
}

fn recording_roots(settings: &Map<String, Value>) -> Vec<PathBuf> {
    let mut raw_paths = Vec::new();
    if let Some(cfg) = settings.get("obs_recording_path").and_then(Value::as_str)
        && !cfg.trim().is_empty()
    {
        raw_paths.push(PathBuf::from(cfg));
    }
    raw_paths.push(obs_recording_directory(None));
    let mut roots = Vec::new();
    for p in raw_paths {
        let exp = expand_user(&p);
        if !exp.is_absolute() || has_symlink_component(&exp) {
            continue;
        }
        if let Ok(res) = fs::canonicalize(&exp)
            && res.is_dir()
            && !roots.contains(&res)
        {
            roots.push(res);
        }
    }
    roots
}

fn clip_destination(
    data_dir: &Path,
    stable_id: &str,
    suffix: &str,
) -> Result<PathBuf, std::io::Error> {
    let safe_id: String = stable_id
        .chars()
        .map(|c| {
            if c.is_alphanumeric() || c == '-' || c == '_' {
                c
            } else {
                '_'
            }
        })
        .take(160)
        .collect();
    let safe_id = if safe_id.is_empty() {
        "game".to_owned()
    } else {
        safe_id
    };
    let media_root = data_dir.join("media");
    fs::create_dir_all(&media_root)?;
    let media_resolved = fs::canonicalize(&media_root)?;
    let root = media_resolved.join("clips").join(&safe_id);
    if has_symlink_component(&root) {
        return Err(std::io::Error::other(
            "The OpenBox clip directory contains a symlink.",
        ));
    }
    fs::create_dir_all(&root)?;
    let stamp = Timestamp::now().strftime("%Y%m%d-%H%M%S").to_string();
    let hex_id = &Uuid::new_v4().simple().to_string()[..12];
    Ok(root.join(format!("{stamp}-{hex_id}{suffix}")))
}

fn copy_clip(
    data_dir: &Path,
    source: &str,
    stable_id: &str,
    settings: &Map<String, Value>,
) -> Option<String> {
    let text = source.trim();
    if text.is_empty() || text.contains('\0') {
        return None;
    }
    let src = expand_user(Path::new(text));
    if !src.is_absolute() || has_symlink_component(&src) || !src.is_file() {
        return None;
    }
    let ext = src
        .extension()
        .and_then(|e| e.to_str())
        .map(|e| format!(".{}", e.to_lowercase()))
        .unwrap_or_default();
    if !CLIP_EXTENSIONS.contains(&ext.as_str()) {
        return None;
    }
    if fs::metadata(&src).ok()?.len() > MAX_CLIP_BYTES {
        return None;
    }
    let resolved = fs::canonicalize(&src).ok()?;
    let roots = recording_roots(settings);
    if !roots
        .iter()
        .any(|r| resolved != *r && resolved.starts_with(r))
    {
        return None;
    }
    let dest = clip_destination(
        data_dir,
        stable_id,
        if ext.is_empty() { ".mp4" } else { &ext },
    )
    .ok()?;
    if fs::copy(&resolved, &dest).is_err() {
        let _ = fs::remove_file(&dest);
        return None;
    }
    fs::canonicalize(&dest)
        .ok()
        .map(|p| p.to_string_lossy().into_owned())
}

fn public_clip(app: &AppState, value: &Value) -> Option<Value> {
    let obj = value.as_object()?;
    let path = obj.get("path").and_then(Value::as_str)?.trim();
    if path.is_empty() {
        return None;
    }
    let approved = app.media_probe.approved_media_path(path, true).ok()?;
    let mut res = Map::new();
    res.insert("path".into(), json!(approved.to_string_lossy()));
    for key in [
        "clip_id",
        "created_at",
        "launch_id",
        "title",
        "source",
        "fallback",
    ] {
        if let Some(v) = obj.get(key)
            && !v.is_null()
            && v.as_str() != Some("")
        {
            if key == "fallback" {
                res.insert(key.to_owned(), json!(v.as_bool().unwrap_or(false)));
            } else {
                res.insert(
                    key.to_owned(),
                    Value::String(match v {
                        Value::String(s) => s.clone(),
                        other => other.to_string(),
                    }),
                );
            }
        }
    }
    Some(Value::Object(res))
}

fn game_from_query(state: &Value, ctx: &RequestCtx<'_>) -> Result<Value, ApiError> {
    let mut payload = Map::new();
    if let Some(gid) = ctx.query_first("game_id").filter(|s| !s.is_empty()) {
        payload.insert("game_id".into(), Value::String(gid));
    } else if let Some(id) = ctx.query_first("id").filter(|s| !s.is_empty()) {
        if let Ok(n) = id.parse::<i64>() {
            payload.insert("id".into(), json!(n));
        } else {
            payload.insert("game_id".into(), Value::String(id));
        }
    }
    let idx = find_game_index_in_state(state, &payload)
        .map_err(|_| ApiError::new(400, "GAME_NOT_FOUND", "Game not found."))?;
    state
        .get("games")
        .and_then(Value::as_array)
        .and_then(|a| a.get(idx))
        .cloned()
        .ok_or_else(|| ApiError::new(400, "GAME_NOT_FOUND", "Game not found."))
}

fn game_from_payload(state: &Value, payload: &Map<String, Value>) -> Result<Value, ApiError> {
    let idx = find_game_index_in_state(state, payload)
        .map_err(|_| ApiError::new(400, "GAME_NOT_FOUND", "Game not found."))?;
    state
        .get("games")
        .and_then(Value::as_array)
        .and_then(|a| a.get(idx))
        .cloned()
        .ok_or_else(|| ApiError::new(400, "GAME_NOT_FOUND", "Game not found."))
}

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    match ctx.path {
        "/api/v2/obs/status" => Some(json_response(
            StatusCode::OK,
            &obs_recording_status(None),
            ctx.instance,
        )),
        "/api/v2/clips" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let game = match game_from_query(&state, ctx) {
                Ok(g) => g,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let stable_id = game
                .get("game_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim();
            if stable_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::new(400, "GAME_NOT_FOUND", "Game has no stable ID."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let raw_clips = game
                .get("clips")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            let start = raw_clips.len().saturating_sub(MAX_CLIPS_PER_GAME);
            let clips: Vec<Value> = raw_clips[start..]
                .iter()
                .filter_map(|c| public_clip(&ctx.app, c))
                .collect();
            Some(json_response(
                StatusCode::OK,
                &json!({"game_id": stable_id, "clips": clips, "total": clips.len()}),
                ctx.instance,
            ))
        }
        "/api/v2/reels" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let game = match game_from_query(&state, ctx) {
                Ok(g) => g,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let manifest = build_reel_manifest(game.as_object(), None, None);
            Some(json_response(StatusCode::OK, &manifest, ctx.instance))
        }
        _ => None,
    }
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/obs/attach" => {
            let video_path = str_field(payload, "path").trim().to_owned();
            let state = match ctx.app.load_state() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let game = match game_from_payload(&state, payload) {
                Ok(g) => g,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let mut target = game.as_object().cloned().unwrap_or_default();
            let path = match attach_recording(&mut target, &video_path) {
                Ok(p) => p,
                Err(err) => {
                    return Some(api_error_response(
                        &ApiError::not_found(err),
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
            let vid_rec = target.get("video_recording").cloned();
            let vid = target.get("video").cloned();
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
                    if let Some(vr) = vid_rec {
                        g.insert("video_recording".into(), vr);
                    }
                    if let Some(v) = vid {
                        g.insert("video".into(), v);
                    }
                }
                Ok(())
            }) {
                return Some(state_error_response(&e, ctx.request_id, ctx.instance));
            }
            ctx.app.bump_media_epoch();
            Some(json_response(
                StatusCode::OK,
                &json!({"path": path, "obs": obs_recording_status(None)}),
                ctx.instance,
            ))
        }
        "/api/v2/clips/capture" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let game = match game_from_payload(&state, payload) {
                Ok(g) => g,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let stable_id = game
                .get("game_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim()
                .to_owned();
            if stable_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::new(400, "GAME_NOT_FOUND", "Game has no stable ID."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let settings = state
                .get("settings")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            let obs_res = if replay_enabled(Some(&settings)) {
                save_replay_buffer(Some(&settings))
            } else {
                None
            };
            let source = obs_res
                .as_ref()
                .map(|v| saved_filename(v, 0))
                .unwrap_or_default();
            let mut fallback = false;
            let mut clip_path = copy_clip(&ctx.app.data_dir, &source, &stable_id, &settings);
            if clip_path.is_none() {
                fallback = true;
                if let Ok(dest) = clip_destination(&ctx.app.data_dir, &stable_id, ".png") {
                    if let Ok(p) = capture_screenshot(&dest, "") {
                        clip_path = Some(p);
                    } else {
                        let _ = fs::remove_file(&dest);
                    }
                }
            }
            let Some(final_path) = clip_path else {
                return Some(api_error_response(
                    &ApiError::new(
                        400,
                        "CLIP_UNAVAILABLE",
                        "OBS replay is unavailable and a screenshot could not be captured.",
                    ),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let now = Timestamp::now()
                .strftime("%Y-%m-%dT%H:%M:%S+00:00")
                .to_string();
            let launch_id = {
                let a = str_field(payload, "launch_id");
                if !a.is_empty() {
                    a
                } else {
                    str_field(payload, "launchId")
                }
            };
            let clip = json!({
                "clip_id": format!("clip-{}", Uuid::new_v4().simple()),
                "path": final_path,
                "created_at": now,
                "launch_id": launch_id,
                "title": if fallback { "Screenshot fallback" } else { "Replay clip" },
                "source": if fallback { "screenshot" } else { "obs_replay" },
                "fallback": fallback,
            });
            let sid_c = stable_id.clone();
            let clip_c = clip.clone();
            let path_c = final_path.clone();
            if let Err(e) = ctx.app.transact_state(move |st| {
                let mut lookup = Map::new();
                lookup.insert("game_id".into(), Value::String(sid_c));
                let idx = find_game_index_in_state(st, &lookup).map_err(|_| {
                    openbox_store::StateError::Corrupt(
                        "Game disappeared while saving the clip.".into(),
                    )
                })?;
                if let Some(g) = st
                    .get_mut("games")
                    .and_then(Value::as_array_mut)
                    .and_then(|a| a.get_mut(idx))
                    .and_then(Value::as_object_mut)
                {
                    let clips = g.entry("clips").or_insert_with(|| Value::Array(Vec::new()));
                    if !clips.is_array() {
                        *clips = Value::Array(Vec::new());
                    }
                    let arr = clips.as_array_mut().unwrap();
                    arr.push(clip_c);
                    if arr.len() > MAX_CLIPS_PER_GAME {
                        let drop_n = arr.len() - MAX_CLIPS_PER_GAME;
                        arr.drain(..drop_n);
                    }
                    if !fallback {
                        g.insert("video_recording".into(), Value::String(path_c));
                    }
                }
                Ok(())
            }) {
                return Some(state_error_response(&e, ctx.request_id, ctx.instance));
            }
            ctx.app.bump_media_epoch();
            Some(json_response(
                StatusCode::OK,
                &json!({
                    "game_id": stable_id,
                    "clip": public_clip(&ctx.app, &clip),
                    "fallback": fallback,
                    "obs": !fallback,
                }),
                ctx.instance,
            ))
        }
        "/api/v2/reels/create" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let game = match game_from_payload(&state, payload) {
                Ok(g) => g,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let stable_id = game
                .get("game_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim()
                .to_owned();
            if stable_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::new(400, "GAME_NOT_FOUND", "Game has no stable ID."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let year = payload.get("year").map(|v| match v {
                Value::String(s) => s.clone(),
                Value::Number(n) => n.to_string(),
                _ => String::new(),
            });
            let data_dir = ctx.app.data_dir.clone();
            let sid_c = stable_id.clone();
            let snapshot = game.as_object().cloned().unwrap_or_default();
            let job_name = format!("reel:{stable_id}");
            match ctx
                .app
                .jobs
                .submit(&job_name, true, Some("reel"), None, move |_jctx| {
                    let y = year.as_deref().filter(|s| !s.is_empty());
                    let mut res = create_reel(Some(&snapshot), &data_dir, y)?;
                    if let Some(obj) = res.as_object_mut() {
                        obj.insert("game_id".into(), Value::String(sid_c.clone()));
                    }
                    Ok(res)
                }) {
                Ok(job) => Some(json_response(
                    StatusCode::ACCEPTED,
                    &json!({
                        "state": job.get("state").and_then(Value::as_str).unwrap_or("queued"),
                        "job_id": job.get("job_id").and_then(Value::as_str).unwrap_or(""),
                        "game_id": stable_id,
                    }),
                    ctx.instance,
                )),
                Err(e) => Some(api_error_response(
                    &ApiError::conflict(e),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        _ => None,
    }
}
