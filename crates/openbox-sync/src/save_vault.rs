//! Save Vault — indexed, retained, SHA-256-verified save-file snapshots (`pkg/parity/parity_save_vault.py`, ADR 0109).

use std::collections::{HashMap, HashSet};
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::LazyLock;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::UNIX_EPOCH;

use jiff::Timestamp;
use openbox_core::ApiError;
use openbox_io::{atomic_write_text, fsync_directory};
use openbox_platform::user_symlink_component;
use parking_lot::ReentrantMutex;
use regex::Regex;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use uuid::Uuid;

use crate::saves::{
    SAVE_SYNC_LOCK, discover_save_paths, expand_user_path, extra_save_candidates, file_mode_bits,
    file_sha256, reject_symlink_components, resolve_non_strict, save_source_for, set_mode,
};

pub const VAULT_DIR_NAME: &str = "save-vault";
pub const INDEX_FILE_NAME: &str = "index.json";
pub const SNAPSHOT_FILE_NAME: &str = "snapshot.json";
pub const INDEX_FORMAT: i64 = 1;
pub const SNAPSHOT_FORMAT: i64 = 1;
pub const DEFAULT_KEEP: usize = 5;
pub const MAX_KEEP: usize = 50;
pub const MAX_SNAPSHOT_BYTES: u64 = 512 * 1024 * 1024;
pub const MAX_SNAPSHOT_FILES: usize = 5_000;
pub const SIZE_WARNING_BYTES: u64 = 5 * 1024 * 1024 * 1024;
pub const PREVIEW_BUCKET_LIMIT: usize = 200;

static VAULT_LOCK: LazyLock<ReentrantMutex<()>> = LazyLock::new(|| ReentrantMutex::new(()));
static SNAPSHOT_SEQ: AtomicU64 = AtomicU64::new(0);
static SAFE_KEY_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[^A-Za-z0-9_.-]+").unwrap());

pub fn vault_dir_for(data_dir: &Path) -> PathBuf {
    data_dir.join(VAULT_DIR_NAME)
}

pub fn game_vault_key(game: &Value) -> String {
    let gid = game
        .get("game_id")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("");
    if !gid.is_empty() {
        let cleaned = SAFE_KEY_RE.replace_all(gid, "_");
        let trimmed = cleaned.trim_matches(|c| c == '.' || c == '_');
        if !trimmed.is_empty() {
            return trimmed.chars().take(80).collect();
        }
    }
    let name = game.get("name").and_then(Value::as_str).unwrap_or("");
    let platform = game.get("platform").and_then(Value::as_str).unwrap_or("");
    let mut hasher = Sha256::new();
    hasher.update(format!("{name}:{platform}").as_bytes());
    let hex_str = hex::encode(hasher.finalize());
    format!("g-{}", &hex_str[..16])
}

fn utc_iso(ts: Timestamp) -> String {
    let dt = jiff::tz::TimeZone::UTC.to_datetime(ts);
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
        dt.year(),
        dt.month(),
        dt.day(),
        dt.hour(),
        dt.minute(),
        dt.second()
    )
}

fn utc_compact(ts: Timestamp) -> String {
    let dt = jiff::tz::TimeZone::UTC.to_datetime(ts);
    format!(
        "{:04}{:02}{:02}T{:02}{:02}{:02}Z",
        dt.year(),
        dt.month(),
        dt.day(),
        dt.hour(),
        dt.minute(),
        dt.second()
    )
}

fn new_snapshot_id() -> String {
    let bytes = Uuid::new_v4();
    format!("snap-{}", &hex::encode(bytes.as_bytes())[..16])
}

fn empty_index() -> Value {
    json!({
        "format": INDEX_FORMAT,
        "last_auto_at": "",
        "snapshots": [],
    })
}

pub fn clamp_keep(val: Option<i64>, default: usize) -> usize {
    let n = val.unwrap_or(default as i64);
    n.clamp(1, MAX_KEEP as i64) as usize
}

fn reject_symlinks(path: &Path) -> Result<(), ApiError> {
    if let Err(err) = reject_symlink_components(path) {
        return Err(ApiError::vault_path_escape(err.to_string()));
    }
    if let Some(comp) = user_symlink_component(path) {
        return Err(ApiError::vault_path_escape(format!(
            "Save path may not contain symlinks: {}",
            comp.display()
        )));
    }
    Ok(())
}

fn validate_relpath(relpath: &str) -> Result<Vec<String>, ApiError> {
    if relpath.is_empty() || relpath.contains('\0') {
        return Err(ApiError::vault_path_escape(
            "Save relative path is invalid.",
        ));
    }
    if relpath.starts_with('/') || relpath.starts_with('\\') || relpath.contains('\\') {
        return Err(ApiError::vault_path_escape(format!(
            "Save relative path must be relative POSIX: {relpath}"
        )));
    }
    let parts: Vec<String> = relpath.split('/').map(str::to_owned).collect();
    if parts.is_empty() || parts.iter().any(|p| p.is_empty() || p == "." || p == "..") {
        return Err(ApiError::vault_path_escape(format!(
            "Save relative path escapes root: {relpath}"
        )));
    }
    if parts[0].contains(':') {
        return Err(ApiError::vault_path_escape(format!(
            "Save relative path may not contain a drive prefix: {relpath}"
        )));
    }
    Ok(parts)
}

fn resolve_contained_target(
    root: &Path,
    relpath: &str,
    is_file_root: bool,
) -> Result<PathBuf, ApiError> {
    let parts = validate_relpath(relpath)?;
    reject_symlinks(root)?;
    let resolved_root = resolve_non_strict(root);
    if is_file_root {
        let root_name = resolved_root
            .file_name()
            .and_then(|s| s.to_str())
            .unwrap_or("");
        if parts.len() != 1 || parts[0] != relpath || parts[0] != root_name {
            return Err(ApiError::vault_path_escape(format!(
                "Save restore target does not match file root: {relpath}"
            )));
        }
        reject_symlinks(&resolved_root)?;
        return Ok(resolved_root);
    }
    let mut joined = resolved_root.clone();
    for p in &parts {
        joined.push(p);
    }
    let target = resolve_non_strict(&joined);
    if target != resolved_root && !target.starts_with(&resolved_root) {
        return Err(ApiError::vault_path_escape(format!(
            "Save restore path escapes save root: {relpath}"
        )));
    }
    let mut cursor = resolved_root;
    for p in &parts {
        cursor.push(p);
        if cursor
            .symlink_metadata()
            .map(|m| m.file_type().is_symlink())
            .unwrap_or(false)
        {
            return Err(ApiError::vault_path_escape(format!(
                "Save restore target contains symlink: {}",
                cursor.display()
            )));
        }
    }
    Ok(target)
}

