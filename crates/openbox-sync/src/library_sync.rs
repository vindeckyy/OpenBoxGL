//! Pure, causal catalog synchronization (v3 DAG) (`pkg/parity/parity_library_sync.py`).

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

use jiff::Timestamp;
use openbox_io::fsync_directory;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use uuid::Uuid;

use crate::saves::{expand_user_path, set_mode};

pub const SYNC_METADATA_KEY: &str = "library_sync";
pub const SYNC_FORMAT: i64 = 3;
pub const SYNC_DIRECTORY: &str = "openbox-library-v3";
pub const EVENT_DIRECTORY: &str = "events";
pub const MAX_EVENT_BYTES: usize = 1024 * 1024;
pub const MAX_EVENTS: usize = 50_000;
pub const MAX_CATALOG_BYTES: usize = 256 * 1024;
pub const JOURNAL_SETTING: &str = "library_journal_enabled";

pub const CATALOG_FIELDS: &[&str] = &[
    "game_id",
    "name",
    "sort_title",
    "alternate_names",
    "platform",
    "genre",
    "year",
    "developer",
    "publisher",
    "series",
    "region",
    "esrb",
    "max_players",
    "description",
    "notes",
    "wikipedia_url",
    "source",
    "steam_app_id",
    "heroic_app_id",
    "lutris_id",
    "gameyfin_id",
    "igdb_id",
    "ra_game_id",
    "launchbox_db_id",
    "disc_count",
    "rom_name",
    "clone_of",
    "set_type",
    "manual_entry",
    "tags",
    "library_sync_id",
];

pub const PROVIDER_ID_FIELDS: &[&str] = &[
    "steam_app_id",
    "heroic_app_id",
    "lutris_id",
    "gameyfin_id",
    "igdb_id",
    "ra_game_id",
    "launchbox_db_id",
];

pub const EXPLICIT_IDENTITY_FIELDS: &[&str] = &[
    "library_sync_id",
    "sync_identity",
    "verified_sync_key",
    "verified_provider_identity",
];

pub const EVENT_FIELDS: &[&str] = &[
    "format",
    "event_id",
    "device_id",
    "sequence",
    "created_at",
    "sync_key",
    "parents",
    "catalog",
    "tombstone",
];

#[derive(Debug, Clone)]
pub enum SyncError {
    Validation(String),
    Folder(String),
    Stale(String),
}

impl std::fmt::Display for SyncError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SyncError::Validation(s) | SyncError::Folder(s) | SyncError::Stale(s) => f.write_str(s),
        }
    }
}

impl std::error::Error for SyncError {}

/// Canonical UTF-8 compact JSON with sorted object keys (exact match for Python `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"))`).
pub fn canonical_json(value: &Value) -> Vec<u8> {
    let mut out = String::new();
    write_canonical(value, &mut out);
    out.into_bytes()
}

fn write_canonical(value: &Value, out: &mut String) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => out.push_str(&n.to_string()),
        Value::String(s) => {
            if let Ok(encoded) = serde_json::to_string(s) {
                out.push_str(&encoded);
            }
        }
        Value::Array(arr) => {
            out.push('[');
            for (i, item) in arr.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_canonical(item, out);
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            for (i, k) in keys.into_iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                if let Ok(ek) = serde_json::to_string(k) {
                    out.push_str(&ek);
                }
                out.push(':');
                write_canonical(&map[k], out);
            }
            out.push('}');
        }
    }
}

pub fn sha256_canonical(value: &Value) -> String {
    let mut h = Sha256::new();
    h.update(canonical_json(value));
    hex::encode(h.finalize())
}

pub fn utc_now_micros() -> String {
    let now = Timestamp::now();
    let dt = jiff::tz::TimeZone::UTC.to_datetime(now);
    let micros = now.subsec_nanosecond() / 1000;
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
}

pub fn valid_timestamp(val: Option<&Value>) -> bool {
    let Some(s) = val.and_then(Value::as_str) else {
        return false;
    };
    if s.is_empty() || s.len() > 80 {
        return false;
    }
    s.parse::<Timestamp>().is_ok()
}

pub fn clean_id(val: Option<&Value>, label: &str) -> Result<String, SyncError> {
    let raw = match val {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) if n.is_i64() || n.is_u64() => n.to_string(),
        _ => {
            return Err(SyncError::Validation(format!(
                "Sync {label} must be a non-empty string."
            )));
        }
    };
    let trimmed = raw.trim();
    if trimmed.is_empty() || trimmed.len() > 256 || trimmed.chars().any(|c| (c as u32) < 32) {
        return Err(SyncError::Validation(format!("Sync {label} is invalid.")));
    }
    Ok(trimmed.to_owned())
}

