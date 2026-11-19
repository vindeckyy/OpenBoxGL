//! Bounded, observable background work manager backed by `OperationService`.
//! Mirrors `job_manager.py`.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Instant;

use openbox_core::MACHINES;
use openbox_store::OperationService;
use parking_lot::Mutex;
use serde_json::{Map, Value, json};
use tokio::sync::Semaphore;

pub const MAX_ERROR_LENGTH: usize = 800;
pub const DEFAULT_MAX_WORKERS: usize = 4;
pub const DEFAULT_MAX_JOBS: usize = 128;
pub const MAX_JOB_NAME_LENGTH: usize = 128;
pub const MAX_JOB_RESULT_BYTES: usize = 64 * 1024;
pub const HISTORY_LIMIT: usize = 50;

pub fn operation_type_for_name(name: &str) -> &'static str {
    match name {
        "emulator-install-all" => "emulator.install",
        "emulator-update-all" => "emulator.update",
        "media-bulk" => "media.bulk_download",
        "memories-import" => "media.memories_import",
        "metadata" => "metadata.db_sync",
        "metadata-match" => "metadata.apply",
        "library-backup" => "library.backup",
        "library-restore" => "library.restore",
        "library-export" => "library.export",
        "screenscraper-match" => "screenscraper.match",
        "screenscraper-apply" => "screenscraper.apply",
        "steamgrid-match" => "steamgrid.match",
        "steamgrid-apply" => "steamgrid.apply",
        "auto-import" => "storefront.auto_import",
        "storefront-scan" => "storefront.scan",
        "cloud-sync" => "cloud.sync",
        "updater-install" => "updater.install",
        "saves-scan" => "saves.scan",
        "saves-backup" | "save-vault-auto" => "saves.backup",
        "saves-restore" => "saves.restore",
        "trail-refresh" => "metadata.apply",
        "trail-rebuild" | "dna-index-rebuild" => "library.dna_rebuild",
        "deals-refresh" => "storefront.scan",
        "itinerary-plan" => "setup.scan",
        other => {
            if other.starts_with("emulator-install:") {
                "emulator.install"
            } else if other.starts_with("update:") {
                "emulator.update"
            } else if other.starts_with("gameyfin:") {
                "gameyfin.install"
            } else if other.starts_with("saves-backup:")
                || other.starts_with("save-vault-snapshot:")
            {
                "saves.backup"
            } else if other.starts_with("saves-restore:")
                || other.starts_with("save-vault-restore:")
            {
                "saves.restore"
            } else if other.starts_with("setup-scan:") {
                "setup.scan"
            } else if other.starts_with("setup-revalidate:") {
                "setup.revalidate"
            } else if other.starts_with("setup-commit:") {
                "setup.commit"
            } else if other.starts_with("reel:") {
                "clips.reel"
            } else if other.starts_with("metadata-match-preview:") {
                "metadata.match_preview"
            } else if other.starts_with("metadata-match-apply:") {
                "metadata.apply"
            } else {
                "setup.scan"
            }
        }
    }
}

pub fn operation_title_for_name(name: &str) -> String {
    match name {
        "emulator-install-all" => "Install all emulators".to_owned(),
        "emulator-update-all" => "Update all emulators".to_owned(),
        "media-bulk" => "Bulk media download".to_owned(),
        "memories-import" => "Memory roots import".to_owned(),
        "dna-index-rebuild" => "Rebuild Game DNA index".to_owned(),
        "metadata" => "Metadata database sync".to_owned(),
        "metadata-match" => "Metadata auto-match".to_owned(),
        "library-backup" => "Library backup".to_owned(),
        "library-restore" => "Library restore".to_owned(),
        "cloud-sync" => "Cloud sync".to_owned(),
        "updater-install" => "Install update".to_owned(),
        "saves-scan" => "Save path scan".to_owned(),
        "saves-backup" => "Save backup".to_owned(),
        "saves-restore" => "Save restore".to_owned(),
        "storefront-scan" => "Storefront scan".to_owned(),
        "save-vault-auto" => "Scheduled Save Vault snapshot".to_owned(),
        "trail-refresh" => "Refresh RetroAchievements Trail".to_owned(),
        "trail-rebuild" => "Rebuild Achievement Trail cache".to_owned(),
        "deals-refresh" => "Refresh Deal Radar prices".to_owned(),
        "itinerary-plan" => "Build Backlog Itinerary".to_owned(),
        other => {
            if let Some(rest) = other.strip_prefix("emulator-install:") {
                format!("Install emulator {rest}")
            } else if let Some(rest) = other.strip_prefix("update:") {
                format!("Update emulator {rest}")
            } else if let Some(rest) = other.strip_prefix("gameyfin:") {
                format!("Install Gameyfin game {rest}")
            } else if other.starts_with("saves-backup:") {
                "Save backup".to_owned()
            } else if other.starts_with("saves-restore:") {
                "Save restore".to_owned()
            } else if other.starts_with("save-vault-snapshot:") {
                "Save Vault snapshot".to_owned()
            } else if other.starts_with("save-vault-restore:") {
                "Save Vault restore".to_owned()
            } else {
                let base = other.split(':').next().unwrap_or("").replace('-', " ");
                let trimmed = base.trim();
                if trimmed.is_empty() {
                    "Background job".to_owned()
                } else {
                    trimmed
                        .split_whitespace()
                        .map(|w| {
                            let mut c = w.chars();
                            match c.next() {
                                Some(first) => {
                                    format!("{}{}", first.to_uppercase(), c.as_str())
                                }
                                None => String::new(),
                            }
                        })
                        .collect::<Vec<_>>()
                        .join(" ")
                }
            }
        }
    }
}

