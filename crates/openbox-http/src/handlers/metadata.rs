//! Metadata search, status, sync, apply, match, IGDB, match-preview, auto-scrape,
//! scrape-settings, and achievements refresh/override handlers (`handlers/metadata.py`).

use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::{Arc, LazyLock};

use axum::http::StatusCode;
use axum::response::Response;
use openbox_core::{ApiError, job_result_state};
use openbox_metadata::http::{download_bytes_sync, ext_from_url};
use openbox_metadata::igdb::{
    apply_to_game as apply_igdb_metadata, fetch_game as fetch_igdb_game,
    is_configured as igdb_configured, search_games as search_igdb_games,
};
use openbox_metadata::lbdb::{
    DEFAULT_MATCH_ITEMS_LIMIT, MAX_MATCH_ITEMS_LIMIT, MAX_REJECTED_IDS, apply_game_metadata,
    apply_match_decisions, apply_preview_fields, batch_match, create_match_preview_record,
    lbdb_media_candidates, list_match_preview_items, load_match_preview, load_metadata_record,
    match_preview_document, run_match_preview_job, save_match_preview, search_games, sync_database,
};
use openbox_metadata::media::update_steam_metadata;
use openbox_metadata::providers::{APPLY_FIELDS, apply_candidate, search_all};
use openbox_metadata::screenscraper::{
    HASH_TIER_DUAL, apply_to_game as apply_ss_metadata, confident_hash_match,
    is_configured as ss_configured, system_id_for_platform,
};
use openbox_metadata::steam_achievements::{get_achievements, load_steam_credentials};
use openbox_metadata::steamgrid::{
    choose_media as choose_sgdb_media, clean_media_url as clean_sgdb_url,
    game_info as sgdb_game_info, is_configured as sgdb_configured,
    search_games as search_sgdb_games,
};
use openbox_runtime::AppState;
use openbox_runtime::media_probe::MEDIA_TYPES_ALL;
use parking_lot::Mutex;
use regex::Regex;
use serde_json::{Map, Value, json};

use crate::handlers::projection::find_game_index_in_state as find_game_index;
use crate::handlers::shared::{RequestCtx, bool_field, parse_limit, str_field};
use crate::middleware::{api_error_response, json_response, state_error_response};

static METADATA_JOB: LazyLock<Mutex<Map<String, Value>>> = LazyLock::new(|| Mutex::new(Map::new()));
static MATCH_JOB: LazyLock<Mutex<Map<String, Value>>> = LazyLock::new(|| Mutex::new(Map::new()));
static EXACT_MEDIA_URL_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)^https?://\S+$").unwrap());
static NON_ALNUM_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[^a-z0-9]").unwrap());

const AUTO_SCRAPE_SS_BUDGET: usize = 50;
const AUTO_SCRAPE_IGDB_BUDGET: usize = 50;
const AUTO_SCRAPE_SGDB_BUDGET: usize = 50;
const AUTO_SCRAPE_SGDB_KINDS: &[&str] = &["cover", "background", "clear_logo", "icon", "banner"];
const AUTO_SCRAPE_DEFAULT_MEDIA: &[&str] = &["cover", "background", "screenshots"];

const SCRAPE_SETTING_KEYS: &[(&str, bool)] = &[
    ("scrape_after_import", true),
    ("scrape_screenscraper_enabled", false),
    ("scrape_igdb_enabled", false),
    ("scrape_steamgrid_enabled", false),
];

pub fn metadata_db_path(app: &AppState) -> PathBuf {
    app.data_dir.join("metadata").join("launchbox.db")
}

fn game_from_query<'a>(ctx: &RequestCtx<'_>, state: &'a Value) -> Result<&'a Value, ApiError> {
    let mut payload = Map::new();
    if let Some(gid) = ctx.query_first("game_id").filter(|s| !s.is_empty()) {
        payload.insert("game_id".into(), Value::String(gid));
    }
    if let Some(id_val) = ctx
        .query_first("id")
        .or_else(|| ctx.query_first("index"))
        .filter(|s| !s.is_empty())
    {
        payload.insert("id".into(), Value::String(id_val));
    }
    let idx = find_game_index(state, &payload)?;
    state
        .get("games")
        .and_then(Value::as_array)
        .and_then(|arr| arr.get(idx))
        .ok_or_else(|| ApiError::game_not_found("Game not found."))
}

fn preview_state_for_job(ctx: &RequestCtx<'_>, job_id: Option<&str>) -> String {
    let Some(jid) = job_id.filter(|s| !s.is_empty()) else {
        return "ready".to_owned();
    };
    let Some(op) = ctx.app.operations.get(jid) else {
        return "ready".to_owned();
    };
    job_result_state(op.get("state").and_then(Value::as_str)).to_owned()
}

fn scrape_settings_obj(state: &Value) -> Value {
    let settings = state.get("settings").and_then(Value::as_object);
    let mut out = Map::new();
    for &(k, default) in SCRAPE_SETTING_KEYS {
        let v = settings
            .and_then(|s| s.get(k))
            .and_then(Value::as_bool)
            .unwrap_or(default);
        out.insert(k.to_owned(), Value::Bool(v));
    }
    Value::Object(out)
}