fn safe_snapshot_dir(
    vault_dir: &Path,
    game_key: &str,
    folder_name: &str,
) -> Result<PathBuf, ApiError> {
    if folder_name.is_empty()
        || folder_name.contains('/')
        || folder_name.contains('\\')
        || folder_name == "."
        || folder_name == ".."
    {
        return Err(ApiError::vault_path_escape("Invalid snapshot folder name."));
    }
    let base = resolve_non_strict(&vault_dir.join(game_key));
    let candidate = resolve_non_strict(&base.join(folder_name));
    if !candidate.starts_with(&base) || candidate == base {
        return Err(ApiError::vault_path_escape(
            "Snapshot path escapes vault directory.",
        ));
    }
    Ok(candidate)
}

fn shared_paths_for_game(game: &Value, home: Option<&Path>) -> HashSet<String> {
    let mut shared = HashSet::new();
    if let Some(sources) = game.get("save_sources").and_then(Value::as_object) {
        for (path, meta) in sources {
            if meta.get("shared").and_then(Value::as_bool).unwrap_or(false) {
                shared.insert(path.clone());
            }
        }
    }
    let mut items = discover_save_paths(game, home);
    items.extend(extra_save_candidates(game, home));
    for item in items {
        if item.get("shared").and_then(Value::as_bool).unwrap_or(false)
            && let Some(p) = item.get("path").and_then(Value::as_str)
            && !p.is_empty()
        {
            shared.insert(p.to_owned());
        }
    }
    shared
}

pub fn resolve_game_save_roots(
    game: &Value,
    include_shared: bool,
    home: Option<&Path>,
) -> Result<Vec<Value>, ApiError> {
    if !game.is_object() {
        return Err(ApiError::vault_no_saves(
            "No save paths discovered for this game.",
        ));
    }
    let shared_set = shared_paths_for_game(game, home);
    let mut candidates: Vec<(String, bool)> = Vec::new();
    let mut seen_raw: HashSet<String> = HashSet::new();

    if let Some(configured) = game.get("save_paths").and_then(Value::as_array) {
        for raw in configured {
            let text = raw.as_str().map(str::trim).unwrap_or("");
            if !text.is_empty() && seen_raw.insert(text.to_owned()) {
                candidates.push((text.to_owned(), shared_set.contains(text)));
            }
        }
    }
    let mut discovered = discover_save_paths(game, home);
    discovered.extend(extra_save_candidates(game, home));
    for item in discovered {
        let text = item
            .get("path")
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or("");
        let is_shared = item.get("shared").and_then(Value::as_bool).unwrap_or(false)
            || shared_set.contains(text);
        if text.is_empty() || is_shared || !seen_raw.insert(text.to_owned()) {
            continue;
        }
        candidates.push((text.to_owned(), false));
    }

    let mut roots: Vec<Value> = Vec::new();
    let mut seen_resolved: HashSet<PathBuf> = HashSet::new();
    for (raw_text, is_shared) in candidates {
        if !include_shared && is_shared {
            continue;
        }
        let raw_path = expand_user_path(&raw_text);
        reject_symlinks(&raw_path)?;
        if !raw_path.exists() {
            continue;
        }
        let resolved =
            fs::canonicalize(&raw_path).map_err(|e| ApiError::vault_path_escape(e.to_string()))?;
        if !seen_resolved.insert(resolved.clone()) {
            continue;
        }
        roots.push(json!({
            "index": roots.len(),
            "path": resolved.to_string_lossy(),
            "is_file": resolved.is_file(),
            "shared": is_shared,
            "source": Value::Object(save_source_for(game, &raw_text)),
        }));
    }
    if roots.is_empty() {
        return Err(ApiError::vault_no_saves(
            "No existing save files were discovered for this game.",
        ));
    }
    Ok(roots)
}

struct CollectedFile {
    root_idx: usize,
    source_path: PathBuf,
    relpath: String,
    archive_rel: String,
    size: u64,
    mtime: i64,
    mode: u32,
}

fn collect_root_files(roots: &[Value]) -> Result<Vec<CollectedFile>, ApiError> {
    let mut collected = Vec::new();
    let mut total_bytes: u64 = 0;

    for root_info in roots {
        let root_idx = root_info.get("index").and_then(Value::as_u64).unwrap_or(0) as usize;
        let root_path = PathBuf::from(root_info.get("path").and_then(Value::as_str).unwrap_or(""));
        reject_symlinks(&root_path)?;
        let is_file = root_info
            .get("is_file")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let pairs: Vec<(PathBuf, String)> = if is_file {
            let name = root_path
                .file_name()
                .and_then(|s| s.to_str())
                .unwrap_or("save")
                .to_owned();
            vec![(root_path.clone(), name)]
        } else {
            let mut list = Vec::new();
            let mut stack = vec![root_path.clone()];
            while let Some(dir) = stack.pop() {
                let Ok(rd) = fs::read_dir(&dir) else {
                    continue;
                };
                let mut entries: Vec<PathBuf> = rd.flatten().map(|e| e.path()).collect();
                entries.sort();
                for entry in entries {
                    let meta = fs::symlink_metadata(&entry)
                        .map_err(|e| ApiError::vault_path_escape(e.to_string()))?;
                    if meta.file_type().is_symlink() {
                        return Err(ApiError::vault_path_escape(format!(
                            "Save path contains a symlink: {}",
                            entry.display()
                        )));
                    }
                    if meta.is_dir() {
                        stack.push(entry);
                    } else if meta.is_file() {
                        let rel = entry
                            .strip_prefix(&root_path)
                            .map_err(|e| ApiError::vault_path_escape(e.to_string()))?
                            .to_string_lossy()
                            .replace('\\', "/");
                        validate_relpath(&rel)?;
                        list.push((entry, rel));
                    }
                }
            }
            list.sort_by(|a, b| a.1.cmp(&b.1));
            list
        };

        for (file_path, rel) in pairs {
            let mut opts = OpenOptions::new();
            opts.read(true);
            #[cfg(unix)]
            {
                use std::os::unix::fs::OpenOptionsExt;
                opts.custom_flags(crate::saves::libc_o_nofollow());
            }
            let file = opts.open(&file_path).map_err(|e| {
                ApiError::vault_path_escape(format!(
                    "Save file cannot be opened safely: {} ({e})",
                    file_path.display()
                ))
            })?;
            let meta = file
                .metadata()
                .map_err(|e| ApiError::vault_path_escape(e.to_string()))?;
            if !meta.file_type().is_file() {
                return Err(ApiError::vault_path_escape(format!(
                    "Save path is not a regular file: {}",
                    file_path.display()
                )));
            }
            let size = meta.len();
            let mtime = meta
                .modified()
                .ok()
                .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
                .map(|d| d.as_secs() as i64)
                .unwrap_or(0);
            let mode = file_mode_bits(&meta);
            total_bytes = total_bytes.saturating_add(size);
            if collected.len() >= MAX_SNAPSHOT_FILES {
                return Err(ApiError::vault_limit_exceeded(format!(
                    "Save Vault snapshot exceeds {MAX_SNAPSHOT_FILES} files."
                )));
            }
            if total_bytes > MAX_SNAPSHOT_BYTES {
                return Err(ApiError::vault_limit_exceeded(
                    "Save Vault snapshot exceeds the 512 MiB per-snapshot limit.",
                ));
            }
            collected.push(CollectedFile {
                root_idx,
                source_path: file_path,
                archive_rel: format!("roots/{root_idx}/{rel}"),
                relpath: rel,
                size,
                mtime,
                mode,
            });
        }
    }

    if collected.is_empty() {
        return Err(ApiError::vault_no_saves(
            "Save directories exist, but contain no regular save files.",
        ));
    }
    Ok(collected)
}

