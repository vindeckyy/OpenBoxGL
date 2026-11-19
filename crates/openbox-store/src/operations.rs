//! Authoritative durable operation service (`operations.json` + `operations_items/<job_id>.json`).
//! Mirrors `pkg/state/operations.py` (ADR 0011).

use std::collections::HashMap;
use std::fs;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use openbox_core::redact::is_redacted_key;
use openbox_core::{ApiError, MACHINES};
use openbox_io::{atomic_write_text, tokenize_home_paths};
use parking_lot::Mutex;
use serde_json::{Map, Value, json};
use uuid::Uuid;

pub const OPERATIONS_VERSION: u32 = 1;
pub const MAX_OPERATIONS: usize = 100;
pub const RETENTION_DAYS: i64 = 30;
pub const MAX_PROGRESS_PERSIST_INTERVAL: Duration = Duration::from_millis(500);
pub const DEFAULT_LIST_LIMIT: usize = 50;
pub const MAX_LIST_LIMIT: usize = 100;
pub const MAX_ITEMS_LIST_LIMIT: usize = 200;

pub const SSE_EVENT_QUEUED: &str = "job.queued";
pub const SSE_EVENT_PROGRESS: &str = "job.progress";
pub const SSE_EVENT_CANCELLING: &str = "job.cancelling";
pub const SSE_EVENT_FINISHED: &str = "job.finished";
pub const SSE_EVENT_INTERRUPTED: &str = "job.interrupted";

pub type OperationObserver = Arc<dyn Fn(&str, &Value) + Send + Sync>;

#[derive(Debug, Clone, Copy)]
struct OpPolicy {
    resume: &'static str, // "checkpoint" | "retry_only" | "failed_only"
    retry: bool,
    refuse_cancel_promote: bool,
}

fn policy_for(op_type: &str) -> OpPolicy {
    match op_type {
        "setup.scan"
        | "setup.revalidate"
        | "metadata.match_preview"
        | "media.cleanup"
        | "saves.scan" => OpPolicy {
            resume: "checkpoint",
            retry: true,
            refuse_cancel_promote: false,
        },
        "metadata.apply" => OpPolicy {
            resume: "checkpoint",
            retry: true,
            refuse_cancel_promote: true,
        },
        "media.bulk_download" => OpPolicy {
            resume: "checkpoint",
            retry: true,
            refuse_cancel_promote: false,
        },
        "media.memories_import" => OpPolicy {
            resume: "retry_only",
            retry: true,
            refuse_cancel_promote: false,
        },
        "setup.commit"
        | "metadata.db_sync"
        | "emulator.install"
        | "emulator.update"
        | "gameyfin.install"
        | "saves.backup"
        | "library.backup"
        | "library.restore"
        | "library.export"
        | "screenscraper.match"
        | "screenscraper.apply"
        | "steamgrid.match"
        | "steamgrid.apply"
        | "storefront.auto_import"
        | "clips.reel"
        | "cloud.sync"
        | "updater.install" => OpPolicy {
            resume: "retry_only",
            retry: true,
            refuse_cancel_promote: true,
        },
        _ => OpPolicy {
            resume: "retry_only",
            retry: true,
            refuse_cancel_promote: false,
        },
    }
}

fn now_iso() -> String {
    jiff::Timestamp::now().to_string()
}

pub fn sanitize_operation_input(input: &Value) -> Value {
    let Some(obj) = input.as_object() else {
        return json!({});
    };
    let mut cleaned = Map::new();
    for (k, v) in obj {
        if is_redacted_key(k) {
            continue;
        }
        cleaned.insert(k.clone(), tokenize_home_paths(v));
    }
    Value::Object(cleaned)
}

fn refresh_capabilities(doc: &mut Map<String, Value>, promoting: bool) {
    let op_type = doc.get("type").and_then(Value::as_str).unwrap_or("");
    let state = doc.get("state").and_then(Value::as_str).unwrap_or("queued");
    let policy = policy_for(op_type);
    let machine = &MACHINES["operation"];
    let is_active = machine.active.as_ref().is_some_and(|a| a.contains(state));
    let is_terminal = machine.terminal.contains(state);
    let has_checkpoint = doc
        .get("checkpoint")
        .and_then(Value::as_object)
        .is_some_and(|o| !o.is_empty());

    let can_retry = policy.retry && is_terminal;
    let can_resume = policy.resume == "checkpoint" && state == "interrupted" && has_checkpoint;
    let mut can_cancel = is_active;
    if promoting && policy.refuse_cancel_promote {
        can_cancel = false;
    }
    doc.insert("can_cancel".into(), Value::Bool(can_cancel));
    doc.insert("can_retry".into(), Value::Bool(can_retry));
    doc.insert("can_resume".into(), Value::Bool(can_resume));
}

