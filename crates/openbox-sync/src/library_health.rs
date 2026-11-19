//! Library health scoring engine (`pkg/parity/parity_library_health.py`, ADR 0053).

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::path::Path;

use jiff::Timestamp;
use openbox_imports::identity::{
    cross_source_identity, detect_duplicate_identities, game_identity_key, normalize_identity,
};
use openbox_runtime::media_probe::MEDIA_TYPES_ALL;
use serde_json::{Map, Value, json};

pub const DIMENSIONS: &[(&str, i64)] = &[
    ("file_integrity", 35),
    ("duplicates", 20),
    ("artwork", 20),
    ("metadata", 15),
    ("launch_readiness", 10),
];

pub const DEDUCT_MISSING_GAME: i64 = 60;
pub const DEDUCT_MISSING_EXTRA: i64 = 10;
pub const DEDUCT_MISSING_EXTRA_CAP: i64 = 30;
pub const DEDUCT_MISSING_SAVE_PATH: i64 = 5;
pub const DEDUCT_MISSING_SAVE_PATH_CAP: i64 = 10;
pub const DEDUCT_DUPLICATE: i64 = 50;
pub const DEDUCT_METADATA_FIELD: i64 = 15;
pub const DEDUCT_NO_EMULATOR: i64 = 60;
pub const DEDUCT_BROKEN: i64 = 40;
pub const ARTWORK_DEDUCT_OTHER: i64 = 5;

pub const METADATA_FIELDS: &[&str] = &[
    "name",
    "platform",
    "genre",
    "year",
    "developer",
    "description",
];

pub const ROM_SUFFIXES: &[&str] = &[
    ".rom", ".nes", ".sfc", ".smc", ".gba", ".gb", ".gbc", ".iso",
];

const OPTIONAL_MEDIA_FIELDS: &[&str] = &[
    "poster",
    "square",
    "video",
    "video_snap",
    "video_theme",
    "video_trailer",
    "music",
    "video_recording",
];

pub const HEALTH_CACHE_KEY: &str = "health_cache";
pub const FIX_JOURNAL_KEY: &str = "health_fixes";
pub const FIX_JOURNAL_CAP: usize = 50;
pub const DIRTY_IDS_CAP: usize = 5000;
pub const RESCAN_OPTIONS: &[&str] = &["daily", "weekly", "on_startup", "off"];
pub const DEFAULT_RESCAN: &str = "weekly";

fn artwork_deduction(field: &str) -> i64 {
    match field {
        "cover" => 40,
        "banner" | "icon" => 15,
        "clear_logo" | "fanart" | "background" | "screenshots" => 10,
        _ => ARTWORK_DEDUCT_OTHER,
    }
}

fn utcnow_iso() -> String {
    let dt = jiff::tz::TimeZone::UTC.to_datetime(Timestamp::now());
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}+00:00",
        dt.year(),
        dt.month(),
        dt.day(),
        dt.hour(),
        dt.minute(),
        dt.second()
    )
}

fn game_key(game: &Value, index: usize) -> String {
    if let Some(s) = game
        .get("game_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        return s.to_owned();
    }
    if let Some(id) = game.get("id") {
        if let Some(s) = id.as_str().filter(|s| !s.is_empty()) {
            return s.to_owned();
        }
        if let Some(n) = id.as_i64() {
            return n.to_string();
        }
    }
    index.to_string()
}

pub fn default_probe(path: &str, file_only: bool) -> bool {
    if path.is_empty() {
        return false;
    }
    let p = Path::new(path);
    if file_only { p.is_file() } else { p.exists() }
}

fn media_fields_for_health() -> Vec<&'static str> {
    MEDIA_TYPES_ALL
        .iter()
        .copied()
        .filter(|f| *f != "cover" && !OPTIONAL_MEDIA_FIELDS.contains(f))
        .collect()
}

