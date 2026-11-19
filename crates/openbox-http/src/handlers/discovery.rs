//! Game DNA smart search handlers (`handlers/discovery.py`).

use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;

use axum::http::StatusCode;
use axum::response::Response;
use openbox_core::ApiError;
use openbox_discovery::dna::{
    INDEX_FORMAT, LEXICON_VERSION, corpus_hash, doc_id_for, load_index, note_game_removed,
    note_game_upserted, parse_dna_query, rebuild_index, save_index_atomic, signature_matches,
};
use openbox_runtime::AppState;
use serde_json::{Map, Value, json};

use crate::handlers::shared::{RequestCtx, str_field};
use crate::middleware::{api_error_response, json_response, state_error_response};

pub const SYNC_BUILD_THRESHOLD: usize = 5000;
pub const HEAVY_DRIFT_RATIO: f64 = 0.10;

static LAST_BUILD_MS_TENTHS: AtomicU64 = AtomicU64::new(0);

fn set_last_build_ms(ms: f64) {
    LAST_BUILD_MS_TENTHS.store((ms * 10.0).round().max(0.0) as u64, Ordering::Relaxed);
}

fn get_last_build_ms() -> f64 {
    LAST_BUILD_MS_TENTHS.load(Ordering::Relaxed) as f64 / 10.0
}

fn locale_for(state: &Value) -> String {
    state
        .get("settings")
        .and_then(|s| s.get("locale"))
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .unwrap_or("en")
        .chars()
        .take(5)
        .collect()
}

fn state_signature(app: &AppState) -> Option<Value> {
    app.store.signature().map(|s| json!([s.mtime_ns, s.size]))
}

pub fn reconcile_index(
    index: &mut Value,
    games: &[Value],
    locale: &str,
    signature: Option<&Value>,
) -> usize {
    let mut changed = 0usize;
    let by_id: HashMap<String, &Value> = games
        .iter()
        .filter(|g| g.is_object())
        .map(|g| (doc_id_for(g), g))
        .collect();
    let live_ids: HashSet<String> = by_id.keys().cloned().collect();
    let stored_ids: Vec<String> = index
        .get("games")
        .and_then(Value::as_object)
        .map(|m| m.keys().cloned().collect())
        .unwrap_or_default();
    for doc_id in stored_ids {
        if !live_ids.contains(&doc_id) {
            note_game_removed(index, &doc_id);
            changed += 1;
        }
    }
    for (doc_id, &game) in &by_id {
        let needs_update = index
            .get("games")
            .and_then(|g| g.get(doc_id))
            .and_then(|e| e.get("h"))
            .and_then(Value::as_str)
            != Some(corpus_hash(game).as_str());
        if needs_update {
            note_game_upserted(index, game, locale);
            changed += 1;
        }
    }
    if let Some(m) = index.as_object_mut() {
        m.insert(
            "state_signature".into(),
            signature.cloned().unwrap_or(Value::Null),
        );
    }
    changed
}

pub fn submit_rebuild_job(app: &Arc<AppState>, reason: &str) -> Result<Value, String> {
    let app_clone = Arc::clone(app);
    let reason_owned = reason.to_owned();
    app.jobs
        .submit("dna-index-rebuild", true, None, None, move |jctx| {
            let started = Instant::now();
            let state = app_clone.load_state_view().map_err(|e| e.to_string())?;
            let games: Vec<Value> = state
                .get("games")
                .and_then(Value::as_array)
                .map(|a| a.iter().filter(|g| g.is_object()).cloned().collect())
                .unwrap_or_default();
            let locale = locale_for(&state);
            let sig = state_signature(&app_clone);
            let total = games.len();
            let mut merged = Map::new();
            let mut df: HashMap<String, i64> = HashMap::new();
            let chunk_sz = 500;
            let mut start = 0usize;
            while start < total {
                if jctx.is_cancelled() {
                    return Ok(json!({"cancelled": true, "processed": start}));
                }
                let end = (start + chunk_sz).min(total);
                let partial = rebuild_index(&games[start..end], &locale, None);
                if let Some(gm) = partial.get("games").and_then(Value::as_object) {
                    for (k, v) in gm {
                        merged.insert(k.clone(), v.clone());
                    }
                }
                if let Some(dm) = partial.get("df").and_then(Value::as_object) {
                    for (k, v) in dm {
                        *df.entry(k.clone()).or_insert(0) += v.as_i64().unwrap_or(0);
                    }
                }
                jctx.progress(json!({"current": end, "total": total}));
                start = end;
            }
            let count = merged.len();
            let total_len: f64 = merged
                .values()
                .map(|e| e.get("l").and_then(Value::as_f64).unwrap_or(0.0))
                .sum();
            let df_map: Map<String, Value> = df.into_iter().map(|(k, v)| (k, json!(v))).collect();
            let index = json!({
                "format": INDEX_FORMAT,
                "lexicon_version": LEXICON_VERSION,
                "state_signature": sig.unwrap_or(Value::Null),
                "doc_count": count,
                "avg_len": if count > 0 { total_len / count as f64 } else { 1.0 },
                "df": Value::Object(df_map),
                "games": Value::Object(merged),
            });
            save_index_atomic(&index, &app_clone.data_dir)?;
            let ms = started.elapsed().as_secs_f64() * 1000.0;
            set_last_build_ms(ms);
            Ok(json!({
                "indexed": count,
                "build_ms": (ms * 10.0).round() / 10.0,
                "reason": reason_owned,
            }))
        })
}

