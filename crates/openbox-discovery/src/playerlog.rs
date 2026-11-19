//! Player's Log — Time Capsules & Deterministic Milestone Cards (`pkg/parity/parity_playerlog.py`, ADR 0112).

use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use jiff::civil::Date;
use jiff::{Timestamp, ToSpan};
use openbox_core::ApiError;
use openbox_imports::catalog::total_playtime_seconds;
use openbox_runtime::{NotificationSpec, add_notification};
use regex::Regex;
use serde_json::{Map, Value, json};
use uuid::Uuid;

use crate::insights::compute_streak;
use crate::radio::is_completed;

pub const CAPSULE_TITLE_MAX: usize = 120;
pub const CAPSULE_BODY_MAX: usize = 4000;
pub const CAPSULE_KINDS: &[&str] = &["note", "question"];

pub const GAME_PLAYTIME_HOURS: &[i64] = &[10, 25, 50, 100, 250, 500, 1000];
pub const TOTAL_PLAYTIME_HOURS: &[i64] = &[100, 500, 1000, 2500, 5000, 10000];
pub const SESSION_MILESTONES: &[i64] = &[100, 500, 1000];
pub const RA_POINTS_MILESTONES: &[i64] = &[100, 200, 500, 1000, 2500];
pub const STREAK_MILESTONES: &[i64] = &[7, 30, 100, 365];
pub const LIBRARY_AGE_YEARS: &[i64] = &[1, 2, 5, 10];
pub const COLLECTION_MILESTONES: &[i64] = &[10, 25, 50, 100];

static STRICT_DATE_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^\d{4}-\d{2}-\d{2}$").unwrap());
static DATE_PREFIX_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^(\d{4}-\d{2}-\d{2})").unwrap());

fn today_date(today: Option<Date>) -> Date {
    today.unwrap_or_else(|| {
        jiff::tz::TimeZone::system()
            .to_datetime(Timestamp::now())
            .date()
    })
}