#[allow(clippy::too_many_arguments)]
fn make_issue(
    dimension: &str,
    code: &str,
    index: usize,
    game: &Value,
    points: i64,
    detail: &str,
    reason: &str,
    legacy: Option<Value>,
) -> Value {
    let source = game
        .get("source")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .unwrap_or("Local");
    let source_label = game
        .get("plugin_source_name")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .unwrap_or(source);
    let r = if reason.is_empty() {
        code.replace('_', " ")
    } else {
        reason.to_owned()
    };
    json!({
        "dimension": dimension,
        "code": code,
        "index": index,
        "game_id": game_key(game, index),
        "name": game.get("name").and_then(Value::as_str).unwrap_or(""),
        "source": source,
        "source_label": source_label,
        "detail": detail,
        "points": points,
        "reason": r,
        "legacy": legacy.unwrap_or(Value::Null),
    })
}

fn build_dup_index(games: &[Value]) -> HashMap<String, String> {
    let mut dup_index = HashMap::new();
    for group in detect_duplicate_identities(games, true) {
        let ident = group
            .get("identity")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        if ident.is_empty() {
            continue;
        }
        if let Some(members) = group.get("games").and_then(Value::as_array) {
            for m in members {
                if let Some(s) = m.as_str() {
                    dup_index.insert(s.to_owned(), ident.clone());
                } else if let Some(n) = m.as_i64() {
                    dup_index.insert(n.to_string(), ident.clone());
                }
            }
        }
    }
    dup_index
}

fn game_identity_str(game: &Value, dup_index: &HashMap<String, String>) -> String {
    let gid = game
        .get("game_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .or_else(|| {
            game.get("id")
                .and_then(Value::as_i64)
                .map(|n| n.to_string())
        });
    if let Some(ref k) = gid
        && let Some(ident) = dup_index.get(k)
    {
        return ident.clone();
    }
    if let Some(cs) = cross_source_identity(game) {
        return cs;
    }
    if let Some(norm) = normalize_identity(game) {
        return norm;
    }
    game_identity_key(game).join(":")
}