fn clean_exact_media_urls(
    value: Option<&Value>,
    allowed: &HashSet<(String, String)>,
) -> HashMap<String, String> {
    let mut out = HashMap::new();
    if let Some(obj) = value.and_then(Value::as_object) {
        for (k, v) in obj {
            let kind = k.trim();
            let url = v.as_str().unwrap_or("").trim();
            if MEDIA_TYPES_ALL.contains(&kind)
                && EXACT_MEDIA_URL_RE.is_match(url)
                && allowed.contains(&(kind.to_owned(), url.to_owned()))
            {
                out.insert(kind.to_owned(), url.to_owned());
            }
        }
    }
    out
}

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    match ctx.path {
        "/api/v2/metadata/status" => {
            let job = Value::Object(METADATA_JOB.lock().clone());
            let match_job = Value::Object(MATCH_JOB.lock().clone());
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let games = state
                .get("games")
                .and_then(Value::as_array)
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            let total = games.len();
            let matched = games
                .iter()
                .filter(|g| {
                    g.get("launchbox_db_id")
                        .is_some_and(|v| !v.is_null() && v.as_str() != Some(""))
                })
                .count();
            let ratio = if total > 0 {
                ((matched as f64 / total as f64) * 10000.0).round_ties_even() / 10000.0
            } else {
                0.0
            };
            let mut coverage = Map::new();
            coverage.insert("games".into(), json!(total));
            coverage.insert("matched_games".into(), json!(matched));
            coverage.insert("matched_ratio".into(), json!(ratio));
            let mut sorted_types: Vec<&str> = MEDIA_TYPES_ALL.to_vec();
            sorted_types.sort_unstable();
            for field in sorted_types {
                let with_cnt = games
                    .iter()
                    .filter(|g| {
                        g.get(field)
                            .and_then(Value::as_str)
                            .is_some_and(|p| !p.is_empty() && Path::new(p).is_file())
                    })
                    .count();
                coverage.insert(format!("with_{field}"), json!(with_cnt));
            }
            Some(json_response(
                StatusCode::OK,
                &json!({
                    "ready": metadata_db_path(&ctx.app).is_file(),
                    "job": job,
                    "match_job": match_job,
                    "coverage": coverage,
                }),
                ctx.instance,
            ))
        }
        "/api/v2/metadata/search" => {
            let db_path = metadata_db_path(&ctx.app);
            if !db_path.is_file() {
                return Some(api_error_response(
                    &ApiError::conflict("Download the LaunchBox metadata database first."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let game = match game_from_query(ctx, &state) {
                Ok(g) => g,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let title = ctx.query_first("q").unwrap_or_else(|| {
                game.get("name")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned()
            });
            let platform = game.get("platform").and_then(Value::as_str).unwrap_or("");
            let results = match search_games(&db_path, &title, platform, 20) {
                Ok(r) => r,
                Err(e) => {
                    return Some(api_error_response(
                        &ApiError::bad_request(e),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let mut query_game = game.clone();
            if let Some(m) = query_game.as_object_mut() {
                m.insert("name".into(), Value::String(title));
            }
            let candidates = search_all(&state, &db_path, &query_game, None, 20);
            Some(json_response(
                StatusCode::OK,
                &json!({"results": results, "candidates": candidates}),
                ctx.instance,
            ))
        }
        "/api/v2/metadata/igdb/search" => {
            let query = ctx.query_first("q").unwrap_or_default();
            let platform = ctx.query_first("platform").unwrap_or_default();
            match search_igdb_games(&query, &platform, 12) {
                Ok(results) => Some(json_response(
                    StatusCode::OK,
                    &json!({"results": results}),
                    ctx.instance,
                )),
                Err(e) => Some(api_error_response(
                    &ApiError::bad_request(e),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/metadata/matches/preview" => {
            let pid = ctx.query_first("preview_id").unwrap_or_default();
            let pid = pid.trim();
            if pid.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("preview_id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let preview = match load_match_preview(&ctx.app.data_dir, pid, false) {
                Ok(p) => p,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let mut doc = match_preview_document(&preview);
            let mut st = preview_state_for_job(ctx, preview.get("job_id").and_then(Value::as_str));
            if preview.get("state").and_then(Value::as_str) == Some("ready") {
                st = "ready".to_owned();
            }
            doc["state"] = Value::String(st);
            Some(json_response(StatusCode::OK, &doc, ctx.instance))
        }
        "/api/v2/metadata/matches/items" => {
            let pid = ctx.query_first("preview_id").unwrap_or_default();
            let pid = pid.trim();
            if pid.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("preview_id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let cursor = ctx.query_first("cursor");
            let limit = match parse_limit(
                ctx.query_first("limit").as_deref(),
                DEFAULT_MATCH_ITEMS_LIMIT,
                MAX_MATCH_ITEMS_LIMIT,
            ) {
                Ok(n) => n,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let match_class = ctx.query_first("class").filter(|s| !s.is_empty());
            if let Some(ref mc) = match_class
                && !matches!(
                    mc.as_str(),
                    "exact_review" | "likely" | "possible" | "unmatched"
                )
            {
                return Some(api_error_response(
                    &ApiError::bad_request("Invalid class filter."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            match list_match_preview_items(
                &ctx.app.data_dir,
                pid,
                cursor.as_deref(),
                limit,
                match_class.as_deref(),
            ) {
                Ok(payload) => Some(json_response(StatusCode::OK, &payload, ctx.instance)),
                Err(e) => Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/metadata/media-candidates" => {
            let db_id = ctx
                .query_first("database_id")
                .and_then(|s| s.trim().parse::<i64>().ok())
                .unwrap_or(0);
            if db_id <= 0 {
                return Some(api_error_response(
                    &ApiError::bad_request("database_id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let db_path = metadata_db_path(&ctx.app);
            if !db_path.is_file() {
                return Some(api_error_response(
                    &ApiError::conflict("Download the metadata database first."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            match lbdb_media_candidates(&db_path, db_id) {
                Ok(candidates) => Some(json_response(
                    StatusCode::OK,
                    &json!({"database_id": db_id, "candidates": candidates}),
                    ctx.instance,
                )),
                Err(e) => Some(api_error_response(
                    &ApiError::bad_request(e),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/metadata/scrape-settings" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            Some(json_response(
                StatusCode::OK,
                &scrape_settings_obj(&state),
                ctx.instance,
            ))
        }
        _ => None,
    }
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/metadata/steam" => {
            let state = match ctx.app.load_state_view() {
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
            if let Err(e) = update_steam_metadata(&mut target, &ctx.app.data_dir) {
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
                &json!({"ok": true}),
                ctx.instance,
            ))
        }
        "/api/v2/metadata/sync" => {
            {
                let mut job = METADATA_JOB.lock();
                if job.get("state").and_then(Value::as_str) == Some("downloading") {
                    let cur = Value::Object(job.clone());
                    return Some(json_response(StatusCode::OK, &cur, ctx.instance));
                }
                job.clear();
                job.insert("state".into(), Value::String("downloading".to_owned()));
            }
            let db_path = metadata_db_path(&ctx.app);
            let _ = ctx
                .app
                .jobs
                .submit("metadata", false, None, None, move |_ctx| {
                    let res = sync_database(&db_path, |phase, cur, tot| {
                        let mut job = METADATA_JOB.lock();
                        job.insert("phase".into(), Value::String(phase.to_owned()));
                        job.insert("bytes".into(), json!(cur));
                        job.insert("total".into(), json!(tot));
                    });
                    let mut job = METADATA_JOB.lock();
                    job.clear();
                    match res {
                        Ok(()) => {
                            job.insert("state".into(), Value::String("done".to_owned()));
                            job.insert("phase".into(), Value::String("done".to_owned()));
                            Ok(json!({"state": "done"}))
                        }
                        Err(err) => {
                            job.insert("state".into(), Value::String("error".to_owned()));
                            job.insert("error".into(), Value::String(err.clone()));
                            Err(err)
                        }
                    }
                });
            Some(json_response(
                StatusCode::ACCEPTED,
                &json!({"state": "downloading"}),
                ctx.instance,
            ))
        }
        "/api/v2/metadata/match" => {
            let db_path = metadata_db_path(&ctx.app);
            if !db_path.is_file() {
                return Some(api_error_response(
                    &ApiError::conflict("Download the metadata database first."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let platform = payload
                .get("platform")
                .and_then(Value::as_str)
                .unwrap_or("all")
                .to_owned();
            {
                let mut job = MATCH_JOB.lock();
                if job.get("state").and_then(Value::as_str) == Some("running") {
                    let cur = Value::Object(job.clone());
                    return Some(json_response(StatusCode::OK, &cur, ctx.instance));
                }
                job.clear();
                job.insert("state".into(), Value::String("running".to_owned()));
                job.insert("matched".into(), json!(0));
                job.insert("scanned".into(), json!(0));
                job.insert("errors".into(), json!([]));
            }
            let app = Arc::clone(&ctx.app);
            let _ = ctx
                .app
                .jobs
                .submit("metadata-match", false, None, None, move |_ctx| {
                    let state = app.load_state().map_err(|e| e.to_string())?;
                    let candidates: Vec<&Value> = state
                        .get("games")
                        .and_then(Value::as_array)
                        .map(|arr| {
                            arr.iter()
                                .filter(|g| {
                                    let no_id = g
                                        .get("launchbox_db_id")
                                        .is_none_or(|v| v.is_null() || v.as_str() == Some(""));
                                    let plat_ok = platform == "all"
                                        || g.get("platform").and_then(Value::as_str)
                                            == Some(platform.as_str());
                                    let has_name = g
                                        .get("name")
                                        .and_then(Value::as_str)
                                        .is_some_and(|s| !s.trim().is_empty());
                                    no_id && plat_ok && has_name
                                })
                                .collect()
                        })
                        .unwrap_or_default();
                    let scanned = candidates.len();
                    if scanned == 0 {
                        let mut job = MATCH_JOB.lock();
                        job.insert("state".into(), Value::String("done".to_owned()));
                        job.insert("scanned".into(), json!(0));
                        job.insert("matched".into(), json!(0));
                        return Ok(json!({"scanned": 0, "matched": 0}));
                    }
                    let titles: Vec<(String, String)> = candidates
                        .iter()
                        .map(|g| {
                            (
                                g["name"].as_str().unwrap_or("").trim().to_owned(),
                                g.get("platform")
                                    .and_then(Value::as_str)
                                    .unwrap_or("")
                                    .to_owned(),
                            )
                        })
                        .collect();
                    let matches = match batch_match(&db_path, &titles) {
                        Ok(m) => m,
                        Err(e) => {
                            let mut job = MATCH_JOB.lock();
                            job.insert("state".into(), Value::String("error".to_owned()));
                            job.insert("error".into(), Value::String(e.clone()));
                            return Err(e);
                        }
                    };
                    let mut matched_records: HashMap<String, String> = HashMap::new();
                    for g in &candidates {
                        let gid = g.get("game_id").and_then(Value::as_str).unwrap_or("");
                        let key = (
                            g["name"].as_str().unwrap_or("").trim().to_owned(),
                            g.get("platform")
                                .and_then(Value::as_str)
                                .unwrap_or("")
                                .to_owned(),
                        );
                        if let Some(rec) = matches.get(&key) {
                            matched_records.insert(gid.to_owned(), rec["database_id"].to_string());
                        }
                    }
                    let mut matched_count = 0usize;
                    let mut errors: Vec<String> = Vec::new();
                    if !matched_records.is_empty()
                        && let Err(e) = app.transact_state(|st| {
                            if let Some(games) = st.get_mut("games").and_then(Value::as_array_mut) {
                                for g in games.iter_mut() {
                                    if let Some(gid) = g.get("game_id").and_then(Value::as_str)
                                        && let Some(db_id) = matched_records.get(gid)
                                    {
                                        g["launchbox_db_id"] = Value::String(db_id.clone());
                                        matched_count += 1;
                                    }
                                }
                            }
                            Ok(())
                        })
                    {
                        errors.push(format!("Batch state update error: {e}"));
                    }
                    let mut job = MATCH_JOB.lock();
                    job.insert("state".into(), Value::String("done".to_owned()));
                    job.insert("scanned".into(), json!(scanned));
                    job.insert("matched".into(), json!(matched_count));
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
                    Ok(json!({"scanned": scanned, "matched": matched_count}))
                });
            Some(json_response(
                StatusCode::ACCEPTED,
                &json!({"state": "running"}),
                ctx.instance,
            ))
        }
        "/api/v2/metadata/apply" => {
            let db_path = metadata_db_path(&ctx.app);
            if !db_path.is_file() {
                return Some(api_error_response(
                    &ApiError::bad_request("Download the metadata database first."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let empty_arr = Vec::new();
            let Some(media_arr) = payload
                .get("media")
                .map(|v| v.as_array())
                .unwrap_or(Some(&empty_arr))
            else {
                return Some(api_error_response(
                    &ApiError::bad_request("Invalid media selection."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let mut media_types: Vec<String> = Vec::new();
            for m in media_arr {
                let s = m.as_str().unwrap_or("");
                if !MEDIA_TYPES_ALL.contains(&s) {
                    return Some(api_error_response(
                        &ApiError::bad_request("Invalid media selection."),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
                media_types.push(s.to_owned());
            }
            let Some(db_id) = payload.get("database_id").and_then(|v| match v {
                Value::Number(n) => n.as_i64(),
                Value::String(s) => s.trim().parse::<i64>().ok(),
                _ => None,
            }) else {
                return Some(api_error_response(
                    &ApiError::bad_request("database_id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let overwrite = bool_field(payload, "overwrite");
            let allowed_urls: HashSet<(String, String)> = lbdb_media_candidates(&db_path, db_id)
                .unwrap_or_default()
                .into_iter()
                .filter_map(|e| {
                    Some((
                        e.get("kind")?.as_str()?.to_owned(),
                        e.get("url")?.as_str()?.to_owned(),
                    ))
                })
                .collect();
            let exact_urls = clean_exact_media_urls(payload.get("media_urls"), &allowed_urls);
            media_types.retain(|k| !exact_urls.contains_key(k));

            let state = match ctx.app.load_state() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let idx = match find_game_index(&state, payload) {
                Ok(i) => i,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let original = state["games"][idx].as_object().cloned().unwrap_or_default();
            if media_types.iter().any(|k| k == "manual")
                && original
                    .get("path")
                    .and_then(Value::as_str)
                    .map(str::trim)
                    .is_none_or(str::is_empty)
            {
                return Some(api_error_response(
                    &ApiError::bad_request(
                        "This game has no file path, so no manual can be imported.",
                    ),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let gid = original
                .get("game_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            let reg_pri: Option<Vec<String>> = state
                .get("settings")
                .and_then(|s| s.get("region_priority"))
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .filter_map(Value::as_str)
                        .map(ToOwned::to_owned)
                        .collect()
                });
            let mut updated = original.clone();
            let lb_media_root = ctx.app.data_dir.join("media").join("launchbox");
            if let Err(e) = apply_game_metadata(
                &mut updated,
                &db_path,
                db_id,
                &media_types,
                &lb_media_root,
                overwrite,
                reg_pri.as_deref(),
            ) {
                return Some(api_error_response(
                    &ApiError::bad_request(e),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if !exact_urls.is_empty() {
                let item_root = lb_media_root.join(db_id.to_string());
                for (kind, url) in &exact_urls {
                    if !overwrite
                        && original
                            .get(kind)
                            .and_then(Value::as_str)
                            .is_some_and(|s| !s.is_empty())
                    {
                        continue;
                    }
                    let dest = item_root.join(format!("{kind}{}", ext_from_url(url, ".jpg")));
                    if let Ok(path) = download_bytes_sync(url, &dest) {
                        if kind == "screenshots" {
                            updated.insert(kind.clone(), json!([path]));
                        } else {
                            updated.insert(kind.clone(), Value::String(path));
                        }
                    }
                }
            }
            let notes: Vec<Value> = updated
                .remove("_media_notes")
                .and_then(|v| v.as_array().cloned())
                .unwrap_or_default();
            let mut changes = BTreeMap::new();
            for (k, v) in &updated {
                if original.get(k) != Some(v) {
                    changes.insert(k.clone(), v.clone());
                }
            }
            let updated_keys: Vec<String> = changes.keys().cloned().collect();
            if let Err(e) = ctx.app.transact_state(|st| {
                if let Some(games) = st.get_mut("games").and_then(Value::as_array_mut)
                    && let Some(g) = games
                        .iter_mut()
                        .find(|x| x.get("game_id").and_then(Value::as_str) == Some(&gid))
                    && let Some(m) = g.as_object_mut()
                {
                    for (k, v) in &changes {
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
                &json!({"updated": updated_keys, "notes": notes}),
                ctx.instance,
            ))
        }
        "/api/v2/metadata/igdb/apply" => {
            let Some(igdb_id) = payload.get("igdb_id").and_then(|v| match v {
                Value::Number(n) => n.as_i64(),
                Value::String(s) => s.trim().parse::<i64>().ok(),
                _ => None,
            }) else {
                return Some(api_error_response(
                    &ApiError::bad_request("igdb_id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let state = match ctx.app.load_state() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let idx = match find_game_index(&state, payload) {
                Ok(i) => i,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let gid = state["games"][idx]
                .get("game_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            let metadata = match fetch_igdb_game(igdb_id) {
                Ok(m) => m,
                Err(e) => {
                    return Some(api_error_response(
                        &ApiError::bad_request(e),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let mut game_name = String::new();
            if let Err(e) = ctx.app.transact_state(|st| {
                if let Some(games) = st.get_mut("games").and_then(Value::as_array_mut)
                    && let Some(g) = games
                        .iter_mut()
                        .find(|x| x.get("game_id").and_then(Value::as_str) == Some(&gid))
                    && let Some(m) = g.as_object_mut()
                {
                    apply_igdb_metadata(m, &metadata);
                    game_name = m
                        .get("name")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned();
                }
                Ok(())
            }) {
                return Some(state_error_response(&e, ctx.request_id, ctx.instance));
            }
            Some(json_response(
                StatusCode::OK,
                &json!({"applied": true, "game": game_name}),
                ctx.instance,
            ))
        }
        "/api/v2/metadata/candidates/apply" => {
            let Some(candidate) = payload.get("candidate").filter(|v| v.is_object()) else {
                return Some(api_error_response(
                    &ApiError::bad_request("candidate must be an object"),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let overwrite = bool_field(payload, "overwrite");
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
            if let Err(e) = apply_candidate(&mut target, candidate, overwrite, None) {
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
                    for key in APPLY_FIELDS {
                        if let Some(v) = target.get(*key) {
                            let empty = v.is_null()
                                || v.as_str() == Some("")
                                || v.as_array().is_some_and(Vec::is_empty);
                            if !empty {
                                let cur_empty = m
                                    .get(*key)
                                    .map(|x| {
                                        x.is_null()
                                            || x.as_str() == Some("")
                                            || x.as_array().is_some_and(Vec::is_empty)
                                    })
                                    .unwrap_or(true);
                                if overwrite || cur_empty {
                                    m.insert((*key).to_owned(), v.clone());
                                }
                            }
                        }
                    }
                    if let Some(prov) = target
                        .get("metadata_provider")
                        .and_then(Value::as_str)
                        .filter(|s| !s.is_empty())
                        && (overwrite
                            || m.get("metadata_provider")
                                .and_then(Value::as_str)
                                .is_none_or(str::is_empty))
                    {
                        m.insert("metadata_provider".into(), Value::String(prov.to_owned()));
                    }
                }
                Ok(())
            }) {
                return Some(state_error_response(&e, ctx.request_id, ctx.instance));
            }
            Some(json_response(
                StatusCode::OK,
                &json!({"ok": true, "game_id": gid}),
                ctx.instance,
            ))
        }
        "/api/v2/achievements/refresh" => {
            let game_id = str_field(payload, "game_id");
            let source = str_field(payload, "source");
            if game_id.is_empty() || source.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("game_id and source are required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if source == "retroachievements" {
                return Some(api_error_response(
                    &ApiError::bad_request("Use the RetroAchievements route to match a ROM."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let state = match ctx.app.load_state() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let idx = match find_game_index(&state, payload) {
                Ok(i) => i,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let game = &state["games"][idx];
            if source == "steam" {
                let app_id = game
                    .get("steam_app_id")
                    .map(|v| match v {
                        Value::String(s) => s.trim().to_owned(),
                        Value::Number(n) => n.to_string(),
                        _ => String::new(),
                    })
                    .unwrap_or_default();
                if app_id.is_empty() {
                    return Some(api_error_response(
                        &ApiError::bad_request("This game has no Steam app id."),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
                let creds = load_steam_credentials(&ctx.app.data_dir);
                if creds.api_key.is_empty() || creds.steam_id.is_empty() {
                    return Some(api_error_response(
                        &ApiError::bad_request("Configure the Steam API key and Steam ID first."),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
                if let Err(e) = get_achievements(
                    &ctx.app.data_dir,
                    &app_id,
                    &creds.steam_id,
                    &creds.api_key,
                    false,
                ) {
                    return Some(api_error_response(
                        &ApiError::bad_request(e),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            } else if source.starts_with("plugin:") {
                // Plugin achievement refresh is wired in Phase 9.
                let parts: Vec<&str> = source.splitn(3, ':').collect();
                if parts.len() != 3 || parts[1].is_empty() || parts[2].is_empty() {
                    return Some(api_error_response(
                        &ApiError::bad_request(
                            "Plugin achievement sources must be plugin:<id>:<source>.",
                        ),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            } else {
                return Some(api_error_response(
                    &ApiError::bad_request("Unknown achievement source."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            ctx.app.invalidate_caches();
            Some(json_response(
                StatusCode::OK,
                &json!({"ok": true, "game_id": game_id, "source": source}),
                ctx.instance,
            ))
        }
        "/api/v2/achievements/override" => {
            let game_id = str_field(payload, "game_id").to_owned();
            let source = str_field(payload, "source").to_owned();
            if game_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("game_id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if !matches!(source.as_str(), "retroachievements" | "steam" | "gog")
                && !source.starts_with("plugin:")
            {
                return Some(api_error_response(
                    &ApiError::bad_request("Unknown achievement source."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if bool_field(payload, "clear") {
                if let Err(e) = ctx.app.transact_state(|st| {
                    if let Some(ov) = st
                        .get_mut("achievement_overrides")
                        .and_then(Value::as_object_mut)
                        && let Some(pg) = ov.get_mut(&game_id).and_then(Value::as_object_mut)
                    {
                        pg.remove(&source);
                        if pg.is_empty() {
                            ov.remove(&game_id);
                        }
                    }
                    Ok(())
                }) {
                    return Some(state_error_response(&e, ctx.request_id, ctx.instance));
                }
                return Some(json_response(
                    StatusCode::OK,
                    &json!({"ok": true, "cleared": true}),
                    ctx.instance,
                ));
            }
            let parse_int = |k: &str| match payload.get(k) {
                None | Some(Value::Null) => Ok(0i64),
                Some(Value::Number(n)) => n.as_i64().ok_or(()),
                Some(Value::String(s)) => s.trim().parse::<i64>().map_err(|_| ()),
                _ => Err(()),
            };
            let (Ok(earned), Ok(total)) = (parse_int("earned"), parse_int("total")) else {
                return Some(api_error_response(
                    &ApiError::bad_request("earned and total must be whole numbers."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            if earned < 0 || total < 0 || earned > total || total > 10000 {
                return Some(api_error_response(
                    &ApiError::bad_request(
                        "Achievement totals must satisfy 0 <= earned <= total <= 10000.",
                    ),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let mastered = payload
                .get("mastered")
                .and_then(Value::as_bool)
                .unwrap_or(total > 0 && earned >= total);
            let mut not_found = false;
            let tx = ctx.app.transact_state(|st| {
                let mut probe = Map::new();
                probe.insert("game_id".into(), Value::String(game_id.clone()));
                if find_game_index(st, &probe).is_err() {
                    not_found = true;
                    return Err(openbox_store::StateError::Corrupt("not found".into()));
                }
                let root = st.as_object_mut().unwrap();
                let ov = root
                    .entry("achievement_overrides".to_owned())
                    .or_insert_with(|| Value::Object(Map::new()));
                if !ov.is_object() {
                    *ov = Value::Object(Map::new());
                }
                let pg = ov
                    .as_object_mut()
                    .unwrap()
                    .entry(game_id.clone())
                    .or_insert_with(|| Value::Object(Map::new()));
                if !pg.is_object() {
                    *pg = Value::Object(Map::new());
                }
                pg.as_object_mut().unwrap().insert(
                    source.clone(),
                    json!({
                        "earned": earned,
                        "total": total,
                        "mastered": mastered,
                        "manual": true,
                    }),
                );
                Ok(())
            });
            if not_found {
                return Some(api_error_response(
                    &ApiError::game_not_found("Game not found."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if let Err(e) = tx {
                return Some(state_error_response(&e, ctx.request_id, ctx.instance));
            }
            Some(json_response(
                StatusCode::OK,
                &json!({
                    "source": source,
                    "earned": earned,
                    "total": total,
                    "mastered": mastered,
                    "manual": true,
                }),
                ctx.instance,
            ))
        }
        "/api/v2/metadata/matches/preview" => {
            let db_path = metadata_db_path(&ctx.app);
            if !db_path.is_file() {
                return Some(api_error_response(
                    &ApiError::conflict("Download the metadata database first."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let game_ids: Option<Vec<String>> = match payload.get("game_ids") {
                None | Some(Value::Null) => None,
                Some(Value::Array(a)) => Some(
                    a.iter()
                        .filter_map(|v| v.as_str().map(ToOwned::to_owned))
                        .collect(),
                ),
                _ => {
                    return Some(api_error_response(
                        &ApiError::bad_request("game_ids must be an array or null."),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let batch_id = str_field(payload, "import_batch_id");
            let preview = match create_match_preview_record(
                &ctx.app.data_dir,
                game_ids,
                if batch_id.is_empty() {
                    None
                } else {
                    Some(batch_id)
                },
            ) {
                Ok(p) => p,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let pid = preview["preview_id"].as_str().unwrap_or("").to_owned();
            let app = Arc::clone(&ctx.app);
            let pid_c = pid.clone();
            let target = json!({"preview_id": pid});
            let job = match ctx.app.jobs.submit(
                &format!("metadata-match-preview:{pid}"),
                true,
                Some("metadata.match_preview"),
                Some(&target),
                move |jctx| {
                    let st = app.load_state().map_err(|e| e.to_string())?;
                    run_match_preview_job(
                        &app.data_dir,
                        &db_path,
                        &pid_c,
                        &st,
                        Some(jctx.cancel_flag()),
                        |gid, aid| {
                            let gid_s = gid.to_owned();
                            let aid_s = aid.to_owned();
                            let _ = app.transact_state(|cur| {
                                if let Some(games) =
                                    cur.get_mut("games").and_then(Value::as_array_mut)
                                    && let Some(g) = games.iter_mut().find(|x| {
                                        x.get("game_id").and_then(Value::as_str) == Some(&gid_s)
                                    })
                                    && g.get("launchbox_db_id")
                                        .is_none_or(|v| v.is_null() || v.as_str() == Some(""))
                                {
                                    g["launchbox_db_id"] = Value::String(aid_s);
                                }
                                Ok(())
                            });
                        },
                    )
                    .map_err(|e| e.message)
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
            let job_id = job
                .get("job_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            if let Ok(mut reloaded) = load_match_preview(&ctx.app.data_dir, &pid, true) {
                reloaded["job_id"] = Value::String(job_id.clone());
                reloaded["state"] = Value::String("queued".to_owned());
                let _ = save_match_preview(&ctx.app.data_dir, &reloaded);
            }
            let st = preview_state_for_job(ctx, Some(&job_id));
            Some(json_response(
                StatusCode::ACCEPTED,
                &json!({
                    "preview_id": pid,
                    "revision": preview["revision"],
                    "job_id": job_id,
                    "state": st,
                }),
                ctx.instance,
            ))
        }
        "/api/v2/metadata/matches/decisions" => {
            let pid = str_field(payload, "preview_id");
            if pid.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("preview_id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let Some(items) = payload
                .get("items")
                .and_then(Value::as_array)
                .filter(|a| !a.is_empty())
            else {
                return Some(api_error_response(
                    &ApiError::bad_request("items must be a non-empty array."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let (mut result, rejections) =
                match apply_match_decisions(&ctx.app.data_dir, pid, items) {
                    Ok(pair) => pair,
                    Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
                };
            if !rejections.is_empty() {
                let _ = ctx.app.transact_state(|st| {
                    if let Some(games) = st.get_mut("games").and_then(Value::as_array_mut) {
                        for (gid, db_id) in &rejections {
                            if let Some(g) = games.iter_mut().find(|x| {
                                x.get("game_id").and_then(Value::as_str) == Some(gid.as_str())
                            }) && let Some(gm) = g.as_object_mut()
                            {
                                let mut existing: Vec<String> = gm
                                    .get("rejected_launchbox_ids")
                                    .and_then(Value::as_array)
                                    .map(|a| {
                                        a.iter()
                                            .filter_map(|v| v.as_str().map(ToOwned::to_owned))
                                            .collect()
                                    })
                                    .unwrap_or_default();
                                if !existing.contains(db_id) {
                                    existing.push(db_id.clone());
                                }
                                if existing.len() > MAX_REJECTED_IDS {
                                    let drop_n = existing.len() - MAX_REJECTED_IDS;
                                    existing.drain(..drop_n);
                                }
                                gm.insert(
                                    "rejected_launchbox_ids".into(),
                                    Value::Array(existing.into_iter().map(Value::String).collect()),
                                );
                            }
                        }
                    }
                    Ok(())
                });
            }
            if let Some(m) = result.as_object_mut() {
                m.insert("preview_id".into(), Value::String(pid.to_owned()));
            }
            Some(json_response(StatusCode::OK, &result, ctx.instance))
        }
        "/api/v2/metadata/matches/apply" => {
            let db_path = metadata_db_path(&ctx.app);
            if !db_path.is_file() {
                return Some(api_error_response(
                    &ApiError::conflict("Download the metadata database first."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let pid = str_field(payload, "preview_id").to_owned();
            if pid.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("preview_id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let Some(revision) = payload.get("revision").and_then(Value::as_i64) else {
                return Some(api_error_response(
                    &ApiError::bad_request("revision is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let game_ids: Option<HashSet<String>> = match payload.get("game_ids") {
                None | Some(Value::Null) => None,
                Some(Value::Array(a)) => Some(
                    a.iter()
                        .filter_map(|v| v.as_str().map(ToOwned::to_owned))
                        .collect(),
                ),
                _ => {
                    return Some(api_error_response(
                        &ApiError::bad_request("game_ids must be an array or null."),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let field_allow_list: Option<Vec<String>> = payload
                .get("field_allow_list")
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str().map(ToOwned::to_owned))
                        .collect()
                });
            let media_allow = payload
                .get("media_allow_list")
                .and_then(Value::as_array)
                .is_some_and(|a| !a.is_empty());
            if media_allow {
                return Some(api_error_response(
                    &ApiError::bad_request(
                        "media_allow_list is not supported in metadata apply; use /api/media/bulk.",
                    ),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let replace_existing = bool_field(payload, "replace_existing");
            if replace_existing
                && field_allow_list.as_ref().is_none_or(Vec::is_empty)
                && !media_allow
            {
                return Some(api_error_response(
                    &ApiError::bad_request("replace_existing requires a non-empty allow-list."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if let Err(e) = load_match_preview(&ctx.app.data_dir, &pid, false) {
                return Some(api_error_response(&e, ctx.request_id, ctx.instance));
            }
            let app = Arc::clone(&ctx.app);
            let pid_c = pid.clone();
            let target = json!({"preview_id": pid, "revision": revision});
            let job = match ctx.app.jobs.submit(
                &format!("metadata-match-apply:{pid}"),
                true,
                Some("metadata.apply"),
                Some(&target),
                move |jctx| {
                    let preview =
                        load_match_preview(&app.data_dir, &pid_c, false).map_err(|e| e.message)?;
                    if preview.get("revision").and_then(Value::as_i64).unwrap_or(0) != revision {
                        return Err("Preview revision is stale.".to_owned());
                    }
                    let decisions = preview
                        .get("decisions")
                        .and_then(Value::as_object)
                        .cloned()
                        .unwrap_or_default();
                    for (gid, dec) in decisions {
                        if jctx.is_cancelled() {
                            break;
                        }
                        if let Some(ref w) = game_ids
                            && !w.contains(&gid)
                        {
                            continue;
                        }
                        let act = dec.get("action").and_then(Value::as_str).unwrap_or("");
                        let Some(db_id_s) = dec
                            .get("database_id")
                            .and_then(Value::as_str)
                            .filter(|s| !s.is_empty())
                        else {
                            continue;
                        };
                        if !matches!(act, "accept" | "choose") {
                            continue;
                        }
                        let Ok(db_id) = db_id_s.parse::<i64>() else {
                            continue;
                        };
                        let record = load_metadata_record(&db_path, db_id)?;
                        let gid_c = gid.clone();
                        let db_id_owned = db_id_s.to_owned();
                        let allow_c = field_allow_list.clone();
                        let _ = app.transact_state(|st| {
                            if let Some(games) = st.get_mut("games").and_then(Value::as_array_mut)
                                && let Some(g) = games.iter_mut().find(|x| {
                                    x.get("game_id").and_then(Value::as_str) == Some(&gid_c)
                                })
                                && let Some(gm) = g.as_object_mut()
                            {
                                let _ = apply_preview_fields(
                                    gm,
                                    &record,
                                    &db_id_owned,
                                    allow_c.as_deref(),
                                    replace_existing,
                                );
                            }
                            Ok(())
                        });
                    }
                    Ok(preview)
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
                &json!({
                    "job_id": job.get("job_id").cloned().unwrap_or(Value::Null),
                    "preview_id": pid,
                    "revision": revision,
                }),
                ctx.instance,
            ))
        }
        "/api/v2/metadata/auto-scrape" => {
            let batch = str_field(payload, "import_batch_id").to_owned();
            if batch.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("import_batch_id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let state = match ctx.app.load_state() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let scrape_cfg = scrape_settings_obj(&state);
            if !scrape_cfg["scrape_after_import"].as_bool().unwrap_or(true) {
                return Some(json_response(
                    StatusCode::OK,
                    &json!({"queued": false, "reason": "scrape_after_import is disabled"}),
                    ctx.instance,
                ));
            }
            let media_types: Vec<String> = match payload.get("media_types") {
                Some(Value::Array(a)) => a
                    .iter()
                    .filter_map(|v| v.as_str().map(ToOwned::to_owned))
                    .collect(),
                _ => state
                    .get("settings")
                    .and_then(|s| s.get("auto_import_media_types"))
                    .and_then(Value::as_array)
                    .map(|a| {
                        a.iter()
                            .filter_map(|v| v.as_str().map(ToOwned::to_owned))
                            .collect()
                    })
                    .filter(|v: &Vec<String>| !v.is_empty())
                    .unwrap_or_else(|| {
                        AUTO_SCRAPE_DEFAULT_MEDIA
                            .iter()
                            .map(|s| (*s).to_owned())
                            .collect()
                    }),
            };
            if !media_types
                .iter()
                .all(|m| MEDIA_TYPES_ALL.contains(&m.as_str()))
            {
                return Some(api_error_response(
                    &ApiError::bad_request("Invalid media type selection."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let overwrite = bool_field(payload, "overwrite");
            let db_path = metadata_db_path(&ctx.app);
            let preview_id = if db_path.is_file() {
                create_match_preview_record(&ctx.app.data_dir, None, Some(&batch))
                    .ok()
                    .and_then(|p| p["preview_id"].as_str().map(ToOwned::to_owned))
            } else {
                None
            };

            let app1 = Arc::clone(&ctx.app);
            let batch1 = batch.clone();
            let pid1 = preview_id.clone();
            let db1 = db_path.clone();
            let match_target = json!({"import_batch_id": batch, "preview_id": preview_id});
            let match_job = match ctx.app.jobs.submit(
                &format!("metadata-match:{batch}"),
                true,
                Some("metadata.match_auto"),
                Some(&match_target),
                move |jctx| {
                    let mut summary = json!({"batch": batch1, "preview_id": pid1});
                    if db1.is_file()
                        && let Some(ref pid) = pid1
                    {
                        if let Ok(st) = app1.load_state()
                            && let Ok(prev) = run_match_preview_job(
                                &app1.data_dir,
                                &db1,
                                pid,
                                &st,
                                Some(jctx.cancel_flag()),
                                |gid, aid| {
                                    let gid_s = gid.to_owned();
                                    let aid_s = aid.to_owned();
                                    let _ = app1.transact_state(|cur| {
                                        if let Some(games) =
                                            cur.get_mut("games").and_then(Value::as_array_mut)
                                            && let Some(g) = games.iter_mut().find(|x| {
                                                x.get("game_id").and_then(Value::as_str)
                                                    == Some(&gid_s)
                                            })
                                            && g.get("launchbox_db_id").is_none_or(|v| {
                                                v.is_null() || v.as_str() == Some("")
                                            })
                                        {
                                            g["launchbox_db_id"] = Value::String(aid_s);
                                        }
                                        Ok(())
                                    });
                                },
                            )
                        {
                            summary["lbdb"] = json!({
                                "counts": prev["counts"],
                                "auto_applied": prev["auto_applied"],
                            });
                        }
                    } else {
                        summary["lbdb"] = json!({"skipped": "metadata database not downloaded"});
                    }
                    // Opt-in ScreenScraper dual-hash pass
                    let cfg = app1
                        .load_state()
                        .map(|s| scrape_settings_obj(&s))
                        .unwrap_or_else(|_| json!({}));
                    if cfg["scrape_screenscraper_enabled"]
                        .as_bool()
                        .unwrap_or(false)
                        && ss_configured()
                    {
                        let mut matched = 0usize;
                        let mut errors = Vec::new();
                        if let Ok(st) = app1.load_state() {
                            let unmatched: Vec<Value> = st
                                .get("games")
                                .and_then(Value::as_array)
                                .map(|a| {
                                    a.iter()
                                        .filter(|g| {
                                            g.get("import_batch_id").and_then(Value::as_str)
                                                == Some(batch1.as_str())
                                                && g.get("launchbox_db_id").is_none_or(|v| {
                                                    v.is_null() || v.as_str() == Some("")
                                                })
                                                && g.get("igdb_id").is_none_or(Value::is_null)
                                                && g.get("screenscraper_id")
                                                    .is_none_or(Value::is_null)
                                                && g.get("path")
                                                    .and_then(Value::as_str)
                                                    .is_some_and(|p| Path::new(p).is_file())
                                        })
                                        .take(AUTO_SCRAPE_SS_BUDGET)
                                        .cloned()
                                        .collect()
                                })
                                .unwrap_or_default();
                            for g in unmatched {
                                if jctx.is_cancelled() {
                                    break;
                                }
                                let p = g["path"].as_str().unwrap_or("");
                                let sid = system_id_for_platform(
                                    g.get("platform").and_then(Value::as_str).unwrap_or(""),
                                );
                                match confident_hash_match(
                                    Path::new(p),
                                    sid,
                                    Some(&app1.data_dir.join("cache")),
                                ) {
                                    Ok((meta, tier)) if tier == HASH_TIER_DUAL => {
                                        let gid = g["game_id"].as_str().unwrap_or("").to_owned();
                                        let flds: Vec<String> = [
                                            "name",
                                            "description",
                                            "year",
                                            "genre",
                                            "developer",
                                            "publisher",
                                        ]
                                        .iter()
                                        .map(|s| (*s).to_owned())
                                        .collect();
                                        if app1
                                            .transact_state(|cur| {
                                                if let Some(games) = cur
                                                    .get_mut("games")
                                                    .and_then(Value::as_array_mut)
                                                    && let Some(t) = games.iter_mut().find(|x| {
                                                        x.get("game_id").and_then(Value::as_str)
                                                            == Some(&gid)
                                                    })
                                                    && let Some(m) = t.as_object_mut()
                                                {
                                                    apply_ss_metadata(m, &meta, &flds);
                                                    m.insert(
                                                        "matched_by".into(),
                                                        Value::String("hash".to_owned()),
                                                    );
                                                    m.insert(
                                                        "match_confidence".into(),
                                                        Value::String(HASH_TIER_DUAL.to_owned()),
                                                    );
                                                }
                                                Ok(())
                                            })
                                            .is_ok()
                                        {
                                            matched += 1;
                                        }
                                    }
                                    Ok(_) => {}
                                    Err(e) => errors.push(e),
                                }
                            }
                        }
                        summary["screenscraper"] = json!({"matched": matched, "errors": errors});
                    } else {
                        summary["screenscraper"] = json!({"skipped": true});
                    }
                    // Opt-in IGDB exact-title pass
                    if cfg["scrape_igdb_enabled"].as_bool().unwrap_or(false) && igdb_configured() {
                        let mut matched = 0usize;
                        let mut errors = Vec::new();
                        if let Ok(st) = app1.load_state() {
                            let unmatched: Vec<Value> = st
                                .get("games")
                                .and_then(Value::as_array)
                                .map(|a| {
                                    a.iter()
                                        .filter(|g| {
                                            g.get("import_batch_id").and_then(Value::as_str)
                                                == Some(batch1.as_str())
                                                && g.get("launchbox_db_id").is_none_or(|v| {
                                                    v.is_null() || v.as_str() == Some("")
                                                })
                                                && g.get("igdb_id").is_none_or(Value::is_null)
                                                && g.get("screenscraper_id")
                                                    .is_none_or(Value::is_null)
                                        })
                                        .take(AUTO_SCRAPE_IGDB_BUDGET)
                                        .cloned()
                                        .collect()
                                })
                                .unwrap_or_default();
                            for g in unmatched {
                                if jctx.is_cancelled() {
                                    break;
                                }
                                let name =
                                    g.get("name").and_then(Value::as_str).unwrap_or("").trim();
                                if name.is_empty() {
                                    continue;
                                }
                                let plat = g.get("platform").and_then(Value::as_str).unwrap_or("");
                                let wanted = NON_ALNUM_RE
                                    .replace_all(&name.to_lowercase(), "")
                                    .into_owned();
                                match search_igdb_games(name, plat, 5) {
                                    Ok(cands) => {
                                        let hit = cands.into_iter().find(|c| {
                                            let cn = c
                                                .get("name")
                                                .and_then(Value::as_str)
                                                .unwrap_or("")
                                                .to_lowercase();
                                            NON_ALNUM_RE.replace_all(&cn, "") == wanted
                                        });
                                        if let Some(top) = hit
                                            && let Some(id) = top.get("id").and_then(Value::as_i64)
                                            && let Ok(meta) = fetch_igdb_game(id)
                                        {
                                            let gid =
                                                g["game_id"].as_str().unwrap_or("").to_owned();
                                            if app1
                                                .transact_state(|cur| {
                                                    if let Some(games) = cur
                                                        .get_mut("games")
                                                        .and_then(Value::as_array_mut)
                                                        && let Some(t) =
                                                            games.iter_mut().find(|x| {
                                                                x.get("game_id")
                                                                    .and_then(Value::as_str)
                                                                    == Some(&gid)
                                                            })
                                                        && let Some(m) = t.as_object_mut()
                                                    {
                                                        apply_igdb_metadata(m, &meta);
                                                        m.insert(
                                                            "matched_by".into(),
                                                            Value::String("igdb".to_owned()),
                                                        );
                                                        m.insert(
                                                            "match_confidence".into(),
                                                            Value::String("exact_title".to_owned()),
                                                        );
                                                    }
                                                    Ok(())
                                                })
                                                .is_ok()
                                            {
                                                matched += 1;
                                            }
                                        }
                                    }
                                    Err(e) => errors.push(e),
                                }
                            }
                        }
                        summary["igdb"] = json!({"matched": matched, "errors": errors});
                    } else {
                        summary["igdb"] = json!({"skipped": true});
                    }
                    Ok(summary)
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

            let app2 = Arc::clone(&ctx.app);
            let batch2 = batch.clone();
            let mt2 = media_types.clone();
            let media_target = json!({"import_batch_id": batch, "media_types": media_types});
            let media_job = match ctx.app.jobs.submit(
                &format!("metadata-media:{batch}"),
                true,
                Some("metadata.media_auto"),
                Some(&media_target),
                move |jctx| {
                    let mut summary = json!({"batch": batch2, "media_types": mt2});
                    let st = app2.load_state().map_err(|e| e.to_string())?;
                    let targets: Vec<Value> = st
                        .get("games")
                        .and_then(Value::as_array)
                        .map(|a| {
                            a.iter()
                                .filter(|g| {
                                    g.get("import_batch_id").and_then(Value::as_str)
                                        == Some(batch2.as_str())
                                        && g.get("launchbox_db_id")
                                            .is_some_and(|v| !v.is_null() && v.as_str() != Some(""))
                                })
                                .cloned()
                                .collect()
                        })
                        .unwrap_or_default();
                    summary["targets"] = json!(targets.len());
                    if db_path.is_file() {
                        let lb_root = app2.data_dir.join("media").join("launchbox");
                        let mut changes_by_id: HashMap<String, Map<String, Value>> = HashMap::new();
                        let mut errors = Vec::new();
                        for g in &targets {
                            if jctx.is_cancelled() {
                                break;
                            }
                            let gid = g["game_id"].as_str().unwrap_or("").to_owned();
                            let Some(db_id) = g.get("launchbox_db_id").and_then(|v| match v {
                                Value::Number(n) => n.as_i64(),
                                Value::String(s) => s.trim().parse::<i64>().ok(),
                                _ => None,
                            }) else {
                                continue;
                            };
                            let orig = g.as_object().cloned().unwrap_or_default();
                            let mut upd = orig.clone();
                            match apply_game_metadata(
                                &mut upd, &db_path, db_id, &mt2, &lb_root, overwrite, None,
                            ) {
                                Ok(()) => {
                                    upd.remove("_media_notes");
                                    let mut ch = Map::new();
                                    for (k, v) in upd {
                                        if orig.get(&k) != Some(&v) {
                                            ch.insert(k, v);
                                        }
                                    }
                                    if !ch.is_empty() {
                                        changes_by_id.insert(gid, ch);
                                    }
                                }
                                Err(e) => errors.push(e),
                            }
                        }
                        if !changes_by_id.is_empty() {
                            let _ = app2.transact_state(|cur| {
                                if let Some(games) =
                                    cur.get_mut("games").and_then(Value::as_array_mut)
                                {
                                    for g in games.iter_mut() {
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
                            });
                            app2.bump_media_epoch();
                        }
                        summary["lbdb"] = json!({"updated": changes_by_id.len(), "errors": errors});
                    } else {
                        summary["lbdb"] = json!({"skipped": "metadata database not downloaded"});
                    }
                    // Opt-in SteamGridDB fill
                    let cfg = scrape_settings_obj(&st);
                    if cfg["scrape_steamgrid_enabled"].as_bool().unwrap_or(false)
                        && sgdb_configured()
                    {
                        let kinds: Vec<String> = AUTO_SCRAPE_SGDB_KINDS
                            .iter()
                            .filter(|k| mt2.iter().any(|m| m == **k))
                            .map(|s| (*s).to_owned())
                            .collect();
                        let mut filled = 0usize;
                        let mut errors = Vec::new();
                        if !kinds.is_empty() {
                            let sg_targets: Vec<Value> = st
                                .get("games")
                                .and_then(Value::as_array)
                                .map(|a| {
                                    a.iter()
                                        .filter(|g| {
                                            g.get("import_batch_id").and_then(Value::as_str)
                                                == Some(batch2.as_str())
                                                && (g.get("launchbox_db_id").is_some_and(|v| {
                                                    !v.is_null() && v.as_str() != Some("")
                                                }) || g
                                                    .get("igdb_id")
                                                    .is_some_and(|v| !v.is_null())
                                                    || g.get("screenscraper_id")
                                                        .is_some_and(|v| !v.is_null()))
                                                && kinds.iter().any(|k| {
                                                    g.get(k)
                                                        .and_then(Value::as_str)
                                                        .is_none_or(str::is_empty)
                                                })
                                        })
                                        .take(AUTO_SCRAPE_SGDB_BUDGET)
                                        .cloned()
                                        .collect()
                                })
                                .unwrap_or_default();
                            let cache_dir = app2.data_dir.join("cache");
                            for g in sg_targets {
                                if jctx.is_cancelled() {
                                    break;
                                }
                                let name = g.get("name").and_then(Value::as_str).unwrap_or("");
                                let gid = g["game_id"].as_str().unwrap_or("").to_owned();
                                let missing: Vec<String> = kinds
                                    .iter()
                                    .filter(|k| {
                                        g.get(*k).and_then(Value::as_str).is_none_or(str::is_empty)
                                    })
                                    .cloned()
                                    .collect();
                                if let Ok(found) = search_sgdb_games(name, 1, Some(&cache_dir))
                                    && let Some(first) = found.first()
                                    && let Some(sgid) = first.get("id").and_then(Value::as_i64)
                                    && let Ok(meta) = sgdb_game_info(sgid, Some(&cache_dir))
                                {
                                    let urls = choose_sgdb_media(&meta, &missing);
                                    let slug: String = name
                                        .chars()
                                        .filter(|c| {
                                            c.is_alphanumeric()
                                                || *c == '-'
                                                || *c == '_'
                                                || *c == ' '
                                        })
                                        .collect::<String>()
                                        .trim()
                                        .replace(' ', "-")
                                        .chars()
                                        .take(60)
                                        .collect();
                                    let mroot = app2
                                        .data_dir
                                        .join("media")
                                        .join("steamgrid")
                                        .join(if slug.is_empty() { &gid } else { &slug });
                                    let mut downloaded = Map::new();
                                    for (k, u) in urls {
                                        let cu = clean_sgdb_url(&u);
                                        if cu.is_empty() {
                                            continue;
                                        }
                                        let dest =
                                            mroot.join(format!("{k}{}", ext_from_url(&cu, ".jpg")));
                                        if let Ok(p) = download_bytes_sync(&cu, &dest) {
                                            downloaded.insert(k, Value::String(p));
                                        }
                                    }
                                    if !downloaded.is_empty() {
                                        let dl = downloaded.clone();
                                        let gid_c = gid.clone();
                                        if app2
                                            .transact_state(|cur| {
                                                if let Some(games) = cur
                                                    .get_mut("games")
                                                    .and_then(Value::as_array_mut)
                                                    && let Some(t) = games.iter_mut().find(|x| {
                                                        x.get("game_id").and_then(Value::as_str)
                                                            == Some(&gid_c)
                                                    })
                                                    && let Some(m) = t.as_object_mut()
                                                {
                                                    for (k, v) in &dl {
                                                        m.insert(k.clone(), v.clone());
                                                    }
                                                }
                                                Ok(())
                                            })
                                            .is_ok()
                                        {
                                            filled += 1;
                                        }
                                    }
                                } else {
                                    errors.push(format!("{name}: lookup failed"));
                                }
                            }
                            if filled > 0 {
                                app2.bump_media_epoch();
                            }
                        }
                        summary["steamgrid"] = json!({"filled": filled, "errors": errors});
                    } else {
                        summary["steamgrid"] = json!({"skipped": true});
                    }
                    Ok(summary)
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
                &json!({
                    "queued": true,
                    "import_batch_id": batch,
                    "match_job_id": match_job.get("job_id").cloned().unwrap_or(Value::Null),
                    "media_job_id": media_job.get("job_id").cloned().unwrap_or(Value::Null),
                    "preview_id": preview_id,
                }),
                ctx.instance,
            ))
        }
        "/api/v2/metadata/scrape-settings" => {
            let mut updates = Map::new();
            for &(k, default) in SCRAPE_SETTING_KEYS {
                if let Some(v) = payload.get(k) {
                    updates.insert(k.to_owned(), Value::Bool(v.as_bool().unwrap_or(default)));
                }
            }
            if let Err(e) = ctx.app.transact_state(|st| {
                let settings = st
                    .as_object_mut()
                    .unwrap()
                    .entry("settings".to_owned())
                    .or_insert_with(|| Value::Object(Map::new()));
                if let Some(sm) = settings.as_object_mut() {
                    for (k, v) in &updates {
                        sm.insert(k.clone(), v.clone());
                    }
                }
                Ok(())
            }) {
                return Some(state_error_response(&e, ctx.request_id, ctx.instance));
            }
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            Some(json_response(
                StatusCode::OK,
                &scrape_settings_obj(&state),
                ctx.instance,
            ))
        }
        _ => None,
    }
}
