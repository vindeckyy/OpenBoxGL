//! Backlog Itinerary — dated multi-week backlog planner (`pkg/parity/parity_itinerary.py`, ADR 0113).

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

use jiff::Timestamp;
use jiff::civil::{Date, DateTime};
use openbox_core::ApiError;
use openbox_imports::catalog::total_playtime_seconds;
use openbox_io::atomic_write_text;
use openbox_metadata::igdb;
use serde_json::{Map, Value, json};
use uuid::Uuid;

use crate::radio::{eligible, is_completed, norm};

pub const VALID_STRATEGIES: &[&str] = &["quick-wins", "comeback", "raided"];
pub const DEFAULT_EST_HOURS: f64 = 8.0;
pub const MIN_EST_HOURS: f64 = 0.5;
pub const MIN_OWN_HISTORY_SAMPLES: usize = 3;
pub const IGDB_TTL_SECONDS: i64 = 30 * 86_400;
pub const HORIZON_DAYS_MIN: i64 = 7;
pub const HORIZON_DAYS_MAX: i64 = 365;
pub const MINUTES_PER_DAY_MIN: i64 = 5;
pub const MINUTES_PER_DAY_MAX: i64 = 960;
pub const DONE_PLAYTIME_RATIO: f64 = 1.15;

fn utc_now(now: Option<Timestamp>) -> Timestamp {
    now.unwrap_or_else(Timestamp::now)
}

fn utc_iso_z(ts: Timestamp) -> String {
    let dt = jiff::tz::TimeZone::UTC.to_datetime(ts);
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
        dt.year(),
        dt.month(),
        dt.day(),
        dt.hour(),
        dt.minute(),
        dt.second()
    )
}

pub fn clamp_horizon_days(raw: Option<&Value>, default: i64) -> i64 {
    let val = match raw {
        None | Some(Value::Null) => return default,
        Some(Value::String(s)) if s.trim().is_empty() => return default,
        Some(Value::Number(n)) => n.as_i64().unwrap_or(default),
        Some(Value::String(s)) => s.trim().parse::<i64>().unwrap_or(default),
        _ => return default,
    };
    val.clamp(HORIZON_DAYS_MIN, HORIZON_DAYS_MAX)
}

pub fn clamp_minutes_per_day(raw: Option<&Value>, default: i64) -> i64 {
    let val = match raw {
        None | Some(Value::Null) => return default,
        Some(Value::String(s)) if s.trim().is_empty() => return default,
        Some(Value::Number(n)) => n.as_i64().unwrap_or(default),
        Some(Value::String(s)) => s.trim().parse::<i64>().unwrap_or(default),
        _ => return default,
    };
    val.clamp(MINUTES_PER_DAY_MIN, MINUTES_PER_DAY_MAX)
}

fn trashed_ids(state: &Value) -> HashSet<String> {
    let mut out = HashSet::new();
    if let Some(arr) = state.get("trash").and_then(Value::as_array) {
        for entry in arr {
            if let Some(s) = entry.as_str().map(str::trim).filter(|s| !s.is_empty()) {
                out.insert(s.to_owned());
            } else if let Some(obj) = entry.as_object() {
                let gid = obj
                    .get("game_id")
                    .and_then(Value::as_str)
                    .map(str::trim)
                    .filter(|s| !s.is_empty())
                    .or_else(|| {
                        obj.get("game")
                            .and_then(|g| g.get("game_id"))
                            .and_then(Value::as_str)
                            .map(str::trim)
                            .filter(|s| !s.is_empty())
                    });
                if let Some(g) = gid {
                    out.insert(g.to_owned());
                }
            }
        }
    }
    out
}

