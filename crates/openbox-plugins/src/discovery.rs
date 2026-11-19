//! Game discovery capability plugins (`pkg/parity/parity_plugin_discovery.py`).

use std::collections::HashMap;
use std::path::Path;
use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Map, Value, json};

use crate::host::{list_plugins, run_plugin_hook};
use crate::storefront::{plugin_manifest, require_plugin_capability};

pub const MAX_LISTS: usize = 10;
pub const MAX_IDS: usize = 50;
pub const MAX_ID_LENGTH: usize = 160;
pub const MAX_NAME_LENGTH: usize = 80;

static ID_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[^a-z0-9._-]+").unwrap());

fn slug(value: &str) -> String {
    let lowered = value.trim().to_lowercase();
    let replaced = ID_RE.replace_all(&lowered, "-");
    replaced.trim_matches('-').chars().take(80).collect()
}

fn empty_result(error: &str) -> Value {
    let mut payload = json!({
        "lists": {},
        "names": {},
        "game_ids": {},
        "truncated": false,
    });
    if !error.is_empty() {
        payload.as_object_mut().unwrap().insert(
            "error".to_owned(),
            Value::String(error.chars().take(500).collect()),
        );
    }
    payload
}

#[allow(clippy::too_many_arguments)]
pub fn fetch_plugin_discovery(
    plugins_dir: &Path,
    plugin_id: &str,
    games: &[Value],
    limit: usize,
    timeout: Option<u64>,
    state: Option<&Value>,
    metadata_db: Option<&Path>,
    app_root: Option<&Path>,
) -> Result<Value, String> {
    let manifest = plugin_manifest(plugins_dir, plugin_id)?;
    require_plugin_capability(&manifest, "discovery")?;
    let cap = limit.clamp(1, MAX_IDS);
    let mid = manifest["id"].as_str().unwrap_or(plugin_id);

    let mut index: HashMap<String, usize> = HashMap::new();
    for (pos, game) in games.iter().enumerate() {
        let Some(obj) = game.as_object() else {
            continue;
        };
        let gid = obj
            .get("game_id")
            .or_else(|| obj.get("id"))
            .and_then(|v| match v {
                Value::String(s) => Some(s.trim().to_owned()),
                Value::Number(n) => Some(n.to_string()),
                _ => None,
            })
            .unwrap_or_default();
        if !gid.is_empty() {
            index.entry(gid).or_insert(pos);
        }
    }

    let (res_opt, err) = run_plugin_hook(
        plugins_dir,
        mid,
        "discovery",
        &json!({"limit": cap}),
        timeout,
        state,
        metadata_db,
        app_root,
    );
    if !err.is_empty() {
        tracing::warn!("Plugin {} discovery failed: {}", mid, err);
        return Ok(empty_result(&err));
    }
    let Some(res) = res_opt else {
        return Ok(empty_result("Plugin discovery must return a JSON object."));
    };
    let Some(rows) = res.get("lists").and_then(Value::as_array) else {
        return Ok(empty_result("Plugin discovery must return {lists: [...]}."));
    };

    let mut lists = Map::new();
    let mut names = Map::new();
    let mut game_ids_map = Map::new();
    let mut truncated = rows.len() > MAX_LISTS;

    for raw in rows.iter().take(MAX_LISTS) {
        let Some(obj) = raw.as_object() else {
            continue;
        };
        let list_id = slug(obj.get("id").and_then(Value::as_str).unwrap_or(""));
        let Some(name) = obj.get("name").and_then(Value::as_str).map(str::trim) else {
            continue;
        };
        if list_id.is_empty() || name.is_empty() {
            continue;
        }
        let Some(raw_ids) = obj.get("game_ids").and_then(Value::as_array) else {
            continue;
        };
        if raw_ids.len() > cap {
            truncated = true;
        }
        let mut ids: Vec<String> = Vec::new();
        let mut indices: Vec<Value> = Vec::new();
        for val in raw_ids.iter().take(cap) {
            let gid = match val {
                Value::String(s) => s.trim().chars().take(MAX_ID_LENGTH).collect::<String>(),
                Value::Number(n) => n
                    .as_i64()
                    .or_else(|| n.as_f64().map(|f| f as i64))
                    .map(|i| i.to_string())
                    .unwrap_or_default(),
                _ => continue,
            };
            if let Some(&pos) = index.get(&gid)
                && !ids.contains(&gid)
            {
                ids.push(gid);
                indices.push(json!(pos));
            }
        }
        if indices.is_empty() {
            continue;
        }
        let key: String = format!("plugin:{mid}:{list_id}")
            .chars()
            .take(MAX_ID_LENGTH)
            .collect();
        lists.insert(key.clone(), Value::Array(indices));
        names.insert(
            key.clone(),
            Value::String(name.chars().take(MAX_NAME_LENGTH).collect()),
        );
        game_ids_map.insert(
            key,
            Value::Array(ids.into_iter().map(Value::String).collect()),
        );
    }

    Ok(json!({
        "lists": lists,
        "names": names,
        "game_ids": game_ids_map,
        "truncated": truncated,
    }))
}

pub fn merge_plugin_discovery(
    payload: &mut Value,
    games: &[Value],
    plugins_dir: &Path,
    limit: usize,
    state: Option<&Value>,
    metadata_db: Option<&Path>,
    app_root: Option<&Path>,
) {
    if !payload.is_object() {
        *payload = json!({});
    }
    let root_obj = payload.as_object_mut().unwrap();
    if !root_obj
        .get("discovery_names")
        .is_some_and(Value::is_object)
    {
        root_obj.insert("discovery_names".to_owned(), json!({}));
    }

    let manifests = list_plugins(plugins_dir);
    for item in manifests {
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
        let has_cap = manifest
            .get("capabilities")
            .and_then(Value::as_object)
            .and_then(|c| c.get("discovery"))
            .is_some_and(Value::is_object);
        if !has_cap {
            continue;
        }
        let plugin_id = manifest.get("id").and_then(Value::as_str).unwrap_or("");
        if plugin_id.is_empty() {
            continue;
        }
        let Ok(result) = fetch_plugin_discovery(
            plugins_dir,
            plugin_id,
            games,
            limit,
            None,
            state,
            metadata_db,
            app_root,
        ) else {
            continue;
        };
        if result
            .get("error")
            .and_then(Value::as_str)
            .is_some_and(|s| !s.is_empty())
        {
            continue;
        }
        if let Some(lists) = result.get("lists").and_then(Value::as_object) {
            for (k, indices) in lists {
                if indices.is_array() {
                    root_obj.insert(k.clone(), indices.clone());
                }
            }
        }
        if let Some(names) = result.get("names").and_then(Value::as_object)
            && let Some(dest_names) = root_obj
                .get_mut("discovery_names")
                .and_then(Value::as_object_mut)
        {
            for (k, name) in names {
                if name.is_string() {
                    dest_names.insert(k.clone(), name.clone());
                }
            }
        }
    }
}
