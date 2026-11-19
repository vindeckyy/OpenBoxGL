//! Granular library backups (Format 2 writer, Formats 1 & 2 reader), rotation, restore, and diff (`pkg/parity/parity_backup.py`).

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Cursor, Read, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::LazyLock;
use std::time::UNIX_EPOCH;

use jiff::Timestamp;
use openbox_core::{
    APP_VERSION, ApiError, STATE_SCHEMA_VERSION, default_state, merge_preserved_credentials,
    redact_settings, redact_state_for_export,
};
use openbox_io::{atomic_copy_stream, atomic_write_text, fsync_directory};
use openbox_platform::user_symlink_component;
use regex::Regex;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use zip::write::SimpleFileOptions;
use zip::{CompressionMethod, ZipArchive, ZipWriter};

use crate::saves::{file_mode_bits, resolve_non_strict, set_mode};

pub const BACKUP_FORMAT_VERSION: i64 = 2;
pub const READABLE_BACKUP_FORMATS: &[i64] = &[1, 2];
pub const WRITABLE_BACKUP_FORMATS: &[i64] = &[2];
pub const MAX_BACKUP_MEMBERS: usize = 50_000;
pub const MAX_BACKUP_MEMBER_BYTES: u64 = 4 * 1024 * 1024 * 1024;
pub const MAX_BACKUP_TOTAL_BYTES: u64 = 32 * 1024 * 1024 * 1024;
pub const AUTO_BACKUP_DAYS: i64 = 7;
pub const AUTO_BACKUP_KEEP: usize = 4;
pub const AUTO_BACKUP_ITEMS: &[&str] = &["library", "settings", "media", "plugins", "themes"];
pub const DIFF_PREVIEW_LIMIT: usize = 200;

pub const DIFF_FIELDS: &[&str] = &[
    "name",
    "title",
    "platform",
    "genre",
    "rating",
    "progress",
    "favorite",
    "hidden",
    "play_count",
    "playtime_seconds",
    "price_paid_cents",
    "price_currency",
];

static SHA256_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[0-9a-f]{64}$").unwrap());

pub fn backup_item_path(key: &str) -> Option<&'static str> {
    match key {
        "settings" => Some("settings.json"),
        "library" => Some("library.json"),
        "media" => Some("media"),
        "plugins" => Some("plugins"),
        "themes" => Some("themes"),
        "extension_data" => Some("extension-data"),
        _ => None,
    }
}

pub fn auto_backup_due(settings: &Value) -> bool {
    let Some(cfg) = settings.as_object() else {
        return false;
    };
    if !cfg
        .get("backup_auto_enabled")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        return false;
    }
    let raw = cfg
        .get("last_auto_backup")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("");
    if raw.is_empty() {
        return true;
    }
    let parsed = parse_iso_timestamp(raw);
    let Some(last) = parsed else {
        return true;
    };
    (Timestamp::now().as_second() - last.as_second()) >= AUTO_BACKUP_DAYS * 86400
}

fn parse_iso_timestamp(raw: &str) -> Option<Timestamp> {
    if let Ok(ts) = raw.parse::<Timestamp>() {
        return Some(ts);
    }
    if let Ok(dt) = raw.parse::<jiff::civil::DateTime>() {
        return jiff::tz::TimeZone::UTC.to_timestamp(dt).ok();
    }
    None
}

pub fn normalize_items(items: Option<&[String]>) -> Result<Vec<String>, ApiError> {
    let Some(list) = items else {
        return Ok(vec!["library".to_owned(), "settings".to_owned()]);
    };
    if list.is_empty() {
        return Ok(vec!["library".to_owned(), "settings".to_owned()]);
    }
    let mut clean = Vec::new();
    for item in list {
        for part in item.split(',') {
            let key = part.trim().to_lowercase();
            if backup_item_path(&key).is_some() && !clean.contains(&key) {
                clean.push(key);
            }
        }
    }
    if clean.is_empty() {
        return Err(ApiError::bad_request("Select at least one backup item."));
    }
    Ok(clean)
}

pub fn backup_path(data_dir: &Path, prefix: &str) -> PathBuf {
    let now = Timestamp::now();
    let dt = jiff::tz::TimeZone::UTC.to_datetime(now);
    let micros = now.subsec_nanosecond() / 1000;
    let stamp = format!(
        "{:04}-{:02}-{:02}-{:02}-{:02}-{:02}-{:06}",
        dt.year(),
        dt.month(),
        dt.day(),
        dt.hour(),
        dt.minute(),
        dt.second(),
        micros
    );
    data_dir
        .join("backups")
        .join(format!("{prefix}-{stamp}.zip"))
}

fn reject_symlink_components(path: &Path) -> Result<(), ApiError> {
    if let Some(comp) = user_symlink_component(path) {
        return Err(ApiError::bad_request(format!(
            "Backup path may not contain symlinks: {}",
            comp.display()
        )));
    }
    Ok(())
}

fn plugin_secret_names_from_manifest(manifest: &Value) -> HashSet<String> {
    let mut out = HashSet::new();
    if let Some(props) = manifest
        .get("settings")
        .and_then(|s| s.get("properties"))
        .and_then(Value::as_object)
    {
        for (name, field) in props {
            if field.get("format").and_then(Value::as_str) == Some("password") {
                out.insert(name.clone());
            }
        }
    }
    out
}

