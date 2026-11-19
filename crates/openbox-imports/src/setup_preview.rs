//! Setup Center preview scan, classification, retention, commit, and compatibility report
//! (`pkg/parity/parity_setup_preview.py` + `pkg/parity/parity_compat.py`).

use std::collections::{BTreeSet, HashMap, HashSet};
use std::fs::{self, File};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use base64::Engine as _;
use openbox_core::{ApiError, KNOWN_SETTINGS};
use openbox_emulators::faugus::scan_faugus_games;
use openbox_emulators::{
    Adapter, detect_adapter_prefix, find_adapter, load_adapters, platform_options_for_extension,
};
use openbox_io::{atomic_write_bytes, atomic_write_text};
use openbox_platform::{
    contained_path, default_data_dir, expand_user, flatpak_app_installed,
    resolve_native_executable, safe_identifier, which,
};
use openbox_runtime::AppState;
const ACTIVE_STATES: &[&str] = &["queued", "running", "cancelling"];
use regex::Regex;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use crate::arcade_import::import_arcade;

use crate::bulk_edit::import_xbox360_folder;
use crate::exclusions::{exclusion_set, filter_imported};
use crate::folder_import::{
    generate_m3u, import_multi_platform, import_rpcs3_hdd, import_scummvm, import_vita3k,
    platform_by_extension,
};
use crate::identity::{cross_source_identity, normalize_path_identity, source_identities};
use crate::importers::{import_heroic, import_lutris, import_steam};
use crate::state_imports::merge_imported_game;

pub const MAX_PREVIEWS: usize = 10;
pub const PREVIEW_TTL_HOURS: i64 = 24;
pub const MAX_ENTRIES: usize = 100_000;
pub const MAX_DECISION_BATCH: usize = 200;
pub const DEFAULT_ITEMS_LIMIT: usize = 50;
pub const MAX_ITEMS_LIMIT: usize = 200;
pub const REPORT_FORMAT: &str = "openbox-compat-report";
pub const REPORT_VERSION: u32 = 1;
pub const READINESS_LIBRARY_LIMIT: usize = 20_000;

static TOKEN_DECL_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(--[A-Za-z0-9-]+)\s*:").expect("valid token regex"));
static ROOT_BLOCK_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?s):root\s*\{(.*?)\}").expect("valid root block regex"));

fn preview_path(preview_id: &str, data_dir: &Path) -> Result<PathBuf, ApiError> {
    let name = safe_identifier(preview_id, "preview_id")
        .map_err(|e| ApiError::bad_request(e.to_string()))?;
    let fname = format!("{name}.json");
    contained_path(&data_dir.join("previews"), &[fname.as_str()])
        .map_err(|e| ApiError::bad_request(e.to_string()))
}

fn library_sig(app: &AppState) -> String {
    match app.store.signature() {
        None => "missing".to_owned(),
        Some(sig) => format!("{}:{}:{}", sig.mtime_ns, sig.size, sig.ino),
    }
}

fn folder_fingerprint(path: &str) -> String {
    let target = expand_user(Path::new(path));
    if !target.is_dir() {
        return format!("missing:{path}");
    }
    let mut hasher = Sha256::new();
    let resolved = fs::canonicalize(&target).unwrap_or_else(|_| target.clone());
    hasher.update(resolved.to_string_lossy().as_bytes());
    if let Ok(meta) = fs::metadata(&target) {
        let mtime = meta
            .modified()
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        hasher.update(format!("{}:{mtime}", meta.len()).as_bytes());
    }
    hex::encode(hasher.finalize())
}

fn file_fingerprint(path: &Path) -> String {
    match fs::metadata(path) {
        Ok(meta) => {
            let resolved = fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
            let mtime = meta
                .modified()
                .ok()
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_nanos())
                .unwrap_or(0);
            format!("{}:{}:{mtime}", resolved.display(), meta.len())
        }
        Err(_) => format!("missing:{}", path.display()),
    }
}

fn expires_at_iso(hours: i64) -> String {
    let target = jiff::Timestamp::now()
        .checked_add(jiff::Span::new().hours(hours))
        .unwrap_or_else(|_| jiff::Timestamp::now());
    target.to_string()
}

fn is_expired(expires_at: &str) -> bool {
    if expires_at.is_empty() {
        return false;
    }
    let Ok(parsed) = expires_at.parse::<jiff::Timestamp>() else {
        return false;
    };
    parsed < jiff::Timestamp::now()
}

fn candidate_id(source_type: &str, source_id: &str, identity: &str, path: &str) -> String {
    let payload = format!("{source_type}|{source_id}|{identity}|{path}");
    hex::encode(Sha256::digest(payload.as_bytes()))[..24].to_owned()
}

fn identity_for_game(game: &Value) -> String {
    if let Some(first) = source_identities(game).into_iter().next() {
        return first;
    }
    if let Some(sid) = game
        .get("scummvm_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        return format!("scummvm:{sid}");
    }
    if let Some(p) = game
        .get("path")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        return format!("path:{}", normalize_path_identity(p));
    }
    format!(
        "name:{}:{}",
        game.get("name").and_then(Value::as_str).unwrap_or(""),
        game.get("platform").and_then(Value::as_str).unwrap_or("")
    )
}

fn adapter_choice(adapter: &Adapter) -> Value {
    json!({
        "adapter_id": adapter.adapter_id,
        "emulator_id": adapter.emulator_id,
        "label": adapter.label,
        "recommended": adapter.recommended,
        "flatpak_app_id": adapter.flatpak_app_id,
    })
}

pub fn emulator_choices_for_platform(platform: &str, adapters: &[Adapter]) -> Vec<Value> {
    if platform.is_empty() {
        return Vec::new();
    }
    adapters
        .iter()
        .filter(|a| a.platform == platform)
        .map(adapter_choice)
        .collect()
}

fn adapter_installed(adapter: &Adapter) -> bool {
    if detect_adapter_prefix(adapter).is_empty() {
        return false;
    }
    if let Some(ref native) = adapter.native_exe
        && !native.is_empty()
        && resolve_native_executable(native).is_some()
    {
        return true;
    }
    for pat in &adapter.executable_patterns {
        if which(pat).is_some() {
            return true;
        }
    }
    if let Some(ref fp) = adapter.flatpak_app_id
        && !fp.is_empty()
        && flatpak_app_installed(fp)
    {
        return true;
    }
    false
}

pub fn classify_emulator_readiness(game: &Value, adapters: &[Adapter]) -> &'static str {
    let platform = game.get("platform").and_then(Value::as_str).unwrap_or("");
    let custom_launch = game
        .get("launch")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    let adapter_id = game
        .get("emulator_adapter_id")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    let emulator_id = game
        .get("emulator_id")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    let path = game.get("path").and_then(Value::as_str).unwrap_or("");
    let adapter = if !adapter_id.is_empty() || !emulator_id.is_empty() {
        find_adapter(adapters, adapter_id, emulator_id)
    } else {
        None
    };
    if !custom_launch.is_empty() && adapter_id.is_empty() {
        return "unknown";
    }
    if adapter.is_none() && custom_launch.is_empty() {
        let p = Path::new(path);
        let ext = p
            .extension()
            .and_then(|e| e.to_str())
            .map(|e| format!(".{}", e.to_lowercase()))
            .unwrap_or_default();
        if matches!(ext.as_str(), ".exe" | ".sh" | ".appimage") && p.is_file() {
            return "ready";
        }
        if adapters.iter().any(|a| a.platform == platform) {
            return "blocked";
        }
        return "unknown";
    }
    if let Some(ad) = adapter {
        if adapter_installed(ad) {
            "ready"
        } else {
            "warning"
        }
    } else {
        "unknown"
    }
}

