//! Multi-platform folder scanning, M3U/CUE parsing, ROM ranking, ScummVM/RPCS3/Vita3K,
//! BIOS dependency hints, and profile discovery (`pkg/parity/parity_import.py`).

use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use openbox_core::ApiError;
use openbox_emulators::{
    Adapter, build_adapter_argv, build_platform_by_extension, build_platform_emulators,
    detect_adapter_prefix, load_adapters, parse_yaml_def,
};
use openbox_io::atomic_write_text;
use openbox_platform::{
    IS_MACOS, IS_WINDOWS, display_name_for_file, expand_user, home_dir, join_command,
    resolve_non_strict, which,
};
use regex::Regex;
use serde_json::{Value, json};

use crate::bulk_edit::{pick_best_rom, rank_rom_group, resolve_vita_title};

pub const BASE_EXTENSIONS: &[&str] = &[
    ".sh",
    ".appimage",
    ".exe",
    ".bat",
    ".cmd",
    ".lnk",
    ".iso",
    ".rom",
    ".nes",
    ".sfc",
    ".smc",
    ".gba",
    ".gb",
    ".gbc",
    ".zip",
    ".7z",
    ".rar",
    ".m3u",
    ".cue",
    ".chd",
    ".wbfs",
    ".rvz",
    ".n64",
    ".z64",
    ".v64",
    ".nds",
    ".3ds",
    ".cia",
    ".pbp",
    ".vpk",
    ".xci",
    ".nsp",
    ".wad",
    ".ciso",
    ".gcm",
    ".gcz",
    ".wia",
    ".dol",
    ".elf",
    ".xex",
];

pub const PLATFORM_BY_EXTENSION_EXTRA: &[(&str, &str)] = &[
    (".nes", "NES"),
    (".sfc", "SNES"),
    (".smc", "SNES"),
    (".gba", "Game Boy Advance"),
    (".gb", "Game Boy"),
    (".gbc", "Game Boy Color"),
    (".n64", "Nintendo 64"),
    (".z64", "Nintendo 64"),
    (".v64", "Nintendo 64"),
    (".nds", "Nintendo DS"),
    (".3ds", "Nintendo 3DS"),
    (".cia", "Nintendo 3DS"),
    (".wbfs", "Wii"),
    (".rvz", "GameCube"),
    (".gcm", "GameCube"),
    (".gcz", "GameCube"),
    (".wia", "Wii"),
    (".wad", "WiiWare"),
    (".pbp", "PSP"),
    (".vpk", "PlayStation Vita"),
    (".xci", "Nintendo Switch"),
    (".nsp", "Nintendo Switch"),
    (".chd", "Disc image"),
    (".cue", "Disc image"),
    (".ciso", "Disc image"),
    (".xex", "Xbox 360"),
    (".iso", "Disc image"),
    (".m3u", "Disc image"),
];

static DISC_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)[\s._-]*(?:\(|\[)?(?:disc|disk|cd|dvd|side)\s*([0-9a-d]+)(?:\s+of\s+\d+)?(?:\)|\])?\s*$")
        .expect("valid disc regex")
});
static CUE_FILE_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"(?i)FILE\s+["']?([^"']+)["']?\s+(\w+)"#).expect("valid cue regex")
});
static ROM_TAGS_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\(.*?\)|\[.*?\]").expect("valid rom tags regex"));
static NON_ALPHANUM_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"[^a-z0-9]+").expect("valid non alphanum regex"));
static RPCS3_ABS_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^(?:/|~|[A-Za-z]:[\\/])").expect("valid rpcs3 abs regex"));

pub fn platform_by_extension(adapters: &[Adapter]) -> HashMap<String, String> {
    let mut map = HashMap::new();
    for (ext, plat) in [
        (".nes", "NES"),
        (".sfc", "SNES"),
        (".smc", "SNES"),
        (".gba", "Game Boy Advance"),
        (".gb", "Game Boy"),
        (".gbc", "Game Boy Color"),
        (".iso", "Disc image"),
    ] {
        map.insert(ext.to_owned(), plat.to_owned());
    }
    for (ext, plat) in build_platform_by_extension(adapters) {
        map.insert(ext, plat);
    }
    for &(ext, plat) in PLATFORM_BY_EXTENSION_EXTRA {
        map.insert(ext.to_owned(), plat.to_owned());
    }
    map
}