fn write_index_locked(vault_dir: &Path, index: &Value) -> Result<(), ApiError> {
    fs::create_dir_all(vault_dir).map_err(|e| ApiError::internal(e.to_string()))?;
    set_mode(vault_dir, 0o700);
    let payload = json!({
        "format": INDEX_FORMAT,
        "last_auto_at": index.get("last_auto_at").and_then(Value::as_str).unwrap_or(""),
        "snapshots": index.get("snapshots").and_then(Value::as_array).cloned().unwrap_or_default(),
    });
    let text = serde_json::to_string_pretty(&payload)
        .map_err(|e| ApiError::internal(e.to_string()))?
        + "\n";
    atomic_write_text(&vault_dir.join(INDEX_FILE_NAME), &text, 0o600)
        .map_err(|e| ApiError::internal(e.to_string()))?;
    Ok(())
}

fn sort_entries_desc(entries: &mut [Value]) {
    entries.sort_by(|a, b| {
        let ka = (
            a.get("created_at").and_then(Value::as_str).unwrap_or(""),
            a.get("folder").and_then(Value::as_str).unwrap_or(""),
        );
        let kb = (
            b.get("created_at").and_then(Value::as_str).unwrap_or(""),
            b.get("folder").and_then(Value::as_str).unwrap_or(""),
        );
        kb.cmp(&ka)
    });
}

pub fn rebuild_index(data_dir: &Path, last_auto_at: &str) -> Value {
    let vault_dir = vault_dir_for(data_dir);
    let mut entries: Vec<Value> = Vec::new();
    if vault_dir.is_dir()
        && let Ok(g_rd) = fs::read_dir(&vault_dir)
    {
        let mut game_dirs: Vec<PathBuf> = g_rd.flatten().map(|e| e.path()).collect();
        game_dirs.sort();
        for game_dir in game_dirs {
            let gname = game_dir.file_name().and_then(|s| s.to_str()).unwrap_or("");
            if gname.starts_with('.')
                || !game_dir.is_dir()
                || game_dir
                    .symlink_metadata()
                    .map(|m| m.file_type().is_symlink())
                    .unwrap_or(true)
            {
                continue;
            }
            let Ok(s_rd) = fs::read_dir(&game_dir) else {
                continue;
            };
            let mut snap_dirs: Vec<PathBuf> = s_rd.flatten().map(|e| e.path()).collect();
            snap_dirs.sort();
            for snap_dir in snap_dirs {
                let sname = snap_dir.file_name().and_then(|s| s.to_str()).unwrap_or("");
                if sname.starts_with('.')
                    || !snap_dir.is_dir()
                    || snap_dir
                        .symlink_metadata()
                        .map(|m| m.file_type().is_symlink())
                        .unwrap_or(true)
                {
                    continue;
                }
                let manifest_path = snap_dir.join(SNAPSHOT_FILE_NAME);
                let Ok(bytes) = fs::read(&manifest_path) else {
                    continue;
                };
                let Ok(raw) = serde_json::from_slice::<Value>(&bytes) else {
                    continue;
                };
                let Some(id) = raw
                    .get("id")
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
                else {
                    continue;
                };
                let mut h = Sha256::new();
                h.update(&bytes);
                let digest = hex::encode(h.finalize());
                let trigger = raw
                    .get("trigger")
                    .and_then(Value::as_str)
                    .unwrap_or("manual");
                entries.push(json!({
                    "id": id,
                    "game_id": raw.get("game_id").and_then(Value::as_str).unwrap_or(""),
                    "game_key": gname,
                    "folder": sname,
                    "game_title": raw.get("game_title").and_then(Value::as_str).unwrap_or(""),
                    "created_at": raw.get("created_at").and_then(Value::as_str).unwrap_or(""),
                    "trigger": trigger,
                    "files": raw.get("files").and_then(Value::as_array).map(Vec::len).unwrap_or(0),
                    "bytes": raw.get("bytes").and_then(Value::as_u64).unwrap_or(0),
                    "sha256": digest,
                    "exempt_from_prune": trigger == "pre-restore",
                }));
            }
        }
    }
    sort_entries_desc(&mut entries);
    let rebuilt = json!({
        "format": INDEX_FORMAT,
        "last_auto_at": last_auto_at,
        "snapshots": entries,
    });
    let _guard = VAULT_LOCK.lock();
    let _ = write_index_locked(&vault_dir, &rebuilt);
    rebuilt
}

