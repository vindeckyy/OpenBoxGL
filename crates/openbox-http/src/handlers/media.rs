//! Media serving (Range/304/416), audit, bulk download, durable media queue,
//! cache cleanup, RA badge proxy, Steam trailer / GOG / Bezel / EmuMovies / screenshot,
//! premium helpers, and Memories auto-import (`handlers/media.py`).

use std::collections::{BTreeSet, HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::{Arc, LazyLock};

use axum::http::StatusCode;
use axum::response::Response;
use openbox_core::ApiError;
use openbox_metadata::emumovies::{
    download_emumovies_media, emumovies_type_field, load_emumovies_credentials,
    save_emumovies_credentials,
};
use openbox_metadata::http::download_file_sync;
use openbox_metadata::lbdb::apply_game_metadata;
use openbox_metadata::media::{
    active_video, apply_media_pack, cancel_media_queue_worker, capture_screenshot,
    cleanup_duplicates, cleanup_generated_cache, download_bezel, download_gog_media,
    download_steam_trailer, enqueue_media_job, find_duplicate_media, media_queue_snapshot,
    media_queue_worker_state, pause_media_queue_worker, resume_media_queue_worker,
    retry_media_queue_item, start_media_queue_worker,
};
use openbox_metadata::memories::{
    IMPORT_JOB_NAME, MEMORIES_FIELD, UNASSIGNED_FIELD, apply_import_plan, collect_imports,
    configured_roots, memories_import_enabled,
};
use openbox_metadata::providers::provider_priority;
use openbox_runtime::AppState;
use openbox_runtime::media_probe::MEDIA_TYPES_ALL;
use openbox_store::media_manifest;
use parking_lot::Mutex;
use regex::Regex;
use serde_json::{Map, Value, json};

use crate::handlers::metadata::metadata_db_path;
use crate::handlers::projection::{
    find_game_index_in_state as find_game_index, list_media_packs, public_settings, strings_for,
};
use crate::handlers::shared::{RequestCtx, bool_field, str_field};
use crate::middleware::{api_error_response, json_response, state_error_response};
use crate::static_files::send_file;
use openbox_imports::platform_categories;

static MEDIA_JOB: LazyLock<Mutex<Map<String, Value>>> = LazyLock::new(|| Mutex::new(Map::new()));
static BADGE_STRIP_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[^A-Za-z0-9_-]").unwrap());

const BULK_MEDIA_BATCH_THRESHOLD: usize = 25;

const AUDIT_FIELDS: &[&str] = &[
    "cover",
    "background",
    "screenshots",
    "box_back",
    "box_spine",
    "box_3d",
    "clear_logo",
    "fanart",
    "banner",
    "icon",
    "title_screen",
    "cart_front",
    "cart_back",
    "disc",
    "advertisement",
    "manual",
];

const DIRECT_MEDIA_KINDS: &[&str] = &[
    "cover",
    "background",
    "clear_logo",
    "fanart",
    "banner",
    "icon",
    "poster",
    "square",
    "box_back",
    "box_spine",
    "box_3d",
    "title_screen",
    "cart_front",
    "cart_back",
    "disc",
    "advertisement",
    "manual",
    "video",
    "music",
    "video_snap",
    "video_theme",
    "video_trailer",
    "video_recording",
];

fn record_media_manifest(app: &AppState, path: &Path, source: &str) {
    let enabled = app
        .load_state_view()
        .ok()
        .and_then(|s| s.get("settings")?.get("media_manifest_enabled")?.as_bool())
        .unwrap_or(true);
    if enabled {
        let _ = media_manifest::record(&app.data_dir.join("media"), path, source);
    }
}

fn game_from_query<'a>(ctx: &RequestCtx<'_>, state: &'a Value) -> Result<&'a Value, ApiError> {
    let mut payload = Map::new();
    if let Some(gid) = ctx.query_first("game_id").filter(|s| !s.is_empty()) {
        payload.insert("game_id".into(), Value::String(gid));
    }
    if let Some(id_val) = ctx.query_first("id").filter(|s| !s.is_empty()) {
        payload.insert("id".into(), Value::String(id_val));
    }
    let idx = find_game_index(state, &payload)?;
    state
        .get("games")
        .and_then(Value::as_array)
        .and_then(|arr| arr.get(idx))
        .ok_or_else(|| ApiError::game_not_found("Game not found."))
}

fn memories_public(app: &AppState, entries: &[Value]) -> Vec<Value> {
    let mut out = Vec::new();
    for entry in entries {
        let Some(m) = entry.as_object() else {
            continue;
        };
        let raw_path = m.get("path").and_then(Value::as_str).unwrap_or("");
        let Ok(resolved) = app.media_probe.approved_media_path(raw_path, true) else {
            continue;
        };
        out.push(json!({
            "path": resolved.to_string_lossy(),
            "sha256": m.get("sha256").and_then(Value::as_str).unwrap_or(""),
            "bytes": m.get("bytes").and_then(Value::as_u64).unwrap_or(0),
            "source": m.get("source").and_then(Value::as_str).unwrap_or(""),
            "taken_at": m.get("taken_at").and_then(Value::as_str).unwrap_or(""),
            "imported_at": m.get("imported_at").and_then(Value::as_str).unwrap_or(""),
            "hint": m.get("hint").and_then(Value::as_str).unwrap_or(""),
        }));
    }
    out
}

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    match ctx.path {
        "/api/v2/media/audit" => {
            let platform = ctx
                .query_first("platform")
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| "all".to_owned());
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let games: Vec<&Value> = state
                .get("games")
                .and_then(Value::as_array)
                .map(|arr| {
                    arr.iter()
                        .filter(|g| {
                            platform == "all"
                                || g.get("platform").and_then(Value::as_str)
                                    == Some(platform.as_str())
                        })
                        .collect()
                })
                .unwrap_or_default();
            let mut values: Vec<String> = Vec::new();
            for g in &games {
                for field in AUDIT_FIELDS {
                    if *field == "screenshots" {
                        if let Some(arr) = g.get("screenshots").and_then(Value::as_array) {
                            for p in arr {
                                if let Some(s) = p.as_str() {
                                    values.push(s.to_owned());
                                }
                            }
                        }
                    } else if let Some(s) = g.get(*field).and_then(Value::as_str) {
                        values.push(s.to_owned());
                    }
                }
            }
            let probed = ctx.app.media_probe.probe_paths_batch(&values, true);
            let matched = games
                .iter()
                .filter(|g| {
                    g.get("launchbox_db_id")
                        .is_some_and(|v| !v.is_null() && v.as_str() != Some(""))
                })
                .count();
            let mut payload = Map::new();
            payload.insert("games".into(), json!(games.len()));
            payload.insert("matched".into(), json!(matched));
            for field in AUDIT_FIELDS {
                if *field == "screenshots" {
                    let missing = games
                        .iter()
                        .filter(|g| {
                            !g.get("screenshots")
                                .and_then(Value::as_array)
                                .is_some_and(|arr| {
                                    arr.iter().any(|p| {
                                        p.as_str().is_some_and(|s| {
                                            probed.get(s).copied().unwrap_or(false)
                                        })
                                    })
                                })
                        })
                        .count();
                    payload.insert("missing_screenshots".into(), json!(missing));
                } else {
                    let missing = games
                        .iter()
                        .filter(|g| {
                            let s = g.get(*field).and_then(Value::as_str).unwrap_or("");
                            !probed.get(s).copied().unwrap_or(false)
                        })
                        .count();
                    payload.insert(format!("missing_{field}"), json!(missing));
                }
            }
            Some(json_response(
                StatusCode::OK,
                &Value::Object(payload),
                ctx.instance,
            ))
        }
        "/api/v2/media/bulk/status" => {
            let job = Value::Object(MEDIA_JOB.lock().clone());
            Some(json_response(
                StatusCode::OK,
                &json!({"job": job}),
                ctx.instance,
            ))
        }
        "/api/v2/ra/badge" => {
            let raw_name = ctx.query_first("name").unwrap_or_default();
            let name = BADGE_STRIP_RE.replace_all(&raw_name, "").into_owned();
            let locked = ctx.query_first("locked").as_deref() == Some("1");
            if name.is_empty() {
                return Some(api_error_response(
                    &ApiError::badge_not_found("Badge not found"),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let filename = format!("{name}{}.png", if locked { "_lock" } else { "" });
            let candidate = ctx
                .app
                .data_dir
                .join("media")
                .join("retroachievements")
                .join("badges")
                .join(&filename);
            let Ok(badge) = ctx
                .app
                .media_probe
                .approved_media_path(&candidate.to_string_lossy(), false)
            else {
                return Some(api_error_response(
                    &ApiError::badge_not_found("Badge not found"),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            if !badge.is_file() {
                let url = format!("https://media.retroachievements.org/Badge/{filename}");
                if download_file_sync(&url, &badge, &["image/"], 16 * 1024 * 1024, 15, &[]).is_err()
                {
                    return Some(api_error_response(
                        &ApiError::badge_not_found("Badge not found"),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            }
            Some(send_file(
                ctx.headers,
                &badge,
                Some("image/png"),
                None,
                false,
                ctx.request_id,
                ctx.instance,
            ))
        }
        "/api/v2/media" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let Ok(game) = game_from_query(ctx, &state) else {
                return Some(api_error_response(
                    &ApiError::media_not_found("Media not found"),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let kind = ctx.query_first("kind").unwrap_or_default();
            let raw_path: Option<String> = if kind == "screenshot" {
                let idx = ctx
                    .query_first("index")
                    .and_then(|s| s.parse::<usize>().ok());
                idx.and_then(|i| {
                    game.get("screenshots")?
                        .as_array()?
                        .get(i)?
                        .as_str()
                        .map(ToOwned::to_owned)
                })
            } else if kind == "clip" {
                let idx = ctx
                    .query_first("index")
                    .and_then(|s| s.parse::<usize>().ok());
                idx.and_then(|i| {
                    game.get("clips")?
                        .as_array()?
                        .get(i)?
                        .get("path")?
                        .as_str()
                        .map(ToOwned::to_owned)
                })
            } else if DIRECT_MEDIA_KINDS.contains(&kind.as_str()) {
                if kind == "video" {
                    active_video(&ctx.app, game, None)
                        .map(|(_, p)| p)
                        .or_else(|| {
                            game.get("video")
                                .and_then(Value::as_str)
                                .map(ToOwned::to_owned)
                        })
                } else {
                    game.get(&kind)
                        .and_then(Value::as_str)
                        .map(ToOwned::to_owned)
                }
            } else {
                None
            };
            let Some(rp) = raw_path.filter(|s| !s.is_empty()) else {
                return Some(api_error_response(
                    &ApiError::media_not_found("Media not found"),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let Ok(approved) = ctx.app.media_probe.approved_media_path(&rp, true) else {
                return Some(api_error_response(
                    &ApiError::media_not_found("Media not found"),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            Some(send_file(
                ctx.headers,
                &approved,
                None,
                None,
                false,
                ctx.request_id,
                ctx.instance,
            ))
        }
        "/api/v2/media/duplicates" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let games = state
                .get("games")
                .and_then(Value::as_array)
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            Some(json_response(
                StatusCode::OK,
                &json!({"groups": find_duplicate_media(games, &[])}),
                ctx.instance,
            ))
        }
        "/api/v2/media/queue" => {
            let worker = media_queue_worker_state();
            let mut snap = media_queue_snapshot(
                &ctx.app.data_dir.join("media-queue.json"),
                worker["running"].as_bool().unwrap_or(false),
                worker["paused"].as_bool().unwrap_or(false),
                worker["completed"].as_u64().unwrap_or(0) as usize,
            );
            let failed = snap["failed"]
                .as_u64()
                .unwrap_or(0)
                .max(worker["failed"].as_u64().unwrap_or(0));
            let cancelled = worker["cancelled"].as_bool().unwrap_or(false);
            snap["failed"] = json!(failed);
            snap["cancelled"] = json!(cancelled);
            snap["counts"]["failed"] = json!(failed);
            snap["counts"]["cancelled"] = json!(cancelled);
            Some(json_response(StatusCode::OK, &snap, ctx.instance))
        }
        "/api/v2/premium/strings" => {
            let locale = ctx
                .query_first("locale")
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| "en".to_owned());
            Some(json_response(
                StatusCode::OK,
                &json!({"locale": locale, "strings": strings_for(&locale)}),
                ctx.instance,
            ))
        }
        "/api/v2/premium/media-packs" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let settings = state.get("settings").cloned().unwrap_or_else(|| json!({}));
            Some(json_response(
                StatusCode::OK,
                &json!({"packs": list_media_packs(&settings)}),
                ctx.instance,
            ))
        }
        "/api/v2/premium/platform-categories" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let settings = state.get("settings").cloned().unwrap_or_else(|| json!({}));
            Some(json_response(
                StatusCode::OK,
                &json!({"categories": platform_categories(&settings)}),
                ctx.instance,
            ))
        }
        "/api/v2/memories/status" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let settings = state.get("settings").cloned().unwrap_or_else(|| json!({}));
            let mut memories_count = 0usize;
            let mut games_with = 0usize;
            if let Some(games) = state.get("games").and_then(Value::as_array) {
                for g in games {
                    if let Some(entries) = g.get(MEMORIES_FIELD).and_then(Value::as_array)
                        && !entries.is_empty()
                    {
                        games_with += 1;
                        memories_count += entries.len();
                    }
                }
            }
            let unassigned = state
                .get(UNASSIGNED_FIELD)
                .and_then(Value::as_array)
                .map(Vec::len)
                .unwrap_or(0);
            Some(json_response(
                StatusCode::OK,
                &json!({
                    "enabled": memories_import_enabled(&settings),
                    "roots": configured_roots(&settings).len(),
                    "memories": memories_count,
                    "games_with_memories": games_with,
                    "unassigned": unassigned,
                    "job": ctx.app.jobs.snapshot(IMPORT_JOB_NAME),
                }),
                ctx.instance,
            ))
        }
        "/api/v2/memories" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let un_flag = ctx.query_first("unassigned").unwrap_or_default();
            let source: &[Value] = if matches!(un_flag.as_str(), "1" | "true" | "yes") {
                state
                    .get(UNASSIGNED_FIELD)
                    .and_then(Value::as_array)
                    .map(Vec::as_slice)
                    .unwrap_or(&[])
            } else {
                let game = match game_from_query(ctx, &state) {
                    Ok(g) => g,
                    Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
                };
                game.get(MEMORIES_FIELD)
                    .and_then(Value::as_array)
                    .map(Vec::as_slice)
                    .unwrap_or(&[])
            };
            Some(json_response(
                StatusCode::OK,
                &json!({"memories": memories_public(&ctx.app, source)}),
                ctx.instance,
            ))
        }
        "/api/v2/memories/media" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let source: &[Value] = if ctx.query_first("bucket").as_deref() == Some("unassigned") {
                state
                    .get(UNASSIGNED_FIELD)
                    .and_then(Value::as_array)
                    .map(Vec::as_slice)
                    .unwrap_or(&[])
            } else {
                let Ok(game) = game_from_query(ctx, &state) else {
                    return Some(api_error_response(
                        &ApiError::media_not_found("Media not found"),
                        ctx.request_id,
                        ctx.instance,
                    ));
                };
                game.get(MEMORIES_FIELD)
                    .and_then(Value::as_array)
                    .map(Vec::as_slice)
                    .unwrap_or(&[])
            };
            let Some(idx) = ctx
                .query_first("index")
                .and_then(|s| s.parse::<usize>().ok())
            else {
                return Some(api_error_response(
                    &ApiError::media_not_found("Media not found"),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let Some(raw_path) = source
                .get(idx)
                .and_then(|e| e.get("path"))
                .and_then(Value::as_str)
            else {
                return Some(api_error_response(
                    &ApiError::media_not_found("Media not found"),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let Ok(approved) = ctx.app.media_probe.approved_media_path(raw_path, true) else {
                return Some(api_error_response(
                    &ApiError::media_not_found("Media not found"),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            Some(send_file(
                ctx.headers,
                &approved,
                None,
                None,
                false,
                ctx.request_id,
                ctx.instance,
            ))
        }
        _ => None,
    }
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/media/queue/pause" => Some(json_response(
            StatusCode::OK,
            &pause_media_queue_worker(),
            ctx.instance,
        )),
        "/api/v2/media/queue/resume" => Some(json_response(
            StatusCode::OK,
            &resume_media_queue_worker(Arc::clone(&ctx.app)),
            ctx.instance,
        )),
        "/api/v2/media/queue/cancel" => Some(json_response(
            StatusCode::OK,
            &cancel_media_queue_worker(),
            ctx.instance,
        )),
        "/api/v2/media/queue/retry" => {
            let item_id = str_field(payload, "id");
            if item_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("A queue item id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if !retry_media_queue_item(&ctx.app.data_dir.join("media-queue.json"), item_id) {
                return Some(api_error_response(
                    &ApiError::bad_request("Queue item not found."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if !media_queue_worker_state()["running"]
                .as_bool()
                .unwrap_or(false)
            {
                start_media_queue_worker(Arc::clone(&ctx.app));
            }
            Some(json_response(
                StatusCode::OK,
                &json!({"ok": true, "id": item_id}),
                ctx.instance,
            ))
        }
        "/api/v2/media/cache/cleanup" => {
            let days: i64 = match payload.get("days") {
                None | Some(Value::Null) => 30,
                Some(Value::Number(n)) => match n.as_i64() {
                    Some(v) => v,
                    None => {
                        return Some(api_error_response(
                            &ApiError::bad_request("days must be a whole number."),
                            ctx.request_id,
                            ctx.instance,
                        ));
                    }
                },
                Some(Value::String(s)) => match s.trim().parse::<i64>() {
                    Ok(v) => v,
                    Err(_) => {
                        return Some(api_error_response(
                            &ApiError::bad_request("days must be a whole number."),
                            ctx.request_id,
                            ctx.instance,
                        ));
                    }
                },
                _ => {
                    return Some(api_error_response(
                        &ApiError::bad_request("days must be a whole number."),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let clamped = days.clamp(1, 3650) as u64;
            Some(json_response(
                StatusCode::OK,
                &cleanup_generated_cache(&ctx.app.data_dir, clamped),
                ctx.instance,
            ))
        }
        "/api/v2/premium/media-packs/apply" => {
            let pack_id = str_field(payload, "id").to_owned();
            let mut pack_res: Result<Value, String> = Err("Unknown media pack.".to_owned());
            let tx = ctx.app.transact_state(|st| {
                pack_res = apply_media_pack(st, &pack_id);
                if pack_res.is_err() {
                    return Err(openbox_store::StateError::Corrupt("unknown pack".into()));
                }
                Ok(())
            });
            match (tx, pack_res) {
                (Ok((state, _)), Ok(pack)) => {
                    ctx.app.bump_media_epoch();
                    let pub_set = public_settings(&ctx.app, &state);
                    Some(json_response(
                        StatusCode::OK,
                        &json!({"pack": pack, "settings": pub_set}),
                        ctx.instance,
                    ))
                }
                (_, Err(msg)) => Some(api_error_response(
                    &ApiError::bad_request(msg),
                    ctx.request_id,
                    ctx.instance,
                )),
                (Err(e), _) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/metadata/trailer" => {
            let state = match ctx.app.load_state() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let idx = match find_game_index(&state, payload) {
                Ok(i) => i,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let mut target = state["games"][idx].as_object().cloned().unwrap_or_default();
            let gid = target
                .get("game_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            let path = match download_steam_trailer(&mut target, &ctx.app.data_dir.join("media")) {
                Ok(p) => p,
                Err(e) => {
                    return Some(api_error_response(
                        &ApiError::bad_request(e),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            if !path.is_empty() {
                record_media_manifest(&ctx.app, Path::new(&path), "steam-trailer");
            }
            if let Err(e) = ctx.app.transact_state(|st| {
                if let Some(games) = st.get_mut("games").and_then(Value::as_array_mut)
                    && let Some(g) = games
                        .iter_mut()
                        .find(|x| x.get("game_id").and_then(Value::as_str) == Some(&gid))
                    && let Some(m) = g.as_object_mut()
                {
                    for (k, v) in &target {
                        m.insert(k.clone(), v.clone());
                    }
                }
                Ok(())
            }) {
                return Some(state_error_response(&e, ctx.request_id, ctx.instance));
            }
            ctx.app.bump_media_epoch();
            Some(json_response(
                StatusCode::OK,
                &json!({"video_trailer": path}),
                ctx.instance,
            ))
        }
        "/api/v2/metadata/gog" => {
            let state = match ctx.app.load_state() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let idx = match find_game_index(&state, payload) {
                Ok(i) => i,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let mut target = state["games"][idx].as_object().cloned().unwrap_or_default();
            let gid = target
                .get("game_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            if let Err(e) = download_gog_media(&mut target, &ctx.app.data_dir.join("media")) {
                return Some(api_error_response(
                    &ApiError::bad_request(e),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if let Err(e) = ctx.app.transact_state(|st| {
                if let Some(games) = st.get_mut("games").and_then(Value::as_array_mut)
                    && let Some(g) = games
                        .iter_mut()
                        .find(|x| x.get("game_id").and_then(Value::as_str) == Some(&gid))
                    && let Some(m) = g.as_object_mut()
                {
                    for (k, v) in &target {
                        m.insert(k.clone(), v.clone());
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
                    "cover": target.get("cover").and_then(Value::as_str).unwrap_or(""),
                    "background": target.get("background").and_then(Value::as_str).unwrap_or(""),
                }),
                ctx.instance,
            ))
        }
        "/api/v2/bezels/download" => {
            let platform = str_field(payload, "platform");
            match download_bezel(platform, &ctx.app.data_dir.join("bezels")) {
                Ok(path) => Some(json_response(
                    StatusCode::OK,
                    &json!({"path": path}),
                    ctx.instance,
                )),
                Err(e) => Some(api_error_response(
                    &ApiError::bad_request(e),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/emumovies/settings" => {
            let u = payload
                .get("username")
                .and_then(Value::as_str)
                .unwrap_or("");
            let p = payload
                .get("password")
                .and_then(Value::as_str)
                .unwrap_or("");
            match save_emumovies_credentials(&ctx.app.data_dir, u, p) {
                Ok(v) => Some(json_response(StatusCode::OK, &v, ctx.instance)),
                Err(e) => Some(api_error_response(
                    &ApiError::bad_request(e),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/emumovies/download" => {
            let (u, p) = load_emumovies_credentials(&ctx.app.data_dir);
            let state = match ctx.app.load_state() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let idx = match find_game_index(&state, payload) {
                Ok(i) => i,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let target = state["games"][idx].clone();
            let gid = target
                .get("game_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            let media_type = payload
                .get("type")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .unwrap_or("box");
            let Some(field) = emumovies_type_field(media_type) else {
                return Some(api_error_response(
                    &ApiError::bad_request(format!(
                        "Unsupported EmuMovies media type: {media_type}"
                    )),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let path = match download_emumovies_media(
                &target,
                &u,
                &p,
                &ctx.app.data_dir.join("media"),
                media_type,
            ) {
                Ok(p) => p,
                Err(e) => {
                    return Some(api_error_response(
                        &ApiError::bad_request(e),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            if !path.is_empty() {
                record_media_manifest(&ctx.app, Path::new(&path), &format!("emumovies-{field}"));
            }
            if let Err(e) = ctx.app.transact_state(|st| {
                if let Some(games) = st.get_mut("games").and_then(Value::as_array_mut)
                    && let Some(g) = games
                        .iter_mut()
                        .find(|x| x.get("game_id").and_then(Value::as_str) == Some(&gid))
                    && let Some(m) = g.as_object_mut()
                {
                    if field == "screenshots" {
                        let arr = m
                            .entry("screenshots".to_owned())
                            .or_insert_with(|| Value::Array(Vec::new()));
                        if !arr.is_array() {
                            *arr = Value::Array(Vec::new());
                        }
                        let list = arr.as_array_mut().unwrap();
                        if !list.iter().any(|v| v.as_str() == Some(&path)) {
                            list.push(Value::String(path.clone()));
                        }
                    } else {
                        m.insert(field.to_owned(), Value::String(path.clone()));
                    }
                }
                Ok(())
            }) {
                return Some(state_error_response(&e, ctx.request_id, ctx.instance));
            }
            ctx.app.bump_media_epoch();
            Some(json_response(
                StatusCode::OK,
                &json!({"path": path}),
                ctx.instance,
            ))
        }
        "/api/v2/media/cleanup" => {
            let platform = str_field(payload, "platform");
            let state = match ctx.app.load_state() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let games: Vec<Value> = state
                .get("games")
                .and_then(Value::as_array)
                .map(|arr| {
                    arr.iter()
                        .filter(|g| {
                            platform.is_empty()
                                || platform == "all"
                                || g.get("platform").and_then(Value::as_str).map(str::trim)
                                    == Some(platform)
                        })
                        .cloned()
                        .collect()
                })
                .unwrap_or_default();
            let allowed = vec![ctx.app.data_dir.clone()];
            let groups = find_duplicate_media(&games, &allowed);
            let apply = bool_field(payload, "apply");
            let deleted = cleanup_duplicates(&groups, !apply, &allowed);
            let cache = if apply || payload.contains_key("cache_days") {
                let cd = match payload.get("cache_days") {
                    None | Some(Value::Null) => 30i64,
                    Some(Value::Number(n)) => n.as_i64().unwrap_or(30),
                    Some(Value::String(s)) => match s.trim().parse::<i64>() {
                        Ok(v) => v,
                        Err(_) => {
                            return Some(api_error_response(
                                &ApiError::bad_request("cache_days must be a whole number."),
                                ctx.request_id,
                                ctx.instance,
                            ));
                        }
                    },
                    _ => {
                        return Some(api_error_response(
                            &ApiError::bad_request("cache_days must be a whole number."),
                            ctx.request_id,
                            ctx.instance,
                        ));
                    }
                };
                Some(cleanup_generated_cache(
                    &ctx.app.data_dir,
                    cd.clamp(1, 3650) as u64,
                ))
            } else {
                None
            };
            if apply && !deleted.is_empty() {
                ctx.app.bump_media_epoch();
            }
            Some(json_response(
                StatusCode::OK,
                &json!({
                    "groups": groups.len(),
                    "paths": deleted,
                    "applied": apply,
                    "platform": if platform.is_empty() { "all" } else { platform },
                    "cache": cache,
                }),
                ctx.instance,
            ))
        }
        "/api/v2/screenshot" => {
            let state = match ctx.app.load_state() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let idx = match find_game_index(&state, payload) {
                Ok(i) => i,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let game = &state["games"][idx];
            let gid = game
                .get("game_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            let stem = game
                .get("path")
                .and_then(Value::as_str)
                .and_then(|p| Path::new(p).file_stem())
                .and_then(|s| s.to_str())
                .unwrap_or("game");
            let stamp = jiff::Timestamp::now().strftime("%Y%m%d-%H%M%S").to_string();
            let dest = ctx
                .app
                .data_dir
                .join("media")
                .join("captures")
                .join(format!("{stem}-{stamp}.png"));
            let path = match capture_screenshot(&dest, "") {
                Ok(p) => p,
                Err(e) => {
                    return Some(api_error_response(
                        &ApiError::bad_request(e),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            record_media_manifest(&ctx.app, Path::new(&path), "capture");
            if let Err(e) = ctx.app.transact_state(|st| {
                if let Some(games) = st.get_mut("games").and_then(Value::as_array_mut)
                    && let Some(g) = games
                        .iter_mut()
                        .find(|x| x.get("game_id").and_then(Value::as_str) == Some(&gid))
                    && let Some(m) = g.as_object_mut()
                {
                    let arr = m
                        .entry("screenshots".to_owned())
                        .or_insert_with(|| Value::Array(Vec::new()));
                    if !arr.is_array() {
                        *arr = Value::Array(Vec::new());
                    }
                    let list = arr.as_array_mut().unwrap();
                    if !list.iter().any(|v| v.as_str() == Some(&path)) {
                        list.push(Value::String(path.clone()));
                    }
                }
                Ok(())
            }) {
                return Some(state_error_response(&e, ctx.request_id, ctx.instance));
            }
            ctx.app.bump_media_epoch();
            Some(json_response(
                StatusCode::OK,
                &json!({"path": path}),
                ctx.instance,
            ))
        }
        "/api/v2/media/bulk" => {
            let Some(media_arr) = payload
                .get("media")
                .and_then(Value::as_array)
                .filter(|a| !a.is_empty())
            else {
                return Some(api_error_response(
                    &ApiError::bad_request("Select at least one valid media type."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let mut media_types = Vec::new();
            for m in media_arr {
                let s = m.as_str().unwrap_or("");
                if !MEDIA_TYPES_ALL.contains(&s) {
                    return Some(api_error_response(
                        &ApiError::bad_request("Select at least one valid media type."),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
                media_types.push(s.to_owned());
            }
            let explicit_ids: Option<HashSet<String>> = match payload.get("game_ids") {
                None | Some(Value::Null) => None,
                Some(Value::Array(a)) => Some(
                    a.iter()
                        .filter_map(|v| v.as_str().map(ToOwned::to_owned))
                        .collect(),
                ),
                _ => {
                    return Some(api_error_response(
                        &ApiError::bad_request("game_ids must be an array."),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let db_path = metadata_db_path(&ctx.app);
            let db_avail = db_path.is_file();
            let platform = payload
                .get("platform")
                .and_then(Value::as_str)
                .unwrap_or("all")
                .to_owned();
            let overwrite = bool_field(payload, "overwrite");
            let retry_failed = bool_field(payload, "retry_failed");
            let state = match ctx.app.load_state() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let priority = provider_priority(&state);
            let plugin_pri = state
                .get("settings")
                .and_then(|s| s.get("plugin_metadata_priority"))
                .and_then(Value::as_bool)
                .unwrap_or(false);
            let queue_path = ctx.app.data_dir.join("media-queue.json");
            let mut queued = 0usize;
            if let Some(games) = state.get("games").and_then(Value::as_array) {
                for g in games {
                    let has_lbdb = g
                        .get("launchbox_db_id")
                        .is_some_and(|v| !v.is_null() && v.as_str() != Some(""));
                    if has_lbdb || retry_failed {
                        continue;
                    }
                    if platform != "all"
                        && g.get("platform").and_then(Value::as_str) != Some(platform.as_str())
                    {
                        continue;
                    }
                    let gid = g.get("game_id").and_then(Value::as_str).unwrap_or("");
                    if let Some(ref w) = explicit_ids
                        && !w.contains(gid)
                    {
                        continue;
                    }
                    let mut source = g
                        .get("metadata_provider")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .trim()
                        .to_owned();
                    if source.starts_with("plugin:") {
                        if !priority.iter().any(|p| p == &source) {
                            source.clear();
                        }
                    } else if !matches!(source.as_str(), "screenscraper" | "igdb" | "steamgriddb") {
                        source.clear();
                    }
                    if source.is_empty() && plugin_pri {
                        source = priority
                            .iter()
                            .find(|k| k.starts_with("plugin:"))
                            .cloned()
                            .unwrap_or_default();
                    }
                    if source.is_empty()
                        && g.get("steamgrid_id")
                            .or_else(|| g.get("steamgriddb_id"))
                            .is_some_and(|v| !v.is_null() && v.as_str() != Some(""))
                    {
                        source = "steamgriddb".to_owned();
                    }
                    if source.is_empty() {
                        continue;
                    }
                    if enqueue_media_job(
                        &queue_path,
                        &json!({
                            "game_id": gid,
                            "source": source,
                            "media": media_types,
                            "overwrite": overwrite,
                        }),
                    )
                    .is_ok()
                    {
                        queued += 1;
                    }
                }
            }
            if queued == 0 && !db_avail {
                return Some(api_error_response(
                    &ApiError::bad_request("Download the metadata database first."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if queued > 0 {
                start_media_queue_worker(Arc::clone(&ctx.app));
            }
            let (completed_ids, failed_ids) = {
                let mut job = MEDIA_JOB.lock();
                if job.get("state").and_then(Value::as_str) == Some("running") {
                    let cur = Value::Object(job.clone());
                    return Some(json_response(StatusCode::OK, &cur, ctx.instance));
                }
                let comp: Vec<String> = if retry_failed {
                    job.get("completed_game_ids")
                        .and_then(Value::as_array)
                        .map(|a| {
                            a.iter()
                                .filter_map(|v| v.as_str().map(ToOwned::to_owned))
                                .collect()
                        })
                        .unwrap_or_default()
                } else {
                    Vec::new()
                };
                let fail: Vec<String> = if retry_failed {
                    job.get("failed_game_ids")
                        .and_then(Value::as_array)
                        .map(|a| {
                            a.iter()
                                .filter_map(|v| v.as_str().map(ToOwned::to_owned))
                                .collect()
                        })
                        .unwrap_or_default()
                } else {
                    Vec::new()
                };
                job.clear();
                job.insert("state".into(), Value::String("running".to_owned()));
                job.insert("current".into(), json!(0));
                job.insert("total".into(), json!(0));
                job.insert("updated".into(), json!(0));
                job.insert("errors".into(), json!([]));
                job.insert("completed_game_ids".into(), json!(comp));
                job.insert("failed_game_ids".into(), json!(fail));
                (comp, fail)
            };

            let app = Arc::clone(&ctx.app);
            let _ = ctx
                .app
                .jobs
                .submit("media-bulk", false, None, None, move |jctx| {
                    let st = app.load_state().map_err(|e| e.to_string())?;
                    let games = st
                        .get("games")
                        .and_then(Value::as_array)
                        .map(Vec::as_slice)
                        .unwrap_or(&[]);
                    let games_by_id: HashMap<String, &Value> = games
                        .iter()
                        .filter_map(|g| Some((g.get("game_id")?.as_str()?.to_owned(), g)))
                        .collect();
                    let failed_filter: HashSet<String> = failed_ids.iter().cloned().collect();
                    let mut targets: Vec<(String, i64)> = Vec::new();
                    if db_avail {
                        for g in games {
                            let Some(gid) = g.get("game_id").and_then(Value::as_str) else {
                                continue;
                            };
                            if platform != "all"
                                && g.get("platform").and_then(Value::as_str)
                                    != Some(platform.as_str())
                            {
                                continue;
                            }
                            let Some(db_id) = g.get("launchbox_db_id").and_then(|v| match v {
                                Value::Number(n) => n.as_i64(),
                                Value::String(s) => s.trim().parse::<i64>().ok(),
                                _ => None,
                            }) else {
                                continue;
                            };
                            if retry_failed && !failed_filter.contains(gid) {
                                continue;
                            }
                            if !retry_failed
                                && let Some(ref w) = explicit_ids
                                && !w.contains(gid)
                            {
                                continue;
                            }
                            targets.push((gid.to_owned(), db_id));
                        }
                    }
                    MEDIA_JOB
                        .lock()
                        .insert("total".into(), json!(targets.len()));
                    let mut completed_set: BTreeSet<String> = completed_ids.into_iter().collect();
                    let mut failed_set: BTreeSet<String> = failed_ids.into_iter().collect();
                    let mut updated_count = 0usize;
                    let mut manual_missing = 0usize;
                    let mut errors: Vec<String> = Vec::new();
                    let mut changes_by_id: HashMap<String, Map<String, Value>> = HashMap::new();
                    let lb_root: PathBuf = app.data_dir.join("media").join("launchbox");

                    for (i, (gid, db_id)) in targets.iter().enumerate() {
                        if jctx.is_cancelled() {
                            break;
                        }
                        if completed_set.contains(gid) {
                            continue;
                        }
                        let orig = games_by_id
                            .get(gid)
                            .and_then(|g| g.as_object().cloned())
                            .unwrap_or_default();
                        let name = orig
                            .get("name")
                            .and_then(Value::as_str)
                            .unwrap_or(gid)
                            .to_owned();
                        let mut upd = orig.clone();
                        match apply_game_metadata(
                            &mut upd,
                            &db_path,
                            *db_id,
                            &media_types,
                            &lb_root,
                            overwrite,
                            None,
                        ) {
                            Ok(()) => {
                                if upd.remove("_media_notes").is_some() {
                                    manual_missing += 1;
                                }
                                let mut ch = Map::new();
                                for (k, v) in upd {
                                    if orig.get(&k) != Some(&v) {
                                        ch.insert(k, v);
                                    }
                                }
                                if !ch.is_empty() {
                                    changes_by_id.insert(gid.clone(), ch);
                                } else {
                                    completed_set.insert(gid.clone());
                                }
                                failed_set.remove(gid);
                            }
                            Err(e) => {
                                errors.push(format!("{name}: {e}"));
                                failed_set.insert(gid.clone());
                            }
                        }
                        let mut job = MEDIA_JOB.lock();
                        job.insert("current".into(), json!(i + 1));
                        job.insert("updated".into(), json!(updated_count));
                        job.insert(
                            "errors".into(),
                            json!(
                                errors
                                    .iter()
                                    .rev()
                                    .take(20)
                                    .rev()
                                    .cloned()
                                    .collect::<Vec<_>>()
                            ),
                        );
                        job.insert("manual_missing".into(), json!(manual_missing));
                        job.insert(
                            "completed_game_ids".into(),
                            json!(completed_set.iter().cloned().collect::<Vec<_>>()),
                        );
                        job.insert(
                            "failed_game_ids".into(),
                            json!(failed_set.iter().cloned().collect::<Vec<_>>()),
                        );
                    }

                    if !changes_by_id.is_empty() {
                        if targets.len() <= BULK_MEDIA_BATCH_THRESHOLD {
                            for (gid, ch) in changes_by_id {
                                let res = app.transact_state(|cur| {
                                    if let Some(gs) =
                                        cur.get_mut("games").and_then(Value::as_array_mut)
                                        && let Some(t) = gs.iter_mut().find(|x| {
                                            x.get("game_id").and_then(Value::as_str) == Some(&gid)
                                        })
                                        && let Some(m) = t.as_object_mut()
                                    {
                                        for (k, v) in &ch {
                                            m.insert(k.clone(), v.clone());
                                        }
                                    }
                                    Ok(())
                                });
                                match res {
                                    Ok(_) => {
                                        updated_count += 1;
                                        completed_set.insert(gid);
                                    }
                                    Err(e) => {
                                        errors.push(format!("{gid}: {e}"));
                                        failed_set.insert(gid);
                                    }
                                }
                            }
                        } else {
                            let pending_ids: Vec<String> = changes_by_id.keys().cloned().collect();
                            match app.transact_state(|cur| {
                                if let Some(gs) = cur.get_mut("games").and_then(Value::as_array_mut)
                                {
                                    for g in gs.iter_mut() {
                                        if let Some(gid) = g.get("game_id").and_then(Value::as_str)
                                            && let Some(ch) = changes_by_id.get(gid)
                                            && let Some(m) = g.as_object_mut()
                                        {
                                            for (k, v) in ch {
                                                m.insert(k.clone(), v.clone());
                                            }
                                        }
                                    }
                                }
                                Ok(())
                            }) {
                                Ok(_) => {
                                    updated_count += pending_ids.len();
                                    completed_set.extend(pending_ids);
                                }
                                Err(e) => {
                                    errors.push(format!("batch state update failed: {e}"));
                                    failed_set.extend(pending_ids);
                                }
                            }
                        }
                    }

                    app.bump_media_epoch();
                    let final_state = if !failed_set.is_empty() && !completed_set.is_empty() {
                        "partial"
                    } else if !failed_set.is_empty() {
                        "error"
                    } else {
                        "done"
                    };
                    let mut job = MEDIA_JOB.lock();
                    job.insert("state".into(), Value::String(final_state.to_owned()));
                    job.insert("updated".into(), json!(updated_count));
                    job.insert(
                        "errors".into(),
                        json!(
                            errors
                                .iter()
                                .rev()
                                .take(20)
                                .rev()
                                .cloned()
                                .collect::<Vec<_>>()
                        ),
                    );
                    job.insert(
                        "completed_game_ids".into(),
                        json!(completed_set.iter().cloned().collect::<Vec<_>>()),
                    );
                    job.insert(
                        "failed_game_ids".into(),
                        json!(failed_set.iter().cloned().collect::<Vec<_>>()),
                    );
                    Ok(json!({"state": final_state, "updated": updated_count}))
                });
            Some(json_response(
                StatusCode::ACCEPTED,
                &json!({"state": "running", "queued": queued}),
                ctx.instance,
            ))
        }
        "/api/v2/memories/import" => {
            let state = match ctx.app.load_state() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let settings = state.get("settings").cloned().unwrap_or_else(|| json!({}));
            if !memories_import_enabled(&settings) {
                return Some(json_response(
                    StatusCode::OK,
                    &json!({"enabled": false, "state": "disabled", "added": 0}),
                    ctx.instance,
                ));
            }
            let app = Arc::clone(&ctx.app);
            let job = match ctx
                .app
                .jobs
                .submit(IMPORT_JOB_NAME, false, None, None, move |jctx| {
                    let st = app.load_state().map_err(|e| e.to_string())?;
                    let cur_settings = st.get("settings").cloned().unwrap_or_else(|| json!({}));
                    if !memories_import_enabled(&cur_settings) {
                        return Ok(json!({"enabled": false, "added": 0}));
                    }
                    let extra = configured_roots(&cur_settings);
                    let plan =
                        collect_imports(&st, &app.data_dir, &extra, Some(jctx.cancel_flag()));
                    let (_, applied) = app
                        .transact_state(|s| Ok(apply_import_plan(s, &plan)))
                        .map_err(|e| e.to_string())?;
                    let mut counts = plan["counts"].clone();
                    let att = applied["attached"].as_u64().unwrap_or(0);
                    let un = applied["unassigned"].as_u64().unwrap_or(0);
                    let fail = applied["failed"].as_u64().unwrap_or(0);
                    counts["added"] = json!(att);
                    counts["unassigned"] = json!(un);
                    counts["failed"] = json!(counts["failed"].as_u64().unwrap_or(0) + fail);
                    if att > 0 || un > 0 {
                        app.bump_media_epoch();
                    }
                    Ok(counts)
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
                &json!({
                    "state": job.get("state").and_then(Value::as_str).unwrap_or("queued"),
                    "job_id": job.get("job_id").and_then(Value::as_str).unwrap_or(""),
                }),
                ctx.instance,
            ))
        }
        _ => None,
    }
}
