//! Local OpenBox Plugin API v2 packages, trust, permissions, settings, data, credentials,
//! bubblewrap sandbox command builder, and JSON Lines runner (`plugins.py`).

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::LazyLock;
use std::thread;
use std::time::{Duration, Instant};

use jiff::Timestamp;
use openbox_core::{APP_VERSION as VERSION, ApiError};
use openbox_io::{ArchiveLimits, atomic_write_text, safe_zip_extract, set_mode};
use openbox_platform::{is_executable, resolved_data_dir, which};
use openbox_runtime::media_probe::{MEDIA_PATH_FIELDS, MEDIA_TYPES_ALL};
use openbox_runtime::notifications::{NotificationSpec, add_notification};
use regex::Regex;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use url::Url;

use crate::services::execute_service_requests;

pub const CAPABILITY_HOOKS: &[(&str, &str)] = &[
    ("storefront", "storefront"),
    ("metadata", "metadata"),
    ("media", "media"),
    ("stats", "stats"),
    ("emulator_settings", "emulator"),
    ("discovery", "discovery"),
];

pub const CAPABILITY_HOOK_NAMES: &[&str] = &[
    "storefront",
    "metadata",
    "media",
    "stats",
    "emulator",
    "discovery",
];

pub const PRIVILEGED_HOOK_NAMES: &[&str] = CAPABILITY_HOOK_NAMES;

pub const HOOKS: &[&str] = &[
    "before_launch",
    "after_session",
    "library",
    "command",
    "library_source",
    "events",
    "storefront",
    "metadata",
    "media",
    "stats",
    "emulator",
    "discovery",
];

pub const PERMISSIONS: &[&str] = &["network"];
pub const RESERVED_PLUGIN_IDS: &[&str] = &["data", "credentials", "plugins-state.json"];

pub const PLUGIN_API_VERSIONS: &[i64] = &[2];
pub const PLUGIN_API_VERSION: i64 = 2;
pub const PLUGIN_API_VERSION_SUPPORTED: i64 = 2;
pub const PLUGIN_API_RETIRED: &str = "PLUGIN_API_RETIRED";
pub const PLUGIN_API_RETIRED_MESSAGE: &str =
    "Plugin api_version is required and must be 2. OpenBox 3.0 removed the v1 runner.";

pub const SUBSCRIPTIONS: &[&str] = &[
    "library.changed",
    "library.progress",
    "session.started",
    "session.ended",
    "game.launched",
];

pub const MAX_PLUGIN_PAYLOAD: usize = 2 * 1024 * 1024;
pub const MAX_PLUGIN_OUTPUT_V2: usize = 4 * 1024 * 1024;
pub const MAX_COMMANDS: usize = 32;
pub const MAX_LIBRARY_ENTRIES: usize = 500;
pub const MAX_PLUGIN_DATA: usize = 1024 * 1024;
pub const MAX_PLUGIN_DATA_KEY: usize = 128;
pub const MAX_PLUGIN_DATA_DEPTH: usize = 4;
pub const MAX_CREDENTIAL_KEYS: usize = 128;
pub const MAX_CREDENTIAL_VALUE: usize = 4096;
pub const MAX_CAPABILITY_NAME: usize = 80;
pub const MAX_CAPABILITY_PLATFORM: usize = 40;
pub const MAX_CAPABILITY_MEDIA_KINDS: usize = 20;
pub const MAX_EMULATOR_CAPABILITIES: usize = 8;
pub const MAX_MESSAGE_KEYS: usize = 64;
pub const MAX_MESSAGE_TEXT: usize = 500;
pub const MESSAGE_LOCALES: &[&str] = &["en", "es", "de", "fr", "pt"];
pub const NOTIFICATION_LEVELS: &[&str] = &["info", "success", "warning", "error"];
pub const UNSANDBOXED_PLUGINS_ENV: &str = "OPENBOX_ALLOW_UNSANDBOXED_PLUGINS";
pub const SANDBOX_MASKED_PREFIXES: &[&str] = &["/home", "/tmp", "/run", "/mnt", "/media"];
pub const SETTING_TYPES: &[&str] = &["string", "number", "integer", "boolean"];
pub const EVENT_TRACKED_FIELDS: &[&str] = &[
    "name",
    "platform",
    "progress",
    "favorite",
    "hidden",
    "rating",
    "playtime_seconds",
];
pub const MAX_EVENT_IDS: usize = 500;
pub const MAX_EVENT_UPDATES: usize = 100;

static PLUGIN_ID_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[a-z0-9][a-z0-9._-]{1,63}$").unwrap());
static COMMAND_ID_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[a-z0-9][a-z0-9._-]{0,63}$").unwrap());
static MESSAGE_KEY_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[a-z0-9._-]{1,64}$").unwrap());
static MIN_OPENBOX_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^\d+(\.\d+){0,2}([-+][0-9A-Za-z.-]+)?$").unwrap());
static DIGITS_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\d+").unwrap());

#[derive(Debug, Clone, thiserror::Error)]
pub enum PluginError {
    #[error("{0}")]
    Retired(String),
    #[error("{0}")]
    Validation(String),
    #[error("{0}")]
    Io(String),
}

impl PluginError {
    pub fn code(&self) -> Option<&'static str> {
        match self {
            Self::Retired(_) => Some(PLUGIN_API_RETIRED),
            Self::Validation(_) | Self::Io(_) => None,
        }
    }

    pub fn into_api_error(self) -> ApiError {
        match self {
            Self::Retired(msg) => ApiError::new(400, PLUGIN_API_RETIRED, msg),
            Self::Validation(msg) => ApiError::bad_request(msg),
            Self::Io(msg) => ApiError::bad_request(msg),
        }
    }
}

pub fn is_valid_plugin_id(id: &str) -> bool {
    PLUGIN_ID_RE.is_match(id)
}

pub fn capability_media_kinds() -> HashSet<&'static str> {
    let mut set: HashSet<&'static str> = HashSet::new();
    for k in MEDIA_PATH_FIELDS {
        set.insert(*k);
    }
    for k in MEDIA_TYPES_ALL {
        set.insert(*k);
    }
    set.insert("screenshots");
    set
}

pub fn hook_timeout_secs(hook: &str, override_secs: Option<u64>) -> u64 {
    if let Some(secs) = override_secs {
        return secs.max(1);
    }
    match hook {
        "storefront" => 30,
        "metadata" | "media" | "stats" => 15,
        "emulator" | "discovery" => 10,
        _ => 5,
    }
}

pub fn needs_data_dir_mask(data_posix: &str) -> bool {
    !SANDBOX_MASKED_PREFIXES
        .iter()
        .any(|prefix| data_posix == *prefix || data_posix.starts_with(&format!("{prefix}/")))
}

fn sandbox_available() -> bool {
    let Some(bwrap) = which("bwrap") else {
        return false;
    };
    let Ok(mut child) = Command::new(bwrap)
        .args([
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--ro-bind",
            "/",
            "/",
            "/usr/bin/true",
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
    else {
        return false;
    };
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return status.success(),
            Ok(None) => {
                if Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    return false;
                }
                thread::sleep(Duration::from_millis(20));
            }
            Err(_) => return false,
        }
    }
}

pub fn sandbox_status() -> &'static str {
    if std::env::var(UNSANDBOXED_PLUGINS_ENV).as_deref() == Ok("1") {
        return "disabled";
    }
    if which("bwrap").is_none() {
        return "unavailable";
    }
    if sandbox_available() {
        "ready"
    } else {
        "unavailable"
    }
}

