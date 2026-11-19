//! Achievement Trail — offline RetroAchievements analytics & derived cache
//! (`pkg/parity/parity_trail.py`, ADR 0110).

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, UNIX_EPOCH};

use jiff::civil::{Date, DateTime};
use jiff::tz::TimeZone;
use jiff::{Timestamp, ToSpan};
use openbox_core::ApiError;
use openbox_io::atomic_write_text;
use serde_json::{Map, Value, json};

use crate::http::py_round;
use crate::retroachievements::{RaCredentials, cached, game_progress, match_game};

pub const TRAIL_CACHE_NAME: &str = "trail.json";
pub const TRAIL_CACHE_FORMAT: i64 = 1;
pub const INLINE_REBUILD_THRESHOLD: usize = 250;
pub const MAX_TRAIL_ITEMS: usize = 50;
pub const DEFAULT_SPACING_MILLIS: u64 = 500;

fn valid_sort(sort: &str) -> &'static str {
    match sort {
        "active" => "active",
        "longest" => "longest",
        "points" => "points",
        _ => "closest",
    }
}

/// Parse RA `YYYY-MM-DD HH:MM:SS` or ISO-8601 timestamps into UTC `Timestamp`.
pub fn parse_ra_timestamp(raw: &str) -> Option<Timestamp> {
    let text = raw.trim();
    if text.is_empty() {
        return None;
    }
    if text.len() == 19 {
        let sep = text.as_bytes()[10];
        if sep == b' ' || sep == b'T' {
            let iso = format!("{}T{}", &text[..10], &text[11..]);
            if let Ok(dt) = iso.parse::<DateTime>()
                && let Ok(ts) = TimeZone::UTC.to_timestamp(dt)
            {
                return Some(ts);
            }
        }
    }
    if let Ok(ts) = text.parse::<Timestamp>() {
        return Some(ts);
    }
    if let Ok(dt) = text.parse::<DateTime>()
        && let Ok(ts) = TimeZone::UTC.to_timestamp(dt)
    {
        return Some(ts);
    }
    None
}