fn now_iso() -> String {
    jiff::Timestamp::now().to_string()
}

fn bounded_result(result: Value) -> Map<String, Value> {
    let Some(obj) = result.as_object() else {
        return Map::new();
    };
    let size = serde_json::to_vec(obj).map(|b| b.len()).unwrap_or(0);
    if size > MAX_JOB_RESULT_BYTES {
        let mut out = Map::new();
        out.insert(
            "error".into(),
            Value::String("Job result exceeded the size limit.".to_owned()),
        );
        out.insert("result_truncated".into(), Value::Bool(true));
        return out;
    }
    obj.clone()
}

pub fn legacy_snapshot_fields(job: &Value, operation: Option<&Value>) -> Value {
    let mut snap = job.as_object().cloned().unwrap_or_default();
    if let Some(op) = operation.and_then(Value::as_object) {
        for key in [
            "type",
            "phase",
            "current",
            "total",
            "message",
            "can_cancel",
            "can_retry",
            "can_resume",
            "result",
        ] {
            if let Some(val) = op.get(key)
                && !val.is_null()
            {
                snap.insert(key.into(), val.clone());
            }
        }
        for key in ["job_id", "started_at", "finished_at"] {
            if let Some(val) = op.get(key)
                && val.as_str().is_some_and(|s| !s.is_empty())
            {
                snap.insert(key.into(), val.clone());
            }
        }
        let job_state = snap.get("state").and_then(Value::as_str).unwrap_or("");
        let is_terminal = MACHINES["legacy_job"].terminal.contains(job_state);
        if let Some(op_state) = op.get("state").and_then(Value::as_str)
            && !(is_terminal && op_state == "cancelling")
        {
            snap.insert("state".into(), Value::String(op_state.to_owned()));
        }
        if let Some(err) = op.get("error")
            && !err.is_null()
        {
            snap.insert("error".into(), err.clone());
        }
    }
    Value::Object(snap)
}

#[derive(Clone)]
pub struct JobContext {
    pub job_name: String,
    pub job_id: String,
    cancelled: Arc<AtomicBool>,
    manager: Arc<JobManager>,
}

impl JobContext {
    pub fn is_cancelled(&self) -> bool {
        self.cancelled.load(Ordering::Acquire)
    }

    pub fn progress(&self, patch: Value) -> Value {
        self.manager.update_progress(&self.job_name, patch)
    }
}

struct Inner {
    jobs: HashMap<String, Map<String, Value>>,
    names_by_id: HashMap<String, String>,
    cancel_flags: HashMap<String, Arc<AtomicBool>>,
    inflight: HashSet<String>,
    history: Vec<Value>,
}

pub struct JobManager {
    operations: Arc<OperationService>,
    semaphore: Arc<Semaphore>,
    inner: Mutex<Inner>,
}

impl JobManager {
    pub fn new(operations: Arc<OperationService>) -> Arc<Self> {
        Arc::new(Self {
            operations,
            semaphore: Arc::new(Semaphore::new(DEFAULT_MAX_WORKERS)),
            inner: Mutex::new(Inner {
                jobs: HashMap::new(),
                names_by_id: HashMap::new(),
                cancel_flags: HashMap::new(),
                inflight: HashSet::new(),
                history: Vec::new(),
            }),
        })
    }

    pub fn get_operation_id(&self, name: &str) -> Option<String> {
        self.inner
            .lock()
            .jobs
            .get(name)
            .and_then(|j| j.get("job_id"))
            .and_then(Value::as_str)
            .map(str::to_owned)
    }