fn game_has_media(game: &Value) -> bool {
    if let Some(cover) = game.get("cover").and_then(Value::as_str)
        && !cover.trim().is_empty()
        && Path::new(cover.trim()).is_file()
    {
        return true;
    }
    match game.get("screenshots") {
        Some(Value::String(s)) => !s.trim().is_empty() && Path::new(s.trim()).is_file(),
        Some(Value::Array(arr)) => arr.iter().any(|v| {
            v.as_str()
                .map(str::trim)
                .is_some_and(|s| !s.is_empty() && Path::new(s).is_file())
        }),
        _ => false,
    }
}

pub fn compute_summary(app: &AppState, state: &Value) -> Value {
    let adapters = load_adapters(&app.root_dir, Some(&app.data_dir));
    let games = state
        .get("games")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let mut ready = 0usize;
    let mut warning = 0usize;
    let mut blocked = 0usize;
    let mut unknown = 0usize;
    let mut missing_paths = 0usize;
    let mut duplicate_count = 0usize;
    let mut matched = 0usize;
    let mut media_gaps = 0usize;
    let mut seen_paths = HashSet::new();
    for game in games {
        match classify_emulator_readiness(game, &adapters) {
            "ready" => ready += 1,
            "warning" => warning += 1,
            "blocked" => blocked += 1,
            _ => unknown += 1,
        }
        let path = game.get("path").and_then(Value::as_str).unwrap_or("");
        if !path.is_empty() {
            if !Path::new(path).exists() {
                missing_paths += 1;
            }
            let norm = normalize_path_identity(path);
            if !norm.is_empty() && !seen_paths.insert(norm) {
                duplicate_count += 1;
            }
        }
        if game
            .get("launchbox_db_id")
            .is_some_and(|v| !v.is_null() && v.as_str() != Some(""))
        {
            matched += 1;
        }
        if !game_has_media(game) {
            media_gaps += 1;
        }
    }
    let active_operations = app
        .operations
        .list_jobs(None, MAX_PREVIEWS * 10, None, None, None)
        .get("jobs")
        .and_then(Value::as_array)
        .map(|jobs| {
            jobs.iter()
                .filter(|j| {
                    j.get("state")
                        .and_then(Value::as_str)
                        .is_some_and(|s| ACTIVE_STATES.contains(&s))
                })
                .count()
        })
        .unwrap_or(0);
    let library_count = games.len();
    let metadata_match_percent = if library_count > 0 {
        ((matched as f64 / library_count as f64) * 10_000.0).round() / 100.0
    } else {
        0.0
    };
    let next_action = if library_count == 0 {
        json!({"id": "add_sources", "label": "Add game sources", "step": 2})
    } else if active_operations > 0 {
        json!({"id": "health", "label": "Review active operations", "step": 1})
    } else if blocked > 0 || warning > 0 {
        json!({"id": "fix_launch", "label": "Fix launch readiness", "step": 5})
    } else if metadata_match_percent < 100.0 {
        json!({"id": "review_metadata", "label": "Review metadata matches", "step": 8})
    } else if media_gaps > 0 {
        json!({"id": "download_media", "label": "Download missing media", "step": 8})
    } else {
        json!({"id": "none", "label": "Setup complete", "step": 8})
    };
    json!({
        "library_count": library_count,
        "source_coverage": [{
            "source_id": "library",
            "label": "Library",
            "game_count": library_count,
            "coverage_percent": if library_count > 0 { 100.0 } else { 0.0 },
        }],
        "metadata_match_percent": metadata_match_percent,
        "media_gaps": media_gaps,
        "duplicate_count": duplicate_count,
        "missing_paths": missing_paths,
        "emulator_readiness": {
            "ready": ready,
            "warning": warning,
            "blocked": blocked,
            "unknown": unknown,
        },
        "active_operations": active_operations,
        "next_action": next_action,
    })
}

pub fn load_preview(
    app: &AppState,
    preview_id: &str,
    allow_expired: bool,
) -> Result<Value, ApiError> {
    let pid = preview_id.trim();
    if pid.is_empty() {
        return Err(ApiError::preview_not_found("preview_id is required."));
    }
    let path = preview_path(pid, &app.data_dir)?;
    if !path.is_file() {
        return Err(ApiError::preview_not_found("Preview not found."));
    }
    let text =
        fs::read_to_string(&path).map_err(|_| ApiError::preview_not_found("Preview not found."))?;
    let preview: Value = serde_json::from_str(&text)
        .map_err(|_| ApiError::preview_not_found("Preview not found."))?;
    if !allow_expired
        && is_expired(
            preview
                .get("expires_at")
                .and_then(Value::as_str)
                .unwrap_or(""),
        )
    {
        return Err(ApiError::preview_expired("Preview has expired."));
    }
    Ok(preview)
}

pub fn save_preview(app: &AppState, preview: &Value) -> Result<(), ApiError> {
    let pid = preview
        .get("preview_id")
        .and_then(Value::as_str)
        .ok_or_else(|| ApiError::bad_request("preview_id is required."))?;
    let path = preview_path(pid, &app.data_dir)?;
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    let text = serde_json::to_string(preview).unwrap_or_else(|_| "{}".to_owned());
    atomic_write_text(&path, &text, 0o600).map_err(|e| ApiError::bad_request(e.to_string()))?;
    Ok(())
}

