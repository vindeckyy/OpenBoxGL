//! Core Library CRUD, search, delta, tags, queue, notifications, trash, playlists,
//! webhooks, platform admin, and bulk edit handlers (`handlers/library.py` + parts of `data.py`/`extensions.py`).

use std::collections::HashSet;
use std::fs;
use std::path::Path;
use std::sync::{Arc, LazyLock};

use axum::body::Body;
use axum::http::StatusCode;
use axum::http::header::{CONTENT_ENCODING, ETAG, HeaderValue, VARY};
use axum::response::Response;
use openbox_core::{ApiError, stable_game_id};
use openbox_discovery::discovery::{discovery_lists, related_with_reasons};
use openbox_discovery::filter_presets::{
    bigbox_quick_presets, delete_preset, list_presets, save_preset,
};
use openbox_discovery::query::{filter_games_by_query, parse_query};
use openbox_imports::{
    PROGRESS, bulk_update, bulk_update_with_media, bulk_wizard_changes, canon_progress,
    clean_currency, clean_price_cents, clean_user_rating, consolidate_existing_games,
    custom_field_defs, delete_platform, explorer_facets, find_replace, game_media_paths,
    is_valid_progress, launcher_menu_items, normalize_custom_fields, normalize_filter_rules,
    normalize_manual_sessions, normalize_notes, related_game_ids, rename_platform, tag_counts,
    trash_entry_for, value_to_str,
};
use openbox_platform::{expand_user, open_path, user_symlink_component};
use openbox_runtime::notifications::{clear as clear_notifications, mark_read, unread_count};
use openbox_runtime::play_queue::{
    advance as advance_queue, enqueue as enqueue_queue, remove as remove_queue,
    reorder as reorder_queue, resolve_queue,
};
use openbox_runtime::webhooks::{
    DEFAULT_ATTEMPTS, DEFAULT_TIMEOUT, EVENT_TYPES, MAX_WEBHOOKS, public_webhook_configs,
    validate_webhook,
};
use openbox_runtime::{FIELDS, MEDIA_PATH_FIELDS};
use openbox_store::json_store::{TRASH_CAP, prune_trash};
use rand::RngCore;
use regex::Regex;
use serde_json::{Map, Value, json};

use crate::handlers::projection::{
    clean_extras, find_game_index, public_library_delta, public_library_page, public_settings,
    public_state_cached, public_state_etag,
};
use crate::handlers::shared::{RequestCtx, bool_field, str_field};
use crate::middleware::{
    api_error_response, apply_security_headers, json_response, state_error_response,
};

static ENV_KEY_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z_][A-Za-z0-9_]*$").expect("valid env key regex"));
static DATE_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^\d{4}-\d{2}-\d{2}$").expect("valid date regex"));

fn allowed_progress(state: &Value) -> HashSet<String> {
    state
        .get("settings")
        .and_then(|s| s.get("progress_values"))
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .map(ToOwned::to_owned)
                .collect()
        })
        .unwrap_or_else(|| PROGRESS.iter().map(|p| (*p).to_owned()).collect())
}

fn clean_launch_env(val: Option<&Value>) -> Result<Map<String, Value>, ApiError> {
    let Some(v) = val else {
        return Ok(Map::new());
    };
    if v.is_null() {
        return Ok(Map::new());
    }
    let pairs: Vec<(String, String)> = match v {
        Value::String(s) => {
            let mut out = Vec::new();
            for line in s.lines() {
                let trimmed = line.trim();
                if trimmed.is_empty() || trimmed.starts_with('#') {
                    continue;
                }
                let Some((k, val)) = trimmed.split_once('=') else {
                    return Err(ApiError::bad_request(format!(
                        "Environment override must be KEY=value: {trimmed:?}"
                    )));
                };
                out.push((k.trim().to_owned(), val.trim().to_owned()));
            }
            out
        }
        Value::Object(m) => m
            .iter()
            .map(|(k, val)| (k.trim().to_owned(), value_to_str(val)))
            .collect(),
        _ => {
            return Err(ApiError::bad_request(
                "Environment overrides must be an object or KEY=value lines.",
            ));
        }
    };
    if pairs.len() > 50 {
        return Err(ApiError::bad_request(
            "At most 50 environment overrides per game.",
        ));
    }
    let mut clean = Map::new();
    for (k, val) in pairs {
        if !ENV_KEY_RE.is_match(&k) {
            return Err(ApiError::bad_request(format!(
                "Invalid environment variable name: {k:?}"
            )));
        }
        clean.insert(k, Value::String(val.chars().take(500).collect()));
    }
    Ok(clean)
}

fn clean_game_fields(
    source: &Map<String, Value>,
    allowed: &HashSet<String>,
) -> Result<Map<String, Value>, ApiError> {
    let mut game = Map::new();
    for &key in FIELDS {
        if let Some(v) = source.get(key) {
            game.insert(
                key.to_owned(),
                Value::String(value_to_str(v).trim().to_owned()),
            );
        }
    }
    if let Some(ss) = source.get("save_sources").and_then(Value::as_object) {
        let mut cleaned = Map::new();
        for (path, meta) in ss.iter().take(1000) {
            let p: String = path.trim().chars().take(4000).collect();
            let Some(m) = meta.as_object() else {
                continue;
            };
            if p.is_empty() {
                continue;
            }
            cleaned.insert(
                p,
                json!({
                    "emulator_id": m.get("emulator_id").and_then(Value::as_str).unwrap_or("").chars().take(200).collect::<String>(),
                    "core": m.get("core").and_then(Value::as_str).unwrap_or("").chars().take(200).collect::<String>(),
                    "slot": m.get("slot").and_then(Value::as_str).unwrap_or("").chars().take(200).collect::<String>(),
                }),
            );
        }
        if !cleaned.is_empty() {
            game.insert("save_sources".into(), Value::Object(cleaned));
        }
    }
    for flag in [
        "extract_archive",
        "hidden",
        "broken",
        "portable",
        "launch_confirm",
    ] {
        game.insert(
            flag.to_owned(),
            Value::Bool(source.get(flag).and_then(Value::as_bool).unwrap_or(false)),
        );
    }
    if let Some(dc) = source.get("disc_count") {
        let n = dc
            .as_i64()
            .or_else(|| {
                dc.as_str().and_then(|s| {
                    let t = s.trim();
                    if t.is_empty() {
                        Some(0)
                    } else {
                        t.parse().ok()
                    }
                })
            })
            .or_else(|| if dc.is_null() { Some(0) } else { None })
            .ok_or_else(|| ApiError::bad_request("Disc count must be a number."))?;
        game.insert("disc_count".into(), json!(n.max(0)));
    }
    let progress = canon_progress(game.get("progress"));
    if !is_valid_progress(&progress) {
        return Err(ApiError::bad_request("Unknown progress value."));
    }
    if !progress.is_empty() && !allowed.contains(&progress) {
        return Err(ApiError::bad_request(
            "Progress value is not allowed by the current progress whitelist.",
        ));
    }
    game.insert("progress".into(), Value::String(progress));
    let rating: f64 = match game.get("rating").and_then(Value::as_str) {
        None | Some("") => 0.0,
        Some(s) => s
            .parse::<f64>()
            .map_err(|_| ApiError::bad_request("Rating must be a number from 0 to 5."))?,
    };
    if !(0.0..=5.0).contains(&rating) {
        return Err(ApiError::bad_request("Rating must be between 0 and 5."));
    }
    game.insert("rating".into(), json!(rating));
    if source.contains_key("user_rating") {
        game.insert(
            "user_rating".into(),
            json!(clean_user_rating(source.get("user_rating"))?),
        );
    }
    if source.contains_key("price_paid_cents") {
        if let Some(price) = clean_price_cents(source.get("price_paid_cents"))? {
            game.insert("price_paid_cents".into(), json!(price));
            game.insert(
                "price_currency".into(),
                Value::String(clean_currency(source.get("price_currency"))?),
            );
        }
    } else if source.contains_key("price_currency") {
        return Err(ApiError::bad_request(
            "Currency cannot be set without a price.",
        ));
    }
    Ok(game)
}

fn clean_game_lists_and_misc(
    ctx: &RequestCtx<'_>,
    game: &mut Map<String, Value>,
    source: &Map<String, Value>,
    settings: &Value,
) -> Result<(), ApiError> {
    let save_paths = match source.get("save_paths") {
        None => Vec::new(),
        Some(Value::Array(a)) => a
            .iter()
            .map(|v| value_to_str(v).trim().to_owned())
            .filter(|s| !s.is_empty())
            .take(50)
            .map(Value::String)
            .collect(),
        _ => return Err(ApiError::bad_request("Save paths must be a list.")),
    };
    game.insert("save_paths".into(), Value::Array(save_paths));
    let screenshots = match source.get("screenshots") {
        None => Vec::new(),
        Some(Value::Array(a)) => {
            let mut out = Vec::new();
            for item in a.iter().take(100) {
                let s = value_to_str(item).trim().to_owned();
                if s.is_empty() {
                    continue;
                }
                let approved = ctx
                    .app
                    .media_probe
                    .approved_media_path(&s, false)
                    .map_err(ApiError::bad_request)?;
                out.push(Value::String(approved.to_string_lossy().into_owned()));
            }
            out
        }
        _ => return Err(ApiError::bad_request("Screenshots must be a list.")),
    };
    game.insert("screenshots".into(), Value::Array(screenshots));
    if let Some(alt) = source.get("alternate_names") {
        let names: Vec<Value> = match alt {
            Value::String(s) => s
                .split(';')
                .map(|p| p.trim().to_owned())
                .filter(|p| !p.is_empty())
                .map(Value::String)
                .collect(),
            Value::Array(a) => a
                .iter()
                .map(|v| value_to_str(v).trim().to_owned())
                .filter(|p| !p.is_empty())
                .take(20)
                .map(Value::String)
                .collect(),
            _ => Vec::new(),
        };
        game.insert("alternate_names".into(), Value::Array(names));
    }
    if source.contains_key("launch_env") {
        game.insert(
            "launch_env".into(),
            Value::Object(clean_launch_env(source.get("launch_env"))?),
        );
    }
    game.insert(
        "hide_in_bigbox".into(),
        Value::Bool(
            source
                .get("hide_in_bigbox")
                .and_then(Value::as_bool)
                .unwrap_or(false),
        ),
    );
    if let Some(esrb) = source
        .get("esrb")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
    {
        game.insert("esrb".into(), Value::String(esrb.to_owned()));
    }
    if let Some(cf) = source.get("custom_fields").and_then(Value::as_object) {
        let mut clean_cf = Map::new();
        for (k, v) in cf {
            let kk = k.trim();
            if !kk.is_empty() {
                clean_cf.insert(
                    kk.to_owned(),
                    Value::String(value_to_str(v).trim().to_owned()),
                );
            }
        }
        game.insert("custom_fields".into(), Value::Object(clean_cf));
        let defs = custom_field_defs(settings);
        normalize_custom_fields(game, &defs);
    }
    Ok(())
}

fn clean_playtime_entry(payload: &Map<String, Value>) -> Result<Value, ApiError> {
    let seconds = payload
        .get("seconds")
        .and_then(|v| {
            v.as_i64()
                .or_else(|| v.as_str().and_then(|s| s.trim().parse().ok()))
        })
        .ok_or_else(|| ApiError::bad_request("seconds must be an integer."))?;
    if seconds <= 0 || seconds > 24 * 3600 {
        return Err(ApiError::bad_request(
            "seconds must be between 1 and 86400.",
        ));
    }
    let date = str_field(payload, "date");
    if !date.is_empty() && !DATE_RE.is_match(date) {
        return Err(ApiError::bad_request("date must be YYYY-MM-DD."));
    }
    let note = str_field(payload, "note");
    if note.chars().count() > 200 {
        return Err(ApiError::bad_request("note is limited to 200 characters."));
    }
    Ok(json!({"date": date, "seconds": seconds, "note": note}))
}

