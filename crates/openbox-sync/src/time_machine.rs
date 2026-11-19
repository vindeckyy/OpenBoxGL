//! Local library Time Machine engine (`pkg/parity/parity_time_machine.py`).

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use jiff::Timestamp;
use serde_json::{Map, Value, json};

use crate::library_sync::{
    CATALOG_FIELDS, SYNC_METADATA_KEY, SyncError, ancestors, append_event, catalog_for_sync_key,
    common_base_many, ensure_device_id, journal_enabled, make_event, metadata_mut, next_sequence,
    replace_catalog, stable_sync_key, state_event_map, state_token, sync_enabled, validate_event,
};

pub const JOURNAL_HORIZON_KEY: &str = "journal_horizon";
pub const JOURNAL_REVERTS_KEY: &str = "journal_reverts";
pub const JOURNAL_COMPACT_CHECK_KEY: &str = "journal_compact_checked_at";
pub const JOURNAL_COMPACT_TOTAL_KEY: &str = "journal_compacted_total";
pub const JOURNAL_COMPACT_ERROR_KEY: &str = "journal_compact_error";
pub const JOURNAL_OVERFLOW_KEY: &str = "journal_overflow";

pub const JOURNAL_RETENTION_DAYS: i64 = 400;
pub const JOURNAL_MAX_EVENTS: usize = 25_000;
pub const JOURNAL_COMPACT_CHECK_HOURS: i64 = 24;
pub const EVENT_PAGE_DEFAULT: usize = 200;
pub const EVENT_PAGE_MAX: usize = 1000;
pub const COMPARE_PAGE_DEFAULT: usize = 200;
pub const COMPARE_PAGE_MAX: usize = 1000;
pub const REVERT_FORMAT: &str = "time-machine-revert-v1";
pub const COMPARE_FORMAT: &str = "time-machine-compare-v1";
const AMBIGUOUS_BASE_MARKER: &str = "__sync_ambiguous_bases__";