fn plugin_secrets(plugins_root: &Path, plugin_id: &str) -> HashSet<String> {
    let manifest_path = plugins_root.join(plugin_id).join("plugin.json");
    let Ok(raw) = fs::read_to_string(&manifest_path) else {
        return HashSet::new();
    };
    let Ok(val) = serde_json::from_str::<Value>(&raw) else {
        return HashSet::new();
    };
    plugin_secret_names_from_manifest(&val)
}

pub fn redact_plugin_state(payload: &Value, plugins_root: &Path) -> Value {
    let Some(obj) = payload.as_object() else {
        return json!({});
    };
    let mut cloned = Value::Object(obj.clone());
    if let Some(settings) = cloned.get_mut("settings").and_then(Value::as_object_mut) {
        for (plugin_id, values) in settings.iter_mut() {
            if let Some(vals) = values.as_object_mut() {
                for secret in plugin_secrets(plugins_root, plugin_id) {
                    if vals.contains_key(&secret) {
                        vals.insert(secret, Value::String(String::new()));
                    }
                }
            }
        }
    }
    cloned
}

pub fn merge_preserved_plugin_settings(
    restored: &Value,
    existing: &Value,
    plugins_root: &Path,
) -> Value {
    let mut out = if restored.is_object() {
        restored.clone()
    } else {
        json!({})
    };
    let Some(existing_settings) = existing.get("settings").and_then(Value::as_object) else {
        return out;
    };
    let Some(restored_settings) = out.get_mut("settings").and_then(Value::as_object_mut) else {
        return out;
    };
    for (plugin_id, values) in restored_settings.iter_mut() {
        let Some(vals) = values.as_object_mut() else {
            continue;
        };
        let Some(current) = existing_settings.get(plugin_id).and_then(Value::as_object) else {
            continue;
        };
        for secret in plugin_secrets(plugins_root, plugin_id) {
            let empty_restored = vals
                .get(&secret)
                .and_then(Value::as_str)
                .is_none_or(str::is_empty);
            if empty_restored
                && let Some(cur_val) = current.get(&secret)
                && cur_val.as_str().is_some_and(|s| !s.is_empty())
            {
                vals.insert(secret, cur_val.clone());
            }
        }
    }
    out
}

fn write_archive_file<W: Write + io::Seek>(
    package: &mut ZipWriter<W>,
    source: &Path,
    archive_name: &str,
) -> Result<(String, u64, String), ApiError> {
    let mut opts = OpenOptions::new();
    opts.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.custom_flags(crate::saves::libc_o_nofollow());
    }
    let mut reader = opts.open(source).map_err(|e| {
        ApiError::bad_request(format!(
            "Backup source could not be opened safely: {} ({e})",
            source.display()
        ))
    })?;
    let meta = reader
        .metadata()
        .map_err(|e| ApiError::internal(e.to_string()))?;
    if !meta.file_type().is_file() {
        return Err(ApiError::bad_request(format!(
            "Backup source is not a regular file: {}",
            source.display()
        )));
    }
    let mode_str = format!("{:04o}", file_mode_bits(&meta));
    let zip_opts = SimpleFileOptions::default().compression_method(CompressionMethod::Deflated);
    package
        .start_file(archive_name, zip_opts)
        .map_err(|e| ApiError::internal(e.to_string()))?;
    let mut hasher = Sha256::new();
    let mut size: u64 = 0;
    let mut buf = vec![0u8; 1024 * 1024];
    loop {
        let n = reader
            .read(&mut buf)
            .map_err(|e| ApiError::internal(e.to_string()))?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
        size += n as u64;
        package
            .write_all(&buf[..n])
            .map_err(|e| ApiError::internal(e.to_string()))?;
    }
    Ok((hex::encode(hasher.finalize()), size, mode_str))
}

fn write_private_archive<F>(archive: &Path, populate: F) -> Result<(), ApiError>
where
    F: FnOnce(&mut ZipWriter<&mut File>) -> Result<(), ApiError>,
{
    let parent = archive
        .parent()
        .ok_or_else(|| ApiError::internal("Backup archive has no parent."))?;
    reject_symlink_components(parent)?;
    fs::create_dir_all(parent).map_err(|e| ApiError::internal(e.to_string()))?;
    reject_symlink_components(parent)?;
    set_mode(parent, 0o700);
    if let Ok(rd) = fs::read_dir(parent) {
        for e in rd.flatten() {
            let p = e.path();
            if p.file_name()
                .and_then(|s| s.to_str())
                .is_some_and(|n| n.starts_with("OpenBoxBackup-") && n.ends_with(".zip"))
                && !p
                    .symlink_metadata()
                    .map(|m| m.file_type().is_symlink())
                    .unwrap_or(true)
            {
                set_mode(&p, 0o600);
            }
        }
    }
    let prefix = format!(
        ".{}.",
        archive
            .file_name()
            .and_then(|s| s.to_str())
            .unwrap_or("backup")
    );
    let tmp = tempfile::Builder::new()
        .prefix(&prefix)
        .suffix(".tmp")
        .tempfile_in(parent)
        .map_err(|e| ApiError::internal(e.to_string()))?;
    let (mut file, tmp_path) = tmp.keep().map_err(|e| ApiError::internal(e.to_string()))?;
    set_mode(&tmp_path, 0o600);

    let res = (|| -> Result<(), ApiError> {
        {
            let mut zip = ZipWriter::new(&mut file);
            populate(&mut zip)?;
            zip.finish()
                .map_err(|e| ApiError::internal(e.to_string()))?;
        }
        file.flush()
            .map_err(|e| ApiError::internal(e.to_string()))?;
        file.sync_all()
            .map_err(|e| ApiError::internal(e.to_string()))?;
        Ok(())
    })();
    drop(file);
    if let Err(err) = res {
        let _ = fs::remove_file(&tmp_path);
        return Err(err);
    }
    fs::rename(&tmp_path, archive).map_err(|e| {
        let _ = fs::remove_file(&tmp_path);
        ApiError::internal(e.to_string())
    })?;
    set_mode(archive, 0o600);
    fsync_directory(parent);
    Ok(())
}

