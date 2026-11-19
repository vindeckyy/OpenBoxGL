//! Pure-Rust YAML emulator registry (all 44 `emulator_defs/*.yaml`),
//! launch resolution (5-level precedence), RetroArch core override, and ROM scan helpers.
//! Mirrors `pkg/parity/parity_emulator_defs.py`.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use openbox_platform::{
    IS_LINUX, IS_MACOS, IS_WINDOWS, expand_user, home_dir, join_command, libretro_core_filename,
    libretro_system_core_dir, resolve_native_executable, split_command, which,
};
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

use crate::settings::{apply_to_command, canonical_emulator_id, is_managed};
use crate::tokens::{apply_tokens, build_launch_args, path_parent, validate_startup_args};

pub const SCHEMA_VERSION: u32 = 2;
pub const RETROARCH_CORE_CHOICE_KEY: &str = "retroarch_core";
pub const RETROARCH_CORE_TOKEN: &str = "{retroarch_core}";
pub const RETROARCH_FLATPAK_APP_ID: &str = "org.libretro.RetroArch";
pub const AMBIGUOUS_DISC_EXTENSIONS: &[&str] = &[".bin", ".cue", ".iso"];

static CORE_FILE_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z0-9][A-Za-z0-9_.+-]*_libretro\.(?:so|dylib)$").unwrap());

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct StateConfig {
    pub kind: String,
    pub template: Vec<String>,
    pub capture: Vec<String>,
    pub glob: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Adapter {
    pub adapter_id: String,
    pub emulator_id: String,
    pub label: String,
    pub platform: String,
    pub extensions: Vec<String>,
    pub native_exe: Option<String>,
    pub native_exe_windows: Option<String>,
    pub native_exe_macos: Option<String>,
    pub flatpak_app_id: Option<String>,
    pub startup_args: Vec<String>,
    pub recommended: bool,
    pub priority: i32,
    pub executable_patterns: Vec<String>,
    pub schema_version: u32,
    pub retroarch_core: String,
    pub bios_path: Option<String>,
    pub bios_sha1: Option<String>,
    pub firmware_path: Option<String>,
    pub firmware_sha1: Option<String>,
    pub core_path: Option<String>,
    pub state: StateConfig,
}

pub fn valid_core_name(name: &str) -> bool {
    CORE_FILE_RE.is_match(name)
}

fn core_choice(raw: Option<&str>) -> String {
    let text = raw.unwrap_or("").trim();
    if valid_core_name(text) {
        text.to_owned()
    } else {
        String::new()
    }
}

fn strip_yaml_comment(line: &str) -> String {
    let mut out = String::with_capacity(line.len());
    let mut quote: Option<char> = None;
    for ch in line.chars() {
        if let Some(q) = quote {
            out.push(ch);
            if ch == q {
                quote = None;
            }
        } else if ch == '"' || ch == '\'' {
            quote = Some(ch);
            out.push(ch);
        } else if ch == '#' {
            break;
        } else {
            out.push(ch);
        }
    }
    out
}

fn unquote_yaml(raw: &str) -> String {
    let trimmed = raw.trim();
    if trimmed.len() >= 2
        && ((trimmed.starts_with('"') && trimmed.ends_with('"'))
            || (trimmed.starts_with('\'') && trimmed.ends_with('\'')))
    {
        return trimmed[1..trimmed.len() - 1].to_owned();
    }
    trimmed.to_owned()
}

fn parse_inline_list(raw: &str) -> Option<Vec<Value>> {
    let trimmed = raw.trim();
    if !trimmed.starts_with('[') || !trimmed.ends_with(']') {
        return None;
    }
    let inner = trimmed[1..trimmed.len() - 1].trim();
    if inner.is_empty() {
        return Some(Vec::new());
    }
    let mut items = Vec::new();
    let mut cur = String::new();
    let mut quote: Option<char> = None;
    for ch in inner.chars() {
        if let Some(q) = quote {
            cur.push(ch);
            if ch == q {
                quote = None;
            }
        } else if ch == '"' || ch == '\'' {
            quote = Some(ch);
            cur.push(ch);
        } else if ch == ',' {
            let s = unquote_yaml(&cur);
            if !s.is_empty() {
                items.push(Value::String(s));
            }
            cur.clear();
        } else {
            cur.push(ch);
        }
    }
    let tail = unquote_yaml(&cur);
    if !tail.is_empty() {
        items.push(Value::String(tail));
    }
    Some(items)
}

fn parse_inline_map(raw: &str) -> Option<Map<String, Value>> {
    let trimmed = raw.trim();
    if !trimmed.starts_with('{') || !trimmed.ends_with('}') {
        return None;
    }
    let inner = trimmed[1..trimmed.len() - 1].trim();
    let mut out = Map::new();
    if inner.is_empty() {
        return Some(out);
    }
    for part in inner.split(',') {
        if let Some((k, v)) = part.split_once(':') {
            out.insert(k.trim().to_owned(), parse_yaml_scalar(v));
        }
    }
    Some(out)
}

fn parse_yaml_scalar(raw: &str) -> Value {
    let trimmed = raw.trim();
    if trimmed.is_empty() || trimmed == "null" || trimmed == "~" {
        return Value::Null;
    }
    if trimmed == "true" {
        return Value::Bool(true);
    }
    if trimmed == "false" {
        return Value::Bool(false);
    }
    if let Some(list) = parse_inline_list(trimmed) {
        return Value::Array(list);
    }
    if let Some(map) = parse_inline_map(trimmed) {
        return Value::Object(map);
    }
    if !(trimmed.starts_with('"') || trimmed.starts_with('\''))
        && let Ok(n) = trimmed.parse::<i64>()
    {
        return Value::from(n);
    }
    Value::String(unquote_yaml(trimmed))
}

/// Pure-Rust YAML subset parser handling all OpenBox `emulator_defs/*.yaml` constructs
/// (top-level scalars, inline lists/maps, indented `- item` lists, and one-level nested maps
/// including lists inside nested map keys like `state.template` / `state.glob`).
pub fn parse_yaml_def(text: &str) -> Result<Map<String, Value>, String> {
    let mut data: Map<String, Value> = Map::new();
    let mut current_key: Option<String> = None;
    let mut current_subkey: Option<String> = None;

    for raw_line in text.lines() {
        let cleaned = strip_yaml_comment(raw_line);
        let trimmed = cleaned.trim();
        if trimmed.is_empty() {
            continue;
        }
        let indent = cleaned
            .chars()
            .take_while(|c| *c == ' ' || *c == '\t')
            .count();
        if indent == 0 {
            current_subkey = None;
            if let Some(k) = trimmed.strip_suffix(':') {
                let key = k.trim().to_owned();
                data.insert(key.clone(), Value::Null);
                current_key = Some(key);
            } else if let Some((k, v)) = trimmed.split_once(':') {
                let key = k.trim().to_owned();
                data.insert(key, parse_yaml_scalar(v));
                current_key = None;
            }
            continue;
        }
        let Some(ref top) = current_key else {
            continue;
        };
        if let Some(item_raw) = trimmed.strip_prefix("- ") {
            let item = parse_yaml_scalar(item_raw);
            if let Some(ref sub) = current_subkey {
                if let Some(Value::Object(map)) = data.get_mut(top) {
                    match map.get_mut(sub) {
                        Some(Value::Array(arr)) => arr.push(item),
                        _ => {
                            map.insert(sub.clone(), Value::Array(vec![item]));
                        }
                    }
                }
            } else {
                match data.get_mut(top) {
                    Some(Value::Array(arr)) => arr.push(item),
                    _ => {
                        data.insert(top.clone(), Value::Array(vec![item]));
                    }
                }
            }
        } else if let Some((sk, sv)) = trimmed.split_once(':') {
            let subkey = sk.trim().to_owned();
            if !data.get(top).is_some_and(Value::is_object) {
                data.insert(top.clone(), Value::Object(Map::new()));
            }
            if let Some(Value::Object(map)) = data.get_mut(top) {
                if sv.trim().is_empty() {
                    map.insert(subkey.clone(), Value::Null);
                    current_subkey = Some(subkey);
                } else {
                    map.insert(subkey, parse_yaml_scalar(sv));
                    current_subkey = None;
                }
            }
        }
    }
    Ok(data)
}

fn val_as_str_opt(v: Option<&Value>) -> Option<String> {
    match v {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) => {
            let t = s.trim();
            if t.is_empty() {
                None
            } else {
                Some(t.to_owned())
            }
        }
        Some(Value::Number(n)) => Some(n.to_string()),
        Some(Value::Bool(b)) => Some(b.to_string()),
        _ => None,
    }
}