pub fn is_eligible_for_itinerary(
    game: &Value,
    state: &Value,
    platforms: Option<&[String]>,
    trashed: Option<&HashSet<String>>,
) -> bool {
    if !eligible(game) {
        return false;
    }
    let gid = game
        .get("game_id")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("");
    if gid.is_empty() {
        return false;
    }
    let owned_trash;
    let tset = match trashed {
        Some(t) => t,
        None => {
            owned_trash = trashed_ids(state);
            &owned_trash
        }
    };
    if tset.contains(gid) {
        return false;
    }
    let prog = norm(game.get("progress"));
    if prog.contains("abandoned") {
        return false;
    }
    if let Some(raw_allowed) = state
        .get("settings")
        .and_then(|s| s.get("progress_values"))
        .and_then(Value::as_array)
        && !raw_allowed.is_empty()
        && !prog.is_empty()
    {
        let allowed_set: HashSet<String> = raw_allowed
            .iter()
            .filter_map(Value::as_str)
            .map(|s| s.trim().to_lowercase())
            .filter(|s| !s.is_empty())
            .collect();
        if !allowed_set.is_empty() && !allowed_set.contains(&prog) {
            return false;
        }
    }
    if let Some(plats) = platforms {
        let scope: HashSet<String> = plats
            .iter()
            .map(|p| p.trim().to_lowercase())
            .filter(|p| !p.is_empty())
            .collect();
        if !scope.is_empty() {
            let gplat = game
                .get("platform")
                .and_then(Value::as_str)
                .map(|s| s.trim().to_lowercase())
                .unwrap_or_default();
            if !scope.contains(&gplat) {
                return false;
            }
        }
    }
    true
}

fn median(values: &[f64]) -> f64 {
    let mut s = values.to_vec();
    s.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let n = s.len();
    if n % 2 == 1 {
        s[n / 2]
    } else {
        (s[n / 2 - 1] + s[n / 2]) / 2.0
    }
}

pub fn build_own_history_medians(games: &[Value]) -> (HashMap<String, f64>, Option<f64>) {
    let mut by_plat: HashMap<String, Vec<f64>> = HashMap::new();
    let mut all_hours: Vec<f64> = Vec::new();
    for g in games {
        if !g.is_object()
            || g.get("hidden").and_then(Value::as_bool).unwrap_or(false)
            || !is_completed(g)
        {
            continue;
        }
        let secs = total_playtime_seconds(g);
        if secs <= 0 {
            continue;
        }
        let hours = secs as f64 / 3600.0;
        let plat = g
            .get("platform")
            .and_then(Value::as_str)
            .map(|s| s.trim().to_lowercase())
            .unwrap_or_default();
        all_hours.push(hours);
        if !plat.is_empty() {
            by_plat.entry(plat).or_default().push(hours);
        }
    }
    let plat_medians: HashMap<String, f64> = by_plat
        .into_iter()
        .filter(|(_, v)| v.len() >= MIN_OWN_HISTORY_SAMPLES)
        .map(|(p, v)| (p, median(&v)))
        .collect();
    let global_med = if all_hours.len() >= MIN_OWN_HISTORY_SAMPLES {
        Some(median(&all_hours))
    } else {
        None
    };
    (plat_medians, global_med)
}

pub fn load_ra_progress_map(data_dir: Option<&Path>) -> HashMap<String, Value> {
    let mut result = HashMap::new();
    let Some(dir) = data_dir else {
        return result;
    };
    let ra_dir = dir.join("cache").join("retroachievements");
    if !ra_dir.is_dir() {
        return result;
    }
    let Ok(rd) = std::fs::read_dir(&ra_dir) else {
        return result;
    };
    for ent in rd.flatten() {
        let path = ent.path();
        let Some(fname) = path.file_name().and_then(|s| s.to_str()) else {
            continue;
        };
        if !path.is_file()
            || !fname.ends_with(".json")
            || fname == "systems.json"
            || fname.starts_with("system-")
        {
            continue;
        }
        let Some(stem) = path.file_stem().and_then(|s| s.to_str()) else {
            continue;
        };
        if let Ok(raw) = std::fs::read_to_string(&path)
            && let Ok(val) = serde_json::from_str::<Value>(&raw)
            && let Some(total) = val.get("total").and_then(Value::as_i64)
            && total > 0
        {
            let earned = val.get("earned").and_then(Value::as_i64).unwrap_or(0);
            let pct = ((earned as f64 * 100.0 / total as f64) * 10.0).round() / 10.0;
            result.insert(
                stem.to_owned(),
                json!({
                    "earned": earned,
                    "total": total,
                    "progress_pct": pct,
                    "mastered": val.get("mastered").and_then(Value::as_bool).unwrap_or(false),
                }),
            );
        }
    }
    result
}