fn is_hex64(s: &str) -> bool {
    s.len() == 64 && s.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

fn json_safe(val: &Value, depth: usize) -> Result<Value, SyncError> {
    if depth > 8 {
        return Err(SyncError::Validation(
            "Catalog values are nested too deeply.".to_owned(),
        ));
    }
    match val {
        Value::Null | Value::Bool(_) | Value::String(_) => Ok(val.clone()),
        Value::Number(n) => {
            if let Some(f) = n.as_f64()
                && !f.is_finite()
            {
                return Err(SyncError::Validation(
                    "Catalog values must be finite.".to_owned(),
                ));
            }
            Ok(val.clone())
        }
        Value::Array(arr) => {
            if arr.len() > 10_000 {
                return Err(SyncError::Validation(
                    "Catalog list is too large.".to_owned(),
                ));
            }
            let mut out = Vec::with_capacity(arr.len());
            for item in arr {
                out.push(json_safe(item, depth + 1)?);
            }
            Ok(Value::Array(out))
        }
        Value::Object(map) => {
            if map.len() > 1_000 {
                return Err(SyncError::Validation(
                    "Catalog object is too large.".to_owned(),
                ));
            }
            let mut out = Map::new();
            for (k, v) in map {
                if k.len() > 128 {
                    return Err(SyncError::Validation(
                        "Catalog keys are invalid.".to_owned(),
                    ));
                }
                out.insert(k.clone(), json_safe(v, depth + 1)?);
            }
            Ok(Value::Object(out))
        }
    }
}

pub fn build_catalog(game: &Value) -> Result<Map<String, Value>, SyncError> {
    let Some(obj) = game.as_object() else {
        return Err(SyncError::Validation(
            "A game must be an object.".to_owned(),
        ));
    };
    let mut catalog = Map::new();
    for field in CATALOG_FIELDS {
        if let Some(v) = obj.get(*field)
            && !v.is_null()
        {
            catalog.insert((*field).into(), json_safe(v, 0)?);
        }
    }
    if canonical_json(&Value::Object(catalog.clone())).len() > MAX_CATALOG_BYTES {
        return Err(SyncError::Validation("Catalog is too large.".to_owned()));
    }
    Ok(catalog)
}

pub fn stable_sync_key(game: &Value) -> Result<String, SyncError> {
    let Some(obj) = game.as_object() else {
        return Err(SyncError::Validation(
            "A game must be an object.".to_owned(),
        ));
    };
    for field in EXPLICIT_IDENTITY_FIELDS {
        if let Some(v) = obj.get(*field)
            && !v.is_null()
            && !v.is_object()
            && !v.is_array()
            && !v.is_boolean()
            && v.as_str().is_none_or(|s| !s.is_empty())
        {
            return Ok(format!("verified:{}", clean_id(Some(v), field)?));
        }
    }
    if obj
        .get("provider_identity_verified")
        .and_then(Value::as_bool)
        == Some(true)
    {
        for field in PROVIDER_ID_FIELDS {
            if let Some(v) = obj.get(*field)
                && !v.is_null()
                && !v.is_object()
                && !v.is_array()
                && !v.is_boolean()
                && v.as_str().is_none_or(|s| !s.is_empty())
            {
                return Ok(format!(
                    "verified:provider:{field}:{}",
                    clean_id(Some(v), field)?
                ));
            }
        }
    }
    let gid = obj.get("game_id");
    if gid.is_none() || gid == Some(&Value::Null) || gid.and_then(Value::as_str) == Some("") {
        return Err(SyncError::Validation(
            "A game needs a stable game_id or explicit verified sync identity.".to_owned(),
        ));
    }
    Ok(format!("game:{}", clean_id(gid, "game_id")?))
}

pub fn compute_event_id(event: &Map<String, Value>) -> String {
    let mut without = Map::new();
    for (k, v) in event {
        if k != "event_id" {
            without.insert(k.clone(), v.clone());
        }
    }
    sha256_canonical(&Value::Object(without))
}

pub fn make_event(
    device_id: &str,
    sequence: i64,
    sync_key: &str,
    parents: &[String],
    catalog: Option<&Value>,
    tombstone: bool,
    created_at: Option<&str>,
) -> Result<Value, SyncError> {
    let dev = clean_id(Some(&Value::String(device_id.to_owned())), "device_id")?;
    let skey = clean_id(Some(&Value::String(sync_key.to_owned())), "sync_key")?;
    if sequence < 1 {
        return Err(SyncError::Validation(
            "Event sequence must be a positive integer.".to_owned(),
        ));
    }
    let mut parent_ids: Vec<String> = Vec::with_capacity(parents.len());
    let mut seen_p = HashSet::new();
    for p in parents {
        let cp = clean_id(Some(&Value::String(p.clone())), "parent event ID")?;
        if !seen_p.insert(cp.clone()) {
            return Err(SyncError::Validation(
                "Event parents must be unique.".to_owned(),
            ));
        }
        parent_ids.push(cp);
    }
    if parent_ids.len() > 256 {
        return Err(SyncError::Validation(
            "An event has too many parents.".to_owned(),
        ));
    }
    parent_ids.sort();
    let when = created_at
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .unwrap_or_else(utc_now_micros);
    if !valid_timestamp(Some(&Value::String(when.clone()))) {
        return Err(SyncError::Validation(
            "Event timestamp is invalid.".to_owned(),
        ));
    }
    if tombstone && catalog.is_some() {
        return Err(SyncError::Validation(
            "An event cannot contain both catalog and tombstone.".to_owned(),
        ));
    }
    if !tombstone && catalog.is_none() {
        return Err(SyncError::Validation(
            "An event needs a catalog or tombstone.".to_owned(),
        ));
    }
    let mut ev = Map::new();
    ev.insert("format".into(), json!(SYNC_FORMAT));
    ev.insert("device_id".into(), Value::String(dev));
    ev.insert("sequence".into(), json!(sequence));
    ev.insert("created_at".into(), Value::String(when));
    ev.insert("sync_key".into(), Value::String(skey.clone()));
    ev.insert(
        "parents".into(),
        Value::Array(parent_ids.into_iter().map(Value::String).collect()),
    );
    if tombstone {
        ev.insert("tombstone".into(), Value::Bool(true));
    } else {
        let mut cat = build_catalog(catalog.unwrap())?;
        if let Some(rest) = skey.strip_prefix("verified:")
            && !cat
                .get("library_sync_id")
                .and_then(Value::as_str)
                .is_some_and(|s| !s.is_empty())
        {
            cat.insert("library_sync_id".into(), Value::String(rest.to_owned()));
        }
        ev.insert("catalog".into(), Value::Object(cat));
    }
    let eid = compute_event_id(&ev);
    ev.insert("event_id".into(), Value::String(eid));
    let val = Value::Object(ev);
    validate_event(&val)
}

pub fn validate_event(event: &Value) -> Result<Value, SyncError> {
    let Some(obj) = event.as_object() else {
        return Err(SyncError::Validation("Event shape is invalid.".to_owned()));
    };
    for k in obj.keys() {
        if !EVENT_FIELDS.contains(&k.as_str()) {
            return Err(SyncError::Validation("Event shape is invalid.".to_owned()));
        }
    }
    for req in [
        "format",
        "event_id",
        "device_id",
        "sequence",
        "created_at",
        "sync_key",
        "parents",
    ] {
        if !obj.contains_key(req) {
            return Err(SyncError::Validation(
                "Event has missing or unexpected fields.".to_owned(),
            ));
        }
    }
    if obj.get("format").and_then(Value::as_i64) != Some(SYNC_FORMAT) {
        return Err(SyncError::Validation(
            "Unsupported library sync format.".to_owned(),
        ));
    }
    let actual_id = clean_id(obj.get("event_id"), "event_id")?;
    if !is_hex64(&actual_id) {
        return Err(SyncError::Validation("Event ID is invalid.".to_owned()));
    }
    if compute_event_id(obj) != actual_id {
        return Err(SyncError::Validation(
            "Event content hash does not match event_id.".to_owned(),
        ));
    }
    clean_id(obj.get("device_id"), "device_id")?;
    let seq = obj
        .get("sequence")
        .and_then(Value::as_i64)
        .filter(|n| *n >= 1);
    if seq.is_none() || obj.get("sequence").is_some_and(Value::is_boolean) {
        return Err(SyncError::Validation(
            "Event sequence is invalid.".to_owned(),
        ));
    }
    if !valid_timestamp(obj.get("created_at")) {
        return Err(SyncError::Validation(
            "Event timestamp is invalid.".to_owned(),
        ));
    }
    let skey = clean_id(obj.get("sync_key"), "sync_key")?;
    if let Some(rest) = skey.strip_prefix("game:") {
        if rest.is_empty() {
            return Err(SyncError::Validation(
                "Game sync key is invalid.".to_owned(),
            ));
        }
    } else if let Some(rest) = skey.strip_prefix("verified:") {
        if rest.is_empty() {
            return Err(SyncError::Validation(
                "Verified sync key is invalid.".to_owned(),
            ));
        }
    } else {
        return Err(SyncError::Validation(
            "Unsupported sync key namespace.".to_owned(),
        ));
    }
    let Some(parents) = obj.get("parents").and_then(Value::as_array) else {
        return Err(SyncError::Validation(
            "Event parents are invalid.".to_owned(),
        ));
    };
    if parents.len() > 256 {
        return Err(SyncError::Validation(
            "Event parents are invalid.".to_owned(),
        ));
    }
    let mut p_strs = Vec::with_capacity(parents.len());
    for p in parents {
        let pid = clean_id(Some(p), "parent event ID")?;
        if !is_hex64(&pid) {
            return Err(SyncError::Validation(
                "Event parent ID is invalid.".to_owned(),
            ));
        }
        p_strs.push(pid);
    }
    let mut sorted_p = p_strs.clone();
    sorted_p.sort();
    sorted_p.dedup();
    if sorted_p != p_strs {
        return Err(SyncError::Validation(
            "Event parents must be unique and sorted.".to_owned(),
        ));
    }
    let has_cat = obj.contains_key("catalog");
    let has_tomb = obj.contains_key("tombstone");
    if has_cat == has_tomb || (has_tomb && obj.get("tombstone") != Some(&Value::Bool(true))) {
        return Err(SyncError::Validation(
            "Event must contain exactly one valid catalog or tombstone.".to_owned(),
        ));
    }
    if has_cat {
        let Some(cat) = obj.get("catalog").and_then(Value::as_object) else {
            return Err(SyncError::Validation(
                "Event catalog is invalid.".to_owned(),
            ));
        };
        for k in cat.keys() {
            if !CATALOG_FIELDS.contains(&k.as_str()) {
                return Err(SyncError::Validation(
                    "Event catalog contains a non-catalog field.".to_owned(),
                ));
            }
        }
        if let Some(rest) = skey.strip_prefix("game:") {
            let gid = cat.get("game_id").and_then(Value::as_str).unwrap_or("");
            if gid != rest {
                return Err(SyncError::Validation(
                    "Game sync identity does not match event key.".to_owned(),
                ));
            }
        }
        if let Some(rest) = skey.strip_prefix("verified:") {
            let ident = cat
                .get("library_sync_id")
                .and_then(Value::as_str)
                .unwrap_or("");
            if !ident.is_empty() && ident != rest {
                return Err(SyncError::Validation(
                    "Verified sync identity does not match event key.".to_owned(),
                ));
            }
        }
        build_catalog(obj.get("catalog").unwrap())?;
    }
    if canonical_json(event).len() > MAX_EVENT_BYTES {
        return Err(SyncError::Validation("Event is too large.".to_owned()));
    }
    Ok(event.clone())
}

pub fn validate_event_set(
    events: &[Value],
    known_events: &[Value],
) -> Result<Vec<Value>, SyncError> {
    if events.len() > MAX_EVENTS {
        return Err(SyncError::Validation("Too many sync events.".to_owned()));
    }
    let mut known: HashMap<String, Value> = HashMap::new();
    for item in known_events {
        let checked = validate_event(item)?;
        let id = checked["event_id"].as_str().unwrap_or("").to_owned();
        known.insert(id, checked);
    }
    let mut result_order = Vec::new();
    let mut result: HashMap<String, Value> = HashMap::new();
    for item in events {
        let checked = validate_event(item)?;
        let id = checked["event_id"].as_str().unwrap_or("").to_owned();
        if result.contains_key(&id) {
            return Err(SyncError::Validation(
                "Duplicate event identity.".to_owned(),
            ));
        }
        if let Some(k) = known.get(&id) {
            if *k != checked {
                return Err(SyncError::Validation(
                    "Duplicate event identity.".to_owned(),
                ));
            }
            continue;
        }
        result_order.push(id.clone());
        result.insert(id, checked);
    }
    let mut combined = known;
    for (k, v) in &result {
        combined.insert(k.clone(), v.clone());
    }
    for checked in result.values() {
        let skey = checked["sync_key"].as_str().unwrap_or("");
        if let Some(parents) = checked["parents"].as_array() {
            for p in parents {
                let pid = p.as_str().unwrap_or("");
                let Some(parent_ev) = combined.get(pid) else {
                    return Err(SyncError::Validation(
                        "Event DAG has a missing parent.".to_owned(),
                    ));
                };
                if parent_ev["sync_key"].as_str().unwrap_or("") != skey {
                    return Err(SyncError::Validation(
                        "Event parent belongs to another sync key.".to_owned(),
                    ));
                }
            }
        }
    }

    let mut visiting = HashSet::new();
    let mut visited = HashSet::new();
    fn visit(
        ident: &str,
        combined: &HashMap<String, Value>,
        visiting: &mut HashSet<String>,
        visited: &mut HashSet<String>,
    ) -> Result<(), SyncError> {
        if visiting.contains(ident) {
            return Err(SyncError::Validation(
                "Event DAG contains a cycle.".to_owned(),
            ));
        }
        if visited.contains(ident) {
            return Ok(());
        }
        visiting.insert(ident.to_owned());
        if let Some(ev) = combined.get(ident)
            && let Some(parents) = ev["parents"].as_array()
        {
            for p in parents {
                if let Some(pid) = p.as_str() {
                    visit(pid, combined, visiting, visited)?;
                }
            }
        }
        visiting.remove(ident);
        visited.insert(ident.to_owned());
        Ok(())
    }
    for ident in combined.keys() {
        visit(ident, &combined, &mut visiting, &mut visited)?;
    }
    Ok(result_order
        .into_iter()
        .filter_map(|id| result.remove(&id))
        .collect())
}

pub fn sync_folder(folder: &str, create: bool) -> Result<PathBuf, SyncError> {
    let root = expand_user_path(folder);
    if !root.is_dir() {
        return Err(SyncError::Folder(format!(
            "Sync folder does not exist: {}",
            root.display()
        )));
    }
    let target = root.join(SYNC_DIRECTORY);
    if create {
        fs::create_dir_all(target.join(EVENT_DIRECTORY)).map_err(|_| {
            SyncError::Folder(format!(
                "Unable to create sync folder: {}",
                target.display()
            ))
        })?;
    }
    Ok(target)
}

pub fn write_event(folder: &str, event: &Value) -> Result<PathBuf, SyncError> {
    let checked = validate_event(event)?;
    let eid = checked["event_id"].as_str().unwrap_or("");
    if !is_hex64(eid) {
        return Err(SyncError::Validation("Event ID is invalid.".to_owned()));
    }
    let target = sync_folder(folder, true)?
        .join(EVENT_DIRECTORY)
        .join(format!("{eid}.json"));
    let mut encoded = canonical_json(&checked);
    encoded.push(b'\n');
    if target.exists() {
        let raw = fs::read_to_string(&target).map_err(|_| {
            SyncError::Validation(format!(
                "Existing event cannot be replaced: {}",
                target.display()
            ))
        })?;
        let parsed: Value = serde_json::from_str(&raw).map_err(|_| {
            SyncError::Validation(format!(
                "Existing event cannot be replaced: {}",
                target.display()
            ))
        })?;
        if validate_event(&parsed)? == checked {
            return Ok(target);
        }
        return Err(SyncError::Validation(format!(
            "Immutable event differs from existing file: {}",
            target.display()
        )));
    }
    let parent = target.parent().unwrap_or_else(|| Path::new("."));
    let prefix = format!(".{eid}.json.");
    let tmp = tempfile::Builder::new()
        .prefix(&prefix)
        .suffix(".tmp")
        .tempfile_in(parent)
        .map_err(|_| {
            SyncError::Folder(format!(
                "Unable to publish sync event: {}",
                target.display()
            ))
        })?;
    let (mut f, tmp_path) = tmp.keep().map_err(|_| {
        SyncError::Folder(format!(
            "Unable to publish sync event: {}",
            target.display()
        ))
    })?;
    let write_res = (|| -> std::io::Result<()> {
        f.write_all(&encoded)?;
        f.flush()?;
        f.sync_all()?;
        Ok(())
    })();
    drop(f);
    if write_res.is_err() {
        let _ = fs::remove_file(&tmp_path);
        return Err(SyncError::Folder(format!(
            "Unable to publish sync event: {}",
            target.display()
        )));
    }
    set_mode(&tmp_path, 0o600);
    if fs::rename(&tmp_path, &target).is_err() {
        let _ = fs::remove_file(&tmp_path);
        return Err(SyncError::Folder(format!(
            "Unable to publish sync event: {}",
            target.display()
        )));
    }
    fsync_directory(parent);
    Ok(target)
}

pub fn read_events(folder: &str) -> Result<Vec<Value>, SyncError> {
    let target = sync_folder(folder, false)?;
    let events_dir = target.join(EVENT_DIRECTORY);
    if !events_dir.exists() {
        return Ok(Vec::new());
    }
    if !events_dir.is_dir() {
        return Err(SyncError::Folder(format!(
            "Sync event directory is not a directory: {}",
            events_dir.display()
        )));
    }
    let mut paths: Vec<PathBuf> = fs::read_dir(&events_dir)
        .map_err(|e| SyncError::Folder(e.to_string()))?
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().and_then(|e| e.to_str()) == Some("json"))
        .collect();
    paths.sort();
    if paths.len() > MAX_EVENTS {
        return Err(SyncError::Validation(
            "Too many sync event files.".to_owned(),
        ));
    }
    let mut events = Vec::with_capacity(paths.len());
    for path in paths {
        let fname = path.file_name().and_then(|s| s.to_str()).unwrap_or("");
        let meta = fs::metadata(&path)
            .map_err(|_| SyncError::Validation(format!("Invalid sync event file: {fname}")))?;
        if meta.len() as usize > MAX_EVENT_BYTES {
            return Err(SyncError::Validation(format!(
                "Sync event is too large: {fname}"
            )));
        }
        let raw = fs::read_to_string(&path)
            .map_err(|_| SyncError::Validation(format!("Invalid sync event file: {fname}")))?;
        let payload: Value = serde_json::from_str(&raw)
            .map_err(|_| SyncError::Validation(format!("Invalid sync event file: {fname}")))?;
        let checked = validate_event(&payload)?;
        let stem = path.file_stem().and_then(|s| s.to_str()).unwrap_or("");
        if stem != checked["event_id"].as_str().unwrap_or("") {
            return Err(SyncError::Validation(format!(
                "Sync event filename does not match its hash: {fname}"
            )));
        }
        events.push(checked);
    }
    validate_event_set(&events, &[])?;
    Ok(events)
}