pub fn locate_runner_v2(app_root: Option<&Path>) -> Option<PathBuf> {
    if let Some(root) = app_root {
        let p = root.join("plugin_runner_v2.py");
        if p.is_file() {
            return Some(p);
        }
    }
    if let Ok(env_root) = std::env::var("OPENBOX_APP_ROOT") {
        let p = PathBuf::from(env_root).join("plugin_runner_v2.py");
        if p.is_file() {
            return Some(p);
        }
    }
    if let Ok(cwd) = std::env::current_dir() {
        let p = cwd.join("plugin_runner_v2.py");
        if p.is_file() {
            return Some(p);
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        let mut cur = exe.parent().map(Path::to_path_buf);
        for _ in 0..5 {
            if let Some(dir) = cur {
                let p = dir.join("plugin_runner_v2.py");
                if p.is_file() {
                    return Some(p);
                }
                cur = dir.parent().map(Path::to_path_buf);
            } else {
                break;
            }
        }
    }
    None
}

fn require_api_version(api_version: i64) -> Result<(), PluginError> {
    if api_version == 2 {
        return Ok(());
    }
    if api_version == 1 {
        return Err(PluginError::Retired(PLUGIN_API_RETIRED_MESSAGE.to_owned()));
    }
    Err(PluginError::Validation(format!(
        "Plugin requires API v{api_version}; this build supports v2."
    )))
}

fn path_to_posix(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}

fn is_python_entry(entry: &Path) -> bool {
    entry
        .extension()
        .and_then(|e| e.to_str())
        .is_some_and(|ext| ext.eq_ignore_ascii_case("py"))
}

pub fn sandboxed_command(
    package_root: &Path,
    entry: &Path,
    share_net: bool,
    api_version: i64,
    data_dir_override: Option<&Path>,
    runner_override: Option<&Path>,
) -> Result<Option<Vec<String>>, PluginError> {
    require_api_version(api_version)?;
    let Some(bwrap) = which("bwrap") else {
        return Ok(None);
    };
    let canonical_pkg =
        fs::canonicalize(package_root).unwrap_or_else(|_| package_root.to_path_buf());
    let canonical_entry = fs::canonicalize(entry).unwrap_or_else(|_| entry.to_path_buf());
    let relative_entry = canonical_entry.strip_prefix(&canonical_pkg).map_err(|_| {
        PluginError::Validation("Plugin entry must stay inside package.".to_owned())
    })?;
    let is_py = is_python_entry(&canonical_entry);

    let data_dir = match data_dir_override {
        Some(p) => fs::canonicalize(p).unwrap_or_else(|_| p.to_path_buf()),
        None => {
            let d = resolved_data_dir();
            fs::canonicalize(&d).unwrap_or(d)
        }
    };
    let data_posix = path_to_posix(&data_dir);

    let mut command = vec![
        bwrap.to_string_lossy().into_owned(),
        "--die-with-parent".to_owned(),
        "--new-session".to_owned(),
        "--unshare-all".to_owned(),
    ];
    if share_net {
        command.push("--share-net".to_owned());
    }
    command.extend([
        "--ro-bind".to_owned(),
        "/".to_owned(),
        "/".to_owned(),
        "--tmpfs".to_owned(),
        "/home".to_owned(),
        "--tmpfs".to_owned(),
        "/tmp".to_owned(),
        "--tmpfs".to_owned(),
        "/run".to_owned(),
        "--tmpfs".to_owned(),
        "/mnt".to_owned(),
        "--tmpfs".to_owned(),
        "/media".to_owned(),
        "--proc".to_owned(),
        "/proc".to_owned(),
        "--dev".to_owned(),
        "/dev".to_owned(),
    ]);
    if needs_data_dir_mask(&data_posix) {
        command.push("--tmpfs".to_owned());
        command.push(data_posix);
    }

    command.extend([
        "--tmpfs".to_owned(),
        "/opt".to_owned(),
        "--dir".to_owned(),
        "/opt/openbox".to_owned(),
        "--dir".to_owned(),
        "/opt/openbox/plugin".to_owned(),
        "--dir".to_owned(),
        "/opt/openbox/runner".to_owned(),
        "--ro-bind".to_owned(),
        canonical_pkg.to_string_lossy().into_owned(),
        "/opt/openbox/plugin".to_owned(),
    ]);

    let container_entry = format!("/opt/openbox/plugin/{}", path_to_posix(relative_entry));
    if is_py {
        let runner_path = runner_override
            .map(Path::to_path_buf)
            .or_else(|| locate_runner_v2(None))
            .ok_or_else(|| {
                PluginError::Validation("plugin_runner_v2.py could not be found.".to_owned())
            })?;
        let runner_abs = fs::canonicalize(&runner_path).unwrap_or(runner_path);
        let runner_parent = runner_abs
            .parent()
            .unwrap_or_else(|| Path::new("/"))
            .to_path_buf();
        command.push("--ro-bind".to_owned());
        command.push(runner_parent.to_string_lossy().into_owned());
        command.push("/opt/openbox/runner".to_owned());

        if let Ok(data_rel) = data_dir.strip_prefix(&runner_parent) {
            let masked = Path::new("/opt/openbox/runner").join(data_rel);
            command.push("--tmpfs".to_owned());
            command.push(path_to_posix(&masked));
            if runner_abs.strip_prefix(&data_dir).is_ok() {
                command.push("--ro-bind".to_owned());
                command.push(runner_abs.to_string_lossy().into_owned());
                command.push("/opt/openbox/runner/plugin_runner_v2.py".to_owned());
            }
        }
        let python = which("python3")
            .or_else(|| which("python"))
            .ok_or_else(|| {
                PluginError::Validation(
                    "Python 3 is required on PATH to run .py plugins.".to_owned(),
                )
            })?;
        command.extend([
            "--chdir".to_owned(),
            "/tmp".to_owned(),
            python.to_string_lossy().into_owned(),
            "-B".to_owned(),
            "/opt/openbox/runner/plugin_runner_v2.py".to_owned(),
            container_entry,
        ]);
    } else {
        command.extend(["--chdir".to_owned(), "/tmp".to_owned(), container_entry]);
    }
    Ok(Some(command))
}

fn unsandboxed_command(
    entry: &Path,
    runner_override: Option<&Path>,
) -> Result<Vec<String>, PluginError> {
    if is_python_entry(entry) {
        let runner = runner_override
            .map(Path::to_path_buf)
            .or_else(|| locate_runner_v2(None))
            .ok_or_else(|| {
                PluginError::Validation("plugin_runner_v2.py could not be found.".to_owned())
            })?;
        let python = which("python3")
            .or_else(|| which("python"))
            .ok_or_else(|| {
                PluginError::Validation(
                    "Python 3 is required on PATH to run .py plugins.".to_owned(),
                )
            })?;
        Ok(vec![
            python.to_string_lossy().into_owned(),
            "-B".to_owned(),
            runner.to_string_lossy().into_owned(),
            entry.to_string_lossy().into_owned(),
        ])
    } else {
        Ok(vec![entry.to_string_lossy().into_owned()])
    }
}

pub fn plugin_command(
    package_root: &Path,
    entry: &Path,
    trusted: bool,
    share_net: bool,
    api_version: i64,
    data_dir_override: Option<&Path>,
    runner_override: Option<&Path>,
) -> Result<Option<Vec<String>>, PluginError> {
    require_api_version(api_version)?;
    if std::env::var(UNSANDBOXED_PLUGINS_ENV).as_deref() == Ok("1") {
        tracing::warn!(
            "Running plugin {:?} without an OS sandbox because {}=1",
            package_root.file_name().unwrap_or_default(),
            UNSANDBOXED_PLUGINS_ENV
        );
        return Ok(Some(unsandboxed_command(entry, runner_override)?));
    }
    if let Some(cmd) = sandboxed_command(
        package_root,
        entry,
        share_net,
        api_version,
        data_dir_override,
        runner_override,
    )? && sandbox_available()
    {
        return Ok(Some(cmd));
    }
    if trusted {
        tracing::warn!(
            "Running plugin {:?} without an OS sandbox: user-trusted package",
            package_root.file_name().unwrap_or_default()
        );
        return Ok(Some(unsandboxed_command(entry, runner_override)?));
    }
    tracing::warn!(
        "Skipping plugin {:?} because bubblewrap is unavailable; trust it in the Plugins manager to run it unsandboxed",
        package_root.file_name().unwrap_or_default()
    );
    Ok(None)
}

pub fn state_file(directory: &Path) -> PathBuf {
    directory.join("plugins-state.json")
}

pub fn load_plugin_state(directory: &Path) -> Map<String, Value> {
    let path = state_file(directory);
    let Ok(raw) = fs::read_to_string(path) else {
        return Map::new();
    };
    match serde_json::from_str::<Value>(&raw) {
        Ok(Value::Object(map)) => map,
        _ => Map::new(),
    }
}

pub fn save_plugin_state(directory: &Path, state: &Map<String, Value>) -> Result<(), PluginError> {
    let path = state_file(directory);
    let text = serde_json::to_string_pretty(&Value::Object(state.clone()))
        .map_err(|e| PluginError::Io(e.to_string()))?
        + "\n";
    atomic_write_text(&path, &text, 0o600)
        .map(|_| ())
        .map_err(|e| PluginError::Io(e.to_string()))
}

// ---------------------------------------------------------------------------
// Manifest validation helpers
// ---------------------------------------------------------------------------

fn clean_commands(raw: Option<&Value>, plugin_id: &str) -> Result<Vec<Value>, PluginError> {
    let Some(val) = raw else {
        return Ok(Vec::new());
    };
    if val.is_null() {
        return Ok(Vec::new());
    }
    let Some(arr) = val.as_array() else {
        return Err(PluginError::Validation(
            "Plugin commands must be a bounded list.".to_owned(),
        ));
    };
    if arr.len() > MAX_COMMANDS {
        return Err(PluginError::Validation(
            "Plugin commands must be a bounded list.".to_owned(),
        ));
    }
    let mut commands = Vec::with_capacity(arr.len());
    let mut seen: HashSet<String> = HashSet::new();
    for item in arr {
        let Some(obj) = item.as_object() else {
            return Err(PluginError::Validation(
                "Plugin commands must be objects with id and label.".to_owned(),
            ));
        };
        let command_id = obj
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_owned();
        let label = obj
            .get("label")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_owned();
        if !COMMAND_ID_RE.is_match(&command_id) || label.is_empty() {
            return Err(PluginError::Validation(
                "Plugin commands need a valid id and a label.".to_owned(),
            ));
        }
        if !seen.insert(command_id.clone()) {
            return Err(PluginError::Validation(
                "Plugin command ids must be unique.".to_owned(),
            ));
        }
        let desc = obj
            .get("description")
            .and_then(Value::as_str)
            .unwrap_or("")
            .chars()
            .take(280)
            .collect::<String>();
        commands.push(json!({
            "id": command_id,
            "label": label.chars().take(80).collect::<String>(),
            "description": desc,
            "plugin_id": plugin_id,
            "source": "manifest",
        }));
    }
    Ok(commands)
}

fn clean_permissions(raw: Option<&Value>) -> Result<Vec<String>, PluginError> {
    let Some(val) = raw else {
        return Ok(Vec::new());
    };
    if val.is_null() {
        return Ok(Vec::new());
    }
    let Some(arr) = val.as_array() else {
        return Err(PluginError::Validation(
            "Plugin permissions must be a list of strings.".to_owned(),
        ));
    };
    let mut unknown = BTreeSet::new();
    let mut out = BTreeSet::new();
    for item in arr {
        let Some(s) = item.as_str() else {
            return Err(PluginError::Validation(
                "Plugin permissions must be a list of strings.".to_owned(),
            ));
        };
        if PERMISSIONS.contains(&s) {
            out.insert(s.to_owned());
        } else {
            unknown.insert(s.to_owned());
        }
    }
    if !unknown.is_empty() {
        let joined = unknown.into_iter().collect::<Vec<_>>().join(", ");
        return Err(PluginError::Validation(format!(
            "Plugin requests unknown permissions: {joined}."
        )));
    }
    Ok(out.into_iter().collect())
}

fn clean_settings_schema(raw: Option<&Value>) -> Result<Option<Value>, PluginError> {
    let Some(val) = raw else {
        return Ok(None);
    };
    if val.is_null() {
        return Ok(None);
    }
    let Some(obj) = val.as_object() else {
        return Err(PluginError::Validation(
            "Plugin settings must be a JSON Schema object.".to_owned(),
        ));
    };
    let schema_type = obj.get("type").and_then(Value::as_str).unwrap_or("object");
    if schema_type != "object" {
        return Err(PluginError::Validation(
            "Plugin settings schema must be an object schema.".to_owned(),
        ));
    }
    let properties = match obj.get("properties") {
        None => Map::new(),
        Some(Value::Object(m)) => m.clone(),
        Some(_) => {
            return Err(PluginError::Validation(
                "Plugin settings schema properties must be an object.".to_owned(),
            ));
        }
    };
    for (name, field) in &properties {
        let Some(fobj) = field.as_object() else {
            return Err(PluginError::Validation(format!(
                "Plugin setting '{name}' must be an object."
            )));
        };
        let ftype = fobj.get("type").and_then(Value::as_str).unwrap_or("string");
        if !SETTING_TYPES.contains(&ftype) {
            return Err(PluginError::Validation(format!(
                "Plugin setting '{name}' has unsupported type '{ftype}'."
            )));
        }
        if let Some(ev) = fobj.get("enum") {
            let valid = ev.as_array().is_some_and(|a| !a.is_empty());
            if !valid {
                return Err(PluginError::Validation(format!(
                    "Plugin setting '{name}' enum must be a non-empty list."
                )));
            }
        }
    }
    let required = match obj.get("required") {
        None => Vec::new(),
        Some(Value::Array(arr)) => {
            let mut reqs = Vec::with_capacity(arr.len());
            for item in arr {
                let Some(s) = item.as_str() else {
                    return Err(PluginError::Validation(
                        "Plugin settings schema has an invalid required list.".to_owned(),
                    ));
                };
                if !properties.contains_key(s) {
                    return Err(PluginError::Validation(
                        "Plugin settings schema has an invalid required list.".to_owned(),
                    ));
                }
                reqs.push(Value::String(s.to_owned()));
            }
            reqs
        }
        Some(_) => {
            return Err(PluginError::Validation(
                "Plugin settings schema has an invalid required list.".to_owned(),
            ));
        }
    };
    Ok(Some(json!({
        "type": "object",
        "properties": Value::Object(properties),
        "required": required,
    })))
}

fn clean_messages(raw: Option<&Value>) -> Result<Map<String, Value>, PluginError> {
    let Some(val) = raw else {
        return Ok(Map::new());
    };
    if val.is_null() {
        return Ok(Map::new());
    }
    let Some(obj) = val.as_object() else {
        return Err(PluginError::Validation(format!(
            "Plugin messages must be an object with at most {MAX_MESSAGE_KEYS} keys."
        )));
    };
    if obj.len() > MAX_MESSAGE_KEYS {
        return Err(PluginError::Validation(format!(
            "Plugin messages must be an object with at most {MAX_MESSAGE_KEYS} keys."
        )));
    }
    let mut out = Map::new();
    for (key, trans) in obj {
        if !MESSAGE_KEY_RE.is_match(key) {
            return Err(PluginError::Validation(format!(
                "Plugin message key '{key}' is invalid."
            )));
        }
        let Some(tobj) = trans.as_object() else {
            return Err(PluginError::Validation(format!(
                "Plugin message '{key}' must map locales to text."
            )));
        };
        let mut clean = Map::new();
        for (locale, text) in tobj {
            if !MESSAGE_LOCALES.contains(&locale.as_str()) {
                continue;
            }
            let Some(s) = text.as_str() else {
                return Err(PluginError::Validation(format!(
                    "Plugin message '{key}' has a non-text '{locale}' translation."
                )));
            };
            clean.insert(
                locale.clone(),
                Value::String(s.chars().take(MAX_MESSAGE_TEXT).collect()),
            );
        }
        out.insert(key.clone(), Value::Object(clean));
    }
    Ok(out)
}

fn clean_optional_text(
    raw: Option<&Value>,
    label: &str,
    maximum: usize,
) -> Result<String, PluginError> {
    let Some(val) = raw else {
        return Ok(String::new());
    };
    if val.is_null() {
        return Ok(String::new());
    }
    let Some(s) = val.as_str() else {
        return Err(PluginError::Validation(format!(
            "Plugin {label} must be text."
        )));
    };
    Ok(s.trim().chars().take(maximum).collect())
}

fn clean_homepage(raw: Option<&Value>) -> Result<String, PluginError> {
    let Some(val) = raw else {
        return Ok(String::new());
    };
    if val.is_null() {
        return Ok(String::new());
    }
    let Some(s) = val.as_str() else {
        return Err(PluginError::Validation(
            "Plugin homepage must be a URL.".to_owned(),
        ));
    };
    let trimmed = s.trim();
    if trimmed.is_empty() {
        return Ok(String::new());
    }
    let parsed = Url::parse(trimmed).map_err(|_| {
        PluginError::Validation(
            "Plugin homepage must be an HTTPS URL of at most 300 characters.".to_owned(),
        )
    })?;
    if trimmed.len() > 300 || parsed.scheme() != "https" || parsed.host_str().is_none() {
        return Err(PluginError::Validation(
            "Plugin homepage must be an HTTPS URL of at most 300 characters.".to_owned(),
        ));
    }
    Ok(trimmed.to_owned())
}

fn clean_media_kinds(raw: Option<&Value>, name: &str) -> Result<Vec<String>, PluginError> {
    let Some(val) = raw else {
        return Ok(vec!["cover".to_owned(), "background".to_owned()]);
    };
    if val.is_null() {
        return Ok(vec!["cover".to_owned(), "background".to_owned()]);
    }
    let Some(arr) = val.as_array() else {
        return Err(PluginError::Validation(format!(
            "Plugin capability {name} media_kinds must be a bounded list."
        )));
    };
    if arr.len() > MAX_CAPABILITY_MEDIA_KINDS {
        return Err(PluginError::Validation(format!(
            "Plugin capability {name} media_kinds must be a bounded list."
        )));
    }
    let allowed = capability_media_kinds();
    let mut kinds = Vec::new();
    for k in arr {
        let Some(s) = k.as_str() else {
            return Err(PluginError::Validation(format!(
                "Plugin capability {name} declares an unknown media kind."
            )));
        };
        if !allowed.contains(s) {
            return Err(PluginError::Validation(format!(
                "Plugin capability {name} declares an unknown media kind."
            )));
        }
        if !kinds.iter().any(|existing| existing == s) {
            kinds.push(s.to_owned());
        }
    }
    Ok(kinds)
}

fn clean_emulator_capabilities(raw: &Value) -> Result<Vec<Value>, PluginError> {
    let Some(arr) = raw.as_array() else {
        return Err(PluginError::Validation(
            "Plugin emulator_settings must be a list of at most 8 emulators.".to_owned(),
        ));
    };
    if arr.len() > MAX_EMULATOR_CAPABILITIES {
        return Err(PluginError::Validation(
            "Plugin emulator_settings must be a list of at most 8 emulators.".to_owned(),
        ));
    }
    let mut out = Vec::new();
    let mut seen = HashSet::new();
    for entry in arr {
        let Some(obj) = entry.as_object() else {
            return Err(PluginError::Validation(
                "Plugin emulator_settings entries must be objects.".to_owned(),
            ));
        };
        let Some(emu_id) = obj.get("emulator_id").and_then(Value::as_str) else {
            return Err(PluginError::Validation(
                "Plugin emulator_settings needs a valid emulator_id.".to_owned(),
            ));
        };
        if !COMMAND_ID_RE.is_match(emu_id) {
            return Err(PluginError::Validation(
                "Plugin emulator_settings needs a valid emulator_id.".to_owned(),
            ));
        }
        if !seen.insert(emu_id.to_owned()) {
            return Err(PluginError::Validation(
                "Plugin emulator_settings ids must be unique.".to_owned(),
            ));
        }
        let label = match obj.get("label") {
            None | Some(Value::Null) => emu_id.to_owned(),
            Some(Value::String(s)) if s.is_empty() => emu_id.to_owned(),
            Some(Value::String(s)) => s.trim().chars().take(MAX_CAPABILITY_NAME).collect(),
            _ => {
                return Err(PluginError::Validation(
                    "Plugin emulator_settings label must be text.".to_owned(),
                ));
            }
        };
        let Some(schema) = clean_settings_schema(obj.get("schema"))? else {
            return Err(PluginError::Validation(
                "Plugin emulator_settings requires a settings schema.".to_owned(),
            ));
        };
        let scopes_raw = obj
            .get("scopes")
            .cloned()
            .unwrap_or_else(|| json!(["global", "platform"]));
        let Some(sarr) = scopes_raw.as_array() else {
            return Err(PluginError::Validation(
                "Plugin emulator_settings scopes must be global and/or platform.".to_owned(),
            ));
        };
        let mut scope_set = HashSet::new();
        for s in sarr {
            let Some(ss) = s.as_str() else {
                return Err(PluginError::Validation(
                    "Plugin emulator_settings scopes must be global and/or platform.".to_owned(),
                ));
            };
            if ss != "global" && ss != "platform" {
                return Err(PluginError::Validation(
                    "Plugin emulator_settings scopes must be global and/or platform.".to_owned(),
                ));
            }
            scope_set.insert(ss);
        }
        let mut scopes = Vec::new();
        for s in ["global", "platform"] {
            if scope_set.contains(s) {
                scopes.push(s);
            }
        }
        if scopes.is_empty() {
            return Err(PluginError::Validation(
                "Plugin emulator_settings needs at least one scope.".to_owned(),
            ));
        }
        out.push(json!({
            "emulator_id": emu_id,
            "label": label,
            "schema": schema,
            "scopes": scopes,
        }));
    }
    Ok(out)
}

fn clean_capabilities(
    raw: Option<&Value>,
    plugin_id: &str,
    hooks: &[String],
) -> Result<Map<String, Value>, PluginError> {
    let Some(val) = raw else {
        return Ok(Map::new());
    };
    if val.is_null() {
        return Ok(Map::new());
    }
    let Some(obj) = val.as_object() else {
        return Err(PluginError::Validation(
            "Plugin capabilities must be an object.".to_owned(),
        ));
    };
    if obj.is_empty() {
        return Ok(Map::new());
    }
    let hook_set: HashSet<&str> = hooks.iter().map(String::as_str).collect();
    let mut out = Map::new();
    let mut seen_ids = HashSet::new();
    for (name, req_hook) in CAPABILITY_HOOKS {
        let Some(value) = obj.get(*name) else {
            continue;
        };
        if !hook_set.contains(*req_hook) {
            return Err(PluginError::Validation(format!(
                "Plugin capability {name} requires the {req_hook} hook."
            )));
        }
        if *name == "emulator_settings" {
            out.insert(
                (*name).to_owned(),
                Value::Array(clean_emulator_capabilities(value)?),
            );
            continue;
        }
        let Some(cobj) = value.as_object() else {
            return Err(PluginError::Validation(format!(
                "Plugin capability {name} must be an object."
            )));
        };
        let Some(cap_id) = cobj.get("id").and_then(Value::as_str) else {
            return Err(PluginError::Validation(format!(
                "Plugin capability {name} needs a valid id."
            )));
        };
        if !PLUGIN_ID_RE.is_match(cap_id) {
            return Err(PluginError::Validation(format!(
                "Plugin capability {name} needs a valid id."
            )));
        }
        let Some(cap_name) = cobj.get("name").and_then(Value::as_str) else {
            return Err(PluginError::Validation(format!(
                "Plugin capability {name} needs a name."
            )));
        };
        let cap_name_trimmed = cap_name.trim();
        if cap_name_trimmed.is_empty() {
            return Err(PluginError::Validation(format!(
                "Plugin capability {name} needs a name."
            )));
        }
        if !seen_ids.insert(cap_id.to_owned()) {
            return Err(PluginError::Validation(format!(
                "Plugin capability id '{cap_id}' is duplicated in {plugin_id}."
            )));
        }
        let mut cap = Map::new();
        cap.insert("id".to_owned(), Value::String(cap_id.to_owned()));
        cap.insert(
            "name".to_owned(),
            Value::String(cap_name_trimmed.chars().take(MAX_CAPABILITY_NAME).collect()),
        );
        if *name == "storefront" || *name == "metadata" {
            let platform = match cobj.get("platform") {
                None | Some(Value::Null) => String::new(),
                Some(Value::String(s)) => s
                    .trim()
                    .chars()
                    .take(MAX_CAPABILITY_PLATFORM)
                    .collect::<String>(),
                Some(_) => {
                    return Err(PluginError::Validation(format!(
                        "Plugin {name} platform must be text."
                    )));
                }
            };
            cap.insert("platform".to_owned(), Value::String(platform));
        } else if *name == "media" {
            let kinds = clean_media_kinds(cobj.get("media_kinds"), name)?;
            cap.insert(
                "media_kinds".to_owned(),
                Value::Array(kinds.into_iter().map(Value::String).collect()),
            );
        }
        out.insert((*name).to_owned(), Value::Object(cap));
    }
    Ok(out)
}

fn version_tuple(value: &str) -> Vec<u64> {
    let parts: Vec<u64> = DIGITS_RE
        .find_iter(value)
        .take(3)
        .filter_map(|m| m.as_str().parse::<u64>().ok())
        .collect();
    if parts.is_empty() { vec![0] } else { parts }
}

fn clean_min_openbox(raw: Option<&Value>, api_version: i64) -> Result<String, PluginError> {
    let Some(val) = raw else {
        return Ok(String::new());
    };
    if val.is_null() {
        return Ok(String::new());
    }
    let text = match val {
        Value::String(s) => s.trim().to_owned(),
        other => other.to_string(),
    };
    if text.is_empty() {
        return Ok(String::new());
    }
    if api_version < 2 {
        return Err(PluginError::Validation(
            "min_openbox needs plugin api_version 2.".to_owned(),
        ));
    }
    if !MIN_OPENBOX_RE.is_match(&text) {
        return Err(PluginError::Validation(
            "Plugin min_openbox must be a version number.".to_owned(),
        ));
    }
    if version_tuple(&text) > version_tuple(VERSION) {
        return Err(PluginError::Validation(format!(
            "Plugin needs OpenBox {text} or newer; this build is {VERSION}."
        )));
    }
    Ok(text)
}

fn clean_subscriptions(raw: Option<&Value>, api_version: i64) -> Result<Vec<String>, PluginError> {
    let Some(val) = raw else {
        return Ok(Vec::new());
    };
    if val.is_null() {
        return Ok(Vec::new());
    }
    let Some(arr) = val.as_array() else {
        return Err(PluginError::Validation(
            "Plugin subscriptions must be a list of at most 8 names.".to_owned(),
        ));
    };
    if arr.is_empty() {
        return Ok(Vec::new());
    }
    if api_version < 2 {
        return Err(PluginError::Validation(
            "subscriptions need plugin api_version 2.".to_owned(),
        ));
    }
    if arr.len() > 8 {
        return Err(PluginError::Validation(
            "Plugin subscriptions must be a list of at most 8 names.".to_owned(),
        ));
    }
    let mut names = Vec::new();
    for item in arr {
        let name = match item {
            Value::String(s) => s.trim().to_owned(),
            other => other.to_string(),
        };
        if !SUBSCRIPTIONS.contains(&name.as_str()) {
            return Err(PluginError::Validation(format!(
                "Plugin subscribes to an unknown event: {name}"
            )));
        }
        if !names.contains(&name) {
            names.push(name);
        }
    }
    Ok(names)
}

pub fn read_manifest(path: &Path) -> Result<Map<String, Value>, PluginError> {
    let raw = fs::read_to_string(path.join("plugin.json")).map_err(|_| {
        PluginError::Validation("Plugin package needs a valid plugin.json.".to_owned())
    })?;
    let parsed: Value = serde_json::from_str(&raw).map_err(|_| {
        PluginError::Validation("Plugin package needs a valid plugin.json.".to_owned())
    })?;
    let Some(mut manifest) = parsed.as_object().cloned() else {
        return Err(PluginError::Validation(
            "Plugin package needs a valid plugin.json.".to_owned(),
        ));
    };

    let plugin_id = manifest
        .get("id")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    let entry = manifest
        .get("entry")
        .and_then(Value::as_str)
        .unwrap_or("plugin.py")
        .to_owned();
    let name_ok = manifest
        .get("name")
        .and_then(Value::as_str)
        .is_some_and(|s| !s.is_empty());
    let version_ok = manifest
        .get("version")
        .and_then(Value::as_str)
        .is_some_and(|s| !s.is_empty());
    if !PLUGIN_ID_RE.is_match(&plugin_id)
        || RESERVED_PLUGIN_IDS.contains(&plugin_id.as_str())
        || !name_ok
        || !version_ok
    {
        return Err(PluginError::Validation(
            "Plugin id, name, and version are required.".to_owned(),
        ));
    }

    let hooks_raw = manifest
        .get("hooks")
        .cloned()
        .unwrap_or_else(|| Value::Array(Vec::new()));
    let Some(hooks_arr) = hooks_raw.as_array() else {
        return Err(PluginError::Validation(
            "Plugin declares an unsupported hook.".to_owned(),
        ));
    };
    let mut hooks = Vec::with_capacity(hooks_arr.len());
    for h in hooks_arr {
        let Some(hs) = h.as_str() else {
            return Err(PluginError::Validation(
                "Plugin declares an unsupported hook.".to_owned(),
            ));
        };
        if !HOOKS.contains(&hs) {
            return Err(PluginError::Validation(
                "Plugin declares an unsupported hook.".to_owned(),
            ));
        }
        hooks.push(hs.to_owned());
    }

    let Some(api_val) = manifest.get("api_version") else {
        return Err(PluginError::Retired(PLUGIN_API_RETIRED_MESSAGE.to_owned()));
    };
    let Some(api_version) = api_val.as_i64() else {
        return Err(PluginError::Validation(
            "Plugin api_version must be a positive integer.".to_owned(),
        ));
    };
    if api_version < 1 {
        return Err(PluginError::Validation(
            "Plugin api_version must be a positive integer.".to_owned(),
        ));
    }
    if api_version == 1 {
        return Err(PluginError::Retired(PLUGIN_API_RETIRED_MESSAGE.to_owned()));
    }
    if !PLUGIN_API_VERSIONS.contains(&api_version) {
        return Err(PluginError::Validation(format!(
            "Plugin requires API v{api_version}; this build supports v2."
        )));
    }

    let canonical_pkg = fs::canonicalize(path).map_err(|_| {
        PluginError::Validation("Plugin entry must be a Python file inside the package.".to_owned())
    })?;
    let entry_path = fs::canonicalize(path.join(&entry)).map_err(|_| {
        PluginError::Validation("Plugin entry must be a Python file inside the package.".to_owned())
    })?;
    let inside = entry_path.starts_with(&canonical_pkg) && entry_path != canonical_pkg;
    // Allow .py entrypoints (standard) OR native executable v2 entrypoints (D2 additive support).
    let valid_entry = inside
        && entry_path.is_file()
        && (is_python_entry(&entry_path) || is_executable(&entry_path));
    if !valid_entry {
        return Err(PluginError::Validation(
            "Plugin entry must be a Python file inside the package.".to_owned(),
        ));
    }

    let commands = clean_commands(manifest.get("commands"), &plugin_id)?;
    let permissions = clean_permissions(manifest.get("permissions"))?;
    let settings = clean_settings_schema(manifest.get("settings"))?;
    let messages = clean_messages(manifest.get("messages"))?;
    let capabilities = clean_capabilities(manifest.get("capabilities"), &plugin_id, &hooks)?;
    let min_openbox = clean_min_openbox(manifest.get("min_openbox"), api_version)?;
    let subscriptions = clean_subscriptions(manifest.get("subscriptions"), api_version)?;
    let description = clean_optional_text(manifest.get("description"), "description", 500)?;
    let author = clean_optional_text(manifest.get("author"), "author", 80)?;
    let homepage = clean_homepage(manifest.get("homepage"))?;

    manifest.insert("id".to_owned(), Value::String(plugin_id));
    manifest.insert("entry".to_owned(), Value::String(entry));
    manifest.insert(
        "hooks".to_owned(),
        Value::Array(hooks.into_iter().map(Value::String).collect()),
    );
    manifest.insert("api_version".to_owned(), json!(api_version));
    manifest.insert("min_openbox".to_owned(), Value::String(min_openbox));
    manifest.insert(
        "subscriptions".to_owned(),
        Value::Array(subscriptions.into_iter().map(Value::String).collect()),
    );
    manifest.insert("commands".to_owned(), Value::Array(commands));
    manifest.insert(
        "permissions".to_owned(),
        Value::Array(permissions.into_iter().map(Value::String).collect()),
    );
    manifest.insert("settings".to_owned(), settings.unwrap_or(Value::Null));
    manifest.insert("capabilities".to_owned(), Value::Object(capabilities));
    manifest.insert("messages".to_owned(), Value::Object(messages));
    manifest.insert("description".to_owned(), Value::String(description));
    manifest.insert("author".to_owned(), Value::String(author));
    manifest.insert("homepage".to_owned(), Value::String(homepage));
    Ok(manifest)
}

// ---------------------------------------------------------------------------
// Settings validation
// ---------------------------------------------------------------------------

fn check_setting_value(
    name: &str,
    field: &Map<String, Value>,
    value: &Value,
) -> Result<Value, PluginError> {
    let ftype = field
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or("string");
    match ftype {
        "string" => {
            let Some(s) = value.as_str() else {
                return Err(PluginError::Validation(format!(
                    "Setting '{name}' must be a string."
                )));
            };
            if let Some(min_len) = field.get("minLength").and_then(Value::as_u64)
                && (s.chars().count() as u64) < min_len
            {
                return Err(PluginError::Validation(format!(
                    "Setting '{name}' is too short."
                )));
            }
            if let Some(max_len) = field.get("maxLength").and_then(Value::as_u64)
                && (s.chars().count() as u64) > max_len
            {
                return Err(PluginError::Validation(format!(
                    "Setting '{name}' is too long."
                )));
            }
            Ok(Value::String(s.to_owned()))
        }
        "boolean" => {
            let Some(b) = value.as_bool() else {
                return Err(PluginError::Validation(format!(
                    "Setting '{name}' must be true or false."
                )));
            };
            Ok(Value::Bool(b))
        }
        "integer" => {
            let Some(n) = value.as_i64() else {
                return Err(PluginError::Validation(format!(
                    "Setting '{name}' must be a whole number."
                )));
            };
            if let Some(min) = field.get("minimum").and_then(Value::as_f64)
                && (n as f64) < min
            {
                return Err(PluginError::Validation(format!(
                    "Setting '{name}' is below the minimum."
                )));
            }
            if let Some(max) = field.get("maximum").and_then(Value::as_f64)
                && (n as f64) > max
            {
                return Err(PluginError::Validation(format!(
                    "Setting '{name}' is above the maximum."
                )));
            }
            Ok(json!(n))
        }
        "number" => {
            let Some(n) = value.as_f64() else {
                return Err(PluginError::Validation(format!(
                    "Setting '{name}' must be a number."
                )));
            };
            if let Some(min) = field.get("minimum").and_then(Value::as_f64)
                && n < min
            {
                return Err(PluginError::Validation(format!(
                    "Setting '{name}' is below the minimum."
                )));
            }
            if let Some(max) = field.get("maximum").and_then(Value::as_f64)
                && n > max
            {
                return Err(PluginError::Validation(format!(
                    "Setting '{name}' is above the maximum."
                )));
            }
            Ok(value.clone())
        }
        _ => Err(PluginError::Validation(format!(
            "Setting '{name}' has unsupported type '{ftype}'."
        ))),
    }
}

pub fn validate_plugin_settings(
    schema: Option<&Value>,
    values: &Value,
) -> Result<Map<String, Value>, PluginError> {
    let empty_obj = Map::new();
    let schema_obj = schema.and_then(Value::as_object).unwrap_or(&empty_obj);
    let properties = schema_obj
        .get("properties")
        .and_then(Value::as_object)
        .unwrap_or(&empty_obj);
    let Some(val_obj) = values.as_object() else {
        return Err(PluginError::Validation(
            "Plugin settings must be an object.".to_owned(),
        ));
    };
    let mut unknown: Vec<String> = val_obj
        .keys()
        .filter(|k| !properties.contains_key(*k))
        .cloned()
        .collect();
    unknown.sort();
    if !unknown.is_empty() {
        return Err(PluginError::Validation(format!(
            "Unknown plugin settings: {}.",
            unknown.join(", ")
        )));
    }
    let required: HashSet<&str> = schema_obj
        .get("required")
        .and_then(Value::as_array)
        .map(|a| a.iter().filter_map(Value::as_str).collect())
        .unwrap_or_default();

    let mut cleaned = Map::new();
    for (name, field_val) in properties {
        let Some(field) = field_val.as_object() else {
            continue;
        };
        let value = if let Some(v) = val_obj.get(name) {
            v
        } else if let Some(def) = field.get("default") {
            def
        } else if required.contains(name.as_str()) {
            return Err(PluginError::Validation(format!(
                "Setting '{name}' is required."
            )));
        } else {
            continue;
        };
        if let Some(enum_arr) = field.get("enum").and_then(Value::as_array)
            && !enum_arr.contains(value)
        {
            let allowed = enum_arr
                .iter()
                .map(|v| match v {
                    Value::String(s) => s.clone(),
                    other => other.to_string(),
                })
                .collect::<Vec<_>>()
                .join(", ");
            return Err(PluginError::Validation(format!(
                "Setting '{name}' must be one of: {allowed}."
            )));
        }
        cleaned.insert(name.clone(), check_setting_value(name, field, value)?);
    }
    Ok(cleaned)
}

// ---------------------------------------------------------------------------
// Package checksums, trust, permissions, settings, data, credentials
// ---------------------------------------------------------------------------

fn walk_package_files(
    root: &Path,
    cur: &Path,
    out: &mut Vec<(String, PathBuf)>,
) -> Result<(), std::io::Error> {
    for entry in fs::read_dir(cur)? {
        let entry = entry?;
        let meta = fs::symlink_metadata(entry.path())?;
        if meta.file_type().is_symlink() {
            continue;
        }
        if meta.is_dir() {
            walk_package_files(root, &entry.path(), out)?;
        } else if meta.is_file()
            && let Ok(rel) = entry.path().strip_prefix(root)
        {
            out.push((path_to_posix(rel), entry.path()));
        }
    }
    Ok(())
}

fn package_files(root: &Path) -> Result<Vec<(String, PathBuf)>, std::io::Error> {
    let mut files = Vec::new();
    walk_package_files(root, root, &mut files)?;
    files.sort_by(|a, b| a.0.cmp(&b.0));
    Ok(files)
}

pub fn legacy_plugin_checksum(directory: &Path, plugin_id: &str) -> Result<String, std::io::Error> {
    let root = directory.join(plugin_id);
    let mut digest = Sha256::new();
    let mut buf = [0u8; 65536];
    for (rel, path) in package_files(&root)? {
        digest.update(rel.as_bytes());
        digest.update([0u8]);
        let mut f = File::open(path)?;
        loop {
            let n = f.read(&mut buf)?;
            if n == 0 {
                break;
            }
            digest.update(&buf[..n]);
        }
    }
    Ok(hex::encode(digest.finalize()))
}

pub fn plugin_checksum(directory: &Path, plugin_id: &str) -> Result<String, std::io::Error> {
    let root = directory.join(plugin_id);
    let mut digest = Sha256::new();
    let mut buf = [0u8; 65536];
    for (rel, path) in package_files(&root)? {
        let mut file_digest = Sha256::new();
        let mut f = File::open(path)?;
        loop {
            let n = f.read(&mut buf)?;
            if n == 0 {
                break;
            }
            file_digest.update(&buf[..n]);
        }
        digest.update(rel.as_bytes());
        digest.update([0u8]);
        digest.update(file_digest.finalize());
    }
    Ok(hex::encode(digest.finalize()))
}

pub fn plugin_trust_status(directory: &Path, plugin_id: &str) -> Value {
    let Ok(checksum) = plugin_checksum(directory, plugin_id) else {
        return json!({
            "trusted": false,
            "checksum": "",
            "checksum_matches": false,
            "granted_at": "",
            "retrust": false,
        });
    };
    let state = load_plugin_state(directory);
    let record = state
        .get("trust")
        .and_then(Value::as_object)
        .and_then(|t| t.get(plugin_id))
        .and_then(Value::as_object);
    let rec_trusted = record
        .and_then(|r| r.get("trusted"))
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let rec_checksum = record
        .and_then(|r| r.get("checksum"))
        .and_then(Value::as_str)
        .unwrap_or("");
    let granted_at = record
        .and_then(|r| r.get("granted_at"))
        .and_then(Value::as_str)
        .unwrap_or("");
    let checksum_matches = rec_checksum == checksum;
    let retrust = if rec_trusted && !checksum_matches {
        legacy_plugin_checksum(directory, plugin_id)
            .map(|leg| leg == rec_checksum)
            .unwrap_or(false)
    } else {
        false
    };
    json!({
        "trusted": rec_trusted && checksum_matches,
        "checksum": checksum,
        "checksum_matches": checksum_matches,
        "granted_at": granted_at,
        "retrust": retrust,
    })
}

pub fn set_plugin_trust(
    directory: &Path,
    plugin_id: &str,
    trusted: bool,
) -> Result<bool, PluginError> {
    let manifest = read_manifest(&directory.join(plugin_id))?;
    let id = manifest["id"].as_str().unwrap_or(plugin_id).to_owned();
    let mut state = load_plugin_state(directory);
    let trust = state
        .entry("trust")
        .or_insert_with(|| Value::Object(Map::new()));
    if !trust.is_object() {
        *trust = Value::Object(Map::new());
    }
    let map = trust.as_object_mut().unwrap();
    if trusted {
        let checksum =
            plugin_checksum(directory, &id).map_err(|e| PluginError::Io(e.to_string()))?;
        let now = Timestamp::now()
            .strftime("%Y-%m-%dT%H:%M:%S+00:00")
            .to_string();
        map.insert(
            id,
            json!({
                "trusted": true,
                "checksum": checksum,
                "granted_at": now,
            }),
        );
    } else {
        map.remove(&id);
    }
    save_plugin_state(directory, &state)?;
    Ok(trusted)
}

pub fn plugin_permission_grants(directory: &Path, plugin_id: &str) -> Vec<String> {
    let state = load_plugin_state(directory);
    let mut out = BTreeSet::new();
    if let Some(arr) = state
        .get("permissions")
        .and_then(Value::as_object)
        .and_then(|p| p.get(plugin_id))
        .and_then(Value::as_array)
    {
        for item in arr {
            if let Some(s) = item.as_str()
                && PERMISSIONS.contains(&s)
            {
                out.insert(s.to_owned());
            }
        }
    }
    out.into_iter().collect()
}

pub fn set_plugin_permissions(
    directory: &Path,
    plugin_id: &str,
    permissions: &[String],
) -> Result<Vec<String>, PluginError> {
    let manifest = read_manifest(&directory.join(plugin_id))?;
    let id = manifest["id"].as_str().unwrap_or(plugin_id).to_owned();
    let declared: HashSet<&str> = manifest
        .get("permissions")
        .and_then(Value::as_array)
        .map(|a| a.iter().filter_map(Value::as_str).collect())
        .unwrap_or_default();

    let mut wanted = BTreeSet::new();
    let mut unknown = BTreeSet::new();
    let mut undeclared = BTreeSet::new();
    for item in permissions {
        if !PERMISSIONS.contains(&item.as_str()) {
            unknown.insert(item.clone());
        } else if !declared.contains(item.as_str()) {
            undeclared.insert(item.clone());
        } else {
            wanted.insert(item.clone());
        }
    }
    if !unknown.is_empty() {
        return Err(PluginError::Validation(format!(
            "Unknown permissions: {}.",
            unknown.into_iter().collect::<Vec<_>>().join(", ")
        )));
    }
    if !undeclared.is_empty() {
        return Err(PluginError::Validation(format!(
            "Plugin does not declare permissions: {}.",
            undeclared.into_iter().collect::<Vec<_>>().join(", ")
        )));
    }

    let granted: Vec<String> = wanted.into_iter().collect();
    let mut state = load_plugin_state(directory);
    let perms = state
        .entry("permissions")
        .or_insert_with(|| Value::Object(Map::new()));
    if !perms.is_object() {
        *perms = Value::Object(Map::new());
    }
    perms.as_object_mut().unwrap().insert(
        id,
        Value::Array(granted.iter().cloned().map(Value::String).collect()),
    );
    save_plugin_state(directory, &state)?;
    Ok(granted)
}

pub fn secret_setting_names(manifest: &Map<String, Value>) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    if let Some(props) = manifest
        .get("settings")
        .and_then(Value::as_object)
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

pub fn plugin_settings_values(
    directory: &Path,
    manifest: &Map<String, Value>,
) -> Map<String, Value> {
    let schema = manifest.get("settings").filter(|v| !v.is_null());
    let state = load_plugin_state(directory);
    let id = manifest.get("id").and_then(Value::as_str).unwrap_or("");
    let stored = state
        .get("settings")
        .and_then(Value::as_object)
        .and_then(|s| s.get(id))
        .filter(|v| v.is_object())
        .cloned()
        .unwrap_or_else(|| Value::Object(Map::new()));
    validate_plugin_settings(schema, &stored)
        .or_else(|_| validate_plugin_settings(schema, &Value::Object(Map::new())))
        .unwrap_or_default()
}

pub fn plugin_settings_values_public(
    directory: &Path,
    manifest: &Map<String, Value>,
) -> Map<String, Value> {
    let secrets = secret_setting_names(manifest);
    plugin_settings_values(directory, manifest)
        .into_iter()
        .filter(|(k, _)| !secrets.contains(k))
        .collect()
}

pub fn get_plugin_settings(directory: &Path, plugin_id: &str) -> Result<Value, PluginError> {
    let manifest = read_manifest(&directory.join(plugin_id))?;
    let schema = manifest
        .get("settings")
        .filter(|v| v.is_object())
        .cloned()
        .unwrap_or_else(|| json!({"type": "object", "properties": {}}));
    let values = plugin_settings_values(directory, &manifest);
    Ok(json!({
        "schema": schema,
        "values": values,
    }))
}

pub fn get_plugin_settings_public(directory: &Path, plugin_id: &str) -> Result<Value, PluginError> {
    let manifest = read_manifest(&directory.join(plugin_id))?;
    let schema = manifest
        .get("settings")
        .filter(|v| v.is_object())
        .cloned()
        .unwrap_or_else(|| json!({"type": "object", "properties": {}}));
    let secrets = secret_setting_names(&manifest);
    let values = plugin_settings_values(directory, &manifest);
    let public_vals: Map<String, Value> = values
        .iter()
        .filter(|(k, _)| !secrets.contains(*k))
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    let mut secret_set = Map::new();
    for name in secrets {
        let truthy = values.get(&name).is_some_and(|v| match v {
            Value::Null => false,
            Value::Bool(b) => *b,
            Value::String(s) => !s.is_empty(),
            Value::Number(n) => n.as_f64().is_some_and(|x| x != 0.0),
            Value::Array(a) => !a.is_empty(),
            Value::Object(o) => !o.is_empty(),
        });
        secret_set.insert(name, Value::Bool(truthy));
    }
    Ok(json!({
        "schema": schema,
        "values": public_vals,
        "secret_set": secret_set,
    }))
}

pub fn set_plugin_settings(
    directory: &Path,
    plugin_id: &str,
    values: &Value,
) -> Result<Map<String, Value>, PluginError> {
    let manifest = read_manifest(&directory.join(plugin_id))?;
    let id = manifest["id"].as_str().unwrap_or(plugin_id).to_owned();
    let schema = manifest.get("settings").filter(|v| !v.is_null());
    let cleaned = validate_plugin_settings(schema, values)?;
    let mut state = load_plugin_state(directory);
    let settings = state
        .entry("settings")
        .or_insert_with(|| Value::Object(Map::new()));
    if !settings.is_object() {
        *settings = Value::Object(Map::new());
    }
    settings
        .as_object_mut()
        .unwrap()
        .insert(id, Value::Object(cleaned.clone()));
    save_plugin_state(directory, &state)?;
    Ok(cleaned)
}

fn plugin_store_path(
    directory: &Path,
    folder: &str,
    plugin_id: &str,
) -> Result<PathBuf, PluginError> {
    if !PLUGIN_ID_RE.is_match(plugin_id) {
        return Err(PluginError::Validation("Plugin id is invalid.".to_owned()));
    }
    Ok(directory.join(folder).join(format!("{plugin_id}.json")))
}

fn read_json_store(path: &Path, maximum: usize) -> Map<String, Value> {
    let Ok(meta) = fs::metadata(path) else {
        return Map::new();
    };
    if meta.len() as usize > maximum {
        return Map::new();
    }
    let Ok(raw) = fs::read_to_string(path) else {
        return Map::new();
    };
    match serde_json::from_str::<Value>(&raw) {
        Ok(Value::Object(m)) => m,
        _ => Map::new(),
    }
}

fn write_json_store(path: &Path, data: &Map<String, Value>) -> Result<(), PluginError> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| PluginError::Io(e.to_string()))?;
        let _ = set_mode(parent, 0o700);
    }
    let sorted: BTreeMap<&String, &Value> = data.iter().collect();
    let encoded =
        serde_json::to_string_pretty(&sorted).map_err(|e| PluginError::Io(e.to_string()))? + "\n";
    atomic_write_text(path, &encoded, 0o600)
        .map(|_| ())
        .map_err(|e| PluginError::Io(e.to_string()))
}

