//! Media hygiene, region priority, duplicate detection, cache cleanup, durable media queue,
//! and Steam/GOG/Bezel/Screenshot/Media-Pack helpers (`pkg/parity/parity_media.py`,
//! `parity_premium.py`, `parity_integrations.py`, `pkg/state/media_probe.py`).

use std::collections::{HashMap, HashSet};
use std::fs::{self, File};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, LazyLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use openbox_io::{ArchiveLimits, atomic_write_text, safe_zip_extract};
use openbox_platform::{expand_user, file_lock, which};
use openbox_runtime::AppState;
use openbox_runtime::media_probe::{MEDIA_PATH_FIELDS, MEDIA_TYPES_ALL};
use parking_lot::Mutex;
use regex::Regex;
use serde_json::{Map, Value, json};
use sha1::{Digest as _, Sha1};
use sha2::Sha256;

use crate::emumovies::{download_emumovies_media, load_emumovies_credentials};
use crate::http::{
    download_bytes_sync, download_file_sync, ext_from_url, http_get_json, quote_path,
};
use crate::lbdb::apply_game_metadata;
use crate::providers::{download_candidate_media, search_all};
use crate::steamgrid;

pub const REGION_PRIORITY_DEFAULT: &[&str] = &["North America", "World", "Europe", "Japan", ""];
pub const VIDEO_FIELDS: &[&str] = &[
    "video_snap",
    "video_theme",
    "video_trailer",
    "video_recording",
];

pub const MEDIA_QUEUE_MAX: usize = 5000;
pub const MEDIA_QUEUE_MAX_ATTEMPTS: usize = 3;
pub const MEDIA_QUEUE_BACKOFF_SECS: &[u64] = &[5, 30, 120];
const BUILTIN_QUEUE_SOURCES: &[&str] = &[
    "launchbox",
    "screenscraper",
    "igdb",
    "steamgriddb",
    "emumovies",
];

static HTML_TAG_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"<[^>]+>").unwrap());

pub fn sort_images_by_region(images: &[Value], region_priority: &[String]) -> Vec<Value> {
    let mut sorted = images.to_vec();
    sorted.sort_by(|a, b| {
        let ra = a.get("region").and_then(Value::as_str).unwrap_or("");
        let rb = b.get("region").and_then(Value::as_str).unwrap_or("");
        let ia = region_priority
            .iter()
            .position(|p| p == ra)
            .unwrap_or(region_priority.len());
        let ib = region_priority
            .iter()
            .position(|p| p == rb)
            .unwrap_or(region_priority.len());
        let fa = a.get("filename").and_then(Value::as_str).unwrap_or("");
        let fb = b.get("filename").and_then(Value::as_str).unwrap_or("");
        (ia, fa).cmp(&(ib, fb))
    });
    sorted
}

fn fingerprint_file(path: &Path) -> Result<String, std::io::Error> {
    let size = fs::metadata(path)?.len();
    let mut file = File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; 1024 * 1024];
    loop {
        let n = file.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(format!("{size}:{}", hex::encode(hasher.finalize())))
}

fn canonical_path_loose(p: &Path) -> PathBuf {
    fs::canonicalize(p).unwrap_or_else(|_| p.to_path_buf())
}

pub fn find_duplicate_media(games: &[Value], allowed_roots: &[PathBuf]) -> Vec<Value> {
    let mut size_buckets: HashMap<u64, Vec<PathBuf>> = HashMap::new();
    for game in games {
        let mut paths: Vec<String> = Vec::new();
        for field in MEDIA_PATH_FIELDS {
            if let Some(s) = game
                .get(*field)
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|s| !s.is_empty())
            {
                paths.push(s.to_owned());
            }
        }
        if let Some(shots) = game.get("screenshots").and_then(Value::as_array) {
            for shot in shots {
                if let Some(s) = shot.as_str().map(str::trim).filter(|s| !s.is_empty()) {
                    paths.push(s.to_owned());
                }
            }
        }
        for raw in paths {
            let p = expand_user(Path::new(&raw));
            let Ok(meta) = fs::symlink_metadata(&p) else {
                continue;
            };
            if meta.file_type().is_symlink() || !meta.is_file() {
                continue;
            }
            size_buckets.entry(meta.len()).or_default().push(p);
        }
    }

    let mut buckets: HashMap<String, Vec<String>> = HashMap::new();
    for paths in size_buckets.values() {
        if paths.len() <= 1 {
            continue;
        }
        for p in paths {
            if let Ok(key) = fingerprint_file(p) {
                buckets
                    .entry(key)
                    .or_default()
                    .push(p.to_string_lossy().into_owned());
            }
        }
    }

    let roots: Vec<PathBuf> = allowed_roots
        .iter()
        .map(|r| canonical_path_loose(&expand_user(r)))
        .collect();
    let mut groups = Vec::new();
    for paths in buckets.into_values() {
        let mut unique: Vec<String> = Vec::new();
        for p in paths {
            if !unique.contains(&p) {
                unique.push(p);
            }
        }
        if unique.len() <= 1 {
            continue;
        }
        let mut keep = unique[0].clone();
        if !roots.is_empty()
            && let Some(in_root) = unique.iter().find(|cand| {
                let res = canonical_path_loose(&expand_user(Path::new(cand)));
                roots
                    .iter()
                    .any(|root| res == *root || res.starts_with(root))
            })
        {
            keep = in_root.clone();
        }
        let duplicates: Vec<Value> = unique
            .into_iter()
            .filter(|p| *p != keep)
            .map(Value::String)
            .collect();
        groups.push(json!({"keep": keep, "duplicates": duplicates}));
    }
    groups
}