fn val_as_bool(v: Option<&Value>, default: bool) -> bool {
    match v {
        Some(Value::Bool(b)) => *b,
        Some(Value::String(s)) => matches!(
            s.trim().to_ascii_lowercase().as_str(),
            "1" | "true" | "yes" | "on"
        ),
        Some(Value::Number(n)) => n.as_i64().unwrap_or(0) != 0,
        _ => default,
    }
}

fn val_as_i32(v: Option<&Value>, default: i32) -> i32 {
    match v {
        Some(Value::Number(n)) => n.as_i64().map(|x| x as i32).unwrap_or(default),
        Some(Value::String(s)) => s.trim().parse::<i32>().unwrap_or(default),
        _ => default,
    }
}

fn val_as_str_list(v: Option<&Value>) -> Vec<String> {
    match v {
        Some(Value::Array(arr)) => arr
            .iter()
            .filter_map(|x| match x {
                Value::String(s) => Some(s.clone()),
                Value::Number(n) => Some(n.to_string()),
                Value::Bool(b) => Some(b.to_string()),
                _ => None,
            })
            .collect(),
        Some(Value::String(s)) if !s.trim().is_empty() => vec![s.trim().to_owned()],
        _ => Vec::new(),
    }
}

fn split_state_args(v: Option<&Value>) -> Vec<String> {
    match v {
        None | Some(Value::Null) => Vec::new(),
        Some(Value::Array(_)) => val_as_str_list(v),
        Some(Value::String(s)) => {
            let t = s.trim();
            if t.is_empty() {
                Vec::new()
            } else {
                split_command(t)
                    .unwrap_or_else(|_| t.split_whitespace().map(str::to_owned).collect())
            }
        }
        _ => Vec::new(),
    }
}