fn json_depth(value: &Value) -> Result<usize, PluginError> {
    match value {
        Value::Null | Value::Bool(_) | Value::String(_) => Ok(0),
        Value::Number(n) => {
            if n.as_f64().is_some_and(|x| !x.is_finite()) {
                return Err(PluginError::Validation(
                    "Plugin data must be finite JSON.".to_owned(),
                ));
            }
            Ok(0)
        }
        Value::Array(arr) => {
            let mut max_child = 0usize;
            for item in arr {
                max_child = max_child.max(json_depth(item)?);
            }
            Ok(1 + max_child)
        }
        Value::Object(map) => {
            let mut max_child = 0usize;
            for (k, v) in map {
                if k.is_empty() || k.len() > MAX_PLUGIN_DATA_KEY {
                    return Err(PluginError::Validation(
                        "Plugin data keys must be short strings.".to_owned(),
                    ));
                }
                max_child = max_child.max(json_depth(v)?);
            }
            Ok(1 + max_child)
        }
    }
}

pub fn plugin_data_values(directory: &Path, plugin_id: &str) -> Map<String, Value> {
    let Ok(path) = plugin_store_path(directory, "data", plugin_id) else {
        return Map::new();
    };
    let data = read_json_store(&path, MAX_PLUGIN_DATA);
    if !data.is_empty() {
        match json_depth(&Value::Object(data.clone())) {
            Ok(d) if d <= MAX_PLUGIN_DATA_DEPTH => {}
            _ => return Map::new(),
        }
    }
    data
}