struct Inner {
    operations: HashMap<String, Value>,
    order: Vec<String>,
    last_persist: HashMap<String, Instant>,
    observers: Vec<OperationObserver>,
}

pub struct OperationService {
    data_dir: PathBuf,
    path: PathBuf,
    inner: Mutex<Inner>,
}

impl OperationService {
    pub fn new(data_dir: impl Into<PathBuf>) -> Self {
        let data_dir = data_dir.into();
        let path = data_dir.join("operations.json");
        let svc = Self {
            data_dir,
            path,
            inner: Mutex::new(Inner {
                operations: HashMap::new(),
                order: Vec::new(),
                last_persist: HashMap::new(),
                observers: Vec::new(),
            }),
        };
        svc.load_from_disk();
        svc
    }

    pub fn set_observer(&self, observer: OperationObserver) {
        self.inner.lock().observers.push(observer);
    }

    fn emit(&self, event_name: &str, payload: &Value) {
        let observers = self.inner.lock().observers.clone();
        for obs in observers {
            obs(event_name, payload);
        }
    }

    fn load_from_disk(&self) {
        let Ok(raw) = fs::read_to_string(&self.path) else {
            return;
        };
        let Ok(val) = serde_json::from_str::<Value>(&raw) else {
            return;
        };
        let Some(entries) = val.get("operations").and_then(Value::as_array) else {
            return;
        };
        let active = MACHINES["operation"].active.as_ref();
        let mut guard = self.inner.lock();
        guard.operations.clear();
        guard.order.clear();
        for entry in entries {
            let Some(mut obj) = entry.as_object().cloned() else {
                continue;
            };
            let Some(job_id) = obj
                .get("job_id")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .map(ToOwned::to_owned)
            else {
                continue;
            };
            let st = obj
                .get("state")
                .and_then(Value::as_str)
                .unwrap_or("queued")
                .to_owned();
            if active.is_some_and(|a| a.contains(st.as_str())) {
                let now = now_iso();
                obj.insert("state".into(), Value::String("interrupted".to_owned()));
                if obj
                    .get("message")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .is_empty()
                {
                    obj.insert(
                        "message".into(),
                        Value::String("Interrupted by shutdown.".to_owned()),
                    );
                }
                obj.insert("updated_at".into(), Value::String(now.clone()));
                if obj.get("finished_at").is_none_or(Value::is_null) {
                    obj.insert("finished_at".into(), Value::String(now));
                }
            }
            refresh_capabilities(&mut obj, false);
            guard.operations.insert(job_id.clone(), Value::Object(obj));
            guard.order.push(job_id);
        }
        let Inner {
            operations, order, ..
        } = &mut *guard;
        order.sort_by(|a, b| {
            let ua = operations
                .get(a)
                .and_then(|v| v.get("updated_at"))
                .and_then(Value::as_str)
                .unwrap_or("");
            let ub = operations
                .get(b)
                .and_then(|v| v.get("updated_at"))
                .and_then(Value::as_str)
                .unwrap_or("");
            ub.cmp(ua)
        });
    }

    pub fn persist(&self) {
        let payload = {
            let mut guard = self.inner.lock();
            if guard.order.len() > MAX_OPERATIONS {
                let drop_ids = guard.order.split_off(MAX_OPERATIONS);
                for id in drop_ids {
                    guard.operations.remove(&id);
                }
            }
            let ops: Vec<Value> = guard
                .order
                .iter()
                .filter_map(|id| guard.operations.get(id).cloned())
                .collect();
            json!({
                "version": OPERATIONS_VERSION,
                "operations": ops,
            })
        };
        let text = format!(
            "{}\n",
            serde_json::to_string_pretty(&payload).unwrap_or_else(|_| "{}".to_owned())
        );
        let _ = atomic_write_text(&self.path, &text, 0o600);
    }