pub fn extensions_set(adapters: &[Adapter]) -> HashSet<String> {
    let mut set: HashSet<String> = BASE_EXTENSIONS.iter().map(|s| (*s).to_owned()).collect();
    for ext in build_platform_by_extension(adapters).into_keys() {
        if ext.starts_with('.') {
            set.insert(ext);
        } else {
            set.insert(format!(".{ext}"));
        }
    }
    set
}

pub fn recommend_emulators(platform: &str, adapters: &[Adapter]) -> Vec<Value> {
    let map = build_platform_emulators(adapters);
    map.get(platform)
        .map(|items| {
            items
                .iter()
                .map(|(app_id, name)| {
                    json!({
                        "app_id": app_id,
                        "name": name,
                        "platform": platform,
                    })
                })
                .collect()
        })
        .unwrap_or_default()
}

pub fn generate_m3u(disc_paths: &[PathBuf], output_path: &Path) -> std::io::Result<PathBuf> {
    let parent = output_path.parent().unwrap_or_else(|| Path::new("."));
    let mut lines = Vec::new();
    for path in disc_paths {
        match path.strip_prefix(parent) {
            Ok(rel) => lines.push(rel.to_string_lossy().into_owned()),
            Err(_) => lines.push(path.to_string_lossy().into_owned()),
        }
    }
    atomic_write_text(output_path, &format!("{}\n", lines.join("\n")), 0o600)?;
    Ok(output_path.to_path_buf())
}

fn write_m3u_playlist(
    group: &[PathBuf],
    base: &str,
    m3u_dir: Option<&Path>,
    data_dir: &Path,
) -> Option<PathBuf> {
    let mut targets = Vec::new();
    if let Some(d) = m3u_dir {
        targets.push(d.to_path_buf());
    } else if let Some(p) = group[0].parent() {
        targets.push(p.to_path_buf());
    }
    let generated = data_dir.join("generated");
    if !targets.contains(&generated) {
        targets.push(generated);
    }
    for dir in targets {
        let candidate = dir.join(format!("{base}.m3u"));
        if candidate.exists() {
            return None;
        }
        if fs::create_dir_all(&dir).is_ok()
            && let Ok(written) = generate_m3u(group, &candidate)
        {
            return Some(written);
        }
    }
    None
}

pub fn parse_m3u(m3u_path: &Path) -> Vec<PathBuf> {
    let Ok(content) = fs::read_to_string(m3u_path) else {
        return Vec::new();
    };
    let parent = m3u_path.parent().unwrap_or_else(|| Path::new("."));
    let mut entries = Vec::new();
    for line in content.lines() {
        let trimmed = line.trim();
        if trimmed.is_empty() || trimmed.starts_with('#') {
            continue;
        }
        let lp = PathBuf::from(trimmed);
        let target = if lp.is_absolute() {
            lp
        } else {
            fs::canonicalize(parent.join(&lp))
                .unwrap_or_else(|_| resolve_non_strict(&parent.join(&lp)))
        };
        entries.push(target);
    }
    entries
}

pub fn parse_cue(cue_path: &Path) -> Vec<PathBuf> {
    let Ok(content) = fs::read_to_string(cue_path) else {
        return Vec::new();
    };
    let parent = cue_path.parent().unwrap_or_else(|| Path::new("."));
    let mut entries = Vec::new();
    for cap in CUE_FILE_RE.captures_iter(&content) {
        if let Some(m) = cap.get(1) {
            let filename = m.as_str().trim();
            if filename.is_empty() {
                continue;
            }
            let fp = PathBuf::from(filename);
            let target = if fp.is_absolute() {
                fp
            } else {
                fs::canonicalize(parent.join(&fp))
                    .unwrap_or_else(|_| resolve_non_strict(&parent.join(&fp)))
            };
            entries.push(target);
        }
    }
    entries
}

fn disc_match_start(stem: &str) -> Option<usize> {
    let m = DISC_RE.find(stem)?;
    let matched = m.as_str();
    let lower = matched.to_lowercase();
    for kw in ["disc", "disk", "cd", "dvd", "side"] {
        if let Some(pos) = lower.find(kw) {
            let abs_pos = m.start() + pos;
            if abs_pos > 0 {
                let prev = stem[..abs_pos].chars().next_back().unwrap_or(' ');
                if prev.is_ascii_alphanumeric() {
                    return None;
                }
            }
            return Some(m.start());
        }
    }
    None
}