fn igdb_cache_path(data_dir: &Path, game_id: &str) -> PathBuf {
    data_dir
        .join("cache")
        .join("igdb")
        .join(format!("{game_id}.json"))
}

pub fn read_igdb_cached_hours(
    data_dir: Option<&Path>,
    game_id: &str,
    now: Option<Timestamp>,
) -> (bool, Option<f64>) {
    let Some(dir) = data_dir else {
        return (false, None);
    };
    if game_id.is_empty() {
        return (false, None);
    }
    let path = igdb_cache_path(dir, game_id);
    let Ok(meta) = std::fs::metadata(&path) else {
        return (false, None);
    };
    if !meta.is_file() {
        return (false, None);
    }
    let Ok(mtime) = meta
        .modified()
        .and_then(|st| Timestamp::try_from(st).map_err(|e| std::io::Error::other(e.to_string())))
    else {
        return (false, None);
    };
    let age = utc_now(now).as_second() - mtime.as_second();
    if age >= IGDB_TTL_SECONDS {
        return (false, None);
    }
    let Ok(raw) = std::fs::read_to_string(&path) else {
        return (false, None);
    };
    let Ok(val) = serde_json::from_str::<Value>(&raw) else {
        return (false, None);
    };
    if !val.is_object() {
        return (false, None);
    }
    let hours = val
        .get("time_to_beat_hours")
        .and_then(|v| if v.is_boolean() { None } else { v.as_f64() })
        .filter(|&h| h > 0.0);
    (true, hours)
}

fn write_igdb_cache(data_dir: &Path, game_id: &str, hours: Option<f64>, now: Option<Timestamp>) {
    let path = igdb_cache_path(data_dir, game_id);
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let payload = json!({
        "game_id": game_id,
        "time_to_beat_hours": hours.filter(|&h| h > 0.0),
        "cached_at": utc_iso_z(utc_now(now)),
    });
    if let Ok(text) = serde_json::to_string(&payload) {
        let _ = atomic_write_text(&path, &text, 0o600);
    }
}

pub fn missing_igdb_games(data_dir: &Path, games: &[Value], now: Option<Timestamp>) -> Vec<Value> {
    let mut need = Vec::new();
    for g in games {
        let stored = g
            .get("time_to_beat_hours")
            .and_then(|v| if v.is_boolean() { None } else { v.as_f64() });
        if stored.is_some_and(|h| h > 0.0) {
            continue;
        }
        let gid = g
            .get("game_id")
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or("");
        let (hit, _) = read_igdb_cached_hours(Some(data_dir), gid, now);
        if !hit {
            need.push(g.clone());
        }
    }
    need
}

