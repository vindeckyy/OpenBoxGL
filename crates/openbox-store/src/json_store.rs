//! Canonical transactional, process-safe JSON state store mirroring `state_store.py`.
//!
//! Guarantees byte-level on-disk compatibility with Python `JsonStateStore` (schema v7,
//! `@library` portable tokens, `sessions.json` mirror, `.bak` separate-inode copy before
//! primary replace, hard-link snapshots ordered by `(timestamp, seq)`, 16-byte BLAKE2b
//! no-op digest skip, 50ms write coalescing, and ADR-0007 lock ordering).

use std::collections::{HashMap, HashSet};
use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, LazyLock};
use std::time::{Duration, Instant, UNIX_EPOCH};

use blake2::digest::consts::U16;
use blake2::{Blake2b, Digest};
use openbox_core::{
    APP_VERSION, OrderedMutex, PATH_MODES, SESSIONS_FILE_NAME, SESSIONS_FORMAT, STATE_FORMAT,
    STATE_SCHEMA_VERSION, default_state, is_legacy_indexed_id, is_valid_library_id, lock_level,
    new_library_id, prune_unknown_settings, stable_game_id,
};
use openbox_io::{atomic_write_text, fsync_directory, set_mode};
use openbox_platform::{DEFAULT_LOCK_TIMEOUT, file_lock};
use rand::RngCore;
use regex::Regex;
use serde_json::{Map, Value, json};
use tempfile::Builder;
use thiserror::Error;

use crate::library_v7::{encode_games, expand_games, finish_interrupted_migration};

pub const COMPACT_JSON_THRESHOLD: usize = 1024 * 1024;
pub const QUEUE_CAP: usize = 500;
pub const NOTIFICATIONS_CAP: usize = 200;
pub const TRASH_CAP: usize = 200;
pub const TRASH_MAX_AGE_DAYS: i64 = 30;
pub const WRITE_COALESCE_MS: u64 = 50;

static SNAPSHOT_NAME_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^(\d{8}T\d{12}Z)-(\d{8})-[0-9a-f]{8}\.json$").expect("valid snapshot re")
});

static LIBRARY_FROZEN: AtomicBool = AtomicBool::new(false);

pub fn freeze_library_writes() {
    LIBRARY_FROZEN.store(true, Ordering::SeqCst);
}

pub fn is_library_frozen() -> bool {
    LIBRARY_FROZEN.load(Ordering::SeqCst)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct FileSignature {
    pub mtime_ns: u128,
    pub size: u64,
    pub ino: u64,
}

#[derive(Debug, Error)]
pub enum StateError {
    #[error("{0}")]
    Corrupt(String),
    #[error("{0}")]
    FutureVersion(String),
    #[error("{0}")]
    SchemaTooOld(String),
    #[error("{0}")]
    Migration(String),
    #[error("The library was rolled back; restart OpenBox before it is written again.")]
    Frozen,
    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),
}

impl StateError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::Corrupt(_) => "STATE_CORRUPT",
            Self::FutureVersion(_) => "STATE_FUTURE_VERSION",
            Self::SchemaTooOld(_) => "STATE_SCHEMA_TOO_OLD",
            Self::Migration(_) => "STATE_MIGRATION_FAILED",
            Self::Frozen => "LIBRARY_FROZEN",
            Self::Io(_) => "STATE_CORRUPT",
        }
    }
}

pub fn schema_too_old_detail(version: i64) -> String {
    if version == 6 {
        "This library is schema 6. OpenBox 3.0 does not upgrade it on startup. Run openbox --migrate-library to upgrade it. The file was not changed.".to_owned()
    } else {
        format!(
            "This library is schema {version}. OpenBox 3.0 does not upgrade libraries older than schema 6. Open it with OpenBox 2.1 first. The file was not changed."
        )
    }
}

pub fn raw_schema_version(raw: &Value) -> i64 {
    if raw.is_array() {
        return 1;
    }
    let Some(obj) = raw.as_object() else {
        return 0;
    };
    match obj.get("schema_version") {
        None => 1,
        Some(Value::Number(n)) => n.as_i64().unwrap_or(1),
        Some(Value::String(s)) => s.trim().parse::<i64>().unwrap_or(1),
        _ => 1,
    }
}

fn parse_trash_timestamp(raw: &str) -> Option<i64> {
    let text = raw.trim();
    if text.is_empty() {
        return None;
    }
    if let Ok(ts) = text.parse::<jiff::Timestamp>() {
        return Some(ts.as_second());
    }
    if let Ok(ts) = format!("{text}Z").parse::<jiff::Timestamp>() {
        return Some(ts.as_second());
    }
    None
}

pub fn prune_trash(trash: &[Value], now_secs: Option<i64>) -> Vec<Value> {
    let now = now_secs.unwrap_or_else(|| jiff::Timestamp::now().as_second());
    let cutoff = now - TRASH_MAX_AGE_DAYS * 86_400;
    let kept: Vec<Value> = trash
        .iter()
        .filter(|entry| {
            let Some(obj) = entry.as_object() else {
                return false;
            };
            if let Some(raw) = obj.get("trashed_at").and_then(Value::as_str)
                && let Some(ts) = parse_trash_timestamp(raw)
                && ts < cutoff
            {
                return false;
            }
            true
        })
        .cloned()
        .collect();
    if kept.len() > TRASH_CAP {
        kept[kept.len() - TRASH_CAP..].to_vec()
    } else {
        kept
    }
}

fn path_settings(state: &Map<String, Value>) -> (String, String) {
    let settings = state.get("settings").and_then(Value::as_object);
    let mode = settings
        .and_then(|s| s.get("path_mode"))
        .and_then(Value::as_str)
        .filter(|m| PATH_MODES.contains(m))
        .unwrap_or("absolute")
        .to_owned();
    let root = settings
        .and_then(|s| s.get("library_root"))
        .and_then(Value::as_str)
        .filter(|r| Path::new(r).is_absolute())
        .unwrap_or("")
        .to_owned();
    (mode, root)
}