pub fn create_backup(
    data_dir: &Path,
    state: &Value,
    items: Option<&[String]>,
    keep: usize,
    has_running_games: bool,
    format_version: i64,
) -> Result<PathBuf, ApiError> {
    if !WRITABLE_BACKUP_FORMATS.contains(&format_version) {
        if READABLE_BACKUP_FORMATS.contains(&format_version) {
            return Err(ApiError::backup_format_retired(format!(
                "Backup format {format_version} is no longer written by OpenBox 2.1. Use a 2.0.x build to create a 1.19-compatible export."
            ))
            .with_detail(json!({
                "retired": format_version,
                "writable": WRITABLE_BACKUP_FORMATS,
                "since": "2.1.0",
            })));
        }
        return Err(ApiError::backup_format_unsupported(format!(
            "Backup format {format_version} cannot be written."
        )));
    }
    if has_running_games {
        return Err(ApiError::bad_request(
            "Close running games before creating a backup.",
        ));
    }
    let selected = normalize_items(items)?;
    reject_symlink_components(data_dir)?;
    let archive = backup_path(data_dir, "OpenBoxBackup");
    let now = Timestamp::now();
    let dt = jiff::tz::TimeZone::UTC.to_datetime(now);
    let created = format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}",
        dt.year(),
        dt.month(),
        dt.day(),
        dt.hour(),
        dt.minute(),
        dt.second()
    );
    let mut manifest = Map::new();
    manifest.insert(
        "items".into(),
        Value::Array(selected.iter().cloned().map(Value::String).collect()),
    );
    manifest.insert("created".into(), Value::String(created));
    manifest.insert("redacted_secrets".into(), Value::Bool(true));
    if format_version == 2 {
        manifest.insert("format".into(), json!(BACKUP_FORMAT_VERSION));
        manifest.insert("app_version".into(), Value::String(APP_VERSION.to_owned()));
        manifest.insert("state_schema".into(), json!(STATE_SCHEMA_VERSION));
        manifest.insert(
            "library_id".into(),
            Value::String(
                state
                    .get("library_id")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned(),
            ),
        );
        manifest.insert("created_by".into(), Value::String("openbox".to_owned()));
        manifest.insert("encryption".into(), Value::String("none".to_owned()));
    }

    write_private_archive(&archive, |package| {
        let mut listed: Vec<Value> = Vec::new();
        let add_json = |pkg: &mut ZipWriter<&mut File>,
                        listed: &mut Vec<Value>,
                        name: &str,
                        payload: &Value|
         -> Result<(), ApiError> {
            let text = serde_json::to_string_pretty(payload)
                .map_err(|e| ApiError::internal(e.to_string()))?;
            let data = text.as_bytes();
            let zip_opts =
                SimpleFileOptions::default().compression_method(CompressionMethod::Deflated);
            pkg.start_file(name, zip_opts)
                .map_err(|e| ApiError::internal(e.to_string()))?;
            pkg.write_all(data)
                .map_err(|e| ApiError::internal(e.to_string()))?;
            let mut h = Sha256::new();
            h.update(data);
            listed.push(json!({
                "name": name,
                "size": data.len(),
                "mode": "0600",
                "sha256": hex::encode(h.finalize()),
            }));
            Ok(())
        };

        if selected.iter().any(|s| s == "settings") {
            let empty = Map::new();
            let settings_map = state
                .get("settings")
                .and_then(Value::as_object)
                .unwrap_or(&empty);
            let redacted = Value::Object(redact_settings(settings_map));
            add_json(package, &mut listed, "settings.json", &redacted)?;
        }
        if selected.iter().any(|s| s == "library") {
            let exported = redact_state_for_export(state);
            add_json(package, &mut listed, "library.json", &exported)?;
        }
        for key in ["media", "plugins", "themes", "extension_data"] {
            if !selected.iter().any(|s| s == key) {
                continue;
            }
            let Some(item_rel) = backup_item_path(key) else {
                continue;
            };
            let folder = data_dir.join(item_rel);
            reject_symlink_components(&folder)?;
            if !folder.exists() {
                continue;
            }
            if folder
                .symlink_metadata()
                .map(|m| m.file_type().is_symlink())
                .unwrap_or(false)
            {
                return Err(ApiError::bad_request(format!(
                    "Backup source is a symlink: {}",
                    folder.display()
                )));
            }
            let mut stack = vec![folder.clone()];
            let mut files = Vec::new();
            while let Some(dir) = stack.pop() {
                let Ok(rd) = fs::read_dir(&dir) else {
                    continue;
                };
                for e in rd.flatten() {
                    let p = e.path();
                    let rel = p.strip_prefix(&folder).unwrap_or(&p);
                    if key == "plugins"
                        && rel.components().next().and_then(|c| c.as_os_str().to_str())
                            == Some("credentials")
                    {
                        continue;
                    }
                    let meta = fs::symlink_metadata(&p)
                        .map_err(|err| ApiError::internal(err.to_string()))?;
                    if meta.file_type().is_symlink() {
                        return Err(ApiError::bad_request(format!(
                            "Backup source contains a symlink: {}",
                            p.display()
                        )));
                    }
                    if meta.is_dir() {
                        stack.push(p);
                    } else if meta.is_file() {
                        let rel_s = rel.to_string_lossy().replace('\\', "/");
                        files.push((p, rel_s));
                    }
                }
            }
            files.sort_by(|a, b| a.1.cmp(&b.1));
            for (path, rel_posix) in files {
                let member = format!("{item_rel}/{rel_posix}");
                if key == "plugins" && rel_posix == "plugins-state.json" {
                    let plugin_state: Value = fs::read_to_string(&path)
                        .ok()
                        .and_then(|s| serde_json::from_str(&s).ok())
                        .unwrap_or_else(|| json!({}));
                    let redacted = redact_plugin_state(&plugin_state, &folder);
                    add_json(package, &mut listed, &member, &redacted)?;
                    continue;
                }
                let (digest, size, mode) = write_archive_file(package, &path, &member)?;
                listed.push(json!({
                    "name": member,
                    "size": size,
                    "mode": mode,
                    "sha256": digest,
                }));
            }
        }
        if format_version == 2 {
            manifest.insert("files".into(), Value::Array(listed));
        }
        let manifest_text = serde_json::to_string_pretty(&Value::Object(manifest))
            .map_err(|e| ApiError::internal(e.to_string()))?;
        let zip_opts = SimpleFileOptions::default().compression_method(CompressionMethod::Deflated);
        package
            .start_file("manifest.json", zip_opts)
            .map_err(|e| ApiError::internal(e.to_string()))?;
        package
            .write_all(manifest_text.as_bytes())
            .map_err(|e| ApiError::internal(e.to_string()))?;
        Ok(())
    })?;

    if keep > 0 {
        rotate_backups(&data_dir.join("backups"), keep)?;
    }
    Ok(archive)
}

