//! Safe, cached archive extraction (ZIP + 7z/7zz) mirroring `archives.py`.

use std::collections::{BTreeMap, HashSet};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, Write};
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use openbox_platform::{resolve_non_strict, user_symlink_component, which};
use sha2::{Digest, Sha256};
use tempfile::Builder;
use zip::ZipArchive;

pub const MAX_ARCHIVE_MEMBERS: usize = 25_000;
pub const MAX_ARCHIVE_MEMBER_BYTES: u64 = 2 * 1024 * 1024 * 1024;
pub const MAX_ARCHIVE_TOTAL_BYTES: u64 = 8 * 1024 * 1024 * 1024;
pub const MAX_ARCHIVE_LISTING_BYTES: usize = 16 * 1024 * 1024;

#[derive(Debug, Clone, Copy)]
pub struct ArchiveLimits {
    pub max_members: usize,
    pub max_member_bytes: u64,
    pub max_total_bytes: u64,
}

impl Default for ArchiveLimits {
    fn default() -> Self {
        Self {
            max_members: MAX_ARCHIVE_MEMBERS,
            max_member_bytes: MAX_ARCHIVE_MEMBER_BYTES,
            max_total_bytes: MAX_ARCHIVE_TOTAL_BYTES,
        }
    }
}

pub fn extraction_dir(archive: &Path, cache_root: &Path) -> io::Result<PathBuf> {
    let meta = fs::metadata(archive)?;
    let size = meta.len();
    let mtime_ns = meta
        .modified()
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let resolved = resolve_non_strict(archive);
    let input = format!("{}:{size}:{mtime_ns}", resolved.display());
    let digest = hex::encode(Sha256::digest(input.as_bytes()));
    Ok(cache_root.join(&digest[..20]))
}

/// Safely extract a ZIP archive with traversal, symlink, duplicate-entry, and expansion-bomb checks.
pub fn safe_zip_extract<R: Read + Seek>(
    reader: R,
    destination: &Path,
    limits: ArchiveLimits,
) -> Result<(), String> {
    if user_symlink_component(destination).is_some() {
        return Err("Archive destination contains a symlink.".to_owned());
    }
    let root = resolve_non_strict(destination);
    let mut archive = ZipArchive::new(reader).map_err(|e| format!("Invalid ZIP archive: {e}"))?;
    if archive.len() > limits.max_members {
        return Err("Archive contains too many entries.".to_owned());
    }
    let mut total_bytes: u64 = 0;
    let mut seen_names: HashSet<String> = HashSet::new();
    for i in 0..archive.len() {
        let entry = archive
            .by_index(i)
            .map_err(|e| format!("Invalid ZIP entry: {e}"))?;
        let raw_name = entry
            .name()
            .map_err(|e| format!("Invalid ZIP entry name: {e}"))?
            .into_owned();
        let norm_slashes = raw_name.replace('\\', "/");
        if norm_slashes.is_empty() || norm_slashes.contains('\0') {
            return Err(format!("Unsafe archive path: {raw_name}"));
        }
        let rel = Path::new(&norm_slashes);
        if rel.is_absolute()
            || rel.components().any(|c| {
                matches!(
                    c,
                    Component::ParentDir | Component::RootDir | Component::Prefix(_)
                )
            })
        {
            return Err(format!("Unsafe archive path: {raw_name}"));
        }
        let normalized: String = rel
            .components()
            .filter_map(|c| match c {
                Component::Normal(s) => Some(s.to_string_lossy().into_owned()),
                _ => None,
            })
            .collect::<Vec<_>>()
            .join("/");
        if !seen_names.insert(normalized) {
            return Err(format!("Archive contains a duplicate entry: {raw_name}"));
        }
        let size = entry.size();
        if size > limits.max_member_bytes {
            return Err("Archive member is too large.".to_owned());
        }
        total_bytes = total_bytes
            .checked_add(size)
            .ok_or_else(|| "Archive expands beyond the allowed size.".to_owned())?;
        if total_bytes > limits.max_total_bytes {
            return Err("Archive expands beyond the allowed size.".to_owned());
        }
        let target = resolve_non_strict(&destination.join(rel));
        if target != root && !target.starts_with(&root) {
            return Err(format!("Unsafe archive path: {raw_name}"));
        }
        if let Some(mode) = entry.unix_mode() {
            let ftype = mode & 0o170000;
            if ftype == 0o120000 || ftype == 0o060000 || ftype == 0o020000 || ftype == 0o010000 {
                return Err(format!("Archive links are not supported: {raw_name}"));
            }
        }
    }

    fs::create_dir_all(destination).map_err(|e| e.to_string())?;
    let mut buf = vec![0u8; 1024 * 1024];
    for i in 0..archive.len() {
        let mut entry = archive.by_index(i).map_err(|e| e.to_string())?;
        let raw_name = entry.name().map_err(|e| e.to_string())?.into_owned();
        let norm_slashes = raw_name.replace('\\', "/");
        let target = destination.join(Path::new(&norm_slashes));
        if entry.is_dir() {
            fs::create_dir_all(&target).map_err(|e| e.to_string())?;
            continue;
        }
        if user_symlink_component(&target).is_some() {
            return Err(format!(
                "Archive destination contains a symlink: {raw_name}"
            ));
        }
        if let Some(parent) = target.parent() {
            fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }
        let mut out = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&target)
            .map_err(|e| e.to_string())?;
        let mut written: u64 = 0;
        loop {
            let n = entry.read(&mut buf).map_err(|e| e.to_string())?;
            if n == 0 {
                break;
            }
            written += n as u64;
            if written > limits.max_member_bytes {
                return Err("Archive member is too large.".to_owned());
            }
            out.write_all(&buf[..n]).map_err(|e| e.to_string())?;
        }
    }
    Ok(())
}