fn detect_for_game(
    game: &Value,
    index: usize,
    games: &[Value],
    state: &Value,
    dup_index: &HashMap<String, String>,
    seen: &mut HashMap<String, usize>,
    probe: &dyn Fn(&str, bool) -> bool,
) -> Vec<Value> {
    let mut issues = Vec::new();
    let ident = game_identity_str(game, dup_index);
    if let Some(&first_idx) = seen.get(&ident) {
        let match_name = games
            .get(first_idx)
            .and_then(|g| g.get("name"))
            .and_then(Value::as_str)
            .unwrap_or("");
        let det = format!("Matches {match_name}; identity {ident}");
        issues.push(make_issue(
            "duplicates",
            "duplicate",
            index,
            game,
            DEDUCT_DUPLICATE,
            &det,
            "Duplicate entry",
            Some(json!({"type": "Duplicate", "detail": det})),
        ));
    } else {
        seen.insert(ident, index);
    }

    if game
        .get("manual_entry")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        return issues;
    }

    let path_str = game.get("path").and_then(Value::as_str).unwrap_or("");
    if path_str.is_empty() || !probe(path_str, false) {
        issues.push(make_issue(
            "file_integrity",
            "missing_game",
            index,
            game,
            DEDUCT_MISSING_GAME,
            path_str,
            "Missing game file",
            Some(json!({"type": "Missing game", "detail": path_str})),
        ));
    }
    let cover_str = game.get("cover").and_then(Value::as_str).unwrap_or("");
    if !probe(cover_str, true) {
        issues.push(make_issue(
            "artwork",
            "missing_cover",
            index,
            game,
            artwork_deduction("cover"),
            "No local cover image",
            "Missing box front",
            Some(json!({"type": "Missing box front", "detail": "No local cover image"})),
        ));
    }

    let mut extra_missing = 0i64;
    for kind in ["applications", "versions", "documents"] {
        if let Some(arr) = game.get(kind).and_then(Value::as_array) {
            for extra in arr {
                let extra_path = extra.get("path").and_then(Value::as_str).unwrap_or("");
                if !probe(extra_path, false) {
                    extra_missing += 1;
                    let pts = DEDUCT_MISSING_EXTRA.min(
                        (DEDUCT_MISSING_EXTRA_CAP - (extra_missing - 1) * DEDUCT_MISSING_EXTRA)
                            .max(0),
                    );
                    issues.push(make_issue(
                        "file_integrity",
                        "missing_extra",
                        index,
                        game,
                        pts,
                        extra_path,
                        "Missing extra file",
                        Some(json!({"type": "Missing extra", "detail": extra_path})),
                    ));
                }
            }
        }
    }

    let mut save_missing = 0i64;
    if let Some(saves) = game.get("save_paths").and_then(Value::as_array) {
        for sp in saves {
            let sp_str = sp.as_str().unwrap_or("");
            if !probe(sp_str, false) {
                save_missing += 1;
                let pts = DEDUCT_MISSING_SAVE_PATH.min(
                    (DEDUCT_MISSING_SAVE_PATH_CAP - (save_missing - 1) * DEDUCT_MISSING_SAVE_PATH)
                        .max(0),
                );
                issues.push(make_issue(
                    "file_integrity",
                    "missing_save_path",
                    index,
                    game,
                    pts,
                    sp_str,
                    "Missing save path",
                    Some(json!({"type": "Missing save path", "detail": sp_str})),
                ));
            }
        }
    }

    let suffix = Path::new(path_str)
        .extension()
        .and_then(|e| e.to_str())
        .map(|s| format!(".{}", s.to_lowercase()))
        .unwrap_or_default();
    let launch_empty = game
        .get("launch")
        .and_then(Value::as_str)
        .is_none_or(str::is_empty);
    let platform = game.get("platform").and_then(Value::as_str).unwrap_or("");
    let has_profile = state
        .get("profiles")
        .and_then(|p| p.get(platform))
        .is_some_and(|v| !v.is_null() && v != &Value::String(String::new()));
    if ROM_SUFFIXES.contains(&suffix.as_str()) && launch_empty && !has_profile {
        let p_label = if platform.is_empty() {
            "Unspecified"
        } else {
            platform
        };
        issues.push(make_issue(
            "launch_readiness",
            "no_emulator",
            index,
            game,
            DEDUCT_NO_EMULATOR,
            p_label,
            "No emulator profile",
            Some(json!({"type": "No emulator", "detail": p_label})),
        ));
    }

    for field in media_fields_for_health() {
        if field == "screenshots" {
            continue;
        }
        let present = match game.get(field) {
            Some(Value::Array(arr)) => arr.iter().filter_map(Value::as_str).any(|p| probe(p, true)),
            Some(Value::String(s)) => probe(s, true),
            _ => false,
        };
        if !present {
            let reason = format!("Missing {}", field.replace('_', " "));
            issues.push(make_issue(
                "artwork",
                "missing_artwork",
                index,
                game,
                artwork_deduction(field),
                field,
                &reason,
                None,
            ));
        }
    }

    let shots_present = game
        .get("screenshots")
        .and_then(Value::as_array)
        .is_some_and(|arr| arr.iter().filter_map(Value::as_str).any(|p| probe(p, true)));
    if !shots_present {
        issues.push(make_issue(
            "artwork",
            "missing_artwork",
            index,
            game,
            artwork_deduction("screenshots"),
            "screenshots",
            "Missing screenshots",
            None,
        ));
    }

    for field in METADATA_FIELDS {
        let missing = match game.get(*field) {
            None | Some(Value::Null) => true,
            Some(Value::String(s)) => s.trim().is_empty(),
            Some(Value::Number(_)) => false,
            Some(other) => other.to_string().trim().is_empty(),
        };
        if missing {
            issues.push(make_issue(
                "metadata",
                "missing_metadata",
                index,
                game,
                DEDUCT_METADATA_FIELD,
                field,
                &format!("Missing {field}"),
                None,
            ));
        }
    }

    if game.get("broken").and_then(Value::as_bool).unwrap_or(false) {
        issues.push(make_issue(
            "launch_readiness",
            "broken",
            index,
            game,
            DEDUCT_BROKEN,
            "Flagged broken",
            "Flagged broken",
            None,
        ));
    }

    issues
}

