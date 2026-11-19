//! Persistent notification feed helpers. Mirrors `notifications.py`.

use std::collections::HashSet;

use serde_json::{Map, Value, json};
use uuid::Uuid;

pub const CAP: usize = 200;
pub const DEDUPE_SECONDS: f64 = 600.0;
pub const MAX_PARAMS: usize = 10;
pub const MAX_PARAM_TEXT: usize = 200;
pub const CLOUD_SYNC_KIND: &str = "cloud_sync";

pub const SERVER_NOTICE_KEYS: &[(&str, &[&str])] = &[
    ("notify.server.cloud_sync_ok_title", &[]),
    ("notify.server.cloud_sync_ok_body", &["count"]),
    ("notify.server.cloud_sync_ok_body_one", &["count"]),
    ("notify.server.cloud_sync_failed_title", &[]),
    ("notify.server.cloud_sync_failed_body", &["detail"]),
    ("notify.server.launch_audit_title", &["blocked", "warning"]),
    (
        "notify.server.launch_audit_body",
        &["blocked", "warning", "before_blocked", "before_warning"],
    ),
    ("notify.server.defs_rejected_title", &[]),
    ("notify.server.defs_rejected_body", &["detail"]),
    ("notify.server.webhook_failed_title", &[]),
    ("notify.server.webhook_failed_body", &["event_id", "detail"]),
    ("notify.server.capsule_unlocked_title", &[]),
    ("notify.server.capsule_unlocked_body", &["title"]),
    ("notify.server.deal_drop_title", &[]),
    (
        "notify.server.deal_drop_body",
        &["name", "price", "discount"],
    ),
];

#[derive(Debug, Clone, Default)]
pub struct NotificationSpec<'a> {
    pub kind: &'a str,
    pub level: &'a str,
    pub title: &'a str,
    pub body: &'a str,
    pub source: &'a str,
    pub correlation_id: &'a str,
    pub dedupe_key: &'a str,
    pub title_key: &'a str,
    pub body_key: &'a str,
    pub params: Option<&'a Map<String, Value>>,
    pub now: Option<&'a str>,
}

fn truncate_chars(s: &str, max: usize) -> String {
    s.chars().take(max).collect()
}

fn clean_params(params: Option<&Map<String, Value>>) -> Map<String, Value> {
    let Some(map) = params else {
        return Map::new();
    };
    let mut out = Map::new();
    for (k, v) in map.iter().take(MAX_PARAMS) {
        if k.is_empty() {
            continue;
        }
        let cleaned = match v {
            Value::Bool(_) | Value::Number(_) => v.clone(),
            Value::String(s) => Value::String(truncate_chars(s, MAX_PARAM_TEXT)),
            other => Value::String(truncate_chars(&other.to_string(), MAX_PARAM_TEXT)),
        };
        out.insert(k.clone(), cleaned);
    }
    out
}

fn now_iso() -> String {
    jiff::Timestamp::now().to_string()
}

fn parse_ts(raw: &str) -> f64 {
    raw.parse::<jiff::Timestamp>()
        .map(|ts| ts.as_second() as f64)
        .unwrap_or(0.0)
}