pub fn sync_envelope(state: &mut Map<String, Value>) -> bool {
    let mut changed = false;
    if state.get("format").and_then(Value::as_str) != Some(STATE_FORMAT) {
        state.insert("format".into(), Value::String(STATE_FORMAT.to_owned()));
        changed = true;
    }
    let valid_id = state
        .get("library_id")
        .and_then(Value::as_str)
        .is_some_and(is_valid_library_id);
    if !valid_id {
        state.insert("library_id".into(), Value::String(new_library_id()));
        changed = true;
    }
    let has_created = state
        .get("created_with")
        .and_then(Value::as_str)
        .is_some_and(|s| !s.trim().is_empty());
    if !has_created {
        state.insert("created_with".into(), Value::String(APP_VERSION.to_owned()));
        changed = true;
    }
    let (mode, root) = path_settings(state);
    if state.get("path_mode").and_then(Value::as_str) != Some(mode.as_str()) {
        state.insert("path_mode".into(), Value::String(mode));
        changed = true;
    }
    if state.get("library_root").and_then(Value::as_str) != Some(root.as_str()) {
        state.insert("library_root".into(), Value::String(root));
        changed = true;
    }
    changed
}

fn normalize_game_ids(games: &mut [Value]) -> bool {
    // Fast path: check if every game already has a unique, trimmed, non-legacy game_id
    // and valid legacy_game_ids without allocating any String.
    {
        let mut seen: HashSet<&str> = HashSet::with_capacity(games.len());
        let mut all_clean = true;
        for game in games.iter() {
            let Some(obj) = game.as_object() else {
                continue;
            };
            if let Some(legacy) = obj.get("legacy_game_ids")
                && !legacy.is_array()
            {
                all_clean = false;
                break;
            }
            let Some(gid) = obj.get("game_id").and_then(Value::as_str) else {
                all_clean = false;
                break;
            };
            if gid.is_empty() || gid.trim() != gid || is_legacy_indexed_id(gid) || !seen.insert(gid)
            {
                all_clean = false;
                break;
            }
        }
        if all_clean {
            return false;
        }
    }

    let mut changed = false;
    let mut used: HashSet<String> = HashSet::with_capacity(games.len());
    for game in games.iter_mut() {
        let Some(obj) = game.as_object_mut() else {
            continue;
        };
        if let Some(legacy) = obj.get("legacy_game_ids")
            && !legacy.is_array()
        {
            obj.insert("legacy_game_ids".into(), Value::Array(Vec::new()));
            changed = true;
        }
        let existing = obj
            .get("game_id")
            .and_then(Value::as_str)
            .map(|s| s.trim().to_owned())
            .unwrap_or_default();
        let mut candidate = if existing.is_empty() || is_legacy_indexed_id(&existing) {
            let id = stable_game_id(obj);
            if !existing.is_empty() {
                let entry = obj
                    .entry("legacy_game_ids".to_owned())
                    .or_insert_with(|| Value::Array(Vec::new()));
                if !entry.is_array() {
                    *entry = Value::Array(Vec::new());
                }
                if let Some(arr) = entry.as_array_mut()
                    && !arr.iter().any(|v| v.as_str() == Some(existing.as_str()))
                {
                    arr.push(Value::String(existing.clone()));
                    changed = true;
                }
            }
            id
        } else {
            existing
        };
        if used.contains(&candidate) {
            let base = candidate.clone();
            let mut suffix = 2usize;
            while used.contains(&format!("{base}-{suffix}")) {
                suffix += 1;
            }
            candidate = format!("{base}-{suffix}");
        }
        if obj.get("game_id").and_then(Value::as_str) != Some(candidate.as_str()) {
            obj.insert("game_id".into(), Value::String(candidate.clone()));
            changed = true;
        }
        used.insert(candidate);
    }
    changed
}

