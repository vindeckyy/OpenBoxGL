//! Optional SteamGridDB artwork provider (`pkg/parity/parity_steamgrid.py`).

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs;
use std::path::Path;
use std::sync::LazyLock;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant, SystemTime};

use openbox_core::APP_VERSION;
use openbox_io::{ensure_env_loaded, env_value};
use parking_lot::Mutex;
use serde_json::{Map, Value, json};
use sha1::{Digest as _, Sha1};

use crate::http::{http_get_raw, quote_path, urlencode};
use crate::screenscraper::clean_media_url as ss_clean_media_url;

pub const SGDB_ENDPOINT: &str = "https://www.steamgriddb.com/api/v2";
pub const SGDB_CACHE_TTL_SECS: u64 = 30 * 24 * 3600;
pub const PROVIDER: &str = "steamgrid";
pub const PROVIDER_ORDER: &[&str] = &["launchbox", "igdb", "screenscraper", PROVIDER];

const MIN_REQUEST_INTERVAL: Duration = Duration::from_secs(1);
const MAX_RETRIES: usize = 4;
const BACKOFF_BASE: f64 = 1.5;
const RETRY_AFTER_CAP: f64 = 60.0;

static REQUEST_LOCK: LazyLock<Mutex<Option<Instant>>> = LazyLock::new(|| Mutex::new(None));
static SESSION_DISABLED: AtomicBool = AtomicBool::new(false);

const GRID_COVER_DIMENSIONS: &str = "600x900,342x482,660x930";
const GRID_BANNER_DIMENSIONS: &str = "460x215,920x430";

type AssetEndpoint = (
    &'static str,
    &'static str,
    Option<(&'static str, &'static str)>,
);

const ASSET_ENDPOINTS: &[AssetEndpoint] = &[
    (
        "cover",
        "grids",
        Some(("dimensions", GRID_COVER_DIMENSIONS)),
    ),
    (
        "banner",
        "grids",
        Some(("dimensions", GRID_BANNER_DIMENSIONS)),
    ),
    ("background", "heroes", None),
    ("clear_logo", "logos", None),
    ("icon", "icons", None),
];

pub fn credentials() -> Result<String, String> {
    ensure_env_loaded(None);
    let key = env_value(&["STEAMGRIDDB_API_KEY", "OPENBOX_STEAMGRIDDB_API_KEY"]);
    if key.is_empty() {
        return Err("Set STEAMGRIDDB_API_KEY in ~/.env to use SteamGridDB.".to_owned());
    }
    Ok(key)
}

pub fn is_configured() -> bool {
    credentials().is_ok()
}

pub fn enabled(settings: &Value) -> bool {
    settings
        .get("steamgrid_enabled")
        .and_then(Value::as_bool)
        .unwrap_or(true)
}

pub fn session_disabled() -> bool {
    SESSION_DISABLED.load(Ordering::Acquire)
}

pub fn provider_available(settings: &Value) -> bool {
    enabled(settings) && is_configured() && !session_disabled()
}

pub fn provider_status(settings: &Value, cache_dir: Option<&Path>) -> Value {
    let mut obj = Map::new();
    obj.insert("provider".into(), Value::String(PROVIDER.to_owned()));
    obj.insert("enabled".into(), Value::Bool(enabled(settings)));
    obj.insert("configured".into(), Value::Bool(is_configured()));
    obj.insert("disabled".into(), Value::Bool(session_disabled()));
    if let Some(cd) = cache_dir {
        obj.insert("cache_entries".into(), json!(cache_size(cd)));
    }
    Value::Object(obj)
}

fn throttle() {
    let mut guard = REQUEST_LOCK.lock();
    if let Some(last) = *guard {
        let elapsed = last.elapsed();
        if elapsed < MIN_REQUEST_INTERVAL {
            std::thread::sleep(MIN_REQUEST_INTERVAL - elapsed);
        }
    }
    *guard = Some(Instant::now());
}