pub fn cleanup_generated_cache(data_root: &Path, days: u64) -> Value {
    let root = expand_user(data_root).join("cache").join("media");
    let mut deleted = 0usize;
    let mut bytes = 0u64;
    let mut skipped = 0usize;
    let is_ok_dir = fs::symlink_metadata(&root)
        .map(|m| m.is_dir() && !m.file_type().is_symlink())
        .unwrap_or(false);
    if !is_ok_dir {
        return json!({
            "root": root.to_string_lossy(),
            "deleted": 0,
            "bytes": 0,
            "skipped": 0,
        });
    }
    let now_secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let cutoff = now_secs.saturating_sub(days.saturating_mul(86400));
    let mut files = Vec::new();
    collect_non_symlink_files(&root, &mut files);
    files.sort();
    for path in files {
        let Ok(meta) = fs::metadata(&path) else {
            continue;
        };
        let mtime = meta
            .modified()
            .ok()
            .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
            .map(|d| d.as_secs())
            .unwrap_or(now_secs);
        if mtime >= cutoff {
            continue;
        }
        match fs::remove_file(&path) {
            Ok(()) => {
                deleted += 1;
                bytes = bytes.saturating_add(meta.len());
            }
            Err(_) => {
                skipped += 1;
            }
        }
    }
    json!({
        "root": root.to_string_lossy(),
        "deleted": deleted,
        "bytes": bytes,
        "skipped": skipped,
    })
}

fn collect_non_symlink_files(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(rd) = fs::read_dir(dir) else {
        return;
    };
    for entry in rd.flatten() {
        let p = entry.path();
        let Ok(meta) = fs::symlink_metadata(&p) else {
            continue;
        };
        if meta.file_type().is_symlink() {
            continue;
        }
        if meta.is_dir() {
            collect_non_symlink_files(&p, out);
        } else if meta.is_file() {
            out.push(p);
        }
    }
}

pub fn cleanup_duplicates(
    duplicate_groups: &[Value],
    dry_run: bool,
    allowed_roots: &[PathBuf],
) -> Vec<String> {
    let roots: Vec<PathBuf> = allowed_roots
        .iter()
        .map(|r| canonical_path_loose(&expand_user(r)))
        .collect();
    let mut deleted = Vec::new();
    for group in duplicate_groups {
        let Some(dups) = group.get("duplicates").and_then(Value::as_array) else {
            continue;
        };
        for pval in dups {
            let Some(ps) = pval.as_str() else {
                continue;
            };
            let target = Path::new(ps);
            if dry_run {
                deleted.push(ps.to_owned());
                continue;
            }
            let Ok(meta) = fs::symlink_metadata(target) else {
                continue;
            };
            if meta.file_type().is_symlink() || !meta.is_file() {
                continue;
            }
            let resolved = canonical_path_loose(&expand_user(target));
            if !roots.is_empty()
                && !roots
                    .iter()
                    .any(|r| resolved == *r || resolved.starts_with(r))
            {
                continue;
            }
            if fs::remove_file(&resolved).is_ok() {
                deleted.push(ps.to_owned());
            }
        }
    }
    deleted
}

pub fn active_video(
    app: &AppState,
    game: &Value,
    priorities: Option<&[String]>,
) -> Option<(String, String)> {
    let default_pri: Vec<String> = VIDEO_FIELDS.iter().map(|s| (*s).to_owned()).collect();
    let pri = priorities.unwrap_or(&default_pri);
    for field in pri {
        let raw = game.get(field).and_then(Value::as_str).unwrap_or("").trim();
        if !raw.is_empty() && Path::new(raw).is_file() {
            let san = app.media_probe.sanitize_media_path(raw);
            if !san.is_empty() {
                return Some((field.clone(), raw.to_owned()));
            }
        }
    }
    let legacy = game
        .get("video")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if !legacy.is_empty() && Path::new(legacy).is_file() {
        let san = app.media_probe.sanitize_media_path(legacy);
        if !san.is_empty() {
            return Some(("video".to_owned(), legacy.to_owned()));
        }
    }
    None
}

// ── Durable media download queue ───────────────────────────────────────────

fn media_item_id(game_id: &str, source: &str, kind: &str) -> String {
    let mut h = Sha1::new();
    h.update(format!("{game_id}|{source}|{kind}").as_bytes());
    let hex_s = hex::encode(h.finalize());
    format!("mq-{}", &hex_s[..12])
}

fn clean_media_source(source: &str, data: &Map<String, Value>) -> Result<String, String> {
    let mut val = source.trim().to_owned();
    if val.is_empty() {
        let s = data
            .get("source")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        if BUILTIN_QUEUE_SOURCES.contains(&s) {
            val = s.to_owned();
        } else if data
            .get("launchbox_db_id")
            .is_some_and(|v| !v.is_null() && v.as_str() != Some(""))
        {
            val = "launchbox".to_owned();
        } else if data
            .get("emumovies")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            val = "emumovies".to_owned();
        } else if data
            .get("steamgrid_id")
            .or_else(|| data.get("steamgriddb_id"))
            .is_some_and(|v| !v.is_null() && v.as_str() != Some(""))
        {
            val = "steamgriddb".to_owned();
        } else {
            val = "launchbox".to_owned();
        }
    }
    if BUILTIN_QUEUE_SOURCES.contains(&val.as_str())
        || (val.starts_with("plugin:") && val.len() > "plugin:".len())
    {
        Ok(val)
    } else {
        Err(format!("Unsupported media source: {val}"))
    }
}

fn valid_media_kind(kind: &str) -> Result<String, String> {
    let val = kind.trim();
    if val.is_empty() {
        return Err("A media kind is required.".to_owned());
    }
    if !MEDIA_TYPES_ALL.contains(&val) && val != "video_recording" {
        return Err(format!("Unsupported media kind: {val}"));
    }
    Ok(val.to_owned())
}