pub type ProbeFn<'a> = &'a dyn Fn(&str, bool) -> bool;

pub fn detect_issues(games: &[Value], state: &Value, probe: Option<ProbeFn<'_>>) -> Vec<Value> {
    let default_fn = default_probe;
    let probe_fn: &dyn Fn(&str, bool) -> bool = probe.unwrap_or(&default_fn);
    let dup_index = build_dup_index(games);
    let mut seen: HashMap<String, usize> = HashMap::new();
    let mut issues = Vec::new();
    for (index, game) in games.iter().enumerate() {
        if !game.is_object() {
            continue;
        }
        issues.extend(detect_for_game(
            game, index, games, state, &dup_index, &mut seen, probe_fn,
        ));
    }
    issues
}

fn issue_to_deduction(issue: &Value) -> Value {
    let gid = issue.get("game_id").cloned().unwrap_or_else(|| json!(""));
    let src = issue
        .get("source")
        .and_then(Value::as_str)
        .unwrap_or("Local");
    let src_label = issue
        .get("source_label")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .unwrap_or(src);
    json!({
        "dimension": issue["dimension"],
        "points": issue["points"],
        "reason": issue["reason"],
        "game_id": gid,
        "game_ids": [issue["game_id"]],
        "code": issue["code"],
        "detail": issue["detail"],
        "index": issue["index"],
        "name": issue["name"],
        "source": src,
        "source_label": src_label,
    })
}

fn rescore(deductions: Vec<Value>, game_count: usize) -> Map<String, Value> {
    let mut dim_points: HashMap<&str, i64> = DIMENSIONS.iter().map(|(d, _)| (*d, 0)).collect();
    let mut dim_counts: HashMap<&str, usize> = DIMENSIONS.iter().map(|(d, _)| (*d, 0)).collect();
    for d in &deductions {
        if let Some(dim) = d.get("dimension").and_then(Value::as_str)
            && let Some(pt) = dim_points.get_mut(dim)
        {
            *pt += d.get("points").and_then(Value::as_i64).unwrap_or(0);
            *dim_counts.entry(dim).or_insert(0) += 1;
        }
    }
    let mut dims_obj = Map::new();
    let mut weighted_sum = 0i64;
    for (dim, weight) in DIMENSIONS {
        let sub = (100 - dim_points[dim]).max(0);
        weighted_sum += sub * *weight;
        dims_obj.insert(
            (*dim).into(),
            json!({
                "score": sub,
                "weight": weight,
                "issues": dim_counts[dim],
            }),
        );
    }
    let total = ((weighted_sum as f64) / 100.0).round() as i64;
    let mut out = Map::new();
    out.insert("score".into(), json!(total.clamp(0, 100)));
    out.insert("dimensions".into(), Value::Object(dims_obj));
    out.insert("deductions".into(), Value::Array(deductions));
    out.insert("game_count".into(), json!(game_count));
    out
}

fn compute_source_counts(games: &[Value], rows: &[Value]) -> (Vec<String>, Map<String, Value>) {
    let mut sources: BTreeSet<String> = BTreeSet::new();
    let mut counts: BTreeMap<String, (usize, usize, i64)> = BTreeMap::new();
    for g in games {
        if g.is_object() {
            let s = g
                .get("source")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .unwrap_or("Local")
                .to_owned();
            sources.insert(s.clone());
            counts.entry(s).or_insert((0, 0, 0)).0 += 1;
        }
    }
    for r in rows {
        let s = r
            .get("source")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .unwrap_or("Local")
            .to_owned();
        let entry = counts.entry(s).or_insert((0, 0, 0));
        entry.1 += 1;
        entry.2 += r.get("points").and_then(Value::as_i64).unwrap_or(0);
    }
    let mut map = Map::new();
    for (k, (g, i, p)) in counts {
        map.insert(k, json!({"games": g, "issues": i, "points": p}));
    }
    (sources.into_iter().collect(), map)
}