    pub fn create(&self, operation_type: &str, title: &str, input: Option<&Value>) -> Value {
        let job_id = Uuid::new_v4().simple().to_string();
        let now = now_iso();
        let mut doc = Map::new();
        doc.insert("job_id".into(), Value::String(job_id.clone()));
        doc.insert("root_job_id".into(), Value::String(job_id.clone()));
        doc.insert("retry_of".into(), Value::Null);
        doc.insert("resume_of".into(), Value::Null);
        doc.insert("type".into(), Value::String(operation_type.to_owned()));
        doc.insert("title".into(), Value::String(title.to_owned()));
        doc.insert("state".into(), Value::String("queued".to_owned()));
        doc.insert("phase".into(), Value::Null);
        doc.insert("current".into(), json!(0));
        doc.insert("total".into(), Value::Null);
        doc.insert("message".into(), Value::String(String::new()));
        doc.insert("created_at".into(), Value::String(now.clone()));
        doc.insert("updated_at".into(), Value::String(now));
        doc.insert("started_at".into(), Value::Null);
        doc.insert("finished_at".into(), Value::Null);
        doc.insert(
            "input".into(),
            sanitize_operation_input(input.unwrap_or(&json!({}))),
        );
        doc.insert("checkpoint".into(), Value::Null);
        doc.insert("result".into(), Value::Null);
        doc.insert("error".into(), Value::Null);
        refresh_capabilities(&mut doc, false);
        let val = Value::Object(doc);
        {
            let mut guard = self.inner.lock();
            guard.operations.insert(job_id.clone(), val.clone());
            guard.order.insert(0, job_id);
        }
        self.persist();
        self.emit(
            SSE_EVENT_QUEUED,
            &json!({
                "job_id": val["job_id"],
                "type": val["type"],
                "title": val["title"],
                "state": val["state"],
            }),
        );
        val
    }

    pub fn get(&self, job_id: &str) -> Option<Value> {
        self.inner.lock().operations.get(job_id).cloned()
    }

    pub fn list_jobs(
        &self,
        cursor: Option<&str>,
        limit: usize,
        type_filter: Option<&str>,
        state_filter: Option<&str>,
        root_job_id: Option<&str>,
    ) -> Value {
        let lim = limit.clamp(1, MAX_LIST_LIMIT);
        let ordered: Vec<Value> = {
            let guard = self.inner.lock();
            guard
                .order
                .iter()
                .filter_map(|id| guard.operations.get(id).cloned())
                .filter(|doc| {
                    type_filter.is_none_or(|t| doc.get("type").and_then(Value::as_str) == Some(t))
                        && state_filter
                            .is_none_or(|s| doc.get("state").and_then(Value::as_str) == Some(s))
                        && root_job_id.is_none_or(|r| {
                            doc.get("root_job_id").and_then(Value::as_str) == Some(r)
                        })
                })
                .collect()
        };
        let start = cursor
            .and_then(|c| {
                ordered
                    .iter()
                    .position(|d| d.get("job_id").and_then(Value::as_str) == Some(c))
                    .map(|i| i + 1)
            })
            .unwrap_or(0);
        let end = (start + lim).min(ordered.len());
        let page = ordered[start..end].to_vec();
        let next_cursor = if page.len() == lim && end < ordered.len() {
            page.last()
                .and_then(|d| d.get("job_id"))
                .and_then(Value::as_str)
                .map(|s| Value::String(s.to_owned()))
                .unwrap_or(Value::Null)
        } else {
            Value::Null
        };
        json!({
            "cursor": cursor,
            "next_cursor": next_cursor,
            "jobs": page,
        })
    }

    pub fn mark_running(&self, job_id: &str, message: &str) -> Result<Value, ApiError> {
        let updated = {
            let mut guard = self.inner.lock();
            let Some(Value::Object(obj)) = guard.operations.get_mut(job_id) else {
                return Err(ApiError::job_state_conflict(format!(
                    "Operation {job_id} is not runnable."
                )));
            };
            let current = obj.get("state").and_then(Value::as_str).unwrap_or("");
            if !MACHINES["operation"].allows("start", Some(current), Some("running")) {
                return Err(ApiError::job_state_conflict(format!(
                    "Operation {job_id} is not runnable."
                )));
            }
            let now = now_iso();
            obj.insert("state".into(), Value::String("running".to_owned()));
            if obj.get("started_at").is_none_or(Value::is_null) {
                obj.insert("started_at".into(), Value::String(now.clone()));
            }
            obj.insert("updated_at".into(), Value::String(now));
            if !message.is_empty() {
                obj.insert("message".into(), Value::String(message.to_owned()));
            }
            refresh_capabilities(obj, false);
            Value::Object(obj.clone())
        };
        self.persist();
        Ok(updated)
    }