fn resync_manual_playtime(game: &mut Map<String, Value>) {
    let sessions = normalize_manual_sessions(game.get("manual_sessions"));
    let total: i64 = sessions
        .iter()
        .filter_map(|s| s.get("seconds").and_then(Value::as_i64))
        .sum();
    game.insert("manual_sessions".into(), Value::Array(sessions));
    game.insert("manual_playtime_seconds".into(), json!(total));
}

fn trash_entry_public(entry: &Value) -> Value {
    let game = entry.get("game");
    json!({
        "trash_id": entry.get("trash_id").and_then(Value::as_str).unwrap_or(""),
        "game_id": entry.get("game_id").and_then(Value::as_str)
            .or_else(|| game.and_then(|g| g.get("game_id").and_then(Value::as_str)))
            .unwrap_or(""),
        "name": entry.get("name").and_then(Value::as_str)
            .or_else(|| game.and_then(|g| g.get("name").and_then(Value::as_str)))
            .unwrap_or(""),
        "platform": entry.get("platform").and_then(Value::as_str)
            .or_else(|| game.and_then(|g| g.get("platform").and_then(Value::as_str)))
            .unwrap_or(""),
        "trashed_at": entry.get("trashed_at").and_then(Value::as_str).unwrap_or(""),
        "playlists": entry.get("playlists").cloned().unwrap_or_else(|| json!([])),
    })
}