fn trashed_ids(state: &Value) -> HashSet<String> {
    let mut out = HashSet::new();
    if let Some(arr) = state.get("trash").and_then(Value::as_array) {
        for entry in arr {
            if let Some(s) = entry.as_str().map(str::trim).filter(|s| !s.is_empty()) {
                out.insert(s.to_owned());
            } else if let Some(m) = entry.as_object() {
                let gid = m
                    .get("game_id")
                    .and_then(Value::as_str)
                    .map(str::trim)
                    .filter(|s| !s.is_empty())
                    .or_else(|| {
                        m.get("game")
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

fn iso_week_of(ts: Timestamp) -> (i16, i8) {
    let d = TimeZone::UTC.to_datetime(ts).date();
    let iw = d.iso_week_date();
    (iw.year(), iw.week())
}

fn week_ordinal(year: i16, week: i8) -> i64 {
    let epoch = Date::constant(1970, 1, 1);
    let Ok(monday) = jiff::civil::ISOWeekDate::new(year, week, jiff::civil::Weekday::Monday) else {
        return 0;
    };
    let d = monday.date();
    let days = (d - epoch).get_days() as i64;
    days.div_euclid(7)
}

fn trailing_12_weeks(now: Timestamp) -> Vec<(i16, i8, String)> {
    let d = TimeZone::UTC.to_datetime(now).date();
    let wd_offset = d.weekday().to_monday_zero_offset() as i64;
    let monday = d.checked_sub(wd_offset.days()).unwrap_or(d);
    let mut weeks = Vec::with_capacity(12);
    for offset in (0..=11).rev() {
        let wk_day = monday.checked_sub((offset * 7).days()).unwrap_or(monday);
        let iw = wk_day.iso_week_date();
        weeks.push((
            iw.year(),
            iw.week(),
            format!("{}-W{:02}", iw.year(), iw.week()),
        ));
    }
    weeks
}

fn compute_week_streak(unlock_dts: &[Timestamp], now: Timestamp) -> usize {
    if unlock_dts.is_empty() {
        return 0;
    }
    let ords: HashSet<i64> = unlock_dts
        .iter()
        .map(|&ts| {
            let (y, w) = iso_week_of(ts);
            week_ordinal(y, w)
        })
        .collect();
    let (cy, cw) = iso_week_of(now);
    let cur_ord = week_ordinal(cy, cw);
    let mut anchor = if ords.contains(&cur_ord) {
        cur_ord
    } else if ords.contains(&(cur_ord - 1)) {
        cur_ord - 1
    } else {
        return 0;
    };
    let mut streak = 0usize;
    while ords.contains(&anchor) {
        streak += 1;
        anchor -= 1;
    }
    streak
}

fn velocity_and_eta(
    unlock_events: &[(Timestamp, i64)],
    earned: i64,
    total: i64,
    has_dates: bool,
    now: Timestamp,
) -> Value {
    let remaining = (total - earned).max(0);
    let weeks_12 = trailing_12_weeks(now);
    let mut buckets: HashMap<(i16, i8), (i64, i64)> = weeks_12
        .iter()
        .map(|(y, w, _)| ((*y, *w), (0, 0)))
        .collect();
    if has_dates {
        for &(ts, pts) in unlock_events {
            let key = iso_week_of(ts);
            if let Some(b) = buckets.get_mut(&key) {
                b.0 += 1;
                b.1 += pts.max(0);
            }
        }
    }
    let trail_12: Vec<Value> = weeks_12
        .iter()
        .map(|(y, w, label)| {
            let (c, p) = buckets[&(*y, *w)];
            json!({"week": label, "count": c, "points": p})
        })
        .collect();
    let last_8: Vec<Value> = if has_dates {
        trail_12[trail_12.len().saturating_sub(8)..].to_vec()
    } else {
        Vec::new()
    };
    let active_weeks = if has_dates {
        trail_12
            .iter()
            .filter(|r| r["count"].as_i64().unwrap_or(0) > 0)
            .count()
    } else {
        0
    };

    if total > 0 && remaining == 0 {
        return json!({
            "weekly_velocity": last_8,
            "active_weeks": active_weeks,
            "eta_days": 0,
            "eta_confidence": "complete",
            "eta_reason": "complete",
        });
    }
    if !has_dates {
        return json!({
            "weekly_velocity": [],
            "active_weeks": 0,
            "eta_days": Value::Null,
            "eta_confidence": "low",
            "eta_reason": "no_dates",
        });
    }
    let nonzero_8: Vec<i64> = trail_12[trail_12.len().saturating_sub(8)..]
        .iter()
        .filter_map(|r| r["count"].as_i64())
        .filter(|&c| c > 0)
        .collect();
    let nonzero_12: Vec<i64> = trail_12
        .iter()
        .filter_map(|r| r["count"].as_i64())
        .filter(|&c| c > 0)
        .collect();
    let sample = if nonzero_8.len() >= 4 {
        &nonzero_8
    } else {
        &nonzero_12
    };
    let weekly_rate = if sample.is_empty() {
        0.0
    } else {
        sample.iter().sum::<i64>() as f64 / sample.len() as f64
    };
    let confidence = if active_weeks >= 8 {
        "high"
    } else if active_weeks >= 4 {
        "medium"
    } else {
        "low"
    };
    let (eta_days, reason) =
        if matches!(confidence, "high" | "medium") && weekly_rate > 0.0 && remaining > 0 {
            let d = (((remaining as f64) / weekly_rate) * 7.0).ceil() as i64;
            (json!(d.min(3650)), "forecast")
        } else {
            (
                Value::Null,
                if weekly_rate <= 0.0 {
                    "zero_rate"
                } else {
                    "low_activity"
                },
            )
        };
    json!({
        "weekly_velocity": last_8,
        "active_weeks": active_weeks,
        "eta_days": eta_days,
        "eta_confidence": confidence,
        "eta_reason": reason,
    })
}

fn id_sort_key(val: Option<&Value>) -> (u8, i64, String) {
    let text = match val {
        Some(Value::String(s)) => s.trim().to_owned(),
        Some(Value::Number(n)) => n.to_string(),
        _ => String::new(),
    };
    if !text.is_empty() && text.bytes().all(|b| b.is_ascii_digit()) {
        (0, text.parse::<i64>().unwrap_or(0), String::new())
    } else {
        (1, 0, text.to_lowercase())
    }
}

pub fn compute_game_trail(game: &Value, ra_cache: &Value, now: Option<Timestamp>) -> Value {
    let now_ts = now.unwrap_or_else(Timestamp::now);
    let achievements: Vec<&Map<String, Value>> = ra_cache
        .get("achievements")
        .and_then(Value::as_array)
        .map(|a| a.iter().filter_map(Value::as_object).collect())
        .unwrap_or_default();
    let earned_count = ra_cache
        .get("earned")
        .and_then(Value::as_i64)
        .filter(|&n| n != 0)
        .unwrap_or_else(|| {
            achievements
                .iter()
                .filter(|a| a.get("earned").and_then(Value::as_bool).unwrap_or(false))
                .count() as i64
        });
    let earned_hc = ra_cache
        .get("earned_hardcore")
        .and_then(Value::as_i64)
        .filter(|&n| n != 0)
        .unwrap_or_else(|| {
            achievements
                .iter()
                .filter(|a| a.get("hardcore").and_then(Value::as_bool).unwrap_or(false))
                .count() as i64
        });
    let total_count = ra_cache
        .get("total")
        .and_then(Value::as_i64)
        .filter(|&n| n != 0)
        .unwrap_or(achievements.len() as i64);
    let mastered = ra_cache
        .get("mastered")
        .and_then(Value::as_bool)
        .unwrap_or(false)
        || (total_count > 0 && earned_hc >= total_count);
    let remaining = (total_count - earned_count).max(0);

    let mut unlock_events: Vec<(Timestamp, i64)> = Vec::new();
    let mut earned_points = 0i64;
    let mut total_points = 0i64;
    let mut unearned: Vec<Value> = Vec::new();

    for ach in &achievements {
        let pts = ach
            .get("points")
            .and_then(Value::as_i64)
            .unwrap_or(0)
            .max(0);
        total_points += pts;
        if ach.get("earned").and_then(Value::as_bool).unwrap_or(false) {
            earned_points += pts;
            let dt = ach
                .get("date_earned_hardcore")
                .and_then(Value::as_str)
                .and_then(parse_ra_timestamp)
                .or_else(|| {
                    ach.get("date_earned")
                        .and_then(Value::as_str)
                        .and_then(parse_ra_timestamp)
                });
            if let Some(ts) = dt {
                unlock_events.push((ts, pts));
            }
        } else {
            unearned.push(json!({
                "id": ach.get("id").cloned().unwrap_or(Value::Null),
                "title": ach.get("title").and_then(Value::as_str).unwrap_or(""),
                "description": ach.get("description").and_then(Value::as_str).unwrap_or(""),
                "points": pts,
                "badge": ach.get("badge").and_then(Value::as_str).unwrap_or(""),
            }));
        }
    }

    let has_dates = earned_count == 0 || !unlock_events.is_empty();
    let vel = velocity_and_eta(&unlock_events, earned_count, total_count, has_dates, now_ts);
    let dts: Vec<Timestamp> = unlock_events.iter().map(|(dt, _)| *dt).collect();
    let streak_weeks = if has_dates {
        compute_week_streak(&dts, now_ts)
    } else {
        0
    };
    let last_unlock = dts
        .iter()
        .max()
        .map(|ts| ts.strftime("%Y-%m-%dT%H:%M:%SZ").to_string())
        .unwrap_or_default();

    unearned.sort_by(|a, b| {
        let ap = a["points"].as_i64().unwrap_or(0);
        let bp = b["points"].as_i64().unwrap_or(0);
        let at = a["title"].as_str().unwrap_or("").to_lowercase();
        let bt = b["title"].as_str().unwrap_or("").to_lowercase();
        (ap <= 0, ap, id_sort_key(a.get("id")), at).cmp(&(
            bp <= 0,
            bp,
            id_sort_key(b.get("id")),
            bt,
        ))
    });
    unearned.truncate(10);

    let next_target_pts = ((earned_points / 100) + 1) * 100;
    let progress_pct = if total_count > 0 {
        py_round((earned_count as f64) * 100.0 / (total_count as f64), 1)
    } else {
        0.0
    };
    let ra_gid = game
        .get("ra_game_id")
        .or_else(|| ra_cache.get("game_id"))
        .map(|v| match v {
            Value::String(s) => s.clone(),
            Value::Null => String::new(),
            other => other.to_string(),
        })
        .unwrap_or_default();

    json!({
        "game_id": game.get("game_id").and_then(Value::as_str).unwrap_or(""),
        "name": game.get("name").and_then(Value::as_str).unwrap_or(""),
        "platform": game.get("platform").and_then(Value::as_str).unwrap_or(""),
        "ra_game_id": ra_gid,
        "earned": earned_count,
        "earned_hardcore": earned_hc,
        "total": total_count,
        "remaining": remaining,
        "progress_pct": progress_pct,
        "mastered": mastered,
        "earned_points": earned_points,
        "total_points": total_points,
        "has_dates": has_dates,
        "weekly_velocity": vel["weekly_velocity"],
        "active_weeks": vel["active_weeks"],
        "eta_days": vel["eta_days"],
        "eta_confidence": vel["eta_confidence"],
        "eta_reason": vel["eta_reason"],
        "streak_weeks": streak_weeks,
        "last_unlock_at": last_unlock,
        "next_up": unearned,
        "next_milestone": {
            "points": next_target_pts,
            "remaining": (next_target_pts - earned_points).max(0),
        },
    })
}

fn iter_ra_cache_files(data_dir: &Path) -> Vec<PathBuf> {
    let ra_dir = data_dir.join("cache").join("retroachievements");
    let Ok(rd) = fs::read_dir(&ra_dir) else {
        return Vec::new();
    };
    let mut files: Vec<PathBuf> = rd
        .flatten()
        .map(|e| e.path())
        .filter(|p| {
            if !p.is_file() || p.extension().and_then(|e| e.to_str()) != Some("json") {
                return false;
            }
            let name = p.file_name().and_then(|n| n.to_str()).unwrap_or("");
            name != "systems.json" && !name.starts_with("system-")
        })
        .collect();
    files.sort();
    files
}

pub fn ra_cache_signature(data_dir: &Path, state_sig: &str) -> Value {
    let ra_dir = data_dir.join("cache").join("retroachievements");
    let dir_mtime = fs::metadata(&ra_dir)
        .and_then(|m| m.modified())
        .ok()
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0);
    let files = iter_ra_cache_files(data_dir);
    let mut max_mtime = 0u64;
    for f in &files {
        if let Ok(mt) = fs::metadata(f)
            .and_then(|m| m.modified())
            .ok()
            .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
            .map(|d| d.as_nanos() as u64)
            .ok_or(())
            && mt > max_mtime
        {
            max_mtime = mt;
        }
    }
    json!([state_sig, dir_mtime, files.len(), max_mtime])
}

pub fn compute_trail_index(state: &Value, data_dir: &Path, now: Option<Timestamp>) -> Value {
    let now_ts = now.unwrap_or_else(Timestamp::now);
    let trashed = trashed_ids(state);
    let mut game_by_id: HashMap<String, &Value> = HashMap::new();
    if let Some(games) = state.get("games").and_then(Value::as_array) {
        for g in games {
            if !g.is_object() || g.get("hidden").and_then(Value::as_bool).unwrap_or(false) {
                continue;
            }
            let gid = g
                .get("game_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim();
            if !gid.is_empty() && !trashed.contains(gid) {
                game_by_id.insert(gid.to_owned(), g);
            }
        }
    }

    let mut items: BTreeMap<String, Value> = BTreeMap::new();
    let mut all_events: Vec<(Timestamp, i64)> = Vec::new();
    let mut total_earned = 0i64;
    let mut total_ach = 0i64;
    let mut total_pts = 0i64;
    let mut mastered_games = 0usize;
    let mut any_earned_missing_dates = false;
    let mut any_has_dates = false;

    for cache_path in iter_ra_cache_files(data_dir) {
        let Some(gid) = cache_path.file_stem().and_then(|s| s.to_str()) else {
            continue;
        };
        let Some(&game) = game_by_id.get(gid) else {
            continue;
        };
        let Ok(raw_text) = fs::read_to_string(&cache_path) else {
            continue;
        };
        let Ok(raw) = serde_json::from_str::<Value>(&raw_text) else {
            continue;
        };
        if !raw.is_object() {
            continue;
        }
        let entry = compute_game_trail(game, &raw, Some(now_ts));
        let earned = entry["earned"].as_i64().unwrap_or(0);
        let has_d = entry["has_dates"].as_bool().unwrap_or(false);
        total_earned += earned;
        total_ach += entry["total"].as_i64().unwrap_or(0);
        total_pts += entry["earned_points"].as_i64().unwrap_or(0);
        if entry["mastered"].as_bool().unwrap_or(false) {
            mastered_games += 1;
        }
        if earned > 0 && !has_d {
            any_earned_missing_dates = true;
        }
        if has_d && earned > 0 {
            any_has_dates = true;
        }
        if let Some(achs) = raw.get("achievements").and_then(Value::as_array) {
            for ach in achs {
                if ach.get("earned").and_then(Value::as_bool).unwrap_or(false)
                    && let Some(ts) = ach
                        .get("date_earned_hardcore")
                        .and_then(Value::as_str)
                        .and_then(parse_ra_timestamp)
                        .or_else(|| {
                            ach.get("date_earned")
                                .and_then(Value::as_str)
                                .and_then(parse_ra_timestamp)
                        })
                {
                    let pts = ach
                        .get("points")
                        .and_then(Value::as_i64)
                        .unwrap_or(0)
                        .max(0);
                    all_events.push((ts, pts));
                }
            }
        }
        items.insert(gid.to_owned(), entry);
    }

    let lib_has_dates = total_earned == 0 || any_has_dates;
    let vel = velocity_and_eta(
        &all_events,
        total_earned,
        total_ach,
        lib_has_dates && !(total_earned > 0 && !any_has_dates),
        now_ts,
    );
    let dts: Vec<Timestamp> = all_events.iter().map(|(dt, _)| *dt).collect();
    let streak_weeks = if lib_has_dates {
        compute_week_streak(&dts, now_ts)
    } else {
        0
    };
    let next_target_pts = ((total_pts / 100) + 1) * 100;

    json!({
        "format": TRAIL_CACHE_FORMAT,
        "summary": {
            "games_count": items.len(),
            "mastered_games": mastered_games,
            "earned_achievements": total_earned,
            "total_achievements": total_ach,
            "earned_points": total_pts,
            "has_dates": lib_has_dates && !(any_earned_missing_dates && !any_has_dates),
            "stale_dates_notice": any_earned_missing_dates,
            "weekly_velocity": vel["weekly_velocity"],
            "streak_weeks": streak_weeks,
            "next_milestone": {
                "points": next_target_pts,
                "remaining": (next_target_pts - total_pts).max(0),
            },
        },
        "games": items,
    })
}

fn trail_cache_path(data_dir: &Path) -> PathBuf {
    data_dir.join("cache").join(TRAIL_CACHE_NAME)
}

pub fn rebuild_trail_cache(state: &Value, data_dir: &Path, state_sig: &str) -> Value {
    let sig = ra_cache_signature(data_dir, state_sig);
    let mut index = compute_trail_index(state, data_dir, None);
    if let Some(m) = index.as_object_mut() {
        m.insert("signature".into(), sig);
    }
    let path = trail_cache_path(data_dir);
    if let Ok(text) = serde_json::to_string(&index) {
        let _ = atomic_write_text(&path, &text, 0o600);
    }
    index
}

fn read_trail_cache(path: &Path) -> Option<Value> {
    if !path.is_file() {
        return None;
    }
    let raw = fs::read_to_string(path).ok()?;
    let val: Value = serde_json::from_str(&raw).ok()?;
    if val.get("format").and_then(Value::as_i64) != Some(TRAIL_CACHE_FORMAT)
        || !val.get("games").is_some_and(Value::is_object)
    {
        let _ = fs::remove_file(path);
        return None;
    }
    Some(val)
}

fn sort_items(mut items: Vec<Value>, sort: &str) -> Vec<Value> {
    match valid_sort(sort) {
        "active" => {
            items.sort_by(|a, b| {
                let al = a["last_unlock_at"].as_str().unwrap_or("");
                let bl = b["last_unlock_at"].as_str().unwrap_or("");
                let ae = a["earned"].as_i64().unwrap_or(0);
                let be = b["earned"].as_i64().unwrap_or(0);
                let an = a["name"].as_str().unwrap_or("");
                let bn = b["name"].as_str().unwrap_or("");
                (bl, be, bn).cmp(&(al, ae, an))
            });
        }
        "longest" => {
            items.sort_by(|a, b| {
                let ar = a["remaining"].as_i64().unwrap_or(0);
                let br = b["remaining"].as_i64().unwrap_or(0);
                let ap = a["total_points"].as_i64().unwrap_or(0);
                let bp = b["total_points"].as_i64().unwrap_or(0);
                let an = a["name"].as_str().unwrap_or("").to_lowercase();
                let bn = b["name"].as_str().unwrap_or("").to_lowercase();
                (br, bp, an).cmp(&(ar, ap, bn))
            });
        }
        "points" => {
            items.sort_by(|a, b| {
                let ae = a["earned_points"].as_i64().unwrap_or(0);
                let be = b["earned_points"].as_i64().unwrap_or(0);
                let at = a["total_points"].as_i64().unwrap_or(0);
                let bt = b["total_points"].as_i64().unwrap_or(0);
                let an = a["name"].as_str().unwrap_or("").to_lowercase();
                let bn = b["name"].as_str().unwrap_or("").to_lowercase();
                (be, bt, an).cmp(&(ae, at, bn))
            });
        }
        _ => {
            items.sort_by(|a, b| {
                let ac = if a["remaining"].as_i64().unwrap_or(0) == 0 {
                    1u8
                } else {
                    0
                };
                let bc = if b["remaining"].as_i64().unwrap_or(0) == 0 {
                    1u8
                } else {
                    0
                };
                let ap = a["progress_pct"].as_f64().unwrap_or(0.0);
                let bp = b["progress_pct"].as_f64().unwrap_or(0.0);
                let ar = a["remaining"].as_i64().unwrap_or(0);
                let br = b["remaining"].as_i64().unwrap_or(0);
                let an = a["name"].as_str().unwrap_or("").to_lowercase();
                let bn = b["name"].as_str().unwrap_or("").to_lowercase();
                ac.cmp(&bc)
                    .then_with(|| bp.total_cmp(&ap))
                    .then_with(|| ar.cmp(&br))
                    .then_with(|| an.cmp(&bn))
            });
        }
    }
    items
}

pub fn get_trail<F>(
    state: &Value,
    data_dir: &Path,
    state_sig: &str,
    sort: &str,
    offset: isize,
    limit: isize,
    rebuild_cb: Option<F>,
) -> Value
where
    F: FnOnce(),
{
    let sig = ra_cache_signature(data_dir, state_sig);
    let file_count = sig[2].as_u64().unwrap_or(0) as usize;
    let cache_path = trail_cache_path(data_dir);
    let cached_opt = read_trail_cache(&cache_path);
    let mut stale = false;

    let data = match cached_opt {
        Some(c) if c.get("signature") == Some(&sig) => c,
        Some(c) if file_count > INLINE_REBUILD_THRESHOLD && rebuild_cb.is_some() => {
            stale = true;
            if let Some(cb) = rebuild_cb {
                cb();
            }
            c
        }
        _ => rebuild_trail_cache(state, data_dir, state_sig),
    };

    let rows: Vec<Value> = data
        .get("games")
        .and_then(Value::as_object)
        .map(|m| m.values().cloned().collect())
        .unwrap_or_default();
    let sorted = sort_items(rows, sort);
    let safe_offset = offset.max(0) as usize;
    let safe_limit = limit.clamp(1, 200) as usize;
    let total = sorted.len();
    let start = safe_offset.min(total);
    let end = (start + safe_limit).min(total);
    json!({
        "available": true,
        "summary": data.get("summary").cloned().unwrap_or_else(|| json!({})),
        "items": &sorted[start..end],
        "total": total,
        "offset": safe_offset,
        "limit": safe_limit,
        "sort": valid_sort(sort),
        "stale": stale,
    })
}

pub fn get_trail_game(state: &Value, data_dir: &Path, game_id: &str) -> Result<Value, ApiError> {
    let gid = game_id.trim();
    let trashed = trashed_ids(state);
    let game = state
        .get("games")
        .and_then(Value::as_array)
        .and_then(|arr| {
            arr.iter().find(|g| {
                g.get("game_id").and_then(Value::as_str) == Some(gid) && !trashed.contains(gid)
            })
        })
        .ok_or_else(|| ApiError::trail_game_unknown(format!("Game not found in library: {gid}")))?;
    let cache_path = data_dir
        .join("cache")
        .join("retroachievements")
        .join(format!("{gid}.json"));
    let has_ra_id = game
        .get("ra_game_id")
        .and_then(|v| match v {
            Value::String(s) => Some(s.trim().to_owned()),
            Value::Number(n) => Some(n.to_string()),
            _ => None,
        })
        .is_some_and(|s| !s.is_empty());
    if !cache_path.is_file() && !has_ra_id {
        return Err(ApiError::ra_not_linked(
            "This game is not linked to RetroAchievements yet.",
        ));
    }
    let raw = fs::read_to_string(&cache_path)
        .ok()
        .and_then(|s| serde_json::from_str::<Value>(&s).ok())
        .filter(Value::is_object)
        .unwrap_or_else(|| json!({}));
    Ok(json!({
        "available": true,
        "game": compute_game_trail(game, &raw, None),
    }))
}

pub fn refresh_trail_progress(
    data_dir: &Path,
    state: &Value,
    credentials: &RaCredentials,
    game_ids: Option<&[String]>,
    cancel: Option<&AtomicBool>,
) -> Value {
    let ra_dir = data_dir.join("cache").join("retroachievements");
    let _ = fs::create_dir_all(&ra_dir);
    let trashed = trashed_ids(state);
    let wanted: Option<HashSet<&str>> = game_ids
        .filter(|v| !v.is_empty())
        .map(|v| v.iter().map(String::as_str).collect());

    let targets: Vec<&Value> = state
        .get("games")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .filter(|g| {
                    if !g.is_object() || g.get("hidden").and_then(Value::as_bool).unwrap_or(false) {
                        return false;
                    }
                    let gid = g
                        .get("game_id")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .trim();
                    if gid.is_empty() || trashed.contains(gid) {
                        return false;
                    }
                    if let Some(ref w) = wanted
                        && !w.contains(gid)
                    {
                        return false;
                    }
                    let has_ra = g
                        .get("ra_game_id")
                        .is_some_and(|v| !v.is_null() && v.as_str() != Some(""));
                    has_ra || ra_dir.join(format!("{gid}.json")).is_file()
                })
                .collect()
        })
        .unwrap_or_default();

    let total = targets.len();
    let mut refreshed = 0usize;
    let mut errors: Vec<String> = Vec::new();
    for (idx, game) in targets.iter().enumerate() {
        if cancel.is_some_and(|c| c.load(Ordering::Acquire)) {
            break;
        }
        let gid = game.get("game_id").and_then(Value::as_str).unwrap_or("");
        let name = game.get("name").and_then(Value::as_str).unwrap_or(gid);
        let cache_path = ra_dir.join(format!("{gid}.json"));
        let res: Result<(), String> = (|| {
            let ra_id = match game.get("ra_game_id").and_then(|v| match v {
                Value::Number(n) => n.as_i64(),
                Value::String(s) => s.trim().parse::<i64>().ok(),
                _ => None,
            }) {
                Some(id) => id,
                None => match_game(game, credentials, &ra_dir)?.0,
            };
            let _ = cached(&cache_path, 0, || {
                let progress = game_progress(ra_id, credentials)?;
                let earned = progress["earned"].as_i64().unwrap_or(0);
                let total_ach = progress["total"].as_i64().unwrap_or(0);
                let pct = if total_ach > 0 {
                    py_round((earned as f64) * 100.0 / (total_ach as f64), 1)
                } else {
                    0.0
                };
                let ach: Vec<Value> = progress
                    .get("achievements")
                    .and_then(Value::as_array)
                    .map(|a| a.iter().take(500).cloned().collect())
                    .unwrap_or_default();
                Ok(json!({
                    "game_id": ra_id,
                    "earned": earned,
                    "earned_hardcore": progress["earned_hardcore"].as_i64().unwrap_or(0),
                    "total": total_ach,
                    "progress_pct": pct,
                    "mastered": progress["mastered"].as_bool().unwrap_or(false),
                    "beaten": progress["beaten"].as_i64().unwrap_or(0),
                    "achievements": ach,
                }))
            })?;
            Ok(())
        })();
        match res {
            Ok(()) => refreshed += 1,
            Err(e) => errors.push(format!("{name}: {e}")),
        }
        if idx + 1 < total {
            std::thread::sleep(Duration::from_millis(DEFAULT_SPACING_MILLIS));
        }
    }
    errors.truncate(20);
    json!({"refreshed": refreshed, "total": total, "errors": errors})
}