pub fn fetch_missing_igdb_estimates(
    data_dir: &Path,
    games: &[Value],
    cancel_flag: Option<&AtomicBool>,
    mut progress_cb: Option<&mut dyn FnMut(usize, usize)>,
) -> Value {
    if !igdb::is_configured() {
        return json!({"fetched": 0, "skipped_no_creds": true, "errors": []});
    }
    let targets = missing_igdb_games(data_dir, games, None);
    if targets.is_empty() {
        return json!({"fetched": 0, "skipped_no_creds": false, "errors": []});
    }
    let total = targets.len();
    let mut fetched = 0usize;
    let mut errors: Vec<String> = Vec::new();
    for (idx, game) in targets.iter().enumerate() {
        if cancel_flag.is_some_and(|f| f.load(Ordering::Acquire)) {
            break;
        }
        let gid = game.get("game_id").and_then(Value::as_str).unwrap_or("");
        let mut igdb_id = game.get("igdb_id").and_then(Value::as_i64);
        if igdb_id.is_none() {
            let name = game.get("name").and_then(Value::as_str).unwrap_or("");
            let plat = game.get("platform").and_then(Value::as_str).unwrap_or("");
            match igdb::search_games(name, plat, 1) {
                Ok(hits) => {
                    igdb_id = hits
                        .first()
                        .and_then(|h| h.get("igdb_id").or_else(|| h.get("id")))
                        .and_then(Value::as_i64);
                }
                Err(e) => {
                    if errors.len() < 20 {
                        errors.push(e.chars().take(160).collect());
                    }
                    if let Some(cb) = progress_cb.as_deref_mut() {
                        cb(idx + 1, total);
                    }
                    continue;
                }
            }
        }
        let hours = igdb_id.and_then(igdb::fetch_time_to_beat);
        write_igdb_cache(data_dir, gid, hours, None);
        if hours.is_some_and(|h| h > 0.0) {
            fetched += 1;
        }
        if let Some(cb) = progress_cb.as_deref_mut() {
            cb(idx + 1, total);
        }
    }
    json!({"fetched": fetched, "skipped_no_creds": false, "errors": errors})
}

pub fn estimate_game_hours(
    game: &Value,
    data_dir: Option<&Path>,
    platform_medians: Option<&HashMap<String, f64>>,
    global_median: Option<f64>,
    ra_info: Option<&Value>,
    now: Option<Timestamp>,
) -> Value {
    let gid = game
        .get("game_id")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("");
    let plat_key = game
        .get("platform")
        .and_then(Value::as_str)
        .map(|s| s.trim().to_lowercase())
        .unwrap_or_default();

    let stored_ttb = game
        .get("time_to_beat_hours")
        .and_then(|v| if v.is_boolean() { None } else { v.as_f64() })
        .filter(|&h| h > 0.0);

    let (base_hours, est_source, confidence) = if let Some(h) = stored_ttb {
        (h, "igdb", "high")
    } else {
        let (hit, cached) = read_igdb_cached_hours(data_dir, gid, now);
        if hit && let Some(h) = cached {
            (h, "igdb", "high")
        } else if let Some(&pm) = platform_medians.and_then(|m| m.get(&plat_key)) {
            (pm, "own_history", "medium")
        } else if let Some(gm) = global_median {
            (gm, "own_history", "medium")
        } else {
            (DEFAULT_EST_HOURS, "default", "low")
        }
    };

    let mut est = ((base_hours * 10.0).round() / 10.0).max(MIN_EST_HOURS);
    let mut refined_by_ra = false;
    let mut ra_pct = 0.0f64;
    if let Some(ra) = ra_info
        && let Some(total) = ra.get("total").and_then(Value::as_i64)
        && total > 0
    {
        let earned = ra.get("earned").and_then(Value::as_i64).unwrap_or(0);
        ra_pct = ((earned as f64 * 100.0 / total as f64) * 10.0).round() / 10.0;
        if earned > 0 && earned < total {
            est = ((base_hours * (1.0 - earned as f64 / total as f64) * 10.0).round() / 10.0)
                .max(MIN_EST_HOURS);
            refined_by_ra = true;
        }
    }

    let name = game
        .get("name")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .unwrap_or(gid);
    json!({
        "game_id": gid,
        "name": name,
        "platform": game.get("platform").and_then(Value::as_str).unwrap_or(""),
        "est_hours": est,
        "base_est_hours": (base_hours * 10.0).round() / 10.0,
        "est_source": est_source,
        "confidence": confidence,
        "refined_by_ra": refined_by_ra,
        "ra_pct": ra_pct,
        "playtime_seconds": total_playtime_seconds(game).max(0),
    })
}