pub fn metadata_mut(state: &mut Value) -> Result<&mut Map<String, Value>, SyncError> {
    let Some(root) = state.as_object_mut() else {
        return Err(SyncError::Validation(
            "Sync state must be an object.".to_owned(),
        ));
    };
    let val = root
        .entry(SYNC_METADATA_KEY.to_owned())
        .or_insert_with(|| Value::Object(Map::new()));
    let Some(m) = val.as_object_mut() else {
        return Err(SyncError::Validation(
            "Library sync metadata is invalid.".to_owned(),
        ));
    };
    m.entry("heads".to_owned())
        .or_insert_with(|| Value::Object(Map::new()));
    m.entry("events".to_owned())
        .or_insert_with(|| Value::Object(Map::new()));
    m.entry("outbox".to_owned())
        .or_insert_with(|| Value::Array(Vec::new()));
    Ok(m)
}

pub fn ensure_device_id(state: &mut Value) -> Result<String, SyncError> {
    let m = metadata_mut(state)?;
    let existing = m
        .get("device_id")
        .and_then(Value::as_str)
        .and_then(|s| Uuid::parse_str(s).ok())
        .map(|u| u.to_string());
    if let Some(id) = existing {
        return Ok(id);
    }
    let id = Uuid::new_v4().to_string();
    m.insert("device_id".into(), Value::String(id.clone()));
    Ok(id)
}

pub fn sync_enabled(state: &Value) -> bool {
    if state
        .get(SYNC_METADATA_KEY)
        .and_then(|m| m.get("enabled"))
        .and_then(Value::as_bool)
        == Some(true)
    {
        return true;
    }
    state
        .get("settings")
        .and_then(|s| s.get("library_sync_enabled"))
        .and_then(Value::as_bool)
        == Some(true)
}

pub fn journal_enabled(state: &Value) -> bool {
    let Some(obj) = state.as_object() else {
        return false;
    };
    let Some(settings) = obj.get("settings").and_then(Value::as_object) else {
        return true;
    };
    settings
        .get(JOURNAL_SETTING)
        .and_then(Value::as_bool)
        .unwrap_or(true)
}

