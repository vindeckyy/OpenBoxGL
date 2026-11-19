//! Process registry — single source of truth for active game session globals.
//! Mirrors `pkg/state/registry.py`.

use std::collections::HashMap;

use parking_lot::Mutex;
use serde::{Deserialize, Serialize};
use serde_json::Value;

pub const SESSION_EVENTS_CAP: usize = 100;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SessionInfo {
    pub launch_id: String,
    pub stable_game_id: String,
    pub pid: u32,
    pub pgid: i32,
    pub proc_start_time: f64,
    pub cmd_fingerprint: String,
    pub game_name: String,
}

#[derive(Debug, Default)]
pub struct RegistryState {
    pub running: HashMap<String, Value>,
    pub pending_launches: HashMap<String, Value>,
    pub session_events: Vec<Value>,
    pub event_sequence: u64,
}

pub struct ProcessRegistry {
    inner: Mutex<RegistryState>,
}

impl Default for ProcessRegistry {
    fn default() -> Self {
        Self::new()
    }
}

impl ProcessRegistry {
    pub fn new() -> Self {
        Self {
            inner: Mutex::new(RegistryState::default()),
        }
    }

    pub fn lock(&self) -> parking_lot::MutexGuard<'_, RegistryState> {
        self.inner.lock()
    }

    pub fn is_empty(&self) -> bool {
        self.inner.lock().running.is_empty()
    }

    pub fn running_snapshot(&self) -> HashMap<String, Value> {
        self.inner.lock().running.clone()
    }

    pub fn events_snapshot(&self) -> Vec<Value> {
        self.inner.lock().session_events.clone()
    }

    pub fn record_event(&self, mut event: serde_json::Map<String, Value>) -> Value {
        let mut guard = self.inner.lock();
        guard.event_sequence += 1;
        event.insert("id".into(), Value::from(guard.event_sequence));
        let val = Value::Object(event);
        guard.session_events.push(val.clone());
        if guard.session_events.len() > SESSION_EVENTS_CAP {
            let drain = guard.session_events.len() - SESSION_EVENTS_CAP;
            guard.session_events.drain(..drain);
        }
        val
    }
}