fn normalize_feature_fields(state: &mut Map<String, Value>) -> bool {
    let mut changed = false;
    for (key, cap) in [("queue", QUEUE_CAP), ("notifications", NOTIFICATIONS_CAP)] {
        match state.get_mut(key) {
            Some(Value::Array(arr)) => {
                if arr.len() > cap {
                    arr.truncate(cap);
                    changed = true;
                }
            }
            _ => {
                state.insert(key.into(), Value::Array(Vec::new()));
                changed = true;
            }
        }
    }
    match state.get("trash").and_then(Value::as_array) {
        Some(arr) => {
            let kept = prune_trash(arr, None);
            if kept.len() != arr.len() {
                state.insert("trash".into(), Value::Array(kept));
                changed = true;
            }
        }
        None => {
            state.insert("trash".into(), Value::Array(Vec::new()));
            changed = true;
        }
    }
    match state.get("active_sessions").and_then(Value::as_array) {
        Some(arr) => {
            let valid: Vec<Value> = arr.iter().filter(|v| v.is_object()).cloned().collect();
            if valid.len() != arr.len() {
                state.insert("active_sessions".into(), Value::Array(valid));
                changed = true;
            }
        }
        None => {
            state.insert("active_sessions".into(), Value::Array(Vec::new()));
            changed = true;
        }
    }
    for key in ["emulator_settings", "achievement_overrides"] {
        if !state.get(key).is_some_and(Value::is_object) {
            state.insert(key.into(), Value::Object(Map::new()));
            changed = true;
        }
    }
    if !state.get("party_decks").is_some_and(Value::is_array) {
        state.insert("party_decks".into(), Value::Array(Vec::new()));
        changed = true;
    }
    // Additive 2.2 fields: deals, time_capsules, itinerary
    if !state.get("deals").is_some_and(Value::is_object) {
        state.insert(
            "deals".into(),
            json!({ "watchlist": [], "prices": {}, "last_poll_at": "" }),
        );
        changed = true;
    } else if let Some(deals) = state.get_mut("deals").and_then(Value::as_object_mut) {
        if !deals.get("watchlist").is_some_and(Value::is_array) {
            deals.insert("watchlist".into(), Value::Array(Vec::new()));
            changed = true;
        }
        if !deals.get("prices").is_some_and(Value::is_object) {
            deals.insert("prices".into(), Value::Object(Map::new()));
            changed = true;
        }
        if !deals.get("last_poll_at").is_some_and(Value::is_string) {
            deals.insert("last_poll_at".into(), Value::String(String::new()));
            changed = true;
        }
    }
    match state.get("time_capsules").and_then(Value::as_array) {
        Some(caps) => {
            let valid: Vec<Value> = caps
                .iter()
                .filter(|c| {
                    c.as_object().is_some_and(|o| {
                        o.get("id")
                            .and_then(Value::as_str)
                            .is_some_and(|s| !s.trim().is_empty())
                            && o.get("unlock_at")
                                .and_then(Value::as_str)
                                .is_some_and(|s| !s.trim().is_empty())
                    })
                })
                .cloned()
                .collect();
            if valid.len() != caps.len() {
                state.insert("time_capsules".into(), Value::Array(valid));
                changed = true;
            }
        }
        None => {
            state.insert("time_capsules".into(), Value::Array(Vec::new()));
            changed = true;
        }
    }
    if let Some(itin) = state.get("itinerary")
        && !itin.is_null()
    {
        let valid = itin.as_object().is_some_and(|o| {
            o.get("entries").is_some_and(Value::is_array)
                && o.get("later").is_none_or(Value::is_array)
        });
        if !valid {
            state.insert("itinerary".into(), Value::Null);
            changed = true;
        }
    } else if !state.contains_key("itinerary") {
        state.insert("itinerary".into(), Value::Null);
        changed = true;
    }

    if let Some(Value::Array(games)) = state.get_mut("games") {
        for game in games.iter_mut() {
            if let Some(obj) = game.as_object_mut()
                && obj.contains_key("tags")
                && !obj.get("tags").is_some_and(Value::is_array)
            {
                obj.insert("tags".into(), Value::Array(Vec::new()));
                changed = true;
            }
        }
    }
    changed
}

fn apply_migrations(state: &mut Map<String, Value>, mut version: i64) -> Result<bool, StateError> {
    let mut changed = false;
    while version < i64::from(STATE_SCHEMA_VERSION) {
        match version {
            1 => {
                state
                    .entry("profiles".to_owned())
                    .or_insert_with(|| Value::Object(Map::new()));
                state
                    .entry("history".to_owned())
                    .or_insert_with(|| Value::Array(Vec::new()));
                state
                    .entry("settings".to_owned())
                    .or_insert_with(|| Value::Object(Map::new()));
                state
                    .entry("playlists".to_owned())
                    .or_insert_with(|| Value::Array(Vec::new()));
                state.insert("schema_version".into(), json!(2));
                version = 2;
                changed = true;
            }
            2 => {
                if let Some(Value::Array(games)) = state.get_mut("games") {
                    normalize_game_ids(games);
                }
                state.insert("schema_version".into(), json!(3));
                version = 3;
                changed = true;
            }
            3 => {
                for (k, cap) in [("queue", QUEUE_CAP), ("notifications", NOTIFICATIONS_CAP)] {
                    match state.get_mut(k) {
                        Some(Value::Array(arr)) => arr.truncate(cap),
                        _ => {
                            state.insert(k.into(), Value::Array(Vec::new()));
                        }
                    }
                }
                state.insert("schema_version".into(), json!(4));
                version = 4;
                changed = true;
            }
            4 => {
                if !state.get("ui_state").is_some_and(Value::is_object) {
                    state.insert("ui_state".into(), Value::Object(Map::new()));
                }
                state.insert("schema_version".into(), json!(5));
                version = 5;
                changed = true;
            }
            5 => {
                if !state.get("active_sessions").is_some_and(Value::is_array) {
                    state.insert("active_sessions".into(), Value::Array(Vec::new()));
                }
                state.insert("schema_version".into(), json!(6));
                version = 6;
                changed = true;
            }
            6 => {
                state.insert("format".into(), Value::String(STATE_FORMAT.to_owned()));
                sync_envelope(state);
                state.insert("schema_version".into(), json!(7));
                version = 7;
                changed = true;
            }
            other => {
                return Err(StateError::Corrupt(format!(
                    "No migration is available for schema version {other}."
                )));
            }
        }
    }
    Ok(changed)
}

fn validate_state(state: &Map<String, Value>) -> Result<(), StateError> {
    let Some(games) = state.get("games").and_then(Value::as_array) else {
        return Err(StateError::Corrupt(
            "OpenBox library.json has an invalid games collection.".to_owned(),
        ));
    };
    for (idx, game) in games.iter().enumerate() {
        let Some(obj) = game.as_object() else {
            return Err(StateError::Corrupt(format!(
                "OpenBox library.json has an invalid game at index {idx}."
            )));
        };
        let has_id = obj
            .get("game_id")
            .and_then(Value::as_str)
            .is_some_and(|s| !s.trim().is_empty());
        if !has_id {
            return Err(StateError::Corrupt(format!(
                "OpenBox library.json has a game without an identity at index {idx}."
            )));
        }
    }
    Ok(())
}