pub fn dna_index_for_search(app: &Arc<AppState>, state: &Value) -> (Option<Value>, bool, bool) {
    let games: Vec<Value> = state
        .get("games")
        .and_then(Value::as_array)
        .map(|a| a.iter().filter(|g| g.is_object()).cloned().collect())
        .unwrap_or_default();
    let locale = locale_for(state);
    let sig = state_signature(app);
    let Some(mut index) = load_index(&app.data_dir) else {
        if games.len() >= SYNC_BUILD_THRESHOLD {
            let _ = submit_rebuild_job(app, "missing-index");
            return (None, true, true);
        }
        let started = Instant::now();
        let index = rebuild_index(&games, &locale, sig.as_ref());
        let _ = save_index_atomic(&index, &app.data_dir);
        set_last_build_ms(started.elapsed().as_secs_f64() * 1000.0);
        return (Some(index), false, false);
    };
    let stale = !signature_matches(&index, sig.as_ref());
    let changed = reconcile_index(&mut index, &games, &locale, sig.as_ref());
    if changed > 0 {
        let _ = save_index_atomic(&index, &app.data_dir);
    }
    let drift = changed as f64 / games.len().max(1) as f64;
    if stale && drift > HEAVY_DRIFT_RATIO {
        let _ = submit_rebuild_job(app, "signature-mismatch");
        return (Some(index), true, false);
    }
    (Some(index), false, false)
}

/// Best-effort incremental DNA hook when games are upserted.
pub fn dna_note_upserted(app: &AppState, state: &Value, game: &Value) {
    let Some(mut index) = load_index(&app.data_dir) else {
        return;
    };
    let locale = locale_for(state);
    note_game_upserted(&mut index, game, &locale);
    if let Some(sig) = state_signature(app)
        && let Some(m) = index.as_object_mut()
    {
        m.insert("state_signature".into(), sig);
    }
    let _ = save_index_atomic(&index, &app.data_dir);
}

/// Best-effort incremental DNA hook when games are removed.
pub fn dna_note_removed(app: &AppState, game_ids: &[String]) {
    if game_ids.is_empty() {
        return;
    }
    let Some(mut index) = load_index(&app.data_dir) else {
        return;
    };
    for gid in game_ids {
        if !gid.is_empty() {
            note_game_removed(&mut index, gid);
        }
    }
    if let Some(sig) = state_signature(app)
        && let Some(m) = index.as_object_mut()
    {
        m.insert("state_signature".into(), sig);
    }
    let _ = save_index_atomic(&index, &app.data_dir);
}

fn title_fallback(games: &[Value], query: &str, limit: usize) -> Vec<Value> {
    let q_low = query.to_lowercase();
    let mut results = Vec::new();
    for game in games {
        if game
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_lowercase()
            .contains(&q_low)
        {
            results.push(json!({
                "game_id": doc_id_for(game),
                "score": 1.0,
                "why": ["title match"],
            }));
            if results.len() >= limit {
                break;
            }
        }
    }
    results
}