fn disc_base(path: &Path) -> String {
    let stem = path.file_stem().and_then(|s| s.to_str()).unwrap_or("");
    let cleaned = match disc_match_start(stem) {
        Some(idx) => stem[..idx].trim_matches(|c: char| " ._-\t".contains(c)),
        None => stem,
    };
    let target = if cleaned.is_empty() { stem } else { cleaned };
    display_name_for_file(target)
}

pub fn group_multi_disc(paths: &[PathBuf]) -> Vec<Vec<PathBuf>> {
    let mut groups: HashMap<(PathBuf, String, String), Vec<PathBuf>> = HashMap::new();
    let mut group_order: Vec<(PathBuf, String, String)> = Vec::new();
    let mut singles: Vec<PathBuf> = Vec::new();
    for path in paths {
        let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
        let (stem, suffix) = match name.rfind('.') {
            Some(idx) => (&name[..idx], name[idx..].to_lowercase()),
            None => (name, String::new()),
        };
        if let Some(start) = disc_match_start(stem) {
            let base = {
                let b = stem[..start]
                    .trim_matches(|c: char| " ._-\t".contains(c))
                    .to_lowercase();
                if b.is_empty() { stem.to_lowercase() } else { b }
            };
            let key = (
                path.parent().unwrap_or_else(|| Path::new("")).to_path_buf(),
                base,
                suffix,
            );
            if !groups.contains_key(&key) {
                group_order.push(key.clone());
            }
            groups.entry(key).or_default().push(path.clone());
        } else {
            singles.push(path.clone());
        }
    }
    let mut result = Vec::new();
    for key in group_order {
        if let Some(mut items) = groups.remove(&key) {
            if items.len() > 1 {
                items.sort_by_key(|p| {
                    p.file_name()
                        .and_then(|n| n.to_str())
                        .unwrap_or("")
                        .to_lowercase()
                });
                result.push(items);
            } else {
                singles.extend(items);
            }
        }
    }
    for s in singles {
        result.push(vec![s]);
    }
    result
}

fn scan_directory(folder: &Path, extensions: &HashSet<String>) -> Result<Vec<PathBuf>, ApiError> {
    if !folder.is_dir() {
        return Err(ApiError::bad_request(format!(
            "Folder does not exist: {}",
            folder.display()
        )));
    }
    let ext_set: HashSet<String> = extensions
        .iter()
        .map(|e| {
            let l = e.to_lowercase();
            if l.starts_with('.') {
                l
            } else {
                format!(".{l}")
            }
        })
        .collect();
    let mut seen_dirs = HashSet::new();
    let real_root = fs::canonicalize(folder).unwrap_or_else(|_| folder.to_path_buf());
    seen_dirs.insert(real_root);
    let mut queue = VecDeque::new();
    queue.push_back(folder.to_path_buf());
    let mut found = Vec::new();
    while let Some(dir) = queue.pop_front() {
        let Ok(rd) = fs::read_dir(&dir) else {
            continue;
        };
        for entry in rd.filter_map(Result::ok) {
            let path = entry.path();
            let Ok(ft) = entry.file_type() else {
                continue;
            };
            if ft.is_dir() {
                let real = fs::canonicalize(&path).unwrap_or_else(|_| path.clone());
                if seen_dirs.insert(real) {
                    queue.push_back(path);
                }
            } else if ft.is_file() || (ft.is_symlink() && path.is_file()) {
                let name = entry.file_name().to_string_lossy().into_owned();
                if let Some(idx) = name.rfind('.')
                    && ext_set.contains(&name[idx..].to_lowercase())
                {
                    found.push(path);
                }
            }
        }
    }
    found.sort_by(|a, b| {
        let pa = a.parent().map(|p| p.to_string_lossy()).unwrap_or_default();
        let pb = b.parent().map(|p| p.to_string_lossy()).unwrap_or_default();
        let na = a
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("")
            .to_lowercase();
        let nb = b
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("")
            .to_lowercase();
        pa.cmp(&pb).then_with(|| na.cmp(&nb))
    });
    Ok(found)
}