pub fn set_plugin_data(
    directory: &Path,
    plugin_id: &str,
    patch: &Value,
) -> Result<Map<String, Value>, PluginError> {
    let Some(patch_obj) = patch.as_object() else {
        return Err(PluginError::Validation(
            "Plugin data must be an object.".to_owned(),
        ));
    };
    let mut cleaned = Map::new();
    for (k, v) in patch_obj {
        if k.is_empty() || k.len() > MAX_PLUGIN_DATA_KEY {
            return Err(PluginError::Validation(
                "Plugin data keys must be short strings.".to_owned(),
            ));
        }
        json_depth(v)?;
        cleaned.insert(k.clone(), v.clone());
    }
    let mut merged = plugin_data_values(directory, plugin_id);
    for (k, v) in cleaned {
        merged.insert(k, v);
    }
    if json_depth(&Value::Object(merged.clone()))? > MAX_PLUGIN_DATA_DEPTH {
        return Err(PluginError::Validation(
            "Plugin data is nested too deeply.".to_owned(),
        ));
    }
    let sorted: BTreeMap<&String, &Value> = merged.iter().collect();
    let encoded = serde_json::to_string(&sorted).map_err(|e| PluginError::Io(e.to_string()))?;
    if encoded.len() > MAX_PLUGIN_DATA {
        return Err(PluginError::Validation(
            "Plugin data exceeds the 1 MiB limit.".to_owned(),
        ));
    }
    let path = plugin_store_path(directory, "data", plugin_id)?;
    write_json_store(&path, &merged)?;
    Ok(merged)
}

