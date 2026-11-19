//! Service-agnostic game-stat syncing through a mounted cloud folder (`cloud_sync.py`).

use std::fs;
use std::path::Path;
use std::time::Duration;

use jiff::Timestamp;
use openbox_io::atomic_write_text;
use openbox_platform::file_lock;
use openbox_runtime::notifications::record_cloud_sync_outcome;
use serde_json::{Map, Value, json};

use crate::saves::{SAVE_SYNC_LOCK, expand_user_path};

pub const STAT_FIELDS: &[&str] = &[
    "play_count",
    "playtime_seconds",
    "last_played",
    "progress",
    "rating",
    "favorite",
];

pub const LIBRARY_SYNC_FILE: &str = "openbox-library.json";

const PROGRESS_VALUES: &[&str] = &[
    "",
    "Backlog",
    "Playing",
    "Beaten",
    "Completed",
    "100%",
    "On Hold",
    "Dropped",
    "Endless",
];

#[derive(Debug, Clone)]
pub struct CloudSyncError {
    pub code: &'static str,
    pub message: String,
}

impl CloudSyncError {
    pub fn remote_invalid(msg: impl Into<String>) -> Self {
        Self {
            code: "CLOUD_REMOTE_INVALID",
            message: msg.into(),
        }
    }
    pub fn invalid_folder(msg: impl Into<String>) -> Self {
        Self {
            code: "CLOUD_SYNC_ERROR",
            message: msg.into(),
        }
    }
    pub fn library_sync_unavailable() -> Self {
        Self {
            code: "LIBRARY_SYNC_UNAVAILABLE",
            message: "Full library synchronization is unavailable pending a safe replacement."
                .to_owned(),
        }
    }
}

impl std::fmt::Display for CloudSyncError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for CloudSyncError {}

pub fn nonnegative_int(value: Option<&Value>) -> i64 {
    match value {
        Some(Value::Number(n)) => n
            .as_i64()
            .or_else(|| n.as_f64().map(|f| f as i64))
            .unwrap_or(0)
            .max(0),
        Some(Value::String(s)) => s.trim().parse::<i64>().unwrap_or(0).max(0),
        Some(Value::Bool(b)) => i64::from(*b),
        _ => 0,
    }
}

pub fn legacy_game_key(game: &Value) -> String {
    if let Some(s) = game
        .get("steam_app_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        return format!("steam:{s}");
    }
    if let Some(s) = game
        .get("heroic_app_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        let src = game.get("source").and_then(Value::as_str).unwrap_or("");
        return format!("heroic:{src}:{s}");
    }
    if let Some(s) = game
        .get("lutris_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        return format!("lutris:{s}");
    }
    if let Some(s) = game
        .get("rom_name")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        let src = game.get("source").and_then(Value::as_str).unwrap_or("");
        return format!("arcade:{src}:{s}");
    }
    let raw_path = game.get("path").and_then(Value::as_str).unwrap_or("");
    format!("path:{}", expand_user_path(raw_path).to_string_lossy())
}

pub fn game_key(game: &Value) -> String {
    if let Some(gid) = game
        .get("game_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        return format!("id:{gid}");
    }
    legacy_game_key(game)
}

fn parse_ts(val: &str) -> f64 {
    let trimmed = val.trim();
    if trimmed.is_empty() {
        return 0.0;
    }
    if let Ok(ts) = trimmed.parse::<Timestamp>() {
        return ts.as_second() as f64;
    }
    if let Ok(dt) = trimmed.parse::<jiff::civil::DateTime>()
        && let Ok(ts) = jiff::tz::TimeZone::UTC.to_timestamp(dt)
    {
        return ts.as_second() as f64;
    }
    0.0
}

type RemoteStatePair = (Map<String, Value>, Map<String, Value>);