pub fn load_index(data_dir: &Path) -> Value {
    let vault_dir = vault_dir_for(data_dir);
    let index_path = vault_dir.join(INDEX_FILE_NAME);
    let _guard = VAULT_LOCK.lock();
    if !index_path.is_file() {
        let has_game_dirs = vault_dir.is_dir()
            && fs::read_dir(&vault_dir)
                .ok()
                .into_iter()
                .flatten()
                .flatten()
                .any(|e| {
                    let p = e.path();
                    p.is_dir()
                        && !p
                            .file_name()
                            .and_then(|s| s.to_str())
                            .unwrap_or("")
                            .starts_with('.')
                });
        if has_game_dirs {
            return rebuild_index(data_dir, "");
        }
        return empty_index();
    }
    let Ok(raw_text) = fs::read_to_string(&index_path) else {
        return rebuild_index(data_dir, "");
    };
    let Ok(raw) = serde_json::from_str::<Value>(&raw_text) else {
        return rebuild_index(data_dir, "");
    };
    let Some(snaps) = raw.get("snapshots").and_then(Value::as_array) else {
        return rebuild_index(data_dir, "");
    };
    let valid: Vec<Value> = snaps
        .iter()
        .filter(|s| {
            s.is_object()
                && s.get("id")
                    .and_then(Value::as_str)
                    .is_some_and(|x| !x.is_empty())
        })
        .cloned()
        .collect();
    json!({
        "format": raw.get("format").and_then(Value::as_i64).unwrap_or(INDEX_FORMAT),
        "last_auto_at": raw.get("last_auto_at").and_then(Value::as_str).unwrap_or(""),
        "snapshots": valid,
    })
}

fn copy_with_sha256(src: &Path, dest: &Path) -> Result<String, ApiError> {
    if let Some(parent) = dest.parent() {
        fs::create_dir_all(parent).map_err(|e| ApiError::internal(e.to_string()))?;
        set_mode(parent, 0o700);
    }
    let mut opts = OpenOptions::new();
    opts.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.custom_flags(crate::saves::libc_o_nofollow());
    }
    let mut reader = opts
        .open(src)
        .map_err(|e| ApiError::vault_path_escape(e.to_string()))?;
    let meta = reader
        .metadata()
        .map_err(|e| ApiError::vault_path_escape(e.to_string()))?;
    if !meta.file_type().is_file() {
        return Err(ApiError::vault_path_escape(format!(
            "Save path is not a regular file: {}",
            src.display()
        )));
    }
    let mut out = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .open(dest)
        .map_err(|e| ApiError::internal(e.to_string()))?;
    set_mode(dest, 0o600);
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; 1024 * 1024];
    loop {
        let n = reader
            .read(&mut buf)
            .map_err(|e| ApiError::internal(e.to_string()))?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
        out.write_all(&buf[..n])
            .map_err(|e| ApiError::internal(e.to_string()))?;
    }
    out.flush().map_err(|e| ApiError::internal(e.to_string()))?;
    out.sync_all()
        .map_err(|e| ApiError::internal(e.to_string()))?;
    Ok(hex::encode(hasher.finalize()))
}

fn prune_game_snapshots_locked(
    vault_dir: &Path,
    index: &mut Value,
    game_key: &str,
    keep: usize,
) -> usize {
    let keep = clamp_keep(Some(keep as i64), DEFAULT_KEEP);
    let all_rows = index
        .get("snapshots")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut prunable: Vec<Value> = all_rows
        .iter()
        .filter(|row| {
            row.get("game_key").and_then(Value::as_str) == Some(game_key)
                && !row
                    .get("exempt_from_prune")
                    .and_then(Value::as_bool)
                    .unwrap_or(false)
                && row.get("trigger").and_then(Value::as_str) != Some("pre-restore")
        })
        .cloned()
        .collect();
    sort_entries_desc(&mut prunable);
    if prunable.len() <= keep {
        return 0;
    }
    let to_drop = &prunable[keep..];
    let drop_ids: HashSet<String> = to_drop
        .iter()
        .filter_map(|r| r.get("id").and_then(Value::as_str).map(str::to_owned))
        .collect();
    for row in to_drop {
        if let Some(folder) = row
            .get("folder")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            && let Ok(snap_dir) = safe_snapshot_dir(vault_dir, game_key, folder)
        {
            let _ = fs::remove_dir_all(&snap_dir);
        }
    }
    let kept: Vec<Value> = all_rows
        .into_iter()
        .filter(|r| {
            r.get("id")
                .and_then(Value::as_str)
                .is_none_or(|id| !drop_ids.contains(id))
        })
        .collect();
    if let Some(obj) = index.as_object_mut() {
        obj.insert("snapshots".into(), Value::Array(kept));
    }
    drop_ids.len()
}