fn format_iso(ts: Timestamp) -> String {
    let dt = jiff::tz::TimeZone::UTC.to_datetime(ts);
    let micros = ts.subsec_nanosecond() / 1000;
    if micros > 0 {
        format!(
            "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}.{:06}+00:00",
            dt.year(),
            dt.month(),
            dt.day(),
            dt.hour(),
            dt.minute(),
            dt.second(),
            micros
        )
    } else {
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
}

fn parse_dt(raw: &str) -> Option<Timestamp> {
    let text = raw.trim();
    if text.is_empty() {
        return None;
    }
    if let Ok(ts) = text.parse::<Timestamp>() {
        return Some(ts);
    }
    if let Ok(dt) = text.parse::<jiff::civil::DateTime>() {
        return jiff::tz::TimeZone::UTC.to_timestamp(dt).ok();
    }
    None
}

pub fn parse_as_of_date(value: &str) -> Result<Timestamp, SyncError> {
    let mut text = value.trim().to_owned();
    if text.is_empty() {
        return Err(SyncError::Validation(
            "date parameter is required".to_owned(),
        ));
    }
    if text.len() == 10 && text.as_bytes()[4] == b'-' && text.as_bytes()[7] == b'-' {
        text = format!("{text}T23:59:59.999999");
    }
    parse_dt(&text)
        .ok_or_else(|| SyncError::Validation(format!("date must be ISO-8601, got: {value:?}")))
}

fn validated_events(state: &Value) -> (HashMap<String, Value>, Vec<Value>, Map<String, Value>) {
    let metadata = state
        .get(SYNC_METADATA_KEY)
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let mut events = HashMap::new();
    let mut corrupt = Vec::new();
    let pairs: Vec<(String, Value)> = match metadata.get("events") {
        Some(Value::Object(m)) => m.iter().map(|(k, v)| (k.clone(), v.clone())).collect(),
        Some(Value::Array(arr)) => arr
            .iter()
            .filter_map(|v| {
                let id = v.get("event_id").and_then(Value::as_str)?.to_owned();
                Some((id, v.clone()))
            })
            .collect(),
        _ => Vec::new(),
    };
    for (ident, item) in pairs {
        match validate_event(&item) {
            Ok(checked) if checked["event_id"].as_str() == Some(ident.as_str()) => {
                events.insert(ident, checked);
            }
            _ => {
                let sk = item.get("sync_key").cloned().unwrap_or(Value::Null);
                corrupt.push(json!({
                    "event_id": ident.chars().take(64).collect::<String>(),
                    "sync_key": sk,
                }));
            }
        }
    }
    (events, corrupt, metadata)
}

fn order_key(ev: &Value) -> (i64, i32, i64, String) {
    let ts = ev
        .get("created_at")
        .and_then(Value::as_str)
        .and_then(parse_dt)
        .unwrap_or(Timestamp::MIN);
    (
        ts.as_second(),
        ts.subsec_nanosecond(),
        ev.get("sequence").and_then(Value::as_i64).unwrap_or(0),
        ev.get("event_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned(),
    )
}

fn event_kind(ev: &Value, events: &HashMap<String, Value>) -> &'static str {
    if ev.get("tombstone") == Some(&Value::Bool(true)) {
        return "delete";
    }
    let parents = ev
        .get("parents")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    if parents.is_empty() {
        return "add";
    }
    let known: Vec<&Value> = parents
        .iter()
        .filter_map(Value::as_str)
        .filter_map(|pid| events.get(pid))
        .collect();
    if !known.is_empty()
        && known
            .iter()
            .all(|item| item.get("tombstone") == Some(&Value::Bool(true)))
    {
        return "restore";
    }
    "edit"
}

fn base_catalog(ev: &Value, events: &HashMap<String, Value>) -> (Map<String, Value>, bool) {
    let parents: Vec<String> = ev
        .get("parents")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default();
    if parents.is_empty() {
        return (Map::new(), false);
    }
    let missing = parents.iter().any(|pid| !events.contains_key(pid));
    if parents.len() == 1 {
        let base = events
            .get(&parents[0])
            .filter(|p| p.get("tombstone") != Some(&Value::Bool(true)))
            .and_then(|p| p.get("catalog"))
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        return (base, missing);
    }
    let mut base = common_base_many(&parents, events);
    let ambiguous = base.get(AMBIGUOUS_BASE_MARKER).is_some();
    if ambiguous {
        base = base
            .get(AMBIGUOUS_BASE_MARKER)
            .and_then(Value::as_array)
            .and_then(|a| a.first())
            .cloned()
            .unwrap_or_else(|| json!({}));
    }
    (
        base.as_object().cloned().unwrap_or_default(),
        missing || ambiguous,
    )
}

fn diff_maps(
    before: Option<&Map<String, Value>>,
    after: Option<&Map<String, Value>>,
) -> Map<String, Value> {
    let empty = Map::new();
    let bm = before.unwrap_or(&empty);
    let am = after.unwrap_or(&empty);
    let mut keys: BTreeSet<&String> = bm.keys().collect();
    keys.extend(am.keys());
    let mut out = Map::new();
    for k in keys {
        let bv = bm.get(k).cloned().unwrap_or(Value::Null);
        let av = am.get(k).cloned().unwrap_or(Value::Null);
        if bv != av {
            out.insert(k.clone(), json!({"from": bv, "to": av}));
        }
    }
    out
}

fn event_game_id(ev: &Value, events: &HashMap<String, Value>) -> Option<String> {
    if let Some(gid) = ev
        .get("catalog")
        .and_then(|c| c.get("game_id"))
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        return Some(gid.to_owned());
    }
    let (base, _) = base_catalog(ev, events);
    if let Some(gid) = base
        .get("game_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        return Some(gid.to_owned());
    }
    let skey = ev.get("sync_key").and_then(Value::as_str).unwrap_or("");
    skey.strip_prefix("game:").map(str::to_owned)
}

fn event_row(ev: &Value, events: &HashMap<String, Value>, reverts: &Map<String, Value>) -> Value {
    let empty = Map::new();
    let catalog = ev
        .get("catalog")
        .and_then(Value::as_object)
        .unwrap_or(&empty);
    let (base, base_missing) = base_catalog(ev, events);
    let is_tomb = ev.get("tombstone") == Some(&Value::Bool(true));
    let new_vals = if is_tomb { None } else { Some(catalog) };
    let eid = ev.get("event_id").and_then(Value::as_str).unwrap_or("");
    let name = catalog
        .get("name")
        .or_else(|| base.get("name"))
        .cloned()
        .unwrap_or(Value::Null);
    let mut row = json!({
        "event_id": eid,
        "created_at": ev.get("created_at").cloned().unwrap_or(Value::Null),
        "sequence": ev.get("sequence").and_then(Value::as_i64).unwrap_or(0),
        "sync_key": ev.get("sync_key").cloned().unwrap_or(Value::Null),
        "device_id": ev.get("device_id").cloned().unwrap_or(Value::Null),
        "kind": event_kind(ev, events),
        "game_id": event_game_id(ev, events),
        "name": name,
        "tombstone": is_tomb,
        "parents": ev.get("parents").cloned().unwrap_or_else(|| json!([])),
        "changes": Value::Object(diff_maps(Some(&base), new_vals)),
        "diff_base_missing": base_missing,
    });
    if let Some(link) = reverts.get(eid).filter(|v| v.is_object())
        && let Some(m) = row.as_object_mut()
    {
        m.insert("revert".into(), link.clone());
    }
    row
}