pub fn normalize_media_queue_item(raw: &Value) -> Result<Value, String> {
    let obj = raw
        .as_object()
        .ok_or_else(|| "Media queue jobs must be objects.".to_owned())?;
    let mut data = obj.clone();
    let game_id = data
        .get("game_id")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_owned();
    let kinds: Vec<String> = data
        .get("media")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .map(|s| s.trim().to_owned())
                .filter(|s| !s.is_empty())
                .collect()
        })
        .unwrap_or_default();
    let mut kind = data
        .get("kind")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_owned();
    if kind.is_empty() {
        kind = kinds.first().cloned().unwrap_or_else(|| "cover".to_owned());
    }
    let source = clean_media_source(
        data.get("source").and_then(Value::as_str).unwrap_or(""),
        &data,
    )?;
    if !kinds.is_empty() {
        data.insert(
            "media".into(),
            Value::Array(kinds.into_iter().map(Value::String).collect()),
        );
    }
    let attempts = data
        .get("attempts")
        .and_then(Value::as_u64)
        .unwrap_or(0)
        .min(MEDIA_QUEUE_MAX_ATTEMPTS as u64);
    let state_s = match data.get("state").and_then(Value::as_str).unwrap_or("") {
        "failed" => "failed",
        _ => "queued",
    };
    let last_error: String = data
        .get("last_error")
        .and_then(Value::as_str)
        .unwrap_or("")
        .chars()
        .take(500)
        .collect();
    let next_attempt_at = data
        .get("next_attempt_at")
        .and_then(Value::as_f64)
        .unwrap_or(0.0)
        .max(0.0);
    let id = data
        .get("id")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(ToOwned::to_owned)
        .unwrap_or_else(|| media_item_id(&game_id, &source, &kind));

    data.insert("game_id".into(), Value::String(game_id));
    data.insert("source".into(), Value::String(source));
    data.insert("kind".into(), Value::String(kind));
    data.insert(
        "url".into(),
        Value::String(
            data.get("url")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned(),
        ),
    );
    data.insert(
        "overwrite".into(),
        Value::Bool(
            data.get("overwrite")
                .and_then(Value::as_bool)
                .unwrap_or(false),
        ),
    );
    data.insert("attempts".into(), json!(attempts));
    data.insert("state".into(), Value::String(state_s.to_owned()));
    data.insert("last_error".into(), Value::String(last_error));
    data.insert("next_attempt_at".into(), json!(next_attempt_at));
    data.insert("id".into(), Value::String(id));
    Ok(Value::Object(data))
}

pub fn load_normalized_media_queue(queue_path: &Path) -> Vec<Value> {
    let Ok(raw) = fs::read_to_string(queue_path) else {
        return Vec::new();
    };
    let Ok(Value::Array(arr)) = serde_json::from_str::<Value>(&raw) else {
        return Vec::new();
    };
    arr.iter()
        .filter_map(|it| normalize_media_queue_item(it).ok())
        .collect()
}

pub fn save_media_queue(queue_path: &Path, items: &[Value]) -> Result<(), String> {
    let text = serde_json::to_string_pretty(items).map_err(|e| e.to_string())?;
    atomic_write_text(queue_path, &text, 0o600).map_err(|e| e.to_string())?;
    Ok(())
}

fn with_queue_lock<T, F: FnOnce() -> T>(queue_path: &Path, f: F) -> T {
    let parent = queue_path.parent().unwrap_or_else(|| Path::new("."));
    let _ = fs::create_dir_all(parent);
    let fname = queue_path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| "media-queue.json".to_owned());
    let lock_path = parent.join(format!(".{fname}.lock"));
    let _guard = file_lock(&lock_path, true, Duration::from_secs(10)).ok();
    f()
}

pub fn enqueue_media_job(queue_path: &Path, job: &Value) -> Result<usize, String> {
    with_queue_lock(queue_path, || {
        let mut items = load_normalized_media_queue(queue_path);
        let obj = job
            .as_object()
            .ok_or_else(|| "Media queue jobs must be objects.".to_owned())?;
        let source =
            clean_media_source(obj.get("source").and_then(Value::as_str).unwrap_or(""), obj)?;
        let mut kinds: Vec<String> = obj
            .get("media")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .map(|s| s.trim().to_owned())
                    .filter(|s| !s.is_empty())
                    .collect()
            })
            .unwrap_or_default();
        if kinds.is_empty()
            && let Some(k) = obj
                .get("kind")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|s| !s.is_empty())
        {
            kinds.push(k.to_owned());
        }
        if kinds.is_empty() {
            kinds.push("cover".to_owned());
        }
        for media_kind in kinds {
            valid_media_kind(&media_kind)?;
            let mut copy = obj.clone();
            copy.insert("source".into(), Value::String(source.clone()));
            copy.insert("kind".into(), Value::String(media_kind.clone()));
            copy.insert("media".into(), json!([media_kind]));
            let normalized = normalize_media_queue_item(&Value::Object(copy))?;
            let key = (
                normalized["game_id"].as_str().unwrap_or("").to_owned(),
                normalized["source"].as_str().unwrap_or("").to_owned(),
                normalized["kind"].as_str().unwrap_or("").to_owned(),
            );
            let ow = normalized["overwrite"].as_bool().unwrap_or(false);
            let mut replaced = false;
            for existing in &mut items {
                let ek = (
                    existing["game_id"].as_str().unwrap_or("").to_owned(),
                    existing["source"].as_str().unwrap_or("").to_owned(),
                    existing["kind"].as_str().unwrap_or("").to_owned(),
                );
                if ek == key && existing["overwrite"].as_bool().unwrap_or(false) == ow {
                    *existing = normalized.clone();
                    replaced = true;
                    break;
                }
            }
            if !replaced {
                if items.len() >= MEDIA_QUEUE_MAX {
                    return Err(format!("Media queue is full ({MEDIA_QUEUE_MAX} items)."));
                }
                items.push(normalized);
            }
        }
        save_media_queue(queue_path, &items)?;
        Ok(items.len())
    })
}