pub fn score_library(games: &[Value], state: &Value, probe: Option<ProbeFn<'_>>) -> Value {
    let detected = detect_issues(games, state, probe);
    let deductions: Vec<Value> = detected.iter().map(issue_to_deduction).collect();
    let (sources, source_counts) = compute_source_counts(games, &detected);
    let mut snap = rescore(deductions, games.len());
    snap.insert("computed_at".into(), Value::String(utcnow_iso()));
    snap.insert("dirty".into(), Value::Bool(false));
    snap.insert("full".into(), Value::Bool(true));
    snap.insert(
        "sources".into(),
        Value::Array(sources.into_iter().map(Value::String).collect()),
    );
    snap.insert("source_counts".into(), Value::Object(source_counts));
    Value::Object(snap)
}

pub fn merge_incremental(
    snapshot: &Value,
    games: &[Value],
    state: &Value,
    dirty_ids: &[String],
    probe: Option<ProbeFn<'_>>,
) -> Value {
    let dirty: HashSet<String> = dirty_ids.iter().cloned().collect();
    if dirty.is_empty() {
        let mut out = snapshot.as_object().cloned().unwrap_or_default();
        out.insert("dirty".into(), Value::Bool(false));
        return Value::Object(out);
    }
    let mut kept: Vec<Value> = snapshot
        .get("deductions")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .filter(|d| {
            !d.get("game_ids")
                .and_then(Value::as_array)
                .is_some_and(|arr| {
                    arr.iter()
                        .filter_map(Value::as_str)
                        .any(|gid| dirty.contains(gid))
                })
        })
        .collect();

    let mut by_id: HashMap<String, (usize, &Value)> = HashMap::new();
    for (idx, g) in games.iter().enumerate() {
        if g.is_object() {
            by_id.insert(game_key(g, idx), (idx, g));
        }
    }
    let default_fn = default_probe;
    let probe_fn: &dyn Fn(&str, bool) -> bool = probe.unwrap_or(&default_fn);
    let dup_index = build_dup_index(games);
    let mut seen: HashMap<String, usize> = HashMap::new();
    for (idx, g) in games.iter().enumerate() {
        if !g.is_object() || dirty.contains(&game_key(g, idx)) {
            continue;
        }
        let _ = detect_for_game(g, idx, games, state, &dup_index, &mut seen, probe_fn);
    }
    let mut fresh = Vec::new();
    for gid in &dirty {
        if let Some(&(idx, g)) = by_id.get(gid) {
            fresh.extend(detect_for_game(
                g, idx, games, state, &dup_index, &mut seen, probe_fn,
            ));
        }
    }
    kept.extend(fresh.iter().map(issue_to_deduction));
    let (sources, source_counts) = compute_source_counts(games, &kept);
    let mut out = rescore(kept, games.len());
    out.insert("computed_at".into(), Value::String(utcnow_iso()));
    out.insert("dirty".into(), Value::Bool(false));
    out.insert(
        "full".into(),
        Value::Bool(
            snapshot
                .get("full")
                .and_then(Value::as_bool)
                .unwrap_or(true),
        ),
    );
    out.insert(
        "sources".into(),
        Value::Array(sources.into_iter().map(Value::String).collect()),
    );
    out.insert("source_counts".into(), Value::Object(source_counts));
    Value::Object(out)
}

pub fn get_health_cache(state: &Value) -> Value {
    state
        .get(HEALTH_CACHE_KEY)
        .filter(|v| v.is_object())
        .cloned()
        .unwrap_or_else(|| json!({}))
}

pub fn store_health_cache(state: &mut Value, snapshot: &Value) {
    if let Some(m) = state.as_object_mut() {
        m.insert(HEALTH_CACHE_KEY.into(), snapshot.clone());
    }
}

