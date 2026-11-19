//! Library health v2 API, legacy `/api/v2/health`, missing-file repair wizard,
//! and duplicate detection/merge handlers (`handlers/library_health.py`, `handlers/library.py`).

use std::collections::{BTreeSet, HashMap, HashSet};
use std::sync::Arc;

use axum::http::StatusCode;
use axum::response::Response;
use jiff::Timestamp;
use openbox_core::ApiError;
use openbox_emulators::audit::load_audit;
use openbox_imports::identity::detect_duplicate_identities;
use openbox_metadata::artwork_hygiene::{
    build_report, load_undo_manifest, select_fixable, undo_batch,
};
use openbox_runtime::AppState;
use openbox_sync::duplicates::{MAX_GROUPS, apply_merge, find_duplicates, merge_plan};
use openbox_sync::library_health::{
    DIMENSIONS, detect_issues, get_fix_journal, get_health_cache, mark_fix_undone, record_fix,
    score_library, store_health_cache,
};
use openbox_sync::library_sync::state_token;
use openbox_sync::repair::{
    MAX_CANDIDATES, MAX_FOLDER_DEPTH, MAX_SCAN_ITEMS, apply_repair, plan_repair, resolve_folder,
    scan_candidates, scan_missing_paths,
};
use serde_json::{Map, Value, json};
use uuid::Uuid;

use crate::handlers::shared::{RequestCtx, bool_field, str_field};
use crate::middleware::{api_error_response, json_response, state_error_response};

pub const SCAN_JOB_NAME: &str = "library-health-scan";

fn is_valid_dimension(dim: &str) -> bool {
    DIMENSIONS.iter().any(|(d, _)| *d == dim)
}

fn public_snapshot(snapshot: Option<&Value>) -> Value {
    let empty = json!({});
    let s = snapshot.unwrap_or(&empty);
    let score = s.get("score").cloned().unwrap_or(Value::Null);
    let scanned = !score.is_null();
    json!({
        "score": score,
        "dimensions": s.get("dimensions").cloned().unwrap_or_else(|| json!({})),
        "computed_at": s.get("computed_at").and_then(Value::as_str).unwrap_or(""),
        "game_count": s.get("game_count").and_then(Value::as_u64).unwrap_or(0),
        "dirty": s.get("dirty").and_then(Value::as_bool).unwrap_or(true),
        "full": s.get("full").and_then(Value::as_bool).unwrap_or(true),
        "scanned": scanned,
        "sources": s.get("sources").cloned().unwrap_or_else(|| json!([])),
        "source_counts": s.get("source_counts").cloned().unwrap_or_else(|| json!({})),
    })
}

fn make_trash_entry(state: &Value, game: &Value) -> Value {
    let gid = game.get("game_id").and_then(Value::as_str).unwrap_or("");
    let playlists: Vec<Value> = state
        .get("playlists")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .filter(|p| {
                    p.get("type").and_then(Value::as_str) == Some("manual")
                        && p.get("members")
                            .and_then(Value::as_array)
                            .is_some_and(|m| m.iter().any(|x| x.as_str() == Some(gid)))
                })
                .filter_map(|p| p.get("name").cloned())
                .collect()
        })
        .unwrap_or_default();
    let now = Timestamp::now();
    let dt = jiff::tz::TimeZone::UTC.to_datetime(now);
    let trashed_at = format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
        dt.year(),
        dt.month(),
        dt.day(),
        dt.hour(),
        dt.minute(),
        dt.second()
    );
    let tid = format!("trash-{}", &Uuid::new_v4().simple().to_string()[..12]);
    json!({
        "trash_id": tid,
        "game_id": gid,
        "name": game.get("name").and_then(Value::as_str).unwrap_or(""),
        "platform": game.get("platform").and_then(Value::as_str).unwrap_or(""),
        "trashed_at": trashed_at,
        "playlists": playlists,
        "game": game,
    })
}

fn repair_scope(
    app: &AppState,
    audit_group: Option<&str>,
) -> Result<Option<HashSet<String>>, ApiError> {
    let Some(grp) = audit_group.map(str::trim).filter(|s| !s.is_empty()) else {
        return Ok(None);
    };
    let Some(audit) = load_audit(&app.data_dir) else {
        return Err(ApiError::bad_request(
            "No recent audit report found; run the audit first.",
        ));
    };
    let Some(groups) = audit.get("groups").and_then(Value::as_array) else {
        return Err(ApiError::bad_request(format!(
            "Audit group not found: {grp}."
        )));
    };
    for group in groups {
        if group.get("key").and_then(Value::as_str) == Some(grp) {
            let ids: HashSet<String> = group
                .get("games")
                .and_then(Value::as_array)
                .map(|arr| {
                    arr.iter()
                        .filter_map(|g| {
                            g.get("game_id")
                                .and_then(Value::as_str)
                                .or_else(|| g.as_str())
                                .map(str::to_owned)
                        })
                        .collect()
                })
                .unwrap_or_default();
            return Ok(Some(ids));
        }
    }
    Err(ApiError::bad_request(format!(
        "Audit group not found: {grp}."
    )))
}

pub fn run_health_scan_for(app: &Arc<AppState>) -> Result<Value, String> {
    let state = app.load_state().map_err(|e| e.to_string())?;
    let games = state
        .get("games")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let probe = |p: &str, file_only: bool| app.media_probe.probe_path(p, file_only);
    let snapshot = score_library(games, &state, Some(&probe));
    let computed_at = snapshot
        .get("computed_at")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    let score = snapshot.get("score").cloned().unwrap_or(json!(100));
    let game_count = snapshot.get("game_count").cloned().unwrap_or(json!(0));
    app.transact_state(|cur| {
        store_health_cache(cur, &snapshot);
        if let Some(root) = cur.as_object_mut() {
            let set = root
                .entry("settings".to_owned())
                .or_insert_with(|| Value::Object(Map::new()));
            if let Some(sm) = set.as_object_mut() {
                sm.insert("last_health_rescan".into(), Value::String(computed_at));
            }
        }
        Ok(())
    })
    .map_err(|e| e.to_string())?;
    app.media_probe.clear_caches();
    Ok(json!({"score": score, "game_count": game_count}))
}

