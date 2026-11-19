//! Publish recent games for the native tray menu (`pkg/parity/parity_tray_recent.py`).

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

use openbox_io::atomic_write_text;
use serde_json::{Map, Value};

pub const RECENT_LIMIT: usize = 8;

fn clean_name(val: &str) -> String {
    let joined = val
        .replace('\t', " ")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    let clipped: String = joined.chars().take(120).collect();
    if clipped.is_empty() {
        "Untitled".to_owned()
    } else {
        clipped
    }
}

pub fn recent_games(state: &Value, limit: usize) -> Vec<(String, String)> {
    let mut entries: Vec<(String, String)> = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();
    let mut games: HashMap<String, &Map<String, Value>> = HashMap::new();
    if let Some(arr) = state.get("games").and_then(Value::as_array) {
        for g in arr {
            if let Some(obj) = g.as_object() {
                let id = obj
                    .get("game_id")
                    .or_else(|| obj.get("id"))
                    .and_then(|v| match v {
                        Value::String(s) => Some(s.clone()),
                        Value::Number(n) => Some(n.to_string()),
                        _ => None,
                    })
                    .unwrap_or_default();
                if !id.is_empty() {
                    games.insert(id, obj);
                }
            }
        }
    }
    if let Some(history) = state.get("history").and_then(Value::as_array) {
        for session in history.iter().rev() {
            let Some(sobj) = session.as_object() else {
                continue;
            };
            let gid = sobj
                .get("game_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            if gid.is_empty() || !seen.insert(gid.clone()) {
                continue;
            }
            let name = sobj
                .get("game")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .or_else(|| {
                    games
                        .get(&gid)
                        .and_then(|g| g.get("name"))
                        .and_then(Value::as_str)
                })
                .unwrap_or("");
            entries.push((clean_name(name), gid));
            if entries.len() >= limit {
                return entries;
            }
        }
    }
    let mut played: Vec<(String, String, String)> = Vec::new();
    for g in games.values() {
        let lp = g.get("last_played").and_then(Value::as_str).unwrap_or("");
        let gid = g.get("game_id").and_then(Value::as_str).unwrap_or("");
        if !lp.is_empty() && !gid.is_empty() && !seen.contains(gid) {
            let name = g.get("name").and_then(Value::as_str).unwrap_or("");
            played.push((lp.to_owned(), gid.to_owned(), name.to_owned()));
        }
    }
    played.sort_by(|a, b| b.cmp(a));
    for (_, gid, name) in played {
        if !seen.insert(gid.clone()) {
            continue;
        }
        entries.push((clean_name(&name), gid));
        if entries.len() >= limit {
            break;
        }
    }
    entries
}

pub fn write_recent_games(
    data_parent: &Path,
    state: &Value,
    limit: usize,
) -> Result<PathBuf, String> {
    let path = data_parent.join("native-host-recent");
    let mut content = String::new();
    for (name, gid) in recent_games(state, limit) {
        content.push_str(&name);
        content.push('\t');
        content.push_str(&gid);
        content.push('\n');
    }
    atomic_write_text(&path, &content, 0o600).map_err(|e| e.to_string())?;
    Ok(path)
}