pub fn create_snapshot(
    data_dir: &Path,
    game: &Value,
    trigger: &str,
    include_shared: Option<bool>,
    home: Option<&Path>,
    keep: Option<usize>,
    mut progress_cb: Option<&mut dyn FnMut(usize, usize)>,
) -> Result<Value, ApiError> {
    let trig = match trigger {
        "manual" | "session_close" | "auto" | "pre-restore" => trigger,
        _ => "manual",
    };
    let inc_shared = include_shared.unwrap_or(matches!(trig, "manual" | "pre-restore"));
    let vault_dir = vault_dir_for(data_dir);
    let game_key = game_vault_key(game);
    let snap_id = new_snapshot_id();
    let now = Timestamp::now();
    let created_at = utc_iso(now);

    let _sync_guard = SAVE_SYNC_LOCK.lock();
    let _vault_guard = VAULT_LOCK.lock();

    let roots = resolve_game_save_roots(game, inc_shared, home)?;
    let items = collect_root_files(&roots)?;
    let seq = SNAPSHOT_SEQ.fetch_add(1, Ordering::Relaxed) + 1;
    let suffix = &snap_id[snap_id.len().saturating_sub(4)..];
    let folder_name = format!("{}_{:04}_{}", utc_compact(now), seq, suffix);
    let game_dir = vault_dir.join(&game_key);
    fs::create_dir_all(&game_dir).map_err(|e| ApiError::internal(e.to_string()))?;
    set_mode(&game_dir, 0o700);
    let staging_dir = game_dir.join(format!(".staging-{snap_id}"));
    let final_dir = safe_snapshot_dir(&vault_dir, &game_key, &folder_name)?;

    let res = (|| -> Result<Value, ApiError> {
        fs::create_dir(&staging_dir).map_err(|e| ApiError::internal(e.to_string()))?;
        set_mode(&staging_dir, 0o700);
        let mut manifest_files = Vec::with_capacity(items.len());
        let mut total_bytes: u64 = 0;
        let total_items = items.len();
        for (idx, item) in items.iter().enumerate() {
            let dest = staging_dir.join(&item.archive_rel);
            let digest = copy_with_sha256(&item.source_path, &dest)?;
            total_bytes = total_bytes.saturating_add(item.size);
            manifest_files.push(json!({
                "root": item.root_idx,
                "relpath": item.relpath,
                "archive_rel": item.archive_rel,
                "size": item.size,
                "mtime": item.mtime,
                "mode": item.mode,
                "sha256": digest,
            }));
            if let Some(cb) = progress_cb.as_deref_mut() {
                cb(idx + 1, total_items);
            }
        }

        let game_id = game.get("game_id").and_then(Value::as_str).unwrap_or("");
        let game_title = game
            .get("name")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .unwrap_or("Untitled");
        let manifest = json!({
            "format": SNAPSHOT_FORMAT,
            "id": snap_id,
            "game_id": game_id,
            "game_title": game_title,
            "created_at": created_at,
            "trigger": trig,
            "roots": roots,
            "files": manifest_files,
            "bytes": total_bytes,
        });
        let manifest_text = serde_json::to_string_pretty(&manifest)
            .map_err(|e| ApiError::internal(e.to_string()))?
            + "\n";
        let mut h = Sha256::new();
        h.update(manifest_text.as_bytes());
        let manifest_sha = hex::encode(h.finalize());
        atomic_write_text(&staging_dir.join(SNAPSHOT_FILE_NAME), &manifest_text, 0o600)
            .map_err(|e| ApiError::internal(e.to_string()))?;
        fsync_directory(&staging_dir);
        fs::rename(&staging_dir, &final_dir).map_err(|e| ApiError::internal(e.to_string()))?;
        fsync_directory(&game_dir);

        let mut index = load_index(data_dir);
        let entry = json!({
            "id": snap_id,
            "game_id": game_id,
            "game_key": game_key,
            "folder": folder_name,
            "game_title": game_title,
            "created_at": created_at,
            "trigger": trig,
            "files": manifest_files.len(),
            "bytes": total_bytes,
            "sha256": manifest_sha,
            "exempt_from_prune": trig == "pre-restore",
        });
        let mut snaps = vec![entry.clone()];
        if let Some(existing) = index.get("snapshots").and_then(Value::as_array) {
            for row in existing {
                if row.get("id").and_then(Value::as_str) != Some(snap_id.as_str()) {
                    snaps.push(row.clone());
                }
            }
        }
        if let Some(obj) = index.as_object_mut() {
            obj.insert("snapshots".into(), Value::Array(snaps));
        }
        let pruned = if let Some(k) = keep
            && trig != "pre-restore"
        {
            prune_game_snapshots_locked(&vault_dir, &mut index, &game_key, k)
        } else {
            0
        };
        write_index_locked(&vault_dir, &index)?;
        Ok(json!({
            "snapshot_id": snap_id,
            "game_id": game_id,
            "game_key": game_key,
            "created_at": created_at,
            "trigger": trig,
            "files": manifest_files.len(),
            "bytes": total_bytes,
            "pruned": pruned,
            "snapshot": entry,
        }))
    })();
    if staging_dir.exists() {
        let _ = fs::remove_dir_all(&staging_dir);
    }
    res
}

pub fn list_snapshots(data_dir: &Path, game_id: &str) -> Vec<Value> {
    let index = load_index(data_dir);
    let mut rows = index
        .get("snapshots")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let wanted = game_id.trim();
    if !wanted.is_empty() {
        rows.retain(|r| r.get("game_id").and_then(Value::as_str) == Some(wanted));
    }
    sort_entries_desc(&mut rows);
    rows
}

pub fn vault_summary_readonly(data_dir: &Path) -> Value {
    let index_path = vault_dir_for(data_dir).join(INDEX_FILE_NAME);
    let Ok(raw_text) = fs::read_to_string(&index_path) else {
        return json!({"snapshots": 0, "bytes": 0, "games": 0});
    };
    let Ok(raw) = serde_json::from_str::<Value>(&raw_text) else {
        return json!({"snapshots": 0, "bytes": 0, "games": 0});
    };
    let Some(rows) = raw.get("snapshots").and_then(Value::as_array) else {
        return json!({"snapshots": 0, "bytes": 0, "games": 0});
    };
    let mut count = 0usize;
    let mut bytes = 0u64;
    let mut games: HashSet<String> = HashSet::new();
    for row in rows {
        if row
            .get("id")
            .and_then(Value::as_str)
            .is_some_and(|s| !s.is_empty())
        {
            count += 1;
            bytes = bytes.saturating_add(row.get("bytes").and_then(Value::as_u64).unwrap_or(0));
            if let Some(gk) = row.get("game_key").and_then(Value::as_str)
                && !gk.is_empty()
            {
                games.insert(gk.to_owned());
            }
        }
    }
    json!({
        "snapshots": count,
        "bytes": bytes,
        "games": games.len(),
    })
}

pub fn vault_status(data_dir: &Path, game_id: &str, settings: Option<&Value>) -> Value {
    let rows = list_snapshots(data_dir, "");
    let total_bytes: u64 = rows
        .iter()
        .map(|r| r.get("bytes").and_then(Value::as_u64).unwrap_or(0))
        .sum();
    let games_count: HashSet<&str> = rows
        .iter()
        .filter_map(|r| r.get("game_key").and_then(Value::as_str))
        .filter(|s| !s.is_empty())
        .collect();
    let wanted = game_id.trim();
    let game_rows: Vec<&Value> = if !wanted.is_empty() {
        rows.iter()
            .filter(|r| r.get("game_id").and_then(Value::as_str) == Some(wanted))
            .collect()
    } else {
        Vec::new()
    };
    let game_bytes: u64 = game_rows
        .iter()
        .map(|r| r.get("bytes").and_then(Value::as_u64).unwrap_or(0))
        .sum();
    let cfg = settings.and_then(Value::as_object);
    let enabled = cfg
        .and_then(|c| c.get("save_vault_enabled"))
        .and_then(Value::as_bool)
        .unwrap_or(true);
    let keep = clamp_keep(
        cfg.and_then(|c| c.get("save_vault_keep"))
            .and_then(Value::as_i64),
        DEFAULT_KEEP,
    );
    let on_close = cfg
        .and_then(|c| c.get("save_vault_on_close"))
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let auto_days = cfg
        .and_then(|c| c.get("save_vault_auto_days"))
        .and_then(Value::as_i64)
        .unwrap_or(0);

    json!({
        "snapshots": rows.len(),
        "bytes": total_bytes,
        "games": games_count.len(),
        "over_5gb_warning": total_bytes >= SIZE_WARNING_BYTES,
        "warning_threshold_bytes": SIZE_WARNING_BYTES,
        "game_snapshots": game_rows.len(),
        "game_bytes": game_bytes,
        "settings": {
            "save_vault_enabled": enabled,
            "save_vault_keep": keep,
            "save_vault_on_close": on_close,
            "save_vault_auto_days": auto_days,
        },
    })
}

