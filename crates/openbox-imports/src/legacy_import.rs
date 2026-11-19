//! Legacy platform importers for PICO-8, MSU-1, and Amiga WHDLoad
//! (`pkg/parity/parity_legacy_import.py`).

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};

use openbox_core::ApiError;
use openbox_platform::expand_user;
use serde_json::{Value, json};

const WHDLOAD_SYSTEM_DRAWERS: &[&str] = &["c", "libs", "devs"];

fn required_root(folder: &str) -> Result<PathBuf, ApiError> {
    let root = expand_user(Path::new(folder));
    if !root.is_dir() {
        return Err(ApiError::not_found(format!(
            "Folder does not exist: {}",
            root.display()
        )));
    }
    Ok(root)
}

fn imported_at() -> String {
    jiff::Timestamp::now()
        .strftime("%Y-%m-%dT%H:%M:%S")
        .to_string()
}

fn walk_files(root: &Path, errors: &mut Vec<String>) -> Vec<PathBuf> {
    let mut seen: BTreeMap<String, PathBuf> = BTreeMap::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let rd = match fs::read_dir(&dir) {
            Ok(r) => r,
            Err(err) => {
                errors.push(format!("{}: {err}", dir.display()));
                continue;
            }
        };
        let mut entries: Vec<PathBuf> = rd.filter_map(Result::ok).map(|e| e.path()).collect();
        entries.sort_by_key(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .unwrap_or("")
                .to_lowercase()
        });
        let mut subdirs = Vec::new();
        for path in entries {
            let meta = match fs::metadata(&path) {
                Ok(m) => m,
                Err(err) => {
                    errors.push(format!("{}: {err}", path.display()));
                    continue;
                }
            };
            if meta.is_dir() {
                subdirs.push(path);
            } else if meta.is_file() {
                let real = fs::canonicalize(&path)
                    .unwrap_or_else(|_| path.clone())
                    .to_string_lossy()
                    .into_owned();
                let key = real.to_lowercase();
                let is_link = fs::symlink_metadata(&path)
                    .map(|m| m.file_type().is_symlink())
                    .unwrap_or(false);
                let replace = match seen.get(&key) {
                    None => true,
                    Some(existing) => {
                        let ex_link = fs::symlink_metadata(existing)
                            .map(|m| m.file_type().is_symlink())
                            .unwrap_or(false);
                        ex_link && !is_link
                    }
                };
                if replace {
                    seen.insert(key, path);
                }
            }
        }
        subdirs.reverse();
        stack.extend(subdirs);
    }
    seen.into_values().collect()
}

fn walk_msu_dirs(root: &Path, errors: &mut Vec<String>) -> Vec<PathBuf> {
    let mut found = Vec::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let rd = match fs::read_dir(&dir) {
            Ok(r) => r,
            Err(err) => {
                errors.push(format!("{}: {err}", dir.display()));
                continue;
            }
        };
        let mut subdirs: Vec<PathBuf> = rd
            .filter_map(Result::ok)
            .map(|e| e.path())
            .filter(|p| p.is_dir())
            .collect();
        subdirs.sort_by_key(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .unwrap_or("")
                .to_lowercase()
        });
        for sub in &subdirs {
            if sub
                .file_name()
                .and_then(|n| n.to_str())
                .is_some_and(|n| n.to_lowercase().ends_with(".msu"))
            {
                found.push(sub.clone());
            }
        }
        subdirs.reverse();
        stack.extend(subdirs);
    }
    found
}

pub fn import_pico8(folder: &str, errors: &mut Vec<String>) -> Result<Vec<Value>, ApiError> {
    let root = required_root(folder)?;
    let now = imported_at();
    let mut games = Vec::new();
    let mut seen = HashSet::new();
    for path in walk_files(&root, errors) {
        let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
        let lower = name.to_lowercase();
        let title = if lower.ends_with(".p8.png") {
            &name[..name.len() - ".p8.png".len()]
        } else if lower.ends_with(".p8") {
            &name[..name.len() - ".p8".len()]
        } else {
            continue;
        };
        let real = fs::canonicalize(&path).unwrap_or_else(|_| path.clone());
        if !seen.insert(real) {
            continue;
        }
        games.push(json!({
            "name": title,
            "platform": "PICO-8",
            "genre": "",
            "path": path.to_string_lossy(),
            "source": "PICO-8",
            "emulator_adapter_id": "pico8-pico8",
            "added_at": now,
        }));
    }
    Ok(games)
}