    pub fn update_progress(
        &self,
        job_id: &str,
        phase: Option<&str>,
        current: Option<i64>,
        total: Option<i64>,
        message: Option<&str>,
    ) -> Result<Value, ApiError> {
        let (updated, should_persist) = {
            let mut guard = self.inner.lock();
            let Some(Value::Object(obj)) = guard.operations.get_mut(job_id) else {
                return Err(ApiError::job_state_conflict(format!(
                    "Operation {job_id} is not active."
                )));
            };
            if let Some(p) = phase {
                obj.insert("phase".into(), Value::String(p.to_owned()));
            }
            if let Some(c) = current {
                obj.insert("current".into(), json!(c));
            }
            if let Some(t) = total {
                obj.insert("total".into(), json!(t));
            }
            if let Some(m) = message {
                obj.insert("message".into(), Value::String(m.to_owned()));
            }
            obj.insert("updated_at".into(), Value::String(now_iso()));
            refresh_capabilities(obj, false);
            let snap = Value::Object(obj.clone());
            let now = Instant::now();
            let persist_now = phase.is_some()
                || guard
                    .last_persist
                    .get(job_id)
                    .is_none_or(|t| now.duration_since(*t) >= MAX_PROGRESS_PERSIST_INTERVAL);
            if persist_now {
                guard.last_persist.insert(job_id.to_owned(), now);
            }
            (snap, persist_now)
        };
        if should_persist {
            self.persist();
            self.emit(
                SSE_EVENT_PROGRESS,
                &json!({
                    "job_id": updated["job_id"],
                    "state": updated["state"],
                    "phase": updated["phase"],
                    "current": updated["current"],
                    "total": updated["total"],
                    "message": updated["message"],
                }),
            );
        }
        Ok(updated)
    }

    pub fn request_cancel(&self, job_id: &str) -> Result<Value, ApiError> {
        let updated = {
            let mut guard = self.inner.lock();
            let Some(Value::Object(obj)) = guard.operations.get_mut(job_id) else {
                return Err(ApiError::job_not_cancellable(format!(
                    "Operation {job_id} cannot be cancelled."
                )));
            };
            let cur = obj.get("state").and_then(Value::as_str).unwrap_or("");
            if !MACHINES["operation"].allows("cancel", Some(cur), Some("cancelling")) {
                return Err(ApiError::job_not_cancellable(format!(
                    "Operation {job_id} cannot be cancelled."
                )));
            }
            obj.insert("state".into(), Value::String("cancelling".to_owned()));
            obj.insert("updated_at".into(), Value::String(now_iso()));
            refresh_capabilities(obj, false);
            Value::Object(obj.clone())
        };
        self.persist();
        self.emit(
            SSE_EVENT_CANCELLING,
            &json!({
                "job_id": updated["job_id"],
                "state": updated["state"],
                "message": updated["message"],
            }),
        );
        Ok(updated)
    }

    pub fn finish(
        &self,
        job_id: &str,
        terminal_state: &str,
        result: Option<Value>,
        error: Option<Value>,
        message: Option<&str>,
    ) -> Result<Value, ApiError> {
        let updated = {
            let mut guard = self.inner.lock();
            let Some(Value::Object(obj)) = guard.operations.get_mut(job_id) else {
                return Err(ApiError::job_state_conflict(format!(
                    "Unknown operation {job_id}"
                )));
            };
            let now = now_iso();
            obj.insert("state".into(), Value::String(terminal_state.to_owned()));
            obj.insert("updated_at".into(), Value::String(now.clone()));
            obj.insert("finished_at".into(), Value::String(now));
            if let Some(r) = result {
                obj.insert("result".into(), r);
            }
            if let Some(e) = error {
                obj.insert("error".into(), e);
            }
            if let Some(m) = message {
                obj.insert("message".into(), Value::String(m.to_owned()));
            }
            refresh_capabilities(obj, false);
            Value::Object(obj.clone())
        };
        self.persist();
        let ev = if terminal_state == "interrupted" {
            SSE_EVENT_INTERRUPTED
        } else {
            SSE_EVENT_FINISHED
        };
        self.emit(
            ev,
            &json!({
                "job_id": updated["job_id"],
                "state": updated["state"],
                "result": updated["result"],
                "error": updated["error"],
                "message": updated["message"],
            }),
        );
        Ok(updated)
    }

    pub fn get_items(&self, job_id: &str, offset: usize, limit: usize) -> Value {
        let lim = limit.clamp(1, MAX_ITEMS_LIST_LIMIT);
        let Ok(clean) = openbox_platform::safe_identifier(job_id, "job_id") else {
            return json!({ "job_id": job_id, "items": [], "total": 0, "offset": offset, "limit": lim });
        };
        let path = self
            .data_dir
            .join("operations_items")
            .join(format!("{clean}.json"));
        let items: Vec<Value> = fs::read_to_string(&path)
            .ok()
            .and_then(|s| serde_json::from_str::<Value>(&s).ok())
            .and_then(|v| v.as_array().cloned())
            .unwrap_or_default();
        let total = items.len();
        let start = offset.min(total);
        let end = (start + lim).min(total);
        json!({
            "job_id": job_id,
            "items": items[start..end],
            "total": total,
            "offset": offset,
            "limit": lim,
        })
    }
}