pub fn retry_media_queue_item(queue_path: &Path, item_id: &str) -> bool {
    with_queue_lock(queue_path, || {
        let mut items = load_normalized_media_queue(queue_path);
        let mut changed = false;
        for item in &mut items {
            if item.get("id").and_then(Value::as_str) == Some(item_id)
                && let Some(m) = item.as_object_mut()
            {
                m.insert("state".into(), Value::String("queued".to_owned()));
                m.insert("attempts".into(), json!(0));
                m.insert("last_error".into(), Value::String(String::new()));
                m.insert("next_attempt_at".into(), json!(0.0));
                changed = true;
            }
        }
        if changed {
            let _ = save_media_queue(queue_path, &items);
        }
        changed
    })
}

pub fn media_queue_snapshot(
    queue_path: &Path,
    running: bool,
    paused: bool,
    completed: usize,
) -> Value {
    let items = load_normalized_media_queue(queue_path);
    let failed = items
        .iter()
        .filter(|i| i.get("state").and_then(Value::as_str) == Some("failed"))
        .count();
    let queued = items.len().saturating_sub(failed);
    json!({
        "items": items,
        "queue": items,
        "running": running,
        "paused": paused,
        "completed": completed,
        "failed": failed,
        "counts": {
            "total": items.len(),
            "queued": queued,
            "failed": failed,
            "completed": completed,
        }
    })
}

fn download_media_for_item(
    app: &AppState,
    game: &Value,
    kind: &str,
    item: &Value,
    state: &Value,
) -> Result<String, String> {
    let source = item.get("source").and_then(Value::as_str).unwrap_or("");
    let media_root = app.data_dir.join("media");
    let database_path = app.data_dir.join("metadata").join("launchbox.db");
    match source {
        "launchbox" => {
            if !database_path.is_file() {
                return Err("The LaunchBox metadata database is not installed.".to_owned());
            }
            let db_id = game
                .get("launchbox_db_id")
                .and_then(|v| match v {
                    Value::Number(n) => n.as_i64(),
                    Value::String(s) => s.trim().parse::<i64>().ok(),
                    _ => None,
                })
                .ok_or_else(|| "Game has no LaunchBox database id.".to_owned())?;
            let download_kind = if matches!(kind, "poster" | "square") {
                "cover"
            } else {
                kind
            };
            let mut copy = game.as_object().cloned().unwrap_or_default();
            apply_game_metadata(
                &mut copy,
                &database_path,
                db_id,
                &[download_kind.to_owned()],
                &media_root,
                item.get("overwrite")
                    .and_then(Value::as_bool)
                    .unwrap_or(false),
                None,
            )?;
            if download_kind == "screenshots" {
                let existing: HashSet<&str> = game
                    .get("screenshots")
                    .and_then(Value::as_array)
                    .map(|a| a.iter().filter_map(Value::as_str).collect())
                    .unwrap_or_default();
                if let Some(arr) = copy.get("screenshots").and_then(Value::as_array) {
                    for p in arr {
                        if let Some(ps) = p.as_str()
                            && !existing.contains(ps)
                        {
                            return Ok(ps.to_owned());
                        }
                    }
                }
                Ok(String::new())
            } else {
                let new_val = copy
                    .get(download_kind)
                    .and_then(Value::as_str)
                    .unwrap_or("");
                let old_val = game.get(kind).and_then(Value::as_str).unwrap_or("");
                if !new_val.is_empty() && new_val != old_val {
                    Ok(new_val.to_owned())
                } else {
                    Ok(String::new())
                }
            }
        }
        "emumovies" => {
            let (u, p) = load_emumovies_credentials(&app.data_dir);
            download_emumovies_media(game, &u, &p, &media_root, kind)
        }
        "steamgriddb" => {
            let gid = game
                .get("steamgrid_id")
                .or_else(|| game.get("steamgriddb_id"))
                .and_then(|v| match v {
                    Value::Number(n) => n.as_i64(),
                    Value::String(s) => s.trim().parse::<i64>().ok(),
                    _ => None,
                })
                .ok_or_else(|| "Game has no SteamGridDB match.".to_owned())?;
            let info = steamgrid::game_info(gid, Some(&app.data_dir.join("cache")))?;
            let url = info
                .get("media")
                .and_then(Value::as_array)
                .and_then(|arr| {
                    arr.iter()
                        .find(|e| e.get("kind").and_then(Value::as_str) == Some(kind))
                })
                .and_then(|e| e.get("url"))
                .and_then(Value::as_str)
                .ok_or_else(|| format!("SteamGridDB has no {kind} artwork."))?;
            let ext = ext_from_url(url, ".jpg");
            let slug: String = game
                .get("name")
                .and_then(Value::as_str)
                .unwrap_or("game")
                .to_lowercase()
                .chars()
                .map(|c| if c.is_alphanumeric() { c } else { '-' })
                .collect::<String>()
                .trim_matches('-')
                .to_owned();
            download_candidate_media(
                url,
                kind,
                &media_root
                    .join("steamgriddb")
                    .join(if slug.is_empty() { "game" } else { &slug }),
                Some(&format!("{kind}{ext}")),
            )
        }
        "screenscraper" | "igdb" => {
            let cands = search_all(state, &database_path, game, Some(&[source.to_owned()]), 5);
            for cand in cands {
                if let Some(urls) = cand
                    .get("media")
                    .and_then(|m| m.get(kind))
                    .and_then(Value::as_array)
                {
                    for u in urls {
                        if let Some(us) = u.as_str().map(str::trim).filter(|s| !s.is_empty()) {
                            let ext = ext_from_url(us, ".jpg");
                            let slug: String = game
                                .get("name")
                                .and_then(Value::as_str)
                                .unwrap_or("game")
                                .to_lowercase()
                                .chars()
                                .map(|c| if c.is_alphanumeric() { c } else { '-' })
                                .collect::<String>()
                                .trim_matches('-')
                                .to_owned();
                            return download_candidate_media(
                                us,
                                kind,
                                &media_root.join(source).join(if slug.is_empty() {
                                    "game"
                                } else {
                                    &slug
                                }),
                                Some(&format!("{kind}{ext}")),
                            );
                        }
                    }
                }
            }
            Err(format!("{source} has no {kind} media for this game."))
        }
        other => Err(format!("Unsupported media source: {other}")),
    }
}