fn sheet_platform(
    sheet: &Path,
    platform_map: &HashMap<String, String>,
    fallback: &str,
    initial_targets: Option<&[PathBuf]>,
) -> String {
    let mut pending: VecDeque<(PathBuf, Option<Vec<PathBuf>>)> = VecDeque::new();
    pending.push_back((sheet.to_path_buf(), initial_targets.map(|s| s.to_vec())));
    let mut seen = HashSet::new();
    while let Some((current, cur_targets)) = pending.pop_front() {
        if !seen.insert(current.clone()) {
            continue;
        }
        let targets = cur_targets.unwrap_or_else(|| {
            let ext = current
                .extension()
                .and_then(|e| e.to_str())
                .unwrap_or("")
                .to_lowercase();
            if ext == "cue" {
                parse_cue(&current)
            } else {
                parse_m3u(&current)
            }
        });
        for target in targets {
            let ext = target
                .extension()
                .and_then(|e| e.to_str())
                .map(|e| format!(".{}", e.to_lowercase()))
                .unwrap_or_default();
            if !matches!(ext.as_str(), ".cue" | ".m3u")
                && let Some(mapped) = platform_map.get(&ext)
            {
                return mapped.clone();
            }
            if matches!(ext.as_str(), ".cue" | ".m3u") {
                pending.push_back((target, None));
            }
        }
    }
    fallback.to_owned()
}

fn clean_rom_title(name: &str) -> String {
    let cleaned = if name.contains('(') || name.contains('[') {
        ROM_TAGS_RE.replace_all(name, " ").into_owned()
    } else {
        name.to_owned()
    };
    let norm = NON_ALPHANUM_RE
        .replace_all(&cleaned.to_lowercase(), " ")
        .trim()
        .to_owned();
    if !norm.is_empty() {
        norm
    } else {
        NON_ALPHANUM_RE
            .replace_all(&name.to_lowercase(), " ")
            .trim()
            .to_owned()
    }
}

pub fn dedupe_ranked_imports(additions: Vec<Value>) -> Vec<Value> {
    let mut buckets: HashMap<(String, String), Vec<Value>> = HashMap::new();
    let mut order: Vec<(String, String)> = Vec::new();
    for item in additions {
        let name = item.get("name").and_then(Value::as_str).unwrap_or("");
        let platform = item
            .get("platform")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        let key = (platform, clean_rom_title(name));
        if !buckets.contains_key(&key) {
            order.push(key.clone());
        }
        buckets.entry(key).or_default().push(item);
    }
    let mut result = Vec::new();
    for key in order {
        let Some(mut items) = buckets.remove(&key) else {
            continue;
        };
        if items.len() == 1 {
            result.push(items.remove(0));
            continue;
        }
        let paths: Vec<String> = items
            .iter()
            .filter_map(|e| e.get("path").and_then(Value::as_str).map(ToOwned::to_owned))
            .collect();
        let ranked = rank_rom_group(&paths);
        let best = ranked
            .first()
            .cloned()
            .or_else(|| paths.first().cloned())
            .unwrap_or_default();
        let pos = items
            .iter()
            .position(|e| e.get("path").and_then(Value::as_str) == Some(best.as_str()))
            .unwrap_or(0);
        let mut winner = items.swap_remove(pos);
        if let Some(m) = winner.as_object_mut() {
            m.insert(
                "version_candidates".into(),
                Value::Array(ranked.into_iter().map(Value::String).collect()),
            );
        }
        result.push(winner);
    }
    result
}