pub fn list_events(
    state: &Value,
    days: Option<f64>,
    kind: Option<&str>,
    game_id: Option<&str>,
    offset: usize,
    limit: Option<usize>,
) -> Result<Value, SyncError> {
    let (events, corrupt, metadata) = validated_events(state);
    let empty = Map::new();
    let reverts = metadata
        .get(JOURNAL_REVERTS_KEY)
        .and_then(Value::as_object)
        .unwrap_or(&empty);
    if let Some(d) = days
        && (!d.is_finite() || d <= 0.0)
    {
        return Err(SyncError::Validation(
            "days must be a positive number".to_owned(),
        ));
    }
    let lim = limit.unwrap_or(EVENT_PAGE_DEFAULT).min(EVENT_PAGE_MAX);
    let cutoff = days.map(|d| {
        let secs = (d * 86400.0) as i64;
        Timestamp::from_second(Timestamp::now().as_second().saturating_sub(secs))
            .unwrap_or(Timestamp::MIN)
    });
    let gap = events.values().any(|ev| {
        ev.get("parents")
            .and_then(Value::as_array)
            .is_some_and(|ps| {
                ps.iter()
                    .filter_map(Value::as_str)
                    .any(|pid| !events.contains_key(pid))
            })
    });

    let mut matched: Vec<&Value> = Vec::new();
    for ev in events.values() {
        let created = ev
            .get("created_at")
            .and_then(Value::as_str)
            .and_then(parse_dt);
        if let Some(c) = cutoff
            && created.is_none_or(|dt| dt < c)
        {
            continue;
        }
        if let Some(k) = kind.filter(|s| !s.is_empty()) {
            if k == "revert" {
                let eid = ev.get("event_id").and_then(Value::as_str).unwrap_or("");
                if !reverts.contains_key(eid) {
                    continue;
                }
            } else if event_kind(ev, &events) != k {
                continue;
            }
        }
        if let Some(gid) = game_id.filter(|s| !s.is_empty())
            && event_game_id(ev, &events).as_deref() != Some(gid)
        {
            continue;
        }
        matched.push(ev);
    }
    matched.sort_by_key(|ev| std::cmp::Reverse(order_key(ev)));
    let total = matched.len();
    let page: Vec<Value> = matched
        .into_iter()
        .skip(offset)
        .take(lim)
        .map(|ev| event_row(ev, &events, reverts))
        .collect();
    let stamps: Vec<Timestamp> = events
        .values()
        .filter_map(|e| {
            e.get("created_at")
                .and_then(Value::as_str)
                .and_then(parse_dt)
        })
        .collect();
    let earliest = stamps.iter().min().copied().map(format_iso);
    let latest = stamps.iter().max().copied().map(format_iso);
    let has_more = offset + page.len() < total;
    Ok(json!({
        "events": page,
        "total": total,
        "offset": offset,
        "limit": lim,
        "has_more": has_more,
        "journal_enabled": journal_enabled(state),
        "earliest_at": earliest,
        "latest_at": latest,
        "horizon": metadata.get(JOURNAL_HORIZON_KEY).cloned().unwrap_or(Value::Null),
        "gap": gap,
        "corrupt": corrupt,
    }))
}