fn normalize_state(raw: Option<&Value>, adapter_id: &str) -> Result<StateConfig, String> {
    let Some(val) = raw else {
        return Ok(StateConfig {
            kind: "none".to_owned(),
            ..Default::default()
        });
    };
    if val.is_null() || val.as_str() == Some("") {
        return Ok(StateConfig {
            kind: "none".to_owned(),
            ..Default::default()
        });
    }
    let Some(obj) = val.as_object() else {
        return Err(format!(
            "Adapter {adapter_id} state block must be a mapping."
        ));
    };
    let kind = obj
        .get("kind")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .unwrap_or("none")
        .to_owned();
    if !matches!(
        kind.as_str(),
        "retroarch" | "adapter-cli" | "config-override" | "none"
    ) {
        return Err(format!("Adapter {adapter_id} invalid state.kind: {kind}"));
    }
    let template = split_state_args(obj.get("template"));
    let capture = split_state_args(obj.get("capture"));
    let glob = match obj.get("glob") {
        Some(Value::String(s)) => s
            .replace(',', " ")
            .split_whitespace()
            .map(str::to_owned)
            .collect(),
        other => val_as_str_list(other),
    };
    if kind == "none" && (!template.is_empty() || !capture.is_empty()) {
        return Err(format!(
            "Adapter {adapter_id} kind 'none' must not declare state args."
        ));
    }
    let mut combined = template.clone();
    combined.extend(capture.iter().cloned());
    let invalid = validate_startup_args(&combined);
    if !invalid.is_empty() {
        return Err(format!(
            "Adapter {adapter_id} state args contain unknown tokens: {}",
            invalid.join(", ")
        ));
    }
    Ok(StateConfig {
        kind,
        template,
        capture,
        glob,
    })
}

pub fn normalize_adapter(raw: &Map<String, Value>) -> Result<Adapter, String> {
    let adapter_id = val_as_str_opt(raw.get("adapter_id").or_else(|| raw.get("id")))
        .ok_or_else(|| "Adapter requires adapter_id.".to_owned())?;
    let emulator_id = val_as_str_opt(raw.get("emulator_id").or_else(|| raw.get("flatpak")))
        .ok_or_else(|| "Adapter requires emulator_id.".to_owned())?;
    let platform = val_as_str_opt(raw.get("platform"))
        .ok_or_else(|| "Adapter requires platform.".to_owned())?;

    let extensions: Vec<String> = val_as_str_list(raw.get("extensions"))
        .into_iter()
        .map(|e| e.to_ascii_lowercase().trim_start_matches('.').to_owned())
        .collect();
    let native_exe = val_as_str_opt(raw.get("native_exe").or_else(|| raw.get("native")));
    let native_exe_windows = val_as_str_opt(
        raw.get("native_exe_windows")
            .or_else(|| raw.get("native_windows")),
    );
    let macos_declared = raw.contains_key("native_exe_macos") || raw.contains_key("native_macos");
    let native_exe_macos = if macos_declared {
        Some(
            val_as_str_opt(
                raw.get("native_exe_macos")
                    .or_else(|| raw.get("native_macos")),
            )
            .unwrap_or_default(),
        )
    } else {
        None
    };
    let flatpak_app_id = if IS_LINUX {
        val_as_str_opt(raw.get("flatpak_app_id").or_else(|| raw.get("flatpak")))
    } else {
        None
    };

    let mut startup_args = if raw.get("startup_args").is_some_and(Value::is_array) {
        val_as_str_list(raw.get("startup_args"))
    } else if let Some(s) = val_as_str_opt(raw.get("startup")) {
        split_command(&s).unwrap_or_else(|_| s.split_whitespace().map(str::to_owned).collect())
    } else {
        vec!["{path}".to_owned()]
    };
    if IS_WINDOWS && raw.get("startup_args_windows").is_some_and(Value::is_array) {
        let win_args = val_as_str_list(raw.get("startup_args_windows"));
        if !win_args.is_empty() {
            startup_args = win_args;
        }
    }
    let invalid = validate_startup_args(&startup_args);
    if !invalid.is_empty() {
        return Err(format!(
            "Adapter {adapter_id} startup_args contains unknown tokens: {}",
            invalid.join(", ")
        ));
    }
    let retroarch_core = core_choice(raw.get("retroarch_core").and_then(Value::as_str));
    if startup_args.iter().any(|a| a == RETROARCH_CORE_TOKEN) && retroarch_core.is_empty() {
        return Err(format!(
            "Adapter {adapter_id} uses {RETROARCH_CORE_TOKEN} without a valid retroarch_core."
        ));
    }
    let mut core_path = val_as_str_opt(raw.get("core_path"));
    if core_path.is_none() {
        for idx in 0..startup_args.len().saturating_sub(1) {
            if startup_args[idx] == "-L" && startup_args[idx + 1].starts_with('/') {
                core_path = Some(startup_args[idx + 1].clone());
                break;
            }
        }
    }

    Ok(Adapter {
        label: val_as_str_opt(raw.get("label").or_else(|| raw.get("name")))
            .unwrap_or_else(|| adapter_id.clone()),
        state: normalize_state(raw.get("state"), &adapter_id)?,
        adapter_id,
        emulator_id,
        platform,
        extensions,
        native_exe,
        native_exe_windows,
        native_exe_macos,
        flatpak_app_id,
        startup_args,
        recommended: val_as_bool(raw.get("recommended"), true),
        priority: val_as_i32(raw.get("priority"), 100),
        executable_patterns: val_as_str_list(raw.get("executable_patterns")),
        schema_version: val_as_i32(raw.get("schema_version"), SCHEMA_VERSION as i32) as u32,
        retroarch_core,
        bios_path: val_as_str_opt(raw.get("bios_path").or_else(|| raw.get("bios"))),
        bios_sha1: val_as_str_opt(raw.get("bios_sha1")).map(|s| s.to_ascii_lowercase()),
        firmware_path: val_as_str_opt(raw.get("firmware_path")),
        firmware_sha1: val_as_str_opt(raw.get("firmware_sha1")).map(|s| s.to_ascii_lowercase()),
        core_path,
    })
}

