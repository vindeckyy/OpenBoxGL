//! Metadata and media provider capability plugins (`pkg/parity/parity_plugin_providers.py`).

use std::fs;
use std::net::{IpAddr, ToSocketAddrs};
use std::path::Path;
use std::sync::LazyLock;

use openbox_io::{DownloadOptions, download_file, is_public_ip};
use openbox_metadata::http::block_on_io;
use openbox_metadata::media::enqueue_media_job;
use openbox_metadata::providers::{APPLY_FIELDS, apply_candidate};
use openbox_platform::{contained_path, expand_user};
use regex::Regex;
use serde_json::{Map, Value, json};
use sha1::{Digest, Sha1};
use url::Url;

use crate::host::{capability_media_kinds, run_plugin_hook};
use crate::storefront::{plugin_manifest, require_network_grant, require_plugin_capability};

pub const MAX_CANDIDATES: usize = 20;
pub const MAX_MEDIA_URLS_PER_KIND: usize = 10;
pub const MAX_URL_LENGTH: usize = 2000;
pub const MAX_CANDIDATE_TEXT: usize = 200;
pub const MAX_FIELD_TEXT: usize = 4000;
pub const MAX_CANDIDATE_ID: usize = 160;
pub const MAX_QUEUE_MEDIA: usize = 20;

const IMAGE_MAX_BYTES: usize = 20 * 1024 * 1024;
const MANUAL_MAX_BYTES: usize = 50 * 1024 * 1024;
const MUSIC_MAX_BYTES: usize = 30 * 1024 * 1024;
const VIDEO_MAX_BYTES: usize = 100 * 1024 * 1024;

const VIDEO_KINDS: &[&str] = &[
    "video",
    "video_snap",
    "video_theme",
    "video_trailer",
    "video_recording",
];
const PUBLIC_GAME_FIELDS: &[&str] = &[
    "game_id",
    "name",
    "platform",
    "genre",
    "year",
    "developer",
    "publisher",
    "series",
    "description",
    "esrb",
    "region",
    "sort_title",
    "source",
    "steam_app_id",
    "gog_id",
    "igdb_id",
    "ra_game_id",
    "progress",
    "favorite",
    "playtime_seconds",
    "last_played",
];

static SLUG_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[^a-z0-9._-]+").unwrap());

fn slug(value: &str, limit: usize) -> String {
    let lowered = value.trim().to_lowercase();
    let replaced = SLUG_RE.replace_all(&lowered, "-");
    let trimmed = replaced.trim_matches('-');
    let out: String = trimmed.chars().take(limit).collect();
    if out.is_empty() {
        "media".to_owned()
    } else {
        out
    }
}

fn text_val(val: Option<&Value>, limit: usize) -> String {
    match val {
        Some(Value::String(s)) => s.trim().chars().take(limit).collect(),
        Some(Value::Number(n)) => n.to_string().chars().take(limit).collect(),
        _ => String::new(),
    }
}

fn public_game(game: &Value) -> Value {
    let Some(obj) = game.as_object() else {
        return json!({});
    };
    let mut projected = Map::new();
    for key in PUBLIC_GAME_FIELDS {
        let Some(val) = obj.get(*key) else {
            continue;
        };
        match val {
            Value::Null => continue,
            Value::String(s) if s.is_empty() => continue,
            Value::Bool(b) => {
                projected.insert((*key).to_owned(), Value::Bool(*b));
            }
            Value::String(_) | Value::Number(_) => {
                projected.insert((*key).to_owned(), Value::String(text_val(Some(val), 500)));
            }
            _ => {}
        }
    }
    if !projected.contains_key("game_id")
        && let Some(id_val) = obj.get("id")
        && (id_val.is_string() || id_val.is_number())
    {
        projected.insert(
            "game_id".to_owned(),
            Value::String(text_val(Some(id_val), MAX_CANDIDATE_ID)),
        );
    }
    Value::Object(projected)
}

fn clean_media_kind(kind: &str) -> Option<String> {
    let trimmed = kind.trim();
    if capability_media_kinds().contains(trimmed) {
        Some(trimmed.to_owned())
    } else {
        None
    }
}