fn order_candidates(
    mut cands: Vec<Value>,
    strategy: &str,
) -> Result<(Vec<Value>, Option<&'static str>), ApiError> {
    if !VALID_STRATEGIES.contains(&strategy) {
        return Err(ApiError::itinerary_invalid_strategy(format!(
            "Unsupported strategy '{strategy}'. Choose one of: {}.",
            VALID_STRATEGIES.join(", ")
        )));
    }
    let f = |v: &Value, k: &str| v.get(k).and_then(Value::as_f64).unwrap_or(0.0);
    let i = |v: &Value, k: &str| v.get(k).and_then(Value::as_i64).unwrap_or(0);
    let n = |v: &Value| {
        v.get("name")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_lowercase()
    };

    if strategy == "comeback" {
        cands.sort_by(|a, b| {
            i(b, "playtime_seconds")
                .cmp(&i(a, "playtime_seconds"))
                .then_with(|| {
                    f(a, "est_hours")
                        .partial_cmp(&f(b, "est_hours"))
                        .unwrap_or(std::cmp::Ordering::Equal)
                })
                .then_with(|| n(a).cmp(&n(b)))
        });
        return Ok((cands, None));
    }
    if strategy == "raided" {
        let has_ra = cands.iter().any(|c| {
            f(c, "ra_pct") > 0.0 || c.get("_has_ra").and_then(Value::as_bool).unwrap_or(false)
        });
        if has_ra {
            cands.sort_by(|a, b| {
                f(b, "ra_pct")
                    .partial_cmp(&f(a, "ra_pct"))
                    .unwrap_or(std::cmp::Ordering::Equal)
                    .then_with(|| {
                        f(a, "est_hours")
                            .partial_cmp(&f(b, "est_hours"))
                            .unwrap_or(std::cmp::Ordering::Equal)
                    })
                    .then_with(|| n(a).cmp(&n(b)))
            });
            return Ok((cands, None));
        }
        cands.sort_by(|a, b| {
            f(a, "est_hours")
                .partial_cmp(&f(b, "est_hours"))
                .unwrap_or(std::cmp::Ordering::Equal)
                .then_with(|| i(b, "playtime_seconds").cmp(&i(a, "playtime_seconds")))
                .then_with(|| n(a).cmp(&n(b)))
        });
        return Ok((cands, Some("quick-wins")));
    }
    cands.sort_by(|a, b| {
        f(a, "est_hours")
            .partial_cmp(&f(b, "est_hours"))
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| i(b, "playtime_seconds").cmp(&i(a, "playtime_seconds")))
            .then_with(|| n(a).cmp(&n(b)))
    });
    Ok((cands, None))
}