fn locate_snapshot(
    data_dir: &Path,
    game: Option<&Value>,
    snapshot_id: &str,
) -> Result<(Value, PathBuf, Value), ApiError> {
    let vault_dir = vault_dir_for(data_dir);
    let index = load_index(data_dir);
    let entry = index
        .get("snapshots")
        .and_then(Value::as_array)
        .and_then(|arr| {
            arr.iter()
                .find(|r| r.get("id").and_then(Value::as_str) == Some(snapshot_id))
        })
        .cloned()
        .ok_or_else(|| {
            ApiError::vault_snapshot_missing(format!(
                "Save Vault snapshot not found: {snapshot_id}"
            ))
        })?;
    if let Some(g) = game {
        let gid = g
            .get("game_id")
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or("");
        if !gid.is_empty() && entry.get("game_id").and_then(Value::as_str).unwrap_or("") != gid {
            return Err(ApiError::vault_game_mismatch(
                "Save Vault snapshot belongs to a different game.",
            ));
        }
    }
    let game_key = entry.get("game_key").and_then(Value::as_str).unwrap_or("");
    let folder = entry.get("folder").and_then(Value::as_str).unwrap_or("");
    let snap_dir = safe_snapshot_dir(&vault_dir, game_key, folder)?;
    let manifest_path = snap_dir.join(SNAPSHOT_FILE_NAME);
    if !manifest_path.is_file() {
        return Err(ApiError::vault_snapshot_missing(format!(
            "Snapshot manifest missing on disk: {snapshot_id}"
        )));
    }
    let raw = fs::read_to_string(&manifest_path).map_err(|_| {
        ApiError::vault_snapshot_missing(format!("Snapshot manifest unreadable: {snapshot_id}"))
    })?;
    let manifest: Value = serde_json::from_str(&raw).map_err(|_| {
        ApiError::vault_snapshot_missing(format!("Snapshot manifest unreadable: {snapshot_id}"))
    })?;
    Ok((entry, snap_dir, manifest))
}

pub fn diff_snapshot(data_dir: &Path, game: &Value, snapshot_id: &str) -> Result<Value, ApiError> {
    let (_entry, _snap_dir, manifest) = locate_snapshot(data_dir, Some(game), snapshot_id)?;
    let mut roots: HashMap<i64, &Value> = HashMap::new();
    if let Some(arr) = manifest.get("roots").and_then(Value::as_array) {
        for r in arr {
            if let Some(idx) = r.get("index").and_then(Value::as_i64) {
                roots.insert(idx, r);
            }
        }
    }

    let mut added = Vec::new();
    let mut changed = Vec::new();
    let mut unchanged = Vec::new();
    let mut removed = Vec::new();
    let mut expected_targets: HashSet<PathBuf> = HashSet::new();

    if let Some(files) = manifest.get("files").and_then(Value::as_array) {
        for item in files {
            let root_idx = item.get("root").and_then(Value::as_i64).unwrap_or(0);
            let Some(root_info) = roots.get(&root_idx) else {
                continue;
            };
            let root_path =
                PathBuf::from(root_info.get("path").and_then(Value::as_str).unwrap_or(""));
            let relpath = item.get("relpath").and_then(Value::as_str).unwrap_or("");
            let is_file = root_info
                .get("is_file")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            let target = resolve_contained_target(&root_path, relpath, is_file)?;
            expected_targets.insert(target.clone());
            let row_info = json!({
                "path": target.to_string_lossy(),
                "relpath": relpath,
                "size": item.get("size").and_then(Value::as_u64).unwrap_or(0),
            });
            if !target.exists() {
                added.push(row_info);
                continue;
            }
            let live_hash =
                file_sha256(&target).map_err(|e| ApiError::vault_path_escape(e.to_string()))?;
            if item.get("sha256").and_then(Value::as_str) == Some(live_hash.as_str()) {
                unchanged.push(row_info);
            } else {
                changed.push(row_info);
            }
        }
    }

    for root_info in roots.values() {
        let root_path = PathBuf::from(root_info.get("path").and_then(Value::as_str).unwrap_or(""));
        if !root_path.exists() {
            continue;
        }
        reject_symlinks(&root_path)?;
        if root_info
            .get("is_file")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            let res = resolve_non_strict(&root_path);
            if !expected_targets.contains(&res) && root_path.is_file() {
                let sz = fs::metadata(&root_path).map(|m| m.len()).unwrap_or(0);
                removed.push(json!({
                    "path": root_path.to_string_lossy(),
                    "relpath": root_path.file_name().and_then(|s| s.to_str()).unwrap_or(""),
                    "size": sz,
                }));
            }
        } else {
            let mut stack = vec![root_path.clone()];
            let mut live_files = Vec::new();
            while let Some(dir) = stack.pop() {
                let Ok(rd) = fs::read_dir(&dir) else {
                    continue;
                };
                for e in rd.flatten() {
                    let p = e.path();
                    let meta = fs::symlink_metadata(&p)
                        .map_err(|err| ApiError::vault_path_escape(err.to_string()))?;
                    if meta.file_type().is_symlink() {
                        return Err(ApiError::vault_path_escape(format!(
                            "Live save directory contains symlink: {}",
                            p.display()
                        )));
                    }
                    if meta.is_dir() {
                        stack.push(p);
                    } else if meta.is_file() {
                        live_files.push((p, meta.len()));
                    }
                }
            }
            live_files.sort_by(|a, b| a.0.cmp(&b.0));
            for (live, sz) in live_files {
                let res = resolve_non_strict(&live);
                if !expected_targets.contains(&res) {
                    let rel = live
                        .strip_prefix(&root_path)
                        .ok()
                        .map(|p| p.to_string_lossy().replace('\\', "/"))
                        .unwrap_or_default();
                    removed.push(json!({
                        "path": live.to_string_lossy(),
                        "relpath": rel,
                        "size": sz,
                    }));
                }
            }
        }
    }

    Ok(json!({
        "snapshot_id": snapshot_id,
        "counts": {
            "added": added.len(),
            "changed": changed.len(),
            "removed": removed.len(),
            "unchanged": unchanged.len(),
        },
        "added": &added[..added.len().min(PREVIEW_BUCKET_LIMIT)],
        "changed": &changed[..changed.len().min(PREVIEW_BUCKET_LIMIT)],
        "removed": &removed[..removed.len().min(PREVIEW_BUCKET_LIMIT)],
        "unchanged": &unchanged[..unchanged.len().min(PREVIEW_BUCKET_LIMIT)],
    }))
}