pub fn safe_media_url(raw_url: &str, resolve_dns: bool) -> Result<String, String> {
    let value = raw_url.trim();
    if value.is_empty() || value.len() > MAX_URL_LENGTH {
        return Err("Media URL is empty or too long.".to_owned());
    }
    let parsed = Url::parse(value).map_err(|_| "Media URLs must use HTTPS.".to_owned())?;
    if parsed.scheme() != "https" {
        return Err("Media URLs must use HTTPS.".to_owned());
    }
    if !parsed.username().is_empty() || parsed.password().is_some() {
        return Err("Media URLs may not carry credentials.".to_owned());
    }
    let Some(raw_host) = parsed.host_str() else {
        return Err("Media URL needs a host.".to_owned());
    };
    let host = raw_host
        .trim()
        .trim_start_matches('[')
        .trim_end_matches(']')
        .to_lowercase();
    let host = host.trim_end_matches('.');
    if host == "localhost" || host.ends_with(".localhost") {
        return Err("Media hosts cannot be localhost.".to_owned());
    }
    let host_no_zone = host.split('%').next().unwrap_or(host);
    if let Ok(ip) = host_no_zone.parse::<IpAddr>() {
        if !is_public_ip(ip) {
            return Err("Media hosts must be public addresses.".to_owned());
        }
        return Ok(value.to_owned());
    }
    if !resolve_dns {
        return Ok(value.to_owned());
    }
    let port = parsed.port_or_known_default().unwrap_or(443);
    if let Ok(iter) = (host, port).to_socket_addrs() {
        for sa in iter {
            if !is_public_ip(sa.ip()) {
                return Err("Media hosts must not resolve to private addresses.".to_owned());
            }
        }
    }
    Ok(value.to_owned())
}

fn download_limits(kind: &str) -> (usize, &'static [&'static str], &'static str) {
    if VIDEO_KINDS.contains(&kind) {
        return (VIDEO_MAX_BYTES, &["video/"], ".mp4");
    }
    if kind == "music" {
        return (MUSIC_MAX_BYTES, &["audio/", "application/ogg"], ".mp3");
    }
    if kind == "manual" {
        return (MANUAL_MAX_BYTES, &["application/pdf"], ".pdf");
    }
    (IMAGE_MAX_BYTES, &["image/"], ".bin")
}

pub fn download_plugin_media(
    plugin_id: &str,
    game: &Value,
    kind: &str,
    candidate: &Value,
    destination_root: &Path,
    timeout: u64,
) -> Result<String, String> {
    let Some(clean_kind) = clean_media_kind(kind) else {
        return Err(format!("Unsupported media kind: '{kind}'."));
    };
    let raw_url = candidate
        .as_object()
        .and_then(|o| o.get("url"))
        .and_then(Value::as_str)
        .or_else(|| candidate.as_str())
        .ok_or_else(|| "Media URL must be text.".to_owned())?;
    let safe = safe_media_url(raw_url, true)?;
    let root = expand_user(destination_root);
    fs::create_dir_all(&root).map_err(|e| e.to_string())?;
    let digest = &hex::encode(Sha1::digest(safe.as_bytes()))[..12];
    let game_name = text_val(game.get("name"), 60);
    let prefix = if game_name.is_empty() {
        plugin_id
    } else {
        &game_name
    };
    let (max_bytes, expected_types, ext) = download_limits(&clean_kind);
    let file_name = format!("{}-{}-{}{}", slug(prefix, 60), clean_kind, digest, ext);
    let dest = contained_path(&root, &[file_name.as_str()]).map_err(|e| e.to_string())?;
    let opts = DownloadOptions {
        max_bytes,
        timeout_secs: timeout.max(1),
        expected_types,
        ..DownloadOptions::default()
    };
    block_on_io(async { download_file(&safe, &dest, &opts).await }).map_err(|e| e.to_string())?;
    let verified = contained_path(&root, &[file_name.as_str()]).map_err(|e| e.to_string())?;
    Ok(verified.to_string_lossy().into_owned())
}

fn clean_fields(raw: Option<&Value>) -> Map<String, Value> {
    let Some(obj) = raw.and_then(Value::as_object) else {
        return Map::new();
    };
    let mut fields = Map::new();
    for key in APPLY_FIELDS {
        let Some(val) = obj.get(*key) else {
            continue;
        };
        match val {
            Value::String(s) => {
                let t: String = s.trim().chars().take(MAX_FIELD_TEXT).collect();
                if !t.is_empty() {
                    fields.insert((*key).to_owned(), Value::String(t));
                }
            }
            Value::Number(_) => {
                fields.insert((*key).to_owned(), val.clone());
            }
            _ => {}
        }
    }
    fields
}