fn raw_schema_version_map(obj: &Map<String, Value>) -> i64 {
    match obj.get("schema_version") {
        None => 1,
        Some(Value::Number(n)) => n.as_i64().unwrap_or(1),
        Some(Value::String(s)) => s.trim().parse::<i64>().unwrap_or(1),
        _ => 1,
    }
}

/// Normalize and optionally migrate raw state without cloning the input `Value`.
pub fn normalize_state_owned(raw: Value, migrate: bool) -> Result<(Value, bool), StateError> {
    let mut changed = false;
    let mut state: Map<String, Value> = match raw {
        Value::Array(arr) => {
            changed = true;
            let mut m = Map::new();
            m.insert("games".into(), Value::Array(arr));
            m.insert("schema_version".into(), json!(1));
            m
        }
        Value::Object(obj) => obj,
        _ => {
            return Err(StateError::Corrupt(
                "OpenBox library.json must contain an object or legacy game list.".to_owned(),
            ));
        }
    };

    let version = raw_schema_version_map(&state);
    if version > i64::from(STATE_SCHEMA_VERSION) {
        return Err(StateError::FutureVersion(format!(
            "This library uses schema {version}; this build supports {STATE_SCHEMA_VERSION}. Install a newer OpenBox."
        )));
    }
    if version < 1 {
        return Err(StateError::Corrupt(format!(
            "OpenBox library.json uses unsupported schema version {version}."
        )));
    }
    if !migrate && version < i64::from(STATE_SCHEMA_VERSION) {
        return Err(StateError::SchemaTooOld(schema_too_old_detail(version)));
    }
    if state.contains_key("games") && !state.get("games").is_some_and(Value::is_array) {
        return Err(StateError::Corrupt(
            "OpenBox library.json has an invalid games collection.".to_owned(),
        ));
    }
    if migrate && apply_migrations(&mut state, version)? {
        changed = true;
    }
    // Ensure defaults
    if let Value::Object(defaults) = default_state() {
        for (k, v) in defaults {
            if !state.contains_key(&k) {
                state.insert(k, v);
                changed = true;
            }
        }
    }
    if let Some(Value::Object(settings)) = state.get_mut("settings")
        && !prune_unknown_settings(settings).is_empty()
    {
        changed = true;
    }
    state.insert("schema_version".into(), json!(STATE_SCHEMA_VERSION));
    if sync_envelope(&mut state) {
        changed = true;
    }
    if !state.get("games").is_some_and(Value::is_array) {
        return Err(StateError::Corrupt(
            "OpenBox library.json has an invalid games collection.".to_owned(),
        ));
    }
    if normalize_feature_fields(&mut state) {
        changed = true;
    }
    if let Some(Value::Array(games)) = state.get_mut("games")
        && normalize_game_ids(games)
    {
        changed = true;
    }
    validate_state(&state)?;
    Ok((Value::Object(state), changed))
}

/// Normalize and optionally migrate raw state matching `state_store.py::normalize_state`.
pub fn normalize_state(raw: &Value, migrate: bool) -> Result<(Value, bool), StateError> {
    normalize_state_owned(raw.clone(), migrate)
}

/// Validate and repair an already-current-schema state in place without cloning the catalog.
/// Mirrors `state_store.py::_normalize_committed_state`.
fn normalize_committed_state_in_place(state: &mut Value) -> Result<(), StateError> {
    let Some(obj) = state.as_object_mut() else {
        return Err(StateError::Corrupt(
            "OpenBox state must be an object.".to_owned(),
        ));
    };
    let version = raw_schema_version_map(obj);
    let required = [
        "games",
        "profiles",
        "history",
        "settings",
        "playlists",
        "ui_state",
    ];
    if version != i64::from(STATE_SCHEMA_VERSION) || required.iter().any(|k| !obj.contains_key(*k))
    {
        let owned = std::mem::replace(state, Value::Null);
        let (normalized, _) = normalize_state_owned(owned, true)?;
        *state = normalized;
        return Ok(());
    }
    if let Value::Object(defaults) = default_state() {
        for (k, v) in defaults {
            obj.entry(k).or_insert(v);
        }
    }
    if let Some(Value::Object(settings)) = obj.get_mut("settings") {
        let _ = prune_unknown_settings(settings);
    }
    obj.insert("schema_version".into(), json!(STATE_SCHEMA_VERSION));
    sync_envelope(obj);
    if !obj.get("games").is_some_and(Value::is_array) {
        return Err(StateError::Corrupt(
            "OpenBox library.json has an invalid games collection.".to_owned(),
        ));
    }
    normalize_feature_fields(obj);
    if let Some(Value::Array(games)) = obj.get_mut("games") {
        normalize_game_ids(games);
    }
    validate_state(obj)?;
    Ok(())
}

struct Blake2Writer(Blake2b<U16>);