#[allow(clippy::too_many_arguments)]
pub fn build_itinerary(
    state: &Value,
    horizon_days: i64,
    minutes_per_day: i64,
    strategy: &str,
    platforms: Option<&[String]>,
    data_dir: Option<&Path>,
    pinned_done: Option<&[Value]>,
    now: Option<Timestamp>,
) -> Result<Value, ApiError> {
    if !VALID_STRATEGIES.contains(&strategy) {
        return Err(ApiError::itinerary_invalid_strategy(format!(
            "Unsupported strategy '{strategy}'."
        )));
    }
    let h_days = horizon_days.clamp(HORIZON_DAYS_MIN, HORIZON_DAYS_MAX);
    let m_day = minutes_per_day.clamp(MINUTES_PER_DAY_MIN, MINUTES_PER_DAY_MAX);
    let capacity_hours = ((h_days * m_day) as f64 / 60.0 * 100.0).round() / 100.0;
    let weekly_hours = ((m_day * 7) as f64 / 60.0).max(0.01);
    let max_weeks = ((h_days as f64 / 7.0).ceil() as i64).max(1);

    let all_games: Vec<Value> = state
        .get("games")
        .and_then(Value::as_array)
        .map(|a| a.iter().filter(|g| g.is_object()).cloned().collect())
        .unwrap_or_default();
    let trashed = trashed_ids(state);
    let (plat_medians, global_med) = build_own_history_medians(&all_games);
    let ra_map = load_ra_progress_map(data_dir);

    let pinned_list: Vec<Value> = pinned_done.map(<[Value]>::to_vec).unwrap_or_default();
    let pinned_ids: HashSet<String> = pinned_list
        .iter()
        .filter_map(|e| e.get("game_id").and_then(Value::as_str).map(str::to_owned))
        .collect();

    let mut eligible_cands: Vec<Value> = Vec::new();
    for g in &all_games {
        let gid = g
            .get("game_id")
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or("");
        if pinned_ids.contains(gid) {
            continue;
        }
        if !is_eligible_for_itinerary(g, state, platforms, Some(&trashed)) {
            continue;
        }
        let mut est = estimate_game_hours(
            g,
            data_dir,
            Some(&plat_medians),
            global_med,
            ra_map.get(gid),
            now,
        );
        if let Some(m) = est.as_object_mut() {
            m.insert("_has_ra".into(), Value::Bool(ra_map.contains_key(gid)));
        }
        eligible_cands.push(est);
    }

    if eligible_cands.is_empty() && pinned_list.is_empty() {
        return Err(ApiError::itinerary_no_games(
            "No eligible unfinished games match your Backlog Itinerary filters.",
        ));
    }

    let (ordered, fallback) = order_candidates(eligible_cands, strategy)?;
    let mut entries: Vec<Value> = Vec::new();
    let mut later: Vec<String> = Vec::new();
    let mut later_hours = 0.0f64;
    let mut cum = 0.0f64;
    let mut bd_igdb = 0usize;
    let mut bd_own = 0usize;
    let mut bd_def = 0usize;

    let mut bump_src = |src: &str| match src {
        "igdb" => bd_igdb += 1,
        "own_history" => bd_own += 1,
        "default" => bd_def += 1,
        _ => {}
    };

    for p_entry in pinned_list {
        let est_h = p_entry
            .get("est_hours")
            .and_then(Value::as_f64)
            .unwrap_or(MIN_EST_HOURS);
        let week = (((cum / weekly_hours).floor() as i64) + 1).min(max_weeks);
        let mut clean = Map::new();
        if let Some(m) = p_entry.as_object() {
            for (k, v) in m {
                if !k.starts_with('_') && k != "live_status" {
                    clean.insert(k.clone(), v.clone());
                }
            }
        }
        clean.insert("week".into(), json!(week));
        let src = clean
            .get("est_source")
            .and_then(Value::as_str)
            .unwrap_or("default")
            .to_owned();
        entries.push(Value::Object(clean));
        cum = ((cum + est_h) * 100.0).round() / 100.0;
        bump_src(&src);
    }

    for mut cand in ordered {
        if let Some(m) = cand.as_object_mut() {
            m.remove("_has_ra");
        }
        let est_h = cand
            .get("est_hours")
            .and_then(Value::as_f64)
            .unwrap_or(MIN_EST_HOURS);
        if cum + est_h <= capacity_hours + 1e-9 {
            let week = (((cum / weekly_hours).floor() as i64) + 1).min(max_weeks);
            let mut row = cand.as_object().cloned().unwrap_or_default();
            row.remove("playtime_seconds");
            row.insert("week".into(), json!(week));
            let src = row
                .get("est_source")
                .and_then(Value::as_str)
                .unwrap_or("default")
                .to_owned();
            entries.push(Value::Object(row));
            cum = ((cum + est_h) * 100.0).round() / 100.0;
            bump_src(&src);
        } else {
            if let Some(gid) = cand.get("game_id").and_then(Value::as_str) {
                later.push(gid.to_owned());
            }
            later_hours = ((later_hours + est_h) * 10.0).round() / 10.0;
        }
    }

    let clean_plats: Vec<String> = platforms
        .map(|ps| {
            ps.iter()
                .map(|p| p.trim().to_owned())
                .filter(|p| !p.is_empty())
                .collect()
        })
        .unwrap_or_default();
    let plan_id = format!("itin-{}", &Uuid::new_v4().simple().to_string()[..12]);
    Ok(json!({
        "id": plan_id,
        "created_at": utc_iso_z(utc_now(now)),
        "horizon_days": h_days,
        "minutes_per_day": m_day,
        "strategy": strategy,
        "strategy_used": fallback.unwrap_or(strategy),
        "strategy_fallback": fallback,
        "platforms": clean_plats,
        "capacity_hours": capacity_hours,
        "weekly_hours": (weekly_hours * 100.0).round() / 100.0,
        "planned_hours": cum,
        "entries": entries,
        "later": later,
        "later_hours": later_hours,
        "estimate_breakdown": {
            "igdb": bd_igdb,
            "own_history": bd_own,
            "default": bd_def,
        },
    }))
}

