//! Coordinated cache epochs, LRU `FacetCache` with monotonic budget, and gzip payload cache.
//! Mirrors `pkg/state/cache.py` (ADR-0005).

use std::collections::{HashMap, VecDeque};
use std::io::Write;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;

use bytes::Bytes;
use flate2::Compression;
use flate2::write::GzEncoder;
use openbox_store::FileSignature;
use parking_lot::Mutex;
use serde_json::{Value, json};

pub const GZIP_THRESHOLD: usize = 1024;
pub const FACET_CACHE_MAX: usize = 64;
pub const FACET_BUDGET_MS: f64 = 50.0;
pub const FACET_DEGRADED: &str = "DEGRADED";

pub fn available_locales() -> Value {
    json!([
        {"code": "en", "name": "English", "native": "English"},
        {"code": "es", "name": "Spanish", "native": "Español"},
        {"code": "de", "name": "German", "native": "Deutsch"},
        {"code": "fr", "name": "French", "native": "Français"},
        {"code": "pt", "name": "Portuguese", "native": "Português"},
    ])
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct FacetKey {
    field: String,
    limit: usize,
    len: usize,
    first_id: String,
    last_id: String,
    epoch: u64,
}

struct FacetInner {
    order: VecDeque<FacetKey>,
    map: HashMap<FacetKey, Value>,
    epoch: u64,
}

pub struct FacetCache {
    max_size: usize,
    budget_ms: f64,
    inner: Mutex<FacetInner>,
}

impl Default for FacetCache {
    fn default() -> Self {
        Self::new(FACET_CACHE_MAX, FACET_BUDGET_MS)
    }
}

impl FacetCache {
    pub fn new(max_size: usize, budget_ms: f64) -> Self {
        Self {
            max_size,
            budget_ms,
            inner: Mutex::new(FacetInner {
                order: VecDeque::new(),
                map: HashMap::new(),
                epoch: 0,
            }),
        }
    }

    pub fn clear(&self) {
        let mut guard = self.inner.lock();
        guard.order.clear();
        guard.map.clear();
        guard.epoch += 1;
    }

    pub fn epoch(&self) -> u64 {
        self.inner.lock().epoch
    }

    pub fn compute_facets(
        &self,
        games: &[Value],
        field: &str,
        limit: usize,
        budget_ms: Option<f64>,
    ) -> Value {
        let epoch = self.epoch();
        if !matches!(
            field,
            "genre" | "developer" | "publisher" | "platform" | "progress" | "esrb"
        ) {
            return json!({"facets": [], "degraded": false, "code": "OK", "epoch": epoch});
        }
        let first_id = games
            .first()
            .and_then(|g| g.get("game_id").or_else(|| g.get("id")))
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        let last_id = games
            .last()
            .and_then(|g| g.get("game_id").or_else(|| g.get("id")))
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        let key = FacetKey {
            field: field.to_owned(),
            limit,
            len: games.len(),
            first_id,
            last_id,
            epoch,
        };
        {
            let mut guard = self.inner.lock();
            if let Some(hit) = guard.map.get(&key).cloned() {
                if let Some(pos) = guard.order.iter().position(|k| k == &key) {
                    guard.order.remove(pos);
                }
                guard.order.push_back(key);
                return hit;
            }
        }

        let effective = budget_ms.unwrap_or(self.budget_ms);
        let start = Instant::now();
        let mut counts: HashMap<String, usize> = HashMap::new();
        let mut degraded = false;

        for game in games {
            if start.elapsed().as_secs_f64() * 1000.0 > effective {
                degraded = true;
                break;
            }
            let Some(obj) = game.as_object() else {
                continue;
            };
            if obj.get("hidden").and_then(Value::as_bool).unwrap_or(false) {
                continue;
            }
            match field {
                "genre" => {
                    for part in obj
                        .get("genre")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .split(',')
                    {
                        let label = part.trim();
                        if !label.is_empty() {
                            *counts.entry(label.to_owned()).or_insert(0) += 1;
                        }
                    }
                }
                "developer" => {
                    let label = obj
                        .get("developer")
                        .and_then(Value::as_str)
                        .map(str::trim)
                        .unwrap_or("");
                    if !label.is_empty() {
                        *counts.entry(label.to_owned()).or_insert(0) += 1;
                    }
                }
                "publisher" => {
                    let label = obj
                        .get("publisher")
                        .and_then(Value::as_str)
                        .map(str::trim)
                        .unwrap_or("");
                    if !label.is_empty() {
                        *counts.entry(label.to_owned()).or_insert(0) += 1;
                    }
                }
                "platform" => {
                    let label = obj
                        .get("platform")
                        .and_then(Value::as_str)
                        .map(str::trim)
                        .filter(|s| !s.is_empty())
                        .unwrap_or("Unspecified");
                    *counts.entry(label.to_owned()).or_insert(0) += 1;
                }
                "progress" => {
                    let label = obj
                        .get("progress")
                        .and_then(Value::as_str)
                        .map(str::trim)
                        .filter(|s| !s.is_empty())
                        .unwrap_or("Unset");
                    *counts.entry(label.to_owned()).or_insert(0) += 1;
                }
                "esrb" => {
                    let label = obj
                        .get("esrb")
                        .and_then(Value::as_str)
                        .map(str::trim)
                        .filter(|s| !s.is_empty())
                        .unwrap_or("Unrated");
                    *counts.entry(label.to_owned()).or_insert(0) += 1;
                }
                _ => {}
            }
        }
        if !degraded && start.elapsed().as_secs_f64() * 1000.0 > effective {
            degraded = true;
        }
        let mut items: Vec<(String, usize)> = counts.into_iter().collect();
        items.sort_by(|a, b| {
            b.1.cmp(&a.1)
                .then_with(|| a.0.to_lowercase().cmp(&b.0.to_lowercase()))
        });
        let facets: Vec<Value> = items
            .into_iter()
            .take(limit)
            .map(|(value, count)| json!({"value": value, "count": count}))
            .collect();
        let result = json!({
            "facets": facets,
            "degraded": degraded,
            "code": if degraded { FACET_DEGRADED } else { "OK" },
            "epoch": epoch,
        });

        let mut guard = self.inner.lock();
        if !guard.map.contains_key(&key) {
            guard.order.push_back(key.clone());
        }
        guard.map.insert(key, result.clone());
        while guard.order.len() > self.max_size {
            if let Some(old) = guard.order.pop_front() {
                guard.map.remove(&old);
            }
        }
        result
    }
}

#[derive(Debug)]
pub struct CachedPayload {
    pub signature: (Option<FileSignature>, u64, u64),
    pub raw_json: Bytes,
    pub raw_gzip: Option<Bytes>,
    pub etag: String,
}

pub struct CacheCoordinator {
    media_epoch: AtomicU64,
    plugin_epoch: AtomicU64,
    public_state_cache: Mutex<Option<Arc<CachedPayload>>>,
    public_settings_cache: Mutex<Option<(Option<FileSignature>, Value)>>,
    pub facets: FacetCache,
}

impl Default for CacheCoordinator {
    fn default() -> Self {
        Self::new()
    }
}

impl CacheCoordinator {
    pub fn new() -> Self {
        Self {
            media_epoch: AtomicU64::new(0),
            plugin_epoch: AtomicU64::new(0),
            public_state_cache: Mutex::new(None),
            public_settings_cache: Mutex::new(None),
            facets: FacetCache::default(),
        }
    }

    pub fn media_epoch(&self) -> u64 {
        self.media_epoch.load(Ordering::Acquire)
    }

    pub fn plugin_epoch(&self) -> u64 {
        self.plugin_epoch.load(Ordering::Acquire)
    }

    pub fn bump_media_epoch(&self) {
        self.media_epoch.fetch_add(1, Ordering::AcqRel);
        self.invalidate_all();
    }

    pub fn bump_plugin_epoch(&self) {
        self.plugin_epoch.fetch_add(1, Ordering::AcqRel);
        self.invalidate_all();
    }

    pub fn invalidate_all(&self) {
        *self.public_state_cache.lock() = None;
        *self.public_settings_cache.lock() = None;
        self.facets.clear();
    }

    pub fn get_cached_state(&self, sig: Option<FileSignature>) -> Option<Arc<CachedPayload>> {
        let expected = (sig, self.media_epoch(), self.plugin_epoch());
        let guard = self.public_state_cache.lock();
        guard
            .as_ref()
            .filter(|entry| entry.signature == expected)
            .cloned()
    }

    pub fn set_cached_state_bytes(
        &self,
        sig: Option<FileSignature>,
        raw_json_vec: Vec<u8>,
    ) -> Arc<CachedPayload> {
        let raw_gzip = if raw_json_vec.len() >= GZIP_THRESHOLD {
            let mut enc = GzEncoder::new(Vec::new(), Compression::fast());
            if enc.write_all(&raw_json_vec).is_ok() {
                enc.finish().ok().map(Bytes::from)
            } else {
                None
            }
        } else {
            None
        };
        let digest = blake2_short_hex(&raw_json_vec);
        let entry = Arc::new(CachedPayload {
            signature: (sig, self.media_epoch(), self.plugin_epoch()),
            raw_json: Bytes::from(raw_json_vec),
            raw_gzip,
            etag: format!("\"{digest}\""),
        });
        *self.public_state_cache.lock() = Some(Arc::clone(&entry));
        entry
    }

    pub fn set_cached_state(
        &self,
        sig: Option<FileSignature>,
        payload: Value,
    ) -> Arc<CachedPayload> {
        let raw_json = serde_json::to_vec(&payload).unwrap_or_else(|_| b"{}".to_vec());
        self.set_cached_state_bytes(sig, raw_json)
    }

    pub fn get_cached_settings(&self, sig: Option<FileSignature>) -> Option<Value> {
        let guard = self.public_settings_cache.lock();
        guard
            .as_ref()
            .filter(|(s, _)| *s == sig)
            .map(|(_, v)| v.clone())
    }

    pub fn set_cached_settings(&self, sig: Option<FileSignature>, payload: Value) {
        *self.public_settings_cache.lock() = Some((sig, payload));
    }
}

fn blake2_short_hex(bytes: &[u8]) -> String {
    use blake2::digest::consts::U8;
    use blake2::{Blake2b, Digest};
    let mut hasher = Blake2b::<U8>::new();
    hasher.update(bytes);
    hex::encode(hasher.finalize())
}