pub fn set_sync_error(state: &mut Value, err_msg: &str, code: &str) -> Value {
    let msg: String = err_msg.chars().take(500).collect();
    let final_msg = if msg.is_empty() {
        "Library synchronization metadata is invalid.".to_owned()
    } else {
        msg
    };
    let err_obj = json!({"code": code, "message": final_msg});
    if let Some(root) = state.as_object_mut() {
        let meta = root
            .entry(SYNC_METADATA_KEY.to_owned())
            .or_insert_with(|| Value::Object(Map::new()));
        if !meta.is_object() {
            *meta = Value::Object(Map::new());
        }
        if let Some(m) = meta.as_object_mut() {
            m.insert("error".into(), err_obj.clone());
        }
    }
    err_obj
}

pub fn state_event_map(metadata: &Map<String, Value>) -> Result<HashMap<String, Value>, SyncError> {
    let mut result = HashMap::new();
    match metadata.get("events") {
        None => Ok(result),
        Some(Value::Object(m)) => {
            for (ident, item) in m {
                let checked = validate_event(item)?;
                if checked["event_id"].as_str() != Some(ident.as_str()) {
                    return Err(SyncError::Validation(
                        "Library sync event history key is invalid.".to_owned(),
                    ));
                }
                result.insert(ident.clone(), checked);
            }
            Ok(result)
        }
        Some(Value::Array(arr)) => {
            for item in arr {
                let checked = validate_event(item)?;
                let id = checked["event_id"].as_str().unwrap_or("").to_owned();
                result.insert(id, checked);
            }
            Ok(result)
        }
        _ => Err(SyncError::Validation(
            "Library sync event history is invalid.".to_owned(),
        )),
    }
}

pub fn append_event(
    metadata: &mut Map<String, Value>,
    event: &Value,
    outbox: bool,
) -> Result<(), SyncError> {
    let eid = event["event_id"].as_str().unwrap_or("").to_owned();
    // Normalize events field to Object if needed
    if metadata.get("events").is_some_and(Value::is_array) {
        let map = state_event_map(metadata)?;
        let mut obj = Map::new();
        for (k, v) in map {
            obj.insert(k, v);
        }
        metadata.insert("events".into(), Value::Object(obj));
    }
    let events_obj = metadata
        .entry("events".to_owned())
        .or_insert_with(|| Value::Object(Map::new()))
        .as_object_mut()
        .ok_or_else(|| {
            SyncError::Validation("Library sync event history is invalid.".to_owned())
        })?;
    events_obj.insert(eid.clone(), event.clone());
    if outbox {
        let pending = metadata
            .entry("outbox".to_owned())
            .or_insert_with(|| Value::Array(Vec::new()))
            .as_array_mut()
            .ok_or_else(|| SyncError::Validation("Library sync outbox is invalid.".to_owned()))?;
        if !pending
            .iter()
            .any(|item| item.get("event_id").and_then(Value::as_str) == Some(eid.as_str()))
        {
            pending.push(event.clone());
        }
    }
    Ok(())
}

pub fn next_sequence(metadata: &mut Map<String, Value>) -> i64 {
    let cur = metadata
        .get("next_sequence")
        .and_then(Value::as_i64)
        .filter(|n| *n >= 1)
        .unwrap_or(1);
    metadata.insert("next_sequence".into(), json!(cur + 1));
    cur
}

pub fn catalog_for_sync_key(game: &Value, key: &str) -> Result<Value, SyncError> {
    let mut cat = build_catalog(game)?;
    if let Some(rest) = key.strip_prefix("verified:")
        && !cat
            .get("library_sync_id")
            .and_then(Value::as_str)
            .is_some_and(|s| !s.is_empty())
    {
        cat.insert("library_sync_id".into(), Value::String(rest.to_owned()));
    }
    Ok(Value::Object(cat))
}

fn game_map_from_slice(games: &[Value]) -> Result<BTreeMap<String, Value>, SyncError> {
    let mut out = BTreeMap::new();
    for g in games {
        let key = stable_sync_key(g)?;
        if out.contains_key(&key) {
            return Err(SyncError::Validation(format!(
                "Duplicate local sync identity: {key}"
            )));
        }
        out.insert(key, g.clone());
    }
    Ok(out)
}

pub fn record_local_changes(
    state: &mut Value,
    before_games: &[Value],
    after_games: &[Value],
    now: Option<&str>,
    device: Option<&str>,
) -> Result<Value, SyncError> {
    let dev = match device {
        Some(d) if !d.is_empty() => d.to_owned(),
        _ => ensure_device_id(state)?,
    };
    let before_map = game_map_from_slice(before_games)?;
    let after_map = game_map_from_slice(after_games)?;
    let mut all_keys: BTreeSet<String> = before_map.keys().cloned().collect();
    all_keys.extend(after_map.keys().cloned());

    let metadata = metadata_mut(state)?;
    let created = now
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .unwrap_or_else(utc_now_micros);
    let mut generated = Vec::new();
    for key in all_keys {
        let old = before_map.get(&key);
        let new = after_map.get(&key);
        let old_cat = match old {
            Some(g) => Some(catalog_for_sync_key(g, &key)?),
            None => None,
        };
        let new_cat = match new {
            Some(g) => Some(catalog_for_sync_key(g, &key)?),
            None => None,
        };
        if old.is_some() && new.is_some() && old_cat == new_cat {
            continue;
        }
        let prior: Vec<String> = metadata
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
        let seq = next_sequence(metadata);
        let ev = make_event(
            &dev,
            seq,
            &key,
            &prior,
            new_cat.as_ref(),
            new.is_none(),
            Some(&created),
        )?;
        let eid = ev["event_id"].as_str().unwrap_or("").to_owned();
        append_event(metadata, &ev, true)?;
        if let Some(heads) = metadata.get_mut("heads").and_then(Value::as_object_mut) {
            heads.insert(key, json!([eid]));
        }
        generated.push(ev);
    }
    let outbox = metadata
        .get("outbox")
        .cloned()
        .unwrap_or_else(|| Value::Array(Vec::new()));
    Ok(json!({
        "events": generated,
        "outbox": outbox,
        "changed": generated.len(),
    }))
}

pub fn bootstrap_local_catalog(
    state: &mut Value,
    now: Option<&str>,
    device: Option<&str>,
) -> Value {
    let games = state
        .get("games")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let already_bootstrapped = state
        .get(SYNC_METADATA_KEY)
        .and_then(|m| m.get("catalog_bootstrapped"))
        .and_then(Value::as_bool)
        == Some(true);

    if already_bootstrapped {
        let baseline_res: Result<Vec<Value>, SyncError> = (|| {
            let meta = metadata_mut(state)?;
            let events = state_event_map(meta)?;
            let mut baseline = Vec::new();
            if let Some(heads) = meta.get("heads").and_then(Value::as_object) {
                for ids in heads.values() {
                    if let Some(arr) = ids.as_array()
                        && let Some(last_id) = arr.last().and_then(Value::as_str)
                        && let Some(ev) = events.get(last_id)
                        && ev.get("tombstone") != Some(&Value::Bool(true))
                        && let Some(mut cat) = ev.get("catalog").and_then(Value::as_object).cloned()
                    {
                        let skey = ev.get("sync_key").and_then(Value::as_str).unwrap_or("");
                        if let Some(rest) = skey.strip_prefix("verified:")
                            && !cat
                                .get("library_sync_id")
                                .and_then(Value::as_str)
                                .is_some_and(|s| !s.is_empty())
                        {
                            cat.insert("library_sync_id".into(), Value::String(rest.to_owned()));
                        }
                        baseline.push(Value::Object(cat));
                    }
                }
            }
            Ok(baseline)
        })();
        match baseline_res.and_then(|b| record_local_changes(state, &b, &games, now, device)) {
            Ok(res) => {
                if let Ok(m) = metadata_mut(state) {
                    m.remove("error");
                }
                return res;
            }
            Err(err) => {
                set_sync_error(state, &err.to_string(), "SYNC_IDENTITY_AMBIGUOUS");
                let outbox = state
                    .get(SYNC_METADATA_KEY)
                    .and_then(|m| m.get("outbox"))
                    .cloned()
                    .unwrap_or_else(|| json!([]));
                return json!({"events": [], "outbox": outbox, "changed": 0, "error": err.to_string()});
            }
        }
    }

    let has_existing = state
        .get(SYNC_METADATA_KEY)
        .map(|m| {
            let ev_nonempty = m
                .get("events")
                .and_then(Value::as_object)
                .is_some_and(|o| !o.is_empty());
            let hd_nonempty = m
                .get("heads")
                .and_then(Value::as_object)
                .is_some_and(|o| !o.is_empty());
            ev_nonempty || hd_nonempty
        })
        .unwrap_or(false);
    if has_existing {
        if let Ok(m) = metadata_mut(state) {
            m.insert("catalog_bootstrapped".into(), Value::Bool(true));
        }
        let outbox = state
            .get(SYNC_METADATA_KEY)
            .and_then(|m| m.get("outbox"))
            .cloned()
            .unwrap_or_else(|| json!([]));
        return json!({"events": [], "outbox": outbox, "changed": 0});
    }

    match record_local_changes(state, &[], &games, now, device) {
        Ok(res) => {
            if let Ok(m) = metadata_mut(state) {
                m.insert("catalog_bootstrapped".into(), Value::Bool(true));
                m.remove("error");
            }
            res
        }
        Err(err) => {
            set_sync_error(state, &err.to_string(), "SYNC_IDENTITY_AMBIGUOUS");
            json!({"events": [], "outbox": [], "changed": 0, "error": err.to_string()})
        }
    }
}