pub fn import_multi_platform(
    folder: &str,
    adapters: &[Adapter],
    write_m3u: bool,
    m3u_dir: Option<&Path>,
    data_dir: &Path,
) -> Result<Vec<Value>, ApiError> {
    let root = expand_user(Path::new(folder));
    let exts = extensions_set(adapters);
    let plat_map = platform_by_extension(adapters);
    let found = scan_directory(&root, &exts)?;

    let mut referenced: HashSet<String> = HashSet::new();
    let mut sheet_targets: HashMap<PathBuf, Vec<PathBuf>> = HashMap::new();
    for path in &found {
        let ext = path
            .extension()
            .and_then(|e| e.to_str())
            .map(|e| format!(".{}", e.to_lowercase()))
            .unwrap_or_default();
        let refs = if ext == ".m3u" {
            parse_m3u(path)
        } else if ext == ".cue" {
            parse_cue(path)
        } else {
            continue;
        };
        for r in &refs {
            referenced.insert(resolve_non_strict(r).to_string_lossy().into_owned());
        }
        sheet_targets.insert(path.clone(), refs);
    }

    let filtered: Vec<PathBuf> = if referenced.is_empty() {
        found
    } else {
        found
            .into_iter()
            .filter(|p| {
                let norm = if p.exists() {
                    fs::canonicalize(p).unwrap_or_else(|_| resolve_non_strict(p))
                } else {
                    resolve_non_strict(p)
                };
                !referenced.contains(&norm.to_string_lossy().into_owned())
            })
            .collect()
    };

    let now = jiff::Timestamp::now()
        .strftime("%Y-%m-%dT%H:%M:%S")
        .to_string();
    let disc_groups = group_multi_disc(&filtered);
    let mut additions = Vec::new();
    for group in disc_groups {
        let (path, name) = if group.len() > 1 {
            let base = disc_base(&group[0]);
            let chosen = if write_m3u {
                if let Some(m3u) = write_m3u_playlist(&group, &base, m3u_dir, data_dir) {
                    sheet_targets.insert(m3u.clone(), group.clone());
                    m3u
                } else {
                    group[0].clone()
                }
            } else {
                group[0].clone()
            };
            (chosen, base)
        } else {
            let p = group[0].clone();
            let stem = p.file_stem().and_then(|s| s.to_str()).unwrap_or("");
            (p.clone(), display_name_for_file(stem))
        };
        let ext = path
            .extension()
            .and_then(|e| e.to_str())
            .map(|e| format!(".{}", e.to_lowercase()))
            .unwrap_or_default();
        let mut platform = plat_map
            .get(&ext)
            .cloned()
            .unwrap_or_else(|| "Imported".to_owned());
        if matches!(ext.as_str(), ".cue" | ".m3u") {
            platform = sheet_platform(
                &path,
                &plat_map,
                &platform,
                sheet_targets.get(&path).map(Vec::as_slice),
            );
        }
        let discs: Vec<Value> = if group.len() > 1 {
            group
                .iter()
                .map(|g| Value::String(g.to_string_lossy().into_owned()))
                .collect()
        } else {
            Vec::new()
        };
        additions.push(json!({
            "name": name,
            "platform": platform,
            "genre": "",
            "path": path.to_string_lossy(),
            "added_at": now,
            "discs": discs,
        }));
    }
    Ok(dedupe_ranked_imports(additions))
}

pub type RecommendationsMap = BTreeMap<String, Vec<Value>>;

pub fn import_with_emulator_choice(
    folder: &str,
    adapters: &[Adapter],
    chosen: &serde_json::Map<String, Value>,
    data_dir: &Path,
) -> Result<(Vec<Value>, RecommendationsMap), ApiError> {
    let candidates = import_multi_platform(folder, adapters, true, None, data_dir)?;
    let now = jiff::Timestamp::now()
        .strftime("%Y-%m-%dT%H:%M:%S")
        .to_string();
    let mut additions = Vec::new();
    let mut recommendations = BTreeMap::new();
    for mut item in candidates {
        let platform = item
            .get("platform")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        recommendations
            .entry(platform.clone())
            .or_insert_with(|| recommend_emulators(&platform, adapters));
        if let Some(m) = item.as_object_mut() {
            if let Some(em) = chosen
                .get(&platform)
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
            {
                m.insert("recommended_emulator".into(), Value::String(em.to_owned()));
            }
            if let Some(Value::Array(versions)) = m.remove("version_candidates") {
                let paths: Vec<String> = versions
                    .into_iter()
                    .filter_map(|v| v.as_str().map(ToOwned::to_owned))
                    .collect();
                let best = pick_best_rom(&paths);
                if !best.is_empty() {
                    m.insert("path".into(), Value::String(best));
                }
            }
            m.insert("added_at".into(), Value::String(now.clone()));
        }
        additions.push(item);
    }
    Ok((additions, recommendations))
}