pub fn create_preview_record(
    app: &AppState,
    sources: &[Value],
    options: &Value,
) -> Result<Value, ApiError> {
    let previews_dir = app.data_dir.join("previews");
    if previews_dir.is_dir() {
        let active = fs::read_dir(&previews_dir)
            .ok()
            .into_iter()
            .flatten()
            .filter_map(Result::ok)
            .filter(|e| {
                let p = e.path();
                p.extension().is_some_and(|x| x == "json")
                    && p.file_name().and_then(|n| n.to_str()) != Some("index.json")
            })
            .filter(|e| {
                fs::read_to_string(e.path())
                    .ok()
                    .and_then(|t| serde_json::from_str::<Value>(&t).ok())
                    .is_some_and(|v| {
                        !is_expired(v.get("expires_at").and_then(Value::as_str).unwrap_or(""))
                    })
            })
            .count();
        if active >= MAX_PREVIEWS {
            return Err(ApiError::preview_limit_exceeded(format!(
                "At most {MAX_PREVIEWS} active previews are allowed."
            )));
        }
    }
    let preview_id = uuid::Uuid::new_v4().simple().to_string();
    let pub_sources: Vec<Value> = sources
        .iter()
        .map(|s| {
            let stype = s.get("type").and_then(Value::as_str).unwrap_or("");
            let id = s
                .get("id")
                .or_else(|| s.get("path"))
                .and_then(Value::as_str)
                .unwrap_or("");
            let label = s
                .get("label")
                .or_else(|| s.get("type"))
                .and_then(Value::as_str)
                .unwrap_or("");
            json!({
                "type": stype,
                "id": id,
                "label": label,
                "path": s.get("path").cloned().unwrap_or(Value::Null),
            })
        })
        .collect();
    let preview = json!({
        "preview_id": preview_id,
        "revision": 1,
        "expires_at": expires_at_iso(PREVIEW_TTL_HOURS),
        "state": "queued",
        "scanned_entries": 0,
        "counts": {
            "additions": 0, "merges": 0, "duplicates": 0, "ambiguities": 0,
            "exclusions": 0, "unsupported": 0, "errors": 0,
        },
        "library_signature": library_sig(app),
        "job_id": Value::Null,
        "sources": pub_sources,
        "_request_sources": sources,
        "options": options,
        "source_fingerprints": {},
        "items": [],
        "decisions": {},
        "commit_tokens": {},
        "revalidated": false,
    });
    save_preview(app, &preview)?;
    Ok(preview)
}

pub fn preview_document(preview: &Value) -> Value {
    let counts = preview.get("counts").cloned().unwrap_or_else(|| json!({}));
    let scanned = preview
        .get("scanned_entries")
        .and_then(Value::as_u64)
        .unwrap_or(0);
    let pending = counts
        .get("ambiguities")
        .and_then(Value::as_u64)
        .unwrap_or(0)
        + counts
            .get("unsupported")
            .and_then(Value::as_u64)
            .unwrap_or(0);
    let message = if pending > 0 {
        format!("Found {scanned} games — {pending} need your pick")
    } else if scanned > 0 {
        format!("Found {scanned} games — review and commit")
    } else {
        "Preview ready — review items and commit".to_owned()
    };
    let mut doc = json!({
        "preview_id": preview["preview_id"],
        "revision": preview.get("revision").and_then(Value::as_i64).unwrap_or(1),
        "expires_at": preview.get("expires_at").cloned().unwrap_or(Value::Null),
        "state": preview.get("state").and_then(Value::as_str).unwrap_or("ready"),
        "scanned_entries": scanned,
        "counts": counts,
        "library_signature": preview.get("library_signature").cloned().unwrap_or(Value::Null),
        "job_id": preview.get("job_id").cloned().unwrap_or(Value::Null),
        "sources": preview.get("sources").cloned().unwrap_or_else(|| json!([])),
        "options": preview.get("options").cloned().unwrap_or_else(|| json!({})),
        "message": message,
    });
    if preview
        .get("revalidated")
        .and_then(Value::as_bool)
        .unwrap_or(false)
        && let Some(m) = doc.as_object_mut()
    {
        m.insert("revalidated".into(), Value::Bool(true));
    }
    doc
}

fn public_item(item: &Value) -> Value {
    json!({
        "candidate_id": item.get("candidate_id").cloned().unwrap_or(Value::Null),
        "group": item.get("group").cloned().unwrap_or(Value::Null),
        "source": item.get("source").cloned().unwrap_or_else(|| json!({})),
        "detected_title": item.get("detected_title").and_then(Value::as_str).unwrap_or(""),
        "detected_platform": item.get("detected_platform").cloned().unwrap_or(Value::Null),
        "intended_action": item.get("intended_action").and_then(Value::as_str).unwrap_or("review"),
        "existing_game_target": item.get("existing_game_target").cloned().unwrap_or(Value::Null),
        "warnings": item.get("warnings").cloned().unwrap_or_else(|| json!([])),
        "emulator_choices": item.get("emulator_choices").cloned().unwrap_or_else(|| json!([])),
        "platform_options": item.get("platform_options").cloned().unwrap_or_else(|| json!([])),
        "selected_emulator_id": item.get("selected_emulator_id").cloned().unwrap_or(Value::Null),
        "selected_adapter_id": item.get("selected_adapter_id").cloned().unwrap_or(Value::Null),
        "launch_setup": item.get("launch_setup").cloned().unwrap_or(Value::Null),
        "merge_diff": item.get("merge_diff").cloned().unwrap_or(Value::Null),
    })
}

fn encode_cursor(preview_id: &str, revision: i64, offset: usize) -> String {
    let raw = serde_json::to_vec(&json!({
        "preview_id": preview_id,
        "revision": revision,
        "offset": offset,
    }))
    .unwrap_or_default();
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(raw)
}

fn decode_cursor(cursor: &str, preview_id: &str, revision: i64) -> Result<usize, ApiError> {
    if cursor.is_empty() {
        return Ok(0);
    }
    let bytes = base64::engine::general_purpose::URL_SAFE_NO_PAD
        .decode(cursor.trim_end_matches('='))
        .map_err(|_| ApiError::preview_stale("Cursor is invalid or stale."))?;
    let val: Value = serde_json::from_slice(&bytes)
        .map_err(|_| ApiError::preview_stale("Cursor is invalid or stale."))?;
    if val.get("preview_id").and_then(Value::as_str) != Some(preview_id)
        || val.get("revision").and_then(Value::as_i64).unwrap_or(0) != revision
    {
        return Err(ApiError::preview_stale(
            "Cursor is bound to a different preview revision.",
        ));
    }
    Ok(val.get("offset").and_then(Value::as_u64).unwrap_or(0) as usize)
}

pub fn list_preview_items(
    app: &AppState,
    preview_id: &str,
    cursor: Option<&str>,
    limit: usize,
) -> Result<Value, ApiError> {
    let preview = load_preview(app, preview_id, false)?;
    let revision = preview.get("revision").and_then(Value::as_i64).unwrap_or(1);
    let limit = limit.clamp(1, MAX_ITEMS_LIMIT);
    let offset = decode_cursor(cursor.unwrap_or(""), preview_id, revision)?;
    let items = preview
        .get("items")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let end = (offset + limit).min(items.len());
    let page: Vec<Value> = if offset < items.len() {
        items[offset..end].iter().map(public_item).collect()
    } else {
        Vec::new()
    };
    let next_cursor = if end < items.len() {
        Some(encode_cursor(preview_id, revision, end))
    } else {
        None
    };
    Ok(json!({
        "preview_id": preview_id,
        "revision": revision,
        "cursor": cursor,
        "next_cursor": next_cursor,
        "items": page,
    }))
}

