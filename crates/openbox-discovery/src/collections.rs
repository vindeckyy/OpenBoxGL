//! Smart collections — named Backlog Radio queries pinned as living shelves (`pkg/parity/parity_collections.py`).

use jiff::civil::DateTime;
use serde_json::{Value, json};

use crate::query::{filter_games_by_query, parse_query};

pub const MAX_COLLECTIONS: usize = 50;
pub const MAX_NAME_LENGTH: usize = 80;
pub const MAX_QUERY_LENGTH: usize = 500;

pub fn list_collections(state: &Value) -> Vec<Value> {
    let Some(items) = state.get("smart_collections").and_then(Value::as_array) else {
        return Vec::new();
    };
    items
        .iter()
        .filter_map(|item| {
            let obj = item.as_object()?;
            let name = obj.get("name").and_then(Value::as_str).unwrap_or("");
            if name.trim().is_empty() {
                return None;
            }
            let query = obj.get("query").and_then(Value::as_str).unwrap_or("");
            Some(json!({"name": name, "query": query}))
        })
        .collect()
}

fn clean_name(name: &str) -> Result<String, String> {
    let text = name.trim();
    if text.is_empty() {
        return Err("Collection name is required.".to_owned());
    }
    if text.chars().count() > MAX_NAME_LENGTH {
        return Err(format!(
            "Collection name must be at most {MAX_NAME_LENGTH} characters."
        ));
    }
    Ok(text.to_owned())
}

fn clean_query(query: &str) -> Result<String, String> {
    let text = query.split_whitespace().collect::<Vec<_>>().join(" ");
    if text.is_empty() {
        return Err("Collection query is required.".to_owned());
    }
    if text.chars().count() > MAX_QUERY_LENGTH {
        return Err(format!(
            "Collection query must be at most {MAX_QUERY_LENGTH} characters."
        ));
    }
    Ok(text)
}

pub fn save_collection(state: &mut Value, name: &str, query: &str) -> Result<String, String> {
    let name = clean_name(name)?;
    let query = clean_query(query)?;
    let Some(root) = state.as_object_mut() else {
        return Err("State must be an object.".to_owned());
    };
    let slot = root
        .entry("smart_collections".to_owned())
        .or_insert_with(|| Value::Array(Vec::new()));
    if !slot.is_array() {
        *slot = Value::Array(Vec::new());
    }
    let entry = json!({"name": name, "query": query});
    if let Some(arr) = slot.as_array_mut() {
        for item in arr.iter_mut() {
            if item.get("name").and_then(Value::as_str) == Some(name.as_str()) {
                *item = entry;
                return Ok(name);
            }
        }
        if arr.len() >= MAX_COLLECTIONS {
            return Err(format!(
                "At most {MAX_COLLECTIONS} smart collections can be saved."
            ));
        }
        arr.push(entry);
    }
    Ok(name)
}

pub fn delete_collection(state: &mut Value, name: &str) -> bool {
    let clean = name.trim();
    let Some(arr) = state
        .get_mut("smart_collections")
        .and_then(Value::as_array_mut)
    else {
        return false;
    };
    let before = arr.len();
    arr.retain(|item| item.get("name").and_then(Value::as_str) != Some(clean));
    arr.len() != before
}

pub fn evaluate_collection(state: &Value, query: &str, now: Option<DateTime>) -> usize {
    let parsed = parse_query(query, now);
    let games = state
        .get("games")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    filter_games_by_query(games, &parsed, now).len()
}

pub fn collections_with_counts(state: &Value, now: Option<DateTime>) -> Vec<Value> {
    list_collections(state)
        .into_iter()
        .map(|mut item| {
            let query = item
                .get("query")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            let count = evaluate_collection(state, &query, now);
            if let Some(m) = item.as_object_mut() {
                m.insert("count".into(), json!(count));
            }
            item
        })
        .collect()
}