pub fn resolved_startup_args(adapter: &Adapter) -> Vec<String> {
    let sys_dir = libretro_system_core_dir();
    adapter
        .startup_args
        .iter()
        .map(|arg| {
            if arg == RETROARCH_CORE_TOKEN && !adapter.retroarch_core.is_empty() {
                format!(
                    "{sys_dir}{}",
                    libretro_core_filename(&adapter.retroarch_core)
                )
            } else {
                arg.clone()
            }
        })
        .collect()
}

pub fn adapter_executable(adapter: &Adapter) -> String {
    let native = adapter.native_exe.as_deref().unwrap_or("");
    if IS_WINDOWS
        && let Some(win) = adapter.native_exe_windows.as_deref()
        && !win.is_empty()
    {
        return win.to_owned();
    }
    if IS_MACOS && let Some(mac) = adapter.native_exe_macos.as_deref() {
        return mac.to_owned();
    }
    native.to_owned()
}

pub fn load_adapters_from_dirs(dirs: &[PathBuf]) -> Vec<Adapter> {
    let mut adapters = Vec::new();
    let mut seen_files = HashSet::new();
    let mut claimed_ids = HashSet::new();

    for folder in dirs {
        if !folder.is_dir() {
            continue;
        }
        let Ok(rd) = fs::read_dir(folder) else {
            continue;
        };
        let mut yaml_paths: Vec<PathBuf> = rd
            .flatten()
            .map(|e| e.path())
            .filter(|p| p.extension().and_then(|e| e.to_str()) == Some("yaml"))
            .collect();
        yaml_paths.sort();
        for path in yaml_paths {
            let Some(fname) = path.file_name().map(|n| n.to_string_lossy().into_owned()) else {
                continue;
            };
            if seen_files.contains(&fname) {
                continue;
            }
            let Ok(text) = fs::read_to_string(&path) else {
                continue;
            };
            let Ok(parsed) = parse_yaml_def(&text) else {
                continue;
            };
            let Ok(adapter) = normalize_adapter(&parsed) else {
                continue;
            };
            seen_files.insert(fname);
            if claimed_ids.insert(adapter.adapter_id.clone()) {
                adapters.push(adapter);
            }
        }
    }
    adapters
}

pub fn definition_search_dirs(root_dir: &Path, data_dir: Option<&Path>) -> Vec<PathBuf> {
    let mut dirs = Vec::new();
    if let Some(dd) = data_dir {
        dirs.push(dd.join("emulator_defs"));
    }
    dirs.push(root_dir.join("emulator_defs"));
    dirs
}

pub fn load_adapters(root_dir: &Path, data_dir: Option<&Path>) -> Vec<Adapter> {
    load_adapters_from_dirs(&definition_search_dirs(root_dir, data_dir))
}

pub fn load_definitions(root_dir: &Path, data_dir: Option<&Path>) -> Vec<Value> {
    load_adapters(root_dir, data_dir)
        .into_iter()
        .map(|a| {
            let startup_args = resolved_startup_args(&a);
            json!({
                "id": a.adapter_id,
                "name": a.label,
                "extensions": a.extensions,
                "platforms": [a.platform],
                "startup": join_command(&startup_args),
                "startup_args": startup_args,
                "executable_patterns": a.executable_patterns,
                "flatpak": a.flatpak_app_id.as_deref().unwrap_or(""),
                "native": adapter_executable(&a),
                "emulator_id": a.emulator_id,
                "emulator_def": a.adapter_id,
                "state": a.state,
            })
        })
        .collect()
}

