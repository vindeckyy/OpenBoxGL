//! Plugin-provided storefronts (`pkg/parity/parity_plugin_storefront.py`).

use std::collections::HashSet;
use std::path::Path;

use openbox_platform::split_command;
use serde_json::{Map, Value, json};
use url::Url;

use crate::host::{
    clean_plugin_game_fields, list_plugins, plugin_permission_grants, plugin_settings_values,
    plugin_settings_values_public, run_plugin_hook,
};

pub const MAX_PAGES: usize = 20;
pub const MAX_ENTRIES: usize = 2000;
pub const MAX_ID_LENGTH: usize = 160;
pub const MAX_TEXT_LENGTH: usize = 300;
pub const MAX_URI_LENGTH: usize = 2000;
pub const MAX_PLATFORM_LENGTH: usize = 40;
pub const MAX_CURSOR_LENGTH: usize = 200;
pub const MAX_SETTINGS_KEYS: usize = 128;

const STORE_TEXT_FIELDS: &[&str] = &["path", "launch", "install_dir"];
const LAUNCH_INTERPRETERS: &[&str] = &[
    "sh",
    "bash",
    "zsh",
    "dash",
    "fish",
    "ksh",
    "csh",
    "tcsh",
    "pwsh",
    "powershell",
    "powershell.exe",
    "cmd",
    "cmd.exe",
    "python",
    "python3",
    "python.exe",
    "perl",
    "ruby",
    "node",
    "osascript",
    "busybox",
];
const SYSTEM_LAUNCH_ALLOW: &[&str] = &[
    "open",
    "xdg-open",
    "steam",
    "steam.exe",
    "lutris",
    "heroic",
    "heroic.exe",
];
const SYSTEM_BIN_DIRS: &[&str] = &[
    "/bin",
    "/sbin",
    "/usr/bin",
    "/usr/sbin",
    "/usr/local/bin",
    "/usr/local/sbin",
];
const NETWORK_ERROR: &str = "Grant network access to this plugin first.";

pub fn plugin_manifest(plugins_dir: &Path, plugin_id: &str) -> Result<Map<String, Value>, String> {
    let wanted = plugin_id.trim();
    if wanted.is_empty() {
        return Err("A plugin id is required.".to_owned());
    }
    for item in list_plugins(plugins_dir) {
        let Some(manifest) = item.as_object() else {
            continue;
        };
        if manifest.get("id").and_then(Value::as_str) != Some(wanted) {
            continue;
        }
        if !manifest
            .get("valid")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            let err = manifest
                .get("error")
                .and_then(Value::as_str)
                .unwrap_or("unknown error");
            return Err(format!("Plugin is invalid: {err}."));
        }
        if !manifest
            .get("enabled")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            return Err("Plugin is disabled.".to_owned());
        }
        return Ok(manifest.clone());
    }
    Err(format!("Unknown plugin: {wanted}."))
}

pub fn require_plugin_capability(
    manifest: &Map<String, Value>,
    capability: &str,
) -> Result<Map<String, Value>, String> {
    manifest
        .get("capabilities")
        .and_then(Value::as_object)
        .and_then(|c| c.get(capability))
        .and_then(Value::as_object)
        .cloned()
        .ok_or_else(|| format!("Plugin does not declare the {capability} capability."))
}

pub fn require_network_grant(
    manifest: &Map<String, Value>,
    plugins_dir: &Path,
) -> Result<(), String> {
    let declared = manifest
        .get("permissions")
        .and_then(Value::as_array)
        .is_some_and(|a| a.iter().any(|v| v.as_str() == Some("network")));
    if !declared {
        return Err("Plugin does not declare network access.".to_owned());
    }
    let mut granted: HashSet<String> = manifest
        .get("granted_permissions")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default();
    if granted.is_empty()
        && let Some(id) = manifest.get("id").and_then(Value::as_str)
    {
        granted.extend(plugin_permission_grants(plugins_dir, id));
    }
    if !granted.contains("network") {
        return Err(NETWORK_ERROR.to_owned());
    }
    Ok(())
}

fn clean_settings_override(settings: Option<&Value>) -> Result<Map<String, Value>, String> {
    let Some(val) = settings else {
        return Ok(Map::new());
    };
    if val.is_null() {
        return Ok(Map::new());
    }
    let Some(obj) = val.as_object() else {
        return Err("Plugin storefront settings must be an object.".to_owned());
    };
    let mut out = Map::new();
    for (k, v) in obj.iter().take(MAX_SETTINGS_KEYS) {
        out.insert(k.chars().take(80).collect(), v.clone());
    }
    Ok(out)
}