pub fn parse_7z_records(text: &str) -> Vec<BTreeMap<String, String>> {
    let mut records = Vec::new();
    let mut current = BTreeMap::new();
    for line in text.lines() {
        if line.trim().is_empty() {
            if !current.is_empty() {
                records.push(std::mem::take(&mut current));
            }
            continue;
        }
        if let Some((k, v)) = line.split_once(" = ") {
            current.insert(k.to_owned(), v.to_owned());
        }
    }
    if !current.is_empty() {
        records.push(current);
    }
    records
}

pub fn validate_7z_member_record(record: &BTreeMap<String, String>) -> Result<Option<u64>, String> {
    let Some(path) = record.get("Path").filter(|s| !s.is_empty()) else {
        return Ok(None);
    };
    if !record.contains_key("Size") {
        return Ok(None);
    }
    let normalized = path.replace('\\', "/");
    let p = Path::new(&normalized);
    if p.is_absolute()
        || p.components().any(|c| {
            matches!(
                c,
                Component::ParentDir | Component::RootDir | Component::Prefix(_)
            )
        })
    {
        return Err(format!("Unsafe archive path: {path}"));
    }
    let attrs = record
        .get("Attributes")
        .map(|s| s.to_lowercase())
        .unwrap_or_default();
    let link_fields = ["Symbolic Link", "Hard Link", "Reparse Point"];
    if link_fields.iter().any(|k| record.contains_key(*k))
        || attrs.split_whitespace().any(|tok| tok.starts_with('l'))
    {
        return Err(format!("Archive links are not supported: {path}"));
    }
    let size = record
        .get("Size")
        .map(|s| s.trim())
        .unwrap_or("0")
        .parse::<u64>()
        .map_err(|_| "Archive member size is invalid.".to_owned())?;
    if size > MAX_ARCHIVE_MEMBER_BYTES {
        return Err("Archive member is too large.".to_owned());
    }
    Ok(Some(size))
}