pub fn project_live_itinerary(state: &Value, now: Option<Timestamp>) -> Value {
    let Some(plan_obj) = state.get("itinerary").and_then(Value::as_object) else {
        return json!({"plan": Value::Null});
    };
    let trashed = trashed_ids(state);
    let game_by_id: HashMap<&str, &Value> = state
        .get("games")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .filter_map(|g| {
                    let gid = g.get("game_id").and_then(Value::as_str)?;
                    if gid.is_empty() { None } else { Some((gid, g)) }
                })
                .collect()
        })
        .unwrap_or_default();

    let now_ts = utc_now(now);
    let now_date = jiff::tz::TimeZone::UTC.to_datetime(now_ts).date();
    let created_date = plan_obj
        .get("created_at")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .and_then(|s| {
            if let Ok(ts) = s.parse::<Timestamp>() {
                Some(jiff::tz::TimeZone::UTC.to_datetime(ts).date())
            } else if let Ok(dt) = s.replace(' ', "T").parse::<DateTime>() {
                Some(dt.date())
            } else if s.len() >= 10 {
                s[..10].parse::<Date>().ok()
            } else {
                None
            }
        })
        .unwrap_or(now_date);

    let m_day = clamp_minutes_per_day(plan_obj.get("minutes_per_day"), 60);
    let elapsed_days = (created_date
        .until(now_date)
        .map(|s| s.get_days())
        .unwrap_or(0)
        + 1)
    .max(1);
    let elapsed_capacity =
        ((i64::from(elapsed_days) * m_day) as f64 / 60.0 * 100.0).round() / 100.0;

    let mut cum = 0.0f64;
    let mut projected_entries: Vec<Value> = Vec::new();
    let mut done_count = 0usize;
    let mut done_hours = 0.0f64;
    let mut stale_count = 0usize;

    if let Some(entries) = plan_obj.get("entries").and_then(Value::as_array) {
        for entry in entries {
            let Some(em) = entry.as_object() else {
                continue;
            };
            let gid = em.get("game_id").and_then(Value::as_str).unwrap_or("");
            let est_h = em
                .get("est_hours")
                .and_then(Value::as_f64)
                .unwrap_or(MIN_EST_HOURS);
            cum = ((cum + est_h) * 100.0).round() / 100.0;
            let live_status = match game_by_id.get(gid) {
                None => {
                    stale_count += 1;
                    "stale"
                }
                Some(&game)
                    if trashed.contains(gid)
                        || game.get("hidden").and_then(Value::as_bool).unwrap_or(false) =>
                {
                    stale_count += 1;
                    "stale"
                }
                Some(&game) => {
                    let played_h = total_playtime_seconds(game) as f64 / 3600.0;
                    let abandoned = norm(game.get("progress")).contains("abandoned");
                    if is_completed(game) || abandoned || played_h >= est_h * DONE_PLAYTIME_RATIO {
                        done_count += 1;
                        done_hours = ((done_hours + est_h) * 10.0).round() / 10.0;
                        "done"
                    } else if cum <= elapsed_capacity {
                        "behind"
                    } else {
                        "on_track"
                    }
                }
            };
            let mut row = em.clone();
            row.insert("live_status".into(), Value::String(live_status.to_owned()));
            projected_entries.push(Value::Object(row));
        }
    }

    let planned_hours = plan_obj
        .get("planned_hours")
        .and_then(Value::as_f64)
        .filter(|&h| h > 0.0)
        .unwrap_or(cum);
    let progress_pct = if planned_hours > 0.0 {
        ((done_hours * 100.0 / planned_hours) * 10.0).round() / 10.0
    } else {
        0.0
    };
    let remaining_hours = (planned_hours - done_hours).max(0.0);
    let weekly_h = plan_obj
        .get("weekly_hours")
        .and_then(Value::as_f64)
        .filter(|&h| h > 0.0)
        .unwrap_or((m_day * 7) as f64 / 60.0)
        .max(0.01);
    let projected_finish_week = if remaining_hours > 0.0 {
        (remaining_hours / weekly_h).ceil() as i64
    } else {
        0
    };

    let mut out = plan_obj.clone();
    out.insert("entries".into(), Value::Array(projected_entries));
    out.insert("done_count".into(), json!(done_count));
    out.insert("done_hours".into(), json!(done_hours));
    out.insert("stale_count".into(), json!(stale_count));
    out.insert("progress_pct".into(), json!(progress_pct));
    out.insert("elapsed_capacity_hours".into(), json!(elapsed_capacity));
    out.insert("projected_finish_week".into(), json!(projected_finish_week));
    json!({"plan": Value::Object(out)})
}