pub fn ancestors(ident: &str, events: &HashMap<String, Value>) -> HashSet<String> {
    let mut result = HashSet::new();
    let mut todo: Vec<String> = events
        .get(ident)
        .and_then(|e| e.get("parents"))
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default();
    while let Some(cur) = todo.pop() {
        if !result.insert(cur.clone()) {
            continue;
        }
        if let Some(ev) = events.get(&cur)
            && let Some(ps) = ev.get("parents").and_then(Value::as_array)
        {
            for p in ps.iter().filter_map(Value::as_str) {
                todo.push(p.to_owned());
            }
        }
    }
    result
}

pub fn is_ancestor(older: &str, newer: &str, events: &HashMap<String, Value>) -> bool {
    older == newer || ancestors(newer, events).contains(older)
}

pub fn publish_outbox(state: &mut Value, folder: &str) -> Result<Value, SyncError> {
    let metadata = metadata_mut(state)?;
    let pending = metadata
        .get("outbox")
        .and_then(Value::as_array)
        .cloned()
        .ok_or_else(|| SyncError::Validation("Library sync outbox is invalid.".to_owned()))?;
    let mut history = state_event_map(metadata)?;
    let known_vec: Vec<Value> = history.values().cloned().collect();
    let pending_checked = validate_event_set(&pending, &known_vec)?;
    for item in pending_checked {
        let eid = item["event_id"].as_str().unwrap_or("").to_owned();
        history.insert(eid, item);
    }
    let mut ordered: Vec<(usize, i64, String, Value)> = history
        .values()
        .map(|item| {
            let eid = item["event_id"].as_str().unwrap_or("").to_owned();
            let depth = ancestors(&eid, &history).len();
            let seq = item["sequence"].as_i64().unwrap_or(0);
            (depth, seq, eid, item.clone())
        })
        .collect();
    ordered.sort_by(|a, b| {
        a.0.cmp(&b.0)
            .then_with(|| a.1.cmp(&b.1))
            .then_with(|| a.2.cmp(&b.2))
    });
    for (_, _, _, item) in &ordered {
        write_event(folder, item)?;
    }
    let published = pending.len();
    metadata.insert("outbox".into(), Value::Array(Vec::new()));
    Ok(json!({
        "published": published,
        "pending": 0,
    }))
}

pub fn state_token(state: &Value) -> Result<String, SyncError> {
    let heads = state
        .get(SYNC_METADATA_KEY)
        .and_then(|m| m.get("heads"))
        .cloned()
        .unwrap_or_else(|| json!({}));
    let games_slice = state
        .get("games")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let gmap = game_map_from_slice(games_slice)?;
    let mut games_obj = Map::new();
    for (k, g) in gmap {
        games_obj.insert(k, Value::Object(build_catalog(&g)?));
    }
    Ok(sha256_canonical(&json!({
        "heads": heads,
        "games": Value::Object(games_obj),
    })))
}

fn event_catalog(ev: &Value) -> Option<Map<String, Value>> {
    if ev.get("tombstone") == Some(&Value::Bool(true)) {
        return None;
    }
    Some(
        ev.get("catalog")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default(),
    )
}

pub fn common_base_many(identifiers: &[String], events: &HashMap<String, Value>) -> Value {
    if identifiers.is_empty() {
        return json!({});
    }
    let mut common: Option<HashSet<String>> = None;
    for ident in identifiers {
        let mut anc = ancestors(ident, events);
        anc.insert(ident.clone());
        common = Some(match common {
            None => anc,
            Some(prev) => prev.intersection(&anc).cloned().collect(),
        });
    }
    let Some(common) = common else {
        return json!({});
    };
    if common.is_empty() {
        return json!({});
    }
    let mut maxima: Vec<String> = common
        .iter()
        .filter(|id| {
            !common
                .iter()
                .any(|other| other != *id && is_ancestor(id, other, events))
        })
        .cloned()
        .collect();
    if maxima.is_empty() {
        maxima = common.into_iter().collect();
    }
    maxima.sort();
    let catalogs: Vec<Value> = maxima
        .iter()
        .map(|id| {
            events
                .get(id)
                .and_then(event_catalog)
                .map(Value::Object)
                .unwrap_or_else(|| json!({}))
        })
        .collect();
    if catalogs.len() > 1 && catalogs[1..].iter().any(|c| *c != catalogs[0]) {
        return json!({"__sync_ambiguous_bases__": catalogs});
    }
    catalogs.into_iter().next().unwrap_or_else(|| json!({}))
}

fn common_base(left: Option<&str>, right: &str, events: &HashMap<String, Value>) -> Value {
    let Some(l) = left else {
        return json!({});
    };
    if !events.contains_key(l) || !events.contains_key(right) {
        return json!({});
    }
    common_base_many(&[l.to_owned(), right.to_owned()], events)
}

fn alternative_values(items: &[Value]) -> Vec<Value> {
    let mut order: Vec<Vec<u8>> = Vec::new();
    let mut grouped: HashMap<Vec<u8>, Value> = HashMap::new();
    for item in items {
        let val = item.get("value").cloned().unwrap_or(Value::Null);
        let present = item.get("present").and_then(Value::as_bool).unwrap_or(true);
        let ident = canonical_json(&json!({"present": present, "value": val}));
        if !grouped.contains_key(&ident) {
            order.push(ident.clone());
            grouped.insert(
                ident.clone(),
                json!({
                    "choice": item.get("choice").cloned().unwrap_or(Value::Null),
                    "event_ids": [],
                    "present": present,
                    "value": val,
                }),
            );
        }
        if let Some(eid) = item
            .get("event_id")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            && let Some(cur) = grouped.get_mut(&ident)
            && let Some(arr) = cur.get_mut("event_ids").and_then(Value::as_array_mut)
            && !arr.iter().any(|x| x.as_str() == Some(eid))
        {
            arr.push(Value::String(eid.to_owned()));
        }
    }
    order
        .into_iter()
        .filter_map(|k| grouped.remove(&k))
        .collect()
}

fn make_conflict_row(key: &str, field: &str, raw_items: &[Value], base: Option<Value>) -> Value {
    let mut alts = alternative_values(raw_items);
    for (idx, alt) in alts.iter_mut().enumerate() {
        let empty_choice = alt
            .get("choice")
            .and_then(Value::as_str)
            .is_none_or(str::is_empty);
        if empty_choice {
            let label = match idx {
                0 => "local".to_owned(),
                1 => "remote".to_owned(),
                n => format!("alternative:{n}"),
            };
            if let Some(m) = alt.as_object_mut() {
                m.insert("choice".into(), Value::String(label));
            }
        }
    }
    let identity: Vec<Value> = alts
        .iter()
        .map(|a| {
            json!({
                "choice": a.get("choice").cloned().unwrap_or(Value::Null),
                "event_ids": a.get("event_ids").cloned().unwrap_or_else(|| json!([])),
                "present": a.get("present").and_then(Value::as_bool).unwrap_or(true),
                "value": a.get("value").cloned().unwrap_or(Value::Null),
            })
        })
        .collect();
    let digest = sha256_canonical(&json!({
        "sync_key": key,
        "field": field,
        "alternatives": identity,
    }));
    let mut row = Map::new();
    row.insert("field".into(), Value::String(field.to_owned()));
    if let Some(first) = alts.first() {
        row.insert(
            "local".into(),
            first.get("value").cloned().unwrap_or(Value::Null),
        );
    }
    if let Some(second) = alts.get(1) {
        row.insert(
            "remote".into(),
            second.get("value").cloned().unwrap_or(Value::Null),
        );
    }
    if let Some(b) = base {
        row.insert("base".into(), b);
    }
    row.insert("alternatives".into(), Value::Array(alts));
    row.insert(
        "conflict_id".into(),
        Value::String(format!("sync-conflict:{digest}")),
    );
    Value::Object(row)
}