pub fn plugin_credentials_values(directory: &Path, plugin_id: &str) -> BTreeMap<String, String> {
    let Ok(path) = plugin_store_path(directory, "credentials", plugin_id) else {
        return BTreeMap::new();
    };
    let raw = read_json_store(&path, MAX_PLUGIN_DATA);
    let mut out = BTreeMap::new();
    for (k, v) in raw {
        if k.is_empty() || k.len() > MAX_PLUGIN_DATA_KEY {
            continue;
        }
        if let Some(s) = v.as_str()
            && s.len() <= MAX_CREDENTIAL_VALUE
        {
            out.insert(k, s.to_owned());
        }
    }
    out
}

pub fn set_plugin_credentials(
    directory: &Path,
    plugin_id: &str,
    values: &Value,
    clear: &[String],
) -> Result<Vec<String>, PluginError> {
    let val_obj = match values {
        Value::Null => Map::new(),
        Value::Object(m) => m.clone(),
        _ => {
            return Err(PluginError::Validation(
                "Plugin credentials must be an object.".to_owned(),
            ));
        }
    };
    let mut existing = plugin_credentials_values(directory, plugin_id);
    for (k, v) in val_obj {
        if k.is_empty() || k.len() > MAX_PLUGIN_DATA_KEY {
            return Err(PluginError::Validation(
                "Plugin credential names must be short strings.".to_owned(),
            ));
        }
        if v.is_null() || v.as_str() == Some("") {
            existing.remove(&k);
            continue;
        }
        let Some(s) = v.as_str() else {
            return Err(PluginError::Validation(
                "Plugin credential values must be strings of at most 4096 characters.".to_owned(),
            ));
        };
        if s.len() > MAX_CREDENTIAL_VALUE {
            return Err(PluginError::Validation(
                "Plugin credential values must be strings of at most 4096 characters.".to_owned(),
            ));
        }
        existing.insert(k, s.to_owned());
    }
    for key in clear {
        existing.remove(key);
    }
    if existing.len() > MAX_CREDENTIAL_KEYS {
        return Err(PluginError::Validation(
            "Plugins may store at most 128 credentials.".to_owned(),
        ));
    }
    let path = plugin_store_path(directory, "credentials", plugin_id)?;
    if existing.is_empty() {
        let _ = fs::remove_file(&path);
    } else {
        let map: Map<String, Value> = existing
            .iter()
            .map(|(k, v)| (k.clone(), Value::String(v.clone())))
            .collect();
        write_json_store(&path, &map)?;
    }
    Ok(existing.into_keys().collect())
}