pub struct MediaQueueWorker {
    running: AtomicBool,
    paused: AtomicBool,
    cancelled: AtomicBool,
    completed: AtomicUsize,
    failed: AtomicUsize,
    lock: Mutex<()>,
}

static MEDIA_QUEUE_WORKER: LazyLock<Arc<MediaQueueWorker>> = LazyLock::new(|| {
    Arc::new(MediaQueueWorker {
        running: AtomicBool::new(false),
        paused: AtomicBool::new(false),
        cancelled: AtomicBool::new(false),
        completed: AtomicUsize::new(0),
        failed: AtomicUsize::new(0),
        lock: Mutex::new(()),
    })
});

pub fn media_queue_worker_state() -> Value {
    let w = &*MEDIA_QUEUE_WORKER;
    json!({
        "running": w.running.load(Ordering::Acquire),
        "paused": w.paused.load(Ordering::Acquire),
        "cancelled": w.cancelled.load(Ordering::Acquire),
        "completed": w.completed.load(Ordering::Acquire),
        "failed": w.failed.load(Ordering::Acquire),
    })
}

pub fn pause_media_queue_worker() -> Value {
    MEDIA_QUEUE_WORKER.paused.store(true, Ordering::Release);
    media_queue_worker_state()
}

pub fn resume_media_queue_worker(app: Arc<AppState>) -> Value {
    MEDIA_QUEUE_WORKER.paused.store(false, Ordering::Release);
    MEDIA_QUEUE_WORKER.cancelled.store(false, Ordering::Release);
    start_media_queue_worker(app);
    media_queue_worker_state()
}

pub fn cancel_media_queue_worker() -> Value {
    MEDIA_QUEUE_WORKER.cancelled.store(true, Ordering::Release);
    MEDIA_QUEUE_WORKER.paused.store(false, Ordering::Release);
    media_queue_worker_state()
}

pub fn start_media_queue_worker(app: Arc<AppState>) {
    let w = Arc::clone(&*MEDIA_QUEUE_WORKER);
    {
        let _g = w.lock.lock();
        if w.running.swap(true, Ordering::AcqRel) {
            return;
        }
        w.cancelled.store(false, Ordering::Release);
        w.paused.store(false, Ordering::Release);
    }
    std::thread::Builder::new()
        .name("openbox-media-queue".into())
        .spawn(move || {
            let queue_path = app.data_dir.join("media-queue.json");
            loop {
                if w.cancelled.load(Ordering::Acquire) {
                    break;
                }
                if w.paused.load(Ordering::Acquire) {
                    std::thread::sleep(Duration::from_millis(500));
                    continue;
                }
                let processed = process_media_queue_once(&app, &queue_path, &w);
                if processed == 0 {
                    break;
                }
            }
            w.running.store(false, Ordering::Release);
        })
        .ok();
}