fn scan_sources(
    app: &AppState,
    sources: &[Value],
) -> Result<(Vec<Value>, Map<String, Value>), ApiError> {
    let adapters = load_adapters(&app.root_dir, Some(&app.data_dir));
    let plat_map = platform_by_extension(&adapters);
    let mut scanned = Vec::new();
    let mut fingerprints = Map::new();
    for (idx, source) in sources.iter().enumerate() {
        let stype = source
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        let key = format!(
            "{idx}:{stype}:{}",
            source
                .get("id")
                .or_else(|| source.get("path"))
                .and_then(Value::as_str)
                .unwrap_or("")
        );
        match stype {
            "folder" => {
                let folder = source
                    .get("path")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .trim();
                if folder.is_empty() {
                    return Err(ApiError::bad_request(
                        "Folder path is required for folder sources.",
                    ));
                }
                let games = import_multi_platform(folder, &adapters, false, None, &app.data_dir)?;
                let fp = folder_fingerprint(folder);
                let label = source
                    .get("id")
                    .and_then(Value::as_str)
                    .map(ToOwned::to_owned)
                    .unwrap_or_else(|| {
                        Path::new(folder)
                            .file_name()
                            .and_then(|n| n.to_str())
                            .unwrap_or("")
                            .to_owned()
                    });
                for mut g in games {
                    if let Some(m) = g.as_object_mut() {
                        m.insert(
                            "_source".into(),
                            json!({"type": "folder", "id": label, "label": label, "path": folder}),
                        );
                    }
                    scanned.push(g);
                }
                fingerprints.insert(key, Value::String(fp));
            }
            "files" => {
                let paths = source
                    .get("paths")
                    .and_then(Value::as_array)
                    .filter(|a| !a.is_empty())
                    .ok_or_else(|| {
                        ApiError::bad_request("paths are required for files sources.")
                    })?;
                let mut hasher = Sha256::new();
                for raw in paths {
                    let p = expand_user(Path::new(raw.as_str().unwrap_or("")));
                    hasher.update(file_fingerprint(&p).as_bytes());
                    if !p.is_file() {
                        continue;
                    }
                    let ext = p
                        .extension()
                        .and_then(|e| e.to_str())
                        .map(|e| format!(".{}", e.to_lowercase()))
                        .unwrap_or_default();
                    let platform = plat_map
                        .get(&ext)
                        .cloned()
                        .unwrap_or_else(|| "Imported".to_owned());
                    let fname = p.file_name().and_then(|n| n.to_str()).unwrap_or("");
                    scanned.push(json!({
                        "name": p.file_stem().and_then(|s| s.to_str()).unwrap_or(""),
                        "platform": platform,
                        "genre": "",
                        "path": p.to_string_lossy(),
                        "discs": [],
                        "_source": {
                            "type": "files",
                            "id": source.get("id").and_then(Value::as_str).unwrap_or(fname),
                            "label": fname,
                            "path": p.to_string_lossy(),
                        },
                    }));
                }
                fingerprints.insert(key, Value::String(hex::encode(hasher.finalize())));
            }
            "steam" | "heroic" | "lutris" | "faugus" | "scummvm" | "rpcs3" | "vita3k" => {
                let games = match stype {
                    "steam" => {
                        let mut e = Vec::new();
                        import_steam(None, &mut e)
                    }
                    "heroic" => import_heroic(None).unwrap_or_default(),
                    "lutris" => import_lutris(None).unwrap_or_default(),
                    "faugus" => scan_faugus_games(None),
                    "scummvm" => import_scummvm(None),
                    "rpcs3" => {
                        let mut e = Vec::new();
                        import_rpcs3_hdd(None, &mut e)
                    }
                    "vita3k" => import_vita3k(None),
                    _ => Vec::new(),
                };
                let mut hasher = Sha256::new();
                for mut g in games {
                    let ident = identity_for_game(&g);
                    hasher.update(ident.as_bytes());
                    if let Some(m) = g.as_object_mut() {
                        let path_val = m.get("path").cloned().unwrap_or(Value::Null);
                        m.insert(
                            "_source".into(),
                            json!({
                                "type": stype,
                                "id": source.get("id").and_then(Value::as_str).unwrap_or(&ident),
                                "label": stype,
                                "path": path_val,
                            }),
                        );
                    }
                    scanned.push(g);
                }
                fingerprints.insert(key, Value::String(hex::encode(hasher.finalize())));
            }
            "xbox360" => {
                let folder = source
                    .get("path")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .trim();
                if folder.is_empty() {
                    return Err(ApiError::bad_request(
                        "path is required for xbox360 sources.",
                    ));
                }
                let games = import_xbox360_folder(folder, "")?;
                let fp = folder_fingerprint(folder);
                for mut g in games {
                    if let Some(m) = g.as_object_mut() {
                        m.insert(
                            "_source".into(),
                            json!({"type": "xbox360", "id": folder, "label": "Xbox 360", "path": folder}),
                        );
                    }
                    scanned.push(g);
                }
                fingerprints.insert(key, Value::String(fp));
            }
            "arcade" => {
                let folder = source
                    .get("path")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .trim();
                if folder.is_empty() {
                    return Err(ApiError::bad_request(
                        "path is required for arcade sources.",
                    ));
                }
                let dat = source.get("dat_path").and_then(Value::as_str).unwrap_or("");
                let set_type = source
                    .get("set_type")
                    .and_then(Value::as_str)
                    .unwrap_or("MAME");
                let adapter_id = source
                    .get("adapter_id")
                    .or_else(|| source.get("emulator_id"))
                    .and_then(Value::as_str)
                    .unwrap_or("");
                let games = import_arcade(folder, dat, adapter_id, set_type, "any", false, &[])?;
                let fp = folder_fingerprint(folder);
                for mut g in games {
                    if let Some(m) = g.as_object_mut() {
                        m.insert(
                            "_source".into(),
                            json!({"type": "arcade", "id": folder, "label": "Arcade", "path": folder}),
                        );
                    }
                    scanned.push(g);
                }
                fingerprints.insert(key, Value::String(fp));
            }
            _ => {
                return Err(ApiError::bad_request(format!(
                    "Unsupported source type: {stype}"
                )));
            }
        }
    }
    Ok((scanned, fingerprints))
}