pub fn plugin_credentials_summary(directory: &Path, plugin_id: &str) -> Value {
    let values = plugin_credentials_values(directory, plugin_id);
    let keys: Vec<String> = values.into_keys().collect();
    let set_map: Map<String, Value> = keys
        .iter()
        .map(|k| (k.clone(), Value::Bool(true)))
        .collect();
    json!({
        "keys": keys,
        "set": set_map,
    })
}

// ---------------------------------------------------------------------------
// Listing, installing, toggling, removing
// ---------------------------------------------------------------------------

pub fn list_plugins(directory: &Path) -> Vec<Value> {
    let disabled: HashSet<String> = load_plugin_state(directory)
        .get("disabled")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default();

    let mut candidates = Vec::new();
    if let Ok(rd) = fs::read_dir(directory) {
        for entry in rd.flatten() {
            let p = entry.path();
            let name = entry.file_name().to_string_lossy().into_owned();
            if p.is_dir() && !name.starts_with('.') && !RESERVED_PLUGIN_IDS.contains(&name.as_str())
            {
                candidates.push((name, p));
            }
        }
    }
    candidates.sort_by(|a, b| a.0.cmp(&b.0));

    let sandbox = if candidates.is_empty() {
        "ready"
    } else {
        sandbox_status()
    };

    let mut plugins = Vec::with_capacity(candidates.len());
    for (name, path) in candidates {
        match read_manifest(&path) {
            Err(err) => {
                let mut invalid = Map::new();
                invalid.insert("id".to_owned(), Value::String(name.clone()));
                invalid.insert("name".to_owned(), Value::String(name));
                invalid.insert("version".to_owned(), Value::String(String::new()));
                invalid.insert("valid".to_owned(), Value::Bool(false));
                invalid.insert("error".to_owned(), Value::String(err.to_string()));
                invalid.insert("enabled".to_owned(), Value::Bool(false));
                invalid.insert("sandbox".to_owned(), Value::String(sandbox.to_owned()));
                invalid.insert("commands".to_owned(), Value::Array(Vec::new()));
                invalid.insert("hooks".to_owned(), Value::Array(Vec::new()));
                if let Some(code) = err.code() {
                    invalid.insert("code".to_owned(), Value::String(code.to_owned()));
                }
                plugins.push(Value::Object(invalid));
            }
            Ok(mut manifest) => {
                let id = manifest["id"].as_str().unwrap_or("").to_owned();
                let trust = plugin_trust_status(directory, &id);
                let grants = plugin_permission_grants(directory, &id);
                manifest.insert("valid".to_owned(), Value::Bool(true));
                manifest.insert("enabled".to_owned(), Value::Bool(!disabled.contains(&id)));
                manifest.insert("sandbox".to_owned(), Value::String(sandbox.to_owned()));
                manifest.insert("checksum".to_owned(), trust["checksum"].clone());
                manifest.insert("trusted".to_owned(), trust["trusted"].clone());
                manifest.insert("retrust".to_owned(), trust["retrust"].clone());
                manifest.insert(
                    "granted_permissions".to_owned(),
                    Value::Array(grants.into_iter().map(Value::String).collect()),
                );
                plugins.push(Value::Object(manifest));
            }
        }
    }
    plugins
}

pub fn plugin_commands(directory: &Path, include_disabled: bool) -> Vec<Value> {
    let mut commands = Vec::new();
    for manifest in list_plugins(directory) {
        let valid = manifest
            .get("valid")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let enabled = manifest
            .get("enabled")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        if !valid || (!include_disabled && !enabled) {
            continue;
        }
        if let Some(arr) = manifest.get("commands").and_then(Value::as_array) {
            commands.extend(arr.iter().cloned());
        }
    }
    commands
}

fn contains_symlink_recursive(dir: &Path) -> Result<bool, std::io::Error> {
    for entry in fs::read_dir(dir)? {
        let entry = entry?;
        let meta = fs::symlink_metadata(entry.path())?;
        if meta.file_type().is_symlink() {
            return Ok(true);
        }
        if meta.is_dir() && contains_symlink_recursive(&entry.path())? {
            return Ok(true);
        }
    }
    Ok(false)
}

fn copy_dir_recursive(src: &Path, dst: &Path) -> Result<(), std::io::Error> {
    fs::create_dir_all(dst)?;
    for entry in fs::read_dir(src)? {
        let entry = entry?;
        let meta = fs::symlink_metadata(entry.path())?;
        let target = dst.join(entry.file_name());
        if meta.is_dir() {
            copy_dir_recursive(&entry.path(), &target)?;
        } else if meta.is_file() {
            fs::copy(entry.path(), &target)?;
            let _ = fs::set_permissions(&target, meta.permissions());
        }
    }
    Ok(())
}

pub fn install_plugin(source: &Path, directory: &Path) -> Result<Map<String, Value>, PluginError> {
    let expanded = openbox_platform::expand_user(source);
    let is_zip = expanded
        .extension()
        .and_then(|e| e.to_str())
        .is_some_and(|e| e.eq_ignore_ascii_case("zip"));
    if !expanded.exists() || (!expanded.is_dir() && !is_zip) {
        return Err(PluginError::Validation(
            "Plugin path must be a directory or ZIP package.".to_owned(),
        ));
    }
    fs::create_dir_all(directory).map_err(|e| PluginError::Io(e.to_string()))?;
    let parent = directory.parent().unwrap_or(directory);
    let temp = tempfile::tempdir_in(parent).map_err(|e| PluginError::Io(e.to_string()))?;
    let staging_extract = temp.path().join("package");
    if expanded.is_dir() {
        if contains_symlink_recursive(&expanded).map_err(|e| PluginError::Io(e.to_string()))? {
            return Err(PluginError::Validation(
                "Plugin directories may not contain symlinks.".to_owned(),
            ));
        }
        copy_dir_recursive(&expanded, &staging_extract)
            .map_err(|e| PluginError::Io(e.to_string()))?;
    } else {
        fs::create_dir_all(&staging_extract).map_err(|e| PluginError::Io(e.to_string()))?;
        let file = File::open(&expanded).map_err(|e| PluginError::Io(e.to_string()))?;
        safe_zip_extract(file, &staging_extract, ArchiveLimits::default())
            .map_err(|e| PluginError::Validation(e.to_string()))?;
    }

    let package = if staging_extract.join("plugin.json").is_file() {
        staging_extract.clone()
    } else {
        fs::read_dir(&staging_extract)
            .ok()
            .and_then(|rd| {
                rd.flatten()
                    .map(|e| e.path())
                    .find(|p| p.is_dir() && p.join("plugin.json").is_file())
            })
            .unwrap_or_else(|| staging_extract.clone())
    };

    let mut manifest = read_manifest(&package)?;
    let id = manifest["id"].as_str().unwrap_or("").to_owned();
    if RESERVED_PLUGIN_IDS.contains(&id.as_str()) {
        return Err(PluginError::Validation("Plugin id is reserved.".to_owned()));
    }
    let destination = directory.join(&id);
    if destination.exists() && !destination.is_dir() {
        return Err(PluginError::Validation(
            "Plugin id collides with an OpenBox file.".to_owned(),
        ));
    }
    let updated = destination.exists();
    let stamp = Timestamp::now().strftime("%Y%m%d-%H%M%S-%6f").to_string();
    let backup = if updated {
        let b = directory.join(".backups").join(format!("{id}-{stamp}"));
        if let Some(bp) = b.parent() {
            fs::create_dir_all(bp).map_err(|e| PluginError::Io(e.to_string()))?;
        }
        Some(b)
    } else {
        None
    };

    let install_stage = directory.join(format!(".{id}.installing"));
    if install_stage.exists() {
        let _ = fs::remove_dir_all(&install_stage);
    }
    copy_dir_recursive(&package, &install_stage).map_err(|e| PluginError::Io(e.to_string()))?;

    let swap_res: Result<(), std::io::Error> = (|| {
        if let Some(ref b) = backup {
            fs::rename(&destination, b)?;
        }
        fs::rename(&install_stage, &destination)?;
        Ok(())
    })();

    if let Err(err) = swap_res {
        if let Some(ref b) = backup
            && !destination.exists()
            && b.exists()
        {
            let _ = fs::rename(b, &destination);
        }
        let _ = fs::remove_dir_all(&install_stage);
        return Err(PluginError::Io(err.to_string()));
    }
    if install_stage.exists() {
        let _ = fs::remove_dir_all(&install_stage);
    }
    if let Some(ref b) = backup
        && b.exists()
        && destination.exists()
    {
        let _ = fs::remove_dir_all(b);
    }

    manifest.insert("updated".to_owned(), Value::Bool(updated));
    Ok(manifest)
}

pub fn set_plugin_enabled(
    directory: &Path,
    plugin_id: &str,
    enabled: bool,
) -> Result<bool, PluginError> {
    let manifest = read_manifest(&directory.join(plugin_id))?;
    let id = manifest["id"].as_str().unwrap_or(plugin_id).to_owned();
    let mut state = load_plugin_state(directory);
    let mut disabled: BTreeSet<String> = state
        .get("disabled")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default();
    if enabled {
        disabled.remove(&id);
    } else {
        disabled.insert(id);
    }
    state.insert(
        "disabled".to_owned(),
        Value::Array(disabled.into_iter().map(Value::String).collect()),
    );
    save_plugin_state(directory, &state)?;
    Ok(enabled)
}

pub fn remove_plugin(directory: &Path, plugin_id: &str) -> Result<String, PluginError> {
    let manifest = read_manifest(&directory.join(plugin_id))?;
    let id = manifest["id"].as_str().unwrap_or(plugin_id).to_owned();
    let stamp = Timestamp::now().strftime("%Y%m%d-%H%M%S-%6f").to_string();
    let trash = directory.join(".removed").join(format!("{id}-{stamp}"));
    if let Some(tp) = trash.parent() {
        fs::create_dir_all(tp).map_err(|e| PluginError::Io(e.to_string()))?;
    }
    fs::rename(directory.join(&id), &trash).map_err(|e| PluginError::Io(e.to_string()))?;

    let mut state = load_plugin_state(directory);
    let mut disabled: BTreeSet<String> = state
        .get("disabled")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default();
    disabled.remove(&id);
    state.insert(
        "disabled".to_owned(),
        Value::Array(disabled.into_iter().map(Value::String).collect()),
    );
    for section in ["trust", "permissions", "settings"] {
        if let Some(obj) = state.get_mut(section).and_then(Value::as_object_mut) {
            obj.remove(&id);
        }
    }
    save_plugin_state(directory, &state)?;

    for folder in ["data", "credentials"] {
        if let Ok(path) = plugin_store_path(directory, folder, &id) {
            let _ = fs::remove_file(path);
        }
    }
    if let Some(parent) = directory.parent() {
        let _ = fs::remove_file(parent.join("plugin-stats").join(format!("{id}.json")));
    }
    Ok(id)
}

// ---------------------------------------------------------------------------
// Hook execution (v2 JSON Lines + single service_requests round-trip)
// ---------------------------------------------------------------------------

pub fn plugin_environment() -> HashMap<String, String> {
    const STRIP_EXACT: &[&str] = &["PYTHONPATH", "PYTHONHOME", "LD_PRELOAD", "LD_LIBRARY_PATH"];
    const SENSITIVE: &[&str] = &[
        "TOKEN",
        "PASSWORD",
        "SECRET",
        "API_KEY",
        "OPENBOX_",
        "RETROACHIEVEMENTS_",
        "EMUMOVIES_",
        "GITHUB_",
        "RA_",
        "IGDB_",
        "GAMEYFIN_",
    ];
    let mut env = HashMap::new();
    for (k, v) in std::env::vars() {
        if STRIP_EXACT.contains(&k.as_str()) {
            continue;
        }
        let upper = k.to_ascii_uppercase();
        if SENSITIVE.iter().any(|pat| upper.contains(pat)) {
            continue;
        }
        env.insert(k, v);
    }
    env.insert("PYTHONNOUSERSITE".to_owned(), "1".to_owned());
    env
}

