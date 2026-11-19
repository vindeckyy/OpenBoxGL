//! Native GOG, EA app, and Ubisoft Connect discovery on Windows
//! (`pkg/parity/parity_storefront_windows.py`).

use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use openbox_platform::{
    expand_user, join_command, resolve_non_strict, split_command, windows_registry_tree,
};
use quick_xml::Reader;
use quick_xml::events::Event;
use regex::Regex;
use serde_json::{Value, json};

static ID_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[0-9]{1,32}$").expect("valid windows store id regex"));

const GOG_REGISTRY: &[(&str, &str)] = &[
    ("HKCU", r"Software\GOG.com\Games"),
    ("HKLM", r"SOFTWARE\WOW6432Node\GOG.com\Games"),
    ("HKLM", r"SOFTWARE\GOG.com\Games"),
];
const EA_REGISTRY: &[(&str, &str)] = &[
    ("HKLM", r"SOFTWARE\EA Games"),
    ("HKLM", r"SOFTWARE\WOW6432Node\EA Games"),
];
const UBISOFT_REGISTRY: &[(&str, &str)] = &[
    ("HKCU", r"SOFTWARE\Ubisoft\Launcher\Installs"),
    ("HKLM", r"SOFTWARE\WOW6432Node\Ubisoft\Launcher\Installs"),
];

const MAX_INFO_BYTES: usize = 1_048_576;
const MAX_XML_BYTES: u64 = 2_097_152;
const MAX_INFO_DIRS: usize = 100;

fn valid_id(raw: &str) -> String {
    let t = raw.trim();
    if ID_RE.is_match(t) {
        t.to_owned()
    } else {
        String::new()
    }
}

fn valid_dir(raw: &str) -> Option<PathBuf> {
    let text = raw.trim().trim_matches('"');
    if text.is_empty() {
        return None;
    }
    let path = expand_user(Path::new(text));
    if path.is_absolute() && path.is_dir() {
        Some(path)
    } else {
        None
    }
}

fn is_inside(base: &Path, target: &Path) -> bool {
    let root = resolve_non_strict(base);
    let cand = resolve_non_strict(target);
    cand.starts_with(&root)
}

fn find_gog_info(install_dir: &Path, game_id: &str) -> Option<PathBuf> {
    let direct = install_dir.join(format!("goggame-{game_id}.info"));
    if direct.is_file() {
        return Some(direct);
    }
    let mut entries: Vec<PathBuf> = fs::read_dir(install_dir)
        .ok()?
        .filter_map(Result::ok)
        .map(|e| e.path())
        .collect();
    entries.sort();
    let mut top = Vec::new();
    let mut nested = Vec::new();
    for entry in entries.into_iter().take(MAX_INFO_DIRS) {
        if entry.is_file() {
            let name = entry.file_name().and_then(|n| n.to_str()).unwrap_or("");
            if name.starts_with("goggame-") && name.ends_with(".info") {
                top.push(entry);
            }
        } else if entry.is_dir()
            && let Ok(rd) = fs::read_dir(&entry)
        {
            let mut children: Vec<PathBuf> = rd
                .filter_map(Result::ok)
                .map(|e| e.path())
                .filter(|p| {
                    p.is_file()
                        && p.file_name()
                            .and_then(|n| n.to_str())
                            .is_some_and(|n| n.starts_with("goggame-") && n.ends_with(".info"))
                })
                .collect();
            children.sort();
            nested.extend(children);
        }
    }
    let mut candidates = top;
    candidates.extend(nested);
    candidates
        .iter()
        .find(|p| {
            p.file_stem()
                .and_then(|s| s.to_str())
                .is_some_and(|s| s.ends_with(game_id))
        })
        .cloned()
        .or_else(|| candidates.into_iter().next())
}

fn explorer_exe() -> String {
    let root = std::env::var("SYSTEMROOT").unwrap_or_else(|_| r"C:\Windows".to_owned());
    PathBuf::from(root)
        .join("explorer.exe")
        .to_string_lossy()
        .into_owned()
}