pub fn list_storefront_plugins(plugins_dir: &Path) -> Vec<Value> {
    let mut items = Vec::new();
    for item in list_plugins(plugins_dir) {
        let Some(manifest) = item.as_object() else {
            continue;
        };
        if !manifest
            .get("valid")
            .and_then(Value::as_bool)
            .unwrap_or(false)
            || !manifest
                .get("enabled")
                .and_then(Value::as_bool)
                .unwrap_or(false)
        {
            continue;
        }
        let Some(cap) = manifest
            .get("capabilities")
            .and_then(Value::as_object)
            .and_then(|c| c.get("storefront"))
            .and_then(Value::as_object)
        else {
            continue;
        };
        let values = plugin_settings_values_public(plugins_dir, manifest);
        let granted = manifest
            .get("granted_permissions")
            .and_then(Value::as_array)
            .is_some_and(|a| a.iter().any(|v| v.as_str() == Some("network")));
        let cap_name = cap
            .get("name")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .or_else(|| manifest.get("name").and_then(Value::as_str))
            .unwrap_or("");
        items.push(json!({
            "plugin_id": manifest.get("id").and_then(Value::as_str).unwrap_or(""),
            "id": cap.get("id").and_then(Value::as_str).unwrap_or(""),
            "name": cap_name,
            "platform": cap.get("platform").and_then(Value::as_str).unwrap_or(""),
            "settings_schema": manifest.get("settings").filter(|v| v.is_object()).cloned().unwrap_or_else(|| json!({"type": "object", "properties": {}})),
            "settings_values": values,
            "permission_granted": granted,
            "enabled": true,
        }));
    }
    items
}

fn https_uri(val: Option<&Value>) -> String {
    let Some(raw) = val.and_then(Value::as_str) else {
        return String::new();
    };
    let candidate = raw.trim();
    if candidate.is_empty() || candidate.len() > MAX_URI_LENGTH {
        return String::new();
    }
    let Ok(parsed) = Url::parse(candidate) else {
        return String::new();
    };
    if parsed.scheme() != "https"
        || parsed.host_str().is_none()
        || !parsed.username().is_empty()
        || parsed.password().is_some()
    {
        return String::new();
    }
    candidate.to_owned()
}

fn text_field(val: Option<&Value>, limit: usize) -> String {
    match val {
        Some(Value::String(s)) => s.trim().chars().take(limit).collect(),
        Some(Value::Number(n)) => n.to_string().chars().take(limit).collect(),
        _ => String::new(),
    }
}

pub fn clean_launch(command: &str) -> Result<String, String> {
    let trimmed = command.trim();
    if trimmed.is_empty() {
        return Ok(String::new());
    }
    let parts = split_command(trimmed)?;
    let Some(first) = parts.first() else {
        return Ok(String::new());
    };
    let executable = Path::new(first);
    let name = executable
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("")
        .to_lowercase();
    if LAUNCH_INTERPRETERS.contains(&name.as_str()) || name.starts_with("python") {
        return Err("Plugin launch command cannot start a shell or interpreter.".to_owned());
    }
    if (executable.is_absolute() || executable.has_root())
        && let Some(parent) = executable.parent()
    {
        let p = parent.to_string_lossy().replace('\\', "/");
        if SYSTEM_BIN_DIRS.contains(&p.as_str()) && !SYSTEM_LAUNCH_ALLOW.contains(&name.as_str()) {
            return Err("Plugin launch command cannot run a system binary.".to_owned());
        }
    }
    Ok(trimmed.to_owned())
}