fn build_hook_payload(
    root: &Path,
    manifest: &Map<String, Value>,
    hook: &str,
    payload: &Value,
) -> Value {
    let Some(mut body) = payload.as_object().cloned() else {
        return payload.clone();
    };
    let id = manifest.get("id").and_then(Value::as_str).unwrap_or("");
    body.insert(
        "plugin_data".to_owned(),
        Value::Object(plugin_data_values(root, id)),
    );
    let capability = PRIVILEGED_HOOK_NAMES.contains(&hook);
    if manifest.get("settings").is_some_and(Value::is_object) {
        let values = plugin_settings_values_public(root, manifest);
        body.insert("settings".to_owned(), Value::Object(values));
    }
    if capability {
        let creds: Map<String, Value> = plugin_credentials_values(root, id)
            .into_iter()
            .map(|(k, v)| (k, Value::String(v)))
            .collect();
        body.insert("credentials".to_owned(), Value::Object(creds));
    }
    Value::Object(body)
}

pub fn parse_v2_response(output: &str) -> (Option<Map<String, Value>>, String) {
    for line in output.lines() {
        let text = line.trim();
        if text.is_empty() {
            continue;
        }
        let Ok(parsed) = serde_json::from_str::<Value>(text) else {
            return (None, "Plugin returned invalid JSON.".to_owned());
        };
        let Some(msg) = parsed.as_object() else {
            return (None, "Plugin output must be a JSON object.".to_owned());
        };
        match msg.get("type").and_then(Value::as_str) {
            Some("error") => {
                let err = msg
                    .get("error")
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
                    .unwrap_or("Plugin failed.");
                return (None, err.to_owned());
            }
            Some("cancelled") => {
                return (None, "Plugin call was cancelled.".to_owned());
            }
            Some("result") => {
                let Some(res) = msg.get("result").and_then(Value::as_object) else {
                    return (None, "Plugin result must be an object.".to_owned());
                };
                return (Some(res.clone()), String::new());
            }
            _ => {}
        }
    }
    (None, "Plugin returned no response.".to_owned())
}

fn invoke_hook_v2(
    root: &Path,
    manifest: &Map<String, Value>,
    hook: &str,
    payload: &Value,
    timeout: Option<u64>,
    app_root: Option<&Path>,
) -> (Option<Map<String, Value>>, String) {
    if manifest.get("api_version").and_then(Value::as_i64) != Some(2) {
        return (None, PLUGIN_API_RETIRED_MESSAGE.to_owned());
    }
    let id = manifest.get("id").and_then(Value::as_str).unwrap_or("");
    let entry_rel = manifest
        .get("entry")
        .and_then(Value::as_str)
        .unwrap_or("plugin.py");
    let package_root = root.join(id);
    let entry = package_root.join(entry_rel);
    let prepared = build_hook_payload(root, manifest, hook, payload);
    let injected_data = prepared.get("plugin_data").cloned();

    let request = match serde_json::to_string(&json!({
        "id": "1",
        "type": "invoke",
        "hook": hook,
        "payload": prepared,
    })) {
        Ok(s) => s,
        Err(e) => return (None, e.to_string()),
    };
    if request.len() > MAX_PLUGIN_PAYLOAD {
        return (
            None,
            "Payload is too large for plugin execution.".to_owned(),
        );
    }

    let trusted = plugin_trust_status(root, id)
        .get("trusted")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let share_net = plugin_permission_grants(root, id)
        .iter()
        .any(|p| p == "network");
    let runner_opt = locate_runner_v2(app_root);
    let data_dir = root.parent();
    let cmd = match plugin_command(
        &package_root,
        &entry,
        trusted,
        share_net,
        2,
        data_dir,
        runner_opt.as_deref(),
    ) {
        Ok(Some(c)) => c,
        Ok(None) => {
            return (
                None,
                "bubblewrap is unavailable; trust this plugin in the Plugins manager to run it unsandboxed."
                    .to_owned(),
            );
        }
        Err(e) => return (None, e.to_string()),
    };

    let Some((prog, args)) = cmd.split_first() else {
        return (None, "Invalid plugin command.".to_owned());
    };
    let mut command = Command::new(prog);
    command
        .args(args)
        .env_clear()
        .envs(plugin_environment())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    let mut child = match command.spawn() {
        Ok(c) => c,
        Err(e) => return (None, e.to_string()),
    };

    let req_bytes = format!("{request}\n").into_bytes();
    if let Some(mut stdin) = child.stdin.take() {
        thread::spawn(move || {
            let _ = stdin.write_all(&req_bytes);
            let _ = stdin.flush();
        });
    }

    let stdout_handle = child.stdout.take().map(|stdout| {
        thread::spawn(move || {
            let mut buf = Vec::new();
            let mut limited = stdout.take((MAX_PLUGIN_OUTPUT_V2 + 1) as u64);
            let _ = limited.read_to_end(&mut buf);
            buf
        })
    });
    let stderr_handle = child.stderr.take().map(|mut stderr| {
        thread::spawn(move || {
            let mut all = Vec::new();
            let mut chunk = [0u8; 4096];
            while let Ok(n) = stderr.read(&mut chunk) {
                if n == 0 {
                    break;
                }
                all.extend_from_slice(&chunk[..n]);
                if all.len() > 8192 {
                    let drop_n = all.len() - 4096;
                    all.drain(..drop_n);
                }
            }
            let start = all.len().saturating_sub(400);
            String::from_utf8_lossy(&all[start..]).into_owned()
        })
    });

    let deadline = Instant::now() + Duration::from_secs(hook_timeout_secs(hook, timeout));
    let exit_status = loop {
        match child.try_wait() {
            Ok(Some(st)) => break Ok(st),
            Ok(None) => {
                if Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    break Err("Plugin hook timed out.".to_owned());
                }
                thread::sleep(Duration::from_millis(15));
            }
            Err(e) => break Err(e.to_string()),
        }
    };

    let stdout_bytes = stdout_handle
        .and_then(|h| h.join().ok())
        .unwrap_or_default();
    let stderr_text = stderr_handle
        .and_then(|h| h.join().ok())
        .unwrap_or_default();

    let status = match exit_status {
        Ok(st) => st,
        Err(msg) => return (None, msg),
    };

    if stdout_bytes.len() > MAX_PLUGIN_OUTPUT_V2 {
        return (None, "Plugin output exceeded the payload limit.".to_owned());
    }
    if !status.success() {
        let code = status.code().unwrap_or(-1);
        return (
            None,
            format!("Plugin exited with status {code}: {stderr_text}"),
        );
    }

    let stdout_str = String::from_utf8_lossy(&stdout_bytes);
    let (mut candidate, err) = parse_v2_response(&stdout_str);
    if !err.is_empty() {
        return (None, err);
    }
    if let Some(ref mut cand) = candidate
        && !PRIVILEGED_HOOK_NAMES.contains(&hook)
        && cand.get("plugin_data") == injected_data.as_ref()
    {
        cand.remove("plugin_data");
    }
    (candidate, String::new())
}

fn finalize_hook_result(
    root: &Path,
    manifest: &Map<String, Value>,
    mut candidate: Map<String, Value>,
) -> Map<String, Value> {
    let id = manifest.get("id").and_then(Value::as_str).unwrap_or("");
    if candidate.remove("credentials").is_some() {
        tracing::warn!(
            "Plugin {} returned credentials; hooks cannot write the credential store",
            id
        );
    }
    if let Some(data) = candidate.get("data") {
        if data.is_object() {
            if let Err(err) = set_plugin_data(root, id, data) {
                tracing::warn!("Plugin {} data was rejected: {}", id, err);
            }
        } else {
            tracing::warn!("Plugin {} returned non-object data; ignored", id);
        }
    }
    candidate
}

pub type HookRunOutcome = (String, Option<Map<String, Value>>, String);

#[allow(clippy::too_many_arguments)]
pub fn run_manifest_hook(
    root: &Path,
    manifest: &Map<String, Value>,
    hook: &str,
    payload: &Value,
    timeout: Option<u64>,
    state: Option<&Value>,
    metadata_db: Option<&Path>,
    app_root: Option<&Path>,
) -> (Option<Map<String, Value>>, String) {
    let (candidate_opt, err) = invoke_hook_v2(root, manifest, hook, payload, timeout, app_root);
    if !err.is_empty() {
        return (candidate_opt, err);
    }
    let Some(mut candidate) = candidate_opt else {
        return (None, String::new());
    };
    if let Some(reqs) = candidate.get("service_requests").cloned() {
        if !PRIVILEGED_HOOK_NAMES.contains(&hook) {
            return (
                None,
                "service_requests are only available to capability hooks.".to_owned(),
            );
        }
        let empty = json!({});
        let st = state.unwrap_or(&empty);
        let results = match execute_service_requests(&reqs, st, metadata_db) {
            Ok(r) => r,
            Err(e) => return (None, e),
        };
        let mut forwarded = payload.as_object().cloned().unwrap_or_default();
        forwarded.insert("service_results".to_owned(), Value::Object(results));
        let (cand2, err2) = invoke_hook_v2(
            root,
            manifest,
            hook,
            &Value::Object(forwarded),
            timeout,
            app_root,
        );
        if !err2.is_empty() {
            return (cand2, err2);
        }
        let Some(mut c2) = cand2 else {
            return (None, String::new());
        };
        if c2.remove("service_requests").is_some() {
            tracing::warn!(
                "Plugin {:?} requested services more than once; the second round was ignored",
                manifest.get("id")
            );
        }
        candidate = c2;
    }
    (
        Some(finalize_hook_result(root, manifest, candidate)),
        String::new(),
    )
}

#[allow(clippy::too_many_arguments)]
pub fn run_plugin_hook(
    directory: &Path,
    plugin_id: &str,
    hook: &str,
    payload: &Value,
    timeout: Option<u64>,
    state: Option<&Value>,
    metadata_db: Option<&Path>,
    app_root: Option<&Path>,
) -> (Option<Map<String, Value>>, String) {
    let manifest = match read_manifest(&directory.join(plugin_id)) {
        Ok(m) => m,
        Err(e) => return (None, e.to_string()),
    };
    let has_hook = manifest
        .get("hooks")
        .and_then(Value::as_array)
        .is_some_and(|a| a.iter().any(|v| v.as_str() == Some(hook)));
    if !has_hook {
        return (None, format!("Plugin does not declare the {hook} hook."));
    }
    run_manifest_hook(
        directory,
        &manifest,
        hook,
        payload,
        timeout,
        state,
        metadata_db,
        app_root,
    )
}

pub fn run_plugin_hooks(
    directory: &Path,
    hook: &str,
    payload: &Value,
    timeout: Option<u64>,
    state: Option<&Value>,
    metadata_db: Option<&Path>,
    app_root: Option<&Path>,
) -> Vec<HookRunOutcome> {
    let mut results = Vec::new();
    for item in list_plugins(directory) {
        let Some(manifest) = item.as_object() else {
            continue;
        };
        let valid = manifest
            .get("valid")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let enabled = manifest
            .get("enabled")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let has_hook = manifest
            .get("hooks")
            .and_then(Value::as_array)
            .is_some_and(|a| a.iter().any(|v| v.as_str() == Some(hook)));
        if !valid || !enabled || !has_hook {
            continue;
        }
        let id = manifest
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        let (cand, err) = run_manifest_hook(
            directory,
            manifest,
            hook,
            payload,
            timeout,
            state,
            metadata_db,
            app_root,
        );
        results.push((id, cand, err));
    }
    results
}

pub fn run_plugins(
    directory: &Path,
    hook: &str,
    payload: &Value,
    state: Option<&Value>,
    metadata_db: Option<&Path>,
    app_root: Option<&Path>,
) -> Value {
    let mut current = payload.clone();
    for item in list_plugins(directory) {
        let Some(manifest) = item.as_object() else {
            continue;
        };
        let valid = manifest
            .get("valid")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let enabled = manifest
            .get("enabled")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let has_hook = manifest
            .get("hooks")
            .and_then(Value::as_array)
            .is_some_and(|a| a.iter().any(|v| v.as_str() == Some(hook)));
        if !valid || !enabled || !has_hook {
            continue;
        }
        let id = manifest.get("id").and_then(Value::as_str).unwrap_or("");
        let (cand, err) = run_manifest_hook(
            directory,
            manifest,
            hook,
            &current,
            None,
            state,
            metadata_db,
            app_root,
        );
        if let Some(obj) = cand {
            current = Value::Object(obj);
        } else if !err.is_empty() {
            tracing::warn!("Plugin {} failed for {}: {}", id, hook, err);
        }
    }
    current
}