pub fn import_gog(errors: &mut Vec<String>) -> Vec<Value> {
    let mut games = Vec::new();
    let mut seen = HashSet::new();
    for &(root, path) in GOG_REGISTRY {
        let tree = windows_registry_tree(root, path);
        for (key, values) in tree {
            let game_id = valid_id(&key);
            if game_id.is_empty() || seen.contains(&game_id) {
                continue;
            }
            let install_dir = values.get("path").and_then(|v| valid_dir(v)).or_else(|| {
                values.get("exe").and_then(|v| {
                    let p = expand_user(Path::new(v.trim().trim_matches('"')));
                    if p.is_absolute() && p.is_file() {
                        p.parent().map(Path::to_path_buf)
                    } else {
                        None
                    }
                })
            });
            let Some(install_dir) = install_dir else {
                errors.push(format!("GOG {game_id}: install directory is missing"));
                continue;
            };
            let info: Option<Value> = find_gog_info(&install_dir, &game_id).and_then(|p| {
                let text = fs::read_to_string(p).ok()?;
                let trimmed = text.trim_start_matches('\u{feff}');
                if trimmed.len() > MAX_INFO_BYTES {
                    return None;
                }
                let val: Value = serde_json::from_str(trimmed).ok()?;
                if let Some(info_id) = val.get("gameId").and_then(Value::as_str).map(valid_id)
                    && !info_id.is_empty()
                    && info_id != game_id
                {
                    return None;
                }
                Some(val)
            });
            let name = info
                .as_ref()
                .and_then(|i| i.get("name").and_then(Value::as_str))
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .or_else(|| {
                    values
                        .get("gameName")
                        .map(|s| s.trim())
                        .filter(|s| !s.is_empty())
                })
                .or_else(|| install_dir.file_name().and_then(|n| n.to_str()))
                .unwrap_or(&game_id)
                .to_owned();

            let mut exe = String::new();
            let mut args: Vec<String> = Vec::new();
            if let Some(tasks) = info
                .as_ref()
                .and_then(|i| i.get("playTasks").and_then(Value::as_array))
            {
                let chosen = tasks
                    .iter()
                    .find(|t| t.get("isPrimary").and_then(Value::as_bool).unwrap_or(false))
                    .or_else(|| {
                        tasks.iter().find(|t| {
                            t.get("path")
                                .and_then(Value::as_str)
                                .is_some_and(|s| !s.is_empty())
                        })
                    });
                if let Some(task) = chosen
                    && let Some(rel) = task.get("path").and_then(Value::as_str)
                {
                    let cand = install_dir.join(rel.replace('\\', std::path::MAIN_SEPARATOR_STR));
                    if cand.is_file() && is_inside(&install_dir, &cand) {
                        exe = cand.to_string_lossy().into_owned();
                        if let Some(a) = task.get("arguments").and_then(Value::as_str)
                            && !a.trim().is_empty()
                        {
                            args = split_command(a.trim()).unwrap_or_default();
                        }
                    }
                }
            }
            if exe.is_empty()
                && let Some(raw_exe) = values.get("exe").map(|s| s.trim().trim_matches('"'))
                && !raw_exe.is_empty()
            {
                let cand = install_dir.join(raw_exe.replace('\\', std::path::MAIN_SEPARATOR_STR));
                if cand.is_file() && is_inside(&install_dir, &cand) {
                    exe = cand.to_string_lossy().into_owned();
                    if let Some(a) = values.get("launchParam")
                        && !a.trim().is_empty()
                    {
                        args = split_command(a.trim()).unwrap_or_default();
                    }
                }
            }
            if exe.is_empty() {
                errors.push(format!(
                    "GOG {name}: no launchable executable in {}",
                    install_dir.display()
                ));
                continue;
            }
            let launch = if args.is_empty() {
                String::new()
            } else {
                let mut argv = vec![exe.clone()];
                argv.extend(args);
                join_command(&argv)
            };
            seen.insert(game_id.clone());
            games.push(json!({
                "name": name,
                "platform": "PC",
                "source": "GOG",
                "collection": "GOG",
                "path": exe,
                "launch": launch,
                "gog_id": game_id,
                "install_dir": install_dir.to_string_lossy(),
            }));
        }
    }
    games
}