pub fn materialize_as_of(state: &Value, when: Option<Timestamp>) -> Result<Value, SyncError> {
    let (events, corrupt, metadata) = validated_events(state);
    let mut eligible: Vec<&Value> = Vec::new();
    for ev in events.values() {
        let created = ev
            .get("created_at")
            .and_then(Value::as_str)
            .and_then(parse_dt)
            .unwrap_or(Timestamp::MIN);
        if when.is_none_or(|w| created <= w) {
            eligible.push(ev);
        }
    }
    let mut heads: HashMap<String, &Value> = HashMap::new();
    for ev in &eligible {
        let key = ev.get("sync_key").and_then(Value::as_str).unwrap_or("");
        if key.is_empty() {
            continue;
        }
        match heads.get(key) {
            None => {
                heads.insert(key.to_owned(), *ev);
            }
            Some(prev) if order_key(ev) > order_key(prev) => {
                heads.insert(key.to_owned(), *ev);
            }
            _ => {}
        }
    }
    let corrupt_keys: BTreeSet<String> = corrupt
        .iter()
        .filter_map(|c| c.get("sync_key").and_then(Value::as_str).map(str::to_owned))
        .collect();
    let mut rows: Vec<Value> = Vec::new();
    for (key, ev) in &heads {
        if corrupt_keys.contains(key) || ev.get("tombstone") == Some(&Value::Bool(true)) {
            continue;
        }
        if let Some(cat) = ev.get("catalog").and_then(Value::as_object)
            && !cat.is_empty()
        {
            let mut m = cat.clone();
            m.insert("sync_key".into(), Value::String(key.clone()));
            rows.push(Value::Object(m));
        }
    }
    rows.sort_by(|a, b| {
        let ka = (
            a.get("name")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_lowercase(),
            a.get("sync_key")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned(),
        );
        let kb = (
            b.get("name")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_lowercase(),
            b.get("sync_key")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned(),
        );
        ka.cmp(&kb)
    });
    let stamps: Vec<Timestamp> = events
        .values()
        .filter_map(|e| {
            e.get("created_at")
                .and_then(Value::as_str)
                .and_then(parse_dt)
        })
        .collect();
    let earliest = stamps.iter().min().copied();
    let latest = stamps.iter().max().copied();
    let horizon = metadata
        .get(JOURNAL_HORIZON_KEY)
        .cloned()
        .unwrap_or(Value::Null);
    let horizon_dt = horizon.as_str().and_then(parse_dt);
    let truncated = match (horizon_dt, when) {
        (Some(h), Some(w)) => w < h,
        _ => false,
    };
    let as_of_str = when
        .map(format_iso)
        .or_else(|| latest.map(format_iso))
        .unwrap_or_else(|| format_iso(Timestamp::now()));
    Ok(json!({
        "as_of": as_of_str,
        "requested": when.map(format_iso),
        "games": rows,
        "count": rows.len(),
        "before_first_event": eligible.is_empty(),
        "earliest_at": earliest.map(format_iso),
        "latest_at": latest.map(format_iso),
        "horizon": horizon,
        "truncated": truncated,
        "corrupt": corrupt,
        "unknown_keys": corrupt_keys.into_iter().collect::<Vec<_>>(),
    }))
}

pub fn compare(
    state: &Value,
    before: Option<&str>,
    after: &str,
    fields: Option<&[String]>,
    limit: Option<usize>,
) -> Result<Value, SyncError> {
    if !journal_enabled(state) {
        return Err(SyncError::Validation(
            "time-machine compare requires the library journal".to_owned(),
        ));
    }
    let after_dt = parse_as_of_date(after)?;
    let before_dt = match before.filter(|s| !s.trim().is_empty()) {
        Some(b) => Some(parse_as_of_date(b)?),
        None => None,
    };
    if let Some(b) = before_dt
        && b > after_dt
    {
        return Err(SyncError::Validation(
            "compare dates are reversed: 'before' is later than 'after'".to_owned(),
        ));
    }
    let later = materialize_as_of(state, Some(after_dt))?;
    let earlier = match before_dt {
        Some(b) => Some(materialize_as_of(state, Some(b))?),
        None => None,
    };

    let by_key = |view: &Value| -> BTreeMap<String, Value> {
        let mut m = BTreeMap::new();
        if let Some(arr) = view.get("games").and_then(Value::as_array) {
            for r in arr {
                if let Some(sk) = r.get("sync_key").and_then(Value::as_str) {
                    m.insert(sk.to_owned(), r.clone());
                }
            }
        }
        m
    };

    let mut added: BTreeMap<String, Value> = BTreeMap::new();
    let mut removed: BTreeMap<String, Value> = BTreeMap::new();
    let mut changed: Vec<Value> = Vec::new();
    if let Some(ref early) = earlier {
        let old_cat = by_key(early);
        let new_cat = by_key(&later);
        for (k, r) in &new_cat {
            if !old_cat.contains_key(k) {
                added.insert(k.clone(), r.clone());
            }
        }
        for (k, r) in &old_cat {
            if !new_cat.contains_key(k) {
                removed.insert(k.clone(), r.clone());
            }
        }
        let wanted: Option<HashSet<&str>> =
            fields.map(|fs| fs.iter().map(String::as_str).collect());
        for (k, old_r) in &old_cat {
            let Some(new_r) = new_cat.get(k) else {
                continue;
            };
            let mut delta = diff_maps(old_r.as_object(), new_r.as_object());
            if let Some(ref w) = wanted {
                delta.retain(|fk, _| w.contains(fk.as_str()));
            }
            if !delta.is_empty() {
                changed.push(json!({"sync_key": k, "fields": Value::Object(delta)}));
            }
        }
    } else {
        added = by_key(&later);
    }

    let page = limit
        .unwrap_or(COMPARE_PAGE_DEFAULT)
        .clamp(1, COMPARE_PAGE_MAX);
    let added_rows: Vec<Value> = added
        .iter()
        .take(page)
        .map(|(k, r)| json!({"sync_key": k, "record": r}))
        .collect();
    let removed_rows: Vec<Value> = removed
        .iter()
        .take(page)
        .map(|(k, r)| json!({"sync_key": k, "record": r}))
        .collect();
    let mut result = json!({
        "format": COMPARE_FORMAT,
        "before": before_dt.map(format_iso),
        "after": format_iso(after_dt),
        "summary": {
            "added": added.len(),
            "removed": removed.len(),
            "changed": changed.len(),
        },
        "added": added_rows,
        "removed": removed_rows,
        "changed": &changed[..changed.len().min(page)],
        "truncated": added.len().max(removed.len()).max(changed.len()) > page,
        "later_truncated": later.get("truncated").and_then(Value::as_bool).unwrap_or(false),
        "later_before_first_event": later.get("before_first_event").and_then(Value::as_bool).unwrap_or(false),
    });
    if let Some(early) = earlier
        && let Some(m) = result.as_object_mut()
    {
        let et = early
            .get("truncated")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let eb = early
            .get("before_first_event")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        m.insert("earlier_truncated".into(), Value::Bool(et));
        m.insert("earlier_before_first_event".into(), Value::Bool(eb));
        m.insert("baseline_incomplete".into(), Value::Bool(et || eb));
    }
    Ok(result)
}