fn selected_alternative(conflicts: Option<&Value>, key: &str, row: &Value) -> Option<Value> {
    let val = conflicts?.as_object()?.get(key)?;
    let field = row.get("field").and_then(Value::as_str).unwrap_or("");
    let cid = row.get("conflict_id").and_then(Value::as_str);
    let selected = if let Some(obj) = val.as_object() {
        cid.and_then(|c| obj.get(c))
            .or_else(|| obj.get(field))
            .and_then(Value::as_str)
    } else if matches!(field, "*" | "__record__") {
        val.as_str()
    } else {
        None
    }?;
    for alt in row.get("alternatives").and_then(Value::as_array)? {
        if alt.get("choice").and_then(Value::as_str) == Some(selected)
            || alt
                .get("event_ids")
                .and_then(Value::as_array)
                .is_some_and(|ids| ids.iter().any(|x| x.as_str() == Some(selected)))
        {
            return Some(alt.clone());
        }
    }
    None
}

fn merge_remote_heads(
    key: &str,
    head_ids: &[String],
    events: &HashMap<String, Value>,
    choices: Option<&Value>,
) -> (Option<Map<String, Value>>, Vec<Value>) {
    if head_ids.is_empty() {
        return (None, Vec::new());
    }
    let mut dedup_ids = Vec::new();
    for id in head_ids {
        if !dedup_ids.contains(id) {
            dedup_ids.push(id.clone());
        }
    }
    let mut candidates = Vec::new();
    for (idx, hid) in dedup_ids.iter().enumerate() {
        let Some(ev) = events.get(hid) else {
            continue;
        };
        let choice = match idx {
            0 => "local".to_owned(),
            1 => "remote".to_owned(),
            n => format!("alternative:{n}"),
        };
        let cat = event_catalog(ev);
        let present = ev.get("tombstone") != Some(&Value::Bool(true));
        candidates.push(json!({
            "event_id": hid,
            "choice": choice,
            "value": cat.map(Value::Object).unwrap_or(Value::Null),
            "present": present,
        }));
    }
    let rec_alts = alternative_values(&candidates);
    if rec_alts.len() > 1
        && rec_alts
            .iter()
            .any(|a| !a.get("present").and_then(Value::as_bool).unwrap_or(true))
    {
        let row = make_conflict_row(key, "__record__", &candidates, None);
        if let Some(sel) = selected_alternative(choices, key, &row) {
            let val = if sel.get("present").and_then(Value::as_bool).unwrap_or(true) {
                sel.get("value").and_then(Value::as_object).cloned()
            } else {
                None
            };
            return (val, Vec::new());
        }
        return (
            candidates
                .first()
                .and_then(|c| c.get("value"))
                .and_then(Value::as_object)
                .cloned(),
            vec![row],
        );
    }
    if rec_alts.len() == 1 {
        let val = if rec_alts[0]
            .get("present")
            .and_then(Value::as_bool)
            .unwrap_or(true)
        {
            rec_alts[0].get("value").and_then(Value::as_object).cloned()
        } else {
            None
        };
        return (val, Vec::new());
    }

    let base = common_base_many(&dedup_ids, events);
    let ambiguous_bases = base
        .get("__sync_ambiguous_bases__")
        .and_then(Value::as_array)
        .cloned();
    let mut all_fields: BTreeSet<String> = BTreeSet::new();
    if let Some(bm) = base.as_object() {
        for k in bm.keys() {
            if k != "__sync_ambiguous_bases__" {
                all_fields.insert(k.clone());
            }
        }
    }
    for c in &candidates {
        if let Some(cm) = c.get("value").and_then(Value::as_object) {
            all_fields.extend(cm.keys().cloned());
        }
    }
    let mut merged = Map::new();
    let mut conflict_rows = Vec::new();
    for field in all_fields {
        let mut values = Vec::new();
        for c in &candidates {
            let cm = c.get("value").and_then(Value::as_object);
            let present = cm.is_some_and(|m| m.contains_key(&field));
            let val = cm
                .and_then(|m| m.get(&field))
                .cloned()
                .unwrap_or(Value::Null);
            values.push(json!({
                "event_id": c["event_id"],
                "choice": c["choice"],
                "present": present,
                "value": val,
            }));
        }
        let alts = alternative_values(&values);
        if alts.len() == 1 {
            if alts[0]
                .get("present")
                .and_then(Value::as_bool)
                .unwrap_or(true)
            {
                merged.insert(field, alts[0].get("value").cloned().unwrap_or(Value::Null));
            }
            continue;
        }
        let base_values: Vec<(bool, Value)> = if let Some(ref amb) = ambiguous_bases {
            amb.iter()
                .map(|b| {
                    let bm = b.as_object();
                    (
                        bm.is_some_and(|m| m.contains_key(&field)),
                        bm.and_then(|m| m.get(&field))
                            .cloned()
                            .unwrap_or(Value::Null),
                    )
                })
                .collect()
        } else {
            let bm = base.as_object();
            vec![(
                bm.is_some_and(|m| m.contains_key(&field)),
                bm.and_then(|m| m.get(&field))
                    .cloned()
                    .unwrap_or(Value::Null),
            )]
        };
        let changed: Vec<&Value> = values
            .iter()
            .filter(|v| {
                let vp = v.get("present").and_then(Value::as_bool).unwrap_or(false);
                let vv = v.get("value").cloned().unwrap_or(Value::Null);
                base_values.iter().any(|(bp, bv)| vp != *bp || vv != *bv)
            })
            .collect();
        if changed.len() == 1 && changed.len() < values.len() {
            if changed[0]
                .get("present")
                .and_then(Value::as_bool)
                .unwrap_or(false)
            {
                merged.insert(
                    field,
                    changed[0].get("value").cloned().unwrap_or(Value::Null),
                );
            }
            continue;
        }
        let base_val = if ambiguous_bases.is_some() {
            Some(Value::Array(
                base_values
                    .iter()
                    .map(|(p, v)| if *p { v.clone() } else { Value::Null })
                    .collect(),
            ))
        } else {
            base.get(&field).cloned()
        };
        let row = make_conflict_row(key, &field, &values, base_val);
        if let Some(sel) = selected_alternative(choices, key, &row) {
            if sel.get("present").and_then(Value::as_bool).unwrap_or(true) {
                merged.insert(field, sel.get("value").cloned().unwrap_or(Value::Null));
            }
        } else {
            if alts[0]
                .get("present")
                .and_then(Value::as_bool)
                .unwrap_or(true)
            {
                merged.insert(field, alts[0].get("value").cloned().unwrap_or(Value::Null));
            }
            conflict_rows.push(row);
        }
    }
    (Some(merged), conflict_rows)
}