fn remove_media_files(
    ctx: &RequestCtx<'_>,
    media_paths: &[String],
    referenced: &HashSet<String>,
) -> (Vec<String>, Vec<String>) {
    let mut deleted = Vec::new();
    let mut shared = Vec::new();
    for path in media_paths {
        let Ok(target) = ctx.app.media_probe.approved_media_path(path, true) else {
            continue;
        };
        let canon = fs::canonicalize(&target)
            .unwrap_or_else(|_| target.clone())
            .to_string_lossy()
            .into_owned();
        let tstr = target.to_string_lossy().into_owned();
        if referenced.contains(&canon) {
            if !shared.contains(&tstr) {
                shared.push(tstr);
            }
            continue;
        }
        if target.is_file() && fs::remove_file(&target).is_ok() && !deleted.contains(&tstr) {
            deleted.push(tstr);
        }
    }
    (deleted, shared)
}

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    match ctx.path {
        "/api/v2/library" => {
            let offset_str = ctx.query_first("offset");
            let limit_str = ctx.query_first("limit");
            let etag = public_state_etag(&ctx.app);
            let inm = ctx
                .headers
                .get("if-none-match")
                .and_then(|v| v.to_str().ok())
                .unwrap_or("")
                .trim();
            if offset_str.is_none() && limit_str.is_none() && !etag.is_empty() && inm == etag {
                let mut resp = Response::builder()
                    .status(StatusCode::NOT_MODIFIED)
                    .body(Body::empty())
                    .unwrap();
                let h = resp.headers_mut();
                if let Ok(ev) = HeaderValue::from_str(&etag) {
                    h.insert(ETAG, ev);
                }
                apply_security_headers(
                    h,
                    "application/json; charset=utf-8",
                    "private, no-cache",
                    false,
                );
                return Some(resp);
            }
            if offset_str.is_some() || limit_str.is_some() {
                let offset: isize = match offset_str.as_deref().filter(|s| !s.is_empty()) {
                    None => 0,
                    Some(s) => match s.parse() {
                        Ok(n) => n,
                        Err(_) => {
                            return Some(api_error_response(
                                &ApiError::bad_request("Invalid pagination parameters"),
                                ctx.request_id,
                                ctx.instance,
                            ));
                        }
                    },
                };
                let limit: isize = match limit_str.as_deref().filter(|s| !s.is_empty()) {
                    None => 500,
                    Some(s) => match s.parse() {
                        Ok(n) => n,
                        Err(_) => {
                            return Some(api_error_response(
                                &ApiError::bad_request("Invalid pagination parameters"),
                                ctx.request_id,
                                ctx.instance,
                            ));
                        }
                    },
                };
                if offset < 0 || !(0..=5000).contains(&limit) {
                    return Some(api_error_response(
                        &ApiError::bad_request("Invalid pagination parameters"),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
                return Some(
                    match public_library_page(&ctx.app, offset as usize, limit as usize) {
                        Ok(resp_payload) => {
                            json_response(StatusCode::OK, &resp_payload, ctx.instance)
                        }
                        Err(err) => state_error_response(&err, ctx.request_id, ctx.instance),
                    },
                );
            }
            let cached = match public_state_cached(&ctx.app) {
                Ok(c) => c,
                Err(err) => return Some(state_error_response(&err, ctx.request_id, ctx.instance)),
            };

            let accepts_gzip = ctx
                .headers
                .get("accept-encoding")
                .and_then(|v| v.to_str().ok())
                .is_some_and(|s| s.contains("gzip"));
            let (body_bytes, is_gzip) = if accepts_gzip
                && let Some(ref gz) = cached.raw_gzip
                && gz.len() < cached.raw_json.len()
            {
                (gz.clone(), true)
            } else {
                (cached.raw_json.clone(), false)
            };
            let mut resp = Response::builder()
                .status(StatusCode::OK)
                .body(Body::from(body_bytes))
                .unwrap();
            let h = resp.headers_mut();
            if let Ok(ev) = HeaderValue::from_str(&etag) {
                h.insert(ETAG, ev);
            }
            if is_gzip {
                h.insert(CONTENT_ENCODING, HeaderValue::from_static("gzip"));
                h.insert(VARY, HeaderValue::from_static("Accept-Encoding"));
            }
            apply_security_headers(
                h,
                "application/json; charset=utf-8",
                "private, no-cache",
                false,
            );
            Some(resp)
        }
        "/api/v2/library/delta" => {
            let ids_str = ctx.query_first("ids").unwrap_or_default();
            let ids: Vec<&str> = ids_str
                .split(',')
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .collect();
            if ids.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("Missing ids parameter"),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if ids.len() > 1000 {
                return Some(api_error_response(
                    &ApiError::bad_request("Too many IDs (max 1000)"),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            match public_library_delta(&ctx.app, &ids) {
                Ok(resp_payload) => {
                    Some(json_response(StatusCode::OK, &resp_payload, ctx.instance))
                }
                Err(err) => Some(state_error_response(&err, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/library/search" => {
            let query = ctx.query_first("q").unwrap_or_default();
            let limit = match ctx.query_first("limit").as_deref() {
                None | Some("") => 50usize,
                Some(s) => match s.parse::<isize>() {
                    Ok(n) => n.clamp(1, 200) as usize,
                    Err(_) => {
                        return Some(api_error_response(
                            &ApiError::bad_request("Invalid limit."),
                            ctx.request_id,
                            ctx.instance,
                        ));
                    }
                },
            };
            if query.trim().is_empty() {
                return Some(json_response(
                    StatusCode::OK,
                    &json!({"results": [], "source": "json", "count": 0}),
                    ctx.instance,
                ));
            }
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(err) => {
                    return Some(api_error_response(
                        &ApiError::state_unavailable(err.to_string()),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let games = state
                .get("games")
                .and_then(Value::as_array)
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            if ctx.app.read_model.enabled() || ctx.app.read_model.should_auto_enable(games.len()) {
                let sig = ctx.app.store.signature();
                if ctx.app.read_model.is_fresh(sig) {
                    let results = ctx.app.read_model.search(&query, limit);
                    let count = results.len();
                    return Some(json_response(
                        StatusCode::OK,
                        &json!({"results": results, "source": "sqlite", "count": count}),
                        ctx.instance,
                    ));
                }
                let rm = Arc::clone(&ctx.app.read_model);
                let st_bg = Arc::clone(&state);
                std::thread::spawn(move || {
                    let _ = rm.rebuild(&st_bg, sig);
                });
            }
            let q_lower = query.to_lowercase();
            let mut results = Vec::new();
            for game in games {
                if game
                    .get("name")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_lowercase()
                    .contains(&q_lower)
                {
                    results.push(game.clone());
                    if results.len() >= limit {
                        break;
                    }
                }
            }
            let count = results.len();
            Some(json_response(
                StatusCode::OK,
                &json!({"results": results, "source": "json", "count": count}),
                ctx.instance,
            ))
        }
        "/api/v2/explorer/facets" => {
            let field = ctx
                .query_first("field")
                .unwrap_or_else(|| "genre".to_owned());
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(err) => {
                    return Some(api_error_response(
                        &ApiError::state_unavailable(err.to_string()),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let games = state
                .get("games")
                .and_then(Value::as_array)
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            let facets = explorer_facets(games, &field, 40);
            Some(json_response(
                StatusCode::OK,
                &json!({"field": field, "facets": facets}),
                ctx.instance,
            ))
        }
        "/api/v2/launcher/menu" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(err) => return Some(state_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let games = state
                .get("games")
                .and_then(Value::as_array)
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            Some(json_response(
                StatusCode::OK,
                &json!({"items": launcher_menu_items(games)}),
                ctx.instance,
            ))
        }
        "/api/v2/queue" => match ctx.app.load_state_view() {
            Ok(state) => Some(json_response(
                StatusCode::OK,
                &json!({"queue": resolve_queue(&state)}),
                ctx.instance,
            )),
            Err(err) => Some(api_error_response(
                &ApiError::state_unavailable(err.to_string()),
                ctx.request_id,
                ctx.instance,
            )),
        },
        "/api/v2/notifications" => match ctx.app.load_state_view() {
            Ok(state) => Some(json_response(
                StatusCode::OK,
                &json!({
                    "notifications": state.get("notifications").cloned().unwrap_or_else(|| json!([])),
                    "unread": unread_count(&state),
                }),
                ctx.instance,
            )),
            Err(err) => Some(api_error_response(
                &ApiError::state_unavailable(err.to_string()),
                ctx.request_id,
                ctx.instance,
            )),
        },
        "/api/v2/tags" => match ctx.app.load_state_view() {
            Ok(state) => {
                let games = state
                    .get("games")
                    .and_then(Value::as_array)
                    .map(Vec::as_slice)
                    .unwrap_or(&[]);
                Some(json_response(
                    StatusCode::OK,
                    &json!({"tags": tag_counts(games)}),
                    ctx.instance,
                ))
            }
            Err(err) => Some(api_error_response(
                &ApiError::state_unavailable(err.to_string()),
                ctx.request_id,
                ctx.instance,
            )),
        },
        "/api/v2/library/trash" => match ctx.app.load_state_view() {
            Ok(state) => {
                let items: Vec<Value> = state
                    .get("trash")
                    .and_then(Value::as_array)
                    .map(|arr| arr.iter().rev().map(trash_entry_public).collect())
                    .unwrap_or_default();
                let count = items.len();
                Some(json_response(
                    StatusCode::OK,
                    &json!({"items": items, "count": count}),
                    ctx.instance,
                ))
            }
            Err(err) => Some(api_error_response(
                &ApiError::state_unavailable(err.to_string()),
                ctx.request_id,
                ctx.instance,
            )),
        },
        "/api/v2/related" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(err) => {
                    return Some(api_error_response(
                        &ApiError::state_unavailable(err.to_string()),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let mut qmap = Map::new();
            for k in ["game_id", "id", "index", "name"] {
                if let Some(v) = ctx.query_first(k) {
                    if (k == "id" || k == "index")
                        && let Ok(n) = v.trim().parse::<i64>()
                    {
                        qmap.insert(k.to_owned(), json!(n));
                        continue;
                    }
                    qmap.insert(k.to_owned(), Value::String(v));
                }
            }
            let games = state
                .get("games")
                .and_then(Value::as_array)
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            match find_game_index(games, &qmap) {
                Ok(index) => {
                    let ids = related_game_ids(games, index, 8);
                    Some(json_response(
                        StatusCode::OK,
                        &json!({"ids": ids}),
                        ctx.instance,
                    ))
                }
                Err(_) => Some(api_error_response(
                    &ApiError::game_not_found("Game not found"),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/discovery" => match ctx.app.load_state_view() {
            Ok(state) => {
                let games = state
                    .get("games")
                    .and_then(Value::as_array)
                    .map(Vec::as_slice)
                    .unwrap_or(&[]);
                let mut payload = discovery_lists(games, 12);
                openbox_plugins::discovery::merge_plugin_discovery(
                    &mut payload,
                    games,
                    &ctx.app.data_dir.join("plugins"),
                    12,
                    Some(&state),
                    None,
                    Some(&ctx.app.root_dir),
                );
                Some(json_response(StatusCode::OK, &payload, ctx.instance))
            }
            Err(err) => Some(state_error_response(&err, ctx.request_id, ctx.instance)),
        },
        "/api/v2/related/rich" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(err) => {
                    return Some(api_error_response(
                        &ApiError::state_unavailable(err.to_string()),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let mut qmap = Map::new();
            for k in ["game_id", "id", "index", "name"] {
                if let Some(v) = ctx.query_first(k) {
                    if (k == "id" || k == "index")
                        && let Ok(n) = v.trim().parse::<i64>()
                    {
                        qmap.insert(k.to_owned(), json!(n));
                        continue;
                    }
                    qmap.insert(k.to_owned(), Value::String(v));
                }
            }
            let games = state
                .get("games")
                .and_then(Value::as_array)
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            match find_game_index(games, &qmap) {
                Ok(index) => {
                    let items = related_with_reasons(games, index, 8);
                    Some(json_response(
                        StatusCode::OK,
                        &json!({"items": items}),
                        ctx.instance,
                    ))
                }
                Err(_) => Some(api_error_response(
                    &ApiError::game_not_found("Game not found"),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/filter-presets" => match ctx.app.load_state_view() {
            Ok(state) => Some(json_response(
                StatusCode::OK,
                &json!({
                    "presets": list_presets(&state),
                    "bigbox_quick": bigbox_quick_presets(&state, 8),
                }),
                ctx.instance,
            )),
            Err(err) => Some(api_error_response(
                &ApiError::state_unavailable(err.to_string()),
                ctx.request_id,
                ctx.instance,
            )),
        },
        "/api/v2/webhooks" => match ctx.app.load_state_view() {
            Ok(state) => {
                let settings = state.get("settings");
                let attempts = settings
                    .and_then(|s| s.get("webhook_attempts"))
                    .and_then(Value::as_i64)
                    .unwrap_or(DEFAULT_ATTEMPTS as i64);
                let timeout = settings
                    .and_then(|s| s.get("webhook_timeout"))
                    .and_then(Value::as_i64)
                    .unwrap_or(DEFAULT_TIMEOUT as i64);
                Some(json_response(
                    StatusCode::OK,
                    &json!({
                        "webhooks": public_webhook_configs(&state),
                        "events": EVENT_TYPES,
                        "attempts": attempts,
                        "timeout": timeout,
                    }),
                    ctx.instance,
                ))
            }
            Err(err) => Some(api_error_response(
                &ApiError::state_unavailable(err.to_string()),
                ctx.request_id,
                ctx.instance,
            )),
        },
        _ => None,
    }
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/favorite" => {
            let mut fav_res: Result<bool, ApiError> = Ok(false);
            let tx = ctx.app.transact_local_state(|state| {
                let Some(Value::Array(games)) = state.get_mut("games") else {
                    fav_res = Err(ApiError::game_not_found("Game not found."));
                    return Err(openbox_store::StateError::Corrupt("not found".into()));
                };
                match find_game_index(games, payload) {
                    Ok(idx) => {
                        let next = !games[idx]
                            .get("favorite")
                            .and_then(Value::as_bool)
                            .unwrap_or(false);
                        if let Some(m) = games[idx].as_object_mut() {
                            m.insert("favorite".into(), Value::Bool(next));
                        }
                        fav_res = Ok(next);
                        Ok(())
                    }
                    Err(err) => {
                        fav_res = Err(err);
                        Err(openbox_store::StateError::Corrupt("not found".into()))
                    }
                }
            });
            match (tx, fav_res) {
                (_, Err(err)) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
                (Err(err), _) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
                (Ok(_), Ok(fav)) => Some(json_response(
                    StatusCode::OK,
                    &json!({"favorite": fav}),
                    ctx.instance,
                )),
            }
        }
        "/api/v2/game" => {
            let state_snap = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(err) => {
                    return Some(api_error_response(
                        &ApiError::state_unavailable(err.to_string()),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let source = payload
                .get("game")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            let allowed = allowed_progress(&state_snap);
            let mut game = match clean_game_fields(&source, &allowed) {
                Ok(g) => g,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let apps = match clean_extras(source.get("applications"), true) {
                Ok(v) => v,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let vers = match clean_extras(source.get("versions"), true) {
                Ok(v) => v,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let mut docs = match clean_extras(source.get("documents"), false) {
                Ok(v) => v,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            for &field in MEDIA_PATH_FIELDS {
                if let Some(p) = game
                    .get(field)
                    .and_then(Value::as_str)
                    .map(ToOwned::to_owned)
                    && !p.is_empty()
                {
                    match ctx.app.media_probe.approved_media_path(&p, false) {
                        Ok(ap) => {
                            game.insert(
                                field.to_owned(),
                                Value::String(ap.to_string_lossy().into_owned()),
                            );
                        }
                        Err(e) => {
                            return Some(api_error_response(
                                &ApiError::bad_request(e),
                                ctx.request_id,
                                ctx.instance,
                            ));
                        }
                    }
                }
            }
            for doc in &mut docs {
                if let Some(p) = doc
                    .get("path")
                    .and_then(Value::as_str)
                    .map(ToOwned::to_owned)
                {
                    match ctx.app.media_probe.approved_media_path(&p, false) {
                        Ok(ap) => {
                            if let Some(m) = doc.as_object_mut() {
                                m.insert(
                                    "path".into(),
                                    Value::String(ap.to_string_lossy().into_owned()),
                                );
                            }
                        }
                        Err(e) => {
                            return Some(api_error_response(
                                &ApiError::bad_request(e),
                                ctx.request_id,
                                ctx.instance,
                            ));
                        }
                    }
                }
            }
            game.insert("applications".into(), Value::Array(apps));
            game.insert("versions".into(), Value::Array(vers));
            game.insert("documents".into(), Value::Array(docs));
            let settings = state_snap
                .get("settings")
                .cloned()
                .unwrap_or_else(|| json!({}));
            drop(state_snap);
            if let Err(err) = clean_game_lists_and_misc(ctx, &mut game, &source, &settings) {
                return Some(api_error_response(&err, ctx.request_id, ctx.instance));
            }
            if game
                .get("name")
                .and_then(Value::as_str)
                .is_none_or(str::is_empty)
            {
                return Some(api_error_response(
                    &ApiError::bad_request("Name is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let raw_path = game
                .get("path")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim()
                .to_owned();
            if raw_path.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("Path is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let cand = expand_user(Path::new(&raw_path));
            if !cand.is_absolute() {
                return Some(api_error_response(
                    &ApiError::bad_request("Game path must be an absolute path."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if user_symlink_component(&cand).is_some() {
                return Some(api_error_response(
                    &ApiError::bad_request("Game path may not contain symlinks."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if !cand.exists() {
                return Some(api_error_response(
                    &ApiError::bad_request("Path must point to an existing local file."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if !cand.is_file() {
                return Some(api_error_response(
                    &ApiError::bad_request("Game path must be a regular file."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            game.insert(
                "path".into(),
                Value::String(cand.to_string_lossy().into_owned()),
            );

            let is_new = payload.get("id").is_none_or(Value::is_null)
                && str_field(payload, "game_id").is_empty();
            let mut mut_err: Option<ApiError> = None;
            let tx = ctx.app.transact_state(|state| {
                let Some(Value::Array(games)) = state.get_mut("games") else {
                    return Ok(());
                };
                if is_new {
                    let mut g = game.clone();
                    g.insert(
                        "added_at".into(),
                        Value::String(
                            jiff::Timestamp::now()
                                .strftime("%Y-%m-%dT%H:%M:%S")
                                .to_string(),
                        ),
                    );
                    games.push(Value::Object(g));
                } else {
                    match find_game_index(games, payload) {
                        Ok(idx) => {
                            if let Some(existing) = games[idx].as_object_mut() {
                                if !game.contains_key("price_paid_cents") {
                                    existing.remove("price_paid_cents");
                                    existing.remove("price_currency");
                                }
                                let existing_gid = existing
                                    .get("game_id")
                                    .cloned()
                                    .unwrap_or_else(|| json!(""));
                                for (k, v) in &game {
                                    existing.insert(k.clone(), v.clone());
                                }
                                existing.insert("game_id".into(), existing_gid);
                            }
                        }
                        Err(err) => {
                            mut_err = Some(ApiError::bad_request(err.message));
                            return Err(openbox_store::StateError::Corrupt("not found".into()));
                        }
                    }
                }
                Ok(())
            });
            if let Some(err) = mut_err {
                return Some(api_error_response(&err, ctx.request_id, ctx.instance));
            }
            if let Err(err) = tx {
                return Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            ctx.app.media_probe.clear_caches();
            Some(json_response(
                StatusCode::OK,
                &json!({"ok": true}),
                ctx.instance,
            ))
        }
        "/api/v2/game/delete" => {
            let delete_media = bool_field(payload, "delete_media");
            let mut media_paths = Vec::new();
            let mut referenced = HashSet::new();
            let mut removed_name = String::new();
            let mut mut_err: Option<ApiError> = None;
            let tx = ctx.app.transact_state(|state| {
                let Some(Value::Array(games)) = state.get_mut("games") else {
                    mut_err = Some(ApiError::game_not_found("Game not found."));
                    return Err(openbox_store::StateError::Corrupt("not found".into()));
                };
                match find_game_index(games, payload) {
                    Ok(idx) => {
                        let game = games.remove(idx);
                        removed_name = game
                            .get("name")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_owned();
                        if delete_media {
                            media_paths = game_media_paths(&game);
                            for other in games.iter() {
                                for p in game_media_paths(other) {
                                    let canon = fs::canonicalize(&p)
                                        .unwrap_or_else(|_| Path::new(&p).to_path_buf())
                                        .to_string_lossy()
                                        .into_owned();
                                    referenced.insert(canon);
                                }
                            }
                        }
                        Ok(())
                    }
                    Err(err) => {
                        mut_err = Some(err);
                        Err(openbox_store::StateError::Corrupt("not found".into()))
                    }
                }
            });
            if let Some(err) = mut_err {
                return Some(api_error_response(&err, ctx.request_id, ctx.instance));
            }
            if let Err(err) = tx {
                return Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let (deleted_media, shared_media) = if delete_media {
                let pair = remove_media_files(ctx, &media_paths, &referenced);
                ctx.app.bump_media_epoch();
                pair
            } else {
                (Vec::new(), Vec::new())
            };
            ctx.app.media_probe.clear_caches();
            Some(json_response(
                StatusCode::OK,
                &json!({
                    "removed": removed_name,
                    "deleted_media": deleted_media,
                    "shared_media": shared_media,
                }),
                ctx.instance,
            ))
        }
        "/api/v2/games/delete-steam" => {
            let mut removed = 0usize;
            let mut left = 0usize;
            match ctx.app.transact_state(|state| {
                let games = state
                    .get("games")
                    .and_then(Value::as_array)
                    .cloned()
                    .unwrap_or_default();
                let mut existing_trash = state
                    .get("trash")
                    .and_then(Value::as_array)
                    .cloned()
                    .unwrap_or_default();
                let pruned = prune_trash(&existing_trash, None);
                let room = TRASH_CAP.saturating_sub(pruned.len());
                let steam_indices: Vec<usize> = games
                    .iter()
                    .enumerate()
                    .filter(|(_, g)| {
                        g.get("source")
                            .and_then(Value::as_str)
                            .is_some_and(|s| s.eq_ignore_ascii_case("steam"))
                    })
                    .map(|(i, _)| i)
                    .collect();
                let moving_indices: HashSet<usize> =
                    steam_indices.iter().take(room).copied().collect();
                removed = moving_indices.len();
                left = steam_indices.len().saturating_sub(removed);
                if removed == 0 {
                    return Ok(());
                }
                let mut member_keys = HashSet::new();
                for &idx in steam_indices.iter().take(room) {
                    let g = &games[idx];
                    existing_trash.push(trash_entry_for(state, g, idx));
                    if let Some(gid) = g.get("game_id").and_then(Value::as_str) {
                        member_keys.insert(gid.to_owned());
                    }
                    member_keys.insert(idx.to_string());
                }
                let kept: Vec<Value> = games
                    .into_iter()
                    .enumerate()
                    .filter(|(i, _)| !moving_indices.contains(i))
                    .map(|(_, g)| g)
                    .collect();
                if let Some(obj) = state.as_object_mut() {
                    obj.insert("games".into(), Value::Array(kept));
                    if let Some(Value::Array(pls)) = obj.get_mut("playlists") {
                        for pl in pls.iter_mut() {
                            if pl.get("type").and_then(Value::as_str) == Some("manual")
                                && let Some(Value::Array(members)) = pl.get_mut("members")
                            {
                                members.retain(|m| {
                                    let s = value_to_str(m);
                                    !member_keys.contains(&s)
                                });
                            }
                        }
                    }
                    obj.insert(
                        "trash".into(),
                        Value::Array(prune_trash(&existing_trash, None)),
                    );
                }
                Ok(())
            }) {
                Ok(_) => Some(json_response(
                    StatusCode::OK,
                    &json!({"removed": removed, "left": left}),
                    ctx.instance,
                )),
                Err(err) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/games/bulk" => {
            let touches_catalog = payload
                .get("changes")
                .and_then(Value::as_object)
                .is_none_or(|changes| {
                    changes.keys().any(|k| {
                        openbox_sync::library_sync::CATALOG_FIELDS.contains(&k.as_str())
                            || k == "tags_add"
                            || k == "tags_remove"
                    })
                });
            if let Some(changes) = payload.get("changes").and_then(Value::as_object)
                && changes.contains_key("progress")
            {
                let state_snap = match ctx.app.load_state_view() {
                    Ok(s) => s,
                    Err(err) => {
                        return Some(state_error_response(&err, ctx.request_id, ctx.instance));
                    }
                };
                let prog = canon_progress(changes.get("progress"));
                if !prog.is_empty() && !allowed_progress(&state_snap).contains(&prog) {
                    return Some(api_error_response(
                        &ApiError::bad_request(
                            "Progress value is not allowed by the current progress whitelist.",
                        ),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            }
            let migrate_media = payload.get("migrate_media") == Some(&Value::Bool(true));
            let data_dir = ctx.app.data_dir.clone();
            let mut bulk_res: Result<(usize, usize, Vec<Value>), ApiError> = Ok((0, 0, Vec::new()));
            let mut run_mut = |state: &mut Value| {
                bulk_res = bulk_update_with_media(
                    state,
                    payload.get("ids"),
                    payload.get("changes"),
                    migrate_media,
                    &data_dir,
                );
                if let Err(ref err) = bulk_res {
                    return Err(openbox_store::StateError::Corrupt(err.message.clone()));
                }
                Ok(())
            };
            let tx = if touches_catalog {
                ctx.app.transact_state(&mut run_mut)
            } else {
                ctx.app.transact_local_state(&mut run_mut)
            };
            match (tx, bulk_res) {
                (_, Err(err)) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
                (Err(err), _) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
                (Ok(_), Ok((changed, moved, errors))) => Some(json_response(
                    StatusCode::OK,
                    &json!({"updated": changed, "media_moved": moved, "media_errors": errors}),
                    ctx.instance,
                )),
            }
        }
        "/api/v2/games/bulk-wizard" => {
            let changes_map =
                match bulk_wizard_changes(payload.get("changes").unwrap_or(&json!({}))) {
                    Ok(m) => m,
                    Err(err) => {
                        return Some(api_error_response(&err, ctx.request_id, ctx.instance));
                    }
                };
            let fields: Vec<String> = changes_map.keys().cloned().collect();
            let changes_val = Value::Object(changes_map);
            let migrate_media = payload.get("migrate_media") == Some(&Value::Bool(true));
            let data_dir = ctx.app.data_dir.clone();
            let mut bulk_res: Result<(usize, usize, Vec<Value>), ApiError> = Ok((0, 0, Vec::new()));
            let tx = ctx.app.transact_state(|state| {
                bulk_res = bulk_update_with_media(
                    state,
                    payload.get("ids"),
                    Some(&changes_val),
                    migrate_media,
                    &data_dir,
                );
                if let Err(ref err) = bulk_res {
                    return Err(openbox_store::StateError::Corrupt(err.message.clone()));
                }
                Ok(())
            });
            match (tx, bulk_res) {
                (_, Err(err)) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
                (Err(err), _) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
                (Ok(_), Ok((changed, moved, errors))) => Some(json_response(
                    StatusCode::OK,
                    &json!({
                        "updated": changed,
                        "fields": fields,
                        "media_moved": moved,
                        "media_errors": errors,
                    }),
                    ctx.instance,
                )),
            }
        }
        "/api/v2/bulk/find-replace" => {
            let dry_run = payload.get("dry_run") != Some(&Value::Bool(false));
            let ids = payload
                .get("ids")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            let field = str_field(payload, "field").to_owned();
            let find = payload
                .get("find")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            let replace = payload
                .get("replace")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            if dry_run {
                let state = match ctx.app.load_state_view() {
                    Ok(s) => s,
                    Err(err) => {
                        return Some(api_error_response(
                            &ApiError::state_unavailable(err.to_string()),
                            ctx.request_id,
                            ctx.instance,
                        ));
                    }
                };
                let mut games = state
                    .get("games")
                    .and_then(Value::as_array)
                    .cloned()
                    .unwrap_or_default();
                match find_replace(&mut games, &ids, &field, &find, &replace, true) {
                    Ok(res) => Some(json_response(StatusCode::OK, &res, ctx.instance)),
                    Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
                }
            } else {
                let mut fr_res: Result<Value, ApiError> = Ok(Value::Null);
                let tx = ctx.app.transact_state(|state| {
                    if let Some(Value::Array(games)) = state.get_mut("games") {
                        fr_res = find_replace(games, &ids, &field, &find, &replace, false);
                    }
                    if let Err(ref err) = fr_res {
                        return Err(openbox_store::StateError::Corrupt(err.message.clone()));
                    }
                    Ok(())
                });
                match (tx, fr_res) {
                    (_, Err(err)) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
                    (Err(err), _) => Some(api_error_response(
                        &ApiError::bad_request(err.to_string()),
                        ctx.request_id,
                        ctx.instance,
                    )),
                    (Ok(_), Ok(res)) => Some(json_response(StatusCode::OK, &res, ctx.instance)),
                }
            }
        }
        "/api/v2/platform/rename" => {
            let old = str_field(payload, "old").to_owned();
            let new = str_field(payload, "new").to_owned();
            let migrate_media = bool_field(payload, "migrate_media");
            let data_dir = ctx.app.data_dir.clone();
            let mut res: Result<Value, ApiError> = Ok(Value::Null);
            let tx = ctx.app.transact_state(|state| {
                res = rename_platform(state, &old, &new, migrate_media, &data_dir);
                if let Err(ref err) = res {
                    return Err(openbox_store::StateError::Corrupt(err.message.clone()));
                }
                Ok(())
            });
            match (tx, res) {
                (_, Err(err)) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
                (Err(err), _) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
                (Ok(_), Ok(val)) => Some(json_response(StatusCode::OK, &val, ctx.instance)),
            }
        }
        "/api/v2/platform/delete" => {
            let name = str_field(payload, "name").to_owned();
            let confirm = payload.get("confirm").and_then(Value::as_str).unwrap_or("");
            if confirm != name {
                return Some(api_error_response(
                    &ApiError::bad_request("Type the platform name to confirm deletion."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let delete_media = bool_field(payload, "delete_media");
            let data_dir = ctx.app.data_dir.clone();
            let mut res: Result<Value, ApiError> = Ok(Value::Null);
            let tx = ctx.app.transact_state(|state| {
                res = delete_platform(state, &name, delete_media, &data_dir);
                if let Err(ref err) = res {
                    return Err(openbox_store::StateError::Corrupt(err.message.clone()));
                }
                Ok(())
            });
            match (tx, res) {
                (_, Err(err)) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
                (Err(err), _) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
                (Ok(_), Ok(val)) => Some(json_response(StatusCode::OK, &val, ctx.instance)),
            }
        }
        "/api/v2/game/store-action" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(err) => {
                    return Some(api_error_response(
                        &ApiError::state_unavailable(err.to_string()),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let games = state
                .get("games")
                .and_then(Value::as_array)
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            let idx = match find_game_index(games, payload) {
                Ok(i) => i,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let game = &games[idx];
            let app_id = game
                .get("steam_app_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim();
            if app_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("This game has no Steam app id."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let action = {
                let a = str_field(payload, "action");
                if a.is_empty() { "install" } else { a }
            };
            if !matches!(action, "install" | "repair") {
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
                &json!({
                    "game_id": game.get("game_id").cloned().unwrap_or(Value::Null),
                    "app_id": app_id,
                    "action": action,
                }),
                ctx.instance,
            ))
        }
        "/api/v2/queue" => {
            let action = {
                let a = str_field(payload, "action");
                if a.is_empty() { "list" } else { a }
            }
            .to_owned();
            let game_ids: Vec<String> = payload
                .get("game_ids")
                .and_then(Value::as_array)
                .map(|a| a.iter().map(value_to_str).collect())
                .unwrap_or_default();
            let ordered: Vec<String> = payload
                .get("ordered_game_ids")
                .and_then(Value::as_array)
                .map(|a| a.iter().map(value_to_str).collect())
                .unwrap_or_default();
            let position = payload
                .get("position")
                .and_then(Value::as_i64)
                .map(|n| n as isize);
            let note = str_field(payload, "note").to_owned();
            let current_gid = payload
                .get("current_game_id")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            let mut next_item: Option<Value> = None;
            let mut err_out: Option<ApiError> = None;
            let tx = ctx.app.transact_state(|state| {
                match action.as_str() {
                    "enqueue" => {
                        if let Err(e) = enqueue_queue(state, &game_ids, position, &note) {
                            err_out = Some(ApiError::bad_request(e));
                            return Err(openbox_store::StateError::Corrupt("queue".into()));
                        }
                    }
                    "remove" => {
                        remove_queue(state, &game_ids);
                    }
                    "reorder" => {
                        if let Err(e) = reorder_queue(state, &ordered) {
                            err_out = Some(ApiError::bad_request(e));
                            return Err(openbox_store::StateError::Corrupt("queue".into()));
                        }
                    }
                    "advance" => {
                        next_item = advance_queue(state, current_gid.as_deref());
                    }
                    "list" | "resolve" => {}
                    _ => {
                        err_out = Some(ApiError::bad_request("Unknown queue action."));
                        return Err(openbox_store::StateError::Corrupt("queue".into()));
                    }
                }
                Ok(())
            });
            if let Some(err) = err_out {
                return Some(api_error_response(&err, ctx.request_id, ctx.instance));
            }
            match tx {
                Ok((committed, _)) => Some(json_response(
                    StatusCode::OK,
                    &json!({
                        "queue": resolve_queue(&committed),
                        "next": if action == "advance" { next_item.unwrap_or(Value::Null) } else { Value::Null },
                    }),
                    ctx.instance,
                )),
                Err(err) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/notifications" => {
            let action = {
                let a = str_field(payload, "action");
                if a.is_empty() { "list" } else { a }
            }
            .to_owned();
            let ids: Option<Vec<String>> = payload
                .get("ids")
                .and_then(Value::as_array)
                .map(|a| a.iter().map(value_to_str).collect());
            if !matches!(action.as_str(), "read" | "clear" | "list") {
                return Some(api_error_response(
                    &ApiError::bad_request("Unknown notification action."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            match ctx.app.transact_state(|state| {
                match action.as_str() {
                    "read" => mark_read(state, ids.as_deref()),
                    "clear" => clear_notifications(state, ids.as_deref()),
                    _ => {}
                }
                Ok(unread_count(state))
            }) {
                Ok((committed, unread)) => Some(json_response(
                    StatusCode::OK,
                    &json!({
                        "notifications": committed.get("notifications").cloned().unwrap_or_else(|| json!([])),
                        "unread": unread,
                    }),
                    ctx.instance,
                )),
                Err(err) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/tags" => {
            let mut changes = Map::new();
            for k in ["tags", "tags_add", "tags_remove"] {
                if let Some(v) = payload.get(k) {
                    changes.insert(k.to_owned(), v.clone());
                }
            }
            if changes.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("No tag changes were supplied."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let changes_val = Value::Object(changes);
            let mut updated_res: Result<usize, ApiError> = Ok(0);
            let tx = ctx.app.transact_state(|state| {
                if let Some(Value::Array(games)) = state.get_mut("games") {
                    updated_res = bulk_update(games, payload.get("ids"), Some(&changes_val));
                }
                if let Err(ref err) = updated_res {
                    return Err(openbox_store::StateError::Corrupt(err.message.clone()));
                }
                Ok(())
            });
            match (tx, updated_res) {
                (_, Err(err)) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
                (Err(err), _) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
                (Ok((committed, _)), Ok(updated)) => {
                    let games = committed
                        .get("games")
                        .and_then(Value::as_array)
                        .map(Vec::as_slice)
                        .unwrap_or(&[]);
                    Some(json_response(
                        StatusCode::OK,
                        &json!({"updated": updated, "tags": tag_counts(games)}),
                        ctx.instance,
                    ))
                }
            }
        }
        "/api/v2/image-group" => {
            let group = payload.get("group").and_then(Value::as_str).unwrap_or("");
            let scope = payload
                .get("scope")
                .and_then(Value::as_str)
                .unwrap_or("global");
            let name = str_field(payload, "name").to_owned();
            let valid_group = matches!(
                group,
                "default"
                    | "cover"
                    | "background"
                    | "screenshot"
                    | "clear_logo"
                    | "fanart"
                    | "banner"
                    | "icon"
                    | "box_back"
                    | "box_spine"
                    | "box_3d"
                    | "title_screen"
                    | "cart_front"
                    | "cart_back"
                    | "disc"
                    | "advertisement"
                    | "manual"
            );
            if !valid_group || !matches!(scope, "global" | "platform" | "playlist") {
                return Some(api_error_response(
                    &ApiError::bad_request("Unknown image group."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if scope != "global" && (name.is_empty() || name.len() > 200) {
                return Some(api_error_response(
                    &ApiError::bad_request("A platform or playlist is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let g_owned = group.to_owned();
            let s_owned = scope.to_owned();
            match ctx.app.transact_state(|state| {
                if let Some(obj) = state.as_object_mut() {
                    let settings = obj
                        .entry("settings".to_owned())
                        .or_insert_with(|| Value::Object(Map::new()));
                    if let Some(sm) = settings.as_object_mut() {
                        if s_owned == "global" {
                            sm.insert(
                                "image_group".into(),
                                Value::String(
                                    if g_owned == "default" {
                                        "cover"
                                    } else {
                                        g_owned.as_str()
                                    }
                                    .to_owned(),
                                ),
                            );
                        } else {
                            let map_key = format!("image_group_by_{s_owned}");
                            let map = sm
                                .entry(map_key)
                                .or_insert_with(|| Value::Object(Map::new()));
                            if let Some(mm) = map.as_object_mut() {
                                if g_owned == "default" {
                                    mm.remove(&name);
                                } else {
                                    mm.insert(name.clone(), Value::String(g_owned.clone()));
                                }
                            }
                        }
                    }
                }
                Ok(())
            }) {
                Ok((committed, _)) => {
                    let pub_s = public_settings(&ctx.app, &committed);
                    Some(json_response(StatusCode::OK, &pub_s, ctx.instance))
                }
                Err(err) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/library/manual-entry" => {
            let state_snap = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(err) => {
                    return Some(api_error_response(
                        &ApiError::state_unavailable(err.to_string()),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let source = payload
                .get("game")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            let mut game = match clean_game_fields(&source, &allowed_progress(&state_snap)) {
                Ok(g) => g,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let name = game
                .get("name")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            if name.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("Name is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            game.insert("manual_entry".into(), Value::Bool(true));
            game.insert("path".into(), Value::String(String::new()));
            let settings = state_snap
                .get("settings")
                .cloned()
                .unwrap_or_else(|| json!({}));
            drop(state_snap);
            if let Err(err) = clean_game_lists_and_misc(ctx, &mut game, &source, &settings) {
                return Some(api_error_response(&err, ctx.request_id, ctx.instance));
            }
            match ctx.app.transact_state(|state| {
                if let Some(obj) = state.as_object_mut() {
                    let games = obj
                        .entry("games".to_owned())
                        .or_insert_with(|| Value::Array(Vec::new()));
                    if let Some(arr) = games.as_array_mut() {
                        arr.push(Value::Object(game.clone()));
                    }
                }
                Ok(())
            }) {
                Ok(_) => {
                    ctx.app.media_probe.clear_caches();
                    Some(json_response(
                        StatusCode::OK,
                        &json!({"ok": true, "name": name}),
                        ctx.instance,
                    ))
                }
                Err(err) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/library/manual-entry/convert" => {
            let path_val = str_field(payload, "path");
            if path_val.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("path is required to convert a shelf entry."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let cand = expand_user(Path::new(path_val));
            if !cand.is_absolute() || !cand.is_file() {
                return Some(api_error_response(
                    &ApiError::bad_request("path must be an existing absolute file."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let mut gid_res: Result<String, ApiError> = Ok(String::new());
            let tx = ctx.app.transact_state(|state| {
                let Some(Value::Array(games)) = state.get_mut("games") else {
                    gid_res = Err(ApiError::game_not_found("Game not found."));
                    return Err(openbox_store::StateError::Corrupt("not found".into()));
                };
                match find_game_index(games, payload) {
                    Ok(idx) => {
                        if !games[idx]
                            .get("manual_entry")
                            .and_then(Value::as_bool)
                            .unwrap_or(false)
                        {
                            gid_res = Err(ApiError::bad_request("Game is not a shelf entry."));
                            return Err(openbox_store::StateError::Corrupt("not shelf".into()));
                        }
                        if let Some(m) = games[idx].as_object_mut() {
                            m.insert(
                                "path".into(),
                                Value::String(cand.to_string_lossy().into_owned()),
                            );
                            m.insert("manual_entry".into(), Value::Bool(false));
                            gid_res = Ok(m
                                .get("game_id")
                                .and_then(Value::as_str)
                                .unwrap_or("")
                                .to_owned());
                        }
                        Ok(())
                    }
                    Err(err) => {
                        gid_res = Err(err);
                        Err(openbox_store::StateError::Corrupt("not found".into()))
                    }
                }
            });
            match (tx, gid_res) {
                (_, Err(err)) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
                (Err(err), _) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
                (Ok(_), Ok(gid)) => {
                    ctx.app.media_probe.clear_caches();
                    Some(json_response(
                        StatusCode::OK,
                        &json!({"ok": true, "game_id": gid, "playable": true}),
                        ctx.instance,
                    ))
                }
            }
        }
        "/api/v2/library/manual-entry/update" => {
            let state_snap = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(err) => {
                    return Some(api_error_response(
                        &ApiError::state_unavailable(err.to_string()),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let source = payload
                .get("game")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            let mut game = match clean_game_fields(&source, &allowed_progress(&state_snap)) {
                Ok(g) => g,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            if game
                .get("name")
                .and_then(Value::as_str)
                .is_none_or(str::is_empty)
            {
                return Some(api_error_response(
                    &ApiError::bad_request("Name is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let settings = state_snap
                .get("settings")
                .cloned()
                .unwrap_or_else(|| json!({}));
            drop(state_snap);
            if let Err(err) = clean_game_lists_and_misc(ctx, &mut game, &source, &settings) {
                return Some(api_error_response(&err, ctx.request_id, ctx.instance));
            }
            let mut gid_res: Result<String, ApiError> = Ok(String::new());
            let tx = ctx.app.transact_state(|state| {
                let Some(Value::Array(games)) = state.get_mut("games") else {
                    gid_res = Err(ApiError::game_not_found("Game not found."));
                    return Err(openbox_store::StateError::Corrupt("not found".into()));
                };
                match find_game_index(games, payload) {
                    Ok(idx) => {
                        if !games[idx]
                            .get("manual_entry")
                            .and_then(Value::as_bool)
                            .unwrap_or(false)
                        {
                            gid_res = Err(ApiError::bad_request("Game is not a shelf entry."));
                            return Err(openbox_store::StateError::Corrupt("not shelf".into()));
                        }
                        if let Some(existing) = games[idx].as_object_mut() {
                            let gid = existing
                                .get("game_id")
                                .and_then(Value::as_str)
                                .unwrap_or("")
                                .to_owned();
                            for (k, v) in &game {
                                existing.insert(k.clone(), v.clone());
                            }
                            existing.insert("game_id".into(), Value::String(gid.clone()));
                            existing.insert("manual_entry".into(), Value::Bool(true));
                            existing.insert("path".into(), Value::String(String::new()));
                            gid_res = Ok(gid);
                        }
                        Ok(())
                    }
                    Err(err) => {
                        gid_res = Err(err);
                        Err(openbox_store::StateError::Corrupt("not found".into()))
                    }
                }
            });
            match (tx, gid_res) {
                (_, Err(err)) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
                (Err(err), _) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
                (Ok(_), Ok(gid)) => {
                    ctx.app.media_probe.clear_caches();
                    Some(json_response(
                        StatusCode::OK,
                        &json!({"ok": true, "game_id": gid, "manual_entry": true}),
                        ctx.instance,
                    ))
                }
            }
        }
        "/api/v2/library/playtime/log" => {
            let entry = match clean_playtime_entry(payload) {
                Ok(e) => e,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let mut gid_res: Result<String, ApiError> = Ok(String::new());
            let tx = ctx.app.transact_state(|state| {
                let Some(Value::Array(games)) = state.get_mut("games") else {
                    gid_res = Err(ApiError::game_not_found("Game not found."));
                    return Err(openbox_store::StateError::Corrupt("not found".into()));
                };
                match find_game_index(games, payload) {
                    Ok(idx) => {
                        if let Some(m) = games[idx].as_object_mut() {
                            let mut sessions = normalize_manual_sessions(m.get("manual_sessions"));
                            sessions.push(entry.clone());
                            m.insert("manual_sessions".into(), Value::Array(sessions));
                            resync_manual_playtime(m);
                            gid_res = Ok(m
                                .get("game_id")
                                .and_then(Value::as_str)
                                .unwrap_or("")
                                .to_owned());
                        }
                        Ok(())
                    }
                    Err(err) => {
                        gid_res = Err(err);
                        Err(openbox_store::StateError::Corrupt("not found".into()))
                    }
                }
            });
            match (tx, gid_res) {
                (_, Err(err)) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
                (Err(err), _) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
                (Ok(_), Ok(gid)) => Some(json_response(
                    StatusCode::OK,
                    &json!({"ok": true, "game_id": gid, "entry": entry}),
                    ctx.instance,
                )),
            }
        }
        "/api/v2/library/playtime/update" => {
            let Some(index) = payload.get("index").and_then(|v| {
                v.as_i64()
                    .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
            }) else {
                return Some(api_error_response(
                    &ApiError::bad_request("index must be an integer."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let entry = match clean_playtime_entry(payload) {
                Ok(e) => e,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let mut gid_res: Result<String, ApiError> = Ok(String::new());
            let tx = ctx.app.transact_state(|state| {
                let Some(Value::Array(games)) = state.get_mut("games") else {
                    gid_res = Err(ApiError::game_not_found("Game not found."));
                    return Err(openbox_store::StateError::Corrupt("not found".into()));
                };
                match find_game_index(games, payload) {
                    Ok(idx) => {
                        if let Some(m) = games[idx].as_object_mut() {
                            let mut sessions = normalize_manual_sessions(m.get("manual_sessions"));
                            if index < 0 || (index as usize) >= sessions.len() {
                                gid_res = Err(ApiError::bad_request("Unknown playtime entry."));
                                return Err(openbox_store::StateError::Corrupt("bad idx".into()));
                            }
                            sessions[index as usize] = entry.clone();
                            m.insert("manual_sessions".into(), Value::Array(sessions));
                            resync_manual_playtime(m);
                            gid_res = Ok(m
                                .get("game_id")
                                .and_then(Value::as_str)
                                .unwrap_or("")
                                .to_owned());
                        }
                        Ok(())
                    }
                    Err(err) => {
                        gid_res = Err(err);
                        Err(openbox_store::StateError::Corrupt("not found".into()))
                    }
                }
            });
            match (tx, gid_res) {
                (_, Err(err)) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
                (Err(err), _) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
                (Ok(_), Ok(gid)) => Some(json_response(
                    StatusCode::OK,
                    &json!({"ok": true, "game_id": gid, "entry": entry}),
                    ctx.instance,
                )),
            }
        }
        "/api/v2/library/playtime/delete" => {
            let Some(index) = payload.get("index").and_then(|v| {
                v.as_i64()
                    .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
            }) else {
                return Some(api_error_response(
                    &ApiError::bad_request("index must be an integer."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let mut gid_res: Result<String, ApiError> = Ok(String::new());
            let tx = ctx.app.transact_state(|state| {
                let Some(Value::Array(games)) = state.get_mut("games") else {
                    gid_res = Err(ApiError::game_not_found("Game not found."));
                    return Err(openbox_store::StateError::Corrupt("not found".into()));
                };
                match find_game_index(games, payload) {
                    Ok(idx) => {
                        if let Some(m) = games[idx].as_object_mut() {
                            let mut sessions = normalize_manual_sessions(m.get("manual_sessions"));
                            if index < 0 || (index as usize) >= sessions.len() {
                                gid_res = Err(ApiError::bad_request("Unknown playtime entry."));
                                return Err(openbox_store::StateError::Corrupt("bad idx".into()));
                            }
                            sessions.remove(index as usize);
                            m.insert("manual_sessions".into(), Value::Array(sessions));
                            resync_manual_playtime(m);
                            gid_res = Ok(m
                                .get("game_id")
                                .and_then(Value::as_str)
                                .unwrap_or("")
                                .to_owned());
                        }
                        Ok(())
                    }
                    Err(err) => {
                        gid_res = Err(err);
                        Err(openbox_store::StateError::Corrupt("not found".into()))
                    }
                }
            });
            match (tx, gid_res) {
                (_, Err(err)) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
                (Err(err), _) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
                (Ok(_), Ok(gid)) => Some(json_response(
                    StatusCode::OK,
                    &json!({"ok": true, "game_id": gid}),
                    ctx.instance,
                )),
            }
        }
        "/api/v2/library/notes/add" => {
            let text = str_field(payload, "text").to_owned();
            if text.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("Note text is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if text.chars().count() > 2000 {
                return Some(api_error_response(
                    &ApiError::bad_request("Note text is limited to 2000 characters."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let stamp = jiff::Timestamp::now()
                .strftime("%Y-%m-%dT%H:%M:%S")
                .to_string();
            let mut gid_res: Result<String, ApiError> = Ok(String::new());
            let tx = ctx.app.transact_state(|state| {
                let Some(Value::Array(games)) = state.get_mut("games") else {
                    gid_res = Err(ApiError::game_not_found("Game not found."));
                    return Err(openbox_store::StateError::Corrupt("not found".into()));
                };
                match find_game_index(games, payload) {
                    Ok(idx) => {
                        if let Some(m) = games[idx].as_object_mut() {
                            let mut entries = normalize_notes(m.get("notes"));
                            entries.push(json!({"ts": stamp, "text": text}));
                            m.insert("notes".into(), Value::Array(entries));
                            gid_res = Ok(m
                                .get("game_id")
                                .and_then(Value::as_str)
                                .unwrap_or("")
                                .to_owned());
                        }
                        Ok(())
                    }
                    Err(err) => {
                        gid_res = Err(err);
                        Err(openbox_store::StateError::Corrupt("not found".into()))
                    }
                }
            });
            match (tx, gid_res) {
                (_, Err(err)) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
                (Err(err), _) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
                (Ok(_), Ok(gid)) => Some(json_response(
                    StatusCode::OK,
                    &json!({"ok": true, "game_id": gid}),
                    ctx.instance,
                )),
            }
        }
        "/api/v2/library/notes/update" => {
            let Some(index) = payload.get("index").and_then(|v| {
                v.as_i64()
                    .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
            }) else {
                return Some(api_error_response(
                    &ApiError::bad_request("index must be an integer."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let text = str_field(payload, "text").to_owned();
            if text.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("Note text is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if text.chars().count() > 2000 {
                return Some(api_error_response(
                    &ApiError::bad_request("Note text is limited to 2000 characters."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let mut gid_res: Result<String, ApiError> = Ok(String::new());
            let tx = ctx.app.transact_state(|state| {
                let Some(Value::Array(games)) = state.get_mut("games") else {
                    gid_res = Err(ApiError::game_not_found("Game not found."));
                    return Err(openbox_store::StateError::Corrupt("not found".into()));
                };
                match find_game_index(games, payload) {
                    Ok(idx) => {
                        if let Some(m) = games[idx].as_object_mut() {
                            let mut entries = normalize_notes(m.get("notes"));
                            if index < 0 || (index as usize) >= entries.len() {
                                gid_res = Err(ApiError::bad_request("Unknown note entry."));
                                return Err(openbox_store::StateError::Corrupt("bad idx".into()));
                            }
                            let ts = entries[index as usize]
                                .get("ts")
                                .and_then(Value::as_str)
                                .unwrap_or("")
                                .to_owned();
                            entries[index as usize] = json!({"ts": ts, "text": text});
                            m.insert("notes".into(), Value::Array(entries));
                            gid_res = Ok(m
                                .get("game_id")
                                .and_then(Value::as_str)
                                .unwrap_or("")
                                .to_owned());
                        }
                        Ok(())
                    }
                    Err(err) => {
                        gid_res = Err(err);
                        Err(openbox_store::StateError::Corrupt("not found".into()))
                    }
                }
            });
            match (tx, gid_res) {
                (_, Err(err)) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
                (Err(err), _) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
                (Ok(_), Ok(gid)) => Some(json_response(
                    StatusCode::OK,
                    &json!({"ok": true, "game_id": gid}),
                    ctx.instance,
                )),
            }
        }
        "/api/v2/library/notes/delete" => {
            let Some(index) = payload.get("index").and_then(|v| {
                v.as_i64()
                    .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
            }) else {
                return Some(api_error_response(
                    &ApiError::bad_request("index must be an integer."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let mut gid_res: Result<String, ApiError> = Ok(String::new());
            let tx = ctx.app.transact_state(|state| {
                let Some(Value::Array(games)) = state.get_mut("games") else {
                    gid_res = Err(ApiError::game_not_found("Game not found."));
                    return Err(openbox_store::StateError::Corrupt("not found".into()));
                };
                match find_game_index(games, payload) {
                    Ok(idx) => {
                        if let Some(m) = games[idx].as_object_mut() {
                            let mut entries = normalize_notes(m.get("notes"));
                            if index < 0 || (index as usize) >= entries.len() {
                                gid_res = Err(ApiError::bad_request("Unknown note entry."));
                                return Err(openbox_store::StateError::Corrupt("bad idx".into()));
                            }
                            entries.remove(index as usize);
                            m.insert("notes".into(), Value::Array(entries));
                            gid_res = Ok(m
                                .get("game_id")
                                .and_then(Value::as_str)
                                .unwrap_or("")
                                .to_owned());
                        }
                        Ok(())
                    }
                    Err(err) => {
                        gid_res = Err(err);
                        Err(openbox_store::StateError::Corrupt("not found".into()))
                    }
                }
            });
            match (tx, gid_res) {
                (_, Err(err)) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
                (Err(err), _) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
                (Ok(_), Ok(gid)) => Some(json_response(
                    StatusCode::OK,
                    &json!({"ok": true, "game_id": gid}),
                    ctx.instance,
                )),
            }
        }
        "/api/v2/library/progress/set" => {
            let state_snap = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(err) => {
                    return Some(api_error_response(
                        &ApiError::state_unavailable(err.to_string()),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let progress = canon_progress(payload.get("progress"));
            if !is_valid_progress(&progress) {
                return Some(api_error_response(
                    &ApiError::bad_request("Unknown progress value."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if !progress.is_empty() && !allowed_progress(&state_snap).contains(&progress) {
                return Some(api_error_response(
                    &ApiError::bad_request(
                        "Progress value is not allowed by the current progress whitelist.",
                    ),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let mut gid_res: Result<String, ApiError> = Ok(String::new());
            let tx = ctx.app.transact_state(|state| {
                let Some(Value::Array(games)) = state.get_mut("games") else {
                    gid_res = Err(ApiError::game_not_found("Game not found."));
                    return Err(openbox_store::StateError::Corrupt("not found".into()));
                };
                match find_game_index(games, payload) {
                    Ok(idx) => {
                        if let Some(m) = games[idx].as_object_mut() {
                            m.insert("progress".into(), Value::String(progress.clone()));
                            m.insert("progress_suggested".into(), Value::Bool(true));
                            gid_res = Ok(m
                                .get("game_id")
                                .and_then(Value::as_str)
                                .unwrap_or("")
                                .to_owned());
                        }
                        Ok(())
                    }
                    Err(err) => {
                        gid_res = Err(err);
                        Err(openbox_store::StateError::Corrupt("not found".into()))
                    }
                }
            });
            match (tx, gid_res) {
                (_, Err(err)) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
                (Err(err), _) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
                (Ok(_), Ok(gid)) => Some(json_response(
                    StatusCode::OK,
                    &json!({"ok": true, "game_id": gid, "progress": progress}),
                    ctx.instance,
                )),
            }
        }
        "/api/v2/library/rating/set" => {
            let rating = match clean_user_rating(payload.get("user_rating")) {
                Ok(r) => r,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let mut gid_res: Result<String, ApiError> = Ok(String::new());
            let tx = ctx.app.transact_state(|state| {
                let Some(Value::Array(games)) = state.get_mut("games") else {
                    gid_res = Err(ApiError::game_not_found("Game not found."));
                    return Err(openbox_store::StateError::Corrupt("not found".into()));
                };
                match find_game_index(games, payload) {
                    Ok(idx) => {
                        if let Some(m) = games[idx].as_object_mut() {
                            m.insert("user_rating".into(), json!(rating));
                            gid_res = Ok(m
                                .get("game_id")
                                .and_then(Value::as_str)
                                .unwrap_or("")
                                .to_owned());
                        }
                        Ok(())
                    }
                    Err(err) => {
                        gid_res = Err(err);
                        Err(openbox_store::StateError::Corrupt("not found".into()))
                    }
                }
            });
            match (tx, gid_res) {
                (_, Err(err)) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
                (Err(err), _) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
                (Ok(_), Ok(gid)) => Some(json_response(
                    StatusCode::OK,
                    &json!({"ok": true, "game_id": gid, "user_rating": rating}),
                    ctx.instance,
                )),
            }
        }
        "/api/v2/library/trash" => {
            let delete_media = bool_field(payload, "delete_media");
            let mut media_paths = Vec::new();
            let mut referenced = HashSet::new();
            let mut entry_res: Result<Value, ApiError> = Ok(Value::Null);
            let tx = ctx.app.transact_state(|state| {
                let idx = {
                    let Some(games) = state.get("games").and_then(Value::as_array) else {
                        entry_res = Err(ApiError::game_not_found("Game not found."));
                        return Err(openbox_store::StateError::Corrupt("not found".into()));
                    };
                    match find_game_index(games, payload) {
                        Ok(i) => i,
                        Err(err) => {
                            entry_res = Err(err);
                            return Err(openbox_store::StateError::Corrupt("not found".into()));
                        }
                    }
                };
                let entry = {
                    let game = &state["games"][idx];
                    if delete_media {
                        media_paths = game_media_paths(game);
                    }
                    trash_entry_for(state, game, idx)
                };
                let mut member_keys = HashSet::new();
                if let Some(gid) = entry.get("game_id").and_then(Value::as_str) {
                    member_keys.insert(gid.to_owned());
                }
                member_keys.insert(idx.to_string());
                if let Some(Value::Array(games)) = state.get_mut("games") {
                    games.remove(idx);
                    if delete_media {
                        for other in games.iter() {
                            for p in game_media_paths(other) {
                                let canon = fs::canonicalize(&p)
                                    .unwrap_or_else(|_| Path::new(&p).to_path_buf())
                                    .to_string_lossy()
                                    .into_owned();
                                referenced.insert(canon);
                            }
                        }
                    }
                }
                if let Some(Value::Array(pls)) = state.get_mut("playlists") {
                    for pl in pls.iter_mut() {
                        if pl.get("type").and_then(Value::as_str) == Some("manual")
                            && let Some(Value::Array(members)) = pl.get_mut("members")
                        {
                            members.retain(|m| !member_keys.contains(&value_to_str(m)));
                        }
                    }
                }
                if let Some(obj) = state.as_object_mut() {
                    let mut trash = obj
                        .get("trash")
                        .and_then(Value::as_array)
                        .cloned()
                        .unwrap_or_default();
                    trash.push(entry.clone());
                    obj.insert("trash".into(), Value::Array(prune_trash(&trash, None)));
                }
                entry_res = Ok(entry);
                Ok(())
            });
            match (tx, entry_res) {
                (_, Err(err)) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
                (Err(err), _) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
                (Ok(_), Ok(entry)) => {
                    let (deleted_media, shared_media) = if delete_media {
                        let pair = remove_media_files(ctx, &media_paths, &referenced);
                        ctx.app.bump_media_epoch();
                        pair
                    } else {
                        (Vec::new(), Vec::new())
                    };
                    ctx.app.media_probe.clear_caches();
                    Some(json_response(
                        StatusCode::OK,
                        &json!({
                            "ok": true,
                            "trash_id": entry["trash_id"],
                            "game_id": entry["game_id"],
                            "name": entry["name"],
                            "deleted_media": deleted_media,
                            "shared_media": shared_media,
                        }),
                        ctx.instance,
                    ))
                }
            }
        }
        "/api/v2/library/trash/restore" => {
            let trash_id = str_field(payload, "trash_id").to_owned();
            if trash_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("trash_id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let mut res: Result<Value, ApiError> = Ok(Value::Null);
            let tx = ctx.app.transact_state(|state| {
                let trash = state
                    .get("trash")
                    .and_then(Value::as_array)
                    .cloned()
                    .unwrap_or_default();
                let Some(pos) = trash.iter().position(|e| {
                    e.get("trash_id").and_then(Value::as_str) == Some(trash_id.as_str())
                }) else {
                    res = Err(ApiError::bad_request("Trash entry not found."));
                    return Err(openbox_store::StateError::Corrupt("not found".into()));
                };
                let entry = trash[pos].clone();
                let mut restored = entry
                    .get("game")
                    .and_then(Value::as_object)
                    .cloned()
                    .unwrap_or_default();
                let used: HashSet<String> = state
                    .get("games")
                    .and_then(Value::as_array)
                    .map(|arr| {
                        arr.iter()
                            .filter_map(|g| {
                                g.get("game_id")
                                    .and_then(Value::as_str)
                                    .map(ToOwned::to_owned)
                            })
                            .collect()
                    })
                    .unwrap_or_default();
                let original = entry
                    .get("game_id")
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
                    .or_else(|| restored.get("game_id").and_then(Value::as_str))
                    .unwrap_or("")
                    .to_owned();
                let base = if original.is_empty() {
                    stable_game_id(&restored)
                } else {
                    original.clone()
                };
                let mut candidate = base.clone();
                let mut suffix = 2usize;
                while used.contains(&candidate) {
                    candidate = format!("{base}-{suffix}");
                    suffix += 1;
                }
                let renamed = candidate != original;
                let restored_name = restored
                    .get("name")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned();
                restored.insert("game_id".into(), Value::String(candidate.clone()));
                if let Some(Value::Array(games)) = state.get_mut("games") {
                    let idx = entry
                        .get("index")
                        .and_then(Value::as_u64)
                        .map(|n| n as usize)
                        .filter(|&i| i <= games.len())
                        .unwrap_or(games.len());
                    games.insert(idx, Value::Object(restored));
                }
                let pl_names: Vec<String> = entry
                    .get("playlists")
                    .and_then(Value::as_array)
                    .map(|a| {
                        a.iter()
                            .filter_map(Value::as_str)
                            .map(ToOwned::to_owned)
                            .collect()
                    })
                    .unwrap_or_default();
                if let Some(Value::Array(pls)) = state.get_mut("playlists") {
                    for pname in &pl_names {
                        for pl in pls.iter_mut() {
                            if pl.get("type").and_then(Value::as_str) == Some("manual")
                                && pl.get("name").and_then(Value::as_str) == Some(pname.as_str())
                                && let Some(pm) = pl.as_object_mut()
                            {
                                let members = pm
                                    .entry("members".to_owned())
                                    .or_insert_with(|| Value::Array(Vec::new()));
                                if let Some(arr) = members.as_array_mut()
                                    && !arr.iter().any(|m| m.as_str() == Some(candidate.as_str()))
                                {
                                    arr.push(Value::String(candidate.clone()));
                                }
                            }
                        }
                    }
                }
                let mut next_trash = trash;
                next_trash.remove(pos);
                if let Some(obj) = state.as_object_mut() {
                    obj.insert("trash".into(), Value::Array(next_trash));
                }
                res = Ok(json!({
                    "ok": true,
                    "restored": true,
                    "game_id": candidate,
                    "name": restored_name,
                    "renamed": renamed,
                }));
                Ok(())
            });
            match (tx, res) {
                (_, Err(err)) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
                (Err(err), _) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
                (Ok(_), Ok(val)) => {
                    ctx.app.media_probe.clear_caches();
                    Some(json_response(StatusCode::OK, &val, ctx.instance))
                }
            }
        }
        "/api/v2/library/trash/purge" => {
            let wanted: Option<HashSet<String>> = match payload.get("ids") {
                None | Some(Value::Null) => None,
                Some(Value::Array(arr)) => Some(arr.iter().map(value_to_str).collect()),
                _ => {
                    return Some(api_error_response(
                        &ApiError::bad_request("ids must be a list of trash entry ids."),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let mut purged = 0usize;
            match ctx.app.transact_state(|state| {
                let trash = state
                    .get("trash")
                    .and_then(Value::as_array)
                    .cloned()
                    .unwrap_or_default();
                let before = trash.len();
                let kept: Vec<Value> = match &wanted {
                    None => Vec::new(),
                    Some(w) => trash
                        .into_iter()
                        .filter(|item| {
                            !item
                                .get("trash_id")
                                .and_then(Value::as_str)
                                .is_some_and(|id| w.contains(id))
                        })
                        .collect(),
                };
                purged = before - kept.len();
                if let Some(obj) = state.as_object_mut() {
                    obj.insert("trash".into(), Value::Array(kept));
                }
                Ok(())
            }) {
                Ok(_) => Some(json_response(
                    StatusCode::OK,
                    &json!({"ok": true, "purged": purged}),
                    ctx.instance,
                )),
                Err(err) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/playlists" => {
            let name = str_field(payload, "name").to_owned();
            let Some(rules_val) = payload.get("rules").filter(|v| v.is_object()) else {
                return Some(api_error_response(
                    &ApiError::bad_request("Playlist name and rules are required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            if name.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("Playlist name and rules are required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let pl_type = {
                let t = str_field(payload, "type").to_lowercase();
                if t.is_empty() { "filter".to_owned() } else { t }
            };
            if !matches!(pl_type.as_str(), "filter" | "manual") {
                return Some(api_error_response(
                    &ApiError::bad_request("Playlist type must be filter or manual."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let clean_rules = if pl_type == "filter" {
                match normalize_filter_rules(rules_val) {
                    Ok(r) => Value::Object(r),
                    Err(err) => {
                        return Some(api_error_response(&err, ctx.request_id, ctx.instance));
                    }
                }
            } else {
                json!({})
            };
            let state_snap = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(err) => {
                    return Some(api_error_response(
                        &ApiError::state_unavailable(err.to_string()),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let games = state_snap
                .get("games")
                .and_then(Value::as_array)
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            let raw_members = payload
                .get("members")
                .or_else(|| payload.get("ids"))
                .cloned()
                .unwrap_or_else(|| json!([]));
            let Some(m_arr) = raw_members.as_array() else {
                return Some(api_error_response(
                    &ApiError::bad_request("Playlist members must be a list."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            if m_arr.len() > 100_000 {
                return Some(api_error_response(
                    &ApiError::bad_request("Playlist members must be a list."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let mut member_ids: Vec<String> = Vec::new();
            for val in m_arr {
                let mut probe = Map::new();
                match val {
                    Value::Object(m) => probe = m.clone(),
                    Value::Number(n) => {
                        probe.insert("id".into(), Value::Number(n.clone()));
                    }
                    Value::String(s) => {
                        probe.insert("game_id".into(), Value::String(s.clone()));
                        if let Ok(n) = s.parse::<u64>() {
                            probe.insert("id".into(), json!(n));
                        }
                    }
                    _ => {}
                }
                let idx = match find_game_index(games, &probe) {
                    Ok(i) => i,
                    Err(err) => {
                        return Some(api_error_response(&err, ctx.request_id, ctx.instance));
                    }
                };
                let sid = games[idx]
                    .get("game_id")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned();
                if !sid.is_empty() && !member_ids.contains(&sid) {
                    member_ids.push(sid);
                }
            }
            let parent = str_field(payload, "parent").to_owned();
            let notes = str_field(payload, "notes").to_owned();
            let members_val = if pl_type == "manual" {
                Value::Array(member_ids.into_iter().map(Value::String).collect())
            } else {
                json!([])
            };
            match ctx.app.transact_state(|state| {
                if let Some(obj) = state.as_object_mut() {
                    let pls = obj
                        .entry("playlists".to_owned())
                        .or_insert_with(|| Value::Array(Vec::new()));
                    if let Some(arr) = pls.as_array_mut() {
                        if let Some(existing) = arr
                            .iter_mut()
                            .find(|p| p.get("name").and_then(Value::as_str) == Some(name.as_str()))
                            && let Some(em) = existing.as_object_mut()
                        {
                            em.insert("rules".into(), clean_rules.clone());
                            em.insert("type".into(), Value::String(pl_type.clone()));
                            em.insert("members".into(), members_val.clone());
                            em.insert("parent".into(), Value::String(parent.clone()));
                            em.insert("notes".into(), Value::String(notes.clone()));
                        } else {
                            arr.push(json!({
                                "name": name,
                                "type": pl_type,
                                "rules": clean_rules,
                                "members": members_val,
                                "parent": parent,
                                "notes": notes,
                            }));
                        }
                    }
                }
                Ok(())
            }) {
                Ok(_) => Some(json_response(
                    StatusCode::OK,
                    &json!({"saved": name}),
                    ctx.instance,
                )),
                Err(err) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/playlists/delete" => {
            let name = str_field(payload, "name").to_owned();
            let mut removed_count = 0usize;
            match ctx.app.transact_state(|state| {
                if let Some(Value::Array(pls)) = state.get_mut("playlists") {
                    let before = pls.len();
                    pls.retain(|p| p.get("name").and_then(Value::as_str) != Some(name.as_str()));
                    removed_count = before - pls.len();
                }
                Ok(())
            }) {
                Ok(_) => {
                    if removed_count == 0 {
                        let disp = if name.is_empty() {
                            "(blank)"
                        } else {
                            name.as_str()
                        };
                        return Some(api_error_response(
                            &ApiError::not_found(format!("Playlist not found: {disp}")),
                            ctx.request_id,
                            ctx.instance,
                        ));
                    }
                    Some(json_response(
                        StatusCode::OK,
                        &json!({"deleted": name}),
                        ctx.instance,
                    ))
                }
                Err(err) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/webhooks" => {
            let raw_configs = payload
                .get("webhooks")
                .or_else(|| payload.get("configs"))
                .cloned()
                .unwrap_or_else(|| json!([]));
            let Some(arr) = raw_configs.as_array() else {
                return Some(api_error_response(
                    &ApiError::bad_request(format!(
                        "Webhooks must be a list of at most {MAX_WEBHOOKS} entries."
                    )),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            if arr.len() > MAX_WEBHOOKS {
                return Some(api_error_response(
                    &ApiError::bad_request(format!(
                        "Webhooks must be a list of at most {MAX_WEBHOOKS} entries."
                    )),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let existing_whs: Vec<Value> = ctx
                .app
                .load_state_view()
                .ok()
                .and_then(|s| {
                    s.get("settings")
                        .and_then(|st| st.get("webhooks"))
                        .and_then(Value::as_array)
                        .cloned()
                })
                .unwrap_or_default();
            let port = Some(ctx.app.port());
            let mut clean = Vec::new();
            for raw in arr {
                let Some(obj) = raw.as_object() else {
                    return Some(api_error_response(
                        &ApiError::bad_request("Webhook configuration must be an object."),
                        ctx.request_id,
                        ctx.instance,
                    ));
                };
                let mut cfg = obj.clone();
                let id = obj
                    .get("id")
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
                    .map(ToOwned::to_owned)
                    .unwrap_or_else(|| {
                        let mut b = [0u8; 8];
                        rand::thread_rng().fill_bytes(&mut b);
                        format!("wh-{}", hex::encode(b))
                    });
                cfg.insert("id".into(), Value::String(id.clone()));
                cfg.insert(
                    "url".into(),
                    Value::String(
                        obj.get("url")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .trim()
                            .to_owned(),
                    ),
                );
                cfg.insert(
                    "events".into(),
                    obj.get("events").cloned().unwrap_or_else(|| json!([])),
                );
                cfg.insert(
                    "enabled".into(),
                    Value::Bool(obj.get("enabled").and_then(Value::as_bool).unwrap_or(true)),
                );
                cfg.insert(
                    "attempts".into(),
                    json!(
                        obj.get("attempts")
                            .and_then(Value::as_i64)
                            .unwrap_or(DEFAULT_ATTEMPTS as i64)
                    ),
                );
                cfg.insert(
                    "timeout".into(),
                    json!(
                        obj.get("timeout")
                            .and_then(Value::as_i64)
                            .unwrap_or(DEFAULT_TIMEOUT as i64)
                    ),
                );
                let has_secret = cfg
                    .get("secret")
                    .and_then(Value::as_str)
                    .is_some_and(|s| !s.is_empty());
                if !has_secret
                    && obj
                        .get("secret_set")
                        .and_then(Value::as_bool)
                        .unwrap_or(false)
                    && let Some(prev) = existing_whs
                        .iter()
                        .find(|w| w.get("id").and_then(Value::as_str) == Some(id.as_str()))
                {
                    cfg.insert(
                        "secret".into(),
                        prev.get("secret").cloned().unwrap_or_else(|| json!("")),
                    );
                }
                let val = Value::Object(cfg);
                if let Err(err) = validate_webhook(&val, port) {
                    return Some(api_error_response(
                        &ApiError::bad_request(err),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
                clean.push(val);
            }
            let attempts = payload
                .get("attempts")
                .and_then(Value::as_i64)
                .unwrap_or(DEFAULT_ATTEMPTS as i64);
            let timeout = payload
                .get("timeout")
                .and_then(Value::as_i64)
                .unwrap_or(DEFAULT_TIMEOUT as i64);
            match ctx.app.transact_state(|state| {
                if let Some(obj) = state.as_object_mut() {
                    let s = obj
                        .entry("settings".to_owned())
                        .or_insert_with(|| Value::Object(Map::new()));
                    if let Some(sm) = s.as_object_mut() {
                        sm.insert("webhooks".into(), Value::Array(clean.clone()));
                        sm.insert("webhook_attempts".into(), json!(attempts));
                        sm.insert("webhook_timeout".into(), json!(timeout));
                    }
                }
                Ok(())
            }) {
                Ok((committed, _)) => Some(json_response(
                    StatusCode::OK,
                    &json!({
                        "webhooks": public_webhook_configs(&committed),
                        "events": EVENT_TYPES,
                    }),
                    ctx.instance,
                )),
                Err(err) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/webhooks/test" => {
            let cfg = payload
                .get("webhook")
                .cloned()
                .unwrap_or_else(|| Value::Object(payload.clone()));
            if let Err(err) = validate_webhook(&cfg, Some(ctx.app.port())) {
                return Some(api_error_response(
                    &ApiError::bad_request(err),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let event_id = ctx
                .app
                .publish_webhook_event("webhook.test", &json!({"ping": true}));
            Some(json_response(
                StatusCode::OK,
                &json!({"ok": true, "event_id": event_id}),
                ctx.instance,
            ))
        }
        "/api/v2/filter-presets" => {
            let name = str_field(payload, "name").trim().to_owned();
            let rules = payload.get("rules").cloned().unwrap_or_else(|| json!({}));
            let bigbox_quick = bool_field(payload, "bigbox_quick");
            let name_mut = name.clone();
            match ctx
                .app
                .transact_state(|cur| Ok(save_preset(cur, &name_mut, &rules, bigbox_quick)))
            {
                Ok((_, Ok(saved))) => Some(json_response(
                    StatusCode::OK,
                    &json!({"saved": saved}),
                    ctx.instance,
                )),
                Ok((_, Err(msg))) => Some(api_error_response(
                    &ApiError::bad_request(msg),
                    ctx.request_id,
                    ctx.instance,
                )),
                Err(err) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/filter-presets/delete" => {
            let name = str_field(payload, "name").trim().to_owned();
            let name_mut = name.clone();
            match ctx
                .app
                .transact_state(|cur| Ok(delete_preset(cur, &name_mut)))
            {
                Ok((_, true)) => Some(json_response(
                    StatusCode::OK,
                    &json!({"deleted": name}),
                    ctx.instance,
                )),
                Ok((_, false)) => Some(api_error_response(
                    &ApiError::bad_request("Preset not found."),
                    ctx.request_id,
                    ctx.instance,
                )),
                Err(err) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/library/query/parse" => {
            let Some(query) = payload.get("query").and_then(Value::as_str) else {
                return Some(api_error_response(
                    &ApiError::bad_request("query must be a string."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            if query.chars().count() > 2000 {
                return Some(api_error_response(
                    &ApiError::bad_request("query is too long."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let mut parsed = parse_query(query, None);
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(err) => {
                    return Some(api_error_response(
                        &ApiError::state_unavailable(err.to_string()),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let games = state
                .get("games")
                .and_then(Value::as_array)
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            let matches = filter_games_by_query(games, &parsed, None);
            let matched_game_ids: Vec<String> = matches
                .iter()
                .take(20_000)
                .filter_map(|g| {
                    if !g.is_object() {
                        return None;
                    }
                    let gid = g
                        .get("game_id")
                        .and_then(Value::as_str)
                        .filter(|s| !s.is_empty())
                        .map(str::to_owned)
                        .or_else(|| {
                            g.get("id").and_then(|v| {
                                if v.is_null() {
                                    None
                                } else {
                                    Some(
                                        v.as_str()
                                            .map(str::to_owned)
                                            .unwrap_or_else(|| v.to_string()),
                                    )
                                }
                            })
                        })?;
                    Some(gid)
                })
                .collect();
            if let Some(m) = parsed.as_object_mut() {
                m.insert("match_count".into(), json!(matches.len()));
                m.insert("matched_game_ids".into(), json!(matched_game_ids));
            }
            Some(json_response(StatusCode::OK, &parsed, ctx.instance))
        }
        "/api/v2/health" => {
            let get_ctx = RequestCtx {
                app: Arc::clone(&ctx.app),
                path: "/api/v2/health",
                query: ctx.query,
                instance: ctx.instance,
                request_id: ctx.request_id,
                headers: ctx.headers,
            };
            crate::handlers::library_health::handle_get(&get_ctx)
        }
        "/api/v2/health/dedupe" => {
            let data_dir = ctx.app.data_dir.clone();
            let mut removed = Vec::new();
            match ctx.app.transact_state(|state| {
                if let Some(Value::Array(games)) = state.get_mut("games") {
                    removed = consolidate_existing_games(games, &data_dir);
                }
                Ok(())
            }) {
                Ok(_) => Some(json_response(
                    StatusCode::OK,
                    &json!({"removed": removed}),
                    ctx.instance,
                )),
                Err(err) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        _ => None,
    }
}