fn validate_revert_fields(fields: Option<&[String]>) -> Result<Option<Vec<String>>, SyncError> {
    let Some(fs) = fields else {
        return Ok(None);
    };
    let mut invalid: Vec<String> = fs
        .iter()
        .filter(|f| !CATALOG_FIELDS.contains(&f.as_str()) || f.as_str() == "game_id")
        .cloned()
        .collect();
    if !invalid.is_empty() {
        invalid.sort();
        return Err(SyncError::Validation(format!(
            "non-catalog fields cannot be reverted: {invalid:?}"
        )));
    }
    Ok(Some(fs.to_vec()))
}

fn catalog_for_key(state: &Value, sync_key: &str) -> Result<Option<Map<String, Value>>, SyncError> {
    let games = state
        .get("games")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    for g in games {
        if stable_sync_key(g).ok().as_deref() == Some(sync_key) {
            return Ok(catalog_for_sync_key(g, sync_key)?.as_object().cloned());
        }
    }
    Ok(None)
}

pub fn plan_revert(
    state: &Value,
    event_id: &str,
    game_id: Option<&str>,
    fields: Option<&[String]>,
    undo: bool,
) -> Result<Value, SyncError> {
    let (events, _, _) = validated_events(state);
    let Some(event) = events.get(event_id) else {
        return Err(SyncError::Validation("event not in journal".to_owned()));
    };
    let wanted = validate_revert_fields(fields)?;
    let key = event.get("sync_key").and_then(Value::as_str).unwrap_or("");
    let empty = Map::new();
    let catalog = event
        .get("catalog")
        .and_then(Value::as_object)
        .unwrap_or(&empty);
    let (base, base_missing) = base_catalog(event, &events);
    let resolved_gid = catalog
        .get("game_id")
        .or_else(|| base.get("game_id"))
        .and_then(Value::as_str)
        .unwrap_or("");
    if let Some(gid) = game_id.filter(|s| !s.is_empty())
        && !resolved_gid.is_empty()
        && gid != resolved_gid
    {
        return Err(SyncError::Validation(
            "event does not belong to that game".to_owned(),
        ));
    }
    let current = catalog_for_key(state, key)?;
    let is_tomb = event.get("tombstone") == Some(&Value::Bool(true));

    let target: Map<String, Value> = if undo {
        if base_missing {
            return Err(SyncError::Validation(
                "cannot undo: the event's base is unavailable".to_owned(),
            ));
        }
        let ev_diff = diff_maps(Some(&base), if is_tomb { None } else { Some(catalog) });
        let mut undo_fields: Vec<String> = ev_diff.keys().cloned().collect();
        if let Some(ref w) = wanted {
            undo_fields.retain(|f| w.contains(f));
        }
        if is_tomb {
            base.clone()
        } else if event
            .get("parents")
            .and_then(Value::as_array)
            .is_none_or(Vec::is_empty)
            && base.is_empty()
        {
            Map::new()
        } else {
            let mut t = current.clone().unwrap_or_default();
            for f in undo_fields {
                if let Some(bv) = base.get(&f) {
                    t.insert(f, bv.clone());
                } else {
                    t.remove(&f);
                }
            }
            t
        }
    } else if is_tomb {
        Map::new()
    } else if let Some(ref w) = wanted {
        if let Some(ref cur) = current {
            let mut t = cur.clone();
            for f in w {
                if let Some(cv) = catalog.get(f) {
                    t.insert(f.clone(), cv.clone());
                } else {
                    t.remove(f);
                }
            }
            t
        } else {
            catalog.clone()
        }
    } else {
        catalog.clone()
    };

    let diff = diff_maps(current.as_ref(), Some(&target));
    let mut changes = Vec::new();
    for (field, vals) in diff {
        if field == "game_id" {
            continue;
        }
        let action = if vals.get("to").is_none_or(Value::is_null) {
            "remove"
        } else {
            "set"
        };
        changes.push(json!({
            "field": field,
            "action": action,
            "from": vals.get("from").cloned().unwrap_or(Value::Null),
            "to": vals.get("to").cloned().unwrap_or(Value::Null),
        }));
    }
    let gid_out = if !resolved_gid.is_empty() {
        Some(resolved_gid.to_owned())
    } else {
        current
            .as_ref()
            .and_then(|c| c.get("game_id"))
            .and_then(Value::as_str)
            .map(str::to_owned)
    };
    let noop = changes.is_empty();
    let target_val = if target.is_empty() {
        Value::Null
    } else {
        Value::Object(target)
    };
    Ok(json!({
        "format": REVERT_FORMAT,
        "sync_key": key,
        "game_id": gid_out,
        "event_id": event["event_id"],
        "undo": undo,
        "fields": wanted,
        "changes": changes,
        "target_catalog": target_val,
        "base_token": state_token(state)?,
        "noop": noop,
    }))
}