fn sibling_roms(parent: &Path, errors: &mut Vec<String>) -> HashMap<String, PathBuf> {
    let mut roms = HashMap::new();
    let rd = match fs::read_dir(parent) {
        Ok(r) => r,
        Err(err) => {
            errors.push(format!("{}: {err}", parent.display()));
            return roms;
        }
    };
    let mut entries: Vec<PathBuf> = rd.filter_map(Result::ok).map(|e| e.path()).collect();
    entries.sort_by_key(|p| {
        p.file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("")
            .to_lowercase()
    });
    for entry in entries {
        if !entry.is_file() {
            continue;
        }
        let ext = entry
            .extension()
            .and_then(|e| e.to_str())
            .map(|e| format!(".{}", e.to_lowercase()))
            .unwrap_or_default();
        if !matches!(ext.as_str(), ".sfc" | ".smc") {
            continue;
        }
        if let Some(stem) = entry.file_stem().and_then(|s| s.to_str()) {
            roms.entry(stem.to_lowercase()).or_insert(entry);
        }
    }
    roms
}

pub fn import_msu1(folder: &str, errors: &mut Vec<String>) -> Result<Vec<Value>, ApiError> {
    let root = required_root(folder)?;
    let now = imported_at();
    let mut games = Vec::new();
    let mut seen = HashSet::new();
    for msu_dir in walk_msu_dirs(&root, errors) {
        let name = msu_dir.file_name().and_then(|n| n.to_str()).unwrap_or("");
        let stem = &name[..name.len().saturating_sub(".msu".len())];
        let Some(parent) = msu_dir.parent() else {
            continue;
        };
        let roms = sibling_roms(parent, errors);
        let Some(rom) = roms.get(&stem.to_lowercase()) else {
            errors.push(format!(
                "{}: no matching .sfc or .smc ROM found",
                msu_dir.display()
            ));
            continue;
        };
        let real = fs::canonicalize(rom).unwrap_or_else(|_| rom.clone());
        if !seen.insert(real) {
            continue;
        }
        games.push(json!({
            "name": rom.file_stem().and_then(|s| s.to_str()).unwrap_or(""),
            "platform": "SNES",
            "genre": "",
            "path": rom.to_string_lossy(),
            "source": "MSU-1",
            "msu1": true,
            "msu_path": msu_dir.to_string_lossy(),
            "added_at": now,
        }));
    }
    Ok(games)
}

fn in_whdload_system_drawer(root: &Path, path: &Path) -> bool {
    let Ok(rel) = path.strip_prefix(root) else {
        return false;
    };
    let mut comps: Vec<&str> = rel
        .components()
        .filter_map(|c| c.as_os_str().to_str())
        .collect();
    if comps.len() <= 1 {
        return false;
    }
    comps.pop();
    comps
        .iter()
        .any(|p| WHDLOAD_SYSTEM_DRAWERS.contains(&p.to_lowercase().as_str()))
}

pub fn import_whdload(folder: &str, errors: &mut Vec<String>) -> Result<Vec<Value>, ApiError> {
    let root = required_root(folder)?;
    let now = imported_at();
    let mut games = Vec::new();
    let mut seen = HashSet::new();
    for path in walk_files(&root, errors) {
        let ext = path
            .extension()
            .and_then(|e| e.to_str())
            .map(|e| format!(".{}", e.to_lowercase()))
            .unwrap_or_default();
        if !matches!(ext.as_str(), ".lha" | ".slave" | ".hdf") {
            continue;
        }
        if in_whdload_system_drawer(&root, &path) {
            continue;
        }
        let real = fs::canonicalize(&path).unwrap_or_else(|_| path.clone());
        if !seen.insert(real) {
            continue;
        }
        games.push(json!({
            "name": path.file_stem().and_then(|s| s.to_str()).unwrap_or(""),
            "platform": "Amiga",
            "genre": "",
            "path": path.to_string_lossy(),
            "source": "WHDLoad",
            "emulator_adapter_id": "amiga-whdload",
            "added_at": now,
        }));
    }
    Ok(games)
}
