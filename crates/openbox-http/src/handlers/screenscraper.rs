//! ScreenScraper v2 provider handlers (`handlers/screenscraper.py`).

use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::sync::{Arc, LazyLock};

use axum::http::StatusCode;
use axum::response::Response;
use openbox_core::ApiError;
use openbox_metadata::http::{download_bytes_sync, ext_from_url};
use openbox_metadata::screenscraper::{
    HASH_TIER_DUAL, HASH_TIER_NONE, HASH_TIER_TITLE, apply_to_game, auto_apply_allowed, cache_size,
    choose_media, clean_media_url, confident_hash_match, game_info, is_configured, search_games,
    system_id_for_platform, user_info,
};
use regex::Regex;
use serde_json::{Map, Value, json};

use crate::handlers::projection::find_game_index_in_state as find_game_index;
use crate::handlers::shared::{RequestCtx, bool_field, str_field};
use crate::middleware::{api_error_response, json_response};

const MATCH_BATCH_LIMIT: usize = 100;
static EXACT_URL_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)^https?://\S+$").unwrap());

fn default_ext_for_kind(kind: &str) -> &'static str {
    match kind {
        "video_snap" => ".mp4",
        "manual" => ".pdf",
        _ => ".jpg",
    }
}

fn clean_exact_media_urls(
    value: Option<&Value>,
    media_kinds: &[String],
    allowed: &HashSet<(String, String)>,
) -> HashMap<String, Value> {
    let mut out = HashMap::new();
    if let Some(obj) = value.and_then(Value::as_object) {
        for (k, v) in obj {
            let kind = k.trim();
            let url = v.as_str().unwrap_or("").trim();
            if media_kinds.iter().any(|mk| mk == kind)
                && EXACT_URL_RE.is_match(url)
                && allowed.contains(&(kind.to_owned(), url.to_owned()))
            {
                if kind == "screenshots" {
                    out.insert(kind.to_owned(), json!([url]));
                } else {
                    out.insert(kind.to_owned(), Value::String(url.to_owned()));
                }
            }
        }
    }
    out
}

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    let cache_dir = ctx.app.data_dir.join("cache");
    match ctx.path {
        "/api/v2/screenscraper/status" => Some(json_response(
            StatusCode::OK,
            &json!({
                "configured": is_configured(),
                "cache_entries": cache_size(&cache_dir),
            }),
            ctx.instance,
        )),
        "/api/v2/screenscraper/search" => {
            let query = ctx.query_first("q").unwrap_or_default();
            let platform = ctx.query_first("platform").unwrap_or_default();
            match search_games(query.trim(), system_id_for_platform(platform.trim()), 12) {
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
        _ => None,
    }
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    let cache_dir = ctx.app.data_dir.join("cache");
    match ctx.path {
        "/api/v2/screenscraper/test" => match user_info(Some(&cache_dir)) {
            Ok(info) => {
                let user = info
                    .get("response")
                    .and_then(|r| r.get("ssuser"))
                    .cloned()
                    .unwrap_or_else(|| json!({}));
                Some(json_response(
                    StatusCode::OK,
                    &json!({"ok": true, "user": user}),
                    ctx.instance,
                ))
            }
            Err(e) => Some(api_error_response(
                &ApiError::bad_request(e),
                ctx.request_id,
                ctx.instance,
            )),
        },
        "/api/v2/screenscraper/info" => {
            let scraper_id = payload.get("scraper_id").and_then(|v| match v {
                Value::Number(n) => n.as_i64(),
                Value::String(s) => s.trim().parse::<i64>().ok(),
                _ => None,
            });
            let rom_path_s = str_field(payload, "rom_path");
            let rom_path = if rom_path_s.is_empty() {
                None
            } else {
                Some(Path::new(rom_path_s))
            };
            let system_id = system_id_for_platform(str_field(payload, "platform"));
            match game_info(scraper_id, rom_path, system_id, Some(&cache_dir)) {
                Ok(meta) => Some(json_response(StatusCode::OK, &meta, ctx.instance)),
                Err(e) => Some(api_error_response(
                    &ApiError::bad_request(e),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/screenscraper/match" => {
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
            let apply_confident = bool_field(payload, "apply_confident");
            let app = Arc::clone(&ctx.app);
            let job =
                match ctx
                    .app
                    .jobs
                    .submit("screenscraper-match", false, None, None, move |jctx| {
                        let state = app.load_state().map_err(|e| e.to_string())?;
                        let mut games: Vec<Value> = state
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
                        }
                        let cdir = app.data_dir.join("cache");
                        let default_fields: Vec<String> = [
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
                        let mut results = Vec::new();
                        for game in games.iter().take(MATCH_BATCH_LIMIT) {
                            if jctx.is_cancelled() {
                                break;
                            }
                            let stable_id = game
                                .get("game_id")
                                .and_then(Value::as_str)
                                .unwrap_or("")
                                .to_owned();
                            let name = game
                                .get("name")
                                .and_then(Value::as_str)
                                .unwrap_or("")
                                .to_owned();
                            let sys_id = system_id_for_platform(
                                game.get("platform").and_then(Value::as_str).unwrap_or(""),
                            );
                            let rom_path = game.get("path").and_then(Value::as_str).unwrap_or("");
                            let had_rom = !rom_path.is_empty() && Path::new(rom_path).is_file();
                            let mut entry = json!({"game_id": stable_id, "name": name});
                            let mut tier = HASH_TIER_NONE;
                            let mut meta_opt: Option<Value> = None;
                            if had_rom {
                                match confident_hash_match(Path::new(rom_path), sys_id, Some(&cdir))
                                {
                                    Ok((m, t)) => {
                                        meta_opt = Some(m);
                                        tier = t;
                                    }
                                    Err(e) => {
                                        entry["hash_error"] = Value::String(e);
                                    }
                                }
                            }
                            if tier == HASH_TIER_DUAL
                                && let Some(meta) = meta_opt
                            {
                                entry["status"] = Value::String("matched".to_owned());
                                entry["matched_by"] = Value::String("hash".to_owned());
                                entry["confidence"] = Value::String(tier.to_owned());
                                entry["scraper_id"] =
                                    meta.get("id").cloned().unwrap_or(Value::Null);
                                entry["match_name"] = Value::String(
                                    meta.get("name")
                                        .and_then(Value::as_str)
                                        .unwrap_or("")
                                        .to_owned(),
                                );
                                if apply_confident && auto_apply_allowed(tier) {
                                    let sid_c = stable_id.clone();
                                    let meta_c = meta.clone();
                                    let flds = default_fields.clone();
                                    let applied = app
                                        .transact_state(|cur| {
                                            if let Some(gs) =
                                                cur.get_mut("games").and_then(Value::as_array_mut)
                                                && let Some(g) = gs.iter_mut().find(|x| {
                                                    x.get("game_id").and_then(Value::as_str)
                                                        == Some(&sid_c)
                                                })
                                                && let Some(m) = g.as_object_mut()
                                            {
                                                apply_to_game(m, &meta_c, &flds);
                                                m.insert(
                                                    "matched_by".into(),
                                                    Value::String("hash".to_owned()),
                                                );
                                                m.insert(
                                                    "match_confidence".into(),
                                                    Value::String(HASH_TIER_DUAL.to_owned()),
                                                );
                                                return Ok(true);
                                            }
                                            Ok(false)
                                        })
                                        .map(|(_, ok)| ok)
                                        .unwrap_or(false);
                                    entry["applied"] = Value::Bool(applied);
                                }
                            } else if !name.trim().is_empty()
                                && let Ok(hits) = search_games(name.trim(), sys_id, 5)
                                && let Some(hit) = hits.first()
                            {
                                entry["status"] = Value::String("matched".to_owned());
                                entry["matched_by"] = Value::String("title".to_owned());
                                entry["confidence"] = Value::String(HASH_TIER_TITLE.to_owned());
                                entry["scraper_id"] = hit.get("id").cloned().unwrap_or(Value::Null);
                                entry["match_name"] = Value::String(
                                    hit.get("name")
                                        .and_then(Value::as_str)
                                        .unwrap_or("")
                                        .to_owned(),
                                );
                            } else {
                                entry["status"] = Value::String(
                                    if had_rom { "not_found" } else { "no_rom" }.to_owned(),
                                );
                                entry["confidence"] = Value::String(HASH_TIER_NONE.to_owned());
                            }
                            results.push(entry);
                        }
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
        "/api/v2/screenscraper/apply" => {
            let stable_id = str_field(payload, "id").to_owned();
            if stable_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("A stable game id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let scraper_id = payload.get("scraper_id").and_then(|v| match v {
                Value::Number(n) => n.as_i64(),
                Value::String(s) => s.trim().parse::<i64>().ok(),
                _ => None,
            });
            let rom_path = str_field(payload, "rom_path").to_owned();
            let fields: Vec<String> = payload
                .get("fields")
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str().map(ToOwned::to_owned))
                        .collect()
                })
                .unwrap_or_else(|| {
                    [
                        "name",
                        "description",
                        "year",
                        "genre",
                        "developer",
                        "publisher",
                    ]
                    .iter()
                    .map(|s| (*s).to_owned())
                    .collect()
                });
            let media_kinds: Vec<String> = payload
                .get("media")
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str().map(ToOwned::to_owned))
                        .collect()
                })
                .unwrap_or_default();
            let replace_existing = bool_field(payload, "replace_existing");
            let exact_payload = payload.get("media_urls").cloned();
            let app = Arc::clone(&ctx.app);
            let job =
                match ctx
                    .app
                    .jobs
                    .submit("screenscraper-apply", false, None, None, move |_ctx| {
                        let state = app.load_state().map_err(|e| e.to_string())?;
                        let mut probe = Map::new();
                        probe.insert("game_id".into(), Value::String(stable_id.clone()));
                        let idx = find_game_index(&state, &probe).map_err(|e| e.message)?;
                        let original = state["games"][idx].clone();
                        let sys_id = system_id_for_platform(
                            original
                                .get("platform")
                                .and_then(Value::as_str)
                                .unwrap_or(""),
                        );
                        let rp = if rom_path.is_empty() {
                            None
                        } else {
                            Some(Path::new(rom_path.as_str()))
                        };
                        let meta =
                            game_info(scraper_id, rp, sys_id, Some(&app.data_dir.join("cache")))?;
                        let reg_pri: Option<Vec<String>> = state
                            .get("settings")
                            .and_then(|s| s.get("region_priority"))
                            .and_then(Value::as_array)
                            .map(|a| {
                                a.iter()
                                    .filter_map(|v| v.as_str().map(ToOwned::to_owned))
                                    .collect()
                            });
                        let mut media_urls =
                            choose_media(&meta, &media_kinds, reg_pri.as_deref(), 20);
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
                        for (k, v) in
                            clean_exact_media_urls(exact_payload.as_ref(), &media_kinds, &allowed)
                        {
                            media_urls.insert(k, v);
                        }
                        let name_or_id = original
                            .get("name")
                            .and_then(Value::as_str)
                            .filter(|s| !s.is_empty())
                            .unwrap_or(&stable_id);
                        let slug: String = name_or_id
                            .chars()
                            .filter(|c| c.is_alphanumeric() || *c == '-' || *c == '_' || *c == ' ')
                            .collect::<String>()
                            .trim()
                            .replace(' ', "-")
                            .chars()
                            .take(60)
                            .collect();
                        let media_root = app
                            .data_dir
                            .join("media")
                            .join("screenscraper")
                            .join(if slug.is_empty() { &stable_id } else { &slug });
                        let mut downloaded: Map<String, Value> = Map::new();
                        for (kind, val) in media_urls {
                            if !replace_existing
                                && original
                                    .get(&kind)
                                    .is_some_and(|v| !v.is_null() && v.as_str() != Some(""))
                            {
                                continue;
                            }
                            if kind == "screenshots" {
                                if let Some(arr) = val.as_array() {
                                    let mut paths = Vec::new();
                                    for (i, u) in arr.iter().enumerate() {
                                        let cu = clean_media_url(u.as_str().unwrap_or(""));
                                        if cu.is_empty() {
                                            continue;
                                        }
                                        let dest = media_root
                                            .join("screenshots")
                                            .join(format!("ss-{i}{}", ext_from_url(&cu, ".jpg")));
                                        if let Ok(p) = download_bytes_sync(&cu, &dest) {
                                            paths.push(Value::String(p));
                                        }
                                    }
                                    if !paths.is_empty() {
                                        downloaded
                                            .insert("screenshots".into(), Value::Array(paths));
                                    }
                                }
                                continue;
                            }
                            let cu = clean_media_url(val.as_str().unwrap_or(""));
                            if cu.is_empty() {
                                continue;
                            }
                            let dest = media_root.join(format!(
                                "{kind}{}",
                                ext_from_url(&cu, default_ext_for_kind(&kind))
                            ));
                            if let Ok(p) = download_bytes_sync(&cu, &dest) {
                                downloaded.insert(kind, Value::String(p));
                            }
                        }
                        let mut game_name = String::new();
                        let dl_keys: Vec<String> = downloaded.keys().cloned().collect();
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
        _ => None,
    }
}