pub fn load_registry(root_dir: &Path, data_dir: Option<&Path>, health: bool) -> Value {
    let adapters = load_adapters(root_dir, data_dir);
    let schema_version = adapters
        .first()
        .map(|a| a.schema_version)
        .unwrap_or(SCHEMA_VERSION);
    let out: Vec<Value> = adapters
        .into_iter()
        .map(|a| {
            let startup_args = resolved_startup_args(&a);
            let mut obj = json!({
                "adapter_id": a.adapter_id,
                "emulator_id": a.emulator_id,
                "label": a.label,
                "platform": a.platform,
                "extensions": a.extensions,
                "native_exe": a.native_exe,
                "flatpak_app_id": a.flatpak_app_id,
                "startup_args": startup_args,
                "recommended": a.recommended,
                "priority": a.priority,
                "state": a.state,
            });
            if health && let Some(map) = obj.as_object_mut() {
                let bios_ok = a
                    .bios_path
                    .as_deref()
                    .map(|p| expand_user(Path::new(p)).exists())
                    .unwrap_or(true);
                let fw_ok = a
                    .firmware_path
                    .as_deref()
                    .map(|p| expand_user(Path::new(p)).exists())
                    .unwrap_or(true);
                let core_ok = a
                    .core_path
                    .as_deref()
                    .map(|p| Path::new(p).is_file())
                    .unwrap_or(true);
                map.insert("bios_ok".into(), Value::Bool(bios_ok));
                map.insert("firmware_ok".into(), Value::Bool(fw_ok));
                map.insert("core_ok".into(), Value::Bool(core_ok));
                if let Some(p) = &a.bios_path {
                    map.insert("bios_path".into(), Value::String(p.clone()));
                    map.insert(
                        "bios_sha1".into(),
                        a.bios_sha1
                            .clone()
                            .map(Value::String)
                            .unwrap_or(Value::Null),
                    );
                }
                if let Some(p) = &a.firmware_path {
                    map.insert("firmware_path".into(), Value::String(p.clone()));
                    map.insert(
                        "firmware_sha1".into(),
                        a.firmware_sha1
                            .clone()
                            .map(Value::String)
                            .unwrap_or(Value::Null),
                    );
                }
                if let Some(p) = &a.core_path {
                    map.insert("core_path".into(), Value::String(p.clone()));
                }
            }
            obj
        })
        .collect();
    json!({
        "schema_version": schema_version,
        "adapters": out,
    })
}

pub fn build_emulators_map(adapters: &[Adapter]) -> BTreeMap<String, Value> {
    let mut map: BTreeMap<String, Value> = BTreeMap::new();
    for a in adapters {
        let name = a.label.split(" (").next().unwrap_or(&a.label).to_owned();
        let exe = adapter_executable(a);
        let entry = map.entry(a.emulator_id.clone()).or_insert_with(|| {
            json!({
                "name": name,
                "native": exe,
                "profiles": {},
            })
        });
        if entry["native"].as_str().unwrap_or("").is_empty() && !exe.is_empty() {
            entry["native"] = Value::String(exe);
        }
        if let Some(profiles) = entry["profiles"].as_object_mut() {
            profiles.insert(
                a.platform.clone(),
                Value::String(join_command(&resolved_startup_args(a))),
            );
        }
    }
    map
}

pub fn build_platform_emulators(adapters: &[Adapter]) -> BTreeMap<String, Vec<(String, String)>> {
    let mut out: BTreeMap<String, Vec<(String, String)>> = BTreeMap::new();
    for a in adapters {
        let list = out.entry(a.platform.clone()).or_default();
        if !list.iter().any(|(id, _)| id == &a.emulator_id) {
            let name = a.label.split(" (").next().unwrap_or(&a.label).to_owned();
            list.push((a.emulator_id.clone(), name));
        }
    }
    out
}

pub fn build_platform_by_extension(adapters: &[Adapter]) -> HashMap<String, String> {
    let mut out = HashMap::new();
    for a in adapters {
        for ext in &a.extensions {
            out.entry(format!(".{ext}"))
                .or_insert_with(|| a.platform.clone());
        }
    }
    out
}

pub fn find_adapter<'a>(
    adapters: &'a [Adapter],
    adapter_id: &str,
    emulator_id: &str,
) -> Option<&'a Adapter> {
    let aid = adapter_id.trim();
    if !aid.is_empty()
        && let Some(found) = adapters.iter().find(|a| a.adapter_id == aid)
    {
        return Some(found);
    }
    let eid = emulator_id.trim();
    if !eid.is_empty() {
        let matches: Vec<&Adapter> = adapters.iter().filter(|a| a.emulator_id == eid).collect();
        if matches.len() == 1 {
            return Some(matches[0]);
        }
    }
    None
}