fn any_live_files(roots: &HashMap<i64, &Value>) -> bool {
    for root_info in roots.values() {
        let root_path = PathBuf::from(root_info.get("path").and_then(Value::as_str).unwrap_or(""));
        if !root_path.exists() {
            continue;
        }
        if root_info
            .get("is_file")
            .and_then(Value::as_bool)
            .unwrap_or(false)
            && root_path.is_file()
        {
            return true;
        }
        if root_path.is_dir() {
            let mut stack = vec![root_path];
            while let Some(dir) = stack.pop() {
                if let Ok(rd) = fs::read_dir(&dir) {
                    for e in rd.flatten() {
                        let p = e.path();
                        if p.is_file() {
                            return true;
                        }
                        if p.is_dir() {
                            stack.push(p);
                        }
                    }
                }
            }
        }
    }
    false
}

pub fn restore_snapshot(
    data_dir: &Path,
    game: &Value,
    snapshot_id: &str,
    dry_run: bool,
    skip_safety: bool,
    include_removals: bool,
    mut progress_cb: Option<&mut dyn FnMut(usize, usize)>,
) -> Result<Value, ApiError> {
    let _sync_guard = SAVE_SYNC_LOCK.lock();
    let _vault_guard = VAULT_LOCK.lock();

    let (_entry, snap_dir, manifest) = locate_snapshot(data_dir, Some(game), snapshot_id)?;
    let mut roots: HashMap<i64, &Value> = HashMap::new();
    if let Some(arr) = manifest.get("roots").and_then(Value::as_array) {
        for r in arr {
            if let Some(idx) = r.get("index").and_then(Value::as_i64) {
                roots.insert(idx, r);
            }
        }
    }

    let snap_dir_res = resolve_non_strict(&snap_dir);
    let mut planned: Vec<(&Value, PathBuf, PathBuf)> = Vec::new();
    if let Some(files) = manifest.get("files").and_then(Value::as_array) {
        for item in files {
            let root_idx = item.get("root").and_then(Value::as_i64).unwrap_or(0);
            let Some(root_info) = roots.get(&root_idx) else {
                return Err(ApiError::vault_path_escape(format!(
                    "Snapshot references unknown root index: {root_idx}"
                )));
            };
            let root_path =
                PathBuf::from(root_info.get("path").and_then(Value::as_str).unwrap_or(""));
            let relpath = item.get("relpath").and_then(Value::as_str).unwrap_or("");
            let is_file = root_info
                .get("is_file")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            let dest = resolve_contained_target(&root_path, relpath, is_file)?;
            let archive_rel = item
                .get("archive_rel")
                .and_then(Value::as_str)
                .unwrap_or("");
            validate_relpath(archive_rel)?;
            let src = resolve_non_strict(&snap_dir.join(archive_rel));
            if !src.starts_with(&snap_dir_res) {
                return Err(ApiError::vault_path_escape(format!(
                    "Snapshot source path escapes vault: {archive_rel}"
                )));
            }
            planned.push((item, src, dest));
        }
    }

    let diff = diff_snapshot(data_dir, game, snapshot_id)?;
    if dry_run {
        let unch = diff
            .get("counts")
            .and_then(|c| c.get("unchanged"))
            .and_then(Value::as_u64)
            .unwrap_or(0);
        return Ok(json!({
            "snapshot_id": snapshot_id,
            "safety_snapshot_id": Value::Null,
            "dry_run": true,
            "diff": diff,
            "files": [],
            "counts": {"restored": 0, "unchanged": unch, "removed": 0, "error": 0},
        }));
    }

    let mut safety_snapshot_id: Option<String> = None;
    if !skip_safety && any_live_files(&roots) {
        match create_snapshot(data_dir, game, "pre-restore", Some(true), None, None, None) {
            Ok(safety) => {
                safety_snapshot_id = safety
                    .get("snapshot_id")
                    .and_then(Value::as_str)
                    .map(str::to_owned);
            }
            Err(err) if err.code == "VAULT_NO_SAVES" => {
                safety_snapshot_id = None;
            }
            Err(err) => {
                return Err(ApiError::vault_safety_snapshot_failed(format!(
                    "Pre-restore safety snapshot failed; live saves were left untouched: {}",
                    err.message
                )));
            }
        }
    }

    let mut results = Vec::new();
    let mut restored_cnt = 0usize;
    let mut unchanged_cnt = 0usize;
    let mut removed_cnt = 0usize;
    let mut error_cnt = 0usize;
    let total = planned.len();

    for (idx, (item, src, dest)) in planned.iter().enumerate() {
        if let Some(cb) = progress_cb.as_deref_mut() {
            cb(idx + 1, total);
        }
        let is_sym = src
            .symlink_metadata()
            .map(|m| m.file_type().is_symlink())
            .unwrap_or(true);
        if !src.is_file() || is_sym {
            error_cnt += 1;
            results.push(json!({
                "path": dest.to_string_lossy(),
                "status": "error",
                "error": "Snapshot file missing or invalid.",
            }));
            continue;
        }
        let res = (|| -> Result<bool, String> {
            let src_sha = file_sha256(src).map_err(|e| e.to_string())?;
            if item.get("sha256").and_then(Value::as_str) != Some(src_sha.as_str()) {
                return Err("Snapshot file checksum mismatch.".to_owned());
            }
            let dest_is_sym = dest
                .symlink_metadata()
                .map(|m| m.file_type().is_symlink())
                .unwrap_or(false);
            if dest.exists()
                && !dest_is_sym
                && dest.is_file()
                && file_sha256(dest).ok().as_deref() == Some(src_sha.as_str())
            {
                return Ok(false);
            }
            let parent = dest
                .parent()
                .ok_or_else(|| "Destination has no parent directory.".to_owned())?;
            fs::create_dir_all(parent).map_err(|e| e.to_string())?;
            let tmp = tempfile::Builder::new()
                .prefix(".vault-restore-")
                .suffix(".tmp")
                .tempfile_in(parent)
                .map_err(|e| e.to_string())?;
            let tmp_path = tmp.into_temp_path();
            let tmp_buf = tmp_path.to_path_buf();
            // Keep so we can overwrite & rename
            let kept = tmp_path.keep().map_err(|e| e.to_string())?;
            let copy_res = copy_with_sha256(src, &kept).map_err(|e| e.message);
            if let Err(e) = copy_res {
                let _ = fs::remove_file(&kept);
                return Err(e);
            }
            let raw_mode = item
                .get("mode")
                .and_then(Value::as_u64)
                .map(|m| (m as u32) & 0o777)
                .unwrap_or(0o600);
            set_mode(&kept, if raw_mode == 0 { 0o600 } else { raw_mode });
            if let Err(e) = fs::rename(&kept, dest) {
                let _ = fs::remove_file(&tmp_buf);
                return Err(e.to_string());
            }
            fsync_directory(parent);
            Ok(true)
        })();
        match res {
            Ok(true) => {
                restored_cnt += 1;
                results.push(json!({"path": dest.to_string_lossy(), "status": "restored"}));
            }
            Ok(false) => {
                unchanged_cnt += 1;
                results.push(json!({"path": dest.to_string_lossy(), "status": "unchanged"}));
            }
            Err(err) => {
                error_cnt += 1;
                results
                    .push(json!({"path": dest.to_string_lossy(), "status": "error", "error": err}));
            }
        }
    }

    if include_removals && let Some(rem) = diff.get("removed").and_then(Value::as_array) {
        for item in rem {
            let p = PathBuf::from(item.get("path").and_then(Value::as_str).unwrap_or(""));
            let is_sym = p
                .symlink_metadata()
                .map(|m| m.file_type().is_symlink())
                .unwrap_or(false);
            if p.is_file() && !is_sym {
                match fs::remove_file(&p) {
                    Ok(()) => {
                        removed_cnt += 1;
                        results.push(json!({"path": p.to_string_lossy(), "status": "removed"}));
                    }
                    Err(e) => {
                        error_cnt += 1;
                        results.push(json!({"path": p.to_string_lossy(), "status": "error", "error": e.to_string()}));
                    }
                }
            }
        }
    }

    Ok(json!({
        "snapshot_id": snapshot_id,
        "safety_snapshot_id": safety_snapshot_id,
        "dry_run": false,
        "files": results,
        "counts": {
            "restored": restored_cnt,
            "unchanged": unchanged_cnt,
            "removed": removed_cnt,
            "error": error_cnt,
        },
    }))
}

