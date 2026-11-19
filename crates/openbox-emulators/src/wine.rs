//! Wine prefix and Proton version discovery for Linux. Mirrors `pkg/parity/parity_wine.py`.

use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use openbox_platform::{IS_LINUX, expand_user, split_command, which};
use serde_json::{Value, json};

pub const DEFAULT_PREFIX_ROOTS: &[&str] = &[
    "~/.wine",
    "~/.local/share/wineprefixes",
    "~/.local/share/lutris/runners/wine",
    "~/.config/faugus-launcher/prefixes",
    "~/.local/share/bottles/bottles",
    "~/Games",
    "~/Faugus",
];

pub const DEFAULT_PROTON_ROOTS: &[&str] = &[
    "~/.steam/root/compatibilitytools.d",
    "~/.steam/steam/compatibilitytools.d",
    "~/.local/share/Steam/compatibilitytools.d",
    "~/.local/share/lutris/runners/wine",
    "~/.config/faugus-launcher/runners",
    "~/.local/share/faugus-launcher/runners",
];

pub fn list_wine_prefixes(search_roots: Option<&[&str]>) -> Vec<Value> {
    if !IS_LINUX {
        return Vec::new();
    }
    let roots = search_roots.unwrap_or(DEFAULT_PREFIX_ROOTS);
    let mut candidates: Vec<PathBuf> = Vec::new();
    for raw in roots {
        let base = expand_user(Path::new(raw));
        if !base.is_dir() {
            continue;
        }
        if base.join("drive_c").is_dir() {
            candidates.push(base.clone());
        }
        if let Ok(rd) = fs::read_dir(&base) {
            for ent in rd.flatten() {
                let child = ent.path();
                if child.is_symlink() || !child.is_dir() {
                    continue;
                }
                if child.join("drive_c").is_dir() {
                    candidates.push(child.clone());
                }
                if let Ok(grd) = fs::read_dir(&child) {
                    for gent in grd.flatten() {
                        let grand = gent.path();
                        if grand.join("drive_c").is_dir() {
                            candidates.push(grand);
                        }
                    }
                }
            }
        }
    }
    if let Ok(env_p) = std::env::var("WINEPREFIX")
        && !env_p.trim().is_empty()
    {
        let p = expand_user(Path::new(env_p.trim()));
        if p.is_dir() && p.join("drive_c").is_dir() {
            candidates.push(p);
        }
    }

    let mut seen = HashSet::new();
    let mut prefixes = Vec::new();
    for p in candidates {
        let key = fs::canonicalize(&p)
            .unwrap_or_else(|_| p.clone())
            .to_string_lossy()
            .into_owned();
        if !seen.insert(key) {
            continue;
        }
        let has_drive_c = p.join("drive_c").is_dir();
        let name = p
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        prefixes.push(json!({
            "path": p.to_string_lossy(),
            "has_drive_c": has_drive_c,
            "name": name,
        }));
    }
    prefixes.sort_by(|a, b| a["path"].as_str().cmp(&b["path"].as_str()));
    prefixes
}

pub fn list_proton_versions(search_roots: Option<&[&str]>) -> Vec<Value> {
    if !IS_LINUX {
        return Vec::new();
    }
    let mut versions = Vec::new();
    let mut seen = HashSet::new();

    for exe in ["wine", "wine64", "proton", "umu-run", "umu-launcher"] {
        if let Some(found) = which(exe) {
            let s = found.to_string_lossy().into_owned();
            if seen.insert(s.clone()) {
                versions.push(json!({"name": s, "path": s, "source": "path"}));
            }
        }
    }
    if which("flatpak").is_some()
        && let Ok(out) = Command::new("flatpak")
            .args(["list", "--app", "--columns=application"])
            .output()
        && out.status.success()
    {
        let text = String::from_utf8_lossy(&out.stdout);
        for line in text.lines() {
            let app = line.trim();
            let lower = app.to_ascii_lowercase();
            if lower.contains("wine") || lower.contains("proton") || lower.contains("faugus") {
                versions.push(
                    json!({"name": app, "path": format!("flatpak:{app}"), "source": "flatpak"}),
                );
            }
        }
    }
    let roots = search_roots.unwrap_or(DEFAULT_PROTON_ROOTS);
    for raw in roots {
        let base = expand_user(Path::new(raw));
        if !base.is_dir() {
            continue;
        }
        let Ok(rd) = fs::read_dir(&base) else {
            continue;
        };
        for ent in rd.flatten() {
            let child = ent.path();
            if child.is_symlink() || !child.is_dir() {
                continue;
            }
            let name = child
                .file_name()
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_default();
            let lower = name.to_ascii_lowercase();
            let has_proton = child.join("proton").is_file()
                || child.join("files/bin/wine").is_file()
                || child.join("dist/bin/wine").is_file()
                || lower.contains("proton")
                || lower.contains("wine");
            if has_proton {
                let key = fs::canonicalize(&child)
                    .unwrap_or_else(|_| child.clone())
                    .to_string_lossy()
                    .into_owned();
                if seen.insert(key) {
                    versions.push(json!({
                        "name": name,
                        "path": child.to_string_lossy(),
                        "source": base.to_string_lossy(),
                    }));
                }
            }
        }
    }
    versions.sort_by(|a, b| a["name"].as_str().cmp(&b["name"].as_str()));
    versions
}

pub fn get_prefix_for_game(game: &Value) -> String {
    if !IS_LINUX {
        return String::new();
    }
    for key in ["wine_prefix", "prefix", "WINEPREFIX"] {
        if let Some(val) = game.get(key).and_then(Value::as_str)
            && !val.is_empty()
        {
            let p = expand_user(Path::new(val));
            if p.is_dir() {
                return p.to_string_lossy().into_owned();
            }
        }
    }
    let launch = game.get("launch").and_then(Value::as_str).unwrap_or("");
    if launch.contains("WINEPREFIX=")
        && let Ok(parts) = split_command(launch)
    {
        for part in parts {
            if let Some(rest) = part.strip_prefix("WINEPREFIX=") {
                let p = expand_user(Path::new(rest.trim()));
                let s = p.to_string_lossy().into_owned();
                if !s.is_empty() {
                    return s;
                }
            }
        }
    }
    String::new()
}