fn index_by_game_id(games: &[Value]) -> HashMap<String, usize> {
    let mut map = HashMap::new();
    for (idx, g) in games.iter().enumerate() {
        if let Some(gid) = g
            .get("game_id")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
        {
            map.insert(gid.to_owned(), idx);
        }
        if let Some(id_n) = g.get("id").and_then(Value::as_i64) {
            map.insert(id_n.to_string(), idx);
        }
    }
    map
}

fn select_issues(deductions: &[Value], dimension: &str, issue_ids: Option<&Value>) -> Vec<Value> {
    let selected: Vec<Value> = deductions
        .iter()
        .filter(|d| d.get("dimension").and_then(Value::as_str) == Some(dimension))
        .cloned()
        .collect();
    match issue_ids {
        None | Some(Value::Null) => selected,
        Some(Value::String(s)) if s == "all" => selected,
        Some(Value::Array(arr)) => {
            let wanted: HashSet<String> = arr
                .iter()
                .map(|v| match v {
                    Value::String(s) => s.clone(),
                    other => other.to_string(),
                })
                .collect();
            selected
                .into_iter()
                .filter(|item| {
                    item.get("game_id")
                        .and_then(Value::as_str)
                        .is_some_and(|s| wanted.contains(s))
                        || item
                            .get("index")
                            .and_then(Value::as_i64)
                            .is_some_and(|n| wanted.contains(&n.to_string()))
                })
                .collect()
        }
        _ => Vec::new(),
    }
}

fn plan_duplicates(state: &Value, issues: &[Value]) -> Vec<Value> {
    let games = state
        .get("games")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let by_id = index_by_game_id(games);
    let wanted: HashSet<String> = issues
        .iter()
        .filter_map(|i| i.get("game_id").and_then(Value::as_str).map(str::to_owned))
        .collect();
    let mut plans = Vec::new();
    for group in detect_duplicate_identities(games, true) {
        let g_ids: Vec<String> = group
            .get("games")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .map(|v| match v {
                        Value::String(s) => s.clone(),
                        other => other.to_string(),
                    })
                    .collect()
            })
            .unwrap_or_default();
        if !g_ids.iter().any(|gid| wanted.contains(gid)) {
            continue;
        }
        let members: Vec<usize> = g_ids
            .iter()
            .filter_map(|gid| by_id.get(gid).copied())
            .collect();
        if members.len() < 2 {
            continue;
        }
        if let Ok(p) = merge_plan(state, &members) {
            plans.push(p);
        }
    }
    plans
}

fn execute_duplicates(state: &mut Value, issues: &[Value]) -> Value {
    let wanted: HashSet<String> = issues
        .iter()
        .filter_map(|i| i.get("game_id").and_then(Value::as_str).map(str::to_owned))
        .collect();
    let before_ids: HashSet<String> = state
        .get("trash")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(|e| e.get("trash_id").and_then(Value::as_str).map(str::to_owned))
                .collect()
        })
        .unwrap_or_default();
    let mut merged = 0usize;
    let groups = {
        let games = state
            .get("games")
            .and_then(Value::as_array)
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        detect_duplicate_identities(games, true)
    };
    for group in groups {
        let g_ids: Vec<String> = group
            .get("games")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .map(|v| match v {
                        Value::String(s) => s.clone(),
                        other => other.to_string(),
                    })
                    .collect()
            })
            .unwrap_or_default();
        if !g_ids.iter().any(|gid| wanted.contains(gid)) {
            continue;
        }
        let state_snap = state.clone();
        let games_cur = state
            .get("games")
            .and_then(Value::as_array)
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        let by_id = index_by_game_id(games_cur);
        let members: Vec<usize> = g_ids
            .iter()
            .filter_map(|gid| by_id.get(gid).copied())
            .collect();
        if members.len() < 2 {
            continue;
        }
        let mut factory = |g: &Value| make_trash_entry(&state_snap, g);
        if let Ok(res) = apply_merge(state, &members, Some(&mut factory)) {
            merged += res.get("merged").and_then(Value::as_u64).unwrap_or(0) as usize;
        }
    }
    let new_ids: Vec<Value> = state
        .get("trash")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(|e| e.get("trash_id").and_then(Value::as_str))
                .filter(|tid| !before_ids.contains(*tid))
                .map(|s| Value::String(s.to_owned()))
                .collect()
        })
        .unwrap_or_default();
    json!({"merged": merged, "trash_ids": new_ids})
}

fn plan_file_integrity(state: &Value, issues: &[Value], folder: &str) -> Result<Value, String> {
    if folder.is_empty() {
        return Ok(json!({"needs_folder": true}));
    }
    let folder_path = resolve_folder(folder)?;
    let candidates = scan_candidates(
        &folder_path.to_string_lossy(),
        MAX_CANDIDATES,
        MAX_FOLDER_DEPTH,
    )?;
    let scan = scan_missing_paths(state, true, MAX_SCAN_ITEMS, None);
    let wanted_ids: HashSet<String> = issues
        .iter()
        .filter_map(|i| i.get("game_id").and_then(Value::as_str).map(str::to_owned))
        .collect();
    let wanted_idxs: HashSet<u64> = issues
        .iter()
        .filter_map(|i| i.get("index").and_then(Value::as_u64))
        .collect();
    let items: Vec<Value> = scan
        .get("items")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .filter(|it| {
            it.get("game_id")
                .and_then(Value::as_str)
                .is_some_and(|s| wanted_ids.contains(s))
                || it
                    .get("id")
                    .and_then(Value::as_u64)
                    .is_some_and(|i| wanted_idxs.contains(&i))
        })
        .collect();
    let mut plan = plan_repair(&items, &candidates, None);
    if let Some(m) = plan.as_object_mut() {
        m.insert(
            "folder".into(),
            Value::String(folder_path.to_string_lossy().into_owned()),
        );
    }
    Ok(plan)
}