pub fn delete_snapshot(
    data_dir: &Path,
    game: Option<&Value>,
    snapshot_id: &str,
) -> Result<Value, ApiError> {
    let vault_dir = vault_dir_for(data_dir);
    let _guard = VAULT_LOCK.lock();
    let (entry, snap_dir, _manifest) = locate_snapshot(data_dir, game, snapshot_id)?;
    fs::remove_dir_all(&snap_dir).map_err(|e| ApiError::internal(e.to_string()))?;
    let mut index = load_index(data_dir);
    if let Some(snaps) = index.get_mut("snapshots").and_then(Value::as_array_mut) {
        snaps.retain(|r| r.get("id").and_then(Value::as_str) != Some(snapshot_id));
    }
    write_index_locked(&vault_dir, &index)?;
    Ok(json!({
        "deleted": true,
        "snapshot_id": snapshot_id,
        "game_id": entry.get("game_id").and_then(Value::as_str).unwrap_or(""),
    }))
}

pub fn auto_vault_due(data_dir: &Path, settings: &Value) -> bool {
    let Some(cfg) = settings.as_object() else {
        return false;
    };
    if !cfg
        .get("save_management_enabled")
        .and_then(Value::as_bool)
        .unwrap_or(true)
        || !cfg
            .get("save_vault_enabled")
            .and_then(Value::as_bool)
            .unwrap_or(true)
    {
        return false;
    }
    let auto_days = cfg
        .get("save_vault_auto_days")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    if auto_days <= 0 {
        return false;
    }
    let index = load_index(data_dir);
    let raw = index
        .get("last_auto_at")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("");
    if raw.is_empty() {
        return true;
    }
    let Ok(last) = raw.parse::<Timestamp>() else {
        return true;
    };
    let elapsed = Timestamp::now().as_second() - last.as_second();
    elapsed >= auto_days * 86400
}

pub fn run_auto_vault_pass(
    data_dir: &Path,
    state: &Value,
    cancel_flag: Option<&AtomicBool>,
) -> Value {
    let settings = state.get("settings").cloned().unwrap_or_else(|| json!({}));
    let keep = clamp_keep(
        settings.get("save_vault_keep").and_then(Value::as_i64),
        DEFAULT_KEEP,
    );
    let games = state
        .get("games")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let mut snapshotted = 0usize;
    let mut skipped = 0usize;
    let mut errors: Vec<String> = Vec::new();

    for game in games {
        if cancel_flag.is_some_and(|c| c.load(Ordering::Relaxed)) {
            break;
        }
        if !game.is_object() || game.get("hidden").and_then(Value::as_bool).unwrap_or(false) {
            continue;
        }
        match create_snapshot(data_dir, game, "auto", Some(false), None, Some(keep), None) {
            Ok(_) => snapshotted += 1,
            Err(err) if err.code == "VAULT_NO_SAVES" => skipped += 1,
            Err(err) => {
                let name = game.get("name").and_then(Value::as_str).unwrap_or("game");
                errors.push(format!("{name}: {}", err.message));
            }
        }
    }

    {
        let _guard = VAULT_LOCK.lock();
        let mut index = load_index(data_dir);
        if let Some(obj) = index.as_object_mut() {
            obj.insert(
                "last_auto_at".into(),
                Value::String(utc_iso(Timestamp::now())),
            );
        }
        let _ = write_index_locked(&vault_dir_for(data_dir), &index);
    }

    let mut out = Map::new();
    out.insert("snapshotted".into(), json!(snapshotted));
    out.insert("skipped".into(), json!(skipped));
    out.insert(
        "errors".into(),
        Value::Array(errors.into_iter().take(20).map(Value::String).collect()),
    );
    Value::Object(out)
}