fn classify_candidates(
    app: &AppState,
    raw_games: Vec<Value>,
    state: &Value,
) -> Result<Vec<Value>, ApiError> {
    let adapters = load_adapters(&app.root_dir, Some(&app.data_dir));
    let filtered = filter_imported(raw_games, state);
    let existing_games = state
        .get("games")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let mut exact: HashMap<String, &Value> = HashMap::new();
    let mut cross: HashMap<String, &Value> = HashMap::new();
    for g in existing_games {
        for k in source_identities(g) {
            exact.entry(k).or_insert(g);
        }
        if let Some(ti) = cross_source_identity(g) {
            cross.entry(ti).or_insert(g);
        }
    }
    let blocked = exclusion_set(state);
    let mut items = Vec::new();
    for mut game in filtered {
        if items.len() >= MAX_ENTRIES {
            return Err(ApiError::preview_entry_limit_exceeded(format!(
                "Preview cannot exceed {MAX_ENTRIES} entries."
            )));
        }
        let source = game
            .as_object_mut()
            .and_then(|m| m.remove("_source"))
            .unwrap_or_else(|| json!({}));
        let identity = identity_for_game(&game);
        let path_str = game.get("path").and_then(Value::as_str).unwrap_or("");
        let cid = candidate_id(
            source.get("type").and_then(Value::as_str).unwrap_or(""),
            source.get("id").and_then(Value::as_str).unwrap_or(""),
            &identity,
            path_str,
        );
        let mut platform = game
            .get("platform")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        let ext = Path::new(path_str)
            .extension()
            .and_then(|e| e.to_str())
            .map(|e| format!(".{}", e.to_lowercase()))
            .unwrap_or_default();
        let mut options = platform_options_for_extension(&ext, &adapters);
        if !options.is_empty()
            && (platform.is_empty()
                || matches!(platform.as_str(), "Disc image" | "Imported")
                || options.contains(&platform))
        {
            platform.clear();
        } else {
            options.clear();
        }
        let choices = emulator_choices_for_platform(&platform, &adapters);
        let mut warnings = Vec::new();
        let mut group = "additions";
        let mut intended_action = "import";
        let mut existing_target = Value::Null;
        let mut merge_diff = Value::Null;

        let source_keys = source_identities(&game);
        let target = source_keys
            .iter()
            .find_map(|k| exact.get(k).copied())
            .or_else(|| cross_source_identity(&game).and_then(|ti| cross.get(&ti).copied()));
        let is_blocked =
            crate::exclusions::exclusion_key(&game).is_some_and(|k| blocked.contains(&k));
        if is_blocked {
            group = "exclusions";
            intended_action = "exclude";
        } else if let Some(t) = target {
            let tpath =
                normalize_path_identity(t.get("path").and_then(Value::as_str).unwrap_or(""));
            let gpath = normalize_path_identity(path_str);
            if tpath == gpath {
                group = "duplicates";
                intended_action = "skip";
            } else {
                group = "merges";
                intended_action = "merge";
                existing_target = json!({
                    "game_id": t.get("game_id").and_then(Value::as_str).unwrap_or(""),
                    "title": t.get("name").and_then(Value::as_str).unwrap_or(""),
                    "platform": t.get("platform").cloned().unwrap_or(Value::Null),
                });
                merge_diff = json!([{
                    "field": "path",
                    "current": t.get("path").cloned().unwrap_or(Value::Null),
                    "proposed": game.get("path").cloned().unwrap_or(Value::Null),
                    "effect": "fill",
                }]);
            }
        } else if platform.is_empty() {
            group = "ambiguities";
            intended_action = "review";
            let msg = if !options.is_empty() {
                "This file type belongs to several systems. Choose one."
            } else {
                "Platform could not be determined."
            };
            warnings.push(json!({"code": "AMBIGUOUS_PLATFORM", "message": msg}));
        } else if choices.is_empty()
            && game
                .get("launch")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim()
                .is_empty()
        {
            group = "unsupported";
            intended_action = "review";
            warnings.push(
                json!({"code": "UNSUPPORTED", "message": "No registry adapters for platform."}),
            );
        }
        items.push(json!({
            "candidate_id": cid,
            "group": group,
            "source": source,
            "detected_title": game.get("name").and_then(Value::as_str).unwrap_or(""),
            "detected_platform": platform,
            "platform_options": options,
            "intended_action": intended_action,
            "existing_game_target": existing_target,
            "warnings": warnings,
            "emulator_choices": choices,
            "selected_emulator_id": Value::Null,
            "selected_adapter_id": Value::Null,
            "launch_setup": Value::Null,
            "merge_diff": merge_diff,
            "_game": game,
            "_identity": identity,
        }));
    }
    Ok(items)
}

fn count_groups(items: &[Value]) -> Value {
    let mut counts: HashMap<&str, usize> = [
        ("additions", 0),
        ("merges", 0),
        ("duplicates", 0),
        ("ambiguities", 0),
        ("exclusions", 0),
        ("unsupported", 0),
        ("errors", 0),
    ]
    .into_iter()
    .collect();
    for item in items {
        let g = item
            .get("group")
            .and_then(Value::as_str)
            .unwrap_or("errors");
        *counts.entry(g).or_insert(0) += 1;
    }
    json!(counts)
}