pub fn rotate_backups(folder: &Path, keep: usize) -> Result<(), ApiError> {
    if keep < 1 {
        return Err(ApiError::bad_request(
            "Backup retention must be at least 1.",
        ));
    }
    reject_symlink_components(folder)?;
    let Ok(rd) = fs::read_dir(folder) else {
        return Ok(());
    };
    let mut archives: Vec<(u64, PathBuf)> = rd
        .flatten()
        .map(|e| e.path())
        .filter(|p| {
            p.file_name()
                .and_then(|s| s.to_str())
                .is_some_and(|n| n.starts_with("OpenBoxBackup-") && n.ends_with(".zip"))
                && p.symlink_metadata()
                    .map(|m| m.is_file() && !m.file_type().is_symlink())
                    .unwrap_or(false)
        })
        .map(|p| {
            let mtime = fs::metadata(&p)
                .ok()
                .and_then(|m| m.modified().ok())
                .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
                .map(|d| d.as_secs())
                .unwrap_or(0);
            (mtime, p)
        })
        .collect();
    archives.sort_by_key(|a| a.0);
    let drop_n = archives.len().saturating_sub(keep);
    for (_, p) in archives.into_iter().take(drop_n) {
        let _ = fs::remove_file(&p);
    }
    Ok(())
}

fn validate_backup_entries(package: &mut ZipArchive<File>) -> Result<Vec<(String, u64)>, ApiError> {
    let count = package.len();
    if count > MAX_BACKUP_MEMBERS {
        return Err(ApiError::bad_request("Backup contains too many files."));
    }
    let mut total: u64 = 0;
    let mut seen: HashSet<String> = HashSet::new();
    let mut infos = Vec::with_capacity(count);
    for i in 0..count {
        let entry = package
            .by_index(i)
            .map_err(|_| ApiError::bad_request("Backup archive is invalid."))?;
        let name = entry
            .name()
            .map_err(|_| ApiError::bad_request("Backup archive is invalid."))?
            .replace('\\', "/");
        let p = Path::new(&name);
        let has_parent = p.components().any(|c| matches!(c, Component::ParentDir));
        if name.is_empty() || name.contains('\0') || p.is_absolute() || has_parent {
            return Err(ApiError::bad_request("Backup contains an unsafe path."));
        }
        let norm = p
            .components()
            .filter_map(|c| match c {
                Component::Normal(s) => Some(s.to_string_lossy().into_owned()),
                _ => None,
            })
            .collect::<Vec<_>>()
            .join("/");
        if !seen.insert(norm) {
            return Err(ApiError::bad_request("Backup contains duplicate entries."));
        }
        if let Some(unix_mode) = entry.unix_mode() {
            let ftype = unix_mode & 0o170000;
            if ftype == 0o120000 || ftype == 0o060000 {
                return Err(ApiError::bad_request("Backup links are not supported."));
            }
        }
        let sz = entry.size();
        if sz > MAX_BACKUP_MEMBER_BYTES {
            return Err(ApiError::bad_request("Backup member is too large."));
        }
        total = total.saturating_add(sz);
        if total > MAX_BACKUP_TOTAL_BYTES {
            return Err(ApiError::bad_request(
                "Backup expands beyond the allowed size.",
            ));
        }
        infos.push((name, sz));
    }
    Ok(infos)
}