pub fn replan_itinerary(
    state: &mut Value,
    payload: &Map<String, Value>,
    data_dir: Option<&Path>,
    now: Option<Timestamp>,
) -> Result<Value, ApiError> {
    let existing = state.get("itinerary").cloned().unwrap_or(Value::Null);
    let horizon_days = clamp_horizon_days(
        payload.get("horizon_days"),
        clamp_horizon_days(existing.get("horizon_days"), 30),
    );
    let minutes_per_day = clamp_minutes_per_day(
        payload.get("minutes_per_day"),
        clamp_minutes_per_day(existing.get("minutes_per_day"), 60),
    );
    let strategy = payload
        .get("strategy")
        .and_then(Value::as_str)
        .or_else(|| existing.get("strategy").and_then(Value::as_str))
        .unwrap_or("quick-wins")
        .trim()
        .to_owned();
    let platforms_val = if payload.contains_key("platforms") {
        payload.get("platforms")
    } else {
        existing.get("platforms")
    };
    let platforms: Option<Vec<String>> = match platforms_val {
        None | Some(Value::Null) => None,
        Some(Value::Array(arr)) => Some(
            arr.iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect(),
        ),
        _ => {
            return Err(ApiError::bad_request(
                "platforms must be a list of platform names.",
            ));
        }
    };

    let live = project_live_itinerary(state, now);
    let mut pinned_done: Vec<Value> = Vec::new();
    let mut dropped_ids: Vec<String> = Vec::new();
    if let Some(entries) = live
        .get("plan")
        .and_then(|p| p.get("entries"))
        .and_then(Value::as_array)
    {
        for entry in entries {
            match entry.get("live_status").and_then(Value::as_str) {
                Some("done") => pinned_done.push(entry.clone()),
                Some("stale") => {
                    if let Some(gid) = entry.get("game_id").and_then(Value::as_str) {
                        dropped_ids.push(gid.to_owned());
                    }
                }
                _ => {}
            }
        }
    }

    let plan = build_itinerary(
        state,
        horizon_days,
        minutes_per_day,
        &strategy,
        platforms.as_deref(),
        data_dir,
        Some(&pinned_done),
        now,
    )?;
    if let Some(root) = state.as_object_mut() {
        root.insert("itinerary".into(), plan);
    }
    let projected = project_live_itinerary(state, now);
    Ok(json!({
        "plan": projected["plan"],
        "dropped_ids": dropped_ids,
    }))
}