pub fn import_scummvm(home: Option<&Path>) -> Vec<Value> {
    let home = home
        .map(Path::to_path_buf)
        .or_else(home_dir)
        .unwrap_or_else(|| PathBuf::from("."));
    let ini_paths = [
        home.join(".config/scummvm/scummvm.ini"),
        home.join(".var/app/org.scummvm.ScummVM/config/scummvm/scummvm.ini"),
    ];
    let now = jiff::Timestamp::now()
        .strftime("%Y-%m-%dT%H:%M:%S")
        .to_string();
    let mut games = Vec::new();
    for ini_path in ini_paths {
        let Ok(text) = fs::read_to_string(&ini_path) else {
            continue;
        };
        let mut sections: Vec<(String, HashMap<String, String>)> = Vec::new();
        for line in text.lines() {
            let trimmed = line.trim();
            if trimmed.is_empty() || trimmed.starts_with('#') || trimmed.starts_with(';') {
                continue;
            }
            if trimmed.starts_with('[') && trimmed.ends_with(']') {
                let sec = trimmed[1..trimmed.len() - 1].trim().to_owned();
                sections.push((sec, HashMap::new()));
            } else if let Some((k, v)) = trimmed.split_once('=')
                && let Some((_, map)) = sections.last_mut()
            {
                map.insert(k.trim().to_lowercase(), v.trim().to_owned());
            }
        }
        for (section, map) in sections {
            if matches!(section.as_str(), "scummvm" | "cloud") || section.is_empty() {
                continue;
            }
            let desc = map
                .get("description")
                .filter(|s| !s.is_empty())
                .cloned()
                .unwrap_or_else(|| section.clone());
            let path = map
                .get("path")
                .filter(|s| !s.is_empty())
                .cloned()
                .unwrap_or_else(|| ini_path.to_string_lossy().into_owned());
            games.push(json!({
                "name": desc,
                "platform": "ScummVM",
                "genre": "Adventure",
                "path": path,
                "launch": format!("scummvm {section}"),
                "source": "scummvm",
                "scummvm_id": section,
                "added_at": now,
            }));
        }
        if !games.is_empty() {
            break;
        }
    }
    games
}

fn read_sfo_title(param_sfo: &Path) -> String {
    let fallback = param_sfo
        .parent()
        .and_then(|p| p.file_name())
        .and_then(|n| n.to_str())
        .unwrap_or("")
        .to_owned();
    let Ok(data) = fs::read(param_sfo) else {
        return fallback;
    };
    if data.len() < 20 || &data[0..4] != b"\x00PSF" {
        return fallback;
    }
    let key_table = u32::from_le_bytes([data[8], data[9], data[10], data[11]]) as usize;
    let data_table = u32::from_le_bytes([data[12], data[13], data[14], data[15]]) as usize;
    let entry_count = u32::from_le_bytes([data[16], data[17], data[18], data[19]]) as usize;
    for i in 0..entry_count {
        let off = 20 + i * 16;
        if off + 16 > data.len() {
            break;
        }
        let key_offset = u16::from_le_bytes([data[off], data[off + 1]]) as usize;
        let data_fmt = u16::from_le_bytes([data[off + 2], data[off + 3]]);
        let data_len =
            u32::from_le_bytes([data[off + 4], data[off + 5], data[off + 6], data[off + 7]])
                as usize;
        let data_offset = u32::from_le_bytes([
            data[off + 12],
            data[off + 13],
            data[off + 14],
            data[off + 15],
        ]) as usize;
        let kstart = key_table.saturating_add(key_offset);
        if kstart >= data.len() {
            continue;
        }
        let kslice = &data[kstart..];
        let kend = kslice.iter().position(|&b| b == 0).unwrap_or(kslice.len());
        let key = String::from_utf8_lossy(&kslice[..kend]);
        if key == "TITLE" && matches!(data_fmt, 0x0204 | 0x0402) {
            let dstart = data_table.saturating_add(data_offset);
            let dend = dstart.saturating_add(data_len).min(data.len());
            if dstart < dend {
                let dslice = &data[dstart..dend];
                let dzero = dslice.iter().position(|&b| b == 0).unwrap_or(dslice.len());
                let title = String::from_utf8_lossy(&dslice[..dzero]).into_owned();
                if !title.is_empty() {
                    return title;
                }
            }
        }
    }
    fallback
}

fn rpcs3_looks_like_path(text: &str) -> bool {
    RPCS3_ABS_RE.is_match(text.trim())
}

fn rpcs3_path_value(val: &Value) -> Option<String> {
    match val {
        Value::Object(m) => {
            for k in ["path", "Path", "game", "Game"] {
                if let Some(s) = m.get(k).and_then(Value::as_str)
                    && rpcs3_looks_like_path(s)
                {
                    return Some(s.trim().to_owned());
                }
            }
            None
        }
        Value::String(s) if rpcs3_looks_like_path(s) => Some(s.trim().to_owned()),
        _ => None,
    }
}

