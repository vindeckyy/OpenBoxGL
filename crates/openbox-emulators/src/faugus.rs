//! Faugus Launcher integration via UMU-Launcher. Mirrors `pkg/parity/parity_faugus.py`.

use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};

use openbox_platform::{IS_LINUX, expand_user};
use serde_json::{Value, json};

const FAUGUS_DIRS: &[&str] = &[
    "~/.config/faugus-launcher",
    "~/.local/share/faugus-launcher",
    "~/Faugus",
    "~/.config/faugus-launcher/prefixes",
    "~/.local/share/faugus-launcher/prefixes",
];

pub fn find_faugus_data_dirs() -> Vec<String> {
    if !IS_LINUX {
        return Vec::new();
    }
    let mut found = HashSet::new();
    for raw in FAUGUS_DIRS {
        let p = expand_user(Path::new(raw));
        if p.is_dir() {
            found.insert(p.to_string_lossy().into_owned());
        }
    }
    for env_key in ["XDG_CONFIG_HOME", "XDG_DATA_HOME"] {
        if let Ok(base) = std::env::var(env_key)
            && !base.trim().is_empty()
        {
            for sub in ["faugus-launcher", "faugus"] {
                let p = Path::new(base.trim()).join(sub);
                if p.is_dir() {
                    found.insert(p.to_string_lossy().into_owned());
                }
            }
        }
    }
    let mut list: Vec<String> = found.into_iter().collect();
    list.sort();
    list
}

fn collect_prefix_dirs(base: &Path) -> Vec<PathBuf> {
    if !base.is_dir() || base.is_symlink() {
        return Vec::new();
    }
    let mut roots = vec![base.to_path_buf()];
    let nested = base.join("prefixes");
    if nested.is_dir() && !nested.is_symlink() {
        roots.push(nested);
    }
    let mut out = Vec::new();
    let mut seen = HashSet::new();
    for root in roots {
        let Ok(rd) = fs::read_dir(&root) else {
            continue;
        };
        for ent in rd.flatten() {
            let child = ent.path();
            if child.is_symlink() || !child.is_dir() || !child.join("drive_c").is_dir() {
                continue;
            }
            let key = fs::canonicalize(&child)
                .unwrap_or_else(|_| child.clone())
                .to_string_lossy()
                .into_owned();
            if seen.insert(key) {
                out.push(child);
            }
        }
    }
    if base.join("drive_c").is_dir() {
        out.push(base.to_path_buf());
    }
    out
}

pub fn scan_faugus_games(data_dirs: Option<&[String]>) -> Vec<Value> {
    let owned_dirs;
    let dirs = match data_dirs {
        Some(d) => d,
        None => {
            owned_dirs = find_faugus_data_dirs();
            &owned_dirs
        }
    };
    let mut candidates = Vec::new();
    let mut seen = HashSet::new();

    for raw in dirs {
        let base = Path::new(raw);
        if !base.is_dir() {
            continue;
        }
        let search_dirs = [base.to_path_buf(), base.join("games"), base.join("configs")];
        for sdir in search_dirs {
            let Ok(rd) = fs::read_dir(&sdir) else {
                continue;
            };
            for ent in rd.flatten() {
                let cfg = ent.path();
                if cfg.is_symlink()
                    || !cfg.is_file()
                    || cfg.extension().and_then(|e| e.to_str()) != Some("json")
                {
                    continue;
                }
                let Ok(text) = fs::read_to_string(&cfg) else {
                    continue;
                };
                let Ok(Value::Object(obj)) = serde_json::from_str::<Value>(&text) else {
                    continue;
                };
                let stem = cfg
                    .file_stem()
                    .map(|s| s.to_string_lossy().into_owned())
                    .unwrap_or_default();
                let name = obj
                    .get("name")
                    .or_else(|| obj.get("title"))
                    .or_else(|| obj.get("game_name"))
                    .and_then(Value::as_str)
                    .map(str::trim)
                    .filter(|s| !s.is_empty())
                    .unwrap_or(&stem)
                    .to_owned();
                let game_id = obj
                    .get("game_id")
                    .or_else(|| obj.get("id"))
                    .or_else(|| obj.get("GAMEID"))
                    .and_then(Value::as_str)
                    .map(str::trim)
                    .filter(|s| !s.is_empty())
                    .unwrap_or(&stem)
                    .to_owned();
                if name.is_empty() || game_id.is_empty() {
                    continue;
                }
                let identity = format!("faugus:{game_id}");
                if !seen.insert(identity.clone()) {
                    continue;
                }
                let prefix = obj
                    .get("prefix")
                    .or_else(|| obj.get("WINEPREFIX"))
                    .or_else(|| obj.get("wine_prefix"))
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .trim()
                    .to_owned();
                let exe = obj
                    .get("exe")
                    .or_else(|| obj.get("path"))
                    .or_else(|| obj.get("executable"))
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .trim()
                    .to_owned();
                let runner = obj
                    .get("runner")
                    .or_else(|| obj.get("PROTONPATH"))
                    .or_else(|| obj.get("proton"))
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .trim()
                    .to_owned();
                candidates.push(json!({
                    "source": "Faugus",
                    "source_identity": identity,
                    "faugus_id": game_id,
                    "name": name,
                    "path": exe,
                    "prefix": prefix,
                    "runner": runner,
                    "config_path": cfg.to_string_lossy(),
                }));
            }
        }

        for prefix in collect_prefix_dirs(base) {
            let game_id = prefix
                .file_name()
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_default();
            let identity = format!("faugus:{game_id}");
            if !seen.insert(identity.clone()) {
                continue;
            }
            let title = game_id
                .replace('-', " ")
                .split_whitespace()
                .map(|w| {
                    let mut chars = w.chars();
                    match chars.next() {
                        Some(f) => format!("{}{}", f.to_uppercase(), chars.as_str()),
                        None => String::new(),
                    }
                })
                .collect::<Vec<_>>()
                .join(" ");
            candidates.push(json!({
                "source": "Faugus",
                "source_identity": identity,
                "faugus_id": game_id,
                "name": title,
                "path": "",
                "prefix": prefix.to_string_lossy(),
                "runner": "",
                "config_path": "",
            }));
        }
    }
    candidates.sort_by(|a, b| {
        a["name"]
            .as_str()
            .unwrap_or("")
            .to_ascii_lowercase()
            .cmp(&b["name"].as_str().unwrap_or("").to_ascii_lowercase())
    });
    candidates
}