pub fn apply_revert(state: &mut Value, plan: &Value) -> Result<Value, SyncError> {
    if !plan.is_object() || plan.get("format").and_then(Value::as_str) != Some(REVERT_FORMAT) {
        return Err(SyncError::Validation("invalid revert plan".to_owned()));
    }
    if !journal_enabled(state) {
        return Err(SyncError::Validation(
            "time-machine reverts require the library journal".to_owned(),
        ));
    }
    let cur_token = state_token(state)?;
    if plan.get("base_token").and_then(Value::as_str) != Some(cur_token.as_str()) {
        return Err(SyncError::Stale(
            "revert plan is stale: the library changed after the preview".to_owned(),
        ));
    }
    let fields: Option<Vec<String>> = plan.get("fields").and_then(Value::as_array).map(|a| {
        a.iter()
            .filter_map(Value::as_str)
            .map(str::to_owned)
            .collect()
    });
    let replay = plan_revert(
        state,
        plan.get("event_id").and_then(Value::as_str).unwrap_or(""),
        plan.get("game_id").and_then(Value::as_str),
        fields.as_deref(),
        plan.get("undo").and_then(Value::as_bool).unwrap_or(false),
    )?;
    if replay.get("noop").and_then(Value::as_bool).unwrap_or(false) {
        return Ok(json!({"changed": false, "applied": 0}));
    }
    let key = replay
        .get("sync_key")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    let target = replay
        .get("target_catalog")
        .filter(|v| !v.is_null())
        .cloned();

    let index = state
        .get("games")
        .and_then(Value::as_array)
        .and_then(|games| {
            games
                .iter()
                .position(|g| stable_sync_key(g).ok().as_deref() == Some(key.as_str()))
        });
    if target.is_none() && index.is_none() {
        return Ok(json!({"changed": false, "applied": 0}));
    }
    let new_game = match (index, &target) {
        (Some(i), Some(t)) => {
            let cur = state
                .get("games")
                .and_then(Value::as_array)
                .and_then(|g| g.get(i));
            replace_catalog(cur, Some(t))
        }
        (Some(_), None) => None,
        (None, Some(t)) => Some(t.clone()),
        (None, None) => None,
    };

    let dev = ensure_device_id(state)?;
    let eid = {
        let meta = metadata_mut(state)?;
        let parents: Vec<String> = meta
            .get("heads")
            .and_then(|h| h.get(&key))
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .map(str::to_owned)
                    .collect()
            })
            .unwrap_or_default();
        let seq = next_sequence(meta);
        let ev = make_event(
            &dev,
            seq,
            &key,
            &parents,
            new_game.as_ref(),
            target.is_none() && new_game.is_none(),
            None,
        )?;
        let eid = ev["event_id"].as_str().unwrap_or("").to_owned();
        append_event(meta, &ev, true)?;
        if let Some(h) = meta.get_mut("heads").and_then(Value::as_object_mut) {
            h.insert(key.clone(), json!([eid]));
        }
        let changed_fields: Vec<Value> = replay
            .get("changes")
            .and_then(Value::as_array)
            .map(|a| a.iter().filter_map(|r| r.get("field").cloned()).collect())
            .unwrap_or_default();
        let reverts = meta
            .entry(JOURNAL_REVERTS_KEY.to_owned())
            .or_insert_with(|| Value::Object(Map::new()));
        if !reverts.is_object() {
            *reverts = Value::Object(Map::new());
        }
        if let Some(rm) = reverts.as_object_mut() {
            rm.insert(
                eid.clone(),
                json!({
                    "source_event": replay["event_id"],
                    "fields": changed_fields,
                    "undo": plan.get("undo").and_then(Value::as_bool).unwrap_or(false),
                }),
            );
        }
        meta.insert("_suppress_local_recording".into(), Value::Bool(true));
        eid
    };

    if let Some(games) = state.get_mut("games").and_then(Value::as_array_mut) {
        match (index, &new_game) {
            (Some(i), None) => {
                games.remove(i);
            }
            (Some(i), Some(ng)) => {
                games[i] = ng.clone();
            }
            (None, Some(ng)) => {
                games.push(ng.clone());
            }
            (None, None) => {}
        }
    }
    let applied_count = replay
        .get("changes")
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0);
    Ok(json!({
        "changed": true,
        "applied": applied_count,
        "event_id": eid,
        "deleted": new_game.is_none() && index.is_some(),
        "restored": index.is_none() && new_game.is_some(),
    }))
}