fn validate_extracted_tree(destination: &Path) -> Result<(), String> {
    let root = resolve_non_strict(destination);
    let mut stack = vec![destination.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let entries = fs::read_dir(&dir).map_err(|e| e.to_string())?;
        for entry in entries.flatten() {
            let path = entry.path();
            let meta = fs::symlink_metadata(&path).map_err(|e| e.to_string())?;
            let ft = meta.file_type();
            if ft.is_symlink() {
                return Err(format!(
                    "Archive links are not supported: {}",
                    entry.file_name().to_string_lossy()
                ));
            }
            if !(ft.is_file() || ft.is_dir()) {
                return Err(format!(
                    "Archive special files are not supported: {}",
                    entry.file_name().to_string_lossy()
                ));
            }
            let resolved = resolve_non_strict(&path);
            if resolved != root && !resolved.starts_with(&root) {
                return Err(format!(
                    "Archive extraction escaped its destination: {}",
                    entry.file_name().to_string_lossy()
                ));
            }
            if ft.is_dir() {
                stack.push(path);
            }
        }
    }
    Ok(())
}

pub fn choose_game_file(destination: &Path, member: &str) -> Result<PathBuf, String> {
    if !member.is_empty() {
        let raw = destination.join(member);
        if fs::symlink_metadata(&raw)
            .map(|m| m.file_type().is_symlink())
            .unwrap_or(false)
        {
            return Err("Archive member is a symlink.".to_owned());
        }
        let selected = resolve_non_strict(&raw);
        let root = resolve_non_strict(destination);
        if !selected.starts_with(&root) || !selected.is_file() {
            return Err(format!("Archive member not found: {member}"));
        }
        return Ok(selected);
    }
    let ignored = ["txt", "nfo", "diz", "jpg", "jpeg", "png", "gif", "pdf"];
    let mut best: Option<(u64, PathBuf)> = None;
    let mut stack = vec![destination.to_path_buf()];
    while let Some(dir) = stack.pop() {
        if let Ok(entries) = fs::read_dir(&dir) {
            for entry in entries.flatten() {
                let p = entry.path();
                let Ok(meta) = fs::symlink_metadata(&p) else {
                    continue;
                };
                if meta.file_type().is_symlink() {
                    continue;
                }
                if meta.file_type().is_dir() {
                    stack.push(p);
                    continue;
                }
                if !meta.file_type().is_file() {
                    continue;
                }
                if p.file_name().and_then(|n| n.to_str()) == Some(".complete") {
                    continue;
                }
                let ext = p
                    .extension()
                    .and_then(|e| e.to_str())
                    .map(str::to_lowercase)
                    .unwrap_or_default();
                if ignored.contains(&ext.as_str()) {
                    continue;
                }
                let size = meta.len();
                if best.as_ref().is_none_or(|(bsize, _)| size > *bsize) {
                    best = Some((size, p));
                }
            }
        }
    }
    best.map(|(_, p)| p)
        .ok_or_else(|| "No launchable file was found in the archive.".to_owned())
}

fn open_regular_nofollow(path: &Path) -> Result<File, String> {
    let mut opts = OpenOptions::new();
    opts.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC);
    }
    let file = opts.open(path).map_err(|e| e.to_string())?;
    let meta = file.metadata().map_err(|e| e.to_string())?;
    if !meta.is_file() {
        return Err("Archive source must be a regular file.".to_owned());
    }
    if meta.len() > MAX_ARCHIVE_TOTAL_BYTES {
        return Err("Archive source is too large.".to_owned());
    }
    Ok(file)
}