fn utc_iso(now_iso: Option<&str>) -> String {
    if let Some(s) = now_iso {
        return s.to_owned();
    }
    let dt = jiff::tz::TimeZone::UTC.to_datetime(Timestamp::now());
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

fn fmt_commas(n: i64) -> String {
    let s = n.abs().to_string();
    let mut out = String::new();
    for (i, ch) in s.chars().rev().enumerate() {
        if i > 0 && i % 3 == 0 {
            out.push(',');
        }
        out.push(ch);
    }
    if n < 0 {
        out.push('-');
    }
    out.chars().rev().collect()
}

fn parse_unlock_date(raw: Option<&Value>) -> Result<Date, ApiError> {
    let text = raw.and_then(Value::as_str).map(str::trim).unwrap_or("");
    if !STRICT_DATE_RE.is_match(text) {
        return Err(ApiError::capsule_invalid_date(
            "unlock_at must be a valid YYYY-MM-DD local date.",
        ));
    }
    text.parse::<Date>()
        .map_err(|_| ApiError::capsule_invalid_date("unlock_at is not a valid calendar date."))
}

fn parse_date_prefix(raw: Option<&Value>) -> Option<Date> {
    let text = raw.and_then(Value::as_str)?.trim();
    let cap = DATE_PREFIX_RE.captures(text)?;
    cap.get(1)?.as_str().parse::<Date>().ok()
}

fn is_leap(year: i16) -> bool {
    (year % 4 == 0 && year % 100 != 0) || (year % 400 == 0)
}

fn add_years(anchor: Date, years: i64) -> Date {
    let target_year = anchor.year() + years as i16;
    let day = if anchor.month() == 2 && anchor.day() == 29 && !is_leap(target_year) {
        28
    } else {
        anchor.day()
    };
    Date::new(target_year, anchor.month(), day).unwrap_or(anchor)
}

fn trashed_game_ids(state: &Value) -> HashSet<String> {
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

fn public_capsule(cap: &Value, today: Date) -> Value {
    let opened_at = cap
        .get("opened_at")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(str::to_owned);
    let unlock_str = cap.get("unlock_at").and_then(Value::as_str).unwrap_or("");
    let due = !unlock_str.is_empty()
        && parse_unlock_date(Some(&Value::String(unlock_str.to_owned()))).is_ok_and(|d| d <= today);
    let mut view = Map::new();
    view.insert(
        "id".into(),
        Value::String(
            cap.get("id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned(),
        ),
    );
    view.insert(
        "game_id".into(),
        Value::String(
            cap.get("game_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned(),
        ),
    );
    view.insert(
        "game_name".into(),
        Value::String(
            cap.get("game_name")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned(),
        ),
    );
    view.insert(
        "title".into(),
        Value::String(
            cap.get("title")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned(),
        ),
    );
    view.insert(
        "kind".into(),
        Value::String(
            cap.get("kind")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .unwrap_or("note")
                .to_owned(),
        ),
    );
    view.insert("unlock_at".into(), Value::String(unlock_str.to_owned()));
    view.insert(
        "created_at".into(),
        Value::String(
            cap.get("created_at")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned(),
        ),
    );
    view.insert(
        "opened_at".into(),
        opened_at
            .as_ref()
            .map_or(Value::Null, |s| Value::String(s.clone())),
    );
    view.insert("locked".into(), Value::Bool(opened_at.is_none()));
    view.insert("due".into(), Value::Bool(due));
    if opened_at.is_some() {
        view.insert(
            "body".into(),
            Value::String(
                cap.get("body")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned(),
            ),
        );
    }
    Value::Object(view)
}

pub fn create_capsule(
    state: &mut Value,
    payload: &Map<String, Value>,
    today: Option<Date>,
    now_iso: Option<&str>,
) -> Result<Value, ApiError> {
    let body = payload.get("body").and_then(Value::as_str).unwrap_or("");
    if body.chars().count() > CAPSULE_BODY_MAX {
        return Err(ApiError::capsule_body_too_large(format!(
            "Capsule body exceeds {CAPSULE_BODY_MAX} characters."
        )));
    }
    if body.trim().is_empty() {
        return Err(ApiError::bad_request("Capsule body is required."));
    }
    let unlock_date = parse_unlock_date(payload.get("unlock_at"))?;
    let kind = payload
        .get("kind")
        .and_then(Value::as_str)
        .map(|s| s.trim().to_lowercase())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "note".to_owned());
    if !CAPSULE_KINDS.contains(&kind.as_str()) {
        return Err(ApiError::bad_request(
            "Capsule kind must be 'note' or 'question'.",
        ));
    }

    let game_id = payload
        .get("game_id")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("")
        .to_owned();
    let mut game_name = payload
        .get("game_name")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("")
        .to_owned();
    if !game_id.is_empty()
        && game_name.is_empty()
        && let Some(games) = state.get("games").and_then(Value::as_array)
    {
        for g in games {
            if g.get("game_id").and_then(Value::as_str) == Some(game_id.as_str()) {
                game_name = g
                    .get("name")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned();
                break;
            }
        }
    }

    let raw_title = payload
        .get("title")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("");
    if raw_title.chars().count() > CAPSULE_TITLE_MAX {
        return Err(ApiError::bad_request(format!(
            "Capsule title exceeds {CAPSULE_TITLE_MAX} characters."
        )));
    }
    let title = if !raw_title.is_empty() {
        raw_title.to_owned()
    } else if !game_name.is_empty() {
        game_name.chars().take(CAPSULE_TITLE_MAX).collect()
    } else {
        "Time Capsule".to_owned()
    };

    let cap_id = format!("cap-{}", &Uuid::new_v4().simple().to_string()[..16]);
    let record = json!({
        "id": cap_id,
        "game_id": game_id,
        "game_name": game_name.chars().take(CAPSULE_TITLE_MAX).collect::<String>(),
        "title": title,
        "body": body,
        "kind": kind,
        "unlock_at": unlock_date.to_string(),
        "created_at": utc_iso(now_iso),
        "opened_at": Value::Null,
    });
    if let Some(root) = state.as_object_mut() {
        let caps = root
            .entry("time_capsules".to_owned())
            .or_insert_with(|| Value::Array(Vec::new()));
        if !caps.is_array() {
            *caps = Value::Array(Vec::new());
        }
        if let Some(arr) = caps.as_array_mut() {
            arr.push(record.clone());
        }
    }
    Ok(public_capsule(&record, today_date(today)))
}

pub fn list_capsules(state: &Value, game_id: &str, today: Option<Date>) -> Value {
    let today_val = today_date(today);
    let wanted = game_id.trim();
    let mut projected: Vec<Value> = state
        .get("time_capsules")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .filter(|c| c.is_object())
                .filter(|c| {
                    wanted.is_empty()
                        || c.get("game_id").and_then(Value::as_str).unwrap_or("") == wanted
                })
                .map(|c| public_capsule(c, today_val))
                .collect()
        })
        .unwrap_or_default();
    projected.sort_by(|a, b| {
        let ka = (
            a.get("unlock_at").and_then(Value::as_str).unwrap_or(""),
            a.get("created_at").and_then(Value::as_str).unwrap_or(""),
            a.get("id").and_then(Value::as_str).unwrap_or(""),
        );
        let kb = (
            b.get("unlock_at").and_then(Value::as_str).unwrap_or(""),
            b.get("created_at").and_then(Value::as_str).unwrap_or(""),
            b.get("id").and_then(Value::as_str).unwrap_or(""),
        );
        ka.cmp(&kb)
    });
    let locked_count = projected
        .iter()
        .filter(|c| c.get("locked").and_then(Value::as_bool).unwrap_or(false))
        .count();
    let total = projected.len();
    json!({
        "capsules": projected,
        "locked_count": locked_count,
        "unlocked_count": total - locked_count,
    })
}

pub fn next_capsule(state: &Value, today: Option<Date>) -> Value {
    let today_val = today_date(today);
    let mut sealed: Vec<(Date, &Value)> = Vec::new();
    let mut due_count = 0usize;
    if let Some(arr) = state.get("time_capsules").and_then(Value::as_array) {
        for cap in arr {
            if !cap.is_object()
                || cap
                    .get("opened_at")
                    .and_then(Value::as_str)
                    .is_some_and(|s| !s.is_empty())
            {
                continue;
            }
            let Ok(u_date) = parse_unlock_date(cap.get("unlock_at")) else {
                continue;
            };
            if u_date <= today_val {
                due_count += 1;
            }
            sealed.push((u_date, cap));
        }
    }
    sealed.sort_by(|a, b| {
        a.0.cmp(&b.0)
            .then_with(|| {
                let ca = a.1.get("created_at").and_then(Value::as_str).unwrap_or("");
                let cb = b.1.get("created_at").and_then(Value::as_str).unwrap_or("");
                ca.cmp(cb)
            })
            .then_with(|| {
                let ia = a.1.get("id").and_then(Value::as_str).unwrap_or("");
                let ib = b.1.get("id").and_then(Value::as_str).unwrap_or("");
                ia.cmp(ib)
            })
    });
    let Some((soonest_date, soonest_cap)) = sealed.into_iter().next() else {
        return json!({"capsule": Value::Null, "days_until": Value::Null, "due_count": 0});
    };
    let days_until = today_val
        .until(soonest_date)
        .map(|s| s.get_days().max(0))
        .unwrap_or(0);
    json!({
        "capsule": public_capsule(soonest_cap, today_val),
        "days_until": days_until,
        "due_count": due_count,
    })
}

pub fn open_capsule(
    state: &mut Value,
    capsule_id: &str,
    today: Option<Date>,
    now_iso: Option<&str>,
) -> Result<Value, ApiError> {
    let cid = capsule_id.trim();
    let today_val = today_date(today);
    let Some(caps) = state.get_mut("time_capsules").and_then(Value::as_array_mut) else {
        return Err(ApiError::capsule_not_found(format!(
            "Time capsule not found: {cid}"
        )));
    };
    let Some(target) = caps
        .iter_mut()
        .find(|c| c.get("id").and_then(Value::as_str) == Some(cid))
    else {
        return Err(ApiError::capsule_not_found(format!(
            "Time capsule not found: {cid}"
        )));
    };
    if target
        .get("opened_at")
        .and_then(Value::as_str)
        .is_some_and(|s| !s.is_empty())
    {
        return Ok(public_capsule(target, today_val));
    }
    let unlock_date = parse_unlock_date(target.get("unlock_at"))?;
    if unlock_date > today_val {
        return Err(ApiError::capsule_locked(format!(
            "Capsule unlocks on {unlock_date} and cannot be opened early."
        )));
    }
    if let Some(obj) = target.as_object_mut() {
        obj.insert("opened_at".into(), Value::String(utc_iso(now_iso)));
    }
    Ok(public_capsule(target, today_val))
}

pub fn delete_capsule(state: &mut Value, capsule_id: &str) -> Result<Value, ApiError> {
    let cid = capsule_id.trim();
    let Some(caps) = state.get_mut("time_capsules").and_then(Value::as_array_mut) else {
        return Err(ApiError::capsule_not_found(format!(
            "Time capsule not found: {cid}"
        )));
    };
    let before = caps.len();
    caps.retain(|c| c.get("id").and_then(Value::as_str) != Some(cid));
    if caps.len() == before {
        return Err(ApiError::capsule_not_found(format!(
            "Time capsule not found: {cid}"
        )));
    }
    Ok(json!({"deleted": true, "id": cid}))
}

pub fn unlock_due_capsules(
    state: &mut Value,
    today: Option<Date>,
    now_iso: Option<&str>,
) -> Vec<Value> {
    let today_val = today_date(today);
    let stamp = utc_iso(now_iso);
    let mut unlocked: Vec<Value> = Vec::new();
    let mut pending_notices: Vec<(String, String)> = Vec::new();
    if let Some(caps) = state.get_mut("time_capsules").and_then(Value::as_array_mut) {
        for cap in caps.iter_mut() {
            if !cap.is_object()
                || cap
                    .get("opened_at")
                    .and_then(Value::as_str)
                    .is_some_and(|s| !s.is_empty())
            {
                continue;
            }
            let Ok(u_date) = parse_unlock_date(cap.get("unlock_at")) else {
                continue;
            };
            if u_date > today_val {
                continue;
            }
            if let Some(obj) = cap.as_object_mut() {
                obj.insert("opened_at".into(), Value::String(stamp.clone()));
            }
            let cid = cap
                .get("id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            let title = cap
                .get("title")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .unwrap_or("Time Capsule")
                .to_owned();
            pending_notices.push((cid, title));
            unlocked.push(public_capsule(cap, today_val));
        }
    }
    for (cid, cap_title) in pending_notices {
        let dedupe = format!("cap:{cid}");
        let body = format!("\u{201c}{cap_title}\u{201d} reached its unlock date.");
        let mut params = Map::new();
        params.insert("title".into(), Value::String(cap_title));
        add_notification(
            state,
            NotificationSpec {
                kind: "capsule",
                level: "info",
                title: "A time capsule is ready to open",
                body: &body,
                source: "playerlog",
                dedupe_key: &dedupe,
                title_key: "notify.server.capsule_unlocked_title",
                body_key: "notify.server.capsule_unlocked_body",
                params: Some(&params),
                now: Some(&stamp),
                ..Default::default()
            },
        );
    }
    unlocked
}

// ── Milestones ───────────────────────────────────────────────────────────────

fn binary_search_crossing(
    events: &[(Date, i64)],
    threshold: i64,
    fallback_date: &str,
) -> (String, bool) {
    if events.is_empty() || threshold <= 0 {
        return (fallback_date.to_owned(), true);
    }
    let mut ordered = events.to_vec();
    ordered.sort_by_key(|(d, _)| *d);
    let mut dates = Vec::with_capacity(ordered.len());
    let mut prefix = Vec::with_capacity(ordered.len());
    let mut running = 0i64;
    for (day, delta) in ordered {
        let val = delta.max(0);
        if val == 0 {
            continue;
        }
        running += val;
        dates.push(day);
        prefix.push(running);
    }
    if prefix.last().is_none_or(|&last| last < threshold) {
        return (fallback_date.to_owned(), true);
    }
    let idx = prefix.partition_point(|&v| v < threshold);
    (dates[idx].to_string(), false)
}

struct RaEntry {
    game_id: String,
    path: PathBuf,
    data: Value,
}

fn load_ra_cache_entries(data_dir: Option<&Path>) -> Vec<RaEntry> {
    let Some(dir) = data_dir else {
        return Vec::new();
    };
    let ra_dir = dir.join("cache").join("retroachievements");
    if !ra_dir.is_dir() {
        return Vec::new();
    }
    let mut paths: Vec<PathBuf> = std::fs::read_dir(&ra_dir)
        .ok()
        .map(|rd| {
            rd.flatten()
                .map(|e| e.path())
                .filter(|p| p.is_file() && p.extension().and_then(|s| s.to_str()) == Some("json"))
                .collect()
        })
        .unwrap_or_default();
    paths.sort();
    let mut out = Vec::new();
    for path in paths {
        let Some(stem) = path.file_stem().and_then(|s| s.to_str()) else {
            continue;
        };
        if let Ok(raw) = std::fs::read_to_string(&path)
            && let Ok(val) = serde_json::from_str::<Value>(&raw)
            && val.is_object()
        {
            out.push(RaEntry {
                game_id: stem.to_owned(),
                path,
                data: val,
            });
        }
    }
    out
}

fn fallback_date_for_game(game: &Value, today_str: &str) -> String {
    for field in ["completed_at", "last_played", "added_at"] {
        if let Some(d) = parse_date_prefix(game.get(field)) {
            return d.to_string();
        }
    }
    today_str.to_owned()
}

fn compute_full_history_streak(
    all_counts: &[(Date, i64)],
    today_val: Date,
) -> (i64, HashMap<i64, String>) {
    if all_counts.is_empty() {
        return (0, HashMap::new());
    }
    let mut by_day: BTreeMap<Date, i64> = BTreeMap::new();
    for &(day, count) in all_counts {
        *by_day.entry(day).or_insert(0) += count;
    }
    let start_day = *by_day.keys().next().unwrap();
    let max_day = *by_day.keys().next_back().unwrap();
    let end_day = max_day.max(today_val);
    let span = start_day.until(end_day).map(|s| s.get_days()).unwrap_or(0) + 1;
    let mut rows: Vec<Value> = Vec::with_capacity(span.max(0) as usize);
    let mut running = 0i64;
    let mut crossed: HashMap<i64, String> = HashMap::new();
    for offset in 0..span {
        let cur = start_day.checked_add(offset.days()).unwrap_or(end_day);
        let cnt = by_day.get(&cur).copied().unwrap_or(0);
        let iso = cur.to_string();
        rows.push(json!({"date": iso, "count": cnt}));
        if cnt > 0 {
            running += 1;
            for &target in STREAK_MILESTONES {
                if running >= target && !crossed.contains_key(&target) {
                    crossed.insert(target, iso.clone());
                }
            }
        } else {
            running = 0;
        }
    }
    let streak_info = compute_streak(&rows);
    let longest = streak_info
        .get("longest")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    (longest, crossed)
}

#[allow(clippy::too_many_lines)]
pub fn compute_milestones(
    state: &Value,
    data_dir: Option<&Path>,
    today: Option<Date>,
    limit: Option<usize>,
) -> Value {
    let today_val = today_date(today);
    let today_str = today_val.to_string();
    let trashed = trashed_game_ids(state);
    let games: Vec<&Value> = state
        .get("games")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .filter(|g| {
                    g.is_object()
                        && !g.get("hidden").and_then(Value::as_bool).unwrap_or(false)
                        && !trashed.contains(g.get("game_id").and_then(Value::as_str).unwrap_or(""))
                })
                .collect()
        })
        .unwrap_or_default();
    let game_by_id: HashMap<String, &Value> = games
        .iter()
        .filter_map(|&g| {
            let gid = g.get("game_id").and_then(Value::as_str)?;
            if gid.is_empty() {
                None
            } else {
                Some((gid.to_owned(), g))
            }
        })
        .collect();
    let seen_ids: HashSet<String> = state
        .get("ui_state")
        .and_then(|u| u.get("milestones_seen"))
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .filter(|s| !s.is_empty())
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default();

    // Collect session events
    let eligible_ids: HashSet<&str> = games
        .iter()
        .filter_map(|g| {
            g.get("game_id")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
        })
        .collect();
    let name_to_gid: HashMap<&str, &str> = games
        .iter()
        .filter_map(|g| {
            let gid = g
                .get("game_id")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())?;
            let name = g
                .get("name")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())?;
            Some((name, gid))
        })
        .collect();

    let mut per_game_events: HashMap<String, Vec<(Date, i64)>> = HashMap::new();
    let mut all_seconds: Vec<(Date, i64)> = Vec::new();
    let mut all_counts: Vec<(Date, i64)> = Vec::new();

    if let Some(history) = state.get("history").and_then(Value::as_array) {
        for entry in history {
            if !entry.is_object() {
                continue;
            }
            let mut gid = entry
                .get("game_id")
                .and_then(Value::as_str)
                .map(str::trim)
                .unwrap_or("")
                .to_owned();
            if gid.is_empty()
                && let Some(gname) = entry.get("game").and_then(Value::as_str)
                && let Some(&mapped) = name_to_gid.get(gname)
            {
                gid = mapped.to_owned();
            }
            let Some(day) = parse_date_prefix(
                entry
                    .get("started")
                    .or_else(|| entry.get("ended"))
                    .or_else(|| entry.get("date")),
            ) else {
                continue;
            };
            let secs = entry
                .get("seconds")
                .and_then(|v| v.as_i64().or_else(|| v.as_f64().map(|f| f as i64)))
                .unwrap_or(0)
                .max(0);
            all_counts.push((day, 1));
            if eligible_ids.is_empty() || eligible_ids.contains(gid.as_str()) {
                all_seconds.push((day, secs));
                if !gid.is_empty() {
                    per_game_events.entry(gid).or_default().push((day, secs));
                }
            }
        }
    }
    for &game in &games {
        let gid = game
            .get("game_id")
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or("");
        if let Some(ms_arr) = game.get("manual_sessions").and_then(Value::as_array) {
            for ms in ms_arr {
                if !ms.is_object() {
                    continue;
                }
                let Some(day) = parse_date_prefix(ms.get("date")) else {
                    continue;
                };
                let secs = ms
                    .get("seconds")
                    .and_then(|v| v.as_i64().or_else(|| v.as_f64().map(|f| f as i64)))
                    .unwrap_or(0)
                    .max(0);
                all_counts.push((day, 1));
                all_seconds.push((day, secs));
                if !gid.is_empty() {
                    per_game_events
                        .entry(gid.to_owned())
                        .or_default()
                        .push((day, secs));
                }
            }
        }
    }

    let ra_entries = load_ra_cache_entries(data_dir);
    let ra_by_gid: HashMap<&str, &Value> = ra_entries
        .iter()
        .map(|e| (e.game_id.as_str(), &e.data))
        .collect();

    let mut latest_candidates: Vec<Date> = all_seconds.iter().map(|(d, _)| *d).collect();
    for &g in &games {
        for field in ["last_played", "completed_at", "added_at"] {
            if let Some(d) = parse_date_prefix(g.get(field)) {
                latest_candidates.push(d);
            }
        }
    }
    let latest_date = latest_candidates
        .into_iter()
        .max()
        .map(|d| d.to_string())
        .unwrap_or_else(|| today_str.clone());

    let mut unlocked: Vec<Value> = Vec::new();
    let mut upcoming: Vec<Value> = Vec::new();

    #[allow(clippy::too_many_arguments)]
    let mut add_unlocked = |mid: String,
                            kind: &str,
                            title: String,
                            subtitle: String,
                            value: i64,
                            threshold: i64,
                            unlocked_at: String,
                            approximate: bool,
                            game: Option<&Value>| {
        let is_new = !seen_ids.contains(&mid);
        let mut obj = Map::new();
        obj.insert("id".into(), Value::String(mid));
        obj.insert("kind".into(), Value::String(kind.to_owned()));
        obj.insert("title".into(), Value::String(title));
        obj.insert("subtitle".into(), Value::String(subtitle));
        obj.insert("value".into(), json!(value));
        obj.insert("threshold".into(), json!(threshold));
        obj.insert("unlocked_at".into(), Value::String(unlocked_at));
        obj.insert("approximate".into(), Value::Bool(approximate));
        obj.insert("is_new".into(), Value::Bool(is_new));
        if let Some(g) = game {
            obj.insert(
                "game_id".into(),
                Value::String(
                    g.get("game_id")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned(),
                ),
            );
            obj.insert(
                "game_name".into(),
                Value::String(
                    g.get("name")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned(),
                ),
            );
        }
        unlocked.push(Value::Object(obj));
    };

    // 1. playtime_game
    for &game in &games {
        let gid = game
            .get("game_id")
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or("");
        if gid.is_empty() {
            continue;
        }
        let secs = total_playtime_seconds(game).max(0);
        if secs < 3600 {
            continue;
        }
        let hours = secs as f64 / 3600.0;
        let gname = game.get("name").and_then(Value::as_str).unwrap_or("Game");
        let events = per_game_events.get(gid).map(Vec::as_slice).unwrap_or(&[]);
        let fallback = if secs >= GAME_PLAYTIME_HOURS[0] * 3600 {
            fallback_date_for_game(game, &today_str)
        } else {
            String::new()
        };
        let mut next_target: Option<i64> = None;
        for &h in GAME_PLAYTIME_HOURS {
            if secs >= h * 3600 {
                let (u_at, approx) = binary_search_crossing(events, h * 3600, &fallback);
                add_unlocked(
                    format!("m:playtime_game:{gid}:{h}h"),
                    "playtime_game",
                    format!("{h}h in {gname}"),
                    format!("{:.1} hours played", (hours * 10.0).round() / 10.0),
                    hours as i64,
                    h,
                    u_at,
                    approx,
                    Some(game),
                );
            } else if next_target.is_none() && hours >= 1.0 {
                next_target = Some(h);
            }
        }
        if let Some(nt) = next_target {
            upcoming.push(json!({
                "id": format!("m:playtime_game:{gid}:{nt}h"),
                "kind": "playtime_game",
                "title": format!("{nt}h in {gname}"),
                "game_id": gid,
                "current": (hours * 10.0).round() / 10.0,
                "target": nt,
                "progress_pct": ((hours / nt as f64 * 100.0).round() as i64).min(99),
            }));
        }
    }

    // 2. playtime_total
    let total_secs: i64 = games.iter().map(|g| total_playtime_seconds(g).max(0)).sum();
    let total_hours = total_secs as f64 / 3600.0;
    let mut next_total: Option<i64> = None;
    for &h in TOTAL_PLAYTIME_HOURS {
        if total_secs >= h * 3600 {
            let (u_at, approx) = binary_search_crossing(&all_seconds, h * 3600, &latest_date);
            add_unlocked(
                format!("m:playtime_total:{h}h"),
                "playtime_total",
                format!("{} Library Hours", fmt_commas(h)),
                format!(
                    "{:.1} total hours across your collection",
                    (total_hours * 10.0).round() / 10.0
                ),
                total_hours as i64,
                h,
                u_at,
                approx,
                None,
            );
        } else if next_total.is_none() {
            next_total = Some(h);
        }
    }
    if let Some(nt) = next_total {
        upcoming.push(json!({
            "id": format!("m:playtime_total:{nt}h"),
            "kind": "playtime_total",
            "title": format!("{} Library Hours", fmt_commas(nt)),
            "current": (total_hours * 10.0).round() / 10.0,
            "target": nt,
            "progress_pct": ((total_hours / nt as f64 * 100.0).round() as i64).min(99),
        }));
    }

    // 3. first_clear
    for &game in &games {
        let gid = game
            .get("game_id")
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or("");
        if gid.is_empty() {
            continue;
        }
        let ra = ra_by_gid.get(gid).copied();
        let cleared = is_completed(game)
            || ra.is_some_and(|r| {
                r.get("mastered").and_then(Value::as_bool).unwrap_or(false)
                    || r.get("beaten").and_then(Value::as_i64).unwrap_or(0) > 0
            });
        if !cleared {
            continue;
        }
        let (u_at, approx) = if let Some(exp) = parse_date_prefix(game.get("completed_at")) {
            (exp.to_string(), false)
        } else if let Some(ev) = per_game_events.get(gid)
            && let Some(max_d) = ev.iter().map(|(d, _)| *d).max()
        {
            (max_d.to_string(), true)
        } else {
            (fallback_date_for_game(game, &today_str), true)
        };
        let gname = game.get("name").and_then(Value::as_str).unwrap_or("Game");
        let subtitle = game
            .get("progress")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .unwrap_or("Completed")
            .to_owned();
        add_unlocked(
            format!("m:first_clear:{gid}"),
            "first_clear",
            format!("Cleared {gname}"),
            subtitle,
            1,
            1,
            u_at,
            approx,
            Some(game),
        );
    }

    // 4. sessions
    let manual_total: i64 = games
        .iter()
        .map(|g| {
            g.get("manual_sessions")
                .and_then(Value::as_array)
                .map_or(0, Vec::len) as i64
        })
        .sum();
    let history_total = state
        .get("history")
        .and_then(Value::as_array)
        .map_or(0, |a| a.iter().filter(|e| e.is_object()).count()) as i64;
    let total_sessions = history_total + manual_total;
    let mut next_sessions: Option<i64> = None;
    for &count in SESSION_MILESTONES {
        if total_sessions >= count {
            let (u_at, approx) = binary_search_crossing(&all_counts, count, &latest_date);
            add_unlocked(
                format!("m:sessions:{count}"),
                "sessions",
                format!("{} Play Sessions", fmt_commas(count)),
                format!("{} logged sessions", fmt_commas(total_sessions)),
                total_sessions,
                count,
                u_at,
                approx,
                None,
            );
        } else if next_sessions.is_none() {
            next_sessions = Some(count);
        }
    }
    if let Some(ns) = next_sessions {
        upcoming.push(json!({
            "id": format!("m:sessions:{ns}"),
            "kind": "sessions",
            "title": format!("{} Play Sessions", fmt_commas(ns)),
            "current": total_sessions,
            "target": ns,
            "progress_pct": ((total_sessions as f64 / ns as f64 * 100.0).round() as i64).min(99),
        }));
    }

    // 5 & 6. ra_points & ra_complete
    let ra_available = !ra_entries.is_empty();
    let mut ra_events: Vec<(Date, i64)> = Vec::new();
    let mut total_ra_pts = 0i64;
    let mut latest_ra_mtime: Option<Date> = None;
    for entry in &ra_entries {
        let file_date = std::fs::metadata(&entry.path)
            .ok()
            .and_then(|m| m.modified().ok())
            .and_then(|st| Timestamp::try_from(st).ok())
            .map(|ts| jiff::tz::TimeZone::UTC.to_datetime(ts).date())
            .unwrap_or(today_val);
        if latest_ra_mtime.is_none_or(|cur| file_date > cur) {
            latest_ra_mtime = Some(file_date);
        }
        let mut earned_dates: Vec<Date> = Vec::new();
        if let Some(achs) = entry.data.get("achievements").and_then(Value::as_array) {
            for ach in achs {
                if !ach.is_object() || !ach.get("earned").and_then(Value::as_bool).unwrap_or(false)
                {
                    continue;
                }
                let pts = ach
                    .get("points")
                    .and_then(Value::as_i64)
                    .unwrap_or(0)
                    .max(0);
                total_ra_pts += pts;
                if let Some(ed) = parse_date_prefix(
                    ach.get("date_earned_hardcore")
                        .or_else(|| ach.get("date_earned")),
                ) {
                    earned_dates.push(ed);
                    ra_events.push((ed, pts));
                }
            }
        }
        let total_ach = entry.data.get("total").and_then(Value::as_i64).unwrap_or(0);
        let earned_ach = entry
            .data
            .get("earned")
            .and_then(Value::as_i64)
            .unwrap_or(0);
        if total_ach > 0
            && (entry
                .data
                .get("mastered")
                .and_then(Value::as_bool)
                .unwrap_or(false)
                || earned_ach >= total_ach)
        {
            let game = game_by_id.get(&entry.game_id).copied();
            let gname = game
                .and_then(|g| g.get("name").and_then(Value::as_str))
                .filter(|s| !s.is_empty())
                .unwrap_or(&entry.game_id);
            let (u_at, approx) = if let Some(max_ed) = earned_dates.into_iter().max() {
                (max_ed.to_string(), false)
            } else {
                (file_date.to_string(), true)
            };
            add_unlocked(
                format!("m:ra_complete:{}", entry.game_id),
                "ra_complete",
                format!("Mastered {gname}"),
                format!("{earned_ach}/{total_ach} RetroAchievements"),
                earned_ach,
                total_ach,
                u_at,
                approx,
                game,
            );
        }
    }
    if ra_available && total_ra_pts > 0 {
        let fallback_ra = latest_ra_mtime.unwrap_or(today_val).to_string();
        let mut next_pts: Option<i64> = None;
        for &pts_target in RA_POINTS_MILESTONES {
            if total_ra_pts >= pts_target {
                let (u_at, approx) = binary_search_crossing(&ra_events, pts_target, &fallback_ra);
                add_unlocked(
                    format!("m:ra_points:{pts_target}"),
                    "ra_points",
                    format!("{} RetroAchievements Points", fmt_commas(pts_target)),
                    format!("{} total points earned", fmt_commas(total_ra_pts)),
                    total_ra_pts,
                    pts_target,
                    u_at,
                    approx,
                    None,
                );
            } else if next_pts.is_none() {
                next_pts = Some(pts_target);
            }
        }
        if let Some(np) = next_pts {
            upcoming.push(json!({
                "id": format!("m:ra_points:{np}"),
                "kind": "ra_points",
                "title": format!("{} RetroAchievements Points", fmt_commas(np)),
                "current": total_ra_pts,
                "target": np,
                "progress_pct": ((total_ra_pts as f64 / np as f64 * 100.0).round() as i64).min(99),
            }));
        }
    }

    // 7. streak
    let (longest_streak, streak_crossed) = compute_full_history_streak(&all_counts, today_val);
    let mut next_streak: Option<i64> = None;
    for &days_target in STREAK_MILESTONES {
        if longest_streak >= days_target
            && let Some(dt_str) = streak_crossed.get(&days_target)
        {
            add_unlocked(
                format!("m:streak:{days_target}d"),
                "streak",
                format!("{days_target}-Day Play Streak"),
                format!("Longest streak: {longest_streak} consecutive days"),
                longest_streak,
                days_target,
                dt_str.clone(),
                false,
                None,
            );
        } else if next_streak.is_none() {
            next_streak = Some(days_target);
        }
    }
    if let Some(ns) = next_streak {
        upcoming.push(json!({
            "id": format!("m:streak:{ns}d"),
            "kind": "streak",
            "title": format!("{ns}-Day Play Streak"),
            "current": longest_streak,
            "target": ns,
            "progress_pct": ((longest_streak as f64 / ns as f64 * 100.0).round() as i64).min(99),
        }));
    }

    // 8. library_age
    let mut added_dates: Vec<Date> = games
        .iter()
        .filter_map(|g| parse_date_prefix(g.get("added_at")))
        .collect();
    let anchor_date = added_dates
        .iter()
        .copied()
        .min()
        .or_else(|| all_counts.iter().map(|(d, _)| *d).min());
    if let Some(anchor) = anchor_date {
        let mut next_age: Option<i64> = None;
        for &years_target in LIBRARY_AGE_YEARS {
            let ann = add_years(anchor, years_target);
            if ann <= today_val {
                add_unlocked(
                    format!("m:library_age:{years_target}y"),
                    "library_age",
                    format!("{years_target}-Year Library Anniversary"),
                    format!("Collecting since {anchor}"),
                    years_target,
                    years_target,
                    ann.to_string(),
                    false,
                    None,
                );
            } else if next_age.is_none() {
                next_age = Some(years_target);
            }
        }
        if let Some(na) = next_age {
            let elapsed_days = anchor
                .until(today_val)
                .map(|s| s.get_days())
                .unwrap_or(0)
                .max(0);
            let target_days = na * 365;
            upcoming.push(json!({
                "id": format!("m:library_age:{na}y"),
                "kind": "library_age",
                "title": format!("{na}-Year Library Anniversary"),
                "current": elapsed_days,
                "target": target_days,
                "progress_pct": ((elapsed_days as f64 / target_days as f64 * 100.0).round() as i64).min(99),
            }));
        }
    }

    // 9. collection
    let collection_count = games.len() as i64;
    added_dates.sort();
    let mut next_coll: Option<i64> = None;
    for &size_target in COLLECTION_MILESTONES {
        if collection_count >= size_target {
            let (u_at, approx) = if added_dates.len() >= size_target as usize {
                (added_dates[size_target as usize - 1].to_string(), false)
            } else {
                (
                    added_dates
                        .last()
                        .map(|d| d.to_string())
                        .unwrap_or_else(|| latest_date.clone()),
                    true,
                )
            };
            add_unlocked(
                format!("m:collection:{size_target}"),
                "collection",
                format!("{size_target} Games Collected"),
                format!("{collection_count} active games in library"),
                collection_count,
                size_target,
                u_at,
                approx,
                None,
            );
        } else if next_coll.is_none() {
            next_coll = Some(size_target);
        }
    }
    if let Some(nc) = next_coll {
        upcoming.push(json!({
            "id": format!("m:collection:{nc}"),
            "kind": "collection",
            "title": format!("{nc} Games Collected"),
            "current": collection_count,
            "target": nc,
            "progress_pct": ((collection_count as f64 / nc as f64 * 100.0).round() as i64).min(99),
        }));
    }

    unlocked.sort_by(|a, b| {
        let ka = (
            a.get("unlocked_at").and_then(Value::as_str).unwrap_or(""),
            a.get("kind").and_then(Value::as_str).unwrap_or(""),
            a.get("id").and_then(Value::as_str).unwrap_or(""),
        );
        let kb = (
            b.get("unlocked_at").and_then(Value::as_str).unwrap_or(""),
            b.get("kind").and_then(Value::as_str).unwrap_or(""),
            b.get("id").and_then(Value::as_str).unwrap_or(""),
        );
        kb.cmp(&ka)
    });
    upcoming.sort_by(|a, b| {
        let pa = a.get("progress_pct").and_then(Value::as_i64).unwrap_or(0);
        let pb = b.get("progress_pct").and_then(Value::as_i64).unwrap_or(0);
        let ka = a.get("kind").and_then(Value::as_str).unwrap_or("");
        let kb = b.get("kind").and_then(Value::as_str).unwrap_or("");
        let ia = a.get("id").and_then(Value::as_str).unwrap_or("");
        let ib = b.get("id").and_then(Value::as_str).unwrap_or("");
        pb.cmp(&pa)
            .then_with(|| ka.cmp(kb))
            .then_with(|| ia.cmp(ib))
    });

    let mut by_kind: Map<String, Value> = Map::new();
    for item in &unlocked {
        let k = item.get("kind").and_then(Value::as_str).unwrap_or("");
        let next = by_kind.get(k).and_then(Value::as_u64).unwrap_or(0) + 1;
        by_kind.insert(k.to_owned(), json!(next));
    }
    let total_unlocked = unlocked.len();
    let new_count = unlocked
        .iter()
        .filter(|i| i.get("is_new").and_then(Value::as_bool).unwrap_or(false))
        .count();
    if let Some(lim) = limit {
        unlocked.truncate(lim);
    }
    upcoming.truncate(8);

    json!({
        "unlocked": unlocked,
        "upcoming": upcoming,
        "counts": {
            "unlocked": total_unlocked,
            "new": new_count,
            "by_kind": Value::Object(by_kind),
        },
        "ra_available": ra_available,
    })
}