fn process_media_queue_once(app: &AppState, queue_path: &Path, w: &MediaQueueWorker) -> usize {
    let Ok(state) = app.load_state_view() else {
        return 0;
    };
    let mut items = load_normalized_media_queue(queue_path);
    let now_secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    let mut processed = 0usize;
    let mut idx = 0usize;

    while idx < items.len() {
        if w.cancelled.load(Ordering::Acquire) || w.paused.load(Ordering::Acquire) {
            break;
        }
        let item = items[idx].clone();
        if item.get("state").and_then(Value::as_str) == Some("failed") {
            idx += 1;
            continue;
        }
        if item
            .get("next_attempt_at")
            .and_then(Value::as_f64)
            .unwrap_or(0.0)
            > now_secs
        {
            idx += 1;
            continue;
        }
        let gid = item.get("game_id").and_then(Value::as_str).unwrap_or("");
        let kind = item.get("kind").and_then(Value::as_str).unwrap_or("cover");
        let overwrite = item
            .get("overwrite")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let game = state
            .get("games")
            .and_then(Value::as_array)
            .and_then(|arr| {
                arr.iter()
                    .find(|g| g.get("game_id").and_then(Value::as_str) == Some(gid))
            })
            .cloned();
        let Some(game) = game else {
            if let Some(m) = items[idx].as_object_mut() {
                m.insert(
                    "last_error".into(),
                    Value::String("Game is no longer in the library.".to_owned()),
                );
                m.insert("attempts".into(), json!(MEDIA_QUEUE_MAX_ATTEMPTS));
                m.insert("state".into(), Value::String("failed".to_owned()));
            }
            w.failed.fetch_add(1, Ordering::Relaxed);
            with_queue_lock(queue_path, || {
                let _ = save_media_queue(queue_path, &items);
            });
            idx += 1;
            continue;
        };
        match download_media_for_item(app, &game, kind, &item, &state) {
            Ok(path) => {
                if !path.is_empty() {
                    let gid_s = gid.to_owned();
                    let kind_s = kind.to_owned();
                    let path_s = path.clone();
                    let _ = app.transact_state(|st| {
                        if let Some(games) = st.get_mut("games").and_then(Value::as_array_mut)
                            && let Some(target) = games
                                .iter_mut()
                                .find(|g| g.get("game_id").and_then(Value::as_str) == Some(&gid_s))
                            && let Some(m) = target.as_object_mut()
                        {
                            if kind_s == "screenshots" {
                                let arr = m
                                    .entry("screenshots".to_owned())
                                    .or_insert_with(|| Value::Array(Vec::new()));
                                if !arr.is_array() {
                                    *arr = Value::Array(Vec::new());
                                }
                                if let Some(list) = arr.as_array_mut() {
                                    if overwrite {
                                        *list = vec![Value::String(path_s)];
                                    } else if !list.iter().any(|v| v.as_str() == Some(&path_s)) {
                                        list.push(Value::String(path_s));
                                    }
                                }
                            } else if overwrite
                                || m.get(&kind_s)
                                    .and_then(Value::as_str)
                                    .is_none_or(str::is_empty)
                            {
                                m.insert(kind_s, Value::String(path_s));
                            }
                        }
                        Ok(())
                    });
                    app.bump_media_epoch();
                    w.completed.fetch_add(1, Ordering::Relaxed);
                }
                items.remove(idx);
                processed += 1;
                with_queue_lock(queue_path, || {
                    let _ = save_media_queue(queue_path, &items);
                });
            }
            Err(err) => {
                let attempts =
                    (item.get("attempts").and_then(Value::as_u64).unwrap_or(0) + 1) as usize;
                if let Some(m) = items[idx].as_object_mut() {
                    m.insert("attempts".into(), json!(attempts));
                    m.insert(
                        "last_error".into(),
                        Value::String(err.chars().take(500).collect()),
                    );
                    if attempts >= MEDIA_QUEUE_MAX_ATTEMPTS {
                        m.insert("state".into(), Value::String("failed".to_owned()));
                        w.failed.fetch_add(1, Ordering::Relaxed);
                    } else {
                        m.insert("state".into(), Value::String("queued".to_owned()));
                        let delay = MEDIA_QUEUE_BACKOFF_SECS
                            [(attempts - 1).min(MEDIA_QUEUE_BACKOFF_SECS.len() - 1)]
                            as f64;
                        m.insert("next_attempt_at".into(), json!(now_secs + delay));
                    }
                }
                with_queue_lock(queue_path, || {
                    let _ = save_media_queue(queue_path, &items);
                });
                idx += 1;
            }
        }
    }
    processed
}

// ── Steam / GOG / Bezel / Screenshot / Media Pack helpers ──────────────────

fn html_unescape_basic(s: &str) -> String {
    s.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&#39;", "'")
        .replace("&apos;", "'")
        .replace("&nbsp;", " ")
}

pub fn update_steam_metadata(game: &mut Map<String, Value>, data_dir: &Path) -> Result<(), String> {
    let app_id = game
        .get("steam_app_id")
        .and_then(|v| match v {
            Value::String(s) => Some(s.clone()),
            Value::Number(n) => Some(n.to_string()),
            _ => None,
        })
        .unwrap_or_default();
    if app_id.is_empty() || !app_id.bytes().all(|b| b.is_ascii_digit()) {
        return Err("This game has no Steam App ID.".to_owned());
    }
    let url = format!("https://store.steampowered.com/api/appdetails?appids={app_id}");
    let payload = http_get_json(
        &url,
        &[("User-Agent", "OpenBox/1".to_owned())],
        15,
        4 * 1024 * 1024,
    )?;
    let rec = payload.get(&app_id).and_then(Value::as_object);
    if !rec
        .and_then(|r| r.get("success"))
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        return Err("Steam did not return metadata for this game.".to_owned());
    }
    let data = rec
        .and_then(|r| r.get("data"))
        .and_then(Value::as_object)
        .ok_or_else(|| "Steam did not return metadata for this game.".to_owned())?;
    if let Some(name) = data
        .get("name")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        game.insert("name".into(), Value::String(name.to_owned()));
    }
    let devs = data
        .get("developers")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>()
                .join(", ")
        })
        .unwrap_or_default();
    let pubs = data
        .get("publishers")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>()
                .join(", ")
        })
        .unwrap_or_default();
    let genres = data
        .get("genres")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(|it| it.get("description").and_then(Value::as_str))
                .collect::<Vec<_>>()
                .join(", ")
        })
        .unwrap_or_default();
    let year = data
        .get("release_date")
        .and_then(|r| r.get("date"))
        .and_then(Value::as_str)
        .unwrap_or("");
    let raw_desc = data
        .get("short_description")
        .and_then(Value::as_str)
        .unwrap_or("");
    let desc = html_unescape_basic(&HTML_TAG_RE.replace_all(raw_desc, ""));

    game.insert("developer".into(), Value::String(devs));
    game.insert("publisher".into(), Value::String(pubs));
    game.insert("genre".into(), Value::String(genres));
    game.insert("year".into(), Value::String(year.to_owned()));
    game.insert("description".into(), Value::String(desc));

    let media = data_dir.join("media").join("steam").join(&app_id);
    let cover_url = format!(
        "https://cdn.cloudflare.steamstatic.com/steam/apps/{app_id}/library_600x900_2x.jpg"
    );
    if let Ok(path) = download_file_sync(
        &cover_url,
        &media.join("cover.jpg"),
        &["image/"],
        32 * 1024 * 1024,
        15,
        &[],
    ) {
        game.insert(
            "cover".into(),
            Value::String(path.to_string_lossy().into_owned()),
        );
    }
    if let Some(header) = data.get("header_image").and_then(Value::as_str)
        && !header.is_empty()
        && let Ok(path) = download_file_sync(
            header,
            &media.join("background.jpg"),
            &["image/"],
            32 * 1024 * 1024,
            15,
            &[],
        )
    {
        game.insert(
            "background".into(),
            Value::String(path.to_string_lossy().into_owned()),
        );
    }
    Ok(())
}