/// Extract a cached game archive (ZIP natively, 7z/RAR via O_NOFOLLOW snapshot + `7z`/`7zz`).
pub fn extract_game(archive: &Path, cache_root: &Path, member: &str) -> Result<PathBuf, String> {
    if fs::symlink_metadata(archive)
        .map(|m| m.file_type().is_symlink())
        .unwrap_or(false)
    {
        return Err("Archive source is a symlink.".to_owned());
    }
    let destination = extraction_dir(archive, cache_root).map_err(|e| e.to_string())?;
    if fs::symlink_metadata(&destination)
        .map(|m| m.file_type().is_symlink())
        .unwrap_or(false)
    {
        return Err("Archive cache destination is a symlink.".to_owned());
    }
    let complete = destination.join(".complete");
    if !complete.is_file() {
        let parent = destination.parent().unwrap_or(cache_root);
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        let staging_dir = Builder::new()
            .prefix(&format!(
                ".{}.extracting-",
                destination
                    .file_name()
                    .map(|n| n.to_string_lossy().into_owned())
                    .unwrap_or_else(|| "arc".to_owned())
            ))
            .tempdir_in(parent)
            .map_err(|e| e.to_string())?;
        let staging = staging_dir.path().to_path_buf();
        let ext = archive
            .extension()
            .and_then(|e| e.to_str())
            .map(str::to_lowercase)
            .unwrap_or_default();
        if ext == "zip" {
            let file = open_regular_nofollow(archive)?;
            safe_zip_extract(file, &staging, ArchiveLimits::default())?;
        } else {
            let mut src = open_regular_nofollow(archive)?;
            let mut snap = Builder::new()
                .prefix(".openbox-archive-")
                .suffix(&format!(".{ext}"))
                .tempfile_in(parent)
                .map_err(|e| e.to_string())?;
            io::copy(&mut src, &mut snap).map_err(|e| e.to_string())?;
            snap.flush().map_err(|e| e.to_string())?;
            let extractor = which("7z")
                .or_else(|| which("7zz"))
                .ok_or_else(|| "7z or 7zz is required to extract this archive.".to_owned())?;
            // Validate 7z listing under 16 MiB cap & 60s timeout
            let mut child = Command::new(&extractor)
                .args(["l", "-slt"])
                .arg(snap.path())
                .stdout(Stdio::piped())
                .stderr(Stdio::null())
                .spawn()
                .map_err(|e| e.to_string())?;
            let mut out_bytes = Vec::new();
            let mut stdout = child
                .stdout
                .take()
                .ok_or_else(|| "7z stdout unavailable".to_owned())?;
            let deadline = Instant::now() + Duration::from_secs(60);
            let mut chunk = [0u8; 64 * 1024];
            loop {
                if Instant::now() > deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err("7z listing timed out.".to_owned());
                }
                let n = stdout.read(&mut chunk).map_err(|e| e.to_string())?;
                if n == 0 {
                    break;
                }
                out_bytes.extend_from_slice(&chunk[..n]);
                if out_bytes.len() > MAX_ARCHIVE_LISTING_BYTES {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err("Archive listing is too large.".to_owned());
                }
            }
            let status = child.wait().map_err(|e| e.to_string())?;
            if !status.success() {
                return Err("7z could not inspect the archive.".to_owned());
            }
            let records = parse_7z_records(&String::from_utf8_lossy(&out_bytes));
            let mut members = 0usize;
            let mut total = 0u64;
            for rec in &records {
                if let Some(sz) = validate_7z_member_record(rec)? {
                    members += 1;
                    total = total.saturating_add(sz);
                    if members > MAX_ARCHIVE_MEMBERS || total > MAX_ARCHIVE_TOTAL_BYTES {
                        return Err("Archive expands beyond the allowed size.".to_owned());
                    }
                }
            }
            let x_status = Command::new(&extractor)
                .args(["x", "-y", "-snl-", "-snh-"])
                .arg(format!("-o{}", staging.display()))
                .arg(snap.path())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status()
                .map_err(|e| e.to_string())?;
            if !x_status.success() {
                return Err("7z extraction failed.".to_owned());
            }
        }
        validate_extracted_tree(&staging)?;
        File::create(staging.join(".complete")).map_err(|e| e.to_string())?;
        if destination.exists() {
            let _ = fs::remove_dir_all(&destination);
        }
        let staging_kept = staging_dir.keep();
        fs::rename(&staging_kept, &destination).map_err(|e| e.to_string())?;
    }
    choose_game_file(&destination, member)
}