pub fn import_rpcs3_hdd(home: Option<&Path>, errors: &mut Vec<String>) -> Vec<Value> {
    let home = home
        .map(Path::to_path_buf)
        .or_else(home_dir)
        .unwrap_or_else(|| PathBuf::from("."));
    let mut candidates: Vec<PathBuf> = Vec::new();
    for root in [
        home.join(".config/rpcs3/dev_hdd0/game"),
        home.join(".var/app/net.rpcs3.RPCS3/config/rpcs3/dev_hdd0/game"),
    ] {
        if !root.is_dir() {
            continue;
        }
        if let Ok(rd) = fs::read_dir(&root) {
            let mut entries: Vec<PathBuf> = rd.filter_map(Result::ok).map(|e| e.path()).collect();
            entries.sort();
            for folder in entries {
                if folder.is_dir() && folder.join("PARAM.SFO").is_file() {
                    candidates.push(folder);
                }
            }
        }
    }
    for cfg in [
        home.join(".config/rpcs3"),
        home.join(".var/app/net.rpcs3.RPCS3/config/rpcs3"),
    ] {
        for fname in ["games.yml", "config.yml"] {
            let path = cfg.join(fname);
            if !path.is_file() {
                continue;
            }
            let Ok(text) = fs::read_to_string(&path) else {
                continue;
            };
            let Ok(map) = parse_yaml_def(&text) else {
                continue;
            };
            if fname == "games.yml" {
                for (k, v) in &map {
                    if rpcs3_looks_like_path(k) {
                        candidates.push(PathBuf::from(k.trim()));
                    }
                    if let Some(p) = rpcs3_path_value(v) {
                        candidates.push(PathBuf::from(p));
                    }
                }
            } else if let Some(games_val) = map.get("games") {
                match games_val {
                    Value::Object(m) => {
                        for (k, v) in m {
                            if let Some(p) = rpcs3_path_value(v) {
                                candidates.push(PathBuf::from(p));
                            } else if rpcs3_looks_like_path(k) {
                                candidates.push(PathBuf::from(k.trim()));
                            }
                        }
                    }
                    Value::Array(arr) => {
                        for v in arr {
                            if let Some(p) = rpcs3_path_value(v) {
                                candidates.push(PathBuf::from(p));
                            }
                        }
                    }
                    _ => {}
                }
            }
        }
    }

    let now = jiff::Timestamp::now()
        .strftime("%Y-%m-%dT%H:%M:%S")
        .to_string();
    let mut games = Vec::new();
    let mut seen = HashSet::new();
    for cand in candidates {
        let expanded = expand_user(&cand);
        if !expanded.exists() {
            errors.push(format!("{}: not found", expanded.display()));
            continue;
        }
        let name = if expanded.is_dir() {
            let sfo = expanded.join("PARAM.SFO");
            if !sfo.is_file() {
                errors.push(format!("{}: no PARAM.SFO found", expanded.display()));
                continue;
            }
            read_sfo_title(&sfo)
        } else if expanded.is_file()
            && expanded
                .extension()
                .and_then(|e| e.to_str())
                .is_some_and(|e| e.eq_ignore_ascii_case("iso"))
        {
            expanded
                .file_stem()
                .and_then(|s| s.to_str())
                .unwrap_or("")
                .to_owned()
        } else {
            errors.push(format!(
                "{}: not a PlayStation 3 game folder or ISO",
                expanded.display()
            ));
            continue;
        };
        let real = fs::canonicalize(&expanded).unwrap_or_else(|_| expanded.clone());
        if !seen.insert(real) {
            continue;
        }
        let pstr = expanded.to_string_lossy().into_owned();
        games.push(json!({
            "name": name,
            "platform": "PlayStation 3",
            "genre": "",
            "path": pstr,
            "launch": format!("rpcs3 {pstr}"),
            "source": "rpcs3",
            "added_at": now,
        }));
    }
    games
}

pub fn import_vita3k(home: Option<&Path>) -> Vec<Value> {
    let home = home
        .map(Path::to_path_buf)
        .or_else(home_dir)
        .unwrap_or_else(|| PathBuf::from("."));
    let roots = [
        home.join(".local/share/Vita3K/Vita3K/ux0/app"),
        home.join(".config/Vita3K/ux0/app"),
        home.join(".var/app/org.vita3k.Vita3K/data/Vita3K/Vita3K/ux0/app"),
    ];
    let now = jiff::Timestamp::now()
        .strftime("%Y-%m-%dT%H:%M:%S")
        .to_string();
    let mut games = Vec::new();
    for root in roots {
        if !root.is_dir() {
            continue;
        }
        if let Ok(rd) = fs::read_dir(&root) {
            let mut entries: Vec<PathBuf> = rd.filter_map(Result::ok).map(|e| e.path()).collect();
            entries.sort();
            for folder in entries {
                if !folder.is_dir() {
                    continue;
                }
                let sfo = folder.join("sce_sys/param.sfo");
                let fname = folder.file_name().and_then(|n| n.to_str()).unwrap_or("");
                let sfo_title = if sfo.is_file() {
                    read_sfo_title(&sfo)
                } else {
                    fname.to_owned()
                };
                let title = resolve_vita_title(&sfo_title, fname, &sfo_title);
                games.push(json!({
                    "name": title,
                    "platform": "PlayStation Vita",
                    "genre": "",
                    "path": folder.to_string_lossy(),
                    "source": "vita3k",
                    "added_at": now,
                }));
            }
        }
        if !games.is_empty() {
            break;
        }
    }
    games
}

