//! SteamGridDB & Artwork Doctor v2 handlers (`handlers/steamgrid.py`).

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::{Arc, LazyLock};

use axum::http::StatusCode;
use axum::response::Response;
use openbox_core::ApiError;
use openbox_metadata::artwork_hygiene::{
    PROVIDER_ATTRIBUTION, build_report, list_undo_batches, load_undo_manifest, select_fixable,
    snapshot_for_replacement, undo_batch, write_undo_manifest,
};
use openbox_metadata::http::{download_bytes_sync, ext_from_url};
use openbox_metadata::steamgrid::{
    apply_to_game, choose_media, clean_media_url, enabled as sgdb_enabled, game_assets, game_info,
    is_configured, provider_available, provider_status, search_games,
};
use regex::Regex;
use serde_json::{Map, Value, json};
use sha2::{Digest as _, Sha256};
use uuid::Uuid;

use crate::handlers::projection::find_game_index_in_state as find_game_index;
use crate::handlers::shared::{RequestCtx, bool_field, str_field};
use crate::middleware::{api_error_response, json_response, state_error_response};

const MATCH_BATCH_LIMIT: usize = 100;
const HYGIENE_BATCH_LIMIT: usize = 200;
const DEFAULT_MEDIA_KINDS: &[&str] = &["cover", "background", "clear_logo", "icon", "banner"];

static EXACT_URL_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)^https?://\S+$").unwrap());

fn require_available(settings: &Value) -> Result<(), ApiError> {
    if provider_available(settings) {
        return Ok(());
    }
    if !sgdb_enabled(settings) {
        return Err(
            ApiError::bad_request("SteamGridDB provider is disabled in Settings.")
                .with_code("STEAMGRID_DISABLED"),
        );
    }
    if !is_configured() {
        return Err(
            ApiError::bad_request("Set STEAMGRIDDB_API_KEY in ~/.env to use SteamGridDB.")
                .with_code("STEAMGRID_UNCONFIGURED"),
        );
    }
    Err(
        ApiError::bad_request("SteamGridDB rejected the API key; provider disabled until restart.")
            .with_code("STEAMGRID_KEY_REJECTED"),
    )
}

fn media_kinds_from(payload: &Map<String, Value>) -> Vec<String> {
    if let Some(arr) = payload
        .get("media")
        .and_then(Value::as_array)
        .filter(|a| !a.is_empty())
    {
        return arr
            .iter()
            .filter_map(|v| v.as_str().map(ToOwned::to_owned))
            .collect();
    }
    DEFAULT_MEDIA_KINDS
        .iter()
        .map(|s| (*s).to_owned())
        .collect()
}

fn media_root_for(data_dir: &Path, game: &Value, fallback: &str) -> PathBuf {
    let stable_id = game
        .get("game_id")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .unwrap_or(fallback);
    let digest = hex::encode(Sha256::digest(stable_id.as_bytes()));
    let short_digest = &digest[..12];
    let name = game.get("name").and_then(Value::as_str).unwrap_or("");
    let slug: String = name
        .chars()
        .filter(|c| c.is_alphanumeric() || *c == '-' || *c == '_' || *c == ' ')
        .collect::<String>()
        .trim()
        .replace(' ', "-")
        .chars()
        .take(60)
        .collect();
    let label = if slug.is_empty() { stable_id } else { &slug };
    data_dir
        .join("media")
        .join("steamgrid")
        .join(format!("{label}-{short_digest}"))
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
            if DEFAULT_MEDIA_KINDS.contains(&kind)
                && EXACT_URL_RE.is_match(url)
                && allowed.contains(&(kind.to_owned(), url.to_owned()))
            {
                out.insert(kind.to_owned(), url.to_owned());
            }
        }
    }
    out
}