fn clean_candidate(
    raw: &Map<String, Value>,
    manifest: &Map<String, Value>,
    capability: &Map<String, Value>,
) -> Option<Value> {
    let cid = match raw.get("id") {
        Some(Value::String(s)) if !s.trim().is_empty() => {
            s.trim().chars().take(MAX_CANDIDATE_ID).collect::<String>()
        }
        Some(Value::Number(n)) => n
            .as_i64()
            .or_else(|| n.as_f64().map(|f| f as i64))?
            .to_string(),
        _ => return None,
    };
    let name = text_val(raw.get("name"), MAX_CANDIDATE_TEXT);
    if name.is_empty() {
        return None;
    }
    let fields = clean_fields(raw.get("fields"));
    let mut media = Map::new();
    if let Some(rm) = raw.get("media").and_then(Value::as_object) {
        for (k, v) in rm {
            let Some(kind) = clean_media_kind(k) else {
                continue;
            };
            let items: Vec<&Value> = match v {
                Value::Array(a) => a.iter().collect(),
                other => vec![other],
            };
            let mut urls = Vec::new();
            for item in items {
                if let Some(s) = item.as_str()
                    && let Ok(safe) = safe_media_url(s, false)
                {
                    urls.push(Value::String(safe));
                    if urls.len() >= MAX_MEDIA_URLS_PER_KIND {
                        break;
                    }
                }
            }
            if !urls.is_empty() {
                media.insert(kind, Value::Array(urls));
            }
        }
    }
    let mid = manifest.get("id").and_then(Value::as_str).unwrap_or("");
    let source = capability
        .get("name")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .or_else(|| {
            manifest
                .get("name")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
        })
        .unwrap_or(mid);
    let platform = text_val(
        raw.get("platform")
            .or_else(|| fields.get("platform"))
            .or_else(|| capability.get("platform")),
        40,
    );
    let year = text_val(raw.get("year").or_else(|| fields.get("year")), 12);
    Some(json!({
        "id": cid,
        "plugin_id": mid,
        "source": source,
        "provider": source,
        "name": name,
        "platform": platform,
        "year": year,
        "fields": fields,
        "media": media,
    }))
}

#[allow(clippy::too_many_arguments)]
pub fn plugin_metadata_search(
    plugins_dir: &Path,
    plugin_id: &str,
    game: &Value,
    limit: usize,
    timeout: Option<u64>,
    state: Option<&Value>,
    metadata_db: Option<&Path>,
    app_root: Option<&Path>,
) -> Result<Vec<Value>, String> {
    let manifest = plugin_manifest(plugins_dir, plugin_id)?;
    let capability = require_plugin_capability(&manifest, "metadata")?;
    require_network_grant(&manifest, plugins_dir)?;
    let cap = limit.clamp(1, MAX_CANDIDATES);
    let payload = json!({
        "action": "search",
        "game": public_game(game),
        "limit": cap,
    });
    let id = manifest["id"].as_str().unwrap_or(plugin_id);
    let (res_opt, err) = run_plugin_hook(
        plugins_dir,
        id,
        "metadata",
        &payload,
        timeout,
        state,
        metadata_db,
        app_root,
    );
    if !err.is_empty() {
        tracing::warn!("Plugin {} metadata search failed: {}", id, err);
        return Ok(Vec::new());
    }
    let Some(res) = res_opt else {
        return Ok(Vec::new());
    };
    let Some(rows) = res.get("candidates").and_then(Value::as_array) else {
        return Ok(Vec::new());
    };
    let mut out = Vec::new();
    for raw in rows.iter().take(MAX_CANDIDATES) {
        if let Some(obj) = raw.as_object()
            && let Some(cand) = clean_candidate(obj, &manifest, &capability)
        {
            out.push(cand);
        }
        if out.len() >= cap {
            break;
        }
    }
    Ok(out)
}