pub fn run_scan_job(app: &AppState, preview_id: &str) -> Result<Value, ApiError> {
    let mut preview = load_preview(app, preview_id, true)?;
    preview["state"] = json!("running");
    save_preview(app, &preview)?;
    let sources = preview
        .get("_request_sources")
        .or_else(|| preview.get("sources"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let state = app
        .load_state_view()
        .map_err(|e| ApiError::bad_request(e.to_string()))?;
    match scan_sources(app, &sources).and_then(|(raw, fps)| {
        let items = classify_candidates(app, raw, &state)?;
        Ok((items, fps))
    }) {
        Ok((items, fps)) => {
            let counts = count_groups(&items);
            let scanned = items.len();
            if let Some(m) = preview.as_object_mut() {
                m.insert("source_fingerprints".into(), Value::Object(fps));
                m.insert("items".into(), Value::Array(items));
                m.insert("scanned_entries".into(), json!(scanned));
                m.insert("counts".into(), counts.clone());
                m.insert("library_signature".into(), Value::String(library_sig(app)));
                m.insert("state".into(), json!("ready"));
                m.remove("_request_sources");
            }
            save_preview(app, &preview)?;
            Ok(json!({"scanned_entries": scanned, "counts": counts}))
        }
        Err(err) => {
            preview["state"] = json!("error");
            let _ = save_preview(app, &preview);
            Err(err)
        }
    }
}

pub fn apply_decisions(
    app: &AppState,
    preview_id: &str,
    decisions: &[Value],
) -> Result<Value, ApiError> {
    if decisions.len() > MAX_DECISION_BATCH {
        return Err(ApiError::bad_request(format!(
            "At most {MAX_DECISION_BATCH} decisions are allowed per request."
        )));
    }
    let adapters = load_adapters(&app.root_dir, Some(&app.data_dir));
    let mut preview = load_preview(app, preview_id, false)?;
    let mut accepted = 0usize;
    let mut decision_map = preview
        .get("decisions")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    if let Some(Value::Array(items)) = preview.get_mut("items") {
        for dec in decisions {
            let cid = dec
                .get("candidate_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim();
            let Some(item) = items
                .iter_mut()
                .find(|i| i.get("candidate_id").and_then(Value::as_str) == Some(cid))
            else {
                return Err(ApiError::bad_request(format!(
                    "Unknown candidate_id: {cid}"
                )));
            };
            let action = dec
                .get("action")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim();
            if !matches!(action, "import" | "merge" | "skip" | "exclude") {
                return Err(ApiError::bad_request("Invalid decision action."));
            }
            let chosen = dec
                .get("platform")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim();
            if !chosen.is_empty() {
                let valid = item
                    .get("platform_options")
                    .and_then(Value::as_array)
                    .is_some_and(|opts| opts.iter().any(|o| o.as_str() == Some(chosen)));
                if !valid {
                    return Err(ApiError::bad_request(
                        "That platform is not one of the options for this file.",
                    ));
                }
                item["detected_platform"] = json!(chosen);
                item["emulator_choices"] =
                    Value::Array(emulator_choices_for_platform(chosen, &adapters));
                item["group"] = json!("additions");
                if let Some(Value::Array(ws)) = item.get_mut("warnings") {
                    ws.retain(|w| {
                        w.get("code").and_then(Value::as_str) != Some("AMBIGUOUS_PLATFORM")
                    });
                }
            }
            if action == "import"
                && item
                    .get("platform_options")
                    .and_then(Value::as_array)
                    .is_some_and(|a| !a.is_empty())
                && item
                    .get("detected_platform")
                    .and_then(Value::as_str)
                    .is_none_or(str::is_empty)
            {
                return Err(ApiError::bad_request(
                    "Choose a platform for this file before importing it.",
                ));
            }
            item["intended_action"] = json!(action);
            if action == "merge" {
                let merge_target = dec
                    .get("merge_target")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .trim();
                if merge_target.is_empty() {
                    return Err(ApiError::bad_request(
                        "merge_target is required for merge decisions.",
                    ));
                }
                if !item["existing_game_target"].is_object() {
                    item["existing_game_target"] = json!({
                        "game_id": merge_target,
                        "title": "",
                        "platform": item.get("detected_platform").cloned().unwrap_or(Value::Null),
                    });
                } else {
                    item["existing_game_target"]["game_id"] = json!(merge_target);
                }
            }
            let em_id = dec.get("emulator_id").cloned().unwrap_or(Value::Null);
            let ad_id = dec.get("adapter_id").cloned().unwrap_or(Value::Null);
            let setup = dec.get("launch_setup").cloned().unwrap_or(Value::Null);
            if setup.as_str() == Some("adapter") && (em_id.is_null() || ad_id.is_null()) {
                return Err(ApiError::bad_request(
                    "adapter launch_setup requires emulator_id and adapter_id.",
                ));
            }
            item["selected_emulator_id"] = em_id;
            item["selected_adapter_id"] = ad_id;
            item["launch_setup"] = setup;
            decision_map.insert(cid.to_owned(), dec.clone());
            accepted += 1;
        }
    }
    preview["decisions"] = Value::Object(decision_map);
    save_preview(app, &preview)?;
    Ok(json!({"accepted": accepted}))
}

pub fn revalidate_preview_record(app: &AppState, preview_id: &str) -> Result<Value, ApiError> {
    let mut preview = load_preview(app, preview_id, false)?;
    let cur_sig = library_sig(app);
    if preview.get("library_signature").and_then(Value::as_str) != Some(cur_sig.as_str()) {
        return Err(ApiError::preview_library_changed(
            "Library changed since preview was created.",
        ));
    }
    let sources = preview
        .get("sources")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let (_, fps) = scan_sources(app, &sources)?;
    if preview.get("source_fingerprints") != Some(&Value::Object(fps)) {
        return Err(ApiError::preview_stale(
            "Source fingerprints changed since preview was created.",
        ));
    }
    preview["revalidated"] = json!(true);
    save_preview(app, &preview)?;
    Ok(preview_document(&preview))
}

pub fn commit_preview(
    app: &AppState,
    preview_id: &str,
    revision: i64,
    emulator_choices: &[Value],
    import_batch_id: &str,
) -> Result<Value, ApiError> {
    let preview = load_preview(app, preview_id, false)?;
    if preview.get("revision").and_then(Value::as_i64).unwrap_or(0) != revision {
        return Err(ApiError::preview_stale("Preview revision is stale."));
    }
    revalidate_preview_record(app, preview_id)?;
    let mut preview = load_preview(app, preview_id, false)?;
    let unresolved = preview
        .get("items")
        .and_then(Value::as_array)
        .is_some_and(|items| {
            items.iter().any(|i| {
                i.get("group").and_then(Value::as_str) == Some("ambiguities")
                    && i.get("intended_action").and_then(Value::as_str) == Some("review")
            })
        });
    if unresolved {
        return Err(ApiError::unresolved_candidates(
            "Unresolved candidates remain in preview.",
        ));
    }
    let token = format!("{preview_id}:{revision}");
    if let Some(existing) = preview.get("commit_tokens").and_then(|m| m.get(&token)) {
        return Ok(existing.clone());
    }
    let choices_by_id: HashMap<String, &Value> = emulator_choices
        .iter()
        .filter_map(|c| {
            let cid = c.get("candidate_id").and_then(Value::as_str)?.to_owned();
            Some((cid, c))
        })
        .collect();
    let decisions = preview
        .get("decisions")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();

    let generated_dir = app.data_dir.join("generated");
    let staging_dir = generated_dir.join(".staging").join(preview_id);
    let _ = fs::create_dir_all(&staging_dir);

    let items = preview
        .get("items")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut added = 0usize;
    let mut merged = 0usize;
    let mut skipped = 0usize;
    let mut excluded = 0usize;
    let mut failed = 0usize;
    let data_dir = app.data_dir.clone();

    let _ = app
        .transact_state(|state| {
            let Some(Value::Array(games)) = state.get_mut("games") else {
                return Ok(());
            };
            let mut exact: HashMap<String, usize> = HashMap::new();
            for (idx, g) in games.iter().enumerate() {
                for k in source_identities(g) {
                    exact.entry(k).or_insert(idx);
                }
            }
            let timestamp = jiff::Timestamp::now()
                .strftime("%Y-%m-%dT%H:%M:%S")
                .to_string();
            for item in &items {
                let action = item
                    .get("intended_action")
                    .and_then(Value::as_str)
                    .unwrap_or("");
                if action == "skip" {
                    skipped += 1;
                    continue;
                }
                if action == "exclude" {
                    excluded += 1;
                    continue;
                }
                if !matches!(action, "import" | "merge") {
                    failed += 1;
                    continue;
                }
                let mut game = item.get("_game").cloned().unwrap_or_else(|| json!({}));
                let cid = item
                    .get("candidate_id")
                    .and_then(Value::as_str)
                    .unwrap_or("");
                let choice = choices_by_id
                    .get(cid)
                    .copied()
                    .or_else(|| decisions.get(cid));
                let setup = choice
                    .and_then(|c| c.get("launch_setup"))
                    .or_else(|| item.get("launch_setup"))
                    .and_then(Value::as_str);
                let sel_em = choice
                    .and_then(|c| c.get("emulator_id"))
                    .or_else(|| item.get("selected_emulator_id"))
                    .and_then(Value::as_str);
                let sel_ad = choice
                    .and_then(|c| c.get("adapter_id"))
                    .or_else(|| item.get("selected_adapter_id"))
                    .and_then(Value::as_str);
                if let Some(m) = game.as_object_mut() {
                    if let Some(plat) = item
                        .get("detected_platform")
                        .and_then(Value::as_str)
                        .filter(|s| !s.is_empty())
                    {
                        m.insert("platform".into(), Value::String(plat.to_owned()));
                    }
                    match setup {
                        Some("adapter" | "install_flatpak") => {
                            if let Some(e) = sel_em {
                                m.insert("emulator_id".into(), Value::String(e.to_owned()));
                            }
                            if let Some(a) = sel_ad {
                                m.insert("emulator_adapter_id".into(), Value::String(a.to_owned()));
                            }
                        }
                        _ => {
                            m.remove("emulator_id");
                            m.remove("emulator_adapter_id");
                        }
                    }
                    if let Some(discs) = m.get("discs").and_then(Value::as_array)
                        && !discs.is_empty()
                    {
                        let disc_paths: Vec<PathBuf> = discs
                            .iter()
                            .filter_map(Value::as_str)
                            .map(PathBuf::from)
                            .collect();
                        if let Some(first) = disc_paths.first() {
                            let base = first.file_stem().and_then(|s| s.to_str()).unwrap_or("disc");
                            let staged = staging_dir.join(format!("{base}.m3u"));
                            if generate_m3u(&disc_paths, &staged).is_ok() {
                                m.insert(
                                    "path".into(),
                                    Value::String(
                                        generated_dir
                                            .join(format!("{base}.m3u"))
                                            .to_string_lossy()
                                            .into_owned(),
                                    ),
                                );
                            }
                        }
                    }
                    m.insert(
                        "import_batch_id".into(),
                        Value::String(import_batch_id.to_owned()),
                    );
                    m.insert("added_at".into(), Value::String(timestamp.clone()));
                }
                if action == "merge"
                    && let Some(gid) = item
                        .get("existing_game_target")
                        .and_then(|t| t.get("game_id"))
                        .and_then(Value::as_str)
                    && let Some(target) = games
                        .iter_mut()
                        .find(|g| g.get("game_id").and_then(Value::as_str) == Some(gid))
                {
                    merge_imported_game(target, &game, true, &data_dir);
                    merged += 1;
                    continue;
                }
                let keys = source_identities(&game);
                if keys.iter().any(|k| exact.contains_key(k)) {
                    skipped += 1;
                    continue;
                }
                let new_idx = games.len();
                for k in keys {
                    exact.entry(k).or_insert(new_idx);
                }
                games.push(game);
                added += 1;
            }
            Ok(())
        })
        .map_err(|e| ApiError::bad_request(e.to_string()))?;

    if staging_dir.is_dir() {
        let _ = fs::create_dir_all(&generated_dir);
        if let Ok(rd) = fs::read_dir(&staging_dir) {
            for entry in rd.filter_map(Result::ok) {
                let p = entry.path();
                if p.extension().is_some_and(|e| e == "m3u")
                    && let Ok(bytes) = fs::read(&p)
                    && let Some(name) = p.file_name()
                {
                    let _ = atomic_write_bytes(&generated_dir.join(name), &bytes, 0o600);
                }
            }
        }
        let _ = fs::remove_dir_all(&staging_dir);
    }

    let result = json!({
        "import_batch_id": import_batch_id,
        "added": added,
        "merged": merged,
        "skipped": skipped,
        "excluded": excluded,
        "failed": failed,
    });
    if let Some(m) = preview.as_object_mut() {
        let tokens = m
            .entry("commit_tokens".to_owned())
            .or_insert_with(|| Value::Object(Map::new()));
        if let Some(tm) = tokens.as_object_mut() {
            tm.insert(token, result.clone());
        }
    }
    save_preview(app, &preview)?;
    Ok(result)
}

// ── Compatibility Report & Readiness Checks (pkg/parity/parity_compat.py) ──

fn root_tokens(css_text: &str) -> BTreeSet<String> {
    let mut tokens = BTreeSet::new();
    for block in ROOT_BLOCK_RE.captures_iter(css_text) {
        if let Some(m) = block.get(1) {
            for tok in TOKEN_DECL_RE.captures_iter(m.as_str()) {
                if let Some(t) = tok.get(1) {
                    tokens.insert(t.as_str().to_owned());
                }
            }
        }
    }
    tokens
}

fn read_json_obj(path: &Path) -> Map<String, Value> {
    fs::read_to_string(path)
        .ok()
        .and_then(|s| serde_json::from_str::<Value>(&s).ok())
        .and_then(|v| v.as_object().cloned())
        .unwrap_or_default()
}

fn backup_rows(backups_dir: &Path) -> Vec<Value> {
    if !backups_dir.is_dir() {
        return Vec::new();
    }
    let mut paths: Vec<PathBuf> = fs::read_dir(backups_dir)
        .ok()
        .into_iter()
        .flatten()
        .filter_map(Result::ok)
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|e| e == "zip"))
        .collect();
    paths.sort();
    paths
        .into_iter()
        .take(50)
        .map(|path| {
            let name = path
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or("")
                .to_owned();
            let manifest: Option<Value> = File::open(&path).ok().and_then(|f| {
                let mut archive = zip::ZipArchive::new(f).ok()?;
                let mut entry = archive.by_name("manifest.json").ok()?;
                let mut text = String::new();
                entry.read_to_string(&mut text).ok()?;
                serde_json::from_str(&text).ok()
            });
            match manifest.and_then(|m| m.as_object().cloned()) {
                None => json!({"name": name, "format": Value::Null, "disposition": "unreadable"}),
                Some(m) => {
                    let declared = m.get("format").and_then(Value::as_i64).unwrap_or(1);
                    let disp = match declared {
                        1 => "both",
                        2 => "2.0-only",
                        _ => "refused",
                    };
                    json!({"name": name, "format": declared, "disposition": disp})
                }
            }
        })
        .collect()
}

fn plugins_section_light(plugins_dir: &Path) -> Vec<Value> {
    if !plugins_dir.is_dir() {
        return Vec::new();
    }
    let mut dirs: Vec<PathBuf> = fs::read_dir(plugins_dir)
        .ok()
        .into_iter()
        .flatten()
        .filter_map(Result::ok)
        .map(|e| e.path())
        .filter(|p| p.is_dir())
        .collect();
    dirs.sort();
    let mut rows = Vec::new();
    for dir in dirs {
        let id = dir
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("")
            .to_owned();
        if matches!(id.as_str(), "data" | "credentials") {
            continue;
        }
        let manifest = read_json_obj(&dir.join("plugin.json"));
        if manifest.is_empty() {
            continue;
        }
        let api_version = manifest
            .get("api_version")
            .and_then(Value::as_i64)
            .unwrap_or(1);
        let valid = api_version == 2;
        let disp = if valid { "compatible" } else { "refused" };
        let err = if api_version == 1 {
            "Plugin API v1 was removed in 3.0; update to api_version 2."
        } else if !valid {
            "Unsupported plugin API version."
        } else {
            ""
        };
        rows.push(json!({
            "id": manifest.get("id").and_then(Value::as_str).unwrap_or(&id),
            "name": manifest.get("name").and_then(Value::as_str).unwrap_or(&id),
            "api_version": api_version,
            "valid": valid,
            "enabled": false,
            "sandbox": "unknown",
            "disposition": disp,
            "error": err,
        }));
    }
    rows
}

pub fn compat_build_report(data_dir: &Path, root_dir: &Path) -> Value {
    let raw = read_json_obj(&data_dir.join("library.json"));
    let settings = raw
        .get("settings")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let migration = read_json_obj(&data_dir.join("migration-report.json"));
    let contract = fs::read_to_string(root_dir.join("static/app.css"))
        .map(|s| root_tokens(&s))
        .unwrap_or_default();
    let mut theme_rows = Vec::new();
    let themes_dir = data_dir.join("themes");
    if themes_dir.is_dir() {
        let mut paths: Vec<PathBuf> = fs::read_dir(&themes_dir)
            .ok()
            .into_iter()
            .flatten()
            .filter_map(Result::ok)
            .map(|e| e.path())
            .filter(|p| p.extension().is_some_and(|e| e == "css"))
            .collect();
        paths.sort();
        for p in paths {
            let name = p
                .file_stem()
                .and_then(|s| s.to_str())
                .unwrap_or("")
                .to_owned();
            match fs::read_to_string(&p) {
                Ok(text) => {
                    let decl = root_tokens(&text);
                    let missing: Vec<String> = contract.difference(&decl).cloned().collect();
                    let unknown: Vec<String> = decl.difference(&contract).cloned().collect();
                    let disp = if missing.is_empty() {
                        "complete"
                    } else {
                        "fallbacks"
                    };
                    theme_rows.push(json!({
                        "name": name,
                        "missing": missing,
                        "unknown": unknown,
                        "disposition": disp,
                    }));
                }
                Err(_) => {
                    theme_rows.push(json!({
                        "name": name,
                        "missing": [],
                        "unknown": [],
                        "disposition": "unreadable",
                    }));
                }
            }
        }
    }
    let known: HashSet<&str> = KNOWN_SETTINGS.iter().copied().collect();
    let mut unknown_keys: Vec<String> = settings
        .keys()
        .filter(|k| !known.contains(k.as_str()))
        .cloned()
        .collect();
    unknown_keys.sort();
    let cloud_folder = settings
        .get("cloud_folder")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();

    let vault_root = data_dir.join("save-vault");
    let vault_games = if vault_root.is_dir() {
        fs::read_dir(&vault_root)
            .ok()
            .into_iter()
            .flatten()
            .filter_map(Result::ok)
            .filter(|e| e.path().is_dir())
            .count()
    } else {
        0
    };
    let deals_enabled = settings
        .get("deals_enabled")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let deals_watchlist = raw
        .get("deals")
        .and_then(|d| d.get("watchlist"))
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0);

    json!({
        "format": REPORT_FORMAT,
        "version": REPORT_VERSION,
        "library": {
            "schema_version": raw.get("schema_version").cloned().unwrap_or(Value::Null),
            "path_mode": raw.get("path_mode").and_then(Value::as_str).unwrap_or("absolute"),
            "library_root": raw.get("library_root").and_then(Value::as_str).unwrap_or(""),
            "upgraded_from": migration.get("from_schema").cloned().unwrap_or(Value::Null),
            "upgrade_backup": data_dir.join("library.v6.backup.json").is_file(),
        },
        "themes": theme_rows,
        "backups": backup_rows(&data_dir.join("backups")),
        "settings": {
            "unknown_keys": unknown_keys,
        },
        "plugins": plugins_section_light(&data_dir.join("plugins")),
        "api": {
            "public_prefix": "/api/v2",
            "host_channel": "/api/host",
            "retired": ["/api/v1", "unversioned /api/*", "legacy host paths"],
        },
        "sync": {
            "configured": !cloud_folder.is_empty(),
            "found": !cloud_folder.is_empty() && Path::new(cloud_folder).is_dir(),
        },
        "save_vault": {
            "games": vault_games,
            "snapshots": 0,
            "bytes": 0,
        },
        "deals": {
            "enabled": deals_enabled,
            "host_allowlist": ["store.steampowered.com"],
            "payload": "appid_and_search_query_only",
            "watchlist_count": deals_watchlist,
        },
    })
}