fn merge_catalogs(
    key: &str,
    local: Option<&Map<String, Value>>,
    remote: Option<&Map<String, Value>>,
    base: &Value,
    conflicts: Option<&Value>,
    local_deleted: bool,
) -> (Option<Map<String, Value>>, Vec<Value>) {
    if local.is_none() && !local_deleted {
        return (remote.cloned(), Vec::new());
    }
    if local.is_none() || remote.is_none() {
        if local == remote {
            return (local.cloned(), Vec::new());
        }
        let items = vec![
            json!({"choice": "local", "present": local.is_some(), "value": local.cloned().map(Value::Object).unwrap_or(Value::Null)}),
            json!({"choice": "remote", "present": remote.is_some(), "value": remote.cloned().map(Value::Object).unwrap_or(Value::Null)}),
        ];
        let row = make_conflict_row(key, "__record__", &items, Some(base.clone()));
        if let Some(sel) = selected_alternative(conflicts, key, &row) {
            let val = if sel.get("present").and_then(Value::as_bool).unwrap_or(true) {
                sel.get("value").and_then(Value::as_object).cloned()
            } else {
                None
            };
            return (val, Vec::new());
        }
        return (local.cloned(), vec![row]);
    }
    let local_m = local.unwrap();
    let remote_m = remote.unwrap();
    if let Some(amb) = base
        .get("__sync_ambiguous_bases__")
        .and_then(Value::as_array)
    {
        let mut canonical_base = Map::new();
        let mut uncertain = BTreeSet::new();
        let mut all_f = BTreeSet::new();
        for variant in amb {
            if let Some(vm) = variant.as_object() {
                all_f.extend(vm.keys().cloned());
            }
        }
        for field in all_f {
            let vals: Vec<Option<&Value>> = amb
                .iter()
                .filter_map(Value::as_object)
                .map(|m| m.get(&field))
                .collect();
            if !vals.is_empty() && vals[1..].iter().all(|v| *v == vals[0]) {
                if let Some(v) = vals[0] {
                    canonical_base.insert(field, v.clone());
                }
            } else {
                uncertain.insert(field);
            }
        }
        let (merged_opt, mut rows) = merge_catalogs(
            key,
            Some(local_m),
            Some(remote_m),
            &Value::Object(canonical_base),
            conflicts,
            local_deleted,
        );
        let mut merged = merged_opt.unwrap_or_else(|| local_m.clone());
        for field in uncertain {
            let lv = local_m.get(&field).cloned().unwrap_or(Value::Null);
            let rv = remote_m.get(&field).cloned().unwrap_or(Value::Null);
            if lv == rv {
                continue;
            }
            let base_arr: Vec<Value> = amb
                .iter()
                .filter_map(Value::as_object)
                .map(|m| m.get(&field).cloned().unwrap_or(Value::Null))
                .collect();
            let row = make_conflict_row(
                key,
                &field,
                &[
                    json!({"choice": "local", "present": local_m.contains_key(&field), "value": lv}),
                    json!({"choice": "remote", "present": remote_m.contains_key(&field), "value": rv}),
                ],
                Some(Value::Array(base_arr)),
            );
            if let Some(sel) = selected_alternative(conflicts, key, &row) {
                if sel.get("present").and_then(Value::as_bool).unwrap_or(true) {
                    merged.insert(field, sel.get("value").cloned().unwrap_or(Value::Null));
                } else {
                    merged.remove(&field);
                }
            } else {
                rows.push(row);
            }
        }
        return (Some(merged), rows);
    }

    let empty = Map::new();
    let base_m = base.as_object().unwrap_or(&empty);
    let mut merged = local_m.clone();
    let mut conflict_rows = Vec::new();
    let mut all_fields: BTreeSet<String> = base_m.keys().cloned().collect();
    all_fields.extend(local_m.keys().cloned());
    all_fields.extend(remote_m.keys().cloned());

    for field in all_fields {
        let lv = local_m.get(&field).cloned().unwrap_or(Value::Null);
        let rv = remote_m.get(&field).cloned().unwrap_or(Value::Null);
        let bv = base_m.get(&field).cloned().unwrap_or(Value::Null);
        let l_changed = lv != bv;
        let r_changed = rv != bv;
        if l_changed && r_changed && lv != rv {
            let row = make_conflict_row(
                key,
                &field,
                &[
                    json!({"choice": "local", "present": local_m.contains_key(&field), "value": lv}),
                    json!({"choice": "remote", "present": remote_m.contains_key(&field), "value": rv}),
                ],
                Some(bv),
            );
            if let Some(sel) = selected_alternative(conflicts, key, &row) {
                if sel.get("present").and_then(Value::as_bool).unwrap_or(true) {
                    merged.insert(field, sel.get("value").cloned().unwrap_or(Value::Null));
                } else {
                    merged.remove(&field);
                }
            } else {
                conflict_rows.push(row);
            }
        } else if r_changed {
            if let Some(rv_present) = remote_m.get(&field) {
                merged.insert(field, rv_present.clone());
            } else {
                merged.remove(&field);
            }
        }
    }
    (Some(merged), conflict_rows)
}

fn incoming_heads(events: &HashMap<String, Value>, key: &str) -> Vec<String> {
    let ids: Vec<String> = events
        .iter()
        .filter(|(_, ev)| ev.get("sync_key").and_then(Value::as_str) == Some(key))
        .map(|(id, _)| id.clone())
        .collect();
    let mut heads: Vec<String> = ids
        .iter()
        .filter(|id| {
            !ids.iter()
                .any(|other| other != *id && ancestors(other, events).contains(*id))
        })
        .cloned()
        .collect();
    heads.sort();
    heads
}

pub fn preview_sync(
    state: &Value,
    incoming: &[Value],
    conflicts: Option<&Value>,
) -> Result<Value, SyncError> {
    let empty = Map::new();
    let metadata = state
        .get(SYNC_METADATA_KEY)
        .and_then(Value::as_object)
        .unwrap_or(&empty);
    let known = state_event_map(metadata)?;
    let known_vec: Vec<Value> = known.values().cloned().collect();
    let validated = validate_event_set(incoming, &known_vec)?;
    let mut combined = known;
    for ev in &validated {
        let id = ev["event_id"].as_str().unwrap_or("").to_owned();
        combined.insert(id, ev.clone());
    }
    let games_slice = state
        .get("games")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let local_games = game_map_from_slice(games_slice)?;
    let heads = metadata.get("heads").and_then(Value::as_object);
    let pending = metadata.get("pending_conflicts").and_then(Value::as_object);

    let mut history_keys: HashSet<String> = HashSet::new();
    if let Some(h) = heads {
        history_keys.extend(h.keys().cloned());
    }
    if let Some(p) = pending {
        history_keys.extend(p.keys().cloned());
    }
    let mut keys: BTreeSet<String> = local_games.keys().cloned().collect();
    for ev in &validated {
        if let Some(sk) = ev.get("sync_key").and_then(Value::as_str) {
            keys.insert(sk.to_owned());
        }
    }
    for ev in combined.values() {
        if let Some(sk) = ev.get("sync_key").and_then(Value::as_str)
            && history_keys.contains(sk)
        {
            keys.insert(sk.to_owned());
        }
    }
    if let Some(p) = pending {
        keys.extend(p.keys().cloned());
    }

    let mut changes = Vec::new();
    for key in keys {
        let local_game = local_games.get(&key);
        let local_catalog = match local_game {
            Some(g) => Some(build_catalog(g)?),
            None => None,
        };
        let local_heads: Vec<String> = heads
            .and_then(|h| h.get(&key))
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .map(str::to_owned)
                    .collect()
            })
            .unwrap_or_default();
        let mut remote_ids: Vec<String> = incoming_heads(&combined, &key)
            .into_iter()
            .filter(|id| {
                !local_heads.contains(id)
                    && !local_heads.iter().any(|lh| is_ancestor(id, lh, &combined))
            })
            .collect();
        if remote_ids.is_empty() {
            continue;
        }
        remote_ids.sort();
        let remote_id = remote_ids.last().cloned().unwrap_or_default();
        let (remote_catalog, remote_conflicts) =
            merge_remote_heads(&key, &remote_ids, &combined, conflicts);
        let local_id = local_heads.last().map(String::as_str);

        let (merged, conflict_rows, mode) = if let Some(lid) = local_id
            && remote_ids
                .iter()
                .all(|rid| is_ancestor(lid, rid, &combined))
        {
            let mode = if !remote_conflicts.is_empty() {
                "conflict"
            } else if remote_catalog.is_none() {
                "delete"
            } else if local_game.is_none() {
                "add"
            } else {
                "update"
            };
            (remote_catalog.clone(), remote_conflicts, mode)
        } else if let Some(lid) = local_id
            && remote_ids
                .iter()
                .all(|rid| is_ancestor(rid, lid, &combined))
        {
            continue;
        } else if let Some(lid) = local_id
            && remote_ids.len() > 1
        {
            let mut cand_ids = vec![lid.to_owned()];
            for r in &remote_ids {
                if !cand_ids.contains(r) {
                    cand_ids.push(r.clone());
                }
            }
            let (m, crows) = merge_remote_heads(&key, &cand_ids, &combined, conflicts);
            let mode = if !crows.is_empty() {
                "conflict"
            } else if m.is_none() {
                "delete"
            } else if local_game.is_none() {
                "add"
            } else {
                "merge"
            };
            (m, crows, mode)
        } else {
            let base = if local_id.is_some() {
                common_base(local_id, &remote_ids[0], &combined)
            } else {
                json!({})
            };
            let local_deleted = local_id
                .and_then(|lid| combined.get(lid))
                .and_then(|ev| ev.get("tombstone"))
                .and_then(Value::as_bool)
                .unwrap_or(false);
            let (m, mut crows) = merge_catalogs(
                &key,
                local_catalog.as_ref(),
                remote_catalog.as_ref(),
                &base,
                conflicts,
                local_deleted,
            );
            let mut combined_c = remote_conflicts;
            combined_c.append(&mut crows);
            let mode = if !combined_c.is_empty() {
                "conflict"
            } else if m.is_none() {
                "delete"
            } else if local_game.is_none() {
                "add"
            } else {
                "merge"
            };
            (m, combined_c, mode)
        };

        changes.push(json!({
            "sync_key": key,
            "event_id": remote_id,
            "event_ids": remote_ids,
            "action": mode,
            "local": local_catalog.map(Value::Object).unwrap_or(Value::Null),
            "remote": remote_catalog.map(Value::Object).unwrap_or(Value::Null),
            "merged": merged.map(Value::Object).unwrap_or(Value::Null),
            "conflicts": conflict_rows,
        }));
    }

    let flat_conflicts: Vec<Value> = changes
        .iter()
        .filter_map(|c| c.get("conflicts").and_then(Value::as_array))
        .flatten()
        .cloned()
        .collect();
    let additions = changes.iter().filter(|c| c["action"] == "add").count();
    let updates = changes
        .iter()
        .filter(|c| c["action"] == "update" || c["action"] == "merge")
        .count();
    let deletions = changes.iter().filter(|c| c["action"] == "delete").count();
    let conflict_count = changes
        .iter()
        .filter(|c| {
            c.get("conflicts")
                .and_then(Value::as_array)
                .is_some_and(|a| !a.is_empty())
        })
        .count();
    let event_ids: Vec<Value> = validated.iter().map(|ev| ev["event_id"].clone()).collect();
    let events_token = sha256_canonical(&Value::Array(validated.clone()));

    Ok(json!({
        "format": SYNC_FORMAT,
        "source_token": state_token(state)?,
        "event_ids": event_ids,
        "events": validated,
        "events_token": events_token,
        "changes": changes,
        "conflicts": flat_conflicts,
        "pending_conflicts": pending.cloned().map(Value::Object).unwrap_or_else(|| json!({})),
        "counts": {
            "additions": additions,
            "updates": updates,
            "deletions": deletions,
            "conflicts": conflict_count,
        },
    }))
}