fn retry_delay(retry_after: &str, attempt: usize) -> Duration {
    if let Ok(secs) = retry_after.parse::<f64>()
        && secs > 0.0
    {
        return Duration::from_secs_f64(secs.min(RETRY_AFTER_CAP));
    }
    Duration::from_secs_f64(BACKOFF_BASE * (2f64.powi(attempt as i32)))
}

pub fn sgdb_request(path: &str, params: Option<&[(&str, String)]>) -> Result<Value, String> {
    let api_key = credentials()?;
    let mut url = format!("{SGDB_ENDPOINT}/{}", path.trim_start_matches('/'));
    if let Some(p) = params
        && !p.is_empty()
    {
        url = format!("{url}?{}", urlencode(p));
    }
    if !url.starts_with("https://") {
        return Err("SteamGridDB requests must stay on https.".to_owned());
    }
    if session_disabled() {
        return Err(
            "SteamGridDB is disabled for this session (the API key was rejected).".to_owned(),
        );
    }
    let auth = format!("Bearer {api_key}");
    let ua = format!("OpenBox/{APP_VERSION}");
    let mut last_error = String::new();

    for attempt in 0..MAX_RETRIES {
        throttle();
        match http_get_raw(
            &url,
            &[("Authorization", auth.clone()), ("User-Agent", ua.clone())],
            30,
            8 * 1024 * 1024,
        ) {
            Ok(resp) => {
                if resp.status == 401 || resp.status == 403 {
                    SESSION_DISABLED.store(true, Ordering::Release);
                    return Err(
                        "SteamGridDB rejected the API key; the provider is disabled until restart."
                            .to_owned(),
                    );
                }
                if resp.status == 429 || matches!(resp.status, 500 | 502 | 503 | 504) {
                    last_error = format!("HTTP {}", resp.status);
                    if attempt + 1 < MAX_RETRIES {
                        std::thread::sleep(retry_delay(&resp.retry_after, attempt));
                    }
                    continue;
                }
                if resp.status >= 400 {
                    last_error = format!("HTTP {}", resp.status);
                    break;
                }
                return serde_json::from_slice(&resp.body)
                    .map_err(|e| format!("Invalid SteamGridDB JSON: {e}"));
            }
            Err(err) => {
                last_error = err;
                break;
            }
        }
    }
    Err(format!("SteamGridDB request failed: {last_error}"))
}

fn cache_key(params: &BTreeMap<String, String>) -> String {
    let pairs: Vec<(&str, String)> = params
        .iter()
        .map(|(k, v)| (k.as_str(), v.clone()))
        .collect();
    let mut h = Sha1::new();
    h.update(urlencode(&pairs).as_bytes());
    hex::encode(h.finalize())
}

pub fn cache_get(cache_dir: &Path, params: &BTreeMap<String, String>) -> Option<Value> {
    let path = cache_dir
        .join("steamgrid")
        .join(format!("{}.json", cache_key(params)));
    let meta = fs::metadata(&path).ok()?;
    let age = SystemTime::now()
        .duration_since(meta.modified().ok()?)
        .ok()?
        .as_secs();
    if age >= SGDB_CACHE_TTL_SECS {
        return None;
    }
    let raw = fs::read_to_string(&path).ok()?;
    serde_json::from_str(&raw).ok()
}

pub fn cache_put(cache_dir: &Path, params: &BTreeMap<String, String>, payload: &Value) {
    let dir = cache_dir.join("steamgrid");
    if fs::create_dir_all(&dir).is_ok()
        && let Ok(text) = serde_json::to_string(payload)
    {
        let _ = fs::write(dir.join(format!("{}.json", cache_key(params))), text);
    }
}

pub fn cache_size(cache_dir: &Path) -> usize {
    let dir = cache_dir.join("steamgrid");
    fs::read_dir(&dir)
        .map(|rd| rd.flatten().filter(|e| e.path().is_file()).count())
        .unwrap_or(0)
}