fn apply_filters(results: Vec<Value>, games: &[Value], filters: Option<&Value>) -> Vec<Value> {
    let Some(fmap) = filters.and_then(Value::as_object) else {
        return results;
    };
    if fmap.is_empty() {
        return results;
    }
    let by_id: HashMap<String, &Value> = games
        .iter()
        .filter(|g| g.is_object())
        .map(|g| (doc_id_for(g), g))
        .collect();
    let mut kept = Vec::new();
    for row in results {
        let gid = row.get("game_id").and_then(Value::as_str).unwrap_or("");
        let Some(&game) = by_id.get(gid) else {
            continue;
        };
        let mut ok = true;
        for (field, wanted) in fmap {
            if !matches!(
                field.as_str(),
                "genre" | "platform" | "play_mode" | "developer" | "publisher"
            ) {
                continue;
            }
            let values: Vec<String> = match wanted.as_array() {
                Some(a) => a
                    .iter()
                    .map(|v| {
                        v.as_str()
                            .map(str::to_owned)
                            .unwrap_or_else(|| v.to_string())
                            .to_lowercase()
                    })
                    .collect(),
                None => vec![
                    wanted
                        .as_str()
                        .map(str::to_owned)
                        .unwrap_or_else(|| wanted.to_string())
                        .to_lowercase(),
                ],
            };
            let hay = game
                .get(field)
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_lowercase();
            if !values.iter().any(|v| hay.contains(v)) {
                ok = false;
                break;
            }
        }
        if ok {
            kept.push(row);
        }
    }
    kept
}

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    if ctx.path != "/api/v2/library/dna/status" {
        return None;
    }
    let state = match ctx.app.load_state_view() {
        Ok(s) => s,
        Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
    };
    let games: Vec<Value> = state
        .get("games")
        .and_then(Value::as_array)
        .map(|a| a.iter().filter(|g| g.is_object()).cloned().collect())
        .unwrap_or_default();
    let locale = locale_for(&state);
    let sig = state_signature(&ctx.app);
    let (indexed, state_name) = match load_index(&ctx.app.data_dir) {
        None => {
            let mut building = false;
            if games.len() >= SYNC_BUILD_THRESHOLD {
                let _ = submit_rebuild_job(&ctx.app, "missing-index");
                building = true;
            }
            (0usize, if building { "building" } else { "missing" })
        }
        Some(mut idx) => {
            let stale = !signature_matches(&idx, sig.as_ref());
            let changed = reconcile_index(&mut idx, &games, &locale, sig.as_ref());
            if changed > 0 {
                let _ = save_index_atomic(&idx, &ctx.app.data_dir);
            }
            let cnt = idx.get("doc_count").and_then(Value::as_u64).unwrap_or(0) as usize;
            (cnt, if stale { "stale" } else { "ready" })
        }
    };
    let total = games.len();
    let with_desc = games
        .iter()
        .filter(|g| {
            g.get("description")
                .and_then(Value::as_str)
                .is_some_and(|s| !s.trim().is_empty())
        })
        .count();
    let coverage = ((100.0 * with_desc as f64 / total.max(1) as f64) * 10.0).round() / 10.0;
    Some(json_response(
        StatusCode::OK,
        &json!({
            "indexed": indexed,
            "total": total,
            "coverage_pct": coverage,
            "index_version": INDEX_FORMAT,
            "lexicon_version": LEXICON_VERSION,
            "build_ms": get_last_build_ms(),
            "state": state_name,
        }),
        ctx.instance,
    ))
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/library/dna/search" => {
            let query = str_field(payload, "query");
            if query.trim().is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("query is required"),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let limit: usize = match payload.get("limit") {
                None | Some(Value::Null) => 20,
                Some(Value::Number(n)) => match n.as_i64() {
                    Some(v) => v.clamp(1, 100) as usize,
                    None => {
                        return Some(api_error_response(
                            &ApiError::bad_request("limit must be an integer"),
                            ctx.request_id,
                            ctx.instance,
                        ));
                    }
                },
                Some(Value::String(s)) => match s.trim().parse::<i64>() {
                    Ok(v) => v.clamp(1, 100) as usize,
                    Err(_) => {
                        return Some(api_error_response(
                            &ApiError::bad_request("limit must be an integer"),
                            ctx.request_id,
                            ctx.instance,
                        ));
                    }
                },
                _ => {
                    return Some(api_error_response(
                        &ApiError::bad_request("limit must be an integer"),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let games: Vec<Value> = state
                .get("games")
                .and_then(Value::as_array)
                .map(|a| a.iter().filter(|g| g.is_object()).cloned().collect())
                .unwrap_or_default();
            let locale = locale_for(&state);
            let (index_opt, degraded, building) = dna_index_for_search(&ctx.app, &state);
            let Some(index) = index_opt else {
                return Some(json_response(
                    StatusCode::OK,
                    &json!({
                        "results": title_fallback(&games, query.trim(), limit),
                        "parse": {"parsed": false},
                        "branch": "title-fallback",
                        "anchor": Value::Null,
                        "degraded": true,
                        "building": building,
                    }),
                    ctx.instance,
                ));
            };
            let mut outcome = parse_dna_query(query, &games, &index, &locale, limit);
            if let Some(m) = outcome.as_object_mut() {
                let raw_results = m
                    .remove("results")
                    .and_then(|v| v.as_array().cloned())
                    .unwrap_or_default();
                let filtered = apply_filters(raw_results, &games, payload.get("filters"));
                m.insert("results".into(), Value::Array(filtered));
                m.insert("degraded".into(), Value::Bool(degraded));
            }
            Some(json_response(StatusCode::OK, &outcome, ctx.instance))
        }
        "/api/v2/library/dna/index/rebuild" => match submit_rebuild_job(&ctx.app, "manual") {
            Ok(job) => Some(json_response(
                StatusCode::ACCEPTED,
                &json!({
                    "state": job.get("state").and_then(Value::as_str).unwrap_or("queued"),
                    "job_id": job.get("job_id").and_then(Value::as_str).unwrap_or(""),
                }),
                ctx.instance,
            )),
            Err(e) => Some(api_error_response(
                &ApiError::job_state_conflict(e),
                ctx.request_id,
                ctx.instance,
            )),
        },
        _ => None,
    }
}
