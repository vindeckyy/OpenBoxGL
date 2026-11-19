//! Bounded, local-first household records and shared-folder transport (`pkg/parity/parity_household.py`).

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;

use jiff::Timestamp;
use openbox_io::atomic_write_text;
use openbox_platform::file_lock;
use serde_json::{Map, Value, json};
use uuid::Uuid;

use crate::library_sync::{
    SyncError, canonical_json, clean_id, sha256_canonical, utc_now_micros, valid_timestamp,
};
use crate::saves::{expand_user_path, set_mode};

pub const HOUSEHOLD_FORMAT: i64 = 1;
pub const HOUSEHOLD_STATE_KEY: &str = "household";
pub const HOUSEHOLD_SYNC_DIRECTORY: &str = "openbox-household-v1";
pub const HOUSEHOLD_EVENT_DIRECTORY: &str = "events";
pub const HOUSEHOLD_LOCK_FILENAME: &str = ".household.lock";

pub const RECORD_KINDS: &[&str] = &["member", "challenge", "challenge_result", "share"];
pub const MAX_RECORD_BYTES: usize = 128 * 1024;
pub const MAX_RECORDS: usize = 10_000;
pub const MAX_PARENTS: usize = 256;
pub const MAX_MEMBER_NAME: usize = 80;
pub const MAX_AVATAR_COLOR: usize = 48;
pub const MAX_CHALLENGE_TITLE: usize = 160;
pub const MAX_CHALLENGE_DESCRIPTION: usize = 2_000;
pub const MAX_PARTICIPANTS: usize = 128;
pub const MAX_RESULT_NOTE: usize = 500;
pub const MAX_STATS_FIELDS: usize = 32;
pub const MAX_STAT_VALUE: i64 = 2_000_000_000;
pub const MAX_CHALLENGE_TARGET: i64 = 2_000_000_000;
pub const MAX_SHARE_ID: usize = 256;

const METRICS: &[&str] = &[
    "completions",
    "finished",
    "playtime_seconds",
    "ra_count",
    "achievements",
    "wins",
];
const CHALLENGE_STATUSES: &[&str] = &["open", "active", "closed", "cancelled", "completed"];
const LOCAL_RESULT_STATUSES: &[&str] = &["open", "active"];
const SHARE_PERIODS: &[&str] = &["weekly", "all_time", "monthly", "daily"];
const SHARE_STAT_FIELDS: &[&str] = &[
    "playtime_seconds",
    "completions",
    "ra_count",
    "games_played",
    "sessions",
    "wins",
];
const COMPLETION_MARKERS: &[&str] = &["beaten", "completed", "mastered", "finished"];

const RECORD_FIELDS: &[&str] = &[
    "format",
    "event_id",
    "device_id",
    "sequence",
    "created_at",
    "sync_key",
    "kind",
    "parents",
    "payload",
    "tombstone",
];
const MEMBER_FIELDS: &[&str] = &["member_id", "display_name", "avatar_color", "stats_shared"];
const CHALLENGE_FIELDS: &[&str] = &[
    "challenge_id",
    "title",
    "description",
    "game_id",
    "metric",
    "target",
    "deadline",
    "created_by",
    "participant_ids",
    "status",
];
const CHALLENGE_RESULT_FIELDS: &[&str] = &[
    "challenge_id",
    "member_id",
    "value",
    "completed",
    "completed_at",
    "game_id",
    "note",
];
const SHARE_FIELDS: &[&str] = &[
    "share_id",
    "member_id",
    "period",
    "stats",
    "window_start",
    "window_end",
];