#[allow(clippy::too_many_arguments)]
pub fn plugin_metadata_apply(
    plugins_dir: &Path,
    plugin_id: &str,
    game: &mut Map<String, Value>,
    candidate_id: &str,
    overwrite: bool,
    queue_path: &Path,
    timeout: Option<u64>,
    state: Option<&Value>,
    metadata_db: Option<&Path>,
    app_root: Option<&Path>,
) -> Result<(), String> {
    let cid: String = candidate_id.trim().chars().take(MAX_CANDIDATE_ID).collect();
    if cid.is_empty() {
        return Err("A candidate id is required.".to_owned());
    }
    let manifest = plugin_manifest(plugins_dir, plugin_id)?;
    let _cap = require_plugin_capability(&manifest, "metadata")?;
    require_network_grant(&manifest, plugins_dir)?;
    let id = manifest["id"].as_str().unwrap_or(plugin_id);
    let payload = json!({
        "action": "apply",
        "game": public_game(&Value::Object(game.clone())),
        "candidate_id": cid,
    });
    let (res_opt, err) = run_plugin_hook(
        plugins_dir,
        id,
        "metadata",
        &payload,
        timeout,
        state,
        metadata_db,
        app_root,
    );
    if !err.is_empty() {
        return Err(format!("Plugin metadata apply failed: {err}"));
    }
    let Some(res) = res_opt else {
        return Err("Plugin metadata apply must return a JSON object.".to_owned());
    };
    let fields = clean_fields(res.get("fields"));
    let cand = json!({
        "source": format!("plugin:{id}"),
        "fields": fields,
    });
    apply_candidate(game, &cand, overwrite, None)?;

    // Queue validated media URLs
    let game_id = game
        .get("game_id")
        .or_else(|| game.get("id"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .chars()
        .take(MAX_CANDIDATE_ID)
        .collect::<String>();
    if !game_id.is_empty()
        && let Some(media) = res.get("media").and_then(Value::as_object)
    {
        let mut queued = 0usize;
        'outer: for (k, v) in media {
            let Some(kind) = clean_media_kind(k) else {
                continue;
            };
            let items: Vec<&Value> = match v {
                Value::Array(a) => a.iter().collect(),
                other => vec![other],
            };
            for item in items {
                let Some(raw_url) = item.as_str() else {
                    continue;
                };
                let Ok(url) = safe_media_url(raw_url, false) else {
                    continue;
                };
                let job = json!({
                    "game_id": game_id,
                    "source": format!("plugin:{id}"),
                    "kind": kind,
                    "url": url,
                    "overwrite": overwrite,
                });
                if enqueue_media_job(queue_path, &job).is_err() {
                    break 'outer;
                }
                queued += 1;
                if queued >= MAX_QUEUE_MEDIA {
                    break 'outer;
                }
            }
        }
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
pub fn plugin_media_search(
    plugins_dir: &Path,
    plugin_id: &str,
    game: &Value,
    kind: &str,
    limit: usize,
    timeout: Option<u64>,
    state: Option<&Value>,
    metadata_db: Option<&Path>,
    app_root: Option<&Path>,
) -> Result<Vec<Value>, String> {
    let manifest = plugin_manifest(plugins_dir, plugin_id)?;
    let capability = require_plugin_capability(&manifest, "media")?;
    require_network_grant(&manifest, plugins_dir)?;
    let Some(clean_kind) = clean_media_kind(kind) else {
        return Err(format!("Unsupported media kind: '{kind}'."));
    };
    if let Some(declared) = capability.get("media_kinds").and_then(Value::as_array)
        && !declared.is_empty()
        && !declared
            .iter()
            .any(|v| v.as_str() == Some(clean_kind.as_str()))
    {
        return Err(format!(
            "Plugin does not provide the {clean_kind} media kind."
        ));
    }
    let cap = limit.clamp(1, MAX_CANDIDATES);
    let id = manifest["id"].as_str().unwrap_or(plugin_id);
    let payload = json!({
        "action": "search",
        "game": public_game(game),
        "kind": clean_kind,
        "limit": cap,
    });
    let (res_opt, err) = run_plugin_hook(
        plugins_dir,
        id,
        "media",
        &payload,
        timeout,
        state,
        metadata_db,
        app_root,
    );
    if !err.is_empty() {
        tracing::warn!("Plugin {} media search failed: {}", id, err);
        return Ok(Vec::new());
    }
    let Some(res) = res_opt else {
        return Ok(Vec::new());
    };
    let Some(rows) = res.get("candidates").and_then(Value::as_array) else {
        return Ok(Vec::new());
    };
    let mut out = Vec::new();
    for raw in rows.iter().take(cap) {
        let Some(obj) = raw.as_object() else {
            continue;
        };
        let Some(raw_url) = obj.get("url").and_then(Value::as_str) else {
            continue;
        };
        let Ok(url) = safe_media_url(raw_url, false) else {
            continue;
        };
        let rkind = obj
            .get("kind")
            .and_then(Value::as_str)
            .and_then(clean_media_kind)
            .unwrap_or_else(|| clean_kind.clone());
        let mut item = Map::new();
        item.insert("url".to_owned(), Value::String(url));
        item.insert("kind".to_owned(), Value::String(rkind));
        for key in ["title", "width", "height"] {
            if let Some(val) = obj.get(key) {
                match val {
                    Value::String(s) => {
                        item.insert(
                            key.to_owned(),
                            Value::String(s.trim().chars().take(MAX_CANDIDATE_TEXT).collect()),
                        );
                    }
                    Value::Number(_) => {
                        item.insert(key.to_owned(), val.clone());
                    }
                    _ => {}
                }
            }
        }
        out.push(Value::Object(item));
    }
    Ok(out)
}