fn clean_entry(
    raw: &Map<String, Value>,
    manifest: &Map<String, Value>,
    capability: &Map<String, Value>,
) -> Option<Map<String, Value>> {
    let their_id = match raw.get("id").or_else(|| raw.get("game_id")) {
        Some(Value::String(s)) if !s.trim().is_empty() => {
            s.trim().chars().take(MAX_ID_LENGTH).collect::<String>()
        }
        Some(Value::Number(n)) => n
            .as_i64()
            .or_else(|| n.as_f64().map(|f| f as i64))?
            .to_string(),
        _ => return None,
    };
    let name = text_field(raw.get("name"), 200);
    if name.is_empty() {
        return None;
    }
    let (installed, installed_known) = match raw.get("installed") {
        Some(Value::Bool(b)) => (*b, true),
        Some(Value::Number(n)) if n.as_i64() == Some(0) => (false, true),
        Some(Value::Number(n)) if n.as_i64() == Some(1) => (true, true),
        _ => (false, false),
    };
    let mut platform = text_field(raw.get("platform"), MAX_PLATFORM_LENGTH);
    if platform.is_empty() {
        platform = capability
            .get("platform")
            .and_then(Value::as_str)
            .unwrap_or("")
            .chars()
            .take(MAX_PLATFORM_LENGTH)
            .collect();
    }
    let plugin_id = manifest.get("id").and_then(Value::as_str).unwrap_or("");
    let source = capability
        .get("name")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .or_else(|| {
            manifest
                .get("name")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
        })
        .unwrap_or(plugin_id);

    let mut entry = Map::new();
    entry.insert(
        "id".to_owned(),
        Value::String(
            format!("plugin:{plugin_id}:{their_id}")
                .chars()
                .take(MAX_ID_LENGTH)
                .collect(),
        ),
    );
    entry.insert("plugin_id".to_owned(), Value::String(plugin_id.to_owned()));
    entry.insert("plugin_store_id".to_owned(), Value::String(their_id));
    entry.insert("name".to_owned(), Value::String(name));
    entry.insert("source".to_owned(), Value::String(source.to_owned()));
    entry.insert("platform".to_owned(), Value::String(platform));
    entry.insert("installed".to_owned(), Value::Bool(installed));
    entry.insert("installed_known".to_owned(), Value::Bool(installed_known));
    entry.insert(
        "install_uri".to_owned(),
        Value::String(https_uri(raw.get("install_uri"))),
    );
    entry.insert(
        "repair_uri".to_owned(),
        Value::String(https_uri(raw.get("repair_uri"))),
    );
    entry.insert("store_catalog".to_owned(), Value::Bool(true));

    for key in STORE_TEXT_FIELDS {
        let val = raw
            .get(*key)
            .and_then(Value::as_str)
            .map(|s| s.trim().chars().take(MAX_URI_LENGTH).collect::<String>())
            .unwrap_or_default();
        entry.insert((*key).to_owned(), Value::String(val));
    }
    let launch_str = entry.get("launch").and_then(Value::as_str).unwrap_or("");
    let cleaned_launch = clean_launch(launch_str).ok()?;
    entry.insert("launch".to_owned(), Value::String(cleaned_launch));
    Some(entry)
}

type StorefrontRowsResult = (
    Map<String, Value>,
    Map<String, Value>,
    Vec<Map<String, Value>>,
);

#[allow(clippy::too_many_arguments)]
fn storefront_rows(
    plugins_dir: &Path,
    plugin_id: &str,
    settings: Option<&Value>,
    action: &str,
    collection: &str,
    cursor: &str,
    installed_only: bool,
    uninstalled_only: bool,
    timeout: u64,
    state: Option<&Value>,
    metadata_db: Option<&Path>,
    app_root: Option<&Path>,
) -> Result<StorefrontRowsResult, String> {
    let manifest = plugin_manifest(plugins_dir, plugin_id)?;
    let capability = require_plugin_capability(&manifest, "storefront")?;
    require_network_grant(&manifest, plugins_dir)?;
    let values = if settings.is_some() && !settings.unwrap().is_null() {
        clean_settings_override(settings)?
    } else {
        plugin_settings_values(plugins_dir, &manifest)
    };
    let id = manifest["id"].as_str().unwrap_or(plugin_id);
    let mut entries = Vec::new();
    let mut next_cursor: String = cursor.trim().chars().take(MAX_CURSOR_LENGTH).collect();
    let mut seen: HashSet<String> = HashSet::new();
    let mut pages = 0usize;

    while pages < MAX_PAGES {
        let payload = json!({
            "action": action,
            "cursor": next_cursor,
            "settings": values,
            "installed_only": installed_only,
            "uninstalled_only": uninstalled_only,
        });
        let (res_opt, err) = run_plugin_hook(
            plugins_dir,
            id,
            "storefront",
            &payload,
            Some(timeout),
            state,
            metadata_db,
            app_root,
        );
        if !err.is_empty() {
            return Err(format!("Plugin storefront failed: {err}"));
        }
        let Some(result) = res_opt else {
            return Err("Plugin storefront must return a JSON object.".to_owned());
        };
        let alt = if collection == "games" {
            "entries"
        } else {
            "games"
        };
        let rows = result
            .get(collection)
            .or_else(|| result.get(alt))
            .and_then(Value::as_array)
            .ok_or_else(|| format!("Plugin storefront must return {{{collection}: [...]}}."))?;

        for raw in rows.iter().take(MAX_ENTRIES) {
            if let Some(obj) = raw.as_object() {
                entries.push(obj.clone());
            }
            if entries.len() >= MAX_ENTRIES {
                break;
            }
        }
        pages += 1;
        if entries.len() >= MAX_ENTRIES {
            break;
        }
        match result.get("next_cursor") {
            None | Some(Value::Null) => break,
            Some(Value::String(s)) => {
                let trimmed: String = s.trim().chars().take(MAX_CURSOR_LENGTH).collect();
                if trimmed.is_empty() {
                    break;
                }
                if !seen.insert(trimmed.clone()) {
                    return Err("Plugin storefront pagination repeated a cursor.".to_owned());
                }
                next_cursor = trimmed;
            }
            Some(_) => return Err("Storefront next_cursor must be text.".to_owned()),
        }
    }
    Ok((manifest, capability, entries))
}