// ---------------------------------------------------------------------------
// Notifications, lifecycle events, library diff, library_source merge
// ---------------------------------------------------------------------------

fn format_template(template: &str, params: &HashMap<String, String>) -> String {
    let mut out = String::with_capacity(template.len());
    let chars: Vec<char> = template.chars().collect();
    let mut i = 0;
    while i < chars.len() {
        if chars[i] == '{' {
            if i + 1 < chars.len() && chars[i + 1] == '{' {
                out.push('{');
                i += 2;
                continue;
            }
            if let Some(end_rel) = chars[i + 1..].iter().position(|c| *c == '}') {
                let key: String = chars[i + 1..i + 1 + end_rel].iter().collect();
                if let Some(val) = params.get(&key) {
                    out.push_str(val);
                    i += end_rel + 2;
                    continue;
                }
                // Missing placeholder -> fall back to raw template like Python's KeyError catch
                return template.to_owned();
            }
            return template.to_owned();
        }
        if chars[i] == '}' && i + 1 < chars.len() && chars[i + 1] == '}' {
            out.push('}');
            i += 2;
            continue;
        }
        out.push(chars[i]);
        i += 1;
    }
    out
}

pub fn resolve_plugin_notification(
    manifest: &Map<String, Value>,
    notification: &Value,
    locale: &str,
) -> Result<Value, PluginError> {
    let notif_obj = match notification {
        Value::String(s) => {
            let mut m = Map::new();
            m.insert("message".to_owned(), Value::String(s.clone()));
            m
        }
        Value::Object(m) => m.clone(),
        _ => {
            return Err(PluginError::Validation(
                "Plugin notification must be an object.".to_owned(),
            ));
        }
    };
    let raw_level = notif_obj
        .get("level")
        .and_then(Value::as_str)
        .unwrap_or("info");
    let level = if NOTIFICATION_LEVELS.contains(&raw_level) {
        raw_level
    } else {
        "info"
    };
    let mut message = String::new();
    if let Some(key_val) = notif_obj.get("key")
        && !key_val.is_null()
        && key_val.as_str() != Some("")
    {
        let key = match key_val {
            Value::String(s) => s.clone(),
            other => other.to_string(),
        };
        let template = manifest
            .get("messages")
            .and_then(Value::as_object)
            .and_then(|m| m.get(&key))
            .and_then(Value::as_object)
            .and_then(|t| {
                t.get(locale)
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
                    .or_else(|| t.get("en").and_then(Value::as_str))
            })
            .map(str::to_owned)
            .unwrap_or_else(|| key.chars().take(MAX_MESSAGE_TEXT).collect());
        let mut safe_params = HashMap::new();
        if let Some(pobj) = notif_obj.get("params").and_then(Value::as_object) {
            for (k, v) in pobj.iter().take(32) {
                let s = match v {
                    Value::String(st) => st.clone(),
                    other => other.to_string(),
                };
                safe_params.insert(k.chars().take(64).collect(), s.chars().take(200).collect());
            }
        }
        message = format_template(&template, &safe_params);
    }
    if message.is_empty()
        && let Some(raw_msg) = notif_obj.get("message").and_then(Value::as_str)
    {
        message = raw_msg.to_owned();
    }
    Ok(json!({
        "level": level,
        "message": message.chars().take(2000).collect::<String>(),
    }))
}

pub fn emit_plugin_event(
    directory: &Path,
    event: &str,
    payload: Option<&Value>,
    state: Option<&mut Value>,
    app_root: Option<&Path>,
) {
    if std::env::var("OPENBOX_SAFE_MODE").is_ok_and(|v| !v.is_empty()) {
        return;
    }
    let mut body = Map::new();
    body.insert("event".to_owned(), Value::String(event.to_owned()));
    if let Some(pobj) = payload.and_then(Value::as_object) {
        for (k, v) in pobj {
            body.insert(k.clone(), v.clone());
        }
    }
    let locale = state
        .as_deref()
        .and_then(|s| s.get("settings"))
        .and_then(|s| s.get("locale"))
        .and_then(Value::as_str)
        .map(|s| s.chars().take(5).collect::<String>())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "en".to_owned());

    let results = run_plugin_hooks(
        directory,
        "events",
        &Value::Object(body),
        None,
        state.as_deref(),
        None,
        app_root,
    );
    let mut pending_notifs = Vec::new();
    for (plugin_id, cand_opt, err) in results {
        if !err.is_empty() {
            tracing::warn!("Plugin {} failed for event {}: {}", plugin_id, event, err);
            continue;
        }
        let Some(cand) = cand_opt else {
            continue;
        };
        let Some(notif) = cand.get("notification").filter(|v| v.is_object()) else {
            continue;
        };
        let Ok(manifest) = read_manifest(&directory.join(&plugin_id)) else {
            continue;
        };
        let Ok(resolved) = resolve_plugin_notification(&manifest, notif, &locale) else {
            continue;
        };
        let msg = resolved["message"].as_str().unwrap_or("").to_owned();
        if msg.is_empty() {
            continue;
        }
        let level = resolved["level"].as_str().unwrap_or("info").to_owned();
        pending_notifs.push((plugin_id, level, msg));
    }
    if let Some(st) = state {
        for (plugin_id, level, msg) in pending_notifs {
            let title: String = msg.chars().take(200).collect();
            let dedupe = format!("plugin:{plugin_id}:{event}");
            add_notification(
                st,
                NotificationSpec {
                    kind: "plugin",
                    level: &level,
                    title: &title,
                    body: &msg,
                    source: &plugin_id,
                    dedupe_key: &dedupe,
                    ..NotificationSpec::default()
                },
            );
        }
    }
}

pub type LibrarySnapshot = HashMap<String, Vec<String>>;

pub fn snapshot_library(state: &Value) -> LibrarySnapshot {
    let mut snap = HashMap::new();
    let Some(games) = state.get("games").and_then(Value::as_array) else {
        return snap;
    };
    for game in games {
        let Some(obj) = game.as_object() else {
            continue;
        };
        let game_id = obj.get("game_id").and_then(Value::as_str).unwrap_or("");
        if game_id.is_empty() {
            continue;
        }
        let mut fp = Vec::with_capacity(EVENT_TRACKED_FIELDS.len());
        for field in EVENT_TRACKED_FIELDS {
            let v = match *field {
                "playtime_seconds" => obj
                    .get(*field)
                    .and_then(Value::as_i64)
                    .unwrap_or(0)
                    .to_string(),
                "favorite" | "hidden" => obj
                    .get(*field)
                    .and_then(Value::as_bool)
                    .unwrap_or(false)
                    .to_string(),
                _ => obj
                    .get(*field)
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned(),
            };
            fp.push(v);
        }
        snap.insert(game_id.to_owned(), fp);
    }
    snap
}

pub fn diff_library_snapshots(before: &LibrarySnapshot, after: &LibrarySnapshot) -> Value {
    let added: Vec<String> = after
        .keys()
        .filter(|id| !before.contains_key(*id))
        .take(MAX_EVENT_IDS)
        .cloned()
        .collect();
    let removed: Vec<String> = before
        .keys()
        .filter(|id| !after.contains_key(*id))
        .take(MAX_EVENT_IDS)
        .cloned()
        .collect();
    let mut updated = Vec::new();
    for (game_id, fp_after) in after {
        if let Some(fp_before) = before.get(game_id)
            && fp_after != fp_before
        {
            let changed: Vec<&str> = EVENT_TRACKED_FIELDS
                .iter()
                .zip(fp_before.iter().zip(fp_after.iter()))
                .filter_map(|(field, (o, n))| if o != n { Some(*field) } else { None })
                .collect();
            if !changed.is_empty() {
                updated.push(json!({"game_id": game_id, "changed": changed}));
            }
            if updated.len() >= MAX_EVENT_UPDATES {
                break;
            }
        }
    }
    json!({
        "added": added,
        "removed": removed,
        "updated": updated,
    })
}

pub fn clean_plugin_game_fields(entry: &Map<String, Value>) -> Result<Map<String, Value>, String> {
    let name = entry
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .chars()
        .take(300)
        .collect::<String>();
    if name.is_empty() {
        return Err("Game name is required.".to_owned());
    }
    let platform = entry
        .get("platform")
        .and_then(Value::as_str)
        .unwrap_or("PC")
        .trim();
    let platform = if platform.is_empty() { "PC" } else { platform };
    let mut game = Map::new();
    game.insert("name".to_owned(), Value::String(name));
    game.insert(
        "platform".to_owned(),
        Value::String(platform.chars().take(80).collect()),
    );
    for key in [
        "path",
        "launch",
        "install_dir",
        "genre",
        "year",
        "developer",
        "publisher",
        "series",
        "description",
        "notes",
        "region",
        "source",
    ] {
        if let Some(s) = entry.get(key).and_then(Value::as_str) {
            let trimmed = s.trim();
            if !trimmed.is_empty() {
                game.insert(
                    key.to_owned(),
                    Value::String(trimmed.chars().take(2000).collect()),
                );
            }
        }
    }
    Ok(game)
}

pub fn merge_library_source_games(
    games: &[Value],
    data_parent: &Path,
    app_root: Option<&Path>,
) -> Vec<Value> {
    let plugins_dir = data_parent.join("plugins");
    let mut seen: HashSet<String> = games
        .iter()
        .filter_map(|g| g.get("game_id").and_then(Value::as_str).map(str::to_owned))
        .collect();
    let mut merged = Vec::new();
    for item in list_plugins(&plugins_dir) {
        let Some(manifest) = item.as_object() else {
            continue;
        };
        let valid = manifest
            .get("valid")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let enabled = manifest
            .get("enabled")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let has_hook = manifest
            .get("hooks")
            .and_then(Value::as_array)
            .is_some_and(|a| a.iter().any(|v| v.as_str() == Some("library_source")));
        if !valid || !enabled || !has_hook {
            continue;
        }
        let plugin_id = manifest.get("id").and_then(Value::as_str).unwrap_or("");
        let plugin_name = manifest
            .get("name")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .unwrap_or(plugin_id);
        let (res_opt, err) = run_plugin_hook(
            &plugins_dir,
            plugin_id,
            "library_source",
            &json!({"api_version": PLUGIN_API_VERSION}),
            None,
            None,
            None,
            app_root,
        );
        if !err.is_empty() {
            tracing::warn!("Plugin {} library_source failed: {}", plugin_id, err);
            continue;
        }
        let Some(res) = res_opt else {
            continue;
        };
        let Some(entries) = res.get("games").and_then(Value::as_array) else {
            continue;
        };
        for entry in entries.iter().take(MAX_LIBRARY_ENTRIES) {
            let Some(eobj) = entry.as_object() else {
                continue;
            };
            let Ok(mut game) = clean_plugin_game_fields(eobj) else {
                continue;
            };
            let their_id = eobj
                .get("id")
                .or_else(|| eobj.get("game_id"))
                .and_then(|v| match v {
                    Value::String(s) => Some(s.clone()),
                    Value::Number(n) => Some(n.to_string()),
                    _ => None,
                })
                .or_else(|| game.get("name").and_then(Value::as_str).map(str::to_owned))
                .unwrap_or_default()
                .chars()
                .take(96)
                .collect::<String>();
            if their_id.is_empty() {
                continue;
            }
            let namespaced: String = format!("plugin:{plugin_id}:{their_id}")
                .chars()
                .take(160)
                .collect();
            if !seen.insert(namespaced.clone()) {
                continue;
            }
            game.insert("game_id".to_owned(), Value::String(namespaced));
            game.insert(
                "plugin_source".to_owned(),
                Value::String(plugin_id.to_owned()),
            );
            game.insert(
                "plugin_source_name".to_owned(),
                Value::String(plugin_name.to_owned()),
            );
            merged.push(Value::Object(game));
        }
    }
    let mut out = games.to_vec();
    let base = out.len();
    for (offset, mut game) in merged.into_iter().enumerate() {
        if let Some(obj) = game.as_object_mut() {
            obj.insert("id".to_owned(), json!(base + offset));
        }
        out.push(game);
    }
    out
}