fn ea_content_ids(path: &Path) -> Vec<String> {
    let Ok(meta) = fs::metadata(path) else {
        return Vec::new();
    };
    if meta.len() > MAX_XML_BYTES {
        return Vec::new();
    }
    let Ok(text) = fs::read_to_string(path) else {
        return Vec::new();
    };
    let mut reader = Reader::from_str(&text);
    let mut in_content_id = false;
    let mut ids = Vec::new();
    let mut buf = Vec::new();
    loop {
        match reader.read_event_into(&mut buf) {
            Ok(Event::Start(e)) => {
                in_content_id = e.name().as_ref() == "contentID";
            }
            Ok(Event::End(_)) => {
                in_content_id = false;
            }
            Ok(Event::Text(t)) if in_content_id => {
                let s = quick_xml::escape::unescape(t.as_ref())
                    .unwrap_or_else(|_| std::borrow::Cow::Borrowed(t.as_ref()));
                let v = valid_id(&s);
                if !v.is_empty() && !ids.contains(&v) {
                    ids.push(v);
                }
            }
            Ok(Event::Eof) | Err(_) => break,
            _ => {}
        }
        buf.clear();
    }
    ids.sort();
    ids
}

pub fn import_ea(errors: &mut Vec<String>) -> Vec<Value> {
    let explorer = explorer_exe();
    let mut roots = Vec::new();
    for env_key in ["ProgramFiles", "ProgramFiles(x86)"] {
        if let Ok(val) = std::env::var(env_key) {
            roots.push(PathBuf::from(val).join("EA Games"));
        }
    }
    for &(root, path) in EA_REGISTRY {
        let tree = windows_registry_tree(root, path);
        for values in tree.values() {
            if let Some(dir) = values
                .get("Install Dir")
                .or_else(|| values.get("InstallDir"))
                .or_else(|| values.get("Path"))
            {
                roots.push(PathBuf::from(dir));
            }
        }
    }
    let mut seen_roots = HashSet::new();
    let mut games = Vec::new();
    let mut seen = HashSet::new();
    for root in roots {
        let key = root.to_string_lossy().to_lowercase();
        if !seen_roots.insert(key) || !root.is_dir() {
            continue;
        }
        let mut children = vec![root.clone()];
        if let Ok(rd) = fs::read_dir(&root) {
            let mut sub: Vec<PathBuf> = rd.filter_map(Result::ok).map(|e| e.path()).collect();
            sub.sort();
            children.extend(sub);
        }
        for game_dir in children {
            if !game_dir.is_dir() {
                continue;
            }
            let installer_dir = game_dir.join("__Installer");
            if !installer_dir.is_dir() {
                continue;
            }
            let content_ids = ea_content_ids(&installer_dir.join("installerdata.xml"));
            let game_name = game_dir
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or("")
                .to_owned();
            if content_ids.is_empty() {
                errors.push(format!("EA {game_name}: no installerdata.xml content id"));
                continue;
            }
            let game_id = content_ids[0].clone();
            if !seen.insert(game_id.clone()) {
                continue;
            }
            let offer_ids = content_ids.join(",");
            let uri = format!("origin2://game/launch?offerIds={offer_ids}&autoDownload=1");
            games.push(json!({
                "name": game_name,
                "platform": "PC",
                "source": "EA",
                "collection": "EA",
                "path": explorer,
                "launch": join_command(&[explorer.clone(), uri]),
                "ea_id": game_id,
                "install_dir": game_dir.to_string_lossy(),
            }));
        }
    }
    games
}

pub fn import_ubisoft(errors: &mut Vec<String>) -> Vec<Value> {
    let explorer = explorer_exe();
    let mut games = Vec::new();
    let mut seen = HashSet::new();
    for &(root, path) in UBISOFT_REGISTRY {
        let tree = windows_registry_tree(root, path);
        for (key, values) in tree {
            let game_id = valid_id(&key);
            if game_id.is_empty() || seen.contains(&game_id) {
                continue;
            }
            let Some(install_dir) = values.get("InstallDir").and_then(|v| valid_dir(v)) else {
                errors.push(format!("Ubisoft {game_id}: install directory is missing"));
                continue;
            };
            seen.insert(game_id.clone());
            let name = install_dir
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or(&game_id)
                .to_owned();
            let uri = format!("uplay://launch/{game_id}/0");
            games.push(json!({
                "name": name,
                "platform": "PC",
                "source": "Ubisoft",
                "collection": "Ubisoft",
                "path": explorer,
                "launch": join_command(&[explorer.clone(), uri]),
                "ubisoft_id": game_id,
                "install_dir": install_dir.to_string_lossy(),
            }));
        }
    }
    games
}