pub fn detect_adapter_prefix(adapter: &Adapter) -> Vec<String> {
    if IS_MACOS && adapter.native_exe_macos.as_deref() == Some("") {
        return Vec::new();
    }
    let native = adapter_executable(adapter);
    if !native.is_empty()
        && let Some(resolved) = resolve_native_executable(&native)
    {
        return vec![resolved];
    }
    if IS_LINUX
        && let Some(fp) = adapter.flatpak_app_id.as_deref()
        && !fp.is_empty()
        && which("flatpak").is_some()
    {
        return vec!["flatpak".to_owned(), "run".to_owned(), fp.to_owned()];
    }
    let mut patterns = adapter.executable_patterns.clone();
    if IS_WINDOWS
        && let Some(win) = &adapter.native_exe_windows
        && !win.is_empty()
    {
        patterns.insert(0, win.clone());
    }
    for pat in patterns {
        if let Some(found) = which(&pat) {
            return vec![found.to_string_lossy().into_owned()];
        }
    }
    Vec::new()
}

pub fn detect_adapter_for_platform<'a>(
    adapters: &'a [Adapter],
    platform: &str,
) -> Option<&'a Adapter> {
    let mut cands: Vec<&Adapter> = adapters.iter().filter(|a| a.platform == platform).collect();
    cands.sort_by(|a, b| {
        (!a.recommended)
            .cmp(&!b.recommended)
            .then_with(|| a.priority.cmp(&b.priority))
            .then_with(|| a.adapter_id.cmp(&b.adapter_id))
    });
    cands
        .into_iter()
        .find(|a| !detect_adapter_prefix(a).is_empty())
}

pub fn retroarch_flatpak_core_dir(home: Option<&Path>) -> PathBuf {
    let base = home
        .map(Path::to_path_buf)
        .or_else(home_dir)
        .unwrap_or_else(|| PathBuf::from("."));
    base.join(".var")
        .join("app")
        .join(RETROARCH_FLATPAK_APP_ID)
        .join("config")
        .join("retroarch")
        .join("cores")
}

pub fn flatpak_core_path(core: &str, prefix: &[String]) -> String {
    let sys_dir = libretro_system_core_dir();
    if prefix.first().map(String::as_str) == Some("flatpak")
        && core.starts_with(&sys_dir)
        && let Some(name) = Path::new(core).file_name()
    {
        return retroarch_flatpak_core_dir(None)
            .join(name)
            .to_string_lossy()
            .into_owned();
    }
    core.to_owned()
}

pub fn apply_core_override(adapter: &Adapter, game: &Value) -> Adapter {
    let choice = game
        .get(RETROARCH_CORE_CHOICE_KEY)
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("");
    if !valid_core_name(choice) {
        return adapter.clone();
    }
    let mut out = adapter.clone();
    if out.startup_args.iter().any(|a| a == RETROARCH_CORE_TOKEN) {
        out.retroarch_core = choice.to_owned();
        return out;
    }
    let sys_dir = libretro_system_core_dir();
    for idx in 0..out.startup_args.len().saturating_sub(1) {
        if out.startup_args[idx] != "-L" {
            continue;
        }
        let cur = out.startup_args[idx + 1].clone();
        if cur.starts_with(&sys_dir) {
            out.startup_args[idx + 1] = format!("{sys_dir}{}", libretro_core_filename(choice));
        } else if cur.ends_with(".dll") && cur.contains("\\cores\\") {
            let dir = cur.rsplit_once('\\').map(|(d, _)| d).unwrap_or("");
            let stem = Path::new(choice)
                .file_stem()
                .and_then(|s| s.to_str())
                .unwrap_or(choice);
            out.startup_args[idx + 1] = format!("{dir}\\{stem}.dll");
        }
    }
    out
}

pub fn installed_retroarch_cores(home: Option<&Path>) -> Vec<Value> {
    let mut found: BTreeMap<String, &'static str> = BTreeMap::new();
    let folders = [
        ("system", PathBuf::from(libretro_system_core_dir())),
        ("flatpak", retroarch_flatpak_core_dir(home)),
    ];
    for (loc, folder) in folders {
        let Ok(rd) = fs::read_dir(&folder) else {
            continue;
        };
        for ent in rd.flatten() {
            let name = ent.file_name().to_string_lossy().into_owned();
            if valid_core_name(&name) {
                found.entry(name).or_insert(loc);
            }
        }
    }
    found
        .into_iter()
        .map(|(name, location)| json!({"name": name, "location": location}))
        .collect()
}

fn is_mame_rom_argument(adapter: &Adapter, args: &[String]) -> bool {
    if args.len() < 2 {
        return false;
    }
    let identity =
        format!("{} {} {}", adapter.adapter_id, adapter.emulator_id, args[0]).to_ascii_lowercase();
    if !identity.contains("mame") {
        return false;
    }
    matches!(
        Path::new(args.last().map(String::as_str).unwrap_or(""))
            .extension()
            .and_then(|e| e.to_str())
            .map(str::to_ascii_lowercase)
            .as_deref(),
        Some("zip" | "7z")
    )
}

