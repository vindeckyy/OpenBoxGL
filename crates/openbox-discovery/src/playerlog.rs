//! Player's Log — Time Capsules & Deterministic Milestone Cards (`pkg/parity/parity_playerlog.py`, ADR 0112).

use std::cmp::{Ordering, Reverse};
use std::collections::{BTreeMap, BinaryHeap, HashMap, HashSet};
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

#[inline]
fn parse_ascii_2(b: &[u8]) -> Option<i8> {
    let d0 = b[0].wrapping_sub(b'0');
    let d1 = b[1].wrapping_sub(b'0');
    if d0 < 10 && d1 < 10 {
        Some((d0 * 10 + d1) as i8)
    } else {
        None
    }
}

fn parse_date_prefix(raw: Option<&Value>) -> Option<Date> {
    let text = raw.and_then(Value::as_str)?.trim();
    let b = text.as_bytes();
    if b.len() < 10 || b[4] != b'-' || b[7] != b'-' {
        return None;
    }
    let y_hi = parse_ascii_2(&b[0..2])? as i16;
    let y_lo = parse_ascii_2(&b[2..4])? as i16;
    let m = parse_ascii_2(&b[5..7])?;
    let d = parse_ascii_2(&b[8..10])?;
    Date::new(y_hi * 100 + y_lo, m, d).ok()
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
    fallback_date: Date,
) -> (Date, bool) {
    if events.is_empty() || threshold <= 0 {
        return (fallback_date, true);
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
        return (fallback_date, true);
    }
    let idx = prefix.partition_point(|&v| v < threshold);
    (dates[idx], false)
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

fn fallback_date_for_game(game: &Value, today_val: Date) -> Date {
    for field in ["completed_at", "last_played", "added_at"] {
        if let Some(d) = parse_date_prefix(game.get(field)) {
            return d;
        }
    }
    today_val
}

fn compute_full_history_streak(
    all_counts: &[(Date, i64)],
    today_val: Date,
) -> (i64, HashMap<i64, Date>) {
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
    let mut crossed: HashMap<i64, Date> = HashMap::new();
    for offset in 0..span {
        let cur = start_day.checked_add(offset.days()).unwrap_or(end_day);
        let cnt = by_day.get(&cur).copied().unwrap_or(0);
        let iso = cur.to_string();
        rows.push(json!({"date": iso, "count": cnt}));
        if cnt > 0 {
            running += 1;
            for &target in STREAK_MILESTONES {
                if running >= target && !crossed.contains_key(&target) {
                    crossed.insert(target, cur);
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

struct UnlockedItem {
    date: Date,
    kind: &'static str,
    id: String,
    value: Value,
}

impl PartialEq for UnlockedItem {
    fn eq(&self, other: &Self) -> bool {
        self.date == other.date && self.kind == other.kind && self.id == other.id
    }
}

impl Eq for UnlockedItem {}

impl PartialOrd for UnlockedItem {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for UnlockedItem {
    fn cmp(&self, other: &Self) -> Ordering {
        self.date
            .cmp(&other.date)
            .then_with(|| self.kind.cmp(other.kind))
            .then_with(|| self.id.cmp(&other.id))
    }
}

struct UnlockedPool<'a> {
    limit: Option<usize>,
    seen_ids: &'a HashSet<String>,
    total_unlocked: usize,
    new_count: usize,
    by_kind: HashMap<&'static str, u64>,
    heap: BinaryHeap<Reverse<UnlockedItem>>,
    all: Vec<UnlockedItem>,
}

impl<'a> UnlockedPool<'a> {
    fn new(limit: Option<usize>, seen_ids: &'a HashSet<String>) -> Self {
        Self {
            limit,
            seen_ids,
            total_unlocked: 0,
            new_count: 0,
            by_kind: HashMap::new(),
            heap: BinaryHeap::with_capacity(limit.unwrap_or(0).min(1024)),
            all: Vec::new(),
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn push_lazy<FId, FTitle, FSub>(
        &mut self,
        date: Date,
        kind: &'static str,
        make_id: FId,
        make_title: FTitle,
        make_subtitle: FSub,
        value: i64,
        threshold: i64,
        approximate: bool,
        game: Option<&Value>,
    ) where
        FId: FnOnce() -> String,
        FTitle: FnOnce() -> String,
        FSub: FnOnce() -> String,
    {
        self.total_unlocked += 1;
        *self.by_kind.entry(kind).or_insert(0) += 1;

        let seen_empty = self.seen_ids.is_empty();
        if let Some(lim) = self.limit {
            if lim == 0 {
                if seen_empty || !self.seen_ids.contains(&make_id()) {
                    self.new_count += 1;
                }
                return;
            }
            if self.heap.len() == lim
                && let Some(Reverse(worst)) = self.heap.peek()
                && (date, kind) < (worst.date, worst.kind)
            {
                if seen_empty || !self.seen_ids.contains(&make_id()) {
                    self.new_count += 1;
                }
                return;
            }
        }

        let mid = make_id();
        let is_new = seen_empty || !self.seen_ids.contains(&mid);
        if is_new {
            self.new_count += 1;
        }

        if let Some(lim) = self.limit
            && self.heap.len() == lim
            && let Some(Reverse(worst)) = self.heap.peek()
            && (date, kind, mid.as_str()) <= (worst.date, worst.kind, worst.id.as_str())
        {
            return;
        }

        let mut obj = Map::new();
        obj.insert("id".into(), Value::String(mid.clone()));
        obj.insert("kind".into(), Value::String(kind.to_owned()));
        obj.insert("title".into(), Value::String(make_title()));
        obj.insert("subtitle".into(), Value::String(make_subtitle()));
        obj.insert("value".into(), json!(value));
        obj.insert("threshold".into(), json!(threshold));
        obj.insert("unlocked_at".into(), Value::String(date.to_string()));
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
        let item = UnlockedItem {
            date,
            kind,
            id: mid,
            value: Value::Object(obj),
        };
        if let Some(lim) = self.limit {
            self.heap.push(Reverse(item));
            if self.heap.len() > lim {
                self.heap.pop();
            }
        } else {
            self.all.push(item);
        }
    }

    fn into_sorted(self) -> (Vec<Value>, usize, usize, Map<String, Value>) {
        let mut items: Vec<UnlockedItem> = if self.limit.is_some() {
            self.heap.into_iter().map(|Reverse(x)| x).collect()
        } else {
            self.all
        };
        items.sort_by(|a, b| b.cmp(a));
        let values = items.into_iter().map(|x| x.value).collect();
        let mut by_kind_map = Map::new();
        for (k, v) in self.by_kind {
            by_kind_map.insert(k.to_owned(), json!(v));
        }
        (values, self.total_unlocked, self.new_count, by_kind_map)
    }
}

struct UpcomingItem {
    progress_pct: i64,
    kind: &'static str,
    id: String,
    value: Value,
}

impl PartialEq for UpcomingItem {
    fn eq(&self, other: &Self) -> bool {
        self.progress_pct == other.progress_pct && self.kind == other.kind && self.id == other.id
    }
}

impl Eq for UpcomingItem {}

impl PartialOrd for UpcomingItem {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for UpcomingItem {
    /// Worse items sort GREATER so `BinaryHeap::peek()` exposes the worst item in the top-8.
    fn cmp(&self, other: &Self) -> Ordering {
        other
            .progress_pct
            .cmp(&self.progress_pct)
            .then_with(|| self.kind.cmp(other.kind))
            .then_with(|| self.id.cmp(&other.id))
    }
}

struct UpcomingPool {
    heap: BinaryHeap<UpcomingItem>,
}

impl UpcomingPool {
    const CAP: usize = 8;

    fn new() -> Self {
        Self {
            heap: BinaryHeap::with_capacity(Self::CAP + 1),
        }
    }

    fn push_lazy<FId, FVal>(
        &mut self,
        progress_pct: i64,
        kind: &'static str,
        make_id: FId,
        make_val: FVal,
    ) where
        FId: FnOnce() -> String,
        FVal: FnOnce(&str) -> Value,
    {
        if self.heap.len() == Self::CAP
            && let Some(worst) = self.heap.peek()
            && (progress_pct < worst.progress_pct
                || (progress_pct == worst.progress_pct && kind > worst.kind))
        {
            return;
        }
        let id = make_id();
        if self.heap.len() == Self::CAP
            && let Some(worst) = self.heap.peek()
            && progress_pct == worst.progress_pct
            && kind == worst.kind
            && id >= worst.id
        {
            return;
        }
        let value = make_val(&id);
        self.heap.push(UpcomingItem {
            progress_pct,
            kind,
            id,
            value,
        });
        if self.heap.len() > Self::CAP {
            self.heap.pop();
        }
    }

    fn into_sorted(self) -> Vec<Value> {
        let mut items: Vec<UpcomingItem> = self.heap.into_iter().collect();
        // Best first (smallest according to UpcomingItem's worse-is-greater Ord)
        items.sort();
        items.into_iter().map(|x| x.value).collect()
    }
}

#[allow(clippy::too_many_lines)]
pub fn compute_milestones(
    state: &Value,
    data_dir: Option<&Path>,
    today: Option<Date>,
    limit: Option<usize>,
) -> Value {
    let today_val = today_date(today);
    let trashed = trashed_game_ids(state);
    let games: Vec<&Value> = state
        .get("games")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .filter(|g| {
                    g.is_object()
                        && !g.get("hidden").and_then(Value::as_bool).unwrap_or(false)
                        && (trashed.is_empty()
                            || !trashed
                                .contains(g.get("game_id").and_then(Value::as_str).unwrap_or("")))
                })
                .collect()
        })
        .unwrap_or_default();
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

    // Collect session events (build eligible_ids / name_to_gid lazily only when history is non-empty)
    let mut per_game_events: HashMap<&str, Vec<(Date, i64)>> = HashMap::new();
    let mut all_seconds: Vec<(Date, i64)> = Vec::new();
    let mut all_counts: Vec<(Date, i64)> = Vec::new();
    let mut history_total = 0i64;

    if let Some(history) = state
        .get("history")
        .and_then(Value::as_array)
        .filter(|h| !h.is_empty())
    {
        let eligible_ids: HashSet<&str> = games
            .iter()
            .filter_map(|g| {
                g.get("game_id")
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
            })
            .collect();
        let mut name_to_gid: Option<HashMap<&str, &str>> = None;
        for entry in history {
            if !entry.is_object() {
                continue;
            }
            history_total += 1;
            let mut gid = entry
                .get("game_id")
                .and_then(Value::as_str)
                .map(str::trim)
                .unwrap_or("");
            if gid.is_empty()
                && let Some(gname) = entry.get("game").and_then(Value::as_str)
            {
                let map = name_to_gid.get_or_insert_with(|| {
                    games
                        .iter()
                        .filter_map(|g| {
                            let id = g
                                .get("game_id")
                                .and_then(Value::as_str)
                                .filter(|s| !s.is_empty())?;
                            let name = g
                                .get("name")
                                .and_then(Value::as_str)
                                .filter(|s| !s.is_empty())?;
                            Some((name, id))
                        })
                        .collect()
                });
                if let Some(&mapped) = map.get(gname) {
                    gid = mapped;
                }
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
            if eligible_ids.is_empty() || eligible_ids.contains(gid) {
                all_seconds.push((day, secs));
                if !gid.is_empty() {
                    per_game_events.entry(gid).or_default().push((day, secs));
                }
            }
        }
    }

    let mut manual_total = 0i64;
    let mut latest_opt: Option<Date> = all_seconds.iter().map(|(d, _)| *d).max();
    let mut total_secs = 0i64;
    let mut added_dates: Vec<Date> = Vec::with_capacity(games.len());

    for &game in &games {
        if let Some(ms_arr) = game.get("manual_sessions").and_then(Value::as_array) {
            manual_total += ms_arr.len() as i64;
            if !ms_arr.is_empty() {
                let gid = game
                    .get("game_id")
                    .and_then(Value::as_str)
                    .map(str::trim)
                    .unwrap_or("");
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
                    if latest_opt.is_none_or(|cur| day > cur) {
                        latest_opt = Some(day);
                    }
                    if !gid.is_empty() {
                        per_game_events.entry(gid).or_default().push((day, secs));
                    }
                }
            }
        }
        total_secs += total_playtime_seconds(game).max(0);
        if let Some(d) = parse_date_prefix(game.get("last_played"))
            && latest_opt.is_none_or(|cur| d > cur)
        {
            latest_opt = Some(d);
        }
        if let Some(d) = parse_date_prefix(game.get("completed_at"))
            && latest_opt.is_none_or(|cur| d > cur)
        {
            latest_opt = Some(d);
        }
        if let Some(d) = parse_date_prefix(game.get("added_at")) {
            if latest_opt.is_none_or(|cur| d > cur) {
                latest_opt = Some(d);
            }
            added_dates.push(d);
        }
    }

    let ra_entries = load_ra_cache_entries(data_dir);
    let ra_by_gid: HashMap<&str, &Value> = ra_entries
        .iter()
        .map(|e| (e.game_id.as_str(), &e.data))
        .collect();
    let latest_date = latest_opt.unwrap_or(today_val);

    let mut unlocked_pool = UnlockedPool::new(limit, &seen_ids);
    let mut upcoming_pool = UpcomingPool::new();

    // 1. playtime_game
    let has_per_game_events = !per_game_events.is_empty();
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
        let events: &[(Date, i64)] = if has_per_game_events {
            per_game_events.get(gid).map(Vec::as_slice).unwrap_or(&[])
        } else {
            &[]
        };
        let fallback = if secs >= GAME_PLAYTIME_HOURS[0] * 3600 {
            fallback_date_for_game(game, today_val)
        } else {
            today_val
        };
        let mut next_target: Option<i64> = None;
        for &h in GAME_PLAYTIME_HOURS {
            if secs >= h * 3600 {
                let (u_at, approx) = binary_search_crossing(events, h * 3600, fallback);
                unlocked_pool.push_lazy(
                    u_at,
                    "playtime_game",
                    || format!("m:playtime_game:{gid}:{h}h"),
                    || format!("{h}h in {gname}"),
                    || format!("{:.1} hours played", (hours * 10.0).round() / 10.0),
                    hours as i64,
                    h,
                    approx,
                    Some(game),
                );
            } else if next_target.is_none() && hours >= 1.0 {
                next_target = Some(h);
            }
        }
        if let Some(nt) = next_target {
            let pct = ((hours / nt as f64 * 100.0).round() as i64).min(99);
            upcoming_pool.push_lazy(
                pct,
                "playtime_game",
                || format!("m:playtime_game:{gid}:{nt}h"),
                |id| {
                    json!({
                        "id": id,
                        "kind": "playtime_game",
                        "title": format!("{nt}h in {gname}"),
                        "game_id": gid,
                        "current": (hours * 10.0).round() / 10.0,
                        "target": nt,
                        "progress_pct": pct,
                    })
                },
            );
        }
    }

    // 2. playtime_total
    let total_hours = total_secs as f64 / 3600.0;
    let mut next_total: Option<i64> = None;
    for &h in TOTAL_PLAYTIME_HOURS {
        if total_secs >= h * 3600 {
            let (u_at, approx) = binary_search_crossing(&all_seconds, h * 3600, latest_date);
            unlocked_pool.push_lazy(
                u_at,
                "playtime_total",
                || format!("m:playtime_total:{h}h"),
                || format!("{} Library Hours", fmt_commas(h)),
                || {
                    format!(
                        "{:.1} total hours across your collection",
                        (total_hours * 10.0).round() / 10.0
                    )
                },
                total_hours as i64,
                h,
                approx,
                None,
            );
        } else if next_total.is_none() {
            next_total = Some(h);
        }
    }
    if let Some(nt) = next_total {
        let pct = ((total_hours / nt as f64 * 100.0).round() as i64).min(99);
        upcoming_pool.push_lazy(
            pct,
            "playtime_total",
            || format!("m:playtime_total:{nt}h"),
            |id| {
                json!({
                    "id": id,
                    "kind": "playtime_total",
                    "title": format!("{} Library Hours", fmt_commas(nt)),
                    "current": (total_hours * 10.0).round() / 10.0,
                    "target": nt,
                    "progress_pct": pct,
                })
            },
        );
    }

    // 3. first_clear
    let has_ra = !ra_by_gid.is_empty();
    for &game in &games {
        let gid = game
            .get("game_id")
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or("");
        if gid.is_empty() {
            continue;
        }
        let cleared = is_completed(game)
            || (has_ra
                && ra_by_gid.get(gid).copied().is_some_and(|r| {
                    r.get("mastered").and_then(Value::as_bool).unwrap_or(false)
                        || r.get("beaten").and_then(Value::as_i64).unwrap_or(0) > 0
                }));
        if !cleared {
            continue;
        }
        let (u_at, approx) = if let Some(exp) = parse_date_prefix(game.get("completed_at")) {
            (exp, false)
        } else if has_per_game_events
            && let Some(ev) = per_game_events.get(gid)
            && let Some(max_d) = ev.iter().map(|(d, _)| *d).max()
        {
            (max_d, true)
        } else {
            (fallback_date_for_game(game, today_val), true)
        };
        let gname = game.get("name").and_then(Value::as_str).unwrap_or("Game");
        let progress_str = game
            .get("progress")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .unwrap_or("Completed");
        unlocked_pool.push_lazy(
            u_at,
            "first_clear",
            || format!("m:first_clear:{gid}"),
            || format!("Cleared {gname}"),
            || progress_str.to_owned(),
            1,
            1,
            approx,
            Some(game),
        );
    }

    // 4. sessions
    let total_sessions = history_total + manual_total;
    let mut next_sessions: Option<i64> = None;
    for &count in SESSION_MILESTONES {
        if total_sessions >= count {
            let (u_at, approx) = binary_search_crossing(&all_counts, count, latest_date);
            unlocked_pool.push_lazy(
                u_at,
                "sessions",
                || format!("m:sessions:{count}"),
                || format!("{} Play Sessions", fmt_commas(count)),
                || format!("{} logged sessions", fmt_commas(total_sessions)),
                total_sessions,
                count,
                approx,
                None,
            );
        } else if next_sessions.is_none() {
            next_sessions = Some(count);
        }
    }
    if let Some(ns) = next_sessions {
        let pct = ((total_sessions as f64 / ns as f64 * 100.0).round() as i64).min(99);
        upcoming_pool.push_lazy(
            pct,
            "sessions",
            || format!("m:sessions:{ns}"),
            |id| {
                json!({
                    "id": id,
                    "kind": "sessions",
                    "title": format!("{} Play Sessions", fmt_commas(ns)),
                    "current": total_sessions,
                    "target": ns,
                    "progress_pct": pct,
                })
            },
        );
    }

    // 5 & 6. ra_points & ra_complete
    let ra_available = !ra_entries.is_empty();
    let mut ra_events: Vec<(Date, i64)> = Vec::new();
    let mut total_ra_pts = 0i64;
    let mut latest_ra_mtime: Option<Date> = None;
    if ra_available {
        let game_by_id: HashMap<&str, &Value> = games
            .iter()
            .filter_map(|&g| {
                let gid = g.get("game_id").and_then(Value::as_str)?;
                if gid.is_empty() { None } else { Some((gid, g)) }
            })
            .collect();
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
                    if !ach.is_object()
                        || !ach.get("earned").and_then(Value::as_bool).unwrap_or(false)
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
                let game = game_by_id.get(entry.game_id.as_str()).copied();
                let gname = game
                    .and_then(|g| g.get("name").and_then(Value::as_str))
                    .filter(|s| !s.is_empty())
                    .unwrap_or(&entry.game_id);
                let (u_at, approx) = if let Some(max_ed) = earned_dates.into_iter().max() {
                    (max_ed, false)
                } else {
                    (file_date, true)
                };
                unlocked_pool.push_lazy(
                    u_at,
                    "ra_complete",
                    || format!("m:ra_complete:{}", entry.game_id),
                    || format!("Mastered {gname}"),
                    || format!("{earned_ach}/{total_ach} RetroAchievements"),
                    earned_ach,
                    total_ach,
                    approx,
                    game,
                );
            }
        }
    }
    if ra_available && total_ra_pts > 0 {
        let fallback_ra = latest_ra_mtime.unwrap_or(today_val);
        let mut next_pts: Option<i64> = None;
        for &pts_target in RA_POINTS_MILESTONES {
            if total_ra_pts >= pts_target {
                let (u_at, approx) = binary_search_crossing(&ra_events, pts_target, fallback_ra);
                unlocked_pool.push_lazy(
                    u_at,
                    "ra_points",
                    || format!("m:ra_points:{pts_target}"),
                    || format!("{} RetroAchievements Points", fmt_commas(pts_target)),
                    || format!("{} total points earned", fmt_commas(total_ra_pts)),
                    total_ra_pts,
                    pts_target,
                    approx,
                    None,
                );
            } else if next_pts.is_none() {
                next_pts = Some(pts_target);
            }
        }
        if let Some(np) = next_pts {
            let pct = ((total_ra_pts as f64 / np as f64 * 100.0).round() as i64).min(99);
            upcoming_pool.push_lazy(
                pct,
                "ra_points",
                || format!("m:ra_points:{np}"),
                |id| {
                    json!({
                        "id": id,
                        "kind": "ra_points",
                        "title": format!("{} RetroAchievements Points", fmt_commas(np)),
                        "current": total_ra_pts,
                        "target": np,
                        "progress_pct": pct,
                    })
                },
            );
        }
    }

    // 7. streak
    let (longest_streak, streak_crossed) = compute_full_history_streak(&all_counts, today_val);
    let mut next_streak: Option<i64> = None;
    for &days_target in STREAK_MILESTONES {
        if longest_streak >= days_target
            && let Some(&dt_val) = streak_crossed.get(&days_target)
        {
            unlocked_pool.push_lazy(
                dt_val,
                "streak",
                || format!("m:streak:{days_target}d"),
                || format!("{days_target}-Day Play Streak"),
                || format!("Longest streak: {longest_streak} consecutive days"),
                longest_streak,
                days_target,
                false,
                None,
            );
        } else if next_streak.is_none() {
            next_streak = Some(days_target);
        }
    }
    if let Some(ns) = next_streak {
        let pct = ((longest_streak as f64 / ns as f64 * 100.0).round() as i64).min(99);
        upcoming_pool.push_lazy(
            pct,
            "streak",
            || format!("m:streak:{ns}d"),
            |id| {
                json!({
                    "id": id,
                    "kind": "streak",
                    "title": format!("{ns}-Day Play Streak"),
                    "current": longest_streak,
                    "target": ns,
                    "progress_pct": pct,
                })
            },
        );
    }

    // 8. library_age
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
                unlocked_pool.push_lazy(
                    ann,
                    "library_age",
                    || format!("m:library_age:{years_target}y"),
                    || format!("{years_target}-Year Library Anniversary"),
                    || format!("Collecting since {anchor}"),
                    years_target,
                    years_target,
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
            let pct = ((elapsed_days as f64 / target_days as f64 * 100.0).round() as i64).min(99);
            upcoming_pool.push_lazy(
                pct,
                "library_age",
                || format!("m:library_age:{na}y"),
                |id| {
                    json!({
                        "id": id,
                        "kind": "library_age",
                        "title": format!("{na}-Year Library Anniversary"),
                        "current": elapsed_days,
                        "target": target_days,
                        "progress_pct": pct,
                    })
                },
            );
        }
    }

    // 9. collection
    let collection_count = games.len() as i64;
    added_dates.sort_unstable();
    let mut next_coll: Option<i64> = None;
    for &size_target in COLLECTION_MILESTONES {
        if collection_count >= size_target {
            let (u_at, approx) = if added_dates.len() >= size_target as usize {
                (added_dates[size_target as usize - 1], false)
            } else {
                (added_dates.last().copied().unwrap_or(latest_date), true)
            };
            unlocked_pool.push_lazy(
                u_at,
                "collection",
                || format!("m:collection:{size_target}"),
                || format!("{size_target} Games Collected"),
                || format!("{collection_count} active games in library"),
                collection_count,
                size_target,
                approx,
                None,
            );
        } else if next_coll.is_none() {
            next_coll = Some(size_target);
        }
    }
    if let Some(nc) = next_coll {
        let pct = ((collection_count as f64 / nc as f64 * 100.0).round() as i64).min(99);
        upcoming_pool.push_lazy(
            pct,
            "collection",
            || format!("m:collection:{nc}"),
            |id| {
                json!({
                    "id": id,
                    "kind": "collection",
                    "title": format!("{nc} Games Collected"),
                    "current": collection_count,
                    "target": nc,
                    "progress_pct": pct,
                })
            },
        );
    }

    let (unlocked, total_unlocked, new_count, by_kind) = unlocked_pool.into_sorted();
    let upcoming = upcoming_pool.into_sorted();

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