fn download_sgdb_media(
    media_urls: &HashMap<String, String>,
    original: &Value,
    media_root: &Path,
    replace_existing: bool,
) -> Map<String, Value> {
    let mut downloaded = Map::new();
    for (kind, url) in media_urls {
        let cu = clean_media_url(url);
        if cu.is_empty() {
            continue;
        }
        if !replace_existing
            && original
                .get(kind)
                .and_then(Value::as_str)
                .is_some_and(|s| !s.is_empty())
        {
            continue;
        }
        let dest = media_root.join(format!("{kind}{}", ext_from_url(&cu, ".png")));
        if let Ok(path) = download_bytes_sync(&cu, &dest) {
            downloaded.insert(kind.clone(), Value::String(path));
        }
    }
    downloaded
}

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    let cache_dir = ctx.app.data_dir.join("cache");
    match ctx.path {
        "/api/v2/steamgrid/status" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let settings = state.get("settings").cloned().unwrap_or_else(|| json!({}));
            Some(json_response(
                StatusCode::OK,
                &provider_status(&settings, Some(&cache_dir)),
                ctx.instance,
            ))
        }
        "/api/v2/steamgrid/search" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let settings = state.get("settings").cloned().unwrap_or_else(|| json!({}));
            if let Err(e) = require_available(&settings) {
                return Some(api_error_response(&e, ctx.request_id, ctx.instance));
            }
            let name = ctx.query_first("q").unwrap_or_default();
            let limit = ctx
                .query_first("limit")
                .and_then(|s| s.trim().parse::<usize>().ok())
                .unwrap_or(12);
            match search_games(name.trim(), limit, Some(&cache_dir)) {
                Ok(results) => Some(json_response(
                    StatusCode::OK,
                    &json!({"results": results, "provider": "steamgrid"}),
                    ctx.instance,
                )),
                Err(e) => Some(api_error_response(
                    &ApiError::bad_request(e),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/steamgrid/hygiene/report" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let mut games: Vec<Value> = state
                .get("games")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            if let Some(lim) = ctx
                .query_first("limit")
                .and_then(|s| s.trim().parse::<usize>().ok())
            {
                games.truncate(lim.max(1));
            }
            let media_root = ctx.app.data_dir.join("media");
            let mut report = build_report(&games, Some(&media_root), 200, true);
            report["batches"] = Value::Array(list_undo_batches(&cache_dir, 20));
            Some(json_response(StatusCode::OK, &report, ctx.instance))
        }
        _ => None,
    }
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    let cache_dir = ctx.app.data_dir.join("cache");
    match ctx.path {
        "/api/v2/steamgrid/test" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let settings = state.get("settings").cloned().unwrap_or_else(|| json!({}));
            if let Err(e) = require_available(&settings) {
                return Some(api_error_response(&e, ctx.request_id, ctx.instance));
            }
            match search_games("steam", 1, None) {
                Ok(results) => Some(json_response(
                    StatusCode::OK,
                    &json!({"ok": true, "probe_results": results.len()}),
                    ctx.instance,
                )),
                Err(e) => Some(api_error_response(
                    &ApiError::bad_request(e),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/steamgrid/info" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let settings = state.get("settings").cloned().unwrap_or_else(|| json!({}));
            if let Err(e) = require_available(&settings) {
                return Some(api_error_response(&e, ctx.request_id, ctx.instance));
            }
            let Some(sgid) = payload.get("steamgrid_id").and_then(|v| match v {
                Value::Number(n) => n.as_i64(),
                Value::String(s) => s.trim().parse::<i64>().ok(),
                _ => None,
            }) else {
                return Some(api_error_response(
                    &ApiError::bad_request("steamgrid_id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            match game_info(sgid, Some(&cache_dir)) {
                Ok(meta) => Some(json_response(StatusCode::OK, &meta, ctx.instance)),
                Err(e) => Some(api_error_response(
                    &ApiError::bad_request(e),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/steamgrid/apply" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let settings = state.get("settings").cloned().unwrap_or_else(|| json!({}));
            if let Err(e) = require_available(&settings) {
                return Some(api_error_response(&e, ctx.request_id, ctx.instance));
            }
            let stable_id = str_field(payload, "id").to_owned();
            if stable_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("A stable game id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let steamgrid_id = payload.get("steamgrid_id").and_then(|v| match v {
                Value::Number(n) => n.as_i64(),
                Value::String(s) => s.trim().parse::<i64>().ok(),
                _ => None,
            });
            let fields: Vec<String> = payload
                .get("fields")
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str().map(ToOwned::to_owned))
                        .collect()
                })
                .unwrap_or_else(|| vec!["name".to_owned(), "year".to_owned()]);
            let media_kinds = media_kinds_from(payload);
            let replace_existing = bool_field(payload, "replace_existing");
            let exact_payload = payload.get("media_urls").cloned();
            let app = Arc::clone(&ctx.app);
            let job = match ctx
                .app
                .jobs
                .submit("steamgrid-apply", false, None, None, move |_ctx| {
                    let st = app.load_state().map_err(|e| e.to_string())?;
                    let mut probe = Map::new();
                    probe.insert("game_id".into(), Value::String(stable_id.clone()));
                    let idx = find_game_index(&st, &probe).map_err(|e| e.message)?;
                    let original = st["games"][idx].clone();
                    let cdir = app.data_dir.join("cache");
                    let sgd_id = match steamgrid_id {
                        Some(id) => id,
                        None => {
                            let name = original.get("name").and_then(Value::as_str).unwrap_or("");
                            let found = search_games(name, 1, Some(&cdir))?;
                            let Some(id) = found
                                .first()
                                .and_then(|f| f.get("id"))
                                .and_then(Value::as_i64)
                            else {
                                return Ok(json!({
                                    "applied": false,
                                    "game": name,
                                    "error": "no_match",
                                }));
                            };
                            id
                        }
                    };
                    let meta = game_info(sgd_id, Some(&cdir))?;
                    let mut media_urls = choose_media(&meta, &media_kinds);
                    let allowed: HashSet<(String, String)> = meta
                        .get("media")
                        .and_then(Value::as_array)
                        .map(|arr| {
                            arr.iter()
                                .filter_map(|e| {
                                    Some((
                                        e.get("kind")?.as_str()?.trim().to_owned(),
                                        e.get("url")?.as_str()?.trim().to_owned(),
                                    ))
                                })
                                .filter(|(_, u)| !u.is_empty())
                                .collect()
                        })
                        .unwrap_or_default();
                    for (k, v) in clean_exact_media_urls(exact_payload.as_ref(), &allowed) {
                        media_urls.insert(k, v);
                    }
                    let mroot = media_root_for(&app.data_dir, &original, &stable_id);
                    let downloaded =
                        download_sgdb_media(&media_urls, &original, &mroot, replace_existing);
                    let dl_keys: Vec<String> = downloaded.keys().cloned().collect();
                    let mut game_name = String::new();
                    app.transact_state(|cur| {
                        if let Some(gs) = cur.get_mut("games").and_then(Value::as_array_mut)
                            && let Some(g) = gs.iter_mut().find(|x| {
                                x.get("game_id").and_then(Value::as_str) == Some(&stable_id)
                            })
                            && let Some(m) = g.as_object_mut()
                        {
                            apply_to_game(m, &meta, &fields);
                            for (k, v) in &downloaded {
                                m.insert(k.clone(), v.clone());
                            }
                            game_name = m
                                .get("name")
                                .and_then(Value::as_str)
                                .unwrap_or("")
                                .to_owned();
                        }
                        Ok(())
                    })
                    .map_err(|e| e.to_string())?;
                    app.bump_media_epoch();
                    Ok(json!({"applied": true, "game": game_name, "media": dl_keys}))
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
                &json!({"state": "queued", "job_id": job.get("job_id").cloned().unwrap_or(Value::Null)}),
                ctx.instance,
            ))
        }
        "/api/v2/steamgrid/match" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let settings = state.get("settings").cloned().unwrap_or_else(|| json!({}));
            if let Err(e) = require_available(&settings) {
                return Some(api_error_response(&e, ctx.request_id, ctx.instance));
            }
            let ids: Vec<String> = payload
                .get("ids")
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str().map(ToOwned::to_owned))
                        .take(MATCH_BATCH_LIMIT)
                        .collect()
                })
                .unwrap_or_default();
            let media_kinds = media_kinds_from(payload);
            let app = Arc::clone(&ctx.app);
            let job = match ctx
                .app
                .jobs
                .submit("steamgrid-match", false, None, None, move |jctx| {
                    let st = app.load_state().map_err(|e| e.to_string())?;
                    let mut games: Vec<Value> = st
                        .get("games")
                        .and_then(Value::as_array)
                        .cloned()
                        .unwrap_or_default();
                    if !ids.is_empty() {
                        let wanted: HashSet<&str> = ids.iter().map(String::as_str).collect();
                        games.retain(|g| {
                            g.get("game_id")
                                .and_then(Value::as_str)
                                .is_some_and(|id| wanted.contains(id))
                        });
                    } else {
                        games.retain(|g| {
                            g.get("cover")
                                .and_then(Value::as_str)
                                .is_none_or(str::is_empty)
                        });
                    }
                    let cdir = app.data_dir.join("cache");
                    let mut results = Vec::new();
                    for game in games.iter().take(MATCH_BATCH_LIMIT) {
                        if jctx.is_cancelled() {
                            break;
                        }
                        let gid = game
                            .get("game_id")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_owned();
                        let name = game
                            .get("name")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .trim()
                            .to_owned();
                        let mut entry = json!({"game_id": gid, "name": name});
                        if name.is_empty() {
                            entry["status"] = Value::String("no_name".to_owned());
                            results.push(entry);
                            continue;
                        }
                        match search_games(&name, 1, Some(&cdir)) {
                            Ok(found) if !found.is_empty() => {
                                let sgd_id =
                                    found[0].get("id").and_then(Value::as_i64).unwrap_or(0);
                                entry["steamgrid_id"] = json!(sgd_id);
                                entry["match_name"] =
                                    found[0].get("name").cloned().unwrap_or_else(|| json!(""));
                                match game_assets(sgd_id, Some(&cdir)) {
                                    Ok(assets) => {
                                        let urls =
                                            choose_media(&json!({"media": assets}), &media_kinds);
                                        let mroot = media_root_for(&app.data_dir, game, &gid);
                                        let downloaded =
                                            download_sgdb_media(&urls, game, &mroot, false);
                                        let dl_keys: Vec<String> =
                                            downloaded.keys().cloned().collect();
                                        if !downloaded.is_empty() {
                                            let gid_c = gid.clone();
                                            let _ = app.transact_state(|cur| {
                                                if let Some(gs) = cur
                                                    .get_mut("games")
                                                    .and_then(Value::as_array_mut)
                                                    && let Some(t) = gs.iter_mut().find(|x| {
                                                        x.get("game_id").and_then(Value::as_str)
                                                            == Some(&gid_c)
                                                    })
                                                    && let Some(m) = t.as_object_mut()
                                                {
                                                    for (k, v) in &downloaded {
                                                        m.insert(k.clone(), v.clone());
                                                    }
                                                    m.insert("steamgrid_id".into(), json!(sgd_id));
                                                }
                                                Ok(())
                                            });
                                        }
                                        entry["status"] = Value::String("matched".to_owned());
                                        entry["media"] = json!(dl_keys);
                                    }
                                    Err(e) => {
                                        entry["status"] = Value::String("error".to_owned());
                                        entry["error"] = Value::String(e);
                                    }
                                }
                            }
                            Ok(_) => {
                                entry["status"] = Value::String("not_found".to_owned());
                            }
                            Err(e) => {
                                entry["status"] = Value::String("error".to_owned());
                                entry["error"] = Value::String(e);
                            }
                        }
                        results.push(entry);
                    }
                    app.bump_media_epoch();
                    let count = results.len();
                    Ok(json!({"matches": results, "count": count}))
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
                &json!({"state": "queued", "job_id": job.get("job_id").cloned().unwrap_or(Value::Null)}),
                ctx.instance,
            ))
        }
        "/api/v2/steamgrid/hygiene/fix" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let settings = state.get("settings").cloned().unwrap_or_else(|| json!({}));
            if let Err(e) = require_available(&settings) {
                return Some(api_error_response(&e, ctx.request_id, ctx.instance));
            }
            let game_ids: Option<Vec<String>> = payload
                .get("game_ids")
                .and_then(Value::as_array)
                .filter(|a| !a.is_empty())
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str().map(ToOwned::to_owned))
                        .collect()
                });
            let fields: Vec<String> = payload
                .get("fields")
                .and_then(Value::as_array)
                .filter(|a| !a.is_empty())
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str().map(ToOwned::to_owned))
                        .collect()
                })
                .unwrap_or_else(|| vec!["cover".to_owned()]);
            let issues: Option<Vec<String>> = payload
                .get("issues")
                .and_then(Value::as_array)
                .filter(|a| !a.is_empty())
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str().map(ToOwned::to_owned))
                        .collect()
                });
            let limit: usize = match payload.get("limit") {
                None | Some(Value::Null) => HYGIENE_BATCH_LIMIT,
                Some(Value::Number(n)) => match n.as_i64() {
                    Some(v) => v.clamp(1, HYGIENE_BATCH_LIMIT as i64) as usize,
                    None => {
                        return Some(api_error_response(
                            &ApiError::bad_request("limit must be an integer."),
                            ctx.request_id,
                            ctx.instance,
                        ));
                    }
                },
                Some(Value::String(s)) => match s.trim().parse::<i64>() {
                    Ok(v) => v.clamp(1, HYGIENE_BATCH_LIMIT as i64) as usize,
                    Err(_) => {
                        return Some(api_error_response(
                            &ApiError::bad_request("limit must be an integer."),
                            ctx.request_id,
                            ctx.instance,
                        ));
                    }
                },
                _ => {
                    return Some(api_error_response(
                        &ApiError::bad_request("limit must be an integer."),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let app = Arc::clone(&ctx.app);
            let job =
                match ctx
                    .app
                    .jobs
                    .submit("steamgrid-hygiene-fix", false, None, None, move |jctx| {
                        let st = app.load_state().map_err(|e| e.to_string())?;
                        let games = st
                            .get("games")
                            .and_then(Value::as_array)
                            .cloned()
                            .unwrap_or_default();
                        let mroot_base = app.data_dir.join("media");
                        let cdir = app.data_dir.join("cache");
                        let report = build_report(&games, Some(&mroot_base), 200, true);
                        let mut targets = select_fixable(
                            &report,
                            issues.as_deref(),
                            Some(&fields),
                            game_ids.as_deref(),
                        );
                        targets.truncate(limit);
                        let batch_id = Uuid::new_v4().simple().to_string();
                        let mut records = Vec::new();
                        let mut results = Vec::new();

                        for target in &targets {
                            if jctx.is_cancelled() {
                                break;
                            }
                            let gid = target["game_id"].as_str().unwrap_or("").to_owned();
                            let field = target["field"].as_str().unwrap_or("cover").to_owned();
                            let mut entry = json!({
                                "game_id": gid,
                                "name": target["name"],
                                "field": field,
                            });
                            let Some(game) = games
                                .iter()
                                .find(|g| g.get("game_id").and_then(Value::as_str) == Some(&gid))
                            else {
                                entry["status"] = Value::String("game_missing".to_owned());
                                results.push(entry);
                                continue;
                            };
                            let name = game
                                .get("name")
                                .and_then(Value::as_str)
                                .unwrap_or("")
                                .trim();
                            if name.is_empty() {
                                entry["status"] = Value::String("error".to_owned());
                                entry["error"] = Value::String(
                                    "This game has no name to search with.".to_owned(),
                                );
                                results.push(entry);
                                continue;
                            }
                            match search_games(name, 1, Some(&cdir)) {
                                Ok(found) if !found.is_empty() => {
                                    let sgd_id =
                                        found[0].get("id").and_then(Value::as_i64).unwrap_or(0);
                                    entry["steamgrid_id"] = json!(sgd_id);
                                    match game_assets(sgd_id, Some(&cdir)) {
                                        Ok(assets) => {
                                            let urls = choose_media(
                                                &json!({"media": assets}),
                                                std::slice::from_ref(&field),
                                            );
                                            let murl = urls
                                                .get(&field)
                                                .map(|u| clean_media_url(u))
                                                .unwrap_or_default();
                                            if murl.is_empty() {
                                                entry["status"] =
                                                    Value::String("not_found".to_owned());
                                            } else {
                                                let prev = game
                                                    .get(&field)
                                                    .and_then(Value::as_str)
                                                    .unwrap_or("");
                                                let mroot =
                                                    media_root_for(&app.data_dir, game, &gid);
                                                let mut rec = snapshot_for_replacement(
                                                    &cdir, &batch_id, &gid, &field, prev, true,
                                                )?;
                                                let dest = mroot.join(format!(
                                                    "{field}{}",
                                                    ext_from_url(&murl, ".png")
                                                ));
                                                match download_bytes_sync(&murl, &dest) {
                                                    Ok(dl_path) => {
                                                        rec["new"] = Value::String(dl_path.clone());
                                                        let gid_c = gid.clone();
                                                        let field_c = field.clone();
                                                        let dl_c = dl_path.clone();
                                                        let _ = app.transact_state(|cur| {
                                                            if let Some(gs) = cur
                                                                .get_mut("games")
                                                                .and_then(Value::as_array_mut)
                                                                && let Some(t) =
                                                                    gs.iter_mut().find(|x| {
                                                                        x.get("game_id")
                                                                            .and_then(Value::as_str)
                                                                            == Some(&gid_c)
                                                                    })
                                                                && let Some(m) = t.as_object_mut()
                                                            {
                                                                m.insert(
                                                                    field_c,
                                                                    Value::String(dl_c),
                                                                );
                                                                m.insert(
                                                                    "artwork_provider".into(),
                                                                    Value::String(
                                                                        PROVIDER_ATTRIBUTION
                                                                            .to_owned(),
                                                                    ),
                                                                );
                                                                m.insert(
                                                                    "steamgrid_id".into(),
                                                                    json!(sgd_id),
                                                                );
                                                            }
                                                            Ok(())
                                                        });
                                                        records.push(rec);
                                                        app.bump_media_epoch();
                                                        entry["status"] =
                                                            Value::String("applied".to_owned());
                                                        entry["media"] = Value::String(dl_path);
                                                    }
                                                    Err(e) => {
                                                        entry["status"] =
                                                            Value::String("error".to_owned());
                                                        entry["error"] = Value::String(e);
                                                    }
                                                }
                                            }
                                        }
                                        Err(e) => {
                                            entry["status"] = Value::String("error".to_owned());
                                            entry["error"] = Value::String(e);
                                        }
                                    }
                                }
                                Ok(_) => {
                                    entry["status"] = Value::String("not_found".to_owned());
                                }
                                Err(e) => {
                                    entry["status"] = Value::String("error".to_owned());
                                    entry["error"] = Value::String(e);
                                }
                            }
                            results.push(entry);
                        }
                        if !records.is_empty() {
                            let _ = write_undo_manifest(
                                &cdir,
                                &batch_id,
                                &records,
                                PROVIDER_ATTRIBUTION,
                            );
                        }
                        let applied = results
                            .iter()
                            .filter(|e| e.get("status").and_then(Value::as_str) == Some("applied"))
                            .count();
                        let total_res = results.len();
                        Ok(json!({
                            "batch_id": batch_id,
                            "provider": PROVIDER_ATTRIBUTION,
                            "applied": applied,
                            "failed": total_res.saturating_sub(applied),
                            "total": targets.len(),
                            "cancelled": jctx.is_cancelled(),
                            "results": results,
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
                &json!({
                    "state": "queued",
                    "job_id": job.get("job_id").cloned().unwrap_or(Value::Null),
                    "provider": PROVIDER_ATTRIBUTION,
                }),
                ctx.instance,
            ))
        }
        "/api/v2/steamgrid/hygiene/undo" => {
            let batch_id = str_field(payload, "batch_id");
            if batch_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("batch_id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let manifest = match load_undo_manifest(&cache_dir, batch_id) {
                Ok(m) => m,
                Err(e) => {
                    return Some(api_error_response(
                        &ApiError::bad_request(e).with_code("ARTWORK_UNDO_MISSING"),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let restored = match undo_batch(&cache_dir, batch_id) {
                Ok(r) => r,
                Err(e) => {
                    return Some(api_error_response(
                        &ApiError::bad_request(e).with_code("ARTWORK_UNDO_MISSING"),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let mut prev_by_key: HashMap<(String, String), String> = HashMap::new();
            if let Some(recs) = manifest.get("records").and_then(Value::as_array) {
                for r in recs {
                    if let (Some(g), Some(f)) = (
                        r.get("game_id").and_then(Value::as_str),
                        r.get("field").and_then(Value::as_str),
                    ) {
                        let prev = r.get("previous").and_then(Value::as_str).unwrap_or("");
                        prev_by_key.insert((g.to_owned(), f.to_owned()), prev.to_owned());
                    }
                }
            }
            let mut removed_cnt = 0usize;
            let mut restored_cnt = 0usize;
            let _ = ctx.app.transact_state(|st| {
                if let Some(games) = st.get_mut("games").and_then(Value::as_array_mut) {
                    for rec in &restored {
                        let gid = rec.get("game_id").and_then(Value::as_str).unwrap_or("");
                        let field = rec.get("field").and_then(Value::as_str).unwrap_or("");
                        let act = rec.get("action").and_then(Value::as_str).unwrap_or("");
                        if gid.is_empty() || field.is_empty() {
                            continue;
                        }
                        if let Some(g) = games
                            .iter_mut()
                            .find(|x| x.get("game_id").and_then(Value::as_str) == Some(gid))
                            && let Some(m) = g.as_object_mut()
                        {
                            if act == "restored" {
                                if let Some(prev) =
                                    prev_by_key.get(&(gid.to_owned(), field.to_owned()))
                                    && !prev.is_empty()
                                {
                                    m.insert(field.to_owned(), Value::String(prev.clone()));
                                    restored_cnt += 1;
                                }
                            } else if act == "removed" {
                                m.insert(field.to_owned(), Value::String(String::new()));
                                removed_cnt += 1;
                            }
                        }
                    }
                }
                Ok(())
            });
            ctx.app.bump_media_epoch();
            Some(json_response(
                StatusCode::OK,
                &json!({
                    "batch_id": batch_id,
                    "restored": restored,
                    "counts": {"removed": removed_cnt, "restored": restored_cnt},
                }),
                ctx.instance,
            ))
        }
        _ => None,
    }
}