    pub fn name_for_operation_id(&self, job_id: &str) -> Option<String> {
        self.inner.lock().names_by_id.get(job_id).cloned()
    }

    pub fn cancel_by_id(&self, job_id: &str) -> bool {
        match self.name_for_operation_id(job_id) {
            Some(name) => self.cancel(&name),
            None => false,
        }
    }

    pub fn snapshot(&self, name: &str) -> Value {
        let job = {
            let guard = self.inner.lock();
            guard
                .jobs
                .get(name)
                .cloned()
                .map(Value::Object)
                .unwrap_or_else(|| json!({}))
        };
        if let Some(jid) = job.get("job_id").and_then(Value::as_str) {
            let op = self.operations.get(jid);
            return legacy_snapshot_fields(&job, op.as_ref());
        }
        job
    }

    pub fn snapshots(&self) -> Map<String, Value> {
        let names: Vec<String> = self.inner.lock().jobs.keys().cloned().collect();
        let mut out = Map::new();
        for name in names {
            out.insert(name.clone(), self.snapshot(&name));
        }
        out
    }

    pub fn history(&self, limit: usize) -> Vec<Value> {
        let clamped = limit.clamp(1, 200);
        let items: Vec<Value> = {
            let guard = self.inner.lock();
            guard.history.iter().rev().take(clamped).cloned().collect()
        };
        items
            .into_iter()
            .map(|job| {
                let op = job
                    .get("job_id")
                    .and_then(Value::as_str)
                    .and_then(|jid| self.operations.get(jid));
                legacy_snapshot_fields(&job, op.as_ref())
            })
            .collect()
    }