fn is_hex64(s: &str) -> bool {
    s.len() == 64 && s.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

fn check_text(
    val: Option<&Value>,
    label: &str,
    max: usize,
    required: bool,
) -> Result<String, SyncError> {
    let Some(s) = val.and_then(Value::as_str) else {
        return Err(SyncError::Validation(format!(
            "Household {label} must be text."
        )));
    };
    let trimmed = s.trim();
    if trimmed.is_empty() && required {
        return Err(SyncError::Validation(format!(
            "Household {label} is required."
        )));
    }
    if trimmed.chars().count() > max || trimmed.chars().any(|c| (c as u32) < 32) {
        return Err(SyncError::Validation(format!(
            "Household {label} is invalid."
        )));
    }
    Ok(trimmed.to_owned())
}

fn check_number(
    val: Option<&Value>,
    label: &str,
    max: i64,
    positive: bool,
) -> Result<i64, SyncError> {
    if val.is_some_and(Value::is_boolean) {
        return Err(SyncError::Validation(format!(
            "Household {label} must be an integer."
        )));
    }
    let Some(n) = val.and_then(Value::as_i64) else {
        return Err(SyncError::Validation(format!(
            "Household {label} must be an integer."
        )));
    };
    let lower = if positive { 1 } else { 0 };
    if n < lower || n > max {
        return Err(SyncError::Validation(format!(
            "Household {label} is outside its bound."
        )));
    }
    Ok(n)
}

fn check_timestamp(val: Option<&Value>, label: &str) -> Result<String, SyncError> {
    if !valid_timestamp(val) {
        return Err(SyncError::Validation(format!(
            "Household {label} timestamp is invalid."
        )));
    }
    Ok(val.unwrap().as_str().unwrap().to_owned())
}

fn parse_dt_opt(raw: &str) -> Option<Timestamp> {
    let text = raw.trim();
    if text.is_empty() {
        return None;
    }
    if let Ok(ts) = text.parse::<Timestamp>() {
        return Some(ts);
    }
    if text.len() == 10
        && text.as_bytes()[4] == b'-'
        && text.as_bytes()[7] == b'-'
        && let Ok(d) = text.parse::<jiff::civil::Date>()
    {
        return jiff::tz::TimeZone::UTC.to_timestamp(d.at(0, 0, 0, 0)).ok();
    }
    if let Ok(dt) = text.parse::<jiff::civil::DateTime>() {
        return jiff::tz::TimeZone::UTC.to_timestamp(dt).ok();
    }
    None
}

fn timestamp_key(raw: &str) -> Timestamp {
    parse_dt_opt(raw).unwrap_or(Timestamp::MIN)
}

fn check_period_bound(val: Option<&Value>, label: &str) -> Result<String, SyncError> {
    if let Some(s) = val.and_then(Value::as_str) {
        let t = s.trim();
        if t.len() == 10 && t.parse::<jiff::civil::Date>().is_ok() {
            return Ok(t.to_owned());
        }
    }
    check_timestamp(val, label)
}

pub fn normalise_share_period(raw: &str) -> Result<String, SyncError> {
    let t = check_text(
        Some(&Value::String(raw.to_owned())),
        "share period",
        20,
        true,
    )?;
    let p = t.to_lowercase().replace('-', "_");
    if !SHARE_PERIODS.contains(&p.as_str()) {
        return Err(SyncError::Validation(
            "Household share period is unsupported.".to_owned(),
        ));
    }
    Ok(p)
}

fn namespace_key(sync_key: &str, kind: &str) -> Result<String, SyncError> {
    let v = clean_id(Some(&Value::String(sync_key.to_owned())), "sync_key")?;
    let prefix = format!("{kind}:");
    if !v.starts_with(&prefix) || v.len() == prefix.len() {
        return Err(SyncError::Validation(
            "Household sync key has the wrong namespace.".to_owned(),
        ));
    }
    Ok(v)
}

fn validate_stats(val: &Value) -> Result<Map<String, Value>, SyncError> {
    let Some(obj) = val.as_object() else {
        return Err(SyncError::Validation(
            "Household shared stats must be a bounded object.".to_owned(),
        ));
    };
    if obj.len() > MAX_STATS_FIELDS {
        return Err(SyncError::Validation(
            "Household shared stats must be a bounded object.".to_owned(),
        ));
    }
    let mut out = Map::new();
    for (k, v) in obj {
        if !SHARE_STAT_FIELDS.contains(&k.as_str()) {
            return Err(SyncError::Validation(
                "Household shared stats contain a private or unknown field.".to_owned(),
            ));
        }
        let n = check_number(Some(v), &format!("shared stat {k}"), MAX_STAT_VALUE, false)?;
        out.insert(k.clone(), json!(n));
    }
    Ok(out)
}

fn validate_payload(kind: &str, payload: &Value, sync_key: &str) -> Result<Value, SyncError> {
    let Some(obj) = payload.as_object() else {
        return Err(SyncError::Validation(
            "Household record payload must be an object.".to_owned(),
        ));
    };
    match kind {
        "member" => {
            if obj.keys().any(|k| !MEMBER_FIELDS.contains(&k.as_str()))
                || !obj.contains_key("member_id")
                || !obj.contains_key("display_name")
                || !obj.contains_key("avatar_color")
            {
                return Err(SyncError::Validation(
                    "Household member payload is invalid.".to_owned(),
                ));
            }
            let mid = clean_id(obj.get("member_id"), "member_id")?;
            if namespace_key(sync_key, kind)? != format!("member:{mid}") {
                return Err(SyncError::Validation(
                    "Household member identity does not match its key.".to_owned(),
                ));
            }
            check_text(
                obj.get("display_name"),
                "display_name",
                MAX_MEMBER_NAME,
                true,
            )?;
            check_text(
                obj.get("avatar_color"),
                "avatar_color",
                MAX_AVATAR_COLOR,
                true,
            )?;
            if let Some(ss) = obj.get("stats_shared")
                && !ss.is_boolean()
            {
                return Err(SyncError::Validation(
                    "Household stats_shared must be boolean.".to_owned(),
                ));
            }
            Ok(payload.clone())
        }
        "challenge" => {
            if obj.keys().any(|k| !CHALLENGE_FIELDS.contains(&k.as_str())) {
                return Err(SyncError::Validation(
                    "Household challenge payload is invalid.".to_owned(),
                ));
            }
            for req in ["challenge_id", "title", "metric", "target", "created_by"] {
                if !obj.contains_key(req) {
                    return Err(SyncError::Validation(
                        "Household challenge payload is invalid.".to_owned(),
                    ));
                }
            }
            let cid = clean_id(obj.get("challenge_id"), "challenge_id")?;
            if namespace_key(sync_key, kind)? != format!("challenge:{cid}") {
                return Err(SyncError::Validation(
                    "Household challenge identity does not match its key.".to_owned(),
                ));
            }
            check_text(
                obj.get("title"),
                "challenge title",
                MAX_CHALLENGE_TITLE,
                true,
            )?;
            if let Some(d) = obj.get("description") {
                check_text(
                    Some(d),
                    "challenge description",
                    MAX_CHALLENGE_DESCRIPTION,
                    false,
                )?;
            }
            if let Some(g) = obj.get("game_id") {
                clean_id(Some(g), "game_id")?;
            }
            let metric = check_text(obj.get("metric"), "challenge metric", 40, true)?;
            if !METRICS.contains(&metric.as_str()) {
                return Err(SyncError::Validation(
                    "Household challenge metric is unsupported.".to_owned(),
                ));
            }
            check_number(
                obj.get("target"),
                "challenge target",
                MAX_CHALLENGE_TARGET,
                true,
            )?;
            if let Some(dl) = obj.get("deadline") {
                check_timestamp(Some(dl), "challenge deadline")?;
            }
            clean_id(obj.get("created_by"), "created_by")?;
            if let Some(pids) = obj.get("participant_ids") {
                let Some(arr) = pids.as_array() else {
                    return Err(SyncError::Validation(
                        "Household challenge participants must be a bounded list.".to_owned(),
                    ));
                };
                if arr.len() > MAX_PARTICIPANTS {
                    return Err(SyncError::Validation(
                        "Household challenge participants must be a bounded list.".to_owned(),
                    ));
                }
                let mut seen = HashSet::new();
                for item in arr {
                    let id = clean_id(Some(item), "challenge participants identifier")?;
                    if !seen.insert(id) {
                        return Err(SyncError::Validation(
                            "Household challenge participants must be unique.".to_owned(),
                        ));
                    }
                }
            }
            if let Some(st) = obj.get("status") {
                let s = check_text(Some(st), "challenge status", 20, true)?;
                if !CHALLENGE_STATUSES.contains(&s.as_str()) {
                    return Err(SyncError::Validation(
                        "Household challenge status is unsupported.".to_owned(),
                    ));
                }
            }
            Ok(payload.clone())
        }
        "challenge_result" => {
            if obj
                .keys()
                .any(|k| !CHALLENGE_RESULT_FIELDS.contains(&k.as_str()))
                || !obj.contains_key("challenge_id")
                || !obj.contains_key("member_id")
                || !obj.contains_key("value")
            {
                return Err(SyncError::Validation(
                    "Household challenge result payload is invalid.".to_owned(),
                ));
            }
            let cid = clean_id(obj.get("challenge_id"), "challenge_id")?;
            let mid = clean_id(obj.get("member_id"), "member_id")?;
            if namespace_key(sync_key, kind)? != format!("challenge_result:{cid}:{mid}") {
                return Err(SyncError::Validation(
                    "Household challenge result identity does not match its key.".to_owned(),
                ));
            }
            check_number(
                obj.get("value"),
                "challenge result value",
                MAX_STAT_VALUE,
                false,
            )?;
            if let Some(c) = obj.get("completed")
                && !c.is_boolean()
            {
                return Err(SyncError::Validation(
                    "Household challenge result completed must be boolean.".to_owned(),
                ));
            }
            if let Some(ca) = obj.get("completed_at")
                && !ca.is_null()
            {
                check_timestamp(Some(ca), "challenge result completion")?;
            }
            if let Some(g) = obj.get("game_id") {
                clean_id(Some(g), "game_id")?;
            }
            if let Some(n) = obj.get("note") {
                check_text(Some(n), "challenge result note", MAX_RESULT_NOTE, false)?;
            }
            Ok(payload.clone())
        }
        "share" => {
            if obj.keys().any(|k| !SHARE_FIELDS.contains(&k.as_str()))
                || !obj.contains_key("share_id")
                || !obj.contains_key("member_id")
                || !obj.contains_key("period")
                || !obj.contains_key("stats")
            {
                return Err(SyncError::Validation(
                    "Household share payload is invalid.".to_owned(),
                ));
            }
            let sid = clean_id(obj.get("share_id"), "share_id")?;
            if sid.len() > MAX_SHARE_ID {
                return Err(SyncError::Validation(
                    "Household share_id is too long.".to_owned(),
                ));
            }
            clean_id(obj.get("member_id"), "member_id")?;
            if namespace_key(sync_key, kind)? != format!("share:{sid}") {
                return Err(SyncError::Validation(
                    "Household share identity does not match its key.".to_owned(),
                ));
            }
            let period_str = obj.get("period").and_then(Value::as_str).unwrap_or("");
            normalise_share_period(period_str)?;
            validate_stats(obj.get("stats").unwrap())?;
            let ws = match obj.get("window_start") {
                Some(v) => Some(check_period_bound(Some(v), "share window start")?),
                None => None,
            };
            let we = match obj.get("window_end") {
                Some(v) => Some(check_period_bound(Some(v), "share window end")?),
                None => None,
            };
            if let (Some(s), Some(e)) = (ws, we)
                && timestamp_key(&s) >= timestamp_key(&e)
            {
                return Err(SyncError::Validation(
                    "Household share window is invalid.".to_owned(),
                ));
            }
            Ok(payload.clone())
        }
        _ => Err(SyncError::Validation(
            "Household record kind is unsupported.".to_owned(),
        )),
    }
}

pub fn compute_record_id(record: &Map<String, Value>) -> String {
    let mut without = Map::new();
    for (k, v) in record {
        if k != "event_id" && k != "record_id" {
            without.insert(k.clone(), v.clone());
        }
    }
    sha256_canonical(&Value::Object(without))
}

#[allow(clippy::too_many_arguments)]
pub fn make_record(
    kind: &str,
    device_id: &str,
    sequence: i64,
    sync_key: &str,
    payload: Option<&Value>,
    parents: &[String],
    tombstone: bool,
    created_at: Option<&str>,
) -> Result<Value, SyncError> {
    let kind_clean = check_text(
        Some(&Value::String(kind.to_owned())),
        "record kind",
        32,
        true,
    )?;
    if !RECORD_KINDS.contains(&kind_clean.as_str()) {
        return Err(SyncError::Validation(
            "Household record kind is unsupported.".to_owned(),
        ));
    }
    let dev = clean_id(Some(&Value::String(device_id.to_owned())), "device_id")?;
    if sequence < 1 {
        return Err(SyncError::Validation(
            "Household record sequence must be positive.".to_owned(),
        ));
    }
    let skey = namespace_key(sync_key, &kind_clean)?;
    if parents.len() > MAX_PARENTS {
        return Err(SyncError::Validation(
            "Household record parents is too large.".to_owned(),
        ));
    }
    let mut pids = Vec::with_capacity(parents.len());
    let mut seen = HashSet::new();
    for p in parents {
        let cp = clean_id(Some(&Value::String(p.clone())), "parent record ID")?;
        if !seen.insert(cp.clone()) {
            return Err(SyncError::Validation(
                "Household record parents must be unique.".to_owned(),
            ));
        }
        pids.push(cp);
    }
    pids.sort();
    let when = match created_at {
        Some(c) if !c.is_empty() => check_timestamp(Some(&Value::String(c.to_owned())), "record")?,
        _ => utc_now_micros(),
    };
    if tombstone && payload.is_some() {
        return Err(SyncError::Validation(
            "A household tombstone cannot contain a payload.".to_owned(),
        ));
    }
    if !tombstone && payload.is_none() {
        return Err(SyncError::Validation(
            "A household record needs a payload or tombstone.".to_owned(),
        ));
    }
    let mut rec = Map::new();
    rec.insert("format".into(), json!(HOUSEHOLD_FORMAT));
    rec.insert("device_id".into(), Value::String(dev));
    rec.insert("sequence".into(), json!(sequence));
    rec.insert("created_at".into(), Value::String(when));
    rec.insert("sync_key".into(), Value::String(skey.clone()));
    rec.insert("kind".into(), Value::String(kind_clean.clone()));
    rec.insert(
        "parents".into(),
        Value::Array(pids.into_iter().map(Value::String).collect()),
    );
    if tombstone {
        rec.insert("tombstone".into(), Value::Bool(true));
    } else {
        let p = validate_payload(&kind_clean, payload.unwrap(), &skey)?;
        rec.insert("payload".into(), p);
    }
    let eid = compute_record_id(&rec);
    rec.insert("event_id".into(), Value::String(eid));
    validate_record(&Value::Object(rec))
}

pub fn validate_record(record: &Value) -> Result<Value, SyncError> {
    let Some(raw) = record.as_object() else {
        return Err(SyncError::Validation(
            "Household record must be an object.".to_owned(),
        ));
    };
    let mut checked = raw.clone();
    if let Some(rid) = checked.remove("record_id") {
        if let Some(eid) = checked.get("event_id")
            && *eid != rid
        {
            return Err(SyncError::Validation(
                "Household record has conflicting identities.".to_owned(),
            ));
        }
        checked.insert("event_id".into(), rid);
    }
    if let Some(rk) = checked.remove("record_key") {
        if let Some(sk) = checked.get("sync_key")
            && *sk != rk
        {
            return Err(SyncError::Validation(
                "Household record has conflicting sync keys.".to_owned(),
            ));
        }
        checked.insert("sync_key".into(), rk);
    }
    if checked.keys().any(|k| !RECORD_FIELDS.contains(&k.as_str())) {
        return Err(SyncError::Validation(
            "Household record shape is invalid.".to_owned(),
        ));
    }
    for req in [
        "format",
        "event_id",
        "device_id",
        "sequence",
        "created_at",
        "sync_key",
        "kind",
        "parents",
    ] {
        if !checked.contains_key(req) {
            return Err(SyncError::Validation(
                "Household record has missing or unexpected fields.".to_owned(),
            ));
        }
    }
    if checked.get("format").and_then(Value::as_i64) != Some(HOUSEHOLD_FORMAT) {
        return Err(SyncError::Validation(
            "Unsupported household record format.".to_owned(),
        ));
    }
    let actual_id = clean_id(checked.get("event_id"), "record_id")?;
    if !is_hex64(&actual_id) {
        return Err(SyncError::Validation(
            "Household record ID is invalid.".to_owned(),
        ));
    }
    if compute_record_id(&checked) != actual_id {
        return Err(SyncError::Validation(
            "Household record content hash does not match its ID.".to_owned(),
        ));
    }
    clean_id(checked.get("device_id"), "device_id")?;
    if checked.get("sequence").is_some_and(Value::is_boolean)
        || checked
            .get("sequence")
            .and_then(Value::as_i64)
            .is_none_or(|n| n < 1)
    {
        return Err(SyncError::Validation(
            "Household record sequence is invalid.".to_owned(),
        ));
    }
    check_timestamp(checked.get("created_at"), "record")?;
    let kind = checked
        .get("kind")
        .and_then(Value::as_str)
        .filter(|k| RECORD_KINDS.contains(k))
        .ok_or_else(|| SyncError::Validation("Household record kind is unsupported.".to_owned()))?
        .to_owned();
    let skey = namespace_key(
        checked
            .get("sync_key")
            .and_then(Value::as_str)
            .unwrap_or(""),
        &kind,
    )?;
    let Some(parents) = checked.get("parents").and_then(Value::as_array) else {
        return Err(SyncError::Validation(
            "Household record parents are invalid.".to_owned(),
        ));
    };
    if parents.len() > MAX_PARENTS {
        return Err(SyncError::Validation(
            "Household record parents are invalid.".to_owned(),
        ));
    }
    let mut p_strs = Vec::with_capacity(parents.len());
    for p in parents {
        let pid = clean_id(Some(p), "parent record ID")?;
        if !is_hex64(&pid) {
            return Err(SyncError::Validation(
                "Household parent record ID is invalid.".to_owned(),
            ));
        }
        p_strs.push(pid);
    }
    let mut sp = p_strs.clone();
    sp.sort();
    sp.dedup();
    if sp != p_strs {
        return Err(SyncError::Validation(
            "Household record parents must be unique and sorted.".to_owned(),
        ));
    }
    let has_payload = checked.contains_key("payload");
    let has_tomb = checked.contains_key("tombstone");
    if has_payload == has_tomb || (has_tomb && checked.get("tombstone") != Some(&Value::Bool(true)))
    {
        return Err(SyncError::Validation(
            "Household record needs exactly one payload or tombstone.".to_owned(),
        ));
    }
    if has_payload {
        validate_payload(&kind, checked.get("payload").unwrap(), &skey)?;
    }
    let val = Value::Object(checked);
    if canonical_json(&val).len() > MAX_RECORD_BYTES {
        return Err(SyncError::Validation(
            "Household record is too large.".to_owned(),
        ));
    }
    Ok(val)
}

pub fn validate_record_set(
    records: &[Value],
    known_records: &[Value],
) -> Result<Vec<Value>, SyncError> {
    if records.len() + known_records.len() > MAX_RECORDS {
        return Err(SyncError::Validation(
            "Too many household records.".to_owned(),
        ));
    }
    let mut known: HashMap<String, Value> = HashMap::new();
    for item in known_records {
        let checked = validate_record(item)?;
        let id = checked["event_id"].as_str().unwrap_or("").to_owned();
        if let Some(prev) = known.get(&id)
            && *prev != checked
        {
            return Err(SyncError::Validation(
                "Duplicate household record identity.".to_owned(),
            ));
        }
        known.insert(id, checked);
    }
    let mut order = Vec::new();
    let mut result: HashMap<String, Value> = HashMap::new();
    for item in records {
        let checked = validate_record(item)?;
        let id = checked["event_id"].as_str().unwrap_or("").to_owned();
        if result.contains_key(&id) {
            return Err(SyncError::Validation(
                "Duplicate household record identity.".to_owned(),
            ));
        }
        if let Some(prev) = known.get(&id) {
            if *prev != checked {
                return Err(SyncError::Validation(
                    "Duplicate household record identity.".to_owned(),
                ));
            }
            continue;
        }
        order.push(id.clone());
        result.insert(id, checked);
    }
    let mut combined = known;
    for (k, v) in &result {
        combined.insert(k.clone(), v.clone());
    }
    for checked in result.values() {
        let sk = checked["sync_key"].as_str().unwrap_or("");
        if let Some(ps) = checked["parents"].as_array() {
            for p in ps {
                let pid = p.as_str().unwrap_or("");
                let Some(parent) = combined.get(pid) else {
                    return Err(SyncError::Validation(
                        "Household record DAG has a missing parent.".to_owned(),
                    ));
                };
                if parent["sync_key"].as_str().unwrap_or("") != sk {
                    return Err(SyncError::Validation(
                        "Household parent belongs to another record key.".to_owned(),
                    ));
                }
            }
        }
    }

    let mut visiting = HashSet::new();
    let mut visited = HashSet::new();
    fn visit(
        id: &str,
        combined: &HashMap<String, Value>,
        visiting: &mut HashSet<String>,
        visited: &mut HashSet<String>,
    ) -> Result<(), SyncError> {
        if visiting.contains(id) {
            return Err(SyncError::Validation(
                "Household record DAG contains a cycle.".to_owned(),
            ));
        }
        if visited.contains(id) {
            return Ok(());
        }
        visiting.insert(id.to_owned());
        if let Some(r) = combined.get(id)
            && let Some(ps) = r["parents"].as_array()
        {
            for p in ps.iter().filter_map(Value::as_str) {
                visit(p, combined, visiting, visited)?;
            }
        }
        visiting.remove(id);
        visited.insert(id.to_owned());
        Ok(())
    }
    for id in combined.keys() {
        visit(id, &combined, &mut visiting, &mut visited)?;
    }
    Ok(order
        .into_iter()
        .filter_map(|id| result.remove(&id))
        .collect())
}

fn ancestors(ident: &str, records: &HashMap<String, Value>) -> HashSet<String> {
    let mut out = HashSet::new();
    let mut todo: Vec<String> = records
        .get(ident)
        .and_then(|r| r.get("parents"))
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default();
    while let Some(cur) = todo.pop() {
        if !out.insert(cur.clone()) {
            continue;
        }
        if let Some(r) = records.get(&cur)
            && let Some(ps) = r.get("parents").and_then(Value::as_array)
        {
            for p in ps.iter().filter_map(Value::as_str) {
                todo.push(p.to_owned());
            }
        }
    }
    out
}

fn is_ancestor(older: &str, newer: &str, records: &HashMap<String, Value>) -> bool {
    older == newer || ancestors(newer, records).contains(older)
}

fn record_order_key(rec: &Value) -> (i64, i32, i64, String, String) {
    let ts = timestamp_key(rec.get("created_at").and_then(Value::as_str).unwrap_or(""));
    (
        ts.as_second(),
        ts.subsec_nanosecond(),
        rec.get("sequence").and_then(Value::as_i64).unwrap_or(0),
        rec.get("device_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned(),
        rec.get("event_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned(),
    )
}

fn compute_heads(records: &HashMap<String, Value>) -> BTreeMap<String, Vec<String>> {
    let mut by_key: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for (id, rec) in records {
        let sk = rec
            .get("sync_key")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        by_key.entry(sk).or_default().push(id.clone());
    }
    let mut out = BTreeMap::new();
    for (k, ids) in by_key {
        let mut heads: Vec<String> = ids
            .iter()
            .filter(|id| !ids.iter().any(|o| o != *id && is_ancestor(id, o, records)))
            .cloned()
            .collect();
        heads.sort();
        out.insert(k, heads);
    }
    out
}

pub fn converge_records(
    records: &[Value],
    known_records: &[Value],
) -> Result<Vec<Value>, SyncError> {
    let incoming = validate_record_set(records, known_records)?;
    let mut combined: HashMap<String, Value> = HashMap::new();
    for item in known_records {
        let checked = validate_record(item)?;
        combined.insert(
            checked["event_id"].as_str().unwrap_or("").to_owned(),
            checked,
        );
    }
    for item in incoming {
        combined.insert(item["event_id"].as_str().unwrap_or("").to_owned(), item);
    }
    let mut selected = Vec::new();
    for ids in compute_heads(&combined).values() {
        if let Some(best) = ids
            .iter()
            .filter_map(|id| combined.get(id))
            .max_by_key(|r| record_order_key(r))
        {
            selected.push(best.clone());
        }
    }
    selected.sort_by(|a, b| {
        let ka = (
            a.get("sync_key")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned(),
            record_order_key(a),
        );
        let kb = (
            b.get("sync_key")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned(),
            record_order_key(b),
        );
        ka.cmp(&kb)
    });
    Ok(selected)
}

pub fn stats_sharing_enabled(state: &Value) -> bool {
    let Some(obj) = state.as_object() else {
        return false;
    };
    let mut candidates = vec![obj];
    if let Some(s) = obj.get("settings").and_then(Value::as_object) {
        candidates.push(s);
    }
    if let Some(h) = obj.get(HOUSEHOLD_STATE_KEY).and_then(Value::as_object) {
        candidates.push(h);
    }
    for cand in candidates {
        for k in [
            "household_stats_sharing",
            "household_stats_enabled",
            "household_share_stats",
            "stats_sharing_enabled",
            "share_stats",
            "share_household_stats",
        ] {
            if cand.get(k) == Some(&Value::Bool(true)) {
                return true;
            }
        }
    }
    false
}

fn stats_window(
    period: &str,
    now_raw: Option<&str>,
) -> Result<(String, Option<Timestamp>, Timestamp), SyncError> {
    let norm = normalise_share_period(period)?;
    let date_only = now_raw.is_some_and(|s| s.trim().len() == 10);
    let end = match now_raw {
        None => Timestamp::now(),
        Some(s) => {
            let parsed = parse_dt_opt(s).ok_or_else(|| {
                SyncError::Validation("Household stats window timestamp is invalid.".to_owned())
            })?;
            if date_only {
                Timestamp::from_second(parsed.as_second() + 86400).unwrap_or(parsed)
            } else {
                parsed
            }
        }
    };
    if norm == "all_time" {
        return Ok((norm, None, end));
    }
    let end_dt = jiff::tz::TimeZone::UTC.to_datetime(end);
    let cal_date = if date_only {
        end_dt.date().yesterday().unwrap_or(end_dt.date())
    } else {
        end_dt.date()
    };
    let start_date = match norm.as_str() {
        "daily" => cal_date,
        "weekly" => {
            let days_since_monday = cal_date.weekday().to_monday_zero_offset() as i64;
            let base = jiff::tz::TimeZone::UTC
                .to_timestamp(cal_date.at(0, 0, 0, 0))
                .unwrap_or(end);
            let shifted = Timestamp::from_second(base.as_second() - days_since_monday * 86400)
                .unwrap_or(base);
            jiff::tz::TimeZone::UTC.to_datetime(shifted).date()
        }
        _ => cal_date.with().day(1).build().unwrap_or(cal_date),
    };
    let start = jiff::tz::TimeZone::UTC
        .to_timestamp(start_date.at(0, 0, 0, 0))
        .unwrap_or(end);
    Ok((norm, Some(start), end))
}

fn completion_marker(val: Option<&Value>) -> bool {
    let s = val.and_then(Value::as_str).unwrap_or("").to_lowercase();
    COMPLETION_MARKERS.iter().any(|m| s.contains(m))
}

fn stat_i64(val: Option<&Value>) -> i64 {
    match val {
        Some(Value::Number(n)) => n
            .as_i64()
            .or_else(|| n.as_f64().map(|f| f as i64))
            .unwrap_or(0)
            .max(0),
        _ => 0,
    }
}

fn activity_timestamp(obj: &Value, fields: &[&str]) -> Option<Timestamp> {
    for f in fields {
        if let Some(s) = obj.get(*f).and_then(Value::as_str)
            && let Some(ts) = parse_dt_opt(s)
        {
            return Some(ts);
        }
    }
    None
}

pub fn collect_stats(
    state: &Value,
    period: &str,
    now_raw: Option<&str>,
) -> Result<Value, SyncError> {
    let games = state
        .get("games")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let history = state
        .get("history")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let (norm, start_opt, end) = stats_window(period, now_raw)?;

    if norm != "all_time" {
        let start = start_opt.unwrap();
        let mut game_rows: HashMap<String, &Value> = HashMap::new();
        let mut game_ids: HashSet<String> = HashSet::new();
        let mut completion_ids: HashSet<String> = HashSet::new();
        let mut ra_ids: HashSet<String> = HashSet::new();
        let mut playtime = 0i64;
        let mut ra_count = 0i64;
        let mut sessions = 0i64;
        let mut wins = 0i64;

        for g in games {
            let gid = g
                .get("game_id")
                .and_then(Value::as_str)
                .map(str::trim)
                .unwrap_or("")
                .to_owned();
            if gid.is_empty() {
                continue;
            }
            let act = activity_timestamp(g, &["last_played", "played_at"]);
            let comp = activity_timestamp(g, &["completed_at", "finished_at", "completion_date"]);
            let ra_at = activity_timestamp(g, &["ra_updated_at", "achievements_updated_at"]);
            if act.is_some_and(|t| t >= start && t < end) {
                game_rows.insert(gid.clone(), g);
                game_ids.insert(gid.clone());
                if completion_marker(g.get("progress")) {
                    completion_ids.insert(gid.clone());
                }
                if ra_at.is_none_or(|t| t >= start && t < end) {
                    ra_ids.insert(gid.clone());
                }
            } else if comp.is_some_and(|t| t >= start && t < end)
                && completion_marker(g.get("progress"))
            {
                completion_ids.insert(gid);
            }
        }
        let mut history_game_ids = HashSet::new();
        let mut history_comp_ids = HashSet::new();
        let mut completions = completion_ids.len() as i64;
        for entry in history {
            let started = activity_timestamp(
                entry,
                &[
                    "started_at",
                    "started",
                    "played_at",
                    "played_on",
                    "timestamp",
                    "created_at",
                ],
            );
            if !started.is_some_and(|t| t >= start && t < end) {
                continue;
            }
            sessions += 1;
            playtime += stat_i64(
                entry
                    .get("seconds")
                    .or_else(|| entry.get("playtime_seconds")),
            );
            let gid = entry
                .get("game_id")
                .or_else(|| entry.get("stable_game_id"))
                .and_then(Value::as_str)
                .map(str::trim)
                .unwrap_or("")
                .to_owned();
            if !gid.is_empty() {
                game_ids.insert(gid.clone());
                history_game_ids.insert(gid.clone());
            }
            let res_s = entry
                .get("result")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_lowercase();
            let won = matches!(res_s.as_str(), "win" | "won" | "complete" | "completed");
            let comp = entry
                .get("completed")
                .and_then(Value::as_bool)
                .unwrap_or(false)
                || completion_marker(entry.get("progress"));
            if won {
                wins += 1;
            }
            if comp || won {
                if !gid.is_empty() {
                    if !completion_ids.contains(&gid) && history_comp_ids.insert(gid) {
                        completions += 1;
                    }
                } else {
                    completions += 1;
                }
            }
            for f in [
                "ra_count_delta",
                "ra_delta",
                "achievements_delta",
                "ra_achievements_delta",
            ] {
                if let Some(v) = entry.get(f) {
                    ra_count += stat_i64(Some(v));
                    break;
                }
            }
        }
        for (gid, g) in &game_rows {
            if !history_game_ids.contains(gid) {
                playtime += stat_i64(g.get("playtime_seconds"));
            }
            if ra_ids.contains(gid) {
                ra_count += stat_i64(
                    g.get("ra_count")
                        .or_else(|| g.get("achievements"))
                        .or_else(|| g.get("ra_achievements")),
                );
            }
        }
        return Ok(json!({
            "playtime_seconds": playtime.min(MAX_STAT_VALUE),
            "completions": completions.min(MAX_STAT_VALUE),
            "ra_count": ra_count.min(MAX_STAT_VALUE),
            "games_played": (game_ids.len() as i64).min(MAX_STAT_VALUE),
            "sessions": sessions.min(MAX_STAT_VALUE),
            "wins": wins.min(MAX_STAT_VALUE),
        }));
    }

    let mut game_ids = HashSet::new();
    let mut playtime = 0i64;
    let mut completions = 0i64;
    let mut ra_count = 0i64;
    let mut games_played = 0i64;
    let mut wins = 0i64;
    for g in games {
        if let Some(gid) = g
            .get("game_id")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
        {
            game_ids.insert(gid.to_owned());
        }
        let secs = stat_i64(g.get("playtime_seconds"));
        playtime += secs;
        if completion_marker(g.get("progress")) {
            completions += 1;
        }
        ra_count += stat_i64(
            g.get("ra_count")
                .or_else(|| g.get("achievements"))
                .or_else(|| g.get("ra_achievements")),
        );
        let plays = stat_i64(g.get("play_count"));
        if plays > 0 || secs > 0 {
            games_played += 1;
        }
    }
    let mut sessions = 0i64;
    for entry in history {
        if !entry.is_object() {
            continue;
        }
        sessions += 1;
        if let Some(gid) = entry
            .get("game_id")
            .or_else(|| entry.get("stable_game_id"))
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
        {
            game_ids.insert(gid.to_owned());
        }
        let r = entry
            .get("result")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_lowercase();
        if matches!(r.as_str(), "win" | "won" | "complete" | "completed") {
            wins += 1;
        }
    }
    if !history.is_empty() {
        games_played = games_played.max(game_ids.len() as i64);
    }
    Ok(json!({
        "playtime_seconds": playtime.min(MAX_STAT_VALUE),
        "completions": completions.min(MAX_STAT_VALUE),
        "ra_count": ra_count.min(MAX_STAT_VALUE),
        "games_played": games_played.min(MAX_STAT_VALUE),
        "sessions": sessions.min(MAX_STAT_VALUE),
        "wins": wins.min(MAX_STAT_VALUE),
    }))
}

type HouseholdBucket = (Map<String, Value>, HashMap<String, Value>);

fn state_bucket(state: &Value) -> Result<HouseholdBucket, SyncError> {
    let Some(root) = state.as_object() else {
        return Err(SyncError::Validation(
            "Household state must be an object.".to_owned(),
        ));
    };
    let Some(raw) = root.get(HOUSEHOLD_STATE_KEY) else {
        return Ok((Map::new(), HashMap::new()));
    };
    let Some(raw_obj) = raw.as_object() else {
        return Err(SyncError::Validation(
            "Household state is invalid.".to_owned(),
        ));
    };
    let values: Vec<Value> = match raw_obj.get("records") {
        None => Vec::new(),
        Some(Value::Object(m)) => {
            let mut out = Vec::with_capacity(m.len());
            for (k, v) in m {
                if v.get("event_id").and_then(Value::as_str) != Some(k.as_str()) {
                    return Err(SyncError::Validation(
                        "Household record map key is invalid.".to_owned(),
                    ));
                }
                out.push(v.clone());
            }
            out
        }
        Some(Value::Array(arr)) => arr.clone(),
        _ => {
            return Err(SyncError::Validation(
                "Household state records are invalid.".to_owned(),
            ));
        }
    };
    let checked = validate_record_set(&values, &[])?;
    if checked.len() != values.len() {
        return Err(SyncError::Validation(
            "Household state contains duplicate records.".to_owned(),
        ));
    }
    let mut records = HashMap::new();
    let mut rec_obj = Map::new();
    for item in checked {
        let id = item["event_id"].as_str().unwrap_or("").to_owned();
        rec_obj.insert(id.clone(), item.clone());
        records.insert(id, item);
    }
    let mut bucket = raw_obj.clone();
    bucket.insert("records".into(), Value::Object(rec_obj));

    let outbox_in = match bucket.get("outbox") {
        None => Vec::new(),
        Some(Value::Array(a)) => a.clone(),
        _ => {
            return Err(SyncError::Validation(
                "Household outbox is invalid.".to_owned(),
            ));
        }
    };
    let mut checked_outbox = Vec::new();
    let mut seen_ob = HashSet::new();
    for item in &outbox_in {
        let c = validate_record(item)?;
        let id = c["event_id"].as_str().unwrap_or("").to_owned();
        if !records.contains_key(&id) {
            return Err(SyncError::Validation(
                "Household outbox record is not in history.".to_owned(),
            ));
        }
        if seen_ob.insert(id) {
            checked_outbox.push(c);
        }
    }
    bucket.insert("outbox".into(), Value::Array(checked_outbox));
    if let Some(dev) = bucket.get("device_id")
        && !dev.is_null()
    {
        clean_id(Some(dev), "device_id")?;
    }
    let seq = bucket
        .get("next_sequence")
        .and_then(Value::as_i64)
        .unwrap_or(1);
    if bucket.get("next_sequence").is_some_and(Value::is_boolean) || seq < 1 {
        return Err(SyncError::Validation(
            "Household next sequence is invalid.".to_owned(),
        ));
    }
    let heads = compute_heads(&records);
    let mut heads_obj = Map::new();
    for (k, v) in heads {
        heads_obj.insert(k, Value::Array(v.into_iter().map(Value::String).collect()));
    }
    bucket.insert("heads".into(), Value::Object(heads_obj));
    Ok((bucket, records))
}

fn device_for(
    state: &Value,
    bucket: &Map<String, Value>,
    device_id: Option<&str>,
) -> Result<String, SyncError> {
    if let Some(d) = device_id.filter(|s| !s.is_empty()) {
        return clean_id(Some(&Value::String(d.to_owned())), "device_id");
    }
    if let Some(d) = bucket
        .get("device_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        return clean_id(Some(&Value::String(d.to_owned())), "device_id");
    }
    if let Some(d) = state
        .get("library_sync")
        .and_then(|m| m.get("device_id"))
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        return clean_id(Some(&Value::String(d.to_owned())), "device_id");
    }
    Ok(Uuid::new_v4().to_string())
}

fn store_record(
    state: &mut Value,
    mut bucket: Map<String, Value>,
    mut records: HashMap<String, Value>,
    record: Value,
    outbox: bool,
) -> Result<Value, SyncError> {
    let checked = validate_record(&record)?;
    let eid = checked["event_id"].as_str().unwrap_or("").to_owned();
    if let Some(existing) = records.get(&eid) {
        if *existing != checked {
            return Err(SyncError::Validation(
                "Household record identity differs from local history.".to_owned(),
            ));
        }
        return Ok(checked);
    }
    records.insert(eid.clone(), checked.clone());
    let mut rec_obj = Map::new();
    for (k, v) in &records {
        rec_obj.insert(k.clone(), v.clone());
    }
    bucket.insert("records".into(), Value::Object(rec_obj));
    let mut heads_obj = Map::new();
    for (k, v) in compute_heads(&records) {
        heads_obj.insert(k, Value::Array(v.into_iter().map(Value::String).collect()));
    }
    bucket.insert("heads".into(), Value::Object(heads_obj));
    let mut pending = bucket
        .get("outbox")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if outbox
        && !pending
            .iter()
            .any(|x| x["event_id"].as_str() == Some(eid.as_str()))
    {
        pending.push(checked.clone());
    }
    bucket.insert("outbox".into(), Value::Array(pending));
    if let Some(root) = state.as_object_mut() {
        root.insert(HOUSEHOLD_STATE_KEY.into(), Value::Object(bucket));
    }
    Ok(checked)
}

fn validate_local_challenge_result(
    records: &HashMap<String, Value>,
    challenge_id: &str,
    member_id: &str,
    game_id: Option<&str>,
    created_at: Option<&str>,
    completed_at: Option<&str>,
) -> Result<(String, Option<String>), SyncError> {
    let result_at = match created_at.filter(|s| !s.is_empty()) {
        Some(c) => check_timestamp(Some(&Value::String(c.to_owned())), "challenge result")?,
        None => utc_now_micros(),
    };
    let comp_val = match completed_at.filter(|s| !s.is_empty()) {
        Some(c) => Some(check_timestamp(
            Some(&Value::String(c.to_owned())),
            "challenge result completion",
        )?),
        None => None,
    };
    let key = format!("challenge:{challenge_id}");
    let all_recs: Vec<Value> = records.values().cloned().collect();
    let converged = converge_records(&all_recs, &[])?;
    let current = converged
        .into_iter()
        .find(|r| r.get("sync_key").and_then(Value::as_str) == Some(key.as_str()));
    let Some(ch_payload) = current.as_ref().and_then(|r| r.get("payload")) else {
        return Err(SyncError::Validation(
            "Household challenge does not exist or was deleted.".to_owned(),
        ));
    };
    let status = ch_payload
        .get("status")
        .and_then(Value::as_str)
        .unwrap_or("open")
        .trim()
        .to_lowercase();
    if !LOCAL_RESULT_STATUSES.contains(&status.as_str()) {
        return Err(SyncError::Validation(
            "Household challenge is not accepting results.".to_owned(),
        ));
    }
    if let Some(parts) = ch_payload.get("participant_ids").and_then(Value::as_array)
        && !parts.is_empty()
        && !parts.iter().any(|p| p.as_str() == Some(member_id))
    {
        return Err(SyncError::Validation(
            "Household result member is not a challenge participant.".to_owned(),
        ));
    }
    if let Some(ch_gid) = ch_payload.get("game_id").and_then(Value::as_str)
        && game_id != Some(ch_gid)
    {
        return Err(SyncError::Validation(
            "Household result game does not match the challenge.".to_owned(),
        ));
    }
    if let Some(dl) = ch_payload.get("deadline").and_then(Value::as_str) {
        let check_at = comp_val.as_deref().unwrap_or(&result_at);
        if timestamp_key(check_at) >= timestamp_key(dl) {
            return Err(SyncError::Validation(
                "Household challenge deadline has passed.".to_owned(),
            ));
        }
    }
    Ok((result_at, comp_val))
}

pub fn record_local(
    state: &mut Value,
    kind: &str,
    payload: Option<&Value>,
    sync_key: Option<&str>,
    device_id: Option<&str>,
    tombstone: bool,
    created_at: Option<&str>,
) -> Result<Value, SyncError> {
    let (mut bucket, records) = state_bucket(state)?;
    let kind_clean = check_text(
        Some(&Value::String(kind.to_owned())),
        "record kind",
        32,
        true,
    )?;
    if !RECORD_KINDS.contains(&kind_clean.as_str()) {
        return Err(SyncError::Validation(
            "Household record kind is unsupported.".to_owned(),
        ));
    }
    let skey = match sync_key {
        Some(s) if !s.is_empty() => namespace_key(s, &kind_clean)?,
        _ => {
            let p = payload.and_then(Value::as_object).ok_or_else(|| {
                SyncError::Validation("Household payload is required to derive its key.".to_owned())
            })?;
            match kind_clean.as_str() {
                "member" => format!("member:{}", clean_id(p.get("member_id"), "member_id")?),
                "challenge" => format!(
                    "challenge:{}",
                    clean_id(p.get("challenge_id"), "challenge_id")?
                ),
                "challenge_result" => format!(
                    "challenge_result:{}:{}",
                    clean_id(p.get("challenge_id"), "challenge_id")?,
                    clean_id(p.get("member_id"), "member_id")?
                ),
                _ => format!("share:{}", clean_id(p.get("share_id"), "share_id")?),
            }
        }
    };
    let dev = device_for(state, &bucket, device_id)?;
    let mut final_created = created_at.map(str::to_owned);
    if kind_clean == "challenge_result" && !tombstone {
        let p = payload.and_then(Value::as_object).ok_or_else(|| {
            SyncError::Validation("Household challenge result payload is required.".to_owned())
        })?;
        let cid = clean_id(p.get("challenge_id"), "challenge_id")?;
        let mid = clean_id(p.get("member_id"), "member_id")?;
        let (res_at, _) = validate_local_challenge_result(
            &records,
            &cid,
            &mid,
            p.get("game_id").and_then(Value::as_str),
            created_at,
            p.get("completed_at").and_then(Value::as_str),
        )?;
        final_created = Some(res_at);
    }
    let seq = bucket
        .get("next_sequence")
        .and_then(Value::as_i64)
        .unwrap_or(1);
    let parents: Vec<String> = bucket
        .get("heads")
        .and_then(|h| h.get(&skey))
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default();
    let rec = make_record(
        &kind_clean,
        &dev,
        seq,
        &skey,
        payload,
        &parents,
        tombstone,
        final_created.as_deref(),
    )?;
    bucket.insert("device_id".into(), Value::String(dev));
    bucket.insert("next_sequence".into(), json!(seq + 1));
    store_record(state, bucket, records, rec, true)
}

pub fn record_member(
    state: &mut Value,
    member_id: &str,
    display_name: &str,
    avatar_color: &str,
    stats_shared: bool,
) -> Result<Value, SyncError> {
    let payload = json!({
        "member_id": member_id,
        "display_name": display_name,
        "avatar_color": if avatar_color.is_empty() { "blue" } else { avatar_color },
        "stats_shared": stats_shared,
    });
    record_local(state, "member", Some(&payload), None, None, false, None)
}

#[allow(clippy::too_many_arguments)]
pub fn record_challenge(
    state: &mut Value,
    challenge_id: &str,
    title: &str,
    created_by: Option<&str>,
    metric: &str,
    target: i64,
    description: &str,
    game_id: Option<&str>,
    deadline: Option<&str>,
    participant_ids: &[String],
    status: &str,
) -> Result<Value, SyncError> {
    let (bucket, _) = state_bucket(state)?;
    let dev = device_for(state, &bucket, None)?;
    let creator = created_by.filter(|s| !s.is_empty()).unwrap_or(&dev);
    let mut payload = Map::new();
    payload.insert(
        "challenge_id".into(),
        Value::String(challenge_id.to_owned()),
    );
    payload.insert("title".into(), Value::String(title.to_owned()));
    payload.insert(
        "metric".into(),
        Value::String(
            if metric.is_empty() {
                "completions"
            } else {
                metric
            }
            .to_owned(),
        ),
    );
    payload.insert("target".into(), json!(target));
    payload.insert("created_by".into(), Value::String(creator.to_owned()));
    payload.insert(
        "status".into(),
        Value::String(if status.is_empty() { "open" } else { status }.to_owned()),
    );
    if !description.is_empty() {
        payload.insert("description".into(), Value::String(description.to_owned()));
    }
    if let Some(g) = game_id.filter(|s| !s.is_empty()) {
        payload.insert("game_id".into(), Value::String(g.to_owned()));
    }
    if let Some(dl) = deadline.filter(|s| !s.is_empty()) {
        payload.insert("deadline".into(), Value::String(dl.to_owned()));
    }
    if !participant_ids.is_empty() {
        let mut sorted = participant_ids.to_vec();
        sorted.sort();
        payload.insert(
            "participant_ids".into(),
            Value::Array(sorted.into_iter().map(Value::String).collect()),
        );
    }
    record_local(
        state,
        "challenge",
        Some(&Value::Object(payload)),
        None,
        Some(&dev),
        false,
        None,
    )
}

#[allow(clippy::too_many_arguments)]
pub fn record_challenge_result(
    state: &mut Value,
    challenge_id: &str,
    member_id: &str,
    value: i64,
    completed: bool,
    completed_at: Option<&str>,
    game_id: Option<&str>,
    note: Option<&str>,
) -> Result<Value, SyncError> {
    let mut payload = Map::new();
    payload.insert(
        "challenge_id".into(),
        Value::String(challenge_id.to_owned()),
    );
    payload.insert("member_id".into(), Value::String(member_id.to_owned()));
    payload.insert("value".into(), json!(value));
    payload.insert("completed".into(), Value::Bool(completed));
    if let Some(ca) = completed_at.filter(|s| !s.is_empty()) {
        payload.insert("completed_at".into(), Value::String(ca.to_owned()));
    }
    if let Some(g) = game_id.filter(|s| !s.is_empty()) {
        payload.insert("game_id".into(), Value::String(g.to_owned()));
    }
    if let Some(n) = note {
        payload.insert("note".into(), Value::String(n.to_owned()));
    }
    record_local(
        state,
        "challenge_result",
        Some(&Value::Object(payload)),
        None,
        None,
        false,
        None,
    )
}

pub fn record_stats_share(
    state: &mut Value,
    member_id: &str,
    period: &str,
    stats_override: Option<&Value>,
    share_id: Option<&str>,
    now_raw: Option<&str>,
) -> Result<Option<Value>, SyncError> {
    if !stats_sharing_enabled(state) {
        return Ok(None);
    }
    let (norm, start_opt, end) = stats_window(period, now_raw)?;
    let values = match stats_override {
        Some(s) => s.clone(),
        None => collect_stats(state, &norm, now_raw)?,
    };
    let mid = clean_id(Some(&Value::String(member_id.to_owned())), "member_id")?;
    let sid = share_id
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .unwrap_or_else(|| format!("{mid}:{norm}"));
    let mut payload = Map::new();
    payload.insert("share_id".into(), Value::String(sid.clone()));
    payload.insert("member_id".into(), Value::String(mid));
    payload.insert("period".into(), Value::String(norm.clone()));
    payload.insert("stats".into(), values);
    if norm != "all_time" && stats_override.is_none() {
        if let Some(s) = start_opt {
            payload.insert("window_start".into(), Value::String(s.to_string()));
        }
        payload.insert("window_end".into(), Value::String(end.to_string()));
    }
    let rec = record_local(
        state,
        "share",
        Some(&Value::Object(payload)),
        Some(&format!("share:{sid}")),
        None,
        false,
        None,
    )?;
    Ok(Some(rec))
}

pub fn household_records(state: &Value, include_tombstones: bool) -> Result<Vec<Value>, SyncError> {
    let (_, records) = state_bucket(state)?;
    let vals: Vec<Value> = records.values().cloned().collect();
    let res = converge_records(&vals, &[])?;
    if include_tombstones {
        Ok(res)
    } else {
        Ok(res
            .into_iter()
            .filter(|r| r.get("payload").is_some())
            .collect())
    }
}

pub fn merge_records(state: &mut Value, incoming: &[Value]) -> Result<Value, SyncError> {
    let (mut bucket, mut records) = state_bucket(state)?;
    let known_vec: Vec<Value> = records.values().cloned().collect();
    let checked = validate_record_set(incoming, &known_vec)?;
    for item in &checked {
        let id = item["event_id"].as_str().unwrap_or("").to_owned();
        records.insert(id, item.clone());
    }
    let mut rec_obj = Map::new();
    for (k, v) in &records {
        rec_obj.insert(k.clone(), v.clone());
    }
    let heads = compute_heads(&records);
    let mut heads_obj = Map::new();
    for (k, v) in heads {
        heads_obj.insert(k, Value::Array(v.into_iter().map(Value::String).collect()));
    }
    bucket.insert("records".into(), Value::Object(rec_obj));
    bucket.insert("heads".into(), Value::Object(heads_obj.clone()));
    if let Some(root) = state.as_object_mut() {
        root.insert(HOUSEHOLD_STATE_KEY.into(), Value::Object(bucket));
    }
    let all_vals: Vec<Value> = records.values().cloned().collect();
    let selected = converge_records(&all_vals, &[])?;
    Ok(json!({
        "records": selected,
        "applied": checked.len(),
        "changed": !checked.is_empty(),
        "heads": Value::Object(heads_obj),
    }))
}

pub fn acknowledge_outbox(state: &mut Value, record_ids: &[String]) -> Result<usize, SyncError> {
    let (mut bucket, _) = state_bucket(state)?;
    if record_ids.len() > MAX_RECORDS {
        return Err(SyncError::Validation(
            "Household acknowledgement IDs is too large.".to_owned(),
        ));
    }
    let mut checked = HashSet::new();
    for id in record_ids {
        let v = clean_id(Some(&Value::String(id.clone())), "record_id")?;
        if !is_hex64(&v) {
            return Err(SyncError::Validation(
                "Household acknowledgement ID is invalid.".to_owned(),
            ));
        }
        checked.insert(v);
    }
    let mut outbox = bucket
        .get("outbox")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let before = outbox.len();
    outbox.retain(|item| {
        item.get("event_id")
            .and_then(Value::as_str)
            .is_none_or(|id| !checked.contains(id))
    });
    let removed = before - outbox.len();
    bucket.insert("outbox".into(), Value::Array(outbox));
    if let Some(root) = state.as_object_mut() {
        root.insert(HOUSEHOLD_STATE_KEY.into(), Value::Object(bucket));
    }
    Ok(removed)
}

pub fn household_sync_folder(folder: &str, create: bool) -> Result<PathBuf, SyncError> {
    let root = expand_user_path(folder);
    if !root.is_dir() {
        return Err(SyncError::Folder(format!(
            "Sync folder does not exist: {}",
            root.display()
        )));
    }
    let target = root.join(HOUSEHOLD_SYNC_DIRECTORY);
    if target
        .symlink_metadata()
        .map(|m| m.file_type().is_symlink() || !m.is_dir())
        .unwrap_or(false)
    {
        return Err(SyncError::Folder(
            "Household sync namespace is not a directory.".to_owned(),
        ));
    }
    if create {
        let events = target.join(HOUSEHOLD_EVENT_DIRECTORY);
        if events
            .symlink_metadata()
            .map(|m| m.file_type().is_symlink() || !m.is_dir())
            .unwrap_or(false)
        {
            return Err(SyncError::Folder(
                "Household sync event directory is not a directory.".to_owned(),
            ));
        }
        fs::create_dir_all(&events).map_err(|_| {
            SyncError::Folder(format!(
                "Unable to create household sync folder: {}",
                target.display()
            ))
        })?;
        set_mode(&target, 0o700);
        set_mode(&events, 0o700);
    }
    Ok(target)
}

fn read_record_file(path: &Path) -> Result<Value, SyncError> {
    let fname = path.file_name().and_then(|s| s.to_str()).unwrap_or("");
    let meta = fs::symlink_metadata(path)
        .map_err(|_| SyncError::Validation(format!("Invalid household record file: {fname}")))?;
    if meta.file_type().is_symlink() || !meta.is_file() {
        return Err(SyncError::Validation(format!(
            "Household record file is not a regular file: {fname}"
        )));
    }
    if meta.len() as usize > MAX_RECORD_BYTES + 1 {
        return Err(SyncError::Validation(format!(
            "Household record is too large: {fname}"
        )));
    }
    let raw = fs::read_to_string(path)
        .map_err(|_| SyncError::Validation(format!("Invalid household record file: {fname}")))?;
    let val: Value = serde_json::from_str(&raw)
        .map_err(|_| SyncError::Validation(format!("Invalid household record file: {fname}")))?;
    let checked = validate_record(&val)?;
    let stem = path.file_stem().and_then(|s| s.to_str()).unwrap_or("");
    if stem != checked["event_id"].as_str().unwrap_or("") {
        return Err(SyncError::Validation(format!(
            "Household record filename does not match its hash: {fname}"
        )));
    }
    Ok(checked)
}

fn read_records_at_root(target: &Path) -> Result<Vec<Value>, SyncError> {
    if !target.exists() {
        return Ok(Vec::new());
    }
    let events_dir = target.join(HOUSEHOLD_EVENT_DIRECTORY);
    if !events_dir.exists() {
        return Ok(Vec::new());
    }
    if events_dir
        .symlink_metadata()
        .map(|m| m.file_type().is_symlink() || !m.is_dir())
        .unwrap_or(true)
    {
        return Err(SyncError::Folder(
            "Household sync event directory is not a directory.".to_owned(),
        ));
    }
    let mut paths: Vec<PathBuf> = fs::read_dir(&events_dir)
        .map_err(|e| SyncError::Folder(e.to_string()))?
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().and_then(|e| e.to_str()) == Some("json"))
        .collect();
    paths.sort();
    if paths.len() > MAX_RECORDS {
        return Err(SyncError::Validation(
            "Too many household sync records.".to_owned(),
        ));
    }
    let mut records = Vec::with_capacity(paths.len());
    for p in paths {
        records.push(read_record_file(&p)?);
    }
    validate_record_set(&records, &[])?;
    Ok(records)
}

fn write_record_at_root(target: &Path, record: &Value) -> Result<PathBuf, SyncError> {
    let checked = validate_record(record)?;
    let eid = checked["event_id"].as_str().unwrap_or("");
    let target_path = target
        .join(HOUSEHOLD_EVENT_DIRECTORY)
        .join(format!("{eid}.json"));
    if target_path.exists() {
        let existing = read_record_file(&target_path)?;
        if existing == checked {
            return Ok(target_path);
        }
        return Err(SyncError::Validation(format!(
            "Immutable household record differs from existing file: {eid}.json"
        )));
    }
    let encoded = format!("{}\n", String::from_utf8_lossy(&canonical_json(&checked)));
    atomic_write_text(&target_path, &encoded, 0o600)
        .map_err(|_| SyncError::Folder("Unable to publish household record.".to_owned()))?;
    set_mode(&target_path, 0o600);
    Ok(target_path)
}

pub fn read_household_records(folder: &str) -> Result<Vec<Value>, SyncError> {
    let target = household_sync_folder(folder, false)?;
    if !target.exists() {
        return Ok(Vec::new());
    }
    let _flock = file_lock(
        &target.join(HOUSEHOLD_LOCK_FILENAME),
        true,
        Duration::from_secs(30),
    )
    .map_err(|_| SyncError::Folder("Unable to lock household sync folder.".to_owned()))?;
    read_records_at_root(&target)
}

pub fn publish_household_outbox(state: &mut Value, folder: &str) -> Result<Value, SyncError> {
    let (bucket, records) = state_bucket(state)?;
    let allow_shares = stats_sharing_enabled(state);
    let pending: Vec<Value> = bucket
        .get("outbox")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let publishable: Vec<Value> = pending
        .iter()
        .filter(|item| item.get("kind").and_then(Value::as_str) != Some("share") || allow_shares)
        .cloned()
        .collect();
    let skipped = pending.len() - publishable.len();

    let mut needed = HashSet::new();
    for item in &publishable {
        let id = item["event_id"].as_str().unwrap_or("").to_owned();
        needed.extend(ancestors(&id, &records));
        needed.insert(id);
    }
    let candidates: Vec<Value> = needed
        .iter()
        .filter_map(|id| records.get(id).cloned())
        .collect();
    let mut published_ids: Vec<String> = publishable
        .iter()
        .filter_map(|i| i.get("event_id").and_then(Value::as_str).map(str::to_owned))
        .collect();
    published_ids.sort();

    let target = household_sync_folder(folder, true)?;
    {
        let _flock = file_lock(
            &target.join(HOUSEHOLD_LOCK_FILENAME),
            true,
            Duration::from_secs(30),
        )
        .map_err(|_| SyncError::Folder("Unable to lock household sync folder.".to_owned()))?;
        let remote = read_records_at_root(&target)?;
        validate_record_set(&candidates, &remote)?;
        let mut combined: HashMap<String, Value> = remote
            .into_iter()
            .map(|r| (r["event_id"].as_str().unwrap_or("").to_owned(), r))
            .collect();
        for c in &candidates {
            combined.insert(c["event_id"].as_str().unwrap_or("").to_owned(), c.clone());
        }
        let mut ordered: Vec<(usize, i64, String, Value)> = candidates
            .into_iter()
            .map(|item| {
                let id = item["event_id"].as_str().unwrap_or("").to_owned();
                let depth = ancestors(&id, &combined).len();
                let seq = item["sequence"].as_i64().unwrap_or(0);
                (depth, seq, id, item)
            })
            .collect();
        ordered.sort_by(|a, b| {
            a.0.cmp(&b.0)
                .then_with(|| a.1.cmp(&b.1))
                .then_with(|| a.2.cmp(&b.2))
        });
        for (_, _, _, item) in &ordered {
            write_record_at_root(&target, item)?;
        }
    }

    let acknowledged = acknowledge_outbox(state, &published_ids)?;
    let rem_pending = state
        .get(HOUSEHOLD_STATE_KEY)
        .and_then(|h| h.get("outbox"))
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0);
    Ok(json!({
        "published": published_ids.len(),
        "acknowledged": acknowledged,
        "pending": rem_pending,
        "stats_skipped": skipped,
        "published_ids": published_ids,
    }))
}

