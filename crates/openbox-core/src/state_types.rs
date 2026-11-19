//! Schema v7 library state types and helper accessors.
//!
//! Preserves all unknown fields on top-level state and individual games while exposing
//! ergonomic typed accessors for hot paths (search, projection, filtering, indexing).

use serde_json::{Map, Value, json};

use crate::identity::new_library_id;
use crate::version::{APP_VERSION, STATE_FORMAT, STATE_SCHEMA_VERSION};

/// Construct a fresh default schema v7 state matching `state_store.py::default_state()`.
pub fn default_state() -> Value {
    json!({
        "format": STATE_FORMAT,
        "schema_version": STATE_SCHEMA_VERSION,
        "library_id": new_library_id(),
        "created_with": APP_VERSION,
        "path_mode": "absolute",
        "library_root": "",
        "games": [],
        "profiles": {},
        "history": [],
        "settings": {},
        "playlists": [],
        "queue": [],
        "notifications": [],
        "ui_state": {},
        "active_sessions": [],
        "trash": [],
        "party_decks": [],
        "emulator_settings": {},
        "achievement_overrides": {},
        "deals": { "watchlist": [], "prices": {}, "last_poll_at": "" },
        "time_capsules": [],
        "itinerary": null
    })
}

/// Read a string field from a JSON object, trimming whitespace (empty string if absent/null).
pub fn get_trimmed_str(obj: &Map<String, Value>, key: &str) -> String {
    match obj.get(key) {
        Some(Value::String(s)) => s.trim().to_owned(),
        Some(Value::Number(n)) => n.to_string(),
        _ => String::new(),
    }
}

/// Read a boolean field using Python-like truthiness for booleans/ints/strings.
pub fn get_bool(obj: &Map<String, Value>, key: &str) -> bool {
    match obj.get(key) {
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_i64().is_some_and(|v| v != 0),
        Some(Value::String(s)) => !s.is_empty(),
        _ => false,
    }
}
