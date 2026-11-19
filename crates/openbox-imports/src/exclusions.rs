//! Import exclusion list for storefront rescans (`pkg/parity/parity_import_policy.py`).

use std::collections::HashSet;

use openbox_core::ApiError;
use serde_json::{Map, Value, json};

fn str_prop(game: &Value, key: &str) -> String {
    match game.get(key) {
        Some(Value::String(s)) => s.trim().to_owned(),
        Some(Value::Number(n)) => n.to_string(),
        _ => String::new(),
    }
}

pub fn exclusion_key(game: &Value) -> Option<Vec<String>> {
    if !str_prop(game, "steam_app_id").is_empty() {
        return Some(vec!["steam".into(), str_prop(game, "steam_app_id")]);
    }
    if !str_prop(game, "heroic_app_id").is_empty() {
        return Some(vec![
            "heroic".into(),
            str_prop(game, "source"),
            str_prop(game, "heroic_app_id"),
        ]);
    }
    for (field, label) in [
        ("lutris_id", "lutris"),
        ("gameyfin_id", "gameyfin"),
        ("gog_id", "gog"),
        ("ea_id", "ea"),
        ("ubisoft_id", "ubisoft"),
    ] {
        let val = str_prop(game, field);
        if !val.is_empty() {
            return Some(vec![label.into(), val]);
        }
    }
    None
}

pub fn list_exclusions(state: &Value) -> Vec<Value> {
    state
        .get("settings")
        .and_then(|s| s.get("import_exclusions"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
}

pub fn exclusion_set(state: &Value) -> HashSet<Vec<String>> {
    let mut blocked = HashSet::new();
    for item in list_exclusions(state) {
        let Some(obj) = item.as_object() else {
            continue;
        };
        let source = obj
            .get("source")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_lowercase();
        let external_id = obj
            .get("external_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_owned();
        if !source.is_empty() && !external_id.is_empty() {
            blocked.insert(vec![source, external_id]);
        }
        if let Some(heroic) = obj.get("heroic").and_then(Value::as_array)
            && heroic.len() == 3
        {
            blocked.insert(
                heroic
                    .iter()
                    .map(|v| v.as_str().unwrap_or("").to_owned())
                    .collect(),
            );
        }
    }
    blocked
}

pub fn add_exclusion(
    state: &mut Value,
    source: &str,
    external_id: &str,
    heroic_source: &str,
) -> Result<Value, ApiError> {
    let source_key = source.trim().to_lowercase();
    let external_id = external_id.trim().to_owned();
    if source_key.is_empty() || external_id.is_empty() {
        return Err(ApiError::bad_request(
            "Source and external id are required.",
        ));
    }
    let mut entry = Map::new();
    entry.insert("source".into(), Value::String(source_key.clone()));
    entry.insert("external_id".into(), Value::String(external_id.clone()));
    if !heroic_source.trim().is_empty() {
        entry.insert(
            "heroic".into(),
            json!(["heroic", heroic_source.trim().to_lowercase(), external_id]),
        );
    }
    let entry_val = Value::Object(entry);
    let mut items = list_exclusions(state);
    let exists = items.iter().any(|item| {
        item.as_object().is_some_and(|o| {
            o.get("source")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_lowercase()
                == source_key
                && o.get("external_id").and_then(Value::as_str).unwrap_or("") == external_id
        })
    });
    if exists {
        return Ok(entry_val);
    }
    items.push(entry_val.clone());
    if let Some(obj) = state.as_object_mut() {
        let settings = obj
            .entry("settings".to_owned())
            .or_insert_with(|| Value::Object(Map::new()));
        if let Some(s) = settings.as_object_mut() {
            s.insert("import_exclusions".into(), Value::Array(items));
        }
    }
    Ok(entry_val)
}

pub fn remove_exclusion(state: &mut Value, source: &str, external_id: &str) -> usize {
    let source_key = source.trim().to_lowercase();
    let external_id = external_id.trim();
    let before = list_exclusions(state);
    let orig_len = before.len();
    let items: Vec<Value> = before
        .into_iter()
        .filter(|item| {
            !item.as_object().is_some_and(|o| {
                o.get("source")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_lowercase()
                    == source_key
                    && o.get("external_id").and_then(Value::as_str).unwrap_or("") == external_id
            })
        })
        .collect();
    let removed = orig_len - items.len();
    if removed > 0
        && let Some(obj) = state.as_object_mut()
    {
        let settings = obj
            .entry("settings".to_owned())
            .or_insert_with(|| Value::Object(Map::new()));
        if let Some(s) = settings.as_object_mut() {
            s.insert("import_exclusions".into(), Value::Array(items));
        }
    }
    removed
}

pub fn filter_imported(imported: Vec<Value>, state: &Value) -> Vec<Value> {
    let blocked = exclusion_set(state);
    if blocked.is_empty() {
        return imported;
    }
    let mut kept = Vec::new();
    for game in imported {
        let Some(key) = exclusion_key(&game) else {
            kept.push(game);
            continue;
        };
        let normalized: Vec<String> = key
            .iter()
            .enumerate()
            .map(|(i, part)| {
                if i == 0 || (key.len() == 3 && i == 1) {
                    part.to_lowercase()
                } else {
                    part.clone()
                }
            })
            .collect();
        let blocked_match = blocked.contains(&normalized)
            || (normalized.len() >= 2
                && blocked.contains(&vec![normalized[0].clone(), normalized[1].clone()]));
        if !blocked_match {
            kept.push(game);
        }
    }
    kept
}