pub fn pull_household_records(state: &mut Value, folder: &str) -> Result<Value, SyncError> {
    let incoming = read_household_records(folder)?;
    let mut res = merge_records(state, &incoming)?;
    let ids: Vec<String> = incoming
        .iter()
        .filter_map(|i| i.get("event_id").and_then(Value::as_str).map(str::to_owned))
        .collect();
    let ack = acknowledge_outbox(state, &ids)?;
    if let Some(m) = res.as_object_mut() {
        m.insert("acknowledged".into(), json!(ack));
    }
    Ok(res)
}

fn result_is_eligible(challenge: &Value, payload: &Value, checked: Option<&Value>) -> bool {
    if let Some(parts) = challenge.get("participant_ids").and_then(Value::as_array) {
        let mid = payload
            .get("member_id")
            .and_then(Value::as_str)
            .unwrap_or("");
        if !parts.iter().any(|p| p.as_str() == Some(mid)) {
            return false;
        }
    }
    if let Some(ch_gid) = challenge.get("game_id").and_then(Value::as_str)
        && payload.get("game_id").and_then(Value::as_str) != Some(ch_gid)
    {
        return false;
    }
    if let Some(dl) = challenge.get("deadline").and_then(Value::as_str) {
        let res_at = payload
            .get("completed_at")
            .and_then(Value::as_str)
            .or_else(|| {
                checked
                    .and_then(|c| c.get("created_at"))
                    .and_then(Value::as_str)
            });
        match res_at {
            None => return false,
            Some(at) if timestamp_key(at) >= timestamp_key(dl) => return false,
            _ => {}
        }
    }
    true
}