pub fn detect_dependencies(emulator_name: &str, home: Option<&Path>) -> Value {
    let home = home
        .map(Path::to_path_buf)
        .or_else(home_dir)
        .unwrap_or_else(|| PathBuf::from("."));
    let hints: Vec<(&str, PathBuf)> = if IS_MACOS {
        let lib = home.join("Library/Application Support");
        match emulator_name {
            "DuckStation" => vec![("PSX BIOS (scph1001.bin)", lib.join("DuckStation/bios"))],
            "RPCS3" => vec![("PS3 firmware (dev_flash)", lib.join("rpcs3/dev_flash"))],
            "RetroArch" => vec![(
                "System/BIOS directory",
                home.join("Documents/RetroArch/system"),
            )],
            _ => Vec::new(),
        }
    } else {
        match emulator_name {
            "DuckStation" => vec![
                (
                    "PSX BIOS (scph1001.bin)",
                    home.join(".local/share/duckstation/bios"),
                ),
                (
                    "PSX BIOS (scph1001.bin)",
                    home.join(".var/app/org.duckstation.DuckStation/data/duckstation/bios"),
                ),
            ],
            "PCSX2" => vec![
                ("PS2 BIOS folder", home.join(".config/PCSX2/bios")),
                (
                    "PS2 BIOS folder",
                    home.join(".var/app/net.pcsx2.PCSX2/config/PCSX2/bios"),
                ),
            ],
            "RPCS3" => vec![
                (
                    "PS3 firmware (dev_flash)",
                    home.join(".config/rpcs3/dev_flash"),
                ),
                (
                    "PS3 firmware (dev_flash)",
                    home.join(".var/app/net.rpcs3.RPCS3/config/rpcs3/dev_flash"),
                ),
            ],
            "RetroArch" => vec![
                (
                    "System/BIOS directory",
                    home.join(".config/retroarch/system"),
                ),
                (
                    "System/BIOS directory",
                    home.join(".var/app/org.libretro.RetroArch/config/retroarch/system"),
                ),
            ],
            _ => Vec::new(),
        }
    };
    let mut required = Vec::new();
    let mut missing = Vec::new();
    for (label, path) in hints {
        let found = path.is_file()
            || (path.is_dir()
                && fs::read_dir(&path)
                    .ok()
                    .and_then(|mut rd| rd.next())
                    .is_some());
        let entry = json!({
            "name": label,
            "path": path.to_string_lossy(),
            "found": found,
        });
        if !found {
            missing.push(entry.clone());
        }
        required.push(entry);
    }
    json!({"required": required, "missing": missing})
}

pub fn discover_profiles(root_dir: &Path, data_dir: &Path) -> BTreeMap<String, String> {
    let adapters = load_adapters(root_dir, Some(data_dir));
    let mut profiles: BTreeMap<String, String> = BTreeMap::new();
    for adapter in &adapters {
        if profiles.contains_key(&adapter.platform) {
            continue;
        }
        let prefix = detect_adapter_prefix(adapter);
        if prefix.is_empty() {
            continue;
        }
        let dummy = json!({"name": adapter.label});
        if let Ok(argv) = build_adapter_argv(adapter, &dummy, "{path}", Some(prefix), "") {
            profiles.insert(adapter.platform.clone(), join_command(&argv));
        }
    }
    if which("dosbox").is_some() {
        profiles
            .entry("DOSBox".to_owned())
            .or_insert_with(|| "dosbox {path}".to_owned());
    }
    if !IS_WINDOWS && which("wine").is_some() {
        profiles
            .entry("Windows".to_owned())
            .or_insert_with(|| "wine {path}".to_owned());
    }
    profiles
}