fn load_backup_manifest(package: &mut ZipArchive<File>) -> Result<Value, ApiError> {
    let mut entry = match package.by_name("manifest.json") {
        Ok(e) => e,
        Err(_) => return Ok(json!({})),
    };
    let mut buf = Vec::new();
    entry
        .read_to_end(&mut buf)
        .map_err(|_| ApiError::bad_request("Backup manifest is invalid."))?;
    let val: Value = serde_json::from_slice(&buf)
        .map_err(|_| ApiError::bad_request("Backup manifest is invalid."))?;
    if !val.is_object() {
        return Err(ApiError::bad_request("Backup manifest is invalid."));
    }
    Ok(val)
}

fn verify_manifest_files(
    package: &mut ZipArchive<File>,
    manifest: &Value,
    infos: &[(String, u64)],
) -> Result<HashSet<String>, ApiError> {
    let declared = match manifest.get("format") {
        None => 1,
        Some(Value::Number(n)) => n
            .as_i64()
            .ok_or_else(|| ApiError::bad_request("Backup manifest format is invalid."))?,
        _ => return Err(ApiError::bad_request("Backup manifest format is invalid.")),
    };
    if declared < 1 {
        return Err(ApiError::bad_request("Backup manifest format is invalid."));
    }
    if declared > BACKUP_FORMAT_VERSION {
        return Err(ApiError::backup_format_unsupported(format!(
            "This backup uses format {declared}; this build reads formats 1 and {BACKUP_FORMAT_VERSION}. Install a newer OpenBox."
        )));
    }
    if declared < 2 {
        return Ok(HashSet::new());
    }
    let Some(listed) = manifest.get("files").and_then(Value::as_array) else {
        return Err(ApiError::bad_request("Backup manifest lists no files."));
    };
    let mut entries: BTreeMap<String, (u64, String)> = BTreeMap::new();
    for entry in listed {
        let Some(name) = entry.get("name").and_then(Value::as_str) else {
            return Err(ApiError::bad_request(
                "Backup manifest file entries are invalid.",
            ));
        };
        let sha = entry.get("sha256").and_then(Value::as_str).unwrap_or("");
        let sz = entry.get("size").and_then(Value::as_u64);
        if !SHA256_RE.is_match(sha) || sz.is_none() {
            return Err(ApiError::bad_request(format!(
                "Backup manifest has no valid hash for {name}."
            )));
        }
        entries.insert(name.to_owned(), (sz.unwrap(), sha.to_owned()));
    }
    let size_map: HashMap<&str, u64> = infos.iter().map(|(n, s)| (n.as_str(), *s)).collect();
    let members: HashSet<String> = infos
        .iter()
        .map(|(n, _)| n.clone())
        .filter(|n| n != "manifest.json" && !n.ends_with('/'))
        .collect();
    let mut unlisted: Vec<&String> = members
        .iter()
        .filter(|m| !entries.contains_key(*m))
        .collect();
    unlisted.sort();
    if let Some(first) = unlisted.first() {
        return Err(ApiError::bad_request(format!(
            "Backup contains files its manifest does not list: {first}"
        )));
    }
    let mut missing: Vec<&String> = entries.keys().filter(|k| !members.contains(*k)).collect();
    missing.sort();
    if let Some(first) = missing.first() {
        return Err(ApiError::bad_request(format!(
            "Backup manifest lists a file missing from the archive: {first}"
        )));
    }
    let mut verified = HashSet::new();
    for (name, (expected_sz, expected_sha)) in &entries {
        if size_map.get(name.as_str()).copied() != Some(*expected_sz) {
            return Err(ApiError::bad_request(format!(
                "Backup file {name} failed its size check; nothing was restored."
            )));
        }
        let mut entry = package
            .by_name(name)
            .map_err(|_| ApiError::bad_request("Backup archive is invalid."))?;
        let mut hasher = Sha256::new();
        let mut buf = vec![0u8; 1024 * 1024];
        loop {
            let n = entry
                .read(&mut buf)
                .map_err(|_| ApiError::bad_request("Backup archive is invalid."))?;
            if n == 0 {
                break;
            }
            hasher.update(&buf[..n]);
        }
        if hex::encode(hasher.finalize()) != *expected_sha {
            return Err(ApiError::bad_request(format!(
                "Backup file {name} failed its SHA-256 check; nothing was restored."
            )));
        }
        verified.insert(name.clone());
    }
    Ok(verified)
}

fn member_item(name: &str) -> &'static str {
    for key in [
        "settings",
        "library",
        "media",
        "plugins",
        "themes",
        "extension_data",
    ] {
        if let Some(rel) = backup_item_path(key) {
            let prefix = rel.trim_end_matches('/');
            if name == prefix || name.starts_with(&format!("{prefix}/")) {
                return key;
            }
        }
    }
    "other"
}