pub fn build_adapter_argv(
    adapter: &Adapter,
    game: &Value,
    rom_path: &str,
    prefix_override: Option<Vec<String>>,
    data_dir: &str,
) -> Result<Vec<String>, String> {
    let adapter = apply_core_override(adapter, game);
    let prefix = prefix_override.unwrap_or_else(|| detect_adapter_prefix(&adapter));
    if prefix.is_empty() {
        return Err(format!("No emulator found for {}.", adapter.label));
    }
    let mut args = prefix.clone();
    let emu_dir = path_parent(&prefix[0]);
    let startup = resolved_startup_args(&adapter);
    for (idx, val) in startup.iter().enumerate() {
        let mut text = apply_tokens(val, game, Some(rom_path), data_dir, &emu_dir);
        if idx > 0 && startup[idx - 1] == "-L" {
            text = flatpak_core_path(&text, &prefix);
        }
        args.push(text);
    }
    if is_mame_rom_argument(&adapter, &args)
        && let Some(last) = args.pop()
    {
        let rom = Path::new(&last);
        args.push("-rompath".to_owned());
        args.push(
            rom.parent()
                .map(|p| p.to_string_lossy().into_owned())
                .unwrap_or_default(),
        );
        args.push(
            rom.file_stem()
                .map(|s| s.to_string_lossy().into_owned())
                .unwrap_or(last),
        );
    }
    Ok(args)
}

const TARGET_TOKENS: &[&str] = &[
    "{path}",
    "{imagepath}",
    "{app_id}",
    "{lutris_id}",
    "{heroic_app_id}",
];

fn command_includes_target(command: &str) -> bool {
    let folded = command.to_ascii_lowercase();
    TARGET_TOKENS.iter().any(|tok| folded.contains(tok))
}

#[derive(Debug, Clone)]
pub struct LaunchPlan {
    pub args: Vec<String>,
    pub cwd: String,
    pub precedence: &'static str,
    pub warnings: Vec<String>,
}

pub fn resolve_launch(
    game: &Value,
    profiles: &Map<String, Value>,
    adapters: &[Adapter],
    data_dir: &Path,
    state: Option<&Value>,
) -> Result<LaunchPlan, String> {
    let path = game
        .get("path")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    if path.is_empty() {
        let name = game
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or("This game");
        return Err(format!("{name} has no launch path."));
    }
    if !Path::new(&path).exists() {
        return Err(format!("The configured path no longer exists:\n{path}"));
    }
    let data_dir_str = data_dir.to_string_lossy().into_owned();
    let game_command = game
        .get("launch")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("");
    let platform = game.get("platform").and_then(Value::as_str).unwrap_or("");
    let mut precedence = "direct_exe";
    let mut args: Option<Vec<String>> = None;

    if !game_command.is_empty() {
        let mut a = build_launch_args(game_command, game, Some(&path), &data_dir_str, "");
        if !command_includes_target(game_command) {
            a.push(path.clone());
        }
        args = Some(a);
        precedence = "game_launch";
    } else {
        let aid = game
            .get("emulator_adapter_id")
            .and_then(Value::as_str)
            .unwrap_or("");
        let eid = game
            .get("emulator_id")
            .and_then(Value::as_str)
            .unwrap_or("");
        if let Some(adapter) = find_adapter(adapters, aid, eid)
            && let Ok(a) = build_adapter_argv(adapter, game, &path, None, &data_dir_str)
        {
            args = Some(a);
            precedence = "game_adapter";
        }
        if args.is_none()
            && let Some(prof_cmd) = profiles
                .get(platform)
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|s| !s.is_empty())
        {
            let mut a = build_launch_args(prof_cmd, game, Some(&path), &data_dir_str, "");
            if !command_includes_target(prof_cmd) {
                a.push(path.clone());
            }
            args = Some(a);
            precedence = "platform_profile";
        }
        if args.is_none()
            && let Some(detected) = detect_adapter_for_platform(adapters, platform)
            && let Ok(a) = build_adapter_argv(detected, game, &path, None, &data_dir_str)
        {
            args = Some(a);
            precedence = "registry_adapter";
        }
    }

    let mut final_args = match args {
        Some(a) => a,
        None => {
            precedence = "direct_exe";
            if Path::new(&path)
                .extension()
                .and_then(|e| e.to_str())
                .map(str::to_ascii_lowercase)
                .as_deref()
                == Some("sh")
            {
                let shell = if which("bash").is_some() {
                    "bash"
                } else if which("sh").is_some() {
                    "sh"
                } else if IS_WINDOWS {
                    return Err("Shell scripts require bash or sh on PATH.".to_owned());
                } else {
                    "bash"
                };
                vec![shell.to_owned(), path.clone()]
            } else {
                vec![path.clone()]
            }
        }
    };

    let mut warnings = Vec::new();
    if let Some(st) = state {
        let canonical = launch_emulator_id(game, &final_args);
        if !canonical.is_empty() && is_managed(st, &canonical) {
            let (new_args, meta_warnings) =
                apply_to_command(&canonical, platform, &final_args, st, data_dir);
            final_args = new_args;
            warnings.extend(meta_warnings);
        }
    }

    let cwd = Path::new(&path)
        .parent()
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_default();
    Ok(LaunchPlan {
        args: final_args,
        cwd,
        precedence,
        warnings,
    })
}

