//! Central `AppState` coordinating storage, caches, SSE, background jobs,
//! process registry, webhooks, media probe, and graceful shutdown.
//! Mirrors `webapp_state.py` + `openbox.py`.

use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicU16, Ordering};

use openbox_platform::resolved_data_dir;
use openbox_store::{JsonStateStore, OperationService, PRIMARY_NAME, SqliteReadModel, StateError};
use parking_lot::Mutex;
use rand::RngCore;
use serde_json::Value;
use tokio::sync::Notify;

fn generate_token_hex() -> String {
    let mut buf = [0u8; 32];
    rand::thread_rng().fill_bytes(&mut buf);
    hex::encode(buf)
}

use crate::cache::CacheCoordinator;
use crate::job_manager::JobManager;
use crate::media_probe::MediaProbeService;
use crate::notifications::{NotificationSpec, add_notification};
use crate::registry::ProcessRegistry;
use crate::sse::SseHub;
use crate::webhooks::{WebhookDispatcher, build_event};

pub type PostCommitHook = Arc<dyn Fn(&Value, &mut Value) + Send + Sync>;

pub struct AppState {
    pub data_dir: PathBuf,
    pub root_dir: PathBuf,
    pub token: String,
    pub store: Arc<JsonStateStore>,
    pub read_model: Arc<SqliteReadModel>,
    pub operations: Arc<OperationService>,
    pub jobs: Arc<JobManager>,
    pub sse: Arc<SseHub>,
    pub registry: Arc<ProcessRegistry>,
    pub cache: Arc<CacheCoordinator>,
    pub media_probe: Arc<MediaProbeService>,
    pub webhooks: Mutex<Option<WebhookDispatcher>>,
    pub startup_errors: Mutex<Vec<String>>,
    pub bound_port: AtomicU16,
    pub shutdown_notify: Arc<Notify>,
    sync_hook: Mutex<Option<PostCommitHook>>,
}

impl AppState {
    pub fn new(data_dir: Option<PathBuf>, root_dir: PathBuf, token: Option<String>) -> Arc<Self> {
        let data_dir = data_dir.unwrap_or_else(resolved_data_dir);
        let _ = std::fs::create_dir_all(&data_dir);
        let token = token.unwrap_or_else(generate_token_hex);
        let store = Arc::new(JsonStateStore::new(data_dir.join(PRIMARY_NAME)));
        let read_model = Arc::new(SqliteReadModel::new(data_dir.join("sqlite_readmodel.db")));
        let operations = Arc::new(OperationService::new(&data_dir));
        let sse = Arc::new(SseHub::new());
        {
            let sse_obs = Arc::clone(&sse);
            operations.set_observer(Arc::new(move |event_name, payload| {
                sse_obs.broadcast_event(event_name, payload);
            }));
        }
        let jobs = JobManager::new(Arc::clone(&operations));
        let registry = Arc::new(ProcessRegistry::new());
        let cache = Arc::new(CacheCoordinator::new());
        let media_probe = Arc::new(MediaProbeService::new(&data_dir));

        Arc::new(Self {
            data_dir,
            root_dir,
            token,
            store,
            read_model,
            operations,
            jobs,
            sse,
            registry,
            cache,
            media_probe,
            webhooks: Mutex::new(None),
            startup_errors: Mutex::new(Vec::new()),
            bound_port: AtomicU16::new(0),
            shutdown_notify: Arc::new(Notify::new()),
            sync_hook: Mutex::new(None),
        })
    }

    pub fn set_sync_hook(&self, hook: PostCommitHook) {
        *self.sync_hook.lock() = Some(hook);
    }

    pub fn load_state(&self) -> Result<Value, StateError> {
        self.store.load()
    }

    pub fn load_state_view(&self) -> Result<Arc<Value>, StateError> {
        self.store.load_readonly()
    }

    pub fn invalidate_caches(&self) {
        self.cache.invalidate_all();
        self.media_probe.clear_caches();
    }

    pub fn bump_media_epoch(&self) {
        self.cache.bump_media_epoch();
        self.media_probe.clear_caches();
    }

    pub fn transact_state<T, F>(&self, mutator: F) -> Result<(Arc<Value>, T), StateError>
    where
        F: FnOnce(&mut Value) -> Result<T, StateError>,
    {
        let hook = self.sync_hook.lock().clone();
        let mut out_slot: Option<T> = None;
        let state = self.store.update(|current| {
            let before = if hook.is_some() {
                Some(current.clone())
            } else {
                None
            };
            let out = mutator(current)?;
            if let (Some(h), Some(prev)) = (&hook, &before) {
                h(prev, current);
            }
            out_slot = Some(out);
            Ok(())
        })?;
        self.invalidate_caches();
        if self.read_model.enabled() {
            let sig = self.store.signature();
            let rm = Arc::clone(&self.read_model);
            let st_bg = Arc::clone(&state);
            std::thread::spawn(move || {
                let _ = rm.rebuild(&st_bg, sig);
            });
        }
        Ok((
            state,
            out_slot.expect("mutator succeeded and populated output"),
        ))
    }

    pub fn emit_notification(&self, spec: NotificationSpec<'_>) {
        let _ = self.transact_state(|state| {
            add_notification(state, spec);
            Ok(())
        });
    }

    pub fn publish_webhook_event(self: &Arc<Self>, event: &str, data: &Value) -> String {
        let Ok(envelope) = build_event(event, data, None, None) else {
            return String::new();
        };
        let event_id = envelope
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        let configs = self
            .load_state_view()
            .ok()
            .and_then(|s| s.get("settings")?.get("webhooks")?.as_array().cloned())
            .unwrap_or_default();
        if configs.is_empty() {
            return event_id;
        }
        let mut guard = self.webhooks.lock();
        let dispatcher = guard.get_or_insert_with(|| {
            let app = Arc::clone(self);
            WebhookDispatcher::spawn(Arc::new(
                move |webhook_id, _ev_id, status, error, sent_at, terminal| {
                    let _ = app.transact_state(|st| {
                        if let Some(hooks) = st
                            .get_mut("settings")
                            .and_then(|s| s.get_mut("webhooks"))
                            .and_then(Value::as_array_mut)
                        {
                            for hook in hooks {
                                if hook.get("id").and_then(Value::as_str)
                                    == Some(webhook_id.as_str())
                                    && let Some(obj) = hook.as_object_mut()
                                {
                                    obj.insert(
                                        "last_status".into(),
                                        status.map(Value::from).unwrap_or(Value::Null),
                                    );
                                    obj.insert("last_error".into(), Value::String(error.clone()));
                                    if !sent_at.is_empty() {
                                        obj.insert(
                                            "last_sent_at".into(),
                                            Value::String(sent_at.clone()),
                                        );
                                    }
                                    if terminal {
                                        obj.insert(
                                            "last_delivery_at".into(),
                                            Value::String(if sent_at.is_empty() {
                                                jiff::Timestamp::now().to_string()
                                            } else {
                                                sent_at.clone()
                                            }),
                                        );
                                    }
                                    break;
                                }
                            }
                        }
                        Ok(())
                    });
                },
            ))
        });
        dispatcher.enqueue(&configs, &envelope);
        event_id
    }

    pub fn request_shutdown(&self) {
        self.jobs.shutdown();
        self.shutdown_notify.notify_waiters();
    }

    pub fn port(&self) -> u16 {
        self.bound_port.load(Ordering::Acquire)
    }

    pub fn set_port(&self, port: u16) {
        self.bound_port.store(port, Ordering::Release);
    }
}
