//! Pure helpers for OpenBox's persistent play queue. Mirrors `play_queue.py`.

use std::collections::{HashMap, HashSet};
use std::path::Path;

use openbox_platform::expand_user;
use serde_json::{Value, json};

pub const CAP: usize = 500;
pub const NOTE_LIMIT: usize = 200;

fn now_iso() -> String {
    jiff::Timestamp::now().to_string()
}

pub fn normalize_queue(value: Option<&Value>) -> Vec<Value> {
    let Some(arr) = value.and_then(Value::as_array) else {
        return Vec::new();
    };
    let mut out = Vec::new();
    for item in arr.iter().take(CAP) {
        let Some(obj) = item.as_object() else {
            continue;
        };
        let gid = obj
            .get("game_id")
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or("");
        if gid.is_empty() {
            continue;
        }
        let added = obj
            .get("added_at")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .unwrap_or_else(now_iso);
        let note: String = obj
            .get("note")
            .and_then(Value::as_str)
            .unwrap_or("")
            .chars()
            .take(NOTE_LIMIT)
            .collect();
        let skip = obj.get("skip").and_then(Value::as_bool).unwrap_or(false);
        out.push(json!({
            "game_id": gid,
            "added_at": added,
            "note": note,
            "skip": skip,
        }));
    }
    out
}

pub fn resolve_queue(state: &Value) -> Vec<Value> {
    let mut games: HashMap<&str, &Value> = HashMap::new();
    if let Some(arr) = state.get("games").and_then(Value::as_array) {
        for g in arr {
            if let Some(gid) = g.get("game_id").and_then(Value::as_str) {
                games.insert(gid, g);
            }
        }
    }
    normalize_queue(state.get("queue"))
        .into_iter()
        .map(|entry| {
            let gid = entry["game_id"].as_str().unwrap_or("");
            let game = games.get(gid).copied();
            let game_path = game
                .and_then(|g| g.get("path"))
                .and_then(Value::as_str)
                .unwrap_or("");
            let path_exists = game.is_some()
                && !game_path.is_empty()
                && expand_user(Path::new(game_path)).exists();
            let mut obj = entry.as_object().cloned().unwrap_or_default();
            obj.insert(
                "name".into(),
                Value::String(
                    game.and_then(|g| g.get("name"))
                        .and_then(Value::as_str)
                        .unwrap_or("Missing")
                        .to_owned(),
                ),
            );
            obj.insert(
                "platform".into(),
                Value::String(
                    game.and_then(|g| g.get("platform"))
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned(),
                ),
            );
            obj.insert(
                "cover".into(),
                Value::String(
                    game.and_then(|g| g.get("cover"))
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned(),
                ),
            );
            obj.insert("path_exists".into(), Value::Bool(path_exists));
            obj.insert("missing".into(), Value::Bool(game.is_none()));
            Value::Object(obj)
        })
        .collect()
}

pub fn enqueue(
    state: &mut Value,
    game_ids: &[String],
    position: Option<isize>,
    note: &str,
) -> Result<Vec<String>, String> {
    if game_ids.iter().any(|s| s.trim().is_empty()) {
        return Err("game_ids must be a list of strings.".to_owned());
    }
    let known: HashSet<String> = state
        .get("games")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .filter_map(|g| g.get("game_id").and_then(Value::as_str).map(str::to_owned))
                .collect()
        })
        .unwrap_or_default();
    for gid in game_ids {
        if !known.contains(gid) {
            return Err(format!("Game not found: {gid}"));
        }
    }
    let mut queue = normalize_queue(state.get("queue"));
    let clean_note: String = note.chars().take(NOTE_LIMIT).collect();
    let entries: Vec<Value> = game_ids
        .iter()
        .map(|gid| {
            json!({
                "game_id": gid,
                "added_at": now_iso(),
                "note": clean_note,
                "skip": false,
            })
        })
        .collect();
    match position {
        None => queue.extend(entries),
        Some(pos) => {
            let idx = pos.clamp(0, queue.len() as isize) as usize;
            queue.splice(idx..idx, entries);
        }
    }
    queue.truncate(CAP);
    if let Some(root) = state.as_object_mut() {
        root.insert("queue".into(), Value::Array(queue));
    }
    Ok(game_ids.to_vec())
}

pub fn remove(state: &mut Value, game_ids: &[String]) -> usize {
    let ids: HashSet<&str> = game_ids.iter().map(String::as_str).collect();
    let before = normalize_queue(state.get("queue"));
    let before_len = before.len();
    let kept: Vec<Value> = before
        .into_iter()
        .filter(|item| !ids.contains(item["game_id"].as_str().unwrap_or("")))
        .collect();
    let removed = before_len - kept.len();
    if let Some(root) = state.as_object_mut() {
        root.insert("queue".into(), Value::Array(kept));
    }
    removed
}

pub fn reorder(state: &mut Value, ordered_game_ids: &[String]) -> Result<Vec<Value>, String> {
    let current = normalize_queue(state.get("queue"));
    let mut cur_ids: Vec<&str> = current
        .iter()
        .filter_map(|i| i["game_id"].as_str())
        .collect();
    let mut req_ids: Vec<&str> = ordered_game_ids.iter().map(String::as_str).collect();
    cur_ids.sort_unstable();
    req_ids.sort_unstable();
    if ordered_game_ids.len() != current.len() || cur_ids != req_ids {
        return Err("ordered_game_ids must contain the current queue entries.".to_owned());
    }
    let mut buckets: HashMap<String, Vec<Value>> = HashMap::new();
    for item in current {
        let gid = item["game_id"].as_str().unwrap_or("").to_owned();
        buckets.entry(gid).or_default().push(item);
    }
    let mut reordered = Vec::with_capacity(ordered_game_ids.len());
    for gid in ordered_game_ids {
        if let Some(list) = buckets.get_mut(gid)
            && !list.is_empty()
        {
            reordered.push(list.remove(0));
        }
    }
    if let Some(root) = state.as_object_mut() {
        root.insert("queue".into(), Value::Array(reordered.clone()));
    }
    Ok(reordered)
}

pub fn advance(state: &mut Value, current_game_id: Option<&str>) -> Option<Value> {
    let mut queue = normalize_queue(state.get("queue"));
    let mut start: isize = -1;
    if let Some(cur) = current_game_id.filter(|s| !s.is_empty()) {
        for (i, item) in queue.iter().enumerate() {
            if item["game_id"].as_str() == Some(cur) {
                start = i as isize;
                break;
            }
        }
    }
    let mut games: HashMap<String, bool> = HashMap::new();
    if let Some(arr) = state.get("games").and_then(Value::as_array) {
        for g in arr {
            if let Some(gid) = g.get("game_id").and_then(Value::as_str) {
                let has_path = g
                    .get("path")
                    .and_then(Value::as_str)
                    .is_some_and(|p| !p.is_empty());
                games.insert(gid.to_owned(), has_path);
            }
        }
    }
    let mut chosen = None;
    for item in queue.iter_mut().skip((start + 1) as usize) {
        if item["skip"].as_bool().unwrap_or(false) {
            continue;
        }
        let gid = item["game_id"].as_str().unwrap_or("");
        if !games.get(gid).copied().unwrap_or(false) {
            item["skip"] = Value::Bool(true);
            continue;
        }
        chosen = Some(item.clone());
        break;
    }
    if let Some(root) = state.as_object_mut() {
        root.insert("queue".into(), Value::Array(queue));
    }
    chosen
}