#[allow(clippy::too_many_arguments)]
pub fn plugin_storefront_catalog(
    plugins_dir: &Path,
    plugin_id: &str,
    settings: Option<&Value>,
    cursor: &str,
    installed_only: bool,
    uninstalled_only: bool,
    timeout: u64,
    state: Option<&Value>,
    metadata_db: Option<&Path>,
    app_root: Option<&Path>,
) -> Result<Vec<Value>, String> {
    let (manifest, capability, rows) = storefront_rows(
        plugins_dir,
        plugin_id,
        settings,
        "catalog",
        "entries",
        cursor,
        installed_only,
        uninstalled_only,
        timeout,
        state,
        metadata_db,
        app_root,
    )?;
    let mut out = Vec::new();
    for raw in rows {
        if let Some(entry) = clean_entry(&raw, &manifest, &capability) {
            out.push(Value::Object(entry));
        }
        if out.len() >= MAX_ENTRIES {
            break;
        }
    }
    Ok(out)
}

#[allow(clippy::too_many_arguments)]
pub fn plugin_storefront_import(
    plugins_dir: &Path,
    plugin_id: &str,
    settings: Option<&Value>,
    installed_only: bool,
    uninstalled_only: bool,
    timeout: u64,
    state: Option<&Value>,
    metadata_db: Option<&Path>,
    app_root: Option<&Path>,
) -> Result<Vec<Value>, String> {
    let (manifest, capability, rows) = storefront_rows(
        plugins_dir,
        plugin_id,
        settings,
        "import",
        "games",
        "",
        installed_only,
        uninstalled_only,
        timeout,
        state,
        metadata_db,
        app_root,
    )?;
    let mid = manifest.get("id").and_then(Value::as_str).unwrap_or("");
    let mname = manifest
        .get("name")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .unwrap_or(mid);

    let mut games = Vec::new();
    for raw in rows {
        let Some(entry) = clean_entry(&raw, &manifest, &capability) else {
            continue;
        };
        let known = entry
            .get("installed_known")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let installed = entry
            .get("installed")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        if known {
            if installed_only && !installed {
                continue;
            }
            if uninstalled_only && installed {
                continue;
            }
        }
        let Ok(mut game) = clean_plugin_game_fields(&entry) else {
            continue;
        };
        let source = entry.get("source").and_then(Value::as_str).unwrap_or("");
        game.insert("game_id".to_owned(), entry["id"].clone());
        game.insert("source".to_owned(), Value::String(source.to_owned()));
        game.insert("collection".to_owned(), Value::String(source.to_owned()));
        game.insert("store_catalog".to_owned(), Value::Bool(true));
        game.insert("store_installed".to_owned(), Value::Bool(installed));
        game.insert("owned".to_owned(), Value::Bool(true));
        game.insert(
            "install_uri".to_owned(),
            entry
                .get("install_uri")
                .cloned()
                .unwrap_or_else(|| json!("")),
        );
        game.insert(
            "repair_uri".to_owned(),
            entry
                .get("repair_uri")
                .cloned()
                .unwrap_or_else(|| json!("")),
        );
        game.insert("plugin_source".to_owned(), Value::String(mid.to_owned()));
        game.insert(
            "plugin_source_name".to_owned(),
            Value::String(mname.to_owned()),
        );
        game.insert(
            "plugin_store_id".to_owned(),
            entry["plugin_store_id"].clone(),
        );
        if !installed
            && game
                .get("notes")
                .and_then(Value::as_str)
                .is_none_or(str::is_empty)
        {
            game.insert(
                "notes".to_owned(),
                Value::String(format!(
                    "Owned via {source}. Install or open the storefront page to get files locally."
                )),
            );
        }
        games.push(Value::Object(game));
    }
    Ok(games)
}