pub fn restore_backup(
    archive_path: &Path,
    data_dir: &Path,
    items: Option<&[String]>,
    has_running_games: bool,
    force: bool,
) -> Result<Value, ApiError> {
    if has_running_games {
        return Err(ApiError::bad_request(
            "Close running games before restoring a backup.",
        ));
    }
    let selected = match items {
        Some(list) if !list.is_empty() => Some(normalize_items(Some(list))?),
        _ => None,
    };
    reject_symlink_components(data_dir)?;
    reject_symlink_components(archive_path)?;
    let file = File::open(archive_path)
        .map_err(|_| ApiError::bad_request("Backup archive is invalid."))?;
    let mut package =
        ZipArchive::new(file).map_err(|_| ApiError::bad_request("Backup archive is invalid."))?;
    let infos = validate_backup_entries(&mut package)?;
    let manifest = load_backup_manifest(&mut package)?;
    let verified = verify_manifest_files(&mut package, &manifest, &infos)?;

    let manifest_items: Option<Vec<String>> =
        manifest.get("items").and_then(Value::as_array).map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect()
        });
    let restore_items = if let Some(s) = selected {
        s
    } else if let Some(m) = manifest_items.filter(|v| !v.is_empty()) {
        normalize_items(Some(&m))?
    } else {
        vec!["library".to_owned(), "settings".to_owned()]
    };

    let member_names: HashSet<String> = infos.iter().map(|(n, _)| n.clone()).collect();
    if !force
        && restore_items.iter().any(|x| x == "library")
        && member_names.contains("library.json")
        && let Some(created) = manifest
            .get("created")
            .and_then(Value::as_str)
            .map(str::trim)
        && !created.is_empty()
    {
        let current = data_dir.join("library.json");
        if current.is_file()
            && let Ok(meta) = fs::metadata(&current)
            && let Ok(mtime) = meta.modified()
            && let Ok(dur) = mtime.duration_since(UNIX_EPOCH)
            && let Some(backup_ts) = parse_iso_timestamp(created)
            && backup_ts.as_second() < dur.as_secs() as i64
        {
            return Err(ApiError::bad_request(
                "This backup is older than the current library. Pass force=True to restore it anyway.",
            ));
        }
    }

    let redacted_secrets = manifest
        .get("redacted_secrets")
        .and_then(Value::as_bool)
        .unwrap_or(false);

    let mut restored_state: Option<Value> = None;
    let library_file = data_dir.join("library.json");
    if restore_items.iter().any(|x| x == "library") && member_names.contains("library.json") {
        if library_file.is_file() {
            reject_symlink_components(&library_file)?;
            let src = File::open(&library_file).map_err(|e| ApiError::internal(e.to_string()))?;
            atomic_copy_stream(
                src,
                &data_dir.join("library.before-restore.json"),
                0o600,
                Some(MAX_BACKUP_MEMBER_BYTES as usize),
            )
            .map_err(|e| ApiError::internal(e.to_string()))?;
        }
        let mut entry = package
            .by_name("library.json")
            .map_err(|_| ApiError::bad_request("Backup library is invalid."))?;
        let mut raw = Vec::new();
        entry
            .read_to_end(&mut raw)
            .map_err(|_| ApiError::bad_request("Backup library is invalid."))?;
        let archived: Value = serde_json::from_slice(&raw)
            .map_err(|_| ApiError::bad_request("Backup library is invalid."))?;
        let Some(archived_obj) = archived.as_object() else {
            return Err(ApiError::bad_request("Backup library is invalid."));
        };
        let mut merged = default_state();
        if let Some(m) = merged.as_object_mut() {
            for (k, v) in archived_obj {
                m.insert(k.clone(), v.clone());
            }
        }
        if !merged.get("games").is_some_and(Value::is_array) {
            return Err(ApiError::bad_request("Backup library is invalid."));
        }
        restored_state = Some(merged);
    }

    let mut archived_settings: Option<Map<String, Value>> = None;
    if restore_items.iter().any(|x| x == "settings") && member_names.contains("settings.json") {
        let mut entry = package
            .by_name("settings.json")
            .map_err(|_| ApiError::bad_request("Backup settings are invalid."))?;
        let mut raw = Vec::new();
        entry
            .read_to_end(&mut raw)
            .map_err(|_| ApiError::bad_request("Backup settings are invalid."))?;
        let parsed: Value = serde_json::from_slice(&raw)
            .map_err(|_| ApiError::bad_request("Backup settings are invalid."))?;
        let Some(obj) = parsed.as_object() else {
            return Err(ApiError::bad_request("Backup settings are invalid."));
        };
        archived_settings = Some(obj.clone());
    }

    if redacted_secrets {
        let existing_settings: Map<String, Value> =
            fs::read_to_string(data_dir.join("settings.json"))
                .ok()
                .and_then(|s| serde_json::from_str::<Value>(&s).ok())
                .and_then(|v| v.as_object().cloned())
                .unwrap_or_default();
        if let Some(arch) = archived_settings {
            archived_settings = Some(merge_preserved_credentials(&arch, &existing_settings));
        }
        if let Some(ref mut st) = restored_state
            && let Some(st_set) = st.get("settings").and_then(Value::as_object).cloned()
        {
            let merged_set = merge_preserved_credentials(&st_set, &existing_settings);
            if let Some(m) = st.as_object_mut() {
                m.insert("settings".into(), Value::Object(merged_set));
            }
        }
    }

    if let (Some(arch_set), Some(st)) = (&archived_settings, &mut restored_state)
        && let Some(m) = st.as_object_mut()
    {
        let entry = m
            .entry("settings".to_owned())
            .or_insert_with(|| Value::Object(Map::new()));
        if !entry.is_object() {
            *entry = Value::Object(Map::new());
        }
        if let Some(sm) = entry.as_object_mut() {
            for (k, v) in arch_set {
                sm.insert(k.clone(), v.clone());
            }
        }
    }

    if let Some(ref st) = restored_state {
        if redacted_secrets {
            let data =
                serde_json::to_string_pretty(st).map_err(|e| ApiError::internal(e.to_string()))?;
            atomic_copy_stream(
                Cursor::new(data.as_bytes()),
                &library_file,
                0o600,
                Some(MAX_BACKUP_MEMBER_BYTES as usize),
            )
            .map_err(|e| ApiError::internal(e.to_string()))?;
        } else {
            let entry = package
                .by_name("library.json")
                .map_err(|_| ApiError::bad_request("Backup library is invalid."))?;
            atomic_copy_stream(
                entry,
                &library_file,
                0o600,
                Some(MAX_BACKUP_MEMBER_BYTES as usize),
            )
            .map_err(|e| ApiError::internal(e.to_string()))?;
        }
    }

    if let Some(ref arch_set) = archived_settings {
        let settings_file = data_dir.join("settings.json");
        if redacted_secrets {
            let data = serde_json::to_string_pretty(&Value::Object(arch_set.clone()))
                .map_err(|e| ApiError::internal(e.to_string()))?;
            atomic_copy_stream(
                Cursor::new(data.as_bytes()),
                &settings_file,
                0o600,
                Some(MAX_BACKUP_MEMBER_BYTES as usize),
            )
            .map_err(|e| ApiError::internal(e.to_string()))?;
        } else {
            let entry = package
                .by_name("settings.json")
                .map_err(|_| ApiError::bad_request("Backup settings are invalid."))?;
            atomic_copy_stream(
                entry,
                &settings_file,
                0o600,
                Some(MAX_BACKUP_MEMBER_BYTES as usize),
            )
            .map_err(|e| ApiError::internal(e.to_string()))?;
        }
    }

    // Restore directory trees: media, plugins, themes, extension_data
    for key in ["media", "plugins", "themes", "extension_data"] {
        if !restore_items.iter().any(|x| x == key) {
            continue;
        }
        let Some(item_rel) = backup_item_path(key) else {
            continue;
        };
        let prefix = format!("{item_rel}/");
        let members: Vec<String> = infos
            .iter()
            .map(|(n, _)| n.clone())
            .filter(|n| n.starts_with(&prefix) && !n.ends_with('/'))
            .collect();
        let raw_root = data_dir.join(item_rel);
        reject_symlink_components(&raw_root)?;
        fs::create_dir_all(&raw_root).map_err(|e| ApiError::internal(e.to_string()))?;
        reject_symlink_components(&raw_root)?;
        let target_root = resolve_non_strict(&raw_root);
        for member in members {
            let relative = &member[prefix.len()..];
            let target = resolve_non_strict(&target_root.join(relative));
            if !target.starts_with(&target_root) {
                return Err(ApiError::bad_request("Backup contains an unsafe path."));
            }
            if let Some(parent) = target.parent() {
                fs::create_dir_all(parent).map_err(|e| ApiError::internal(e.to_string()))?;
            }
            let resolved = resolve_non_strict(&target);
            if !resolved.starts_with(&target_root) {
                return Err(ApiError::bad_request("Backup contains an unsafe path."));
            }
            if key == "plugins" && relative == "plugins-state.json" {
                let mut entry = package
                    .by_name(&member)
                    .map_err(|_| ApiError::bad_request("Backup plugin state is invalid."))?;
                let mut raw = Vec::new();
                entry
                    .read_to_end(&mut raw)
                    .map_err(|_| ApiError::bad_request("Backup plugin state is invalid."))?;
                let mut payload: Value = serde_json::from_slice(&raw)
                    .map_err(|_| ApiError::bad_request("Backup plugin state is invalid."))?;
                if !payload.is_object() {
                    return Err(ApiError::bad_request("Backup plugin state is invalid."));
                }
                if redacted_secrets {
                    let existing: Value = fs::read_to_string(raw_root.join("plugins-state.json"))
                        .ok()
                        .and_then(|s| serde_json::from_str(&s).ok())
                        .unwrap_or_else(|| json!({}));
                    payload = merge_preserved_plugin_settings(&payload, &existing, &raw_root);
                }
                let text = serde_json::to_string_pretty(&payload)
                    .map_err(|e| ApiError::internal(e.to_string()))?
                    + "\n";
                atomic_write_text(&target, &text, 0o600)
                    .map_err(|e| ApiError::internal(e.to_string()))?;
                continue;
            }
            let entry = package
                .by_name(&member)
                .map_err(|_| ApiError::bad_request("Backup archive is invalid."))?;
            atomic_copy_stream(
                entry,
                &target,
                0o600,
                Some(MAX_BACKUP_MEMBER_BYTES as usize),
            )
            .map_err(|e| ApiError::internal(e.to_string()))?;
        }
    }

    let mut report = Vec::new();
    let mut sorted_infos = infos;
    sorted_infos.sort_by(|a, b| a.0.cmp(&b.0));
    for (name, size) in sorted_infos {
        if name.ends_with('/') {
            continue;
        }
        let item = member_item(&name);
        let status = if name == "manifest.json" {
            "manifest"
        } else if restore_items.iter().any(|x| x == item) {
            "restored"
        } else {
            "skipped"
        };
        report.push(json!({
            "name": name,
            "item": item,
            "size": size,
            "verified": verified.contains(&name),
            "status": status,
        }));
    }
    Ok(json!({
        "items": restore_items,
        "files": report,
    }))
}