pub fn challenge_progress(
    challenge: &Value,
    records: &[Value],
    member_id: Option<&str>,
) -> Result<Value, SyncError> {
    let (ch_payload, ch_rec) = if challenge.get("event_id").is_some() {
        (challenge.get("payload"), Some(challenge))
    } else {
        (Some(challenge), None)
    };
    let cid = ch_payload
        .and_then(|p| p.get("challenge_id"))
        .and_then(Value::as_str)
        .unwrap_or("");
    let target = ch_payload
        .and_then(|p| p.get("target"))
        .and_then(Value::as_i64)
        .unwrap_or(0);
    if ch_rec.is_some_and(|r| r.get("tombstone") == Some(&Value::Bool(true))) {
        let fallback_id = ch_rec
            .and_then(|r| r.get("sync_key"))
            .and_then(Value::as_str)
            .and_then(|s| s.split_once(':'))
            .map(|(_, id)| id)
            .unwrap_or(cid);
        return Ok(json!({
            "challenge_id": if cid.is_empty() { fallback_id } else { cid },
            "status": "deleted",
            "deleted": true,
            "current": 0,
            "target": target,
            "completed": false,
            "progress": 0.0,
            "members": [],
            "winner": Value::Null,
        }));
    }
    let ch_p = ch_payload.unwrap();
    let converged = converge_records(records, &[])?;
    let mut latest: BTreeMap<String, (Value, Value)> = BTreeMap::new();
    for rec in converged {
        if rec.get("kind").and_then(Value::as_str) != Some("challenge_result") {
            continue;
        }
        let Some(p) = rec.get("payload") else {
            continue;
        };
        if p.get("challenge_id").and_then(Value::as_str) != Some(cid) {
            continue;
        }
        if !result_is_eligible(ch_p, p, Some(&rec)) {
            continue;
        }
        let mid = p
            .get("member_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        let replace = match latest.get(&mid) {
            None => true,
            Some((_, prev_rec)) => record_order_key(&rec) > record_order_key(prev_rec),
        };
        if replace {
            latest.insert(mid, (p.clone(), rec));
        }
    }

    type WinnerOrderKey = (i64, i32, i64, i32, i64, String, String, String);
    let mut members = Vec::new();
    let mut winner_cand: Option<(WinnerOrderKey, Value)> = None;
    for (mid, (p, rec)) in &latest {
        let val = p.get("value").and_then(Value::as_i64).unwrap_or(0);
        let cur = if target > 0 { val.min(target) } else { 0 };
        let done = p.get("completed").and_then(Value::as_bool).unwrap_or(false) || val >= target;
        if done {
            let ca = p
                .get("completed_at")
                .and_then(Value::as_str)
                .or_else(|| rec.get("created_at").and_then(Value::as_str))
                .unwrap_or("");
            let ca_ts = timestamp_key(ca);
            let rk = record_order_key(rec);
            let key = (
                ca_ts.as_second(),
                ca_ts.subsec_nanosecond(),
                rk.0,
                rk.1,
                rk.2,
                rk.3,
                rk.4,
                mid.clone(),
            );
            if winner_cand.as_ref().is_none_or(|(best_k, _)| key < *best_k) {
                winner_cand = Some((key, json!({"member_id": mid, "result": p})));
            }
        }
        members.push(json!({
            "member_id": mid,
            "current": cur,
            "target": target,
            "completed": done,
            "result": p,
            "record_id": rec.get("event_id").cloned().unwrap_or(Value::Null),
        }));
    }

    let selected = member_id.and_then(|mid| {
        members
            .iter()
            .find(|m| m.get("member_id").and_then(Value::as_str) == Some(mid))
    });
    let current = selected
        .and_then(|m| m.get("current").and_then(Value::as_i64))
        .unwrap_or_else(|| {
            members
                .iter()
                .filter_map(|m| m.get("current").and_then(Value::as_i64))
                .max()
                .unwrap_or(0)
        });
    let completed = selected
        .and_then(|m| m.get("completed").and_then(Value::as_bool))
        .unwrap_or_else(|| {
            members
                .iter()
                .any(|m| m.get("completed").and_then(Value::as_bool).unwrap_or(false))
        });
    let expired = ch_p
        .get("deadline")
        .and_then(Value::as_str)
        .is_some_and(|dl| Timestamp::now() >= timestamp_key(dl) && !completed);
    let status = if completed {
        "complete"
    } else if expired {
        "expired"
    } else {
        ch_p.get("status").and_then(Value::as_str).unwrap_or("open")
    };
    let ratio = if target > 0 {
        ((current as f64) / (target as f64)).min(1.0)
    } else {
        0.0
    };
    let percent = (ratio * 10000.0).round() / 100.0;

    Ok(json!({
        "challenge_id": cid,
        "member_id": member_id,
        "current": current,
        "target": target,
        "completed": completed,
        "complete": completed,
        "expired": expired,
        "status": status,
        "progress": ratio,
        "percent": percent,
        "members": members,
        "winner": winner_cand.map(|(_, w)| w).unwrap_or(Value::Null),
    }))
}