fn load_remote_state(target: &Path) -> Result<RemoteStatePair, CloudSyncError> {
    if !target.is_file() {
        return Ok((Map::new(), Map::new()));
    }
    let raw = fs::read_to_string(target).map_err(|_| {
        CloudSyncError::remote_invalid("Remote cloud statistics file is invalid or unreadable.")
    })?;
    let parsed: Value = serde_json::from_str(&raw).map_err(|_| {
        CloudSyncError::remote_invalid("Remote cloud statistics file is invalid or unreadable.")
    })?;
    let Some(remote) = parsed.as_object() else {
        return Err(CloudSyncError::remote_invalid(
            "Remote cloud statistics file is invalid or unreadable.",
        ));
    };
    let remote_games = match remote.get("games") {
        None => Map::new(),
        Some(Value::Object(m)) => m.clone(),
        _ => {
            return Err(CloudSyncError::remote_invalid(
                "Remote cloud statistics file is invalid or unreadable.",
            ));
        }
    };
    Ok((remote.clone(), remote_games))
}

fn resolve_saved_record(
    remote_games: &Map<String, Value>,
    game: &Value,
    key: &str,
) -> (Map<String, Value>, String) {
    if let Some(m) = remote_games.get(key).and_then(Value::as_object)
        && !m.is_empty()
    {
        return (m.clone(), key.to_owned());
    }
    let legacy = legacy_game_key(game);
    if let Some(m) = remote_games.get(&legacy).and_then(Value::as_object) {
        return (m.clone(), legacy);
    }
    (Map::new(), key.to_owned())
}

fn pick_last_played(local_val: &str, remote_val: &str) -> String {
    let lts = parse_ts(local_val);
    let rts = parse_ts(remote_val);
    if lts > rts {
        local_val.to_owned()
    } else if rts > lts || !remote_val.is_empty() {
        remote_val.to_owned()
    } else {
        local_val.to_owned()
    }
}

fn merge_game_stats(game: &mut Value, saved: &Map<String, Value>) -> (Value, bool) {
    let Some(obj) = game.as_object_mut() else {
        return (json!({}), false);
    };
    let before: Vec<Option<Value>> = STAT_FIELDS.iter().map(|f| obj.get(*f).cloned()).collect();

    let pc = nonnegative_int(obj.get("play_count")).max(nonnegative_int(saved.get("play_count")));
    let pt = nonnegative_int(obj.get("playtime_seconds"))
        .max(nonnegative_int(saved.get("playtime_seconds")));
    let lp = pick_last_played(
        obj.get("last_played").and_then(Value::as_str).unwrap_or(""),
        saved
            .get("last_played")
            .and_then(Value::as_str)
            .unwrap_or(""),
    );
    obj.insert("play_count".into(), json!(pc));
    obj.insert("playtime_seconds".into(), json!(pt));
    obj.insert("last_played".into(), Value::String(lp));

    if !saved.is_empty() {
        let local_prog = obj.get("progress").and_then(Value::as_str).unwrap_or("");
        if local_prog.is_empty() || !PROGRESS_VALUES.contains(&local_prog) {
            let rem_prog = saved.get("progress").and_then(Value::as_str).unwrap_or("");
            if !rem_prog.is_empty() && PROGRESS_VALUES.contains(&rem_prog) {
                obj.insert("progress".into(), Value::String(rem_prog.to_owned()));
            }
        }
        let local_rating = obj.get("rating").and_then(Value::as_f64).unwrap_or(0.0);
        if local_rating == 0.0
            && let Some(r) = saved.get("rating").and_then(Value::as_f64)
            && (0.0..=5.0).contains(&r)
        {
            obj.insert("rating".into(), json!(r));
        }
        if !obj.contains_key("favorite")
            && let Some(fav) = saved.get("favorite").and_then(Value::as_bool)
        {
            obj.insert("favorite".into(), Value::Bool(fav));
        }
    }

    let after: Vec<Option<Value>> = STAT_FIELDS.iter().map(|f| obj.get(*f).cloned()).collect();
    let changed = before != after;
    let mut merged = Map::new();
    for field in STAT_FIELDS {
        let default = if matches!(*field, "play_count" | "playtime_seconds" | "rating") {
            json!(0)
        } else {
            Value::String(String::new())
        };
        merged.insert((*field).into(), obj.get(*field).cloned().unwrap_or(default));
    }
    (Value::Object(merged), changed)
}