fn year_from_ts(val: Option<&Value>) -> String {
    let Some(secs) = val.and_then(Value::as_i64) else {
        return String::new();
    };
    jiff::Timestamp::from_second(secs)
        .ok()
        .map(|ts| {
            jiff::tz::TimeZone::UTC
                .to_datetime(ts)
                .date()
                .year()
                .to_string()
        })
        .unwrap_or_default()
}

pub fn search_games(
    name: &str,
    limit: usize,
    cache_dir: Option<&Path>,
) -> Result<Vec<Value>, String> {
    let trimmed = name.trim();
    if trimmed.is_empty() {
        return Ok(Vec::new());
    }
    let cap = limit.clamp(1, 50);
    let mut key = BTreeMap::new();
    key.insert("sgdb_search".to_owned(), trimmed.to_owned());
    key.insert("limit".to_owned(), cap.to_string());
    if let Some(cd) = cache_dir
        && let Some(cached) = cache_get(cd, &key)
        && let Some(arr) = cached.as_array()
    {
        return Ok(arr.clone());
    }
    let payload = sgdb_request(
        &format!("search/autocomplete/{}", quote_path(trimmed)),
        None,
    )?;
    let rows = payload
        .get("data")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let mut results = Vec::new();
    for row in rows.iter().take(cap) {
        if !row.is_object() {
            continue;
        }
        let types: Vec<Value> = row
            .get("types")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .map(|v| Value::String(v.as_str().unwrap_or("").to_owned()))
                    .collect()
            })
            .unwrap_or_default();
        results.push(json!({
            "id": row.get("id").cloned().unwrap_or(Value::Null),
            "name": row.get("name").and_then(Value::as_str).unwrap_or(""),
            "year": year_from_ts(row.get("release_date")),
            "types": types,
            "verified": row.get("verified").and_then(Value::as_bool).unwrap_or(false),
            "provider": PROVIDER,
        }));
    }
    if let Some(cd) = cache_dir {
        cache_put(cd, &key, &Value::Array(results.clone()));
    }
    Ok(results)
}

fn asset_entry(kind: &str, row: &Value) -> Option<Value> {
    let m = row.as_object()?;
    if m.get("nsfw").and_then(Value::as_bool).unwrap_or(false)
        || m.get("epilepsy").and_then(Value::as_bool).unwrap_or(false)
        || m.get("humor").and_then(Value::as_bool).unwrap_or(false)
    {
        return None;
    }
    let url = m.get("url").and_then(Value::as_str).unwrap_or("").trim();
    if url.is_empty() {
        return None;
    }
    let score = m.get("score").and_then(Value::as_f64).unwrap_or(0.0);
    let width = m
        .get("width")
        .and_then(Value::as_i64)
        .map(|v| json!(v))
        .unwrap_or(Value::Null);
    let height = m
        .get("height")
        .and_then(Value::as_i64)
        .map(|v| json!(v))
        .unwrap_or(Value::Null);
    Some(json!({
        "kind": kind,
        "url": url,
        "thumb": m.get("thumb").and_then(Value::as_str).unwrap_or(""),
        "score": score,
        "style": m.get("style").and_then(Value::as_str).unwrap_or(""),
        "width": width,
        "height": height,
    }))
}

pub fn game_assets(game_id: i64, cache_dir: Option<&Path>) -> Result<Vec<Value>, String> {
    let mut key = BTreeMap::new();
    key.insert("sgdb_assets".to_owned(), game_id.to_string());
    if let Some(cd) = cache_dir
        && let Some(cached) = cache_get(cd, &key)
        && let Some(arr) = cached.as_array()
    {
        return Ok(arr.clone());
    }
    let mut media = Vec::new();
    for (kind, prefix, extra) in ASSET_ENDPOINTS {
        let extra_vec: Vec<(&str, String)> = extra
            .map(|(k, v)| vec![(k, v.to_owned())])
            .unwrap_or_default();
        let payload = sgdb_request(
            &format!("{prefix}/game/{game_id}"),
            if extra_vec.is_empty() {
                None
            } else {
                Some(&extra_vec)
            },
        )?;
        if let Some(rows) = payload.get("data").and_then(Value::as_array) {
            for row in rows {
                if let Some(entry) = asset_entry(kind, row) {
                    media.push(entry);
                }
            }
        }
    }
    media.sort_by(|a, b| {
        let sa = a.get("score").and_then(Value::as_f64).unwrap_or(0.0);
        let sb = b.get("score").and_then(Value::as_f64).unwrap_or(0.0);
        sb.total_cmp(&sa)
    });
    if let Some(cd) = cache_dir {
        cache_put(cd, &key, &Value::Array(media.clone()));
    }
    Ok(media)
}