pub fn compute_leaderboard(
    state: &Value,
    period: Option<&str>,
    limit: usize,
) -> Result<Value, SyncError> {
    let norm_period = match period.filter(|s| !s.trim().is_empty()) {
        Some(p) => Some(normalise_share_period(p)?),
        None => None,
    };
    if !stats_sharing_enabled(state) {
        return Ok(json!({
            "available": false,
            "reason": "stats_sharing_disabled",
            "entries": [],
        }));
    }
    let lim = limit.clamp(1, 100);
    let selected = household_records(state, true)?;
    let mut members: HashMap<String, &Value> = HashMap::new();
    for item in &selected {
        if item.get("kind").and_then(Value::as_str) == Some("member")
            && let Some(p) = item.get("payload")
            && let Some(mid) = p.get("member_id").and_then(Value::as_str)
        {
            members.insert(mid.to_owned(), p);
        }
    }

    let expected_start = match &norm_period {
        Some(p) if p != "all_time" => stats_window(p, None)?.1,
        _ => None,
    };
    let mut totals: HashMap<String, HashMap<&'static str, i64>> = HashMap::new();
    for item in &selected {
        if item.get("kind").and_then(Value::as_str) != Some("share") {
            continue;
        }
        let Some(p) = item.get("payload") else {
            continue;
        };
        let p_period =
            normalise_share_period(p.get("period").and_then(Value::as_str).unwrap_or(""))
                .unwrap_or_default();
        if let Some(ref want) = norm_period {
            if p_period != *want {
                continue;
            }
            if want != "all_time"
                && let Some(ws) = p.get("window_start").and_then(Value::as_str)
            {
                let actual_start = parse_dt_opt(ws);
                if actual_start != expected_start {
                    continue;
                }
                if let Some(we) = p.get("window_end").and_then(Value::as_str)
                    && parse_dt_opt(we).is_none_or(|e| actual_start.is_none_or(|s| e <= s))
                {
                    continue;
                }
            }
        }
        let mid = p
            .get("member_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        let row = totals
            .entry(mid)
            .or_insert_with(|| SHARE_STAT_FIELDS.iter().map(|f| (*f, 0i64)).collect());
        if let Some(stats) = p.get("stats").and_then(Value::as_object) {
            for &field in SHARE_STAT_FIELDS {
                if let Some(v) = stats.get(field).and_then(Value::as_i64) {
                    let cur = row.entry(field).or_insert(0);
                    *cur = (*cur + v).min(MAX_STAT_VALUE);
                }
            }
        }
    }

    let mut entries: Vec<Value> = totals
        .into_iter()
        .map(|(mid, tot)| {
            let prof = members.get(&mid).copied();
            let mut obj = Map::new();
            obj.insert("member_id".into(), Value::String(mid.clone()));
            obj.insert(
                "display_name".into(),
                Value::String(
                    prof.and_then(|p| p.get("display_name"))
                        .and_then(Value::as_str)
                        .unwrap_or(&mid)
                        .to_owned(),
                ),
            );
            obj.insert(
                "avatar_color".into(),
                Value::String(
                    prof.and_then(|p| p.get("avatar_color"))
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned(),
                ),
            );
            for (k, v) in tot {
                obj.insert(k.into(), json!(v));
            }
            Value::Object(obj)
        })
        .collect();
    entries.sort_by(|a, b| {
        let ka = (
            -(a.get("completions").and_then(Value::as_i64).unwrap_or(0)),
            -(a.get("playtime_seconds")
                .and_then(Value::as_i64)
                .unwrap_or(0)),
            -(a.get("ra_count").and_then(Value::as_i64).unwrap_or(0)),
            a.get("member_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned(),
        );
        let kb = (
            -(b.get("completions").and_then(Value::as_i64).unwrap_or(0)),
            -(b.get("playtime_seconds")
                .and_then(Value::as_i64)
                .unwrap_or(0)),
            -(b.get("ra_count").and_then(Value::as_i64).unwrap_or(0)),
            b.get("member_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned(),
        );
        ka.cmp(&kb)
    });
    entries.truncate(lim);
    for (idx, row) in entries.iter_mut().enumerate() {
        if let Some(m) = row.as_object_mut() {
            m.insert("rank".into(), json!(idx + 1));
        }
    }
    Ok(json!({
        "available": true,
        "entries": entries,
        "period": norm_period.unwrap_or_else(|| "all_time".to_owned()),
    }))
}

pub fn household_state_token(state: &Value) -> Result<String, SyncError> {
    let (_, records) = state_bucket(state)?;
    let mut keys: Vec<String> = records.keys().cloned().collect();
    keys.sort();
    Ok(sha256_canonical(&json!({
        "records": keys,
        "stats_sharing": stats_sharing_enabled(state),
    })))
}
