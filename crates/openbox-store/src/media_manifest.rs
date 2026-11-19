//! Media manifest for `<data_dir>/media/manifest.json` mirroring `pkg/state/media_manifest.py`.

use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::Read;
use std::path::Path;
use std::time::UNIX_EPOCH;

use openbox_io::atomic_write_text;
use openbox_platform::resolve_non_strict;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

pub const MANIFEST_NAME: &str = "manifest.json";
pub const MANIFEST_FORMAT: &str = "openbox-media-manifest";
pub const MANIFEST_VERSION: u32 = 1;
pub const UNTRACKED_SOURCE: &str = "untracked";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ManifestEntry {
    pub size: u64,
    pub mtime_ns: u128,
    pub sha256: String,
    pub source: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct VerifyReport {
    pub tracked: usize,
    pub missing: Vec<String>,
    pub changed: Vec<String>,
    pub untracked: Vec<String>,
}

fn sha256_file(path: &Path) -> std::io::Result<String> {
    let mut f = File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; 1024 * 1024];
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hex::encode(hasher.finalize()))
}

fn file_mtime_ns(meta: &fs::Metadata) -> u128 {
    meta.modified()
        .ok()
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map(|d| d.as_nanos())
        .unwrap_or(0)
}

fn build_entry(path: &Path, source: &str) -> std::io::Result<ManifestEntry> {
    let meta = fs::metadata(path)?;
    Ok(ManifestEntry {
        size: meta.len(),
        mtime_ns: file_mtime_ns(&meta),
        sha256: sha256_file(path)?,
        source: source.to_owned(),
    })
}

fn tree_files(media_root: &Path) -> Vec<String> {
    if !media_root.is_dir() {
        return Vec::new();
    }
    let mut found = Vec::new();
    let mut stack = vec![media_root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let Ok(entries) = fs::read_dir(&dir) else {
            continue;
        };
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
            let name = entry.file_name().to_string_lossy().into_owned();
            if name == MANIFEST_NAME || name.ends_with(".tmp") {
                continue;
            }
            if let Ok(rel) = p.strip_prefix(media_root) {
                let posix = rel.to_string_lossy().replace('\\', "/");
                found.push(posix);
            }
        }
    }
    found.sort();
    found
}

pub fn load(media_root: &Path) -> BTreeMap<String, ManifestEntry> {
    let Ok(raw) = fs::read_to_string(media_root.join(MANIFEST_NAME)) else {
        return BTreeMap::new();
    };
    let Ok(val) = serde_json::from_str::<Value>(&raw) else {
        return BTreeMap::new();
    };
    if val.get("format").and_then(Value::as_str) != Some(MANIFEST_FORMAT) {
        return BTreeMap::new();
    }
    let Some(files) = val.get("files").and_then(Value::as_object) else {
        return BTreeMap::new();
    };
    let mut out = BTreeMap::new();
    for (k, v) in files {
        if let Ok(entry) = serde_json::from_value::<ManifestEntry>(v.clone()) {
            out.insert(k.clone(), entry);
        }
    }
    out
}

fn save(media_root: &Path, entries: &BTreeMap<String, ManifestEntry>) -> std::io::Result<()> {
    fs::create_dir_all(media_root)?;
    let payload = json!({
        "format": MANIFEST_FORMAT,
        "version": MANIFEST_VERSION,
        "files": entries,
    });
    let text = format!(
        "{}\n",
        serde_json::to_string_pretty(&payload).unwrap_or_else(|_| "{}".to_owned())
    );
    atomic_write_text(&media_root.join(MANIFEST_NAME), &text, 0o600)?;
    Ok(())
}

pub fn record(media_root: &Path, path: &Path, source: &str) -> Option<String> {
    if !path.is_file() {
        return None;
    }
    let root_res = resolve_non_strict(media_root);
    let path_res = resolve_non_strict(path);
    let rel = path_res
        .strip_prefix(&root_res)
        .ok()?
        .to_string_lossy()
        .replace('\\', "/");
    let mut entries = load(media_root);
    let entry = build_entry(path, source).ok()?;
    entries.insert(rel.clone(), entry);
    save(media_root, &entries).ok()?;
    Some(rel)
}

pub fn verify(media_root: &Path) -> VerifyReport {
    let entries = load(media_root);
    let mut missing = Vec::new();
    let mut changed = Vec::new();
    for (rel, entry) in &entries {
        let full = media_root.join(rel);
        if !full.is_file() {
            missing.push(rel.clone());
            continue;
        }
        if let Ok(meta) = fs::metadata(&full) {
            if meta.len() == entry.size && file_mtime_ns(&meta) == entry.mtime_ns {
                continue;
            }
            if sha256_file(&full).ok().as_deref() != Some(entry.sha256.as_str()) {
                changed.push(rel.clone());
            }
        } else {
            missing.push(rel.clone());
        }
    }
    let untracked: Vec<String> = tree_files(media_root)
        .into_iter()
        .filter(|rel| !entries.contains_key(rel))
        .collect();
    VerifyReport {
        tracked: entries.len(),
        missing,
        changed,
        untracked,
    }
}

pub fn rebuild(media_root: &Path) -> Value {
    if !media_root.is_dir() {
        return json!({ "added": 0, "refreshed": 0, "dropped": 0 });
    }
    let report = verify(media_root);
    let mut entries = load(media_root);
    for rel in &report.missing {
        entries.remove(rel);
    }
    for rel in &report.changed {
        let src = entries
            .get(rel)
            .map(|e| e.source.clone())
            .unwrap_or_else(|| UNTRACKED_SOURCE.to_owned());
        if let Ok(entry) = build_entry(&media_root.join(rel), &src) {
            entries.insert(rel.clone(), entry);
        }
    }
    for rel in &report.untracked {
        if let Ok(entry) = build_entry(&media_root.join(rel), UNTRACKED_SOURCE) {
            entries.insert(rel.clone(), entry);
        }
    }
    if !report.missing.is_empty() || !report.changed.is_empty() || !report.untracked.is_empty() {
        let _ = save(media_root, &entries);
    }
    json!({
        "added": report.untracked.len(),
        "refreshed": report.changed.len(),
        "dropped": report.missing.len(),
    })
}