pub fn apply_synced_stats(game: &mut Value, synced: &Value) {
    if let Some(m) = synced.as_object() {
        merge_game_stats(game, m);
    }
}

pub fn sync_statistics(
    state: &mut Value,
    folder: &str,
    now: Option<&str>,
) -> Result<Value, CloudSyncError> {
    let dir = expand_user_path(folder);
    if !dir.is_dir() {
        return Err(CloudSyncError::invalid_folder(format!(
            "Cloud sync folder does not exist: {}",
            dir.display()
        )));
    }
    let target = dir.join("openbox-statistics.json");
    let ts_now = match now {
        Some(s) if !s.is_empty() => s.to_owned(),
        _ => {
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
    };

    let sync_res: Result<(usize, usize), CloudSyncError> = (|| {
        let _save_guard = SAVE_SYNC_LOCK.lock();
        let lock_path = dir.join(".openbox-statistics.json.lock");
        let _flock = file_lock(&lock_path, true, Duration::from_secs(30)).map_err(|e| {
            CloudSyncError::invalid_folder(format!("Unable to lock cloud sync file: {e}"))
        })?;
        let (remote, remote_games) = load_remote_state(&target)?;
        let remote_generated = parse_ts(
            remote
                .get("generated_at")
                .and_then(Value::as_str)
                .unwrap_or(""),
        );
        let last_sync = parse_ts(
            state
                .get("settings")
                .and_then(|s| s.get("last_cloud_sync"))
                .and_then(Value::as_str)
                .unwrap_or(""),
        );
        let mut merged: Map<String, Value> = remote_games
            .iter()
            .filter(|(_, v)| v.is_object())
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect();
        let mut changed = 0usize;
        if let Some(games) = state.get_mut("games").and_then(Value::as_array_mut) {
            for game in games.iter_mut() {
                let key = game_key(game);
                let (saved, remote_key) = resolve_saved_record(&remote_games, game, &key);
                let (merged_rec, rec_changed) = merge_game_stats(game, &saved);
                if rec_changed {
                    changed += 1;
                }
                merged.insert(key.clone(), merged_rec);
                if remote_key != key {
                    merged.remove(&remote_key);
                }
            }
        }
        let generated_at = if remote_generated > 0.0 && last_sync >= remote_generated {
            ts_now.clone()
        } else {
            remote
                .get("generated_at")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .map(str::to_owned)
                .unwrap_or_else(|| ts_now.clone())
        };
        let total_merged = merged.len();
        let payload = json!({
            "format": 1,
            "generated_at": generated_at,
            "games": Value::Object(merged),
        });
        let text = serde_json::to_string_pretty(&payload)
            .map_err(|e| CloudSyncError::invalid_folder(e.to_string()))?;
        atomic_write_text(&target, &text, 0o600)
            .map_err(|e| CloudSyncError::invalid_folder(e.to_string()))?;
        Ok((total_merged, changed))
    })();

    match sync_res {
        Err(err) => {
            let mut params = Map::new();
            params.insert("detail".into(), Value::String(err.message.clone()));
            record_cloud_sync_outcome(state, false, &err.message, Some(&ts_now), Some(&params));
            Err(err)
        }
        Ok((total_merged, changed)) => {
            if let Some(root) = state.as_object_mut() {
                let settings = root
                    .entry("settings".to_owned())
                    .or_insert_with(|| Value::Object(Map::new()));
                if let Some(sm) = settings.as_object_mut() {
                    sm.insert("last_cloud_sync".into(), Value::String(ts_now.clone()));
                }
            }
            let game_count = state
                .get("games")
                .and_then(Value::as_array)
                .map(Vec::len)
                .unwrap_or(0);
            let mut params = Map::new();
            params.insert("count".into(), json!(game_count));
            record_cloud_sync_outcome(
                state,
                true,
                &format!("Synced {game_count} game(s)."),
                Some(&ts_now),
                Some(&params),
            );
            Ok(json!({
                "path": target.to_string_lossy(),
                "games": total_merged,
                "merged": changed,
                "synced_at": ts_now,
            }))
        }
    }
}