fn build_fix_plan(
    app: &AppState,
    state: &Value,
    dimension: &str,
    issues: &[Value],
    payload: &Map<String, Value>,
) -> Result<Value, ApiError> {
    match dimension {
        "duplicates" => {
            let plans = plan_duplicates(state, issues);
            let merges: Vec<Value> = plans
                .into_iter()
                .map(|p| {
                    json!({
                        "primary": p["primary"],
                        "absorbed": p["absorbed"],
                        "changed_fields": p["changed_fields"],
                    })
                })
                .collect();
            Ok(json!({"kind": "merge", "merges": merges, "undo": "trash"}))
        }
        "artwork" => {
            let games = state
                .get("games")
                .and_then(Value::as_array)
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            let media_root = app.data_dir.join("media");
            let report = build_report(games, Some(&media_root), 200, true);
            let mut field_set: BTreeSet<String> = issues
                .iter()
                .filter(|i| i.get("code").and_then(Value::as_str) == Some("missing_artwork"))
                .filter_map(|i| i.get("detail").and_then(Value::as_str).map(str::to_owned))
                .filter(|s| !s.is_empty())
                .collect();
            field_set.insert("cover".to_owned());
            let fields: Vec<String> = field_set.into_iter().collect();
            let gids: Vec<String> = issues
                .iter()
                .filter_map(|i| i.get("game_id").and_then(Value::as_str).map(str::to_owned))
                .collect();
            let targets = select_fixable(&report, None, Some(&fields), Some(&gids));
            Ok(json!({
                "kind": "artwork_doctor",
                "targets": targets,
                "fields": fields,
                "undo": "artwork_doctor",
            }))
        }
        "file_integrity" => {
            let folder = str_field(payload, "folder");
            let plan = plan_file_integrity(state, issues, folder).map_err(ApiError::bad_request)?;
            if plan
                .get("needs_folder")
                .and_then(Value::as_bool)
                .unwrap_or(false)
            {
                return Ok(json!({
                    "kind": "repair_wizard",
                    "needs_folder": true,
                    "detail": "Pick the folder the files moved to; only unambiguous matches are relinked.",
                }));
            }
            let matches: Vec<Value> = plan
                .get("matches")
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .map(|r| {
                            json!({
                                "id": r["id"],
                                "game_id": r["game_id"],
                                "field": r["field"],
                                "from": r["from"],
                                "to": r["path"],
                            })
                        })
                        .collect()
                })
                .unwrap_or_default();
            Ok(json!({
                "kind": "relink",
                "folder": plan["folder"],
                "matches": matches,
                "ambiguous": plan["ambiguous"],
                "unmatched": plan["unmatched"],
                "undo": "field_restore",
                "note": "Only unambiguous matches are applied; ambiguous rows need the repair wizard.",
            }))
        }
        "metadata" => {
            let gids: BTreeSet<String> = issues
                .iter()
                .filter_map(|i| i.get("game_id").and_then(Value::as_str).map(str::to_owned))
                .collect();
            Ok(json!({
                "kind": "manual",
                "action": "open_editor",
                "game_ids": gids.into_iter().collect::<Vec<_>>(),
                "detail": "Metadata is never auto-filled; open the affected games in the metadata editor.",
            }))
        }
        "launch_readiness" => {
            let plats: BTreeSet<String> = issues
                .iter()
                .filter(|i| i.get("code").and_then(Value::as_str) == Some("no_emulator"))
                .filter_map(|i| i.get("detail").and_then(Value::as_str).map(str::to_owned))
                .collect();
            let gids: BTreeSet<String> = issues
                .iter()
                .filter_map(|i| i.get("game_id").and_then(Value::as_str).map(str::to_owned))
                .collect();
            Ok(json!({
                "kind": "manual",
                "action": "open_emulator_profiles",
                "platforms": plats.into_iter().collect::<Vec<_>>(),
                "game_ids": gids.into_iter().collect::<Vec<_>>(),
                "detail": "Assign an emulator profile for the affected platform.",
            }))
        }
        _ => Err(ApiError::bad_request(format!(
            "Unknown dimension: {dimension}."
        ))),
    }
}

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    match ctx.path {
        "/api/v2/health" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let games = state
                .get("games")
                .and_then(Value::as_array)
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            let probe = |p: &str, file_only: bool| ctx.app.media_probe.probe_path(p, file_only);
            let detected = detect_issues(games, &state, Some(&probe));
            let legacy: Vec<&Value> = detected
                .iter()
                .filter(|i| i.get("legacy").is_some_and(Value::is_object))
                .collect();
            let missing = legacy
                .iter()
                .filter(|i| i["legacy"]["type"] == "Missing game")
                .count();
            let duplicates = legacy
                .iter()
                .filter(|i| i["legacy"]["type"] == "Duplicate")
                .count();
            let missing_media = legacy
                .iter()
                .filter(|i| i["legacy"]["type"] == "Missing box front")
                .count();
            let unconfigured = games
                .iter()
                .filter(|g| {
                    g.get("path")
                        .and_then(Value::as_str)
                        .is_none_or(str::is_empty)
                        && !g
                            .get("manual_entry")
                            .and_then(Value::as_bool)
                            .unwrap_or(false)
                })
                .count();
            let issues: Vec<Value> = legacy
                .iter()
                .map(|i| {
                    let src = i.get("source").and_then(Value::as_str).unwrap_or("Local");
                    let sl = i
                        .get("source_label")
                        .and_then(Value::as_str)
                        .filter(|s| !s.is_empty())
                        .unwrap_or(src);
                    json!({
                        "id": i["index"],
                        "game": i["name"],
                        "type": i["legacy"]["type"],
                        "detail": i["legacy"]["detail"],
                        "source": src,
                        "source_label": sl,
                    })
                })
                .collect();
            Some(json_response(
                StatusCode::OK,
                &json!({
                    "games": games.len(),
                    "missing": missing,
                    "duplicates": duplicates,
                    "unconfigured": unconfigured,
                    "missing_media": missing_media,
                    "issues": issues,
                }),
                ctx.instance,
            ))
        }
        "/api/v2/library/health" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let games_len = state
                .get("games")
                .and_then(Value::as_array)
                .map(Vec::len)
                .unwrap_or(0);
            let cache = get_health_cache(&state);
            let dirty = cache
                .get("dirty_ids")
                .and_then(Value::as_array)
                .is_some_and(|a| !a.is_empty())
                || cache
                    .get("full_rescan_needed")
                    .and_then(Value::as_bool)
                    .unwrap_or(false);
            let mut snap = if cache.get("score").is_some_and(|v| !v.is_null())
                && !dirty
                && cache.get("game_count").and_then(Value::as_u64) == Some(games_len as u64)
            {
                public_snapshot(Some(&cache))
            } else {
                public_snapshot(None)
            };
            if let Some(m) = snap.as_object_mut() {
                m.insert("recomputed".into(), Value::Bool(false));
            }
            Some(json_response(StatusCode::OK, &snap, ctx.instance))
        }
        "/api/v2/library/health/issues" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let dimension_owned = ctx.query_first("dimension").unwrap_or_default();
            let dimension = dimension_owned.trim();
            if !dimension.is_empty() && !is_valid_dimension(dimension) {
                return Some(api_error_response(
                    &ApiError::bad_request(format!("Unknown dimension: {dimension}.")),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let limit: usize = match ctx.query_first("limit").filter(|s| !s.is_empty()) {
                None => 50,
                Some(raw) => match raw.parse::<i64>() {
                    Ok(n) => n.clamp(1, 500) as usize,
                    Err(_) => {
                        return Some(api_error_response(
                            &ApiError::bad_request("limit must be an integer."),
                            ctx.request_id,
                            ctx.instance,
                        ));
                    }
                },
            };
            let offset: usize = match ctx.query_first("offset").filter(|s| !s.is_empty()) {
                None => 0,
                Some(raw) => match raw.parse::<i64>() {
                    Ok(n) => n.max(0) as usize,
                    Err(_) => {
                        return Some(api_error_response(
                            &ApiError::bad_request("offset must be an integer."),
                            ctx.request_id,
                            ctx.instance,
                        ));
                    }
                },
            };
            let games_len = state
                .get("games")
                .and_then(Value::as_array)
                .map(Vec::len)
                .unwrap_or(0);
            let cache = get_health_cache(&state);
            let (mut deductions, scanned) = if let Some(deds) =
                cache.get("deductions").and_then(Value::as_array)
                && !deds.is_empty()
                && cache.get("game_count").and_then(Value::as_u64) == Some(games_len as u64)
            {
                (deds.clone(), true)
            } else {
                (Vec::new(), false)
            };
            let source_owned = ctx.query_first("source").unwrap_or_default();
            let source = source_owned.trim();
            if !source.is_empty() {
                deductions.retain(|item| {
                    item.get("source")
                        .and_then(Value::as_str)
                        .unwrap_or("Local")
                        == source
                });
            }
            if !dimension.is_empty() {
                deductions.retain(|item| {
                    item.get("dimension").and_then(Value::as_str) == Some(dimension)
                });
            }
            let total = deductions.len();
            let page: Vec<Value> = deductions
                .into_iter()
                .skip(offset)
                .take(limit)
                .map(|item| {
                    let src = item
                        .get("source")
                        .and_then(Value::as_str)
                        .unwrap_or("Local");
                    let sl = item
                        .get("source_label")
                        .and_then(Value::as_str)
                        .filter(|s| !s.is_empty())
                        .unwrap_or(src);
                    json!({
                        "game_id": item.get("game_id").and_then(Value::as_str).unwrap_or(""),
                        "index": item.get("index").cloned().unwrap_or(Value::Null),
                        "name": item.get("name").and_then(Value::as_str).unwrap_or(""),
                        "dimension": item.get("dimension").and_then(Value::as_str).unwrap_or(""),
                        "code": item.get("code").and_then(Value::as_str).unwrap_or(""),
                        "reason": item.get("reason").and_then(Value::as_str).unwrap_or(""),
                        "detail": item.get("detail").and_then(Value::as_str).unwrap_or(""),
                        "points": item.get("points").and_then(Value::as_i64).unwrap_or(0),
                        "source": src,
                        "source_label": sl,
                    })
                })
                .collect();
            Some(json_response(
                StatusCode::OK,
                &json!({
                    "scanned": scanned,
                    "issues": page,
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                    "dimension": dimension,
                }),
                ctx.instance,
            ))
        }
        "/api/v2/library/repair" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let include_media = !matches!(
                ctx.query_first("media")
                    .as_deref()
                    .unwrap_or("1")
                    .trim()
                    .to_lowercase()
                    .as_str(),
                "0" | "false" | "no"
            );
            let scope = match repair_scope(&ctx.app, ctx.query_first("audit_group").as_deref()) {
                Ok(s) => s,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let res = scan_missing_paths(&state, include_media, MAX_SCAN_ITEMS, scope.as_ref());
            Some(json_response(StatusCode::OK, &res, ctx.instance))
        }
        "/api/v2/library/duplicates" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let include_title = !matches!(
                ctx.query_first("title")
                    .as_deref()
                    .unwrap_or("1")
                    .trim()
                    .to_lowercase()
                    .as_str(),
                "0" | "false" | "no"
            );
            let res = find_duplicates(&state, MAX_GROUPS, include_title);
            Some(json_response(StatusCode::OK, &res, ctx.instance))
        }
        _ => None,
    }
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/library/health/scan" => {
            let app = Arc::clone(&ctx.app);
            let job = match ctx
                .app
                .jobs
                .submit(SCAN_JOB_NAME, true, None, None, move |_jctx| {
                    run_health_scan_for(&app)
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
        "/api/v2/library/health/fix" => {
            let dimension = str_field(payload, "dimension");
            if !is_valid_dimension(dimension) {
                return Some(api_error_response(
                    &ApiError::bad_request(format!("Unknown dimension: {dimension}.")),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let issue_ids = payload.get("issue_ids");
            if let Some(v) = issue_ids
                && v != &Value::String("all".to_owned())
                && !v.is_array()
                && !v.is_null()
            {
                return Some(api_error_response(
                    &ApiError::bad_request("issue_ids must be a list of game ids or \"all\"."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let dry_run = payload
                .get("dry_run")
                .and_then(Value::as_bool)
                .unwrap_or(true);
            let state = match ctx.app.load_state() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let games = state
                .get("games")
                .and_then(Value::as_array)
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            let probe = |p: &str, file_only: bool| ctx.app.media_probe.probe_path(p, file_only);
            let scored = score_library(games, &state, Some(&probe));
            let deductions = scored
                .get("deductions")
                .and_then(Value::as_array)
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            let issues = select_issues(deductions, dimension, issue_ids);
            if issues.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("No fixable issues match this selection."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let token = state_token(&state).unwrap_or_default();
            if dry_run {
                return match build_fix_plan(&ctx.app, &state, dimension, &issues, payload) {
                    Ok(plan) => Some(json_response(
                        StatusCode::OK,
                        &json!({
                            "dry_run": true,
                            "dimension": dimension,
                            "issue_count": issues.len(),
                            "base_token": token,
                            "plan": plan,
                        }),
                        ctx.instance,
                    )),
                    Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
                };
            }
            let preview_token = str_field(payload, "base_token");
            if preview_token.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request(
                        "Refusing to fix without a dry-run preview: pass base_token from a preview.",
                    ),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if preview_token != token {
                return Some(api_error_response(
                    &ApiError::bad_request(
                        "Fix preview is stale: the library changed after the preview. Re-run the preview.",
                    ),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let fix_id = format!("healthfix-{}", &Uuid::new_v4().simple().to_string()[..12]);
            let (inverse, summary, undo_kind) = match dimension {
                "duplicates" => {
                    let res = match ctx
                        .app
                        .transact_state(|cur| Ok(execute_duplicates(cur, &issues)))
                    {
                        Ok((_, r)) => r,
                        Err(e) => {
                            return Some(state_error_response(&e, ctx.request_id, ctx.instance));
                        }
                    };
                    (
                        json!({"kind": "trash_restore", "trash_ids": res["trash_ids"]}),
                        json!({"merged": res["merged"]}),
                        "trash",
                    )
                }
                "artwork" => {
                    let plan = match build_fix_plan(&ctx.app, &state, dimension, &issues, payload) {
                        Ok(p) => p,
                        Err(e) => {
                            return Some(api_error_response(&e, ctx.request_id, ctx.instance));
                        }
                    };
                    let gids: Vec<Value> = issues
                        .iter()
                        .filter_map(|i| i.get("game_id").and_then(Value::as_str).map(str::to_owned))
                        .collect::<BTreeSet<_>>()
                        .into_iter()
                        .map(Value::String)
                        .collect();
                    let fields = plan
                        .get("fields")
                        .cloned()
                        .unwrap_or_else(|| json!(["cover"]));
                    let mut fix_req = Map::new();
                    fix_req.insert("game_ids".into(), Value::Array(gids.clone()));
                    fix_req.insert("fields".into(), fields);
                    fix_req.insert("limit".into(), json!(gids.len().max(1)));
                    let sg_ctx = RequestCtx {
                        app: Arc::clone(&ctx.app),
                        path: "/api/v2/steamgrid/hygiene/fix",
                        query: "",
                        headers: ctx.headers,
                        request_id: ctx.request_id,
                        instance: ctx.instance,
                    };
                    let resp = crate::handlers::steamgrid::handle_post(&sg_ctx, &fix_req);
                    let job_id = if let Some(r) = resp
                        && r.status() == StatusCode::ACCEPTED
                    {
                        ctx.app
                            .jobs
                            .snapshots()
                            .values()
                            .filter(|j| {
                                j.get("name")
                                    .and_then(Value::as_str)
                                    .is_some_and(|n| n.contains("hygiene"))
                            })
                            .filter_map(|j| {
                                j.get("job_id").and_then(Value::as_str).map(str::to_owned)
                            })
                            .next()
                            .unwrap_or_default()
                    } else {
                        return Some(api_error_response(
                            &ApiError::bad_request("Artwork Doctor refused the fix."),
                            ctx.request_id,
                            ctx.instance,
                        ));
                    };
                    (
                        json!({"kind": "artwork_doctor", "job_id": job_id}),
                        json!({"job_id": job_id, "games": gids.len()}),
                        "artwork_doctor",
                    )
                }
                "file_integrity" => {
                    let folder = str_field(payload, "folder");
                    let pre = match plan_file_integrity(&state, &issues, folder) {
                        Ok(p) => p,
                        Err(msg) => {
                            return Some(api_error_response(
                                &ApiError::bad_request(msg),
                                ctx.request_id,
                                ctx.instance,
                            ));
                        }
                    };
                    if pre
                        .get("needs_folder")
                        .and_then(Value::as_bool)
                        .unwrap_or(false)
                    {
                        return Some(api_error_response(
                            &ApiError::bad_request(
                                "file_integrity fixes need a folder: preview with one first.",
                            ),
                            ctx.request_id,
                            ctx.instance,
                        ));
                    }
                    let res = match ctx.app.transact_state(|cur| {
                        let live = match plan_file_integrity(cur, &issues, folder) {
                            Ok(p) => p,
                            Err(e) => return Ok(Err(e)),
                        };
                        let matches = live
                            .get("matches")
                            .and_then(Value::as_array)
                            .cloned()
                            .unwrap_or_default();
                        let before_rows: Vec<Value> = matches
                            .iter()
                            .map(|r| {
                                json!({
                                    "index": r["id"],
                                    "game_id": r["game_id"],
                                    "field": r["field"],
                                    "before": r["from"],
                                    "after": r["path"],
                                })
                            })
                            .collect();
                        let applied_res = match apply_repair(cur, &matches, None) {
                            Ok(r) => r,
                            Err(e) => return Ok(Err(e)),
                        };
                        let applied_set: HashSet<(u64, String)> = applied_res
                            .get("applied")
                            .and_then(Value::as_array)
                            .map(|a| {
                                a.iter()
                                    .filter_map(|r| {
                                        Some((
                                            r.get("id")?.as_u64()?,
                                            r.get("field")?.as_str()?.to_owned(),
                                        ))
                                    })
                                    .collect()
                            })
                            .unwrap_or_default();
                        let inverse_rows: Vec<Value> = before_rows
                            .into_iter()
                            .filter(|r| {
                                r.get("index")
                                    .and_then(Value::as_u64)
                                    .zip(r.get("field").and_then(Value::as_str))
                                    .is_some_and(|(i, f)| applied_set.contains(&(i, f.to_owned())))
                            })
                            .collect();
                        Ok(Ok(json!({
                            "updated": applied_res["updated"],
                            "skipped": applied_res["skipped"],
                            "inverse": inverse_rows,
                        })))
                    }) {
                        Ok((_, Ok(r))) => r,
                        Ok((_, Err(msg))) => {
                            return Some(api_error_response(
                                &ApiError::bad_request(msg),
                                ctx.request_id,
                                ctx.instance,
                            ));
                        }
                        Err(e) => {
                            return Some(state_error_response(&e, ctx.request_id, ctx.instance));
                        }
                    };
                    let skipped_len = res
                        .get("skipped")
                        .and_then(Value::as_array)
                        .map(Vec::len)
                        .unwrap_or(0);
                    (
                        json!({"kind": "field_restore", "changes": res["inverse"]}),
                        json!({"updated": res["updated"], "skipped": skipped_len}),
                        "field_restore",
                    )
                }
                _ => {
                    let plan = match build_fix_plan(&ctx.app, &state, dimension, &issues, payload) {
                        Ok(p) => p,
                        Err(e) => {
                            return Some(api_error_response(&e, ctx.request_id, ctx.instance));
                        }
                    };
                    return Some(json_response(
                        StatusCode::OK,
                        &json!({
                            "dry_run": false,
                            "dimension": dimension,
                            "action": plan["action"],
                            "detail": plan["detail"],
                            "executed": false,
                        }),
                        ctx.instance,
                    ));
                }
            };

            ctx.app.media_probe.clear_caches();
            let gids: Vec<String> = issues
                .iter()
                .filter_map(|i| i.get("game_id").and_then(Value::as_str).map(str::to_owned))
                .collect::<BTreeSet<_>>()
                .into_iter()
                .collect();
            let entry = json!({
                "fix_id": fix_id,
                "dimension": dimension,
                "game_ids": gids,
                "issue_count": issues.len(),
                "inverse": inverse,
                "summary": summary,
            });
            let _ = ctx.app.transact_state(|cur| {
                record_fix(cur, entry);
                Ok(())
            });
            Some(json_response(
                StatusCode::OK,
                &json!({
                    "dry_run": false,
                    "dimension": dimension,
                    "fix_id": fix_id,
                    "summary": summary,
                    "undo": {"kind": undo_kind, "fix_id": fix_id},
                }),
                ctx.instance,
            ))
        }
        "/api/v2/library/health/undo" => {
            let fix_id = str_field(payload, "fix_id");
            if fix_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("fix_id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let state = match ctx.app.load_state() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let journal = get_fix_journal(&state);
            let Some(entry) = journal
                .into_iter()
                .find(|e| e.get("fix_id").and_then(Value::as_str) == Some(fix_id))
            else {
                return Some(api_error_response(
                    &ApiError::bad_request("Fix not found in the journal."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            if entry
                .get("undone")
                .and_then(Value::as_bool)
                .unwrap_or(false)
            {
                return Some(api_error_response(
                    &ApiError::bad_request("Fix was already undone."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let inverse = entry.get("inverse").cloned().unwrap_or_else(|| json!({}));
            let kind = inverse.get("kind").and_then(Value::as_str).unwrap_or("");
            let undone_count: usize = match kind {
                "trash_restore" => {
                    let tids: Vec<String> = inverse
                        .get("trash_ids")
                        .and_then(Value::as_array)
                        .map(|a| {
                            a.iter()
                                .filter_map(Value::as_str)
                                .map(str::to_owned)
                                .collect()
                        })
                        .unwrap_or_default();
                    let mut cnt = 0usize;
                    let lib_ctx = RequestCtx {
                        app: Arc::clone(&ctx.app),
                        path: "/api/v2/library/trash/restore",
                        query: "",
                        headers: ctx.headers,
                        request_id: ctx.request_id,
                        instance: ctx.instance,
                    };
                    for tid in tids {
                        let mut req = Map::new();
                        req.insert("trash_id".into(), Value::String(tid));
                        if let Some(resp) = crate::handlers::library::handle_post(&lib_ctx, &req)
                            && resp.status() == StatusCode::OK
                        {
                            cnt += 1;
                        }
                    }
                    cnt
                }
                "field_restore" => {
                    let changes = inverse
                        .get("changes")
                        .and_then(Value::as_array)
                        .cloned()
                        .unwrap_or_default();
                    match ctx.app.transact_state(|cur| {
                        let mut restored = 0usize;
                        if let Some(games) = cur.get_mut("games").and_then(Value::as_array_mut) {
                            for ch in &changes {
                                let gid = ch.get("game_id").and_then(Value::as_str).unwrap_or("");
                                let field = ch.get("field").and_then(Value::as_str).unwrap_or("");
                                let after = ch.get("after").and_then(Value::as_str).unwrap_or("");
                                let before = ch.get("before").and_then(Value::as_str).unwrap_or("");
                                if let Some(g) = games
                                    .iter_mut()
                                    .find(|x| x.get("game_id").and_then(Value::as_str) == Some(gid))
                                    .and_then(Value::as_object_mut)
                                    && g.get(field).and_then(Value::as_str).unwrap_or("") == after
                                {
                                    g.insert(field.to_owned(), Value::String(before.to_owned()));
                                    restored += 1;
                                }
                            }
                        }
                        Ok(restored)
                    }) {
                        Ok((_, c)) => c,
                        Err(e) => {
                            return Some(state_error_response(&e, ctx.request_id, ctx.instance));
                        }
                    }
                }
                "artwork_doctor" => {
                    let job_id = inverse.get("job_id").and_then(Value::as_str).unwrap_or("");
                    let batch_id = find_hygiene_batch(&ctx.app, job_id);
                    let Some(bid) = batch_id else {
                        return Some(json_response(
                            StatusCode::OK,
                            &json!({
                                "undone": false,
                                "fix_id": fix_id,
                                "action": "artwork_doctor",
                                "detail": "The Artwork Doctor job has no undo batch yet; undo it from the Artwork Doctor batches list.",
                            }),
                            ctx.instance,
                        ));
                    };
                    let cache_dir = ctx.app.data_dir.join("cache");
                    let manifest = match load_undo_manifest(&cache_dir, &bid) {
                        Ok(m) => m,
                        Err(msg) => {
                            return Some(api_error_response(
                                &ApiError::bad_request(format!("Artwork undo failed: {msg}")),
                                ctx.request_id,
                                ctx.instance,
                            ));
                        }
                    };
                    let restored = match undo_batch(&cache_dir, &bid) {
                        Ok(r) => r,
                        Err(msg) => {
                            return Some(api_error_response(
                                &ApiError::bad_request(format!("Artwork undo failed: {msg}")),
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
                    let count = match ctx.app.transact_state(|cur| {
                        let mut changed = 0usize;
                        if let Some(games) = cur.get_mut("games").and_then(Value::as_array_mut) {
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
                                            changed += 1;
                                        }
                                    } else if act == "removed" {
                                        m.insert(field.to_owned(), Value::String(String::new()));
                                        changed += 1;
                                    }
                                }
                            }
                        }
                        Ok(changed)
                    }) {
                        Ok((_, c)) => c,
                        Err(e) => {
                            return Some(state_error_response(&e, ctx.request_id, ctx.instance));
                        }
                    };
                    ctx.app.bump_media_epoch();
                    count
                }
                _ => {
                    return Some(api_error_response(
                        &ApiError::bad_request(format!("Unknown undo kind: {kind}.")),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let _ = ctx.app.transact_state(|cur| {
                mark_fix_undone(cur, fix_id);
                Ok(())
            });
            ctx.app.media_probe.clear_caches();
            Some(json_response(
                StatusCode::OK,
                &json!({"undone": true, "fix_id": fix_id, "restored": undone_count}),
                ctx.instance,
            ))
        }
        "/api/v2/library/repair/preview" => {
            let folder = match resolve_folder(str_field(payload, "folder")) {
                Ok(f) => f,
                Err(msg) => {
                    return Some(api_error_response(
                        &ApiError::bad_request(msg),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let fields: Option<HashSet<String>> = match payload.get("fields") {
                None | Some(Value::Null) => None,
                Some(Value::Array(arr)) => Some(
                    arr.iter()
                        .filter_map(Value::as_str)
                        .map(str::to_owned)
                        .collect(),
                ),
                _ => {
                    return Some(api_error_response(
                        &ApiError::bad_request("fields must be a list of field names."),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let include_media = payload
                .get("include_media")
                .and_then(Value::as_bool)
                .unwrap_or(true);
            let candidates = match scan_candidates(
                &folder.to_string_lossy(),
                MAX_CANDIDATES,
                MAX_FOLDER_DEPTH,
            ) {
                Ok(c) => c,
                Err(msg) => {
                    return Some(api_error_response(
                        &ApiError::bad_request(msg),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let audit_grp = payload.get("audit_group").and_then(Value::as_str);
            let scope = match repair_scope(&ctx.app, audit_grp) {
                Ok(s) => s,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let scan = scan_missing_paths(&state, include_media, MAX_SCAN_ITEMS, scope.as_ref());
            let items = scan
                .get("items")
                .and_then(Value::as_array)
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            let mut plan = plan_repair(items, &candidates, fields.as_ref());
            if let Some(m) = plan.as_object_mut() {
                m.insert(
                    "folder".into(),
                    Value::String(folder.to_string_lossy().into_owned()),
                );
                m.insert("scanned".into(), scan["count"].clone());
                m.insert("candidates".into(), json!(candidates.len()));
            }
            Some(json_response(StatusCode::OK, &plan, ctx.instance))
        }
        "/api/v2/library/repair/apply" => {
            let folder = match resolve_folder(str_field(payload, "folder")) {
                Ok(f) => f,
                Err(msg) => {
                    return Some(api_error_response(
                        &ApiError::bad_request(msg),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let fields: Option<HashSet<String>> = match payload.get("fields") {
                None | Some(Value::Null) => None,
                Some(Value::Array(arr)) => Some(
                    arr.iter()
                        .filter_map(Value::as_str)
                        .map(str::to_owned)
                        .collect(),
                ),
                _ => {
                    return Some(api_error_response(
                        &ApiError::bad_request("fields must be a list of field names."),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let selection = match payload.get("selection") {
                None | Some(Value::Null) => None,
                Some(Value::Array(arr)) => Some(arr.clone()),
                _ => {
                    return Some(api_error_response(
                        &ApiError::bad_request(
                            "selection must be a list of [id, field] pairs or ids.",
                        ),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let include_media = payload
                .get("include_media")
                .and_then(Value::as_bool)
                .unwrap_or(true);
            let candidates = match scan_candidates(
                &folder.to_string_lossy(),
                MAX_CANDIDATES,
                MAX_FOLDER_DEPTH,
            ) {
                Ok(c) => c,
                Err(msg) => {
                    return Some(api_error_response(
                        &ApiError::bad_request(msg),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let audit_grp = payload
                .get("audit_group")
                .and_then(Value::as_str)
                .map(str::to_owned);
            let scope = match repair_scope(&ctx.app, audit_grp.as_deref()) {
                Ok(s) => s,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let res = ctx.app.transact_state(|cur| {
                let scan = scan_missing_paths(cur, include_media, MAX_SCAN_ITEMS, scope.as_ref());
                let items = scan
                    .get("items")
                    .and_then(Value::as_array)
                    .map(Vec::as_slice)
                    .unwrap_or(&[]);
                let plan = plan_repair(items, &candidates, fields.as_ref());
                let matches = plan
                    .get("matches")
                    .and_then(Value::as_array)
                    .cloned()
                    .unwrap_or_default();
                Ok(apply_repair(cur, &matches, selection.as_deref()))
            });
            ctx.app.media_probe.clear_caches();
            match res {
                Ok((_, Ok(out))) => Some(json_response(StatusCode::OK, &out, ctx.instance)),
                Ok((_, Err(msg))) => Some(api_error_response(
                    &ApiError::bad_request(msg),
                    ctx.request_id,
                    ctx.instance,
                )),
                Err(e) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/library/duplicates/preview" => {
            let Some(arr) = payload.get("ids").and_then(Value::as_array) else {
                return Some(api_error_response(
                    &ApiError::bad_request("ids must be a list of game indexes."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let indexes: Vec<usize> = arr
                .iter()
                .filter_map(|v| v.as_u64().map(|n| n as usize))
                .collect();
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            match merge_plan(&state, &indexes) {
                Ok(plan) => Some(json_response(StatusCode::OK, &plan, ctx.instance)),
                Err(msg) => Some(api_error_response(
                    &ApiError::bad_request(msg),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/library/duplicates/merge" => {
            let Some(arr) = payload.get("ids").and_then(Value::as_array) else {
                return Some(api_error_response(
                    &ApiError::bad_request("A merge needs at least two game indexes."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let indexes: Vec<usize> = arr
                .iter()
                .filter_map(|v| v.as_u64().map(|n| n as usize))
                .collect();
            if indexes.len() < 2 {
                return Some(api_error_response(
                    &ApiError::bad_request("A merge needs at least two game indexes."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let _ = bool_field(payload, "confirm");
            let res = ctx.app.transact_state(|cur| {
                let snap = cur.clone();
                let mut factory = |g: &Value| make_trash_entry(&snap, g);
                Ok(apply_merge(cur, &indexes, Some(&mut factory)))
            });
            ctx.app.media_probe.clear_caches();
            match res {
                Ok((_, Ok(out))) => Some(json_response(StatusCode::OK, &out, ctx.instance)),
                Ok((_, Err(msg))) => Some(api_error_response(
                    &ApiError::bad_request(msg),
                    ctx.request_id,
                    ctx.instance,
                )),
                Err(e) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            }
        }
        _ => None,
    }
}

fn find_hygiene_batch(app: &AppState, job_id: &str) -> Option<String> {
    if job_id.is_empty() {
        return None;
    }
    for job in app.jobs.snapshots().values() {
        if job.get("job_id").and_then(Value::as_str) == Some(job_id)
            && let Some(bid) = job
                .get("result")
                .and_then(|r| r.get("batch_id"))
                .and_then(Value::as_str)
        {
            return Some(bid.to_owned());
        }
    }
    for job in app.jobs.history(50) {
        if job.get("job_id").and_then(Value::as_str) == Some(job_id)
            && let Some(bid) = job
                .get("result")
                .and_then(|r| r.get("batch_id"))
                .and_then(Value::as_str)
        {
            return Some(bid.to_owned());
        }
    }
    None
}