fn library_index(state: &Value) -> (BTreeMap<String, Value>, Map<String, Value>) {
    let mut games = BTreeMap::new();
    if let Some(rows) = state.get("games").and_then(Value::as_array) {
        for g in rows {
            if let Some(gid) = g.get("game_id").and_then(Value::as_str).map(str::trim)
                && !gid.is_empty()
            {
                games.insert(gid.to_owned(), g.clone());
            }
        }
    }
    let settings = state
        .get("settings")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    (games, settings)
}

fn display_name(game: Option<&Value>, fallback: &str) -> String {
    if let Some(g) = game {
        for f in ["name", "title"] {
            if let Some(v) = g.get(f).and_then(Value::as_str).map(str::trim)
                && !v.is_empty()
            {
                return v.to_owned();
            }
        }
    }
    fallback.to_owned()
}

pub fn diff_manifests(
    current_state: &Value,
    archive_path: &Path,
    limit: usize,
) -> Result<Value, ApiError> {
    let (current_games, current_settings) = library_index(current_state);
    let file = File::open(archive_path)
        .map_err(|_| ApiError::bad_request("Backup archive is invalid."))?;
    let mut package =
        ZipArchive::new(file).map_err(|_| ApiError::bad_request("Backup archive is invalid."))?;
    let manifest = load_backup_manifest(&mut package)?;
    let mut names: HashSet<String> = HashSet::new();
    for i in 0..package.len() {
        if let Ok(e) = package.by_index(i)
            && let Ok(n) = e.name()
        {
            names.insert(n.into_owned());
        }
    }
    let (backup_games, backup_settings) = if names.contains("library.json") {
        let mut raw = Vec::new();
        let parsed = package
            .by_name("library.json")
            .ok()
            .and_then(|mut e| e.read_to_end(&mut raw).ok())
            .and_then(|_| serde_json::from_slice::<Value>(&raw).ok())
            .unwrap_or(Value::Null);
        library_index(&parsed)
    } else {
        (BTreeMap::new(), Map::new())
    };

    let mut removed_by_restore: Vec<String> = current_games
        .keys()
        .filter(|k| !backup_games.contains_key(*k))
        .cloned()
        .collect();
    removed_by_restore.sort();
    let mut added_by_restore: Vec<String> = backup_games
        .keys()
        .filter(|k| !current_games.contains_key(*k))
        .cloned()
        .collect();
    added_by_restore.sort();

    let mut changed_rows = Vec::new();
    for (gid, cur) in &current_games {
        let Some(bak) = backup_games.get(gid) else {
            continue;
        };
        let mut fields = Map::new();
        for f in DIFF_FIELDS {
            let cv = cur.get(*f).cloned().unwrap_or(Value::Null);
            let bv = bak.get(*f).cloned().unwrap_or(Value::Null);
            if cv != bv {
                fields.insert((*f).into(), json!({"from": bv, "to": cv}));
            }
        }
        if !fields.is_empty() {
            let nm = display_name(Some(cur), gid);
            let nm = if nm.is_empty() {
                display_name(Some(bak), gid)
            } else {
                nm
            };
            changed_rows.push(json!({
                "game_id": gid,
                "name": nm,
                "fields": Value::Object(fields),
            }));
        }
    }

    let settings_changed = !current_settings.is_empty()
        && !backup_settings.is_empty()
        && current_settings != backup_settings;
    let mut settings_keys = Vec::new();
    if !current_settings.is_empty() && !backup_settings.is_empty() {
        let mut all_keys: HashSet<&String> = current_settings.keys().collect();
        all_keys.extend(backup_settings.keys());
        for k in all_keys {
            if current_settings.get(k) != backup_settings.get(k) {
                settings_keys.push(k.clone());
            }
        }
        settings_keys.sort();
    }
    let manifest_items: Vec<String> = manifest
        .get("items")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default();
    let settings_restored = manifest_items.iter().any(|x| x == "settings");
    let settings_present = names.contains("settings.json");

    let will_remove_rows: Vec<Value> = removed_by_restore
        .iter()
        .take(limit)
        .map(|gid| json!({"game_id": gid, "name": display_name(current_games.get(gid), gid)}))
        .collect();
    let will_add_rows: Vec<Value> = added_by_restore
        .iter()
        .take(limit)
        .map(|gid| json!({"game_id": gid, "name": display_name(backup_games.get(gid), gid)}))
        .collect();
    let changed_ids: Vec<String> = changed_rows
        .iter()
        .take(limit)
        .filter_map(|r| r.get("game_id").and_then(Value::as_str).map(str::to_owned))
        .collect();

    Ok(json!({
        "added": &removed_by_restore[..removed_by_restore.len().min(limit)],
        "removed": &added_by_restore[..added_by_restore.len().min(limit)],
        "changed": changed_ids,
        "restore": {
            "will_remove": {
                "total": removed_by_restore.len(),
                "truncated": removed_by_restore.len() > limit,
                "rows": will_remove_rows,
            },
            "will_add": {
                "total": added_by_restore.len(),
                "truncated": added_by_restore.len() > limit,
                "rows": will_add_rows,
            },
            "will_change": {
                "total": changed_rows.len(),
                "truncated": changed_rows.len() > limit,
                "rows": &changed_rows[..changed_rows.len().min(limit)],
            },
        },
        "settings": {
            "restored": settings_restored,
            "present": settings_present,
            "would_change": settings_changed && settings_restored && settings_present,
            "differs": settings_changed,
            "redacted_secrets": manifest.get("redacted_secrets").and_then(Value::as_bool).unwrap_or(false),
            "keys_changed": &settings_keys[..settings_keys.len().min(limit)],
        },
        "truncated": {
            "added": removed_by_restore.len() > limit,
            "removed": added_by_restore.len() > limit,
            "changed": changed_rows.len() > limit,
            "limit": limit,
        },
        "settings_changed": settings_changed,
        "summary": {
            "added": removed_by_restore.len(),
            "removed": added_by_restore.len(),
            "changed": changed_rows.len(),
            "total": current_games.len(),
        },
    }))
}