pub fn compact_journal(state: &mut Value) -> Result<Value, SyncError> {
    let sync_on = sync_enabled(state);
    let now_dt = Timestamp::now();
    let meta = metadata_mut(state)?;
    let mut events = state_event_map(meta)?;
    let mut protected: HashSet<String> = HashSet::new();
    if let Some(heads) = meta.get("heads").and_then(Value::as_object) {
        for ids in heads.values() {
            if let Some(arr) = ids.as_array() {
                for id in arr.iter().filter_map(Value::as_str) {
                    protected.insert(id.to_owned());
                }
            }
        }
    }
    if sync_on {
        if let Some(outbox) = meta.get("outbox").and_then(Value::as_array) {
            for ev in outbox {
                if let Some(id) = ev.get("event_id").and_then(Value::as_str) {
                    protected.insert(id.to_owned());
                }
            }
        }
        let snap: Vec<String> = protected.iter().cloned().collect();
        for id in snap {
            protected.extend(ancestors(&id, &events));
        }
    }
    let cutoff = Timestamp::from_second(
        now_dt
            .as_second()
            .saturating_sub(JOURNAL_RETENTION_DAYS * 86400),
    )
    .unwrap_or(Timestamp::MIN);
    let expired: HashSet<String> = events
        .values()
        .filter_map(|ev| {
            let id = ev.get("event_id").and_then(Value::as_str)?;
            if protected.contains(id) {
                return None;
            }
            let ts = ev
                .get("created_at")
                .and_then(Value::as_str)
                .and_then(parse_dt)
                .unwrap_or(now_dt);
            (ts < cutoff).then(|| id.to_owned())
        })
        .collect();

    let (drop_set, overflow) = if events.len() > JOURNAL_MAX_EVENTS && !sync_on {
        let mut retained = protected.clone();
        let mut all_evs: Vec<&Value> = events.values().collect();
        all_evs.sort_by_key(|ev| std::cmp::Reverse(order_key(ev)));
        for ev in all_evs {
            if retained.len() >= JOURNAL_MAX_EVENTS {
                break;
            }
            if let Some(id) = ev.get("event_id").and_then(Value::as_str) {
                retained.insert(id.to_owned());
            }
        }
        let drops: HashSet<String> = events
            .keys()
            .filter(|id| !retained.contains(*id))
            .cloned()
            .collect();
        let ov = events.len().saturating_sub(drops.len()) > JOURNAL_MAX_EVENTS;
        (drops, ov)
    } else {
        let ov = events.len().saturating_sub(expired.len()) > JOURNAL_MAX_EVENTS;
        (expired, ov)
    };

    if !drop_set.is_empty() {
        let newest_drop = drop_set
            .iter()
            .filter_map(|id| events.get(id))
            .filter_map(|ev| {
                ev.get("created_at")
                    .and_then(Value::as_str)
                    .and_then(parse_dt)
            })
            .max()
            .unwrap_or(now_dt);
        for id in &drop_set {
            events.remove(id);
        }
        let mut ev_obj = Map::new();
        for (k, v) in &events {
            ev_obj.insert(k.clone(), v.clone());
        }
        meta.insert("events".into(), Value::Object(ev_obj));
        if let Some(outbox) = meta.get_mut("outbox").and_then(Value::as_array_mut) {
            outbox.retain(|ev| {
                ev.get("event_id")
                    .and_then(Value::as_str)
                    .is_none_or(|id| !drop_set.contains(id))
            });
        }
        let prior = meta
            .get(JOURNAL_HORIZON_KEY)
            .and_then(Value::as_str)
            .and_then(parse_dt);
        let horizon = prior.map(|p| p.max(newest_drop)).unwrap_or(newest_drop);
        meta.insert(
            JOURNAL_HORIZON_KEY.into(),
            Value::String(format_iso(horizon)),
        );
        let prev_total = meta
            .get(JOURNAL_COMPACT_TOTAL_KEY)
            .and_then(Value::as_u64)
            .unwrap_or(0);
        meta.insert(
            JOURNAL_COMPACT_TOTAL_KEY.into(),
            json!(prev_total + drop_set.len() as u64),
        );
    }
    if overflow {
        meta.insert(JOURNAL_OVERFLOW_KEY.into(), Value::Bool(true));
    } else {
        meta.remove(JOURNAL_OVERFLOW_KEY);
    }
    meta.insert(
        JOURNAL_COMPACT_CHECK_KEY.into(),
        Value::String(format_iso(now_dt)),
    );
    Ok(json!({
        "dropped": drop_set.len(),
        "remaining": events.len(),
        "horizon": meta.get(JOURNAL_HORIZON_KEY).cloned().unwrap_or(Value::Null),
        "overflow": overflow,
    }))
}