    pub fn update_progress(&self, name: &str, patch: Value) -> Value {
        let Some(patch_obj) = patch.as_object() else {
            return json!({});
        };
        let (job_id, snapshot) = {
            let mut guard = self.inner.lock();
            let current = if guard.jobs.contains_key(name) {
                guard.jobs.get_mut(name)
            } else {
                guard
                    .jobs
                    .values_mut()
                    .find(|j| j.get("job_id").and_then(Value::as_str) == Some(name))
            };
            let Some(cur) = current else {
                return json!({});
            };
            let st = cur.get("state").and_then(Value::as_str).unwrap_or("");
            if st != "queued" && st != "running" {
                return json!({});
            }
            for (k, v) in patch_obj {
                cur.insert(k.clone(), v.clone());
            }
            let jid = cur
                .get("job_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            (jid, Value::Object(cur.clone()))
        };
        if !job_id.is_empty() {
            let phase = patch_obj.get("phase").and_then(Value::as_str);
            let current = patch_obj.get("current").and_then(Value::as_i64);
            let total = patch_obj.get("total").and_then(Value::as_i64);
            let message = patch_obj.get("message").and_then(Value::as_str);
            let _ = self
                .operations
                .update_progress(&job_id, phase, current, total, message);
        }
        snapshot
    }

    pub fn cancel(&self, name: &str) -> bool {
        let (cancelled_job, job_id_to_cancel) = {
            let mut guard = self.inner.lock();
            let (st, job_id) = match guard.jobs.get(name) {
                Some(job) => (
                    job.get("state")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned(),
                    job.get("job_id")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned(),
                ),
                None => return false,
            };
            if st != "queued" && st != "running" {
                return false;
            }
            if let Some(flag) = guard.cancel_flags.get(&job_id) {
                flag.store(true, Ordering::Release);
            }
            if st == "queued" {
                let finished = if let Some(job) = guard.jobs.get_mut(name) {
                    job.insert("state".into(), Value::String("cancelled".to_owned()));
                    job.insert("finished_at".into(), Value::String(now_iso()));
                    Value::Object(job.clone())
                } else {
                    return false;
                };
                guard.names_by_id.remove(&job_id);
                guard.cancel_flags.remove(&job_id);
                guard.inflight.remove(&job_id);
                guard.history.push(finished.clone());
                if guard.history.len() > HISTORY_LIMIT {
                    let drop_n = guard.history.len() - HISTORY_LIMIT;
                    guard.history.drain(..drop_n);
                }
                (Some(finished), None)
            } else {
                (None, Some(job_id))
            }
        };
        if let Some(job) = cancelled_job {
            self.sync_finish(&job);
        } else if let Some(jid) = job_id_to_cancel {
            let _ = self.operations.request_cancel(&jid);
        }
        true
    }

    pub fn submit<F>(
        self: &Arc<Self>,
        name: &str,
        replace: bool,
        operation_type: Option<&str>,
        input_data: Option<&Value>,
        worker: F,
    ) -> Result<Value, String>
    where
        F: FnOnce(JobContext) -> Result<Value, String> + Send + 'static,
    {
        let clean_name = name.trim();
        if clean_name.is_empty() {
            return Err("A job name is required.".to_owned());
        }
        if clean_name.len() > MAX_JOB_NAME_LENGTH {
            return Err("Job name is too long.".to_owned());
        }
        if clean_name.bytes().any(|b| b < 0x20) {
            return Err("Job name contains control characters.".to_owned());
        }

        let (superseded, job_id, job_val, cancel_flag) = {
            let mut guard = self.inner.lock();
            let mut superseded = None;
            if let Some(existing) = guard.jobs.get(clean_name).cloned() {
                let st = existing.get("state").and_then(Value::as_str).unwrap_or("");
                if (st == "queued" || st == "running") && !replace {
                    return Ok(Value::Object(existing));
                }
                if replace && let Some(old_id) = existing.get("job_id").and_then(Value::as_str) {
                    if let Some(flag) = guard.cancel_flags.get(old_id) {
                        flag.store(true, Ordering::Release);
                    }
                    if st == "queued" || st == "running" {
                        let mut sup = existing.clone();
                        sup.insert("state".into(), Value::String("cancelled".to_owned()));
                        sup.insert("finished_at".into(), Value::String(now_iso()));
                        guard.names_by_id.remove(old_id);
                        let sup_val = Value::Object(sup);
                        guard.history.push(sup_val.clone());
                        superseded = Some(sup_val);
                    }
                }
            }
            guard.jobs.remove(clean_name);
            if guard.inflight.len() >= DEFAULT_MAX_JOBS {
                return Err("The background job queue is full.".to_owned());
            }
            let op_type = operation_type.unwrap_or_else(|| operation_type_for_name(clean_name));
            let title = operation_title_for_name(clean_name);
            let op = self.operations.create(op_type, &title, input_data);
            let job_id = op["job_id"].as_str().unwrap_or("").to_owned();
            let mut job = Map::new();
            job.insert("job_id".into(), Value::String(job_id.clone()));
            job.insert("name".into(), Value::String(clean_name.to_owned()));
            job.insert("state".into(), Value::String("queued".to_owned()));
            job.insert("type".into(), Value::String(op_type.to_owned()));
            job.insert("started_at".into(), Value::String(String::new()));
            job.insert("finished_at".into(), Value::String(String::new()));
            job.insert("created_at".into(), Value::String(now_iso()));
            job.insert("error".into(), Value::String(String::new()));
            job.insert("attempt".into(), Value::from(0));
            job.insert("max_attempts".into(), Value::from(1));
            job.insert("duration_seconds".into(), Value::from(0));
            job.insert(
                "can_cancel".into(),
                op.get("can_cancel").cloned().unwrap_or(Value::Bool(true)),
            );
            job.insert(
                "can_retry".into(),
                op.get("can_retry").cloned().unwrap_or(Value::Bool(false)),
            );
            job.insert(
                "can_resume".into(),
                op.get("can_resume").cloned().unwrap_or(Value::Bool(false)),
            );
            let flag = Arc::new(AtomicBool::new(false));
            guard.jobs.insert(clean_name.to_owned(), job.clone());
            guard
                .names_by_id
                .insert(job_id.clone(), clean_name.to_owned());
            guard.cancel_flags.insert(job_id.clone(), Arc::clone(&flag));
            guard.inflight.insert(job_id.clone());
            (superseded, job_id, Value::Object(job), flag)
        };

        if let Some(sup) = superseded {
            self.sync_finish(&sup);
        }

        let mgr = Arc::clone(self);
        let jname = clean_name.to_owned();
        let jid = job_id.clone();
        let sem = Arc::clone(&self.semaphore);
        let ctx = JobContext {
            job_name: jname.clone(),
            job_id: jid.clone(),
            cancelled: Arc::clone(&cancel_flag),
            manager: Arc::clone(self),
        };

        if let Ok(handle) = tokio::runtime::Handle::try_current() {
            handle.spawn(async move {
                let _permit = sem.acquire_owned().await;
                let started = Instant::now();
                let should_run = {
                    let mut guard = mgr.inner.lock();
                    let Some(cur) = guard.jobs.get_mut(&jname) else {
                        return;
                    };
                    if cur.get("job_id").and_then(Value::as_str) != Some(jid.as_str()) {
                        return;
                    }
                    if cancel_flag.load(Ordering::Acquire) {
                        cur.insert("state".into(), Value::String("cancelled".to_owned()));
                        cur.insert("finished_at".into(), Value::String(now_iso()));
                        let finished = Value::Object(cur.clone());
                        guard.names_by_id.remove(&jid);
                        guard.cancel_flags.remove(&jid);
                        guard.inflight.remove(&jid);
                        guard.history.push(finished.clone());
                        drop(guard);
                        mgr.sync_finish(&finished);
                        false
                    } else {
                        cur.insert("state".into(), Value::String("running".to_owned()));
                        cur.insert("started_at".into(), Value::String(now_iso()));
                        cur.insert("attempt".into(), Value::from(1));
                        true
                    }
                };
                if !should_run {
                    return;
                }
                let _ = mgr.operations.mark_running(&jid, "");
                let run_res = tokio::task::spawn_blocking(move || worker(ctx)).await;
                let elapsed = (started.elapsed().as_secs_f64() * 1000.0).round() / 1000.0;
                let finished_val = {
                    let mut guard = mgr.inner.lock();
                    let out = if let Some(cur) = guard.jobs.get_mut(&jname)
                        && cur.get("job_id").and_then(Value::as_str) == Some(jid.as_str())
                    {
                        match run_res {
                            Ok(Ok(val)) => {
                                if cancel_flag.load(Ordering::Acquire) {
                                    cur.insert(
                                        "state".into(),
                                        Value::String("cancelled".to_owned()),
                                    );
                                } else {
                                    for (k, v) in bounded_result(val) {
                                        cur.insert(k, v);
                                    }
                                    cur.insert("state".into(), Value::String("done".to_owned()));
                                }
                            }
                            Ok(Err(err)) => {
                                cur.insert(
                                    "state".into(),
                                    Value::String(
                                        if cancel_flag.load(Ordering::Acquire) {
                                            "cancelled"
                                        } else {
                                            "error"
                                        }
                                        .to_owned(),
                                    ),
                                );
                                let truncated: String =
                                    err.chars().take(MAX_ERROR_LENGTH).collect();
                                cur.insert("error".into(), Value::String(truncated));
                            }
                            Err(join_err) => {
                                cur.insert("state".into(), Value::String("error".to_owned()));
                                cur.insert("error".into(), Value::String(join_err.to_string()));
                            }
                        }
                        cur.insert("finished_at".into(), Value::String(now_iso()));
                        cur.insert("duration_seconds".into(), Value::from(elapsed));
                        let fin = Value::Object(cur.clone());
                        guard.history.push(fin.clone());
                        if guard.history.len() > HISTORY_LIMIT {
                            let drop_n = guard.history.len() - HISTORY_LIMIT;
                            guard.history.drain(..drop_n);
                        }
                        Some(fin)
                    } else {
                        None
                    };
                    guard.inflight.remove(&jid);
                    guard.cancel_flags.remove(&jid);
                    guard.names_by_id.remove(&jid);
                    out
                };
                if let Some(fin) = finished_val {
                    mgr.sync_finish(&fin);
                }
            });
        }

        Ok(job_val)
    }

    fn sync_finish(&self, job: &Value) {
        let Some(jid) = job.get("job_id").and_then(Value::as_str) else {
            return;
        };
        let state = job.get("state").and_then(Value::as_str).unwrap_or("done");
        let op_state = if state == "done"
            && job
                .get("errors")
                .and_then(Value::as_array)
                .is_some_and(|a| !a.is_empty())
        {
            "partial"
        } else {
            state
        };
        let mut res_map = Map::new();
        for key in [
            "added",
            "merged",
            "skipped",
            "excluded",
            "failed",
            "completed",
            "downloaded",
            "updated",
        ] {
            if let Some(n) = job.get(key).and_then(Value::as_i64) {
                res_map.insert(key.into(), Value::from(n));
            }
        }
        let result = if res_map.is_empty() {
            None
        } else {
            Some(Value::Object(res_map))
        };
        let error = if op_state == "error" {
            let raw = job.get("error").cloned().unwrap_or(Value::Null);
            Some(if raw.is_object() {
                raw
            } else {
                json!({"code": "INTERNAL_ERROR", "message": raw.as_str().unwrap_or(""), "request_id": ""})
            })
        } else {
            None
        };
        let message = job.get("message").and_then(Value::as_str);
        let _ = self
            .operations
            .finish(jid, op_state, result, error, message);
    }

    pub fn shutdown(&self) {
        let flags: Vec<Arc<AtomicBool>> =
            self.inner.lock().cancel_flags.values().cloned().collect();
        for flag in flags {
            flag.store(true, Ordering::Release);
        }
    }
}