pub fn download_steam_trailer(
    game: &mut Map<String, Value>,
    media_root: &Path,
) -> Result<String, String> {
    let app_id = game
        .get("steam_app_id")
        .and_then(|v| match v {
            Value::String(s) => Some(s.clone()),
            Value::Number(n) => Some(n.to_string()),
            _ => None,
        })
        .unwrap_or_default();
    if app_id.is_empty() || !app_id.bytes().all(|b| b.is_ascii_digit()) {
        return Err("Steam App ID required for trailer download.".to_owned());
    }
    let url = format!("https://store.steampowered.com/api/appdetails?appids={app_id}");
    let payload = http_get_json(
        &url,
        &[("User-Agent", "OpenBox/1".to_owned())],
        20,
        4 * 1024 * 1024,
    )?;
    let rec = payload.get(&app_id).and_then(Value::as_object);
    if !rec
        .and_then(|r| r.get("success"))
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        return Err("Steam did not return app details.".to_owned());
    }
    let movies = rec
        .and_then(|r| r.get("data"))
        .and_then(|d| d.get("movies"))
        .and_then(Value::as_array)
        .filter(|a| !a.is_empty())
        .ok_or_else(|| "Steam returned no trailers for this game.".to_owned())?;
    let movie = &movies[0];
    let mp4 = movie
        .get("mp4")
        .and_then(Value::as_object)
        .and_then(|m| m.values().filter_map(Value::as_str).find(|s| !s.is_empty()))
        .or_else(|| {
            movie
                .get("webm")
                .and_then(|w| w.get("max"))
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
        })
        .ok_or_else(|| "Steam trailer URL was missing.".to_owned())?;
    let dest = media_root.join("steam").join(&app_id).join("trailer.mp4");
    let saved = download_bytes_sync(mp4, &dest)?;
    game.insert("video_trailer".into(), Value::String(saved.clone()));
    Ok(saved)
}

pub fn download_gog_media(game: &mut Map<String, Value>, media_root: &Path) -> Result<(), String> {
    let app_id = game
        .get("heroic_app_id")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_owned();
    if app_id.is_empty() {
        return Err("Heroic/GOG app id required.".to_owned());
    }
    let slug = app_id.replace("gog_", "");
    let url = format!(
        "https://embed.gog.com/games/ajax/filtered?mediaType=game&search={}",
        quote_path(&slug)
    );
    let payload = http_get_json(
        &url,
        &[("User-Agent", "OpenBox/1".to_owned())],
        20,
        4 * 1024 * 1024,
    )?;
    let products = payload
        .get("products")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let product = products
        .iter()
        .find(|it| {
            let id_s = it
                .get("id")
                .map(|v| match v {
                    Value::String(s) => s.clone(),
                    other => other.to_string(),
                })
                .unwrap_or_default();
            let pslug = it.get("slug").and_then(Value::as_str).unwrap_or("");
            id_s == slug || pslug.contains(&slug)
        })
        .ok_or_else(|| "GOG metadata lookup returned no product.".to_owned())?;
    let root = media_root.join("gog").join(&slug);
    if let Some(img) = product
        .get("img")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        let img_url = if img.starts_with("//") {
            format!("https:{img}")
        } else {
            img.to_owned()
        };
        let p = download_bytes_sync(&img_url, &root.join("cover.jpg"))?;
        game.insert("cover".into(), Value::String(p));
    }
    if let Some(bg) = product
        .get("background")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        let bg_url = if bg.starts_with("//") {
            format!("https:{bg}")
        } else {
            bg.to_owned()
        };
        let p = download_bytes_sync(&bg_url, &root.join("background.jpg"))?;
        game.insert("background".into(), Value::String(p));
    }
    Ok(())
}

pub fn bezel_repo_for(platform: &str) -> Option<&'static str> {
    match platform {
        "NES" => Some("thebezelproject/bezelproject-NintendoEntertainmentSystem"),
        "SNES" => Some("thebezelproject/bezelproject-SuperNintendoEntertainmentSystem"),
        "Nintendo 64" => Some("thebezelproject/bezelproject-Nintendo64"),
        "Game Boy Advance" => Some("thebezelproject/bezelproject-GameBoyAdvance"),
        "Sega Genesis" => Some("thebezelproject/bezelproject-SegaMegadrive"),
        "PlayStation" => Some("thebezelproject/bezelproject-SonyPlayStation"),
        "Arcade" => Some("thebezelproject/bezelproject-MAME"),
        _ => None,
    }
}