pub fn replace_catalog(local: Option<&Value>, catalog: Option<&Value>) -> Option<Value> {
    let cat_obj = catalog?.as_object()?;
    let mut result = local
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let local_id = result
        .get("game_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(str::to_owned);
    for field in CATALOG_FIELDS {
        if *field == "game_id" && local_id.is_some() {
            continue;
        }
        if let Some(v) = cat_obj.get(*field) {
            result.insert((*field).into(), v.clone());
        } else {
            result.remove(*field);
        }
    }
    Some(Value::Object(result))
}

pub fn apply_sync(
    state: &mut Value,
    plan: &Value,
    conflicts: Option<&Value>,
    now: Option<&str>,
) -> Result<Value, SyncError> {
    if !plan.is_object() || plan.get("format").and_then(Value::as_i64) != Some(SYNC_FORMAT) {
        return Err(SyncError::Validation("Sync preview is invalid.".to_owned()));
    }
    let cur_token = state_token(state)?;
    if plan.get("source_token").and_then(Value::as_str) != Some(cur_token.as_str()) {
        return Err(SyncError::Stale(
            "Library sync preview is stale; review incoming changes again.".to_owned(),
        ));
    }
    let incoming = plan
        .get("events")
        .and_then(Value::as_array)
        .ok_or_else(|| SyncError::Validation("Sync preview events are invalid.".to_owned()))?;
    let known = {
        let meta = metadata_mut(state)?;
        state_event_map(meta)?
    };
    let known_vec: Vec<Value> = known.values().cloned().collect();
    let validated = validate_event_set(incoming, &known_vec)?;
    let mut supplied = Vec::with_capacity(incoming.len());
    for item in incoming {
        supplied.push(validate_event(item)?);
    }
    let supplied_ids: Vec<Value> = supplied.iter().map(|e| e["event_id"].clone()).collect();
    if plan.get("event_ids").and_then(Value::as_array) != Some(&supplied_ids) {
        return Err(SyncError::Validation(
            "Sync preview event identities do not match its events.".to_owned(),
        ));
    }
    let events_token = sha256_canonical(&Value::Array(supplied.clone()));
    if let Some(ptok) = plan.get("events_token").and_then(Value::as_str)
        && ptok != events_token
    {
        return Err(SyncError::Validation(
            "Sync preview events were changed after review.".to_owned(),
        ));
    }

    let reviewed = preview_sync(state, &supplied, conflicts)?;
    {
        let meta = metadata_mut(state)?;
        for item in &validated {
            append_event(meta, item, false)?;
        }
    }
    let local_device = ensure_device_id(state)?;
    let changes = reviewed
        .get("changes")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();

    let mut applied = Vec::new();
    let mut unresolved = Vec::new();
    for change in changes {
        let Some(key) = change.get("sync_key").and_then(Value::as_str) else {
            continue;
        };
        let conflict_rows = change
            .get("conflicts")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let remote_ids: Vec<String> = change
            .get("event_ids")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .map(str::to_owned)
                    .collect()
            })
            .filter(|v: &Vec<String>| !v.is_empty())
            .unwrap_or_else(|| {
                change
                    .get("event_id")
                    .and_then(Value::as_str)
                    .map(|s| vec![s.to_owned()])
                    .unwrap_or_default()
            });

        if !conflict_rows.is_empty() {
            for mut row in conflict_rows.clone() {
                if let Some(m) = row.as_object_mut() {
                    m.insert("sync_key".into(), Value::String(key.to_owned()));
                }
                unresolved.push(row);
            }
            let meta = metadata_mut(state)?;
            let pc = meta
                .entry("pending_conflicts".to_owned())
                .or_insert_with(|| Value::Object(Map::new()));
            if let Some(pcm) = pc.as_object_mut() {
                pcm.insert(
                    key.to_owned(),
                    json!({"event_ids": remote_ids, "conflicts": conflict_rows}),
                );
            }
            continue;
        }

        let local_idx = state
            .get("games")
            .and_then(Value::as_array)
            .and_then(|games| {
                games
                    .iter()
                    .position(|g| stable_sync_key(g).ok().as_deref() == Some(key))
            });
        if local_idx.is_none() && remote_ids.is_empty() {
            continue;
        }
        let local_head: Option<String> = state
            .get(SYNC_METADATA_KEY)
            .and_then(|m| m.get("heads"))
            .and_then(|h| h.get(key))
            .and_then(Value::as_array)
            .and_then(|a| a.last())
            .and_then(Value::as_str)
            .map(str::to_owned);
        let mut parent_ids = Vec::new();
        if let Some(lh) = local_head {
            parent_ids.push(lh);
        }
        for rid in &remote_ids {
            if !parent_ids.contains(rid) {
                parent_ids.push(rid.clone());
            }
        }
        let merged = change.get("merged").filter(|v| !v.is_null());
        if merged.is_none() {
            if let Some(idx) = local_idx
                && let Some(games) = state.get_mut("games").and_then(Value::as_array_mut)
            {
                games.remove(idx);
            }
            let meta = metadata_mut(state)?;
            if parent_ids.len() > 1 {
                let seq = next_sequence(meta);
                let res = make_event(&local_device, seq, key, &parent_ids, None, true, now)?;
                let eid = res["event_id"].as_str().unwrap_or("").to_owned();
                append_event(meta, &res, true)?;
                if let Some(h) = meta.get_mut("heads").and_then(Value::as_object_mut) {
                    h.insert(key.to_owned(), json!([eid]));
                }
            } else if let Some(h) = meta.get_mut("heads").and_then(Value::as_object_mut) {
                let arr = remote_ids
                    .last()
                    .map(|id| json!([id]))
                    .unwrap_or_else(|| json!([]));
                h.insert(key.to_owned(), arr);
            }
            if let Some(pc) = meta
                .get_mut("pending_conflicts")
                .and_then(Value::as_object_mut)
            {
                pc.remove(key);
            }
            applied.push(key.to_owned());
            continue;
        }

        let local_game = local_idx.and_then(|i| {
            state
                .get("games")
                .and_then(Value::as_array)
                .and_then(|g| g.get(i))
        });
        let Some(mut replacement) = replace_catalog(local_game, merged) else {
            continue;
        };
        if let Some(rest) = key.strip_prefix("verified:")
            && let Some(rm) = replacement.as_object_mut()
            && !rm
                .get("library_sync_id")
                .and_then(Value::as_str)
                .is_some_and(|s| !s.is_empty())
        {
            rm.insert("library_sync_id".into(), Value::String(rest.to_owned()));
        }
        if let Some(games) = state.get_mut("games").and_then(Value::as_array_mut) {
            if let Some(idx) = local_idx {
                games[idx] = replacement;
            } else {
                games.push(replacement);
            }
        }
        let meta = metadata_mut(state)?;
        if parent_ids.len() > 1 {
            let seq = next_sequence(meta);
            let res = make_event(&local_device, seq, key, &parent_ids, merged, false, now)?;
            let eid = res["event_id"].as_str().unwrap_or("").to_owned();
            append_event(meta, &res, true)?;
            if let Some(h) = meta.get_mut("heads").and_then(Value::as_object_mut) {
                h.insert(key.to_owned(), json!([eid]));
            }
        } else if let Some(h) = meta.get_mut("heads").and_then(Value::as_object_mut) {
            let arr = remote_ids
                .last()
                .map(|id| json!([id]))
                .unwrap_or_else(|| json!([]));
            h.insert(key.to_owned(), arr);
        }
        if let Some(pc) = meta
            .get_mut("pending_conflicts")
            .and_then(Value::as_object_mut)
        {
            pc.remove(key);
        }
        applied.push(key.to_owned());
    }

    Ok(json!({
        "games": state.get("games").cloned().unwrap_or_else(|| json!([])),
        "applied": applied.len(),
        "conflicts": unresolved,
        "changed": !applied.is_empty(),
    }))
}
