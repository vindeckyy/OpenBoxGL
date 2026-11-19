//! Credential redaction for exports and backups. Mirrors `pkg/parity/parity_redact.py`.

use std::collections::HashMap;

use serde_json::{Map, Value};

pub const REDACTED_KEYS: &[&str] = &[
    "gameyfin_password",
    "obs_websocket_password",
    "integration_tokens",
    "secret",
    "api_key",
    "access_token",
    "credentials",
    "museum_kiosk_pin_hash",
];

pub const EXPORT_EXCLUDED_TOP_LEVEL_KEYS: &[&str] =
    &["previews", "operation_history", "operations"];

pub fn is_redacted_key(key: &str) -> bool {
    REDACTED_KEYS.contains(&key)
}

fn is_truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|x| x != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// Return a copy of `settings` with credential fields removed and webhook secrets converted to `secret_set`.
pub fn redact_settings(settings: &Map<String, Value>) -> Map<String, Value> {
    let mut cleaned = settings.clone();
    for key in REDACTED_KEYS {
        cleaned.remove(*key);
    }
    if let Some(Value::Array(hooks)) = cleaned.get("webhooks").cloned() {
        let mut new_hooks = Vec::with_capacity(hooks.len());
        for hook in hooks {
            if let Value::Object(mut obj) = hook {
                let secret = obj.remove("secret");
                let secret_set = secret.as_ref().is_some_and(is_truthy);
                obj.insert("secret_set".into(), Value::Bool(secret_set));
                new_hooks.push(Value::Object(obj));
            } else {
                new_hooks.push(hook);
            }
        }
        cleaned.insert("webhooks".into(), Value::Array(new_hooks));
    }
    cleaned
}

/// Return a detached copy of `state` with transient keys dropped and settings redacted.
pub fn redact_state_for_export(state: &Value) -> Value {
    let Some(obj) = state.as_object() else {
        return Value::Object(Map::new());
    };
    let mut cleaned = obj.clone();
    for key in EXPORT_EXCLUDED_TOP_LEVEL_KEYS {
        cleaned.remove(*key);
    }
    if let Some(Value::Object(settings)) = cleaned.get("settings") {
        cleaned.insert("settings".into(), Value::Object(redact_settings(settings)));
    } else if cleaned.contains_key("settings") {
        cleaned.insert("settings".into(), Value::Object(Map::new()));
    }
    Value::Object(cleaned)
}

/// After restoring a redacted backup, preserve existing local credentials and webhook secrets.
pub fn merge_preserved_credentials(
    restored_settings: &Map<String, Value>,
    existing_settings: &Map<String, Value>,
) -> Map<String, Value> {
    let mut merged = restored_settings.clone();
    for key in REDACTED_KEYS {
        let missing = !merged.get(*key).is_some_and(is_truthy);
        if missing && let Some(existing) = existing_settings.get(*key).filter(|v| is_truthy(v)) {
            merged.insert((*key).to_owned(), existing.clone());
        }
    }

    if let Some(Value::Array(hooks)) = merged.get("webhooks").cloned() {
        let mut existing_by_id: HashMap<String, Map<String, Value>> = HashMap::new();
        if let Some(Value::Array(existing_hooks)) = existing_settings.get("webhooks") {
            for hook in existing_hooks {
                if let Some(obj) = hook.as_object()
                    && let Some(id) = obj
                        .get("id")
                        .and_then(Value::as_str)
                        .filter(|s| !s.is_empty())
                {
                    existing_by_id.insert(id.to_owned(), obj.clone());
                }
            }
        }
        let mut new_hooks = Vec::with_capacity(hooks.len());
        for hook in hooks {
            if let Value::Object(mut obj) = hook {
                let hook_id = obj.get("id").and_then(Value::as_str).map(ToOwned::to_owned);
                obj.remove("secret_set");
                let has_secret = obj.get("secret").is_some_and(is_truthy);
                if !has_secret
                    && let Some(id) = hook_id
                    && let Some(prev) = existing_by_id.get(&id)
                    && let Some(prev_secret) = prev.get("secret").filter(|v| is_truthy(v))
                {
                    obj.insert("secret".into(), prev_secret.clone());
                }
                new_hooks.push(Value::Object(obj));
            } else {
                new_hooks.push(hook);
            }
        }
        merged.insert("webhooks".into(), Value::Array(new_hooks));
    }

    merged
}