fn launch_emulator_id(game: &Value, args: &[String]) -> String {
    for key in ["emulator_id", "emulator_adapter_id"] {
        if let Some(val) = game.get(key).and_then(Value::as_str)
            && let Ok(c) = canonical_emulator_id(val)
        {
            return c.to_owned();
        }
    }
    let exe = Path::new(args.first().map(String::as_str).unwrap_or(""))
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    if exe.starts_with("mame") {
        return "mame".to_owned();
    }
    if exe.contains("retroarch") {
        return "retroarch".to_owned();
    }
    String::new()
}

pub fn scan_folder(
    folder: &Path,
    adapters: &[Adapter],
    emulator_id: Option<&str>,
    recursive: bool,
) -> Result<Vec<Value>, String> {
    let dir = expand_user(folder);
    if !dir.is_dir() {
        return Err("Scan folder does not exist.".to_owned());
    }
    let eid = emulator_id.unwrap_or("").trim();
    let filtered: Vec<&Adapter> = if eid.is_empty() {
        adapters.iter().collect()
    } else {
        adapters
            .iter()
            .filter(|a| a.emulator_id == eid || a.adapter_id == eid)
            .collect()
    };
    let mut ext_map: HashMap<String, &Adapter> = HashMap::new();
    for a in filtered {
        for ext in &a.extensions {
            ext_map.entry(ext.clone()).or_insert(a);
        }
    }
    let mut games = Vec::new();
    collect_folder_games(&dir, recursive, &ext_map, &mut games);
    Ok(games)
}

fn collect_folder_games(
    dir: &Path,
    recursive: bool,
    ext_map: &HashMap<String, &Adapter>,
    out: &mut Vec<Value>,
) {
    let Ok(rd) = fs::read_dir(dir) else {
        return;
    };
    for ent in rd.flatten() {
        let path = ent.path();
        if path.is_dir() {
            if recursive {
                collect_folder_games(&path, true, ext_map, out);
            }
            continue;
        }
        if !path.is_file() {
            continue;
        }
        let Some(ext) = path
            .extension()
            .and_then(|e| e.to_str())
            .map(str::to_ascii_lowercase)
        else {
            continue;
        };
        let Some(adapter) = ext_map.get(&ext) else {
            continue;
        };
        let name = path
            .file_stem()
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or_default();
        let rom_name = path
            .file_name()
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or_default();
        out.push(json!({
            "name": name,
            "platform": adapter.platform,
            "path": path.to_string_lossy(),
            "rom_name": rom_name,
            "source": adapter.label,
            "emulator_def": adapter.adapter_id,
            "emulator_id": adapter.emulator_id,
        }));
    }
}

pub fn merge_profiles_from_definitions(
    existing: &Map<String, Value>,
    adapters: &[Adapter],
) -> Map<String, Value> {
    let mut out = existing.clone();
    for a in adapters {
        let prefix = detect_adapter_prefix(a);
        if prefix.is_empty() {
            continue;
        }
        if let Ok(cmd) =
            build_adapter_argv(a, &json!({"name": a.label}), "{path}", Some(prefix), "")
        {
            out.entry(a.platform.clone())
                .or_insert_with(|| Value::String(join_command(&cmd)));
        }
    }
    out
}

pub fn list_scan_configs(state: &Value) -> Vec<Value> {
    state
        .get("settings")
        .and_then(|s| s.get("emulator_scan_configs"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
}

pub fn save_scan_config(
    state: &mut Value,
    folder: &str,
    emulator_id: &str,
    auto_update: bool,
) -> Result<Value, String> {
    let f = folder.trim();
    let e = emulator_id.trim();
    if f.is_empty() || e.is_empty() {
        return Err("Folder and emulator id are required.".to_owned());
    }
    let mut configs = list_scan_configs(state);
    configs.retain(|item| item.get("folder").and_then(Value::as_str) != Some(f));
    let entry = json!({
        "folder": f,
        "emulator_id": e,
        "auto_update": auto_update,
    });
    configs.push(entry.clone());
    if let Some(root) = state.as_object_mut() {
        let settings = root
            .entry("settings")
            .or_insert_with(|| Value::Object(Map::new()));
        if let Some(s) = settings.as_object_mut() {
            s.insert("emulator_scan_configs".into(), Value::Array(configs));
        }
    }
    Ok(entry)
}

pub fn platform_options_for_extension(extension: &str, adapters: &[Adapter]) -> Vec<String> {
    let clean = extension.trim().to_ascii_lowercase();
    let bare = clean.trim_start_matches('.');
    let dotted = format!(".{bare}");
    if !AMBIGUOUS_DISC_EXTENSIONS.contains(&dotted.as_str()) {
        return Vec::new();
    }
    let mut plats: Vec<String> = adapters
        .iter()
        .filter(|a| a.extensions.iter().any(|e| e == bare) && !a.platform.is_empty())
        .map(|a| a.platform.clone())
        .collect();
    plats.sort();
    plats.dedup();
    if plats.len() > 1 { plats } else { Vec::new() }
}