pub fn mark_dirty_ids(state: &mut Value, game_ids: &[String]) {
    let Some(root) = state.as_object_mut() else {
        return;
    };
    let cache = root
        .entry(HEALTH_CACHE_KEY.to_owned())
        .or_insert_with(|| Value::Object(Map::new()));
    if !cache.is_object() {
        *cache = Value::Object(Map::new());
    }
    let Some(cm) = cache.as_object_mut() else {
        return;
    };
    let dirty = cm
        .entry("dirty_ids".to_owned())
        .or_insert_with(|| Value::Array(Vec::new()));
    if !dirty.is_array() {
        *dirty = Value::Array(Vec::new());
    }
    if let Some(arr) = dirty.as_array_mut() {
        for gid in game_ids {
            if !gid.is_empty() && !arr.iter().any(|x| x.as_str() == Some(gid.as_str())) {
                arr.push(Value::String(gid.clone()));
            }
        }
        if arr.len() > DIRTY_IDS_CAP {
            arr.clear();
            cm.insert("full_rescan_needed".into(), Value::Bool(true));
        }
    }
}

pub fn take_dirty_ids(state: &mut Value) -> Vec<String> {
    let Some(cm) = state
        .get_mut(HEALTH_CACHE_KEY)
        .and_then(Value::as_object_mut)
    else {
        return Vec::new();
    };
    cm.remove("full_rescan_needed");
    cm.remove("dirty_ids")
        .and_then(|v| v.as_array().cloned())
        .unwrap_or_default()
        .into_iter()
        .filter_map(|v| v.as_str().filter(|s| !s.is_empty()).map(str::to_owned))
        .collect()
}

pub fn record_fix(state: &mut Value, entry: Value) -> Value {
    let mut rec = entry.as_object().cloned().unwrap_or_default();
    rec.entry("at".to_owned())
        .or_insert_with(|| Value::String(utcnow_iso()));
    rec.entry("undone".to_owned()).or_insert(Value::Bool(false));
    let val = Value::Object(rec);
    if let Some(root) = state.as_object_mut() {
        let j = root
            .entry(FIX_JOURNAL_KEY.to_owned())
            .or_insert_with(|| Value::Array(Vec::new()));
        if !j.is_array() {
            *j = Value::Array(Vec::new());
        }
        if let Some(arr) = j.as_array_mut() {
            arr.push(val.clone());
            while arr.len() > FIX_JOURNAL_CAP {
                arr.remove(0);
            }
        }
    }
    val
}

pub fn get_fix_journal(state: &Value) -> Vec<Value> {
    state
        .get(FIX_JOURNAL_KEY)
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
}

pub fn mark_fix_undone(state: &mut Value, fix_id: &str) -> bool {
    let Some(arr) = state.get_mut(FIX_JOURNAL_KEY).and_then(Value::as_array_mut) else {
        return false;
    };
    for item in arr.iter_mut() {
        if item.get("fix_id").and_then(Value::as_str) == Some(fix_id)
            && let Some(m) = item.as_object_mut()
        {
            m.insert("undone".into(), Value::Bool(true));
            return true;
        }
    }
    false
}

pub fn normalize_rescan_setting(raw: &str) -> &'static str {
    match raw.trim().to_lowercase().as_str() {
        "daily" => "daily",
        "weekly" => "weekly",
        "on_startup" => "on_startup",
        "off" => "off",
        _ => DEFAULT_RESCAN,
    }
}

pub fn rescan_due(settings: &Value) -> bool {
    let mode = normalize_rescan_setting(
        settings
            .get("health_rescan")
            .and_then(Value::as_str)
            .unwrap_or(DEFAULT_RESCAN),
    );
    if matches!(mode, "off" | "on_startup") {
        return false;
    }
    let last = settings
        .get("last_health_rescan")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("");
    if last.is_empty() {
        return true;
    }
    let Ok(last_ts) = last.parse::<Timestamp>() else {
        return true;
    };
    let days = if mode == "daily" { 1 } else { 7 };
    (Timestamp::now().as_second() - last_ts.as_second()) >= days * 86400
}