pub fn download_bezel(platform: &str, dest_dir: &Path) -> Result<String, String> {
    let Some(repo) = bezel_repo_for(platform) else {
        return Err(format!("No Bezel Project mapping for {platform}."));
    };
    fs::create_dir_all(dest_dir).map_err(|e| e.to_string())?;
    let safe_plat = platform.replace(' ', "_");
    let archive = dest_dir.join(format!("{safe_plat}-bezels.zip"));
    let url = format!("https://codeload.github.com/{repo}/zip/refs/heads/master");
    download_file_sync(&url, &archive, &[], 512 * 1024 * 1024, 120, &[])?;
    let extract_to = dest_dir.join(&safe_plat);
    if fs::symlink_metadata(&extract_to)
        .map(|m| m.file_type().is_symlink())
        .unwrap_or(false)
    {
        return Err("Bezel extraction destination may not be a symlink.".to_owned());
    }
    let staging = dest_dir.join(format!(".{safe_plat}.extracting"));
    let _ = fs::remove_dir_all(&staging);
    fs::create_dir_all(&staging).map_err(|e| e.to_string())?;
    let res = File::open(&archive)
        .map_err(|e| e.to_string())
        .and_then(|f| safe_zip_extract(f, &staging, ArchiveLimits::default()));
    if let Err(err) = res {
        let _ = fs::remove_dir_all(&staging);
        return Err(err);
    }
    if extract_to.exists() {
        let _ = fs::remove_dir_all(&extract_to);
    }
    if let Err(err) = fs::rename(&staging, &extract_to) {
        let _ = fs::remove_dir_all(&staging);
        return Err(err.to_string());
    }
    Ok(extract_to.to_string_lossy().into_owned())
}

pub fn capture_screenshot(dest_path: &Path, window_hint: &str) -> Result<String, String> {
    if let Some(p) = dest_path.parent() {
        fs::create_dir_all(p).map_err(|e| e.to_string())?;
    }
    let dest_s = dest_path.to_string_lossy().into_owned();
    let window = !window_hint.is_empty();
    let mut commands: Vec<Vec<String>> = Vec::new();
    #[cfg(target_os = "macos")]
    {
        if which("screencapture").is_some() {
            commands.push(vec!["screencapture".into(), "-x".into(), dest_s.clone()]);
        }
    }
    if which("gnome-screenshot").is_some() {
        if window {
            commands.push(vec![
                "gnome-screenshot".into(),
                "-w".into(),
                "-f".into(),
                dest_s.clone(),
            ]);
        } else {
            commands.push(vec!["gnome-screenshot".into(), "-f".into(), dest_s.clone()]);
        }
    }
    if which("spectacle").is_some() {
        commands.push(vec![
            "spectacle".into(),
            if window { "-a" } else { "-b" }.into(),
            "-n".into(),
            "-o".into(),
            dest_s.clone(),
        ]);
    }
    if which("scrot").is_some() {
        if window {
            commands.push(vec!["scrot".into(), "-u".into(), dest_s.clone()]);
        } else {
            commands.push(vec!["scrot".into(), dest_s.clone()]);
        }
    }
    if which("import").is_some() {
        commands.push(vec![
            "import".into(),
            "-window".into(),
            "root".into(),
            dest_s.clone(),
        ]);
    }
    let mut last_err = None;
    for cmd in commands {
        match Command::new(&cmd[0]).args(&cmd[1..]).output() {
            Ok(out) if out.status.success() && dest_path.is_file() => return Ok(dest_s),
            Ok(out) => {
                last_err = Some(String::from_utf8_lossy(&out.stderr).trim().to_owned());
            }
            Err(e) => {
                last_err = Some(e.to_string());
            }
        }
    }
    Err(last_err.filter(|s| !s.is_empty()).unwrap_or_else(|| {
        "No screenshot tool found (gnome-screenshot, spectacle, scrot, or ImageMagick import)."
            .to_owned()
    }))
}

pub fn apply_media_pack(state: &mut Value, pack_id: &str) -> Result<Value, String> {
    let packs = [
        json!({
            "id": "clear-logos-default",
            "name": "Platform Clear Logos",
            "description": "High-contrast platform logos for sidebar and Big Box hybrid views.",
            "kinds": ["platform_logo"],
        }),
        json!({
            "id": "controller-xbox",
            "name": "Xbox Controller Prompts",
            "description": "Xbox-style button prompts for Big Box status hints.",
            "kinds": ["controller_art"],
            "mapping_hint": "A Play · B Back · X Favorite · Y Menu",
        }),
        json!({
            "id": "controller-playstation",
            "name": "PlayStation Controller Prompts",
            "description": "PlayStation-style button prompts for Big Box status hints.",
            "kinds": ["controller_art"],
            "mapping_hint": "Cross Play · Circle Back · Square Favorite · Triangle Menu",
        }),
        json!({
            "id": "controller-nintendo",
            "name": "Nintendo Controller Prompts",
            "description": "Nintendo-style button prompts for Big Box status hints.",
            "kinds": ["controller_art"],
            "mapping_hint": "A Play · B Back · Y Favorite · X Menu",
        }),
    ];
    let pack = packs
        .into_iter()
        .find(|p| p.get("id").and_then(Value::as_str) == Some(pack_id))
        .ok_or_else(|| "Unknown media pack.".to_owned())?;
    if let Some(obj) = state.as_object_mut() {
        let settings = obj
            .entry("settings".to_owned())
            .or_insert_with(|| Value::Object(Map::new()));
        if let Some(sm) = settings.as_object_mut() {
            let active = sm
                .entry("active_media_packs".to_owned())
                .or_insert_with(|| Value::Array(Vec::new()));
            if let Some(arr) = active.as_array_mut()
                && !arr.iter().any(|v| v.as_str() == Some(pack_id))
            {
                arr.push(Value::String(pack_id.to_owned()));
            }
            if pack
                .get("kinds")
                .and_then(Value::as_array)
                .is_some_and(|k| k.iter().any(|v| v.as_str() == Some("controller_art")))
            {
                sm.insert(
                    "controller_prompt_pack".into(),
                    Value::String(pack_id.to_owned()),
                );
                sm.insert(
                    "controller_prompt_hint".into(),
                    Value::String(
                        pack.get("mapping_hint")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_owned(),
                    ),
                );
            }
        }
    }
    Ok(pack)
}