pub fn game_info(game_id: i64, cache_dir: Option<&Path>) -> Result<Value, String> {
    let mut key = BTreeMap::new();
    key.insert("sgdb_game".to_owned(), game_id.to_string());
    if let Some(cd) = cache_dir
        && let Some(cached) = cache_get(cd, &key)
    {
        return Ok(cached);
    }
    let resp = sgdb_request(&format!("games/id/{game_id}"), None)?;
    let data = resp.get("data").and_then(Value::as_object);
    let empty = Map::new();
    let d = data.unwrap_or(&empty);
    if d.get("id").is_none() && d.get("name").is_none() {
        return Err("Game not found on SteamGridDB.".to_owned());
    }
    let types: Vec<Value> = d
        .get("types")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .map(|v| Value::String(v.as_str().unwrap_or("").to_owned()))
                .collect()
        })
        .unwrap_or_default();
    let media = game_assets(game_id, cache_dir)?;
    let metadata = json!({
        "id": d.get("id").and_then(Value::as_i64).unwrap_or(game_id),
        "name": d.get("name").and_then(Value::as_str).unwrap_or(""),
        "year": year_from_ts(d.get("release_date")),
        "types": types,
        "verified": d.get("verified").and_then(Value::as_bool).unwrap_or(false),
        "media": media,
    });
    if let Some(cd) = cache_dir {
        cache_put(cd, &key, &metadata);
    }
    Ok(metadata)
}

pub fn clean_media_url(url: &str) -> String {
    ss_clean_media_url(url)
}

pub fn choose_media(metadata: &Value, media_kinds: &[String]) -> HashMap<String, String> {
    let entries = if let Some(arr) = metadata.get("media").and_then(Value::as_array) {
        arr.as_slice()
    } else if let Some(arr) = metadata.as_array() {
        arr.as_slice()
    } else {
        &[]
    };
    let wanted: HashSet<&str> = media_kinds.iter().map(String::as_str).collect();
    let mut ordered: Vec<&Value> = entries.iter().filter(|e| e.is_object()).collect();
    ordered.sort_by(|a, b| {
        let sa = a.get("score").and_then(Value::as_f64).unwrap_or(0.0);
        let sb = b.get("score").and_then(Value::as_f64).unwrap_or(0.0);
        sb.total_cmp(&sa)
    });
    let mut chosen = HashMap::new();
    for entry in ordered {
        let kind = entry.get("kind").and_then(Value::as_str).unwrap_or("");
        if !wanted.contains(kind) || chosen.contains_key(kind) {
            continue;
        }
        let url = clean_media_url(entry.get("url").and_then(Value::as_str).unwrap_or(""));
        if !url.is_empty() {
            chosen.insert(kind.to_owned(), url);
        }
    }
    chosen
}

pub fn apply_to_game(game: &mut Map<String, Value>, metadata: &Value, fields: &[String]) {
    for field in fields {
        if let Some(v) = metadata.get(field) {
            let empty = match v {
                Value::Null => true,
                Value::String(s) => s.is_empty(),
                _ => false,
            };
            if !empty {
                game.insert(field.clone(), v.clone());
            }
        }
    }
    if let Some(id) = metadata.get("id").filter(|v| !v.is_null()) {
        game.insert("steamgrid_id".into(), id.clone());
    }
}