pub fn readiness_checks(data_dir: &Path, root_dir: &Path) -> Vec<Value> {
    let report = compat_build_report(data_dir, root_dir);
    let raw = read_json_obj(&data_dir.join("library.json"));
    let games_len = raw
        .get("games")
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0);
    let mut items = Vec::new();
    for theme in report
        .get("themes")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let name = theme.get("name").and_then(Value::as_str).unwrap_or("");
        if theme.get("disposition").and_then(Value::as_str) == Some("unreadable") {
            items.push(json!({
                "id": format!("theme:{name}"),
                "kind": "theme",
                "label": name,
                "detail": "the theme file could not be read",
                "action": "review",
            }));
            continue;
        }
        let unknown: Vec<&str> = theme
            .get("unknown")
            .and_then(Value::as_array)
            .map(|a| a.iter().filter_map(Value::as_str).collect())
            .unwrap_or_default();
        if !unknown.is_empty() {
            let preview: Vec<&str> = unknown.iter().take(3).copied().collect();
            items.push(json!({
                "id": format!("theme:{name}"),
                "kind": "theme",
                "label": name,
                "detail": format!("{} token(s) the app does not know: {}", unknown.len(), preview.join(", ")),
                "action": "review",
            }));
        }
    }
    for plugin in report
        .get("plugins")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let pid = plugin.get("id").and_then(Value::as_str).unwrap_or("");
        if matches!(pid, "data" | "credentials")
            || plugin.get("disposition").and_then(Value::as_str) == Some("compatible")
        {
            continue;
        }
        items.push(json!({
            "id": format!("plugin:{pid}"),
            "kind": "plugin",
            "label": plugin.get("name").and_then(Value::as_str).unwrap_or(pid),
            "detail": plugin.get("error").and_then(Value::as_str).unwrap_or("incompatible plugin"),
            "action": "update",
        }));
    }
    if let Some(sync) = report.get("sync")
        && sync
            .get("configured")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        && !sync.get("found").and_then(Value::as_bool).unwrap_or(false)
    {
        items.push(json!({
            "id": "sync:folder",
            "kind": "sync",
            "label": "Sync folder",
            "detail": "the configured sync folder is not on disk",
            "action": "check",
        }));
    }
    for archive in report
        .get("backups")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        if archive.get("format").is_none_or(Value::is_null) {
            let name = archive.get("name").and_then(Value::as_str).unwrap_or("");
            items.push(json!({
                "id": format!("backup:{name}"),
                "kind": "backup",
                "label": name,
                "detail": "the archive carries no format and cannot be restored",
                "action": "recreate",
            }));
        }
    }
    if games_len > READINESS_LIBRARY_LIMIT {
        items.push(json!({
            "id": "library:size",
            "kind": "library",
            "label": "Library size",
            "detail": format!("{games_len} games is above the {READINESS_LIBRARY_LIMIT} game scale"),
            "action": "review",
        }));
    }
    let default_dir = default_data_dir();
    let custom = fs::canonicalize(data_dir).ok() != fs::canonicalize(&default_dir).ok();
    if custom {
        items.push(json!({
            "id": "library:data_dir",
            "kind": "library",
            "label": "Data directory",
            "detail": format!("the library lives in {}", data_dir.display()),
            "action": "review",
        }));
    }
    items
}