pub fn add_notification(state: &mut Value, spec: NotificationSpec<'_>) -> Value {
    let Some(root) = state.as_object_mut() else {
        return json!({});
    };
    if !root.get("notifications").is_some_and(Value::is_array) {
        root.insert("notifications".into(), Value::Array(Vec::new()));
    }
    let items = root["notifications"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    let created_at = spec.now.map(str::to_owned).unwrap_or_else(now_iso);
    let created_secs = parse_ts(&created_at);

    for item in &items {
        let Some(obj) = item.as_object() else {
            continue;
        };
        let same_key = !spec.dedupe_key.is_empty()
            && obj.get("dedupe_key").and_then(Value::as_str) == Some(spec.dedupe_key);
        let same_recent = obj.get("kind").and_then(Value::as_str) == Some(spec.kind)
            && obj.get("title").and_then(Value::as_str) == Some(spec.title)
            && (created_secs
                - parse_ts(obj.get("created_at").and_then(Value::as_str).unwrap_or("")))
                < DEDUPE_SECONDS;
        if same_key || same_recent {
            return item.clone();
        }
    }

    let id = format!("nt-{}", &Uuid::new_v4().simple().to_string()[..12]);
    let mut doc = Map::new();
    doc.insert("id".into(), Value::String(id));
    doc.insert("kind".into(), Value::String(spec.kind.to_owned()));
    doc.insert("level".into(), Value::String(spec.level.to_owned()));
    doc.insert(
        "title".into(),
        Value::String(truncate_chars(spec.title, 200)),
    );
    doc.insert(
        "body".into(),
        Value::String(truncate_chars(spec.body, 2000)),
    );
    doc.insert("created_at".into(), Value::String(created_at));
    doc.insert("read".into(), Value::Bool(false));
    if !spec.source.is_empty() {
        doc.insert(
            "source".into(),
            Value::String(truncate_chars(spec.source, 200)),
        );
    }
    if !spec.correlation_id.is_empty() {
        doc.insert(
            "correlation_id".into(),
            Value::String(truncate_chars(spec.correlation_id, 200)),
        );
    }
    if !spec.dedupe_key.is_empty() {
        doc.insert(
            "dedupe_key".into(),
            Value::String(truncate_chars(spec.dedupe_key, 200)),
        );
    }
    if !spec.title_key.is_empty() {
        doc.insert(
            "title_key".into(),
            Value::String(truncate_chars(spec.title_key, 120)),
        );
    }
    if !spec.body_key.is_empty() {
        doc.insert(
            "body_key".into(),
            Value::String(truncate_chars(spec.body_key, 120)),
        );
    }
    let cleaned_params = clean_params(spec.params);
    if !cleaned_params.is_empty() {
        doc.insert("params".into(), Value::Object(cleaned_params));
    }

    let created = Value::Object(doc);
    let mut updated = Vec::with_capacity(items.len().min(CAP - 1) + 1);
    updated.push(created.clone());
    updated.extend(items.into_iter().take(CAP - 1));
    root.insert("notifications".into(), Value::Array(updated));
    created
}

pub fn unread_count(state: &Value) -> usize {
    state
        .get("notifications")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .filter(|item| {
                    item.as_object()
                        .is_some_and(|o| !o.get("read").and_then(Value::as_bool).unwrap_or(false))
                })
                .count()
        })
        .unwrap_or(0)
}

pub fn mark_read(state: &mut Value, ids: Option<&[String]>) {
    let wanted: HashSet<&str> = ids
        .map(|s| s.iter().map(String::as_str).collect())
        .unwrap_or_default();
    let Some(arr) = state
        .as_object_mut()
        .and_then(|o| o.get_mut("notifications"))
        .and_then(Value::as_array_mut)
    else {
        return;
    };
    for item in arr {
        if let Some(obj) = item.as_object_mut() {
            let id = obj.get("id").and_then(Value::as_str).unwrap_or("");
            if wanted.is_empty() || wanted.contains(id) {
                obj.insert("read".into(), Value::Bool(true));
            }
        }
    }
}

pub fn clear(state: &mut Value, ids: Option<&[String]>) {
    let wanted: HashSet<&str> = ids
        .map(|s| s.iter().map(String::as_str).collect())
        .unwrap_or_default();
    let Some(root) = state.as_object_mut() else {
        return;
    };
    if wanted.is_empty() {
        root.insert("notifications".into(), Value::Array(Vec::new()));
        return;
    }
    let kept: Vec<Value> = root
        .get("notifications")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .filter(|item| {
            item.as_object().is_some_and(|o| {
                let id = o.get("id").and_then(Value::as_str).unwrap_or("");
                !wanted.contains(id)
            })
        })
        .collect();
    root.insert("notifications".into(), Value::Array(kept));
}

pub fn record_cloud_sync_outcome(
    state: &mut Value,
    success: bool,
    body: &str,
    now: Option<&str>,
    params: Option<&Map<String, Value>>,
) -> Value {
    if let Some(root) = state.as_object_mut() {
        let kept: Vec<Value> = root
            .get("notifications")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default()
            .into_iter()
            .filter(|item| {
                item.as_object()
                    .and_then(|o| o.get("kind"))
                    .and_then(Value::as_str)
                    != Some(CLOUD_SYNC_KIND)
            })
            .collect();
        root.insert("notifications".into(), Value::Array(kept));
    }
    let mut fallback = Map::new();
    fallback.insert("detail".into(), Value::String(body.to_owned()));
    let effective_params = params.unwrap_or(&fallback);
    add_notification(
        state,
        NotificationSpec {
            kind: CLOUD_SYNC_KIND,
            level: if success { "info" } else { "error" },
            title: if success {
                "Cloud sync succeeded"
            } else {
                "Cloud sync failed"
            },
            body,
            source: "cloud_sync",
            correlation_id: "",
            dedupe_key: "cloud_sync:last",
            title_key: if success {
                "notify.server.cloud_sync_ok_title"
            } else {
                "notify.server.cloud_sync_failed_title"
            },
            body_key: if success {
                "notify.server.cloud_sync_ok_body"
            } else {
                "notify.server.cloud_sync_failed_body"
            },
            params: Some(effective_params),
            now,
        },
    )
}
