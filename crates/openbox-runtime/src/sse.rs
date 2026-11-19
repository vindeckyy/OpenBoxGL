//! Server-Sent Events (SSE) hub, bounded subscriber cap (`503 SSE_BUSY`),
//! and session recap state. Mirrors `pkg/state/sse.py`.

use std::collections::HashMap;
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use parking_lot::Mutex;
use serde_json::{Map, Value, json};
use tokio::sync::mpsc;

use crate::registry::ProcessRegistry;

pub const SSE_MAX_SUBSCRIBERS: usize = 16;
pub const SSE_QUEUE_SIZE: usize = 128;
pub const SSE_MAX_EVENT_BYTES: usize = 64 * 1024;
pub const SSE_BUSY_RETRY_AFTER: u64 = 5;
pub const SESSION_RA_BASELINE_LIMIT: usize = 64;

#[derive(Debug, Clone)]
pub struct SseMessage {
    pub kind: String,
    pub data: String,
}

pub struct SseSubscription {
    id: u64,
    pub rx: mpsc::Receiver<SseMessage>,
    hub: Arc<SseHub>,
}

impl Drop for SseSubscription {
    fn drop(&mut self) {
        self.hub.unregister(self.id);
    }
}

pub struct SseHub {
    next_id: AtomicU64,
    subscribers: Mutex<HashMap<u64, mpsc::Sender<SseMessage>>>,
    last_recap: Mutex<Option<Value>>,
    ra_baselines: Mutex<Vec<(String, Value)>>,
}

impl Default for SseHub {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Debug, Clone)]
pub struct SessionEventParams<'a> {
    pub kind: &'a str,
    pub launch_id: &'a str,
    pub game_name: &'a str,
    pub exit_code: Option<i32>,
    pub seconds: Option<i64>,
    pub timed_out: Option<bool>,
}

impl SseHub {
    pub fn new() -> Self {
        Self {
            next_id: AtomicU64::new(1),
            subscribers: Mutex::new(HashMap::new()),
            last_recap: Mutex::new(None),
            ra_baselines: Mutex::new(Vec::new()),
        }
    }

    pub fn subscribe(self: &Arc<Self>) -> Option<SseSubscription> {
        let mut guard = self.subscribers.lock();
        if guard.len() >= SSE_MAX_SUBSCRIBERS {
            return None;
        }
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let (tx, rx) = mpsc::channel(SSE_QUEUE_SIZE);
        guard.insert(id, tx);
        Some(SseSubscription {
            id,
            rx,
            hub: Arc::clone(self),
        })
    }

    fn unregister(&self, id: u64) {
        self.subscribers.lock().remove(&id);
    }

    pub fn subscriber_count(&self) -> usize {
        self.subscribers.lock().len()
    }

    pub fn broadcast_event(&self, kind: &str, payload: &Value) {
        let Ok(mut data) = serde_json::to_string(payload) else {
            return;
        };
        if data.len() > SSE_MAX_EVENT_BYTES {
            data = format!(r#"{{"truncated":true,"bytes":{}}}"#, data.len());
        }
        let clean_kind: String = kind
            .chars()
            .filter(|c| *c != '\r' && *c != '\n')
            .take(64)
            .collect();
        let msg = SseMessage {
            kind: clean_kind,
            data,
        };
        let mut dead = Vec::new();
        {
            let guard = self.subscribers.lock();
            for (&id, tx) in guard.iter() {
                if tx.try_send(msg.clone()).is_err() {
                    dead.push(id);
                }
            }
        }
        if !dead.is_empty() {
            let mut guard = self.subscribers.lock();
            for id in dead {
                guard.remove(&id);
            }
        }
    }

    pub fn session_event(
        &self,
        registry: &ProcessRegistry,
        data_dir: &Path,
        params: SessionEventParams<'_>,
    ) {
        let mut ev = Map::new();
        ev.insert("kind".into(), Value::String(params.kind.to_owned()));
        ev.insert(
            "launch_id".into(),
            Value::String(params.launch_id.to_owned()),
        );
        ev.insert("game".into(), Value::String(params.game_name.to_owned()));
        ev.insert(
            "time".into(),
            Value::String(
                jiff::Timestamp::now()
                    .to_string()
                    .chars()
                    .take(19)
                    .collect(),
            ),
        );
        if let Some(code) = params.exit_code {
            ev.insert("exit_code".into(), Value::from(code));
        }
        if let Some(t) = params.timed_out {
            ev.insert("timed_out".into(), Value::Bool(t));
        }
        if let Some(s) = params.seconds {
            ev.insert("seconds".into(), Value::from(s));
        }
        let event = registry.record_event(ev);
        if params.kind == "started" {
            let stable_gid = registry
                .lock()
                .running
                .get(params.launch_id)
                .and_then(|v| v.get("stable_game_id"))
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            if !stable_gid.is_empty() {
                self.capture_ra_baseline(data_dir, params.launch_id, &stable_gid);
            }
        }
        self.broadcast_event(params.kind, &event);
    }

    pub fn capture_ra_baseline(&self, data_dir: &Path, launch_id: &str, game_id: &str) {
        if launch_id.is_empty() {
            return;
        }
        let snap = ra_cache_snapshot(data_dir, game_id);
        let mut guard = self.ra_baselines.lock();
        guard.retain(|(k, _)| k != launch_id);
        guard.push((launch_id.to_owned(), snap));
        while guard.len() > SESSION_RA_BASELINE_LIMIT {
            guard.remove(0);
        }
    }

    pub fn pop_ra_baseline(&self, launch_id: &str) -> Option<Value> {
        let mut guard = self.ra_baselines.lock();
        guard
            .iter()
            .position(|(k, _)| k == launch_id)
            .map(|idx| guard.remove(idx).1)
    }

    pub fn store_session_recap(&self, payload: Value) {
        *self.last_recap.lock() = Some(payload);
    }

    pub fn last_session_recap(&self) -> Option<Value> {
        self.last_recap.lock().clone()
    }
}

pub fn ra_cache_snapshot(data_dir: &Path, game_id: &str) -> Value {
    let gid = game_id.trim();
    if gid.is_empty() {
        return json!({});
    }
    let path = data_dir
        .join("cache")
        .join("retroachievements")
        .join(format!("{gid}.json"));
    std::fs::read_to_string(&path)
        .ok()
        .and_then(|s| serde_json::from_str::<Value>(&s).ok())
        .filter(Value::is_object)
        .unwrap_or_else(|| json!({}))
}