impl Write for Blake2Writer {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        self.0.update(buf);
        Ok(buf.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

/// 16-byte BLAKE2b digest over deterministic JSON (zero intermediate buffer allocations).
pub fn state_digest(state: &Value) -> [u8; 16] {
    let mut writer = Blake2Writer(Blake2b::<U16>::new());
    let _ = serde_json::to_writer(&mut writer, state);
    let result = writer.0.finalize();
    let mut out = [0u8; 16];
    out.copy_from_slice(&result);
    out
}

pub fn read_sessions_file(data_dir: &Path) -> Vec<Value> {
    let path = data_dir.join(SESSIONS_FILE_NAME);
    let Ok(raw) = fs::read_to_string(&path) else {
        return Vec::new();
    };
    let Ok(val) = serde_json::from_str::<Value>(&raw) else {
        return Vec::new();
    };
    val.get("sessions")
        .and_then(Value::as_array)
        .map(|arr| arr.iter().filter(|v| v.is_object()).cloned().collect())
        .unwrap_or_default()
}

fn write_sessions_file(data_dir: &Path, sessions: &[Value]) -> std::io::Result<()> {
    let path = data_dir.join(SESSIONS_FILE_NAME);
    if sessions.is_empty() {
        let _ = fs::remove_file(&path);
        return Ok(());
    }
    let payload = json!({
        "format": SESSIONS_FORMAT,
        "written_at": jiff::Timestamp::now().strftime("%Y-%m-%dT%H:%M:%SZ").to_string(),
        "sessions": sessions,
    });
    let text = format!(
        "{}\n",
        serde_json::to_string_pretty(&payload).unwrap_or_else(|_| "{}".to_owned())
    );
    atomic_write_text(&path, &text, 0o600)?;
    Ok(())
}

fn resolve_portable_paths(state: &mut Value) {
    let Some(obj) = state.as_object_mut() else {
        return;
    };
    if obj.get("path_mode").and_then(Value::as_str) != Some("portable") {
        return;
    }
    let root = obj
        .get("library_root")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    if root.is_empty() {
        return;
    }
    if let Some(Value::Array(games)) = obj.get("games") {
        let (expanded, _problems) = expand_games(games, &root);
        obj.insert("games".into(), Value::Array(expanded));
    }
}

#[derive(Debug, Default)]
struct StoreCache {
    state: Option<Arc<Value>>,
    signature: Option<FileSignature>,
    committed_digest: Option<[u8; 16]>,
    games_by_id: HashMap<String, usize>,
    games_by_platform: HashMap<String, Vec<usize>>,
    snapshot_seq: u64,
    last_snapshot: Option<Instant>,
}

impl StoreCache {
    fn reindex(&mut self) {
        self.games_by_id.clear();
        self.games_by_platform.clear();
        let Some(state) = self.state.as_deref() else {
            return;
        };
        let Some(games) = state.get("games").and_then(Value::as_array) else {
            return;
        };
        if self.games_by_id.capacity() < games.len() {
            self.games_by_id.reserve(games.len());
        }
        for (idx, game) in games.iter().enumerate() {
            if let Some(obj) = game.as_object() {
                if let Some(gid) = obj
                    .get("game_id")
                    .and_then(Value::as_str)
                    .map(str::trim)
                    .filter(|s| !s.is_empty())
                {
                    self.games_by_id.insert(gid.to_owned(), idx);
                }
                if let Some(plat) = obj
                    .get("platform")
                    .and_then(Value::as_str)
                    .map(str::trim)
                    .filter(|s| !s.is_empty())
                {
                    self.games_by_platform
                        .entry(plat.to_owned())
                        .or_default()
                        .push(idx);
                }
            }
        }
    }

    fn clear(&mut self) {
        self.state = None;
        self.signature = None;
        self.committed_digest = None;
        self.games_by_id.clear();
        self.games_by_platform.clear();
    }
}

/// Process-safe, indexed, ADR-0007-ordered JSON state store for `library.json`.
pub struct JsonStateStore {
    pub path: PathBuf,
    pub lock_path: PathBuf,
    pub backup_path: PathBuf,
    pub snapshots_dir: PathBuf,
    pub snapshot_limit: usize,
    pub snapshot_debounce: Duration,
    thread_lock: OrderedMutex<StoreCache, { lock_level::STORE_THREAD }>,
    coalesce_lock: OrderedMutex<Option<Value>, { lock_level::STORE_COALESCE }>,
}

impl JsonStateStore {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self::with_options(path, 5, Duration::ZERO)
    }

    pub fn with_options(
        path: impl Into<PathBuf>,
        snapshot_limit: usize,
        snapshot_debounce: Duration,
    ) -> Self {
        let path = path.into();
        let file_name = path
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_else(|| "library.json".to_owned());
        let lock_path = path.with_file_name(format!(".{file_name}.lock"));
        let backup_path = path.with_file_name(format!("{file_name}.bak"));
        let snapshots_dir = path.with_file_name(format!("{file_name}.snapshots"));
        Self {
            path,
            lock_path,
            backup_path,
            snapshots_dir,
            snapshot_limit,
            snapshot_debounce,
            thread_lock: OrderedMutex::new(StoreCache::default()),
            coalesce_lock: OrderedMutex::new(None),
        }
    }

    pub fn data_dir(&self) -> &Path {
        self.path.parent().unwrap_or_else(|| Path::new("."))
    }

    pub fn signature(&self) -> Option<FileSignature> {
        let meta = fs::metadata(&self.path).ok()?;
        let mtime_ns = meta
            .modified()
            .ok()
            .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        #[cfg(unix)]
        let ino = {
            use std::os::unix::fs::MetadataExt;
            meta.ino()
        };
        #[cfg(not(unix))]
        let ino = 0u64;
        Some(FileSignature {
            mtime_ns,
            size: meta.len(),
            ino,
        })
    }

    fn read_json_file(path: &Path) -> Result<Value, StateError> {
        let file = File::open(path).map_err(|_| {
            StateError::Corrupt(format!(
                "Unable to read {}. The original file was preserved; restore or inspect it before continuing.",
                path.display()
            ))
        })?;
        let reader = std::io::BufReader::with_capacity(256 * 1024, file);
        serde_json::from_reader(reader).map_err(|_| {
            StateError::Corrupt(format!(
                "Unable to read {}. The original file was preserved; restore or inspect it before continuing.",
                path.display()
            ))
        })
    }

    fn load_unlocked(&self) -> Result<(Value, bool), StateError> {
        if !self.path.exists() {
            return Ok((default_state(), false));
        }
        let raw = Self::read_json_file(&self.path)?;
        let version = raw_schema_version(&raw);
        if (1..i64::from(STATE_SCHEMA_VERSION)).contains(&version) {
            return Err(StateError::SchemaTooOld(schema_too_old_detail(version)));
        }
        let had_active_sessions = raw
            .as_object()
            .is_some_and(|o| o.contains_key("active_sessions"));
        let (mut state, changed) = normalize_state_owned(raw, false)?;
        if !had_active_sessions && let Some(obj) = state.as_object_mut() {
            obj.insert(
                "active_sessions".into(),
                Value::Array(read_sessions_file(self.data_dir())),
            );
        }
        resolve_portable_paths(&mut state);
        let _ = finish_interrupted_migration(self.data_dir(), &self.path);
        Ok((state, changed || had_active_sessions))
    }

    fn write_unlocked(
        &self,
        cache: &mut StoreCache,
        mut state: Value,
        precomputed_digest: Option<[u8; 16]>,
    ) -> Result<Arc<Value>, StateError> {
        if is_library_frozen() {
            return Err(StateError::Frozen);
        }
        let parent = self.data_dir();
        fs::create_dir_all(parent)?;
        if let Some(obj) = state.as_object_mut() {
            sync_envelope(obj);
        }
        let games_count = state
            .get("games")
            .and_then(Value::as_array)
            .map(Vec::len)
            .unwrap_or(0);

        let fname = self
            .path
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_else(|| "library.json".to_owned());
        let mut temp = Builder::new()
            .prefix(&format!(".{fname}."))
            .suffix(".tmp")
            .tempfile_in(parent)?;

        // Temporarily detach `active_sessions` and encode portable paths in place while serializing
        // so we never clone the full games array for `library.json`.
        let saved_sessions = state
            .as_object_mut()
            .and_then(|obj| obj.remove("active_sessions"));
        let mut saved_expanded_games: Option<Value> = None;
        if let Some(obj) = state.as_object_mut()
            && obj.get("path_mode").and_then(Value::as_str) == Some("portable")
            && let Some(root) = obj
                .get("library_root")
                .and_then(Value::as_str)
                .filter(|r| !r.is_empty())
                .map(ToOwned::to_owned)
            && let Some(Value::Array(games)) = obj.get("games")
        {
            let encoded = encode_games(games, &root);
            saved_expanded_games = obj.insert("games".into(), Value::Array(encoded));
        }

        let serialize_res: Result<(), StateError> = if games_count > 500 {
            let mut bw = std::io::BufWriter::with_capacity(256 * 1024, temp.as_file_mut());
            serde_json::to_writer(&mut bw, &state)
                .map_err(|e| StateError::Corrupt(e.to_string()))
                .and_then(|()| bw.write_all(b"\n").map_err(StateError::Io))
                .and_then(|()| bw.flush().map_err(StateError::Io))
        } else {
            let pretty = serde_json::to_vec_pretty(&state)
                .map_err(|e| StateError::Corrupt(e.to_string()))?;
            let raw_bytes = if pretty.len() > COMPACT_JSON_THRESHOLD {
                serde_json::to_vec(&state).map_err(|e| StateError::Corrupt(e.to_string()))?
            } else {
                pretty
            };
            temp.write_all(&raw_bytes)
                .and_then(|()| temp.write_all(b"\n"))
                .and_then(|()| temp.flush())
                .map_err(StateError::Io)
        };

        // Restore detached fields immediately before propagating any error.
        if let Some(orig_games) = saved_expanded_games
            && let Some(obj) = state.as_object_mut()
        {
            obj.insert("games".into(), orig_games);
        }
        if let Some(sess_val) = saved_sessions
            && let Some(obj) = state.as_object_mut()
        {
            obj.insert("active_sessions".into(), sess_val);
        }
        serialize_res?;

        temp.as_file().sync_all()?;
        let temp_path = temp.into_temp_path();
        set_mode(&temp_path, 0o600)?;

        // Backup first with separate inode before replacing primary (matching state_store.py).
        let bak_tmp = Builder::new()
            .prefix(&format!(".{fname}.bak."))
            .suffix(".tmp")
            .tempfile_in(parent)?;
        let bak_temp_path = bak_tmp.into_temp_path();
        fs::copy(&temp_path, &bak_temp_path)?;
        set_mode(&bak_temp_path, 0o600)?;
        bak_temp_path
            .persist(&self.backup_path)
            .map_err(|e| StateError::Io(e.error))?;
        let _ = set_mode(&self.backup_path, 0o600);

        temp_path
            .persist(&self.path)
            .map_err(|e| StateError::Io(e.error))?;
        let _ = set_mode(&self.path, 0o600);

        let sessions = state
            .get("active_sessions")
            .and_then(Value::as_array)
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        write_sessions_file(parent, sessions)?;

        {
            let mut pending = self.coalesce_lock.lock();
            *pending = None;
        }

        fsync_directory(parent);
        self.rotate_snapshots_locked(cache, false);

        let digest = precomputed_digest.unwrap_or_else(|| state_digest(&state));
        let arc = Arc::new(state);
        cache.state = Some(Arc::clone(&arc));
        cache.signature = self.signature();
        cache.committed_digest = Some(digest);
        cache.reindex();
        Ok(arc)
    }

    pub fn rotate_snapshots(&self, force: bool) {
        let mut cache = self.thread_lock.lock();
        self.rotate_snapshots_locked(&mut cache, force);
    }

    fn rotate_snapshots_locked(&self, cache: &mut StoreCache, force: bool) {
        if self.snapshot_limit == 0 || !self.path.is_file() {
            return;
        }
        let now = Instant::now();
        if !force
            && !self.snapshot_debounce.is_zero()
            && let Some(last) = cache.last_snapshot
            && now.duration_since(last) < self.snapshot_debounce
        {
            return;
        }
        if fs::create_dir_all(&self.snapshots_dir).is_err() {
            return;
        }
        cache.snapshot_seq += 1;
        let seq = cache.snapshot_seq;
        let stamp = jiff::Timestamp::now()
            .strftime("%Y%m%dT%H%M%S%6fZ")
            .to_string();
        let mut rand_bytes = [0u8; 4];
        rand::rngs::OsRng.fill_bytes(&mut rand_bytes);
        let rand_hex = hex::encode(rand_bytes);
        let target = self
            .snapshots_dir
            .join(format!("{stamp}-{seq:08}-{rand_hex}.json"));
        if fs::hard_link(&self.path, &target).is_err() && fs::copy(&self.path, &target).is_err() {
            return;
        }
        let _ = set_mode(&target, 0o600);
        cache.last_snapshot = Some(now);

        let snaps = self.snapshots();
        if snaps.len() > self.snapshot_limit {
            for stale in &snaps[self.snapshot_limit..] {
                if let Some(name) = stale.get("name").and_then(Value::as_str) {
                    let _ = fs::remove_file(self.snapshots_dir.join(name));
                }
            }
        }
    }

    /// Return available snapshots newest-first (ordered by embedded timestamp + sequence, falling back to mtime).
    pub fn snapshots(&self) -> Vec<Value> {
        let Ok(entries) = fs::read_dir(&self.snapshots_dir) else {
            return Vec::new();
        };
        let mut named: Vec<((String, String), Value)> = Vec::new();
        let mut unnamed: Vec<(f64, Value)> = Vec::new();
        for entry in entries.flatten() {
            let p = entry.path();
            if p.extension().and_then(|e| e.to_str()) != Some("json") {
                continue;
            }
            let Ok(meta) = fs::metadata(&p) else {
                continue;
            };
            let name = entry.file_name().to_string_lossy().into_owned();
            let modified = meta
                .modified()
                .ok()
                .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
                .map(|d| d.as_secs_f64())
                .unwrap_or(0.0);
            let item = json!({
                "name": name,
                "size": meta.len(),
                "modified": modified,
            });
            if let Some(caps) = SNAPSHOT_NAME_RE.captures(&name) {
                named.push(((caps[1].to_owned(), caps[2].to_owned()), item));
            } else {
                unnamed.push((modified, item));
            }
        }
        named.sort_by(|a, b| b.0.cmp(&a.0));
        unnamed.sort_by(|a, b| b.0.total_cmp(&a.0));
        named
            .into_iter()
            .map(|(_, v)| v)
            .chain(unnamed.into_iter().map(|(_, v)| v))
            .collect()
    }

    /// Zero-copy read-only snapshot (`Arc::clone`). Never mutates disk unless normalization is needed.
    pub fn load_readonly(&self) -> Result<Arc<Value>, StateError> {
        let mut cache = self.thread_lock.lock();
        let sig = self.signature();
        if let Some(ref cached) = cache.state
            && sig == cache.signature
        {
            return Ok(Arc::clone(cached));
        }
        let _flock = file_lock(&self.lock_path, true, DEFAULT_LOCK_TIMEOUT)?;
        let (state, changed) = self.load_unlocked()?;
        if changed {
            return self.write_unlocked(&mut cache, state, None);
        }
        let digest = state_digest(&state);
        let arc = Arc::new(state);
        cache.state = Some(Arc::clone(&arc));
        cache.signature = self.signature();
        cache.committed_digest = Some(digest);
        cache.reindex();
        Ok(arc)
    }

    pub fn load(&self) -> Result<Value, StateError> {
        Ok((*self.load_readonly()?).clone())
    }

    /// O(1) lookup of a game's array index by `game_id`.
    pub fn get_game_index_by_id(&self, game_id: &str) -> Result<Option<usize>, StateError> {
        let needle = game_id.trim();
        let _arc = self.load_readonly()?;
        let cache = self.thread_lock.lock();
        Ok(cache.games_by_id.get(needle).copied())
    }

    /// O(1) lookup of a game by `game_id`.
    pub fn get_game_by_id(&self, game_id: &str) -> Result<Option<Value>, StateError> {
        let needle = game_id.trim();
        let arc = self.load_readonly()?;
        let cache = self.thread_lock.lock();
        let Some(&idx) = cache.games_by_id.get(needle) else {
            return Ok(None);
        };
        Ok(arc
            .get("games")
            .and_then(Value::as_array)
            .and_then(|g| g.get(idx))
            .cloned())
    }

    /// O(1) lookup of all games for a platform.
    pub fn get_games_by_platform(&self, platform: &str) -> Result<Vec<Value>, StateError> {
        let needle = platform.trim();
        let arc = self.load_readonly()?;
        let cache = self.thread_lock.lock();
        let Some(indices) = cache.games_by_platform.get(needle) else {
            return Ok(Vec::new());
        };
        let Some(games) = arc.get("games").and_then(Value::as_array) else {
            return Ok(Vec::new());
        };
        Ok(indices
            .iter()
            .filter_map(|&idx| games.get(idx).cloned())
            .collect())
    }

    pub fn save(&self, raw: &Value) -> Result<Value, StateError> {
        let mut cache = self.thread_lock.lock();
        let _flock = file_lock(&self.lock_path, true, DEFAULT_LOCK_TIMEOUT)?;
        match normalize_state(raw, true) {
            Ok((normalized, _)) => {
                let arc = self.write_unlocked(&mut cache, normalized, None)?;
                Ok((*arc).clone())
            }
            Err(err) => {
                cache.clear();
                Err(err)
            }
        }
    }

    /// Transactional read-modify-write under thread + cross-process file lock with BLAKE2b no-op skip.
    pub fn update_with_result<R, F>(&self, mutator: F) -> Result<(Arc<Value>, R), StateError>
    where
        F: FnOnce(&mut Value) -> Result<R, StateError>,
    {
        let mut cache = self.thread_lock.lock();
        let _flock = file_lock(&self.lock_path, true, DEFAULT_LOCK_TIMEOUT)?;
        let sig = self.signature();
        let from_cache = cache.state.is_some() && sig == cache.signature;
        let mut state = if from_cache {
            // Take ownership of the cached Arc without cloning when unshared (common case).
            match cache.state.take() {
                Some(arc) => Arc::try_unwrap(arc).unwrap_or_else(|shared| (*shared).clone()),
                None => self.load_unlocked()?.0,
            }
        } else {
            self.load_unlocked()?.0
        };
        let res = match mutator(&mut state) {
            Ok(r) => r,
            Err(e) => {
                cache.clear();
                return Err(e);
            }
        };
        if let Err(e) = normalize_committed_state_in_place(&mut state) {
            cache.clear();
            return Err(e);
        }
        let digest = state_digest(&state);
        if from_cache && cache.committed_digest == Some(digest) {
            let arc = Arc::new(state);
            cache.state = Some(Arc::clone(&arc));
            cache.signature = sig;
            return Ok((arc, res));
        }
        let arc = self.write_unlocked(&mut cache, state, Some(digest))?;
        Ok((arc, res))
    }

    pub fn update<F>(&self, mutator: F) -> Result<Arc<Value>, StateError>
    where
        F: FnOnce(&mut Value) -> Result<(), StateError>,
    {
        let (state, ()) = self.update_with_result(mutator)?;
        Ok(state)
    }

    /// Recover primary from `library.json.bak` (invoking v7 migration writer when `.bak` is schema 6).
    pub fn recover(&self) -> Result<Value, StateError> {
        let mut cache = self.thread_lock.lock();
        let _flock = file_lock(&self.lock_path, true, DEFAULT_LOCK_TIMEOUT)?;
        if !self.backup_path.is_file() {
            return Err(StateError::Corrupt(format!(
                "No last-known-good state exists at {}.",
                self.backup_path.display()
            )));
        }
        let raw = Self::read_json_file(&self.backup_path).map_err(|_| {
            StateError::Corrupt(format!(
                "The last-known-good state is also unusable: {}",
                self.backup_path.display()
            ))
        })?;
        let version = raw_schema_version(&raw);
        if version == 6 {
            let bytes = fs::read(&self.backup_path)?;
            openbox_io::atomic_write_bytes(&self.path, &bytes, 0o600)?;
            drop(cache);
            let (maybe_state, _report) = crate::library_v7::migrate_unlocked_inner(self)?;
            return maybe_state.ok_or_else(|| {
                StateError::Corrupt(format!(
                    "The last-known-good state is also unusable: {}",
                    self.backup_path.display()
                ))
            });
        }
        let (state, _) = normalize_state_owned(raw, true).map_err(|_| {
            StateError::Corrupt(format!(
                "The last-known-good state is also unusable: {}",
                self.backup_path.display()
            ))
        })?;
        let arc = self.write_unlocked(&mut cache, state, None)?;
        Ok((*arc).clone())
    }

    pub fn restore_snapshot(&self, name: &str) -> Result<Value, StateError> {
        let clean = Path::new(name)
            .file_name()
            .ok_or_else(|| StateError::Corrupt(format!("Unknown snapshot: {name}")))?;
        let candidate = self.snapshots_dir.join(clean);
        if !candidate.is_file() {
            return Err(StateError::Corrupt(format!("Unknown snapshot: {name}")));
        }
        let mut cache = self.thread_lock.lock();
        let _flock = file_lock(&self.lock_path, true, DEFAULT_LOCK_TIMEOUT)?;
        let raw = Self::read_json_file(&candidate)?;
        let (state, _) = normalize_state_owned(raw, true)?;
        let arc = self.write_unlocked(&mut cache, state, None)?;
        Ok((*arc).clone())
    }

    pub fn flush_coalesced(&self) -> Result<(), StateError> {
        let mut cache = self.thread_lock.lock();
        let _flock = file_lock(&self.lock_path, true, DEFAULT_LOCK_TIMEOUT)?;
        let pending = {
            let mut guard = self.coalesce_lock.lock();
            guard.take()
        };
        if let Some(state) = pending {
            self.write_unlocked(&mut cache, state, None)?;
        }
        Ok(())
    }

    pub fn migrate_primary_to_v7(&self) -> Result<(Option<Value>, Option<Value>), StateError> {
        let _cache = self.thread_lock.lock();
        let _flock = file_lock(&self.lock_path, true, DEFAULT_LOCK_TIMEOUT)?;
        drop(_cache);
        crate::library_v7::migrate_unlocked_inner(self)
    }
}

pub fn secure_text_write(path: &Path, value: &str) -> std::io::Result<()> {
    atomic_write_text(path, value, 0o600)?;
    Ok(())
}

/// Helper used by `library_v7::migrate_unlocked_inner` to write a migrated state while refreshing store cache.
pub(crate) fn commit_migrated_state(
    store: &JsonStateStore,
    state: &mut Value,
) -> Result<(), StateError> {
    let mut cache = store.thread_lock.lock();
    let owned = std::mem::replace(state, Value::Null);
    let arc = store.write_unlocked(&mut cache, owned, None)?;
    *state = (*arc).clone();
    Ok(())
}

/// Ensure a data directory's legacy file is not lost when migrating from schema 6.
pub(crate) fn snapshot_before_migration(store: &JsonStateStore) -> Option<String> {
    let mut cache = store.thread_lock.lock();
    store.rotate_snapshots_locked(&mut cache, true);
    store
        .snapshots()
        .first()
        .and_then(|v| v.get("name"))
        .and_then(Value::as_str)
        .map(ToOwned::to_owned)
}

pub fn write_atomic_file_handle(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    let mut f = File::create(path)?;
    f.write_all(bytes)?;
    f.sync_all()
}