pub fn maybe_compact_journal(state: &mut Value) -> Option<Value> {
    let meta = state.get(SYNC_METADATA_KEY)?.as_object()?;
    let count = match meta.get("events") {
        Some(Value::Object(m)) => m.len(),
        Some(Value::Array(a)) => a.len(),
        _ => 0,
    };
    let now_dt = Timestamp::now();
    let checked = meta
        .get(JOURNAL_COMPACT_CHECK_KEY)
        .and_then(Value::as_str)
        .and_then(parse_dt);
    let due = count >= JOURNAL_MAX_EVENTS
        || checked.is_none_or(|c| {
            now_dt.as_second().saturating_sub(c.as_second()) >= JOURNAL_COMPACT_CHECK_HOURS * 3600
        });
    if !due {
        return None;
    }
    match compact_journal(state) {
        Ok(report) => {
            if let Ok(m) = metadata_mut(state) {
                m.remove(JOURNAL_COMPACT_ERROR_KEY);
            }
            if report.get("dropped").and_then(Value::as_u64).unwrap_or(0) > 0 {
                Some(report)
            } else {
                None
            }
        }
        Err(err) => {
            if let Ok(m) = metadata_mut(state) {
                m.insert(
                    JOURNAL_COMPACT_ERROR_KEY.into(),
                    Value::String(err.to_string().chars().take(300).collect()),
                );
                m.insert(
                    JOURNAL_COMPACT_CHECK_KEY.into(),
                    Value::String(format_iso(now_dt)),
                );
            }
            None
        }
    }
}

/// Post-commit hook wired into `AppState::set_sync_hook` so every local state mutation
/// automatically records causal catalog events when sync or journal is enabled.
pub fn record_transaction_hook(before: &Value, after: &mut Value) {
    if let Some(m) = after
        .get_mut(SYNC_METADATA_KEY)
        .and_then(Value::as_object_mut)
        && m.remove("_suppress_local_recording").is_some()
    {
        return;
    }
    if !sync_enabled(after) && !journal_enabled(after) {
        return;
    }
    let before_games = before
        .get("games")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let after_games = after
        .get("games")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if before_games == after_games.as_slice() {
        return;
    }
    if let Err(err) =
        crate::library_sync::record_local_changes(after, before_games, &after_games, None, None)
    {
        crate::library_sync::set_sync_error(after, &err.to_string(), "SYNC_IDENTITY_AMBIGUOUS");
    } else {
        maybe_compact_journal(after);
    }
}
