//! Storefront catalog browse and uninstalled import (`pkg/parity/parity_storefront.py`).

use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::Path;
use std::sync::LazyLock;

use openbox_core::ApiError;
use openbox_platform::{join_command, which};
use regex::Regex;
use serde_json::{Map, Value, json};

use crate::importers::{
    heroic_bases, import_heroic, import_lutris, import_steam, load_lutris_records, steam_roots,
    url_opener_command,
};

static STEAM_APP_ID_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#""(\d{1,8})"\s*\{"#).expect("valid steam app id regex"));

pub fn storefront_opener() -> Result<String, ApiError> {
    url_opener_command().ok_or_else(|| {
        ApiError::not_found("A browser or URL handler is required to browse storefront entries.")
    })
}

pub fn steam_owned_app_ids(home: Option<&Path>) -> HashSet<String> {
    let mut app_ids = HashSet::new();
    for root in steam_roots(home) {
        let userdata = root.join("userdata");
        let Ok(rd) = fs::read_dir(&userdata) else {
            continue;
        };
        for account in rd.filter_map(Result::ok) {
            let localconfig = account.path().join("config/localconfig.vdf");
            let Ok(text) = fs::read_to_string(&localconfig) else {
                continue;
            };
            for cap in STEAM_APP_ID_RE.captures_iter(&text) {
                if let Some(m) = cap.get(1)
                    && let Ok(num) = m.as_str().parse::<u64>()
                    && num > 0
                {
                    app_ids.insert(m.as_str().to_owned());
                }
            }
        }
    }
    app_ids
}

pub fn heroic_library_records(base: &Path) -> Vec<(String, Map<String, Value>)> {
    let mut records = Vec::new();
    let candidates = [
        base.join("store_cache/legendaryLibrary.json"),
        base.join("store_cache/gog_library.json"),
        base.join("store_cache/nileLibrary.json"),
        base.join("store_cache/gameDB.json"),
        base.join("GamesConfig/legendary.json"),
    ];
    for path in candidates {
        let Ok(text) = fs::read_to_string(&path) else {
            continue;
        };
        let Ok(payload) = serde_json::from_str::<Value>(&text) else {
            continue;
        };
        match payload {
            Value::Object(obj) => {
                for (k, v) in obj {
                    if let Some(m) = v.as_object() {
                        records.push((k, m.clone()));
                    }
                }
            }
            Value::Array(arr) => {
                for (i, v) in arr.into_iter().enumerate() {
                    if let Some(m) = v.as_object() {
                        records.push((i.to_string(), m.clone()));
                    }
                }
            }
            _ => {}
        }
    }
    records
}

pub fn catalog_steam(home: Option<&Path>) -> Result<Vec<Value>, ApiError> {
    let mut errs = Vec::new();
    let discovered: HashMap<String, Value> = import_steam(home, &mut errs)
        .into_iter()
        .filter_map(|g| {
            let id = g.get("steam_app_id").and_then(Value::as_str)?.to_owned();
            Some((id, g))
        })
        .collect();
    let owned_set = {
        let s = steam_owned_app_ids(home);
        if s.is_empty() {
            discovered.keys().cloned().collect::<HashSet<_>>()
        } else {
            s
        }
    };
    let opener = storefront_opener()?;
    let mut sorted_ids: Vec<String> = owned_set.into_iter().collect();
    sorted_ids.sort_by_key(|s| s.parse::<u64>().unwrap_or(0));
    let mut catalog = Vec::new();
    for app_id in sorted_ids {
        let record = discovered.get(&app_id);
        let installed_game = record.filter(|g| {
            g.get("store_installed")
                .and_then(Value::as_bool)
                .unwrap_or(false)
        });
        let name = record
            .and_then(|r| r.get("name").and_then(Value::as_str))
            .map(ToOwned::to_owned)
            .unwrap_or_else(|| format!("Steam App {app_id}"));
        let installed = installed_game.is_some();
        let install_uri = if installed {
            format!("steam://store/{app_id}")
        } else {
            format!("steam://install/{app_id}")
        };
        let path = installed_game
            .and_then(|g| g.get("path").and_then(Value::as_str))
            .unwrap_or(&opener);
        let launch = installed_game
            .and_then(|g| g.get("launch").and_then(Value::as_str))
            .map(ToOwned::to_owned)
            .unwrap_or_else(|| join_command(&[opener.clone(), format!("steam://store/{app_id}")]));
        let install_dir = record
            .and_then(|r| r.get("install_dir").and_then(Value::as_str))
            .unwrap_or("");
        catalog.push(json!({
            "id": app_id,
            "name": name,
            "source": "Steam",
            "installed": installed,
            "install_uri": install_uri,
            "repair_uri": format!("steam://validate/{app_id}"),
            "path": path,
            "launch": launch,
            "steam_app_id": app_id,
            "install_dir": install_dir,
        }));
    }
    Ok(catalog)
}

pub fn catalog_heroic(home: Option<&Path>) -> Result<Vec<Value>, ApiError> {
    let installed_list = import_heroic(home)?;
    let installed: HashMap<(String, String), Value> = installed_list
        .iter()
        .filter_map(|g| {
            let src = g.get("source").and_then(Value::as_str)?.to_owned();
            let id = g.get("heroic_app_id").and_then(Value::as_str)?.to_owned();
            Some(((src, id), g.clone()))
        })
        .collect();
    let opener = storefront_opener()?;
    let mut catalog = Vec::new();
    let mut seen = HashSet::new();
    for base in heroic_bases(home) {
        for (key, record) in heroic_library_records(&base) {
            let raw_src = record
                .get("store")
                .or_else(|| record.get("store_name"))
                .or_else(|| record.get("platform"))
                .and_then(Value::as_str)
                .unwrap_or("Epic");
            let low = raw_src.to_lowercase();
            let source = if matches!(low.as_str(), "epic" | "legendary") {
                "Epic"
            } else if low == "gog" {
                "GOG"
            } else if matches!(low.as_str(), "amazon" | "nile") {
                "Amazon"
            } else if low.contains("xbox") || low.contains("game pass") {
                "Xbox"
            } else {
                raw_src
            };
            let app_id = ["app_name", "appName", "product_id", "id"]
                .iter()
                .find_map(|k| {
                    record
                        .get(*k)
                        .and_then(Value::as_str)
                        .filter(|s| !s.is_empty())
                })
                .map(ToOwned::to_owned)
                .unwrap_or(key);
            let Some(title) = ["title", "app_title", "name"].iter().find_map(|k| {
                record
                    .get(*k)
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
            }) else {
                continue;
            };
            if !seen.insert((source.to_owned(), app_id.clone())) {
                continue;
            }
            let runner = match source {
                "GOG" => "gog",
                "Amazon" => "nile",
                "Xbox" => "xbox",
                _ => "legendary",
            };
            let inst = installed.get(&(source.to_owned(), app_id.clone()));
            let path = inst
                .and_then(|g| g.get("path").and_then(Value::as_str))
                .unwrap_or(&opener);
            let launch = inst
                .and_then(|g| g.get("launch").and_then(Value::as_str))
                .map(ToOwned::to_owned)
                .unwrap_or_else(|| {
                    join_command(&[
                        opener.clone(),
                        format!("heroic://install/{runner}/{app_id}"),
                    ])
                });
            let install_dir = inst
                .and_then(|g| g.get("install_dir").and_then(Value::as_str))
                .unwrap_or("");
            catalog.push(json!({
                "id": app_id,
                "name": title,
                "source": source,
                "installed": inst.is_some(),
                "install_uri": format!("heroic://install/{runner}/{app_id}"),
                "path": path,
                "launch": launch,
                "heroic_app_id": app_id,
                "install_dir": install_dir,
            }));
        }
    }
    if catalog.is_empty() {
        for g in installed_list {
            let app_id = g.get("heroic_app_id").and_then(Value::as_str).unwrap_or("");
            let launch = g.get("launch").and_then(Value::as_str).unwrap_or("");
            catalog.push(json!({
                "id": app_id,
                "name": g.get("name").and_then(Value::as_str).unwrap_or(""),
                "source": g.get("source").and_then(Value::as_str).unwrap_or(""),
                "installed": true,
                "install_uri": launch.replace("{heroic_app_id}", app_id),
                "path": g.get("path").and_then(Value::as_str).unwrap_or(""),
                "launch": launch,
                "heroic_app_id": app_id,
                "install_dir": g.get("install_dir").and_then(Value::as_str).unwrap_or(""),
            }));
        }
    }
    Ok(catalog)
}

pub fn catalog_lutris(home: Option<&Path>) -> Result<Vec<Value>, ApiError> {
    let command = if let Some(bin) = which("lutris") {
        vec![bin.to_string_lossy().into_owned()]
    } else if let Some(fp) = which("flatpak") {
        vec![
            fp.to_string_lossy().into_owned(),
            "run".into(),
            "net.lutris.Lutris".into(),
        ]
    } else {
        return Err(ApiError::not_found(
            "Lutris or Flatpak is required to browse the Lutris catalog.",
        ));
    };
    let records = load_lutris_records(&command, false)?;
    let installed_games: HashMap<String, Value> = import_lutris(home)
        .unwrap_or_default()
        .into_iter()
        .filter_map(|g| {
            let id = g.get("lutris_id").and_then(Value::as_str)?.to_owned();
            Some((id, g))
        })
        .collect();
    let mut catalog = Vec::new();
    for rec in records {
        let Some(obj) = rec.as_object() else {
            continue;
        };
        let game_id = match obj.get("id") {
            Some(Value::Number(n)) => n.to_string(),
            Some(Value::String(s)) => s.trim().to_owned(),
            _ => String::new(),
        };
        let name = obj
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_owned();
        if game_id.is_empty() || !game_id.chars().all(|c| c.is_ascii_digit()) || name.is_empty() {
            continue;
        }
        let origin = format!(
            "{} {}",
            obj.get("service").and_then(Value::as_str).unwrap_or(""),
            obj.get("source").and_then(Value::as_str).unwrap_or("")
        )
        .to_lowercase();
        let source = if origin.contains("xbox") || origin.contains("game pass") {
            "Xbox"
        } else if origin.contains("origin") || origin.contains("ea app") {
            "EA"
        } else if origin.contains("ubisoft") || origin.contains("uplay") {
            "Ubisoft"
        } else {
            "Lutris"
        };
        let inst = installed_games.get(&game_id);
        let installed = obj.get("installed") == Some(&Value::Bool(true)) || inst.is_some();
        let path = inst
            .and_then(|g| g.get("path").and_then(Value::as_str))
            .unwrap_or(&command[0]);
        let launch = inst
            .and_then(|g| g.get("launch").and_then(Value::as_str))
            .map(ToOwned::to_owned)
            .unwrap_or_else(|| format!("{} lutris:rungameid/{game_id}", command.join(" ")));
        let install_dir = inst
            .and_then(|g| g.get("install_dir").and_then(Value::as_str))
            .unwrap_or("");
        catalog.push(json!({
            "id": game_id,
            "name": name,
            "source": source,
            "installed": installed,
            "install_uri": format!("lutris:rungameid/{game_id}"),
            "path": path,
            "launch": launch,
            "lutris_id": game_id,
            "install_dir": install_dir,
        }));
    }
    Ok(catalog)
}

pub fn storefront_catalog(source: &str, _settings: &Value) -> Result<Vec<Value>, ApiError> {
    match source.trim().to_lowercase().as_str() {
        "steam" => catalog_steam(None),
        "heroic" => catalog_heroic(None),
        "lutris" => catalog_lutris(None),
        "gameyfin" => Err(ApiError::bad_request("Gameyfin URL is not configured.")),
        _ => Err(ApiError::bad_request(
            "Storefront source must be steam, heroic, lutris, or gameyfin.",
        )),
    }
}

pub fn catalog_entries_to_games(
    entries: &[Value],
    uninstalled_only: bool,
    installed_only: bool,
) -> Vec<Value> {
    let mut games = Vec::new();
    for entry in entries {
        let installed = entry
            .get("installed")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        if (uninstalled_only && installed) || (installed_only && !installed) {
            continue;
        }
        let source = entry.get("source").and_then(Value::as_str).unwrap_or("");
        let platform = entry
            .get("platform")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .unwrap_or(
                if matches!(
                    source,
                    "Steam"
                        | "Epic"
                        | "GOG"
                        | "Amazon"
                        | "EA"
                        | "Ubisoft"
                        | "Xbox"
                        | "Lutris"
                        | "Gameyfin"
                ) {
                    "PC"
                } else {
                    "Windows"
                },
            );
        let mut game = json!({
            "name": entry.get("name").and_then(Value::as_str).unwrap_or(""),
            "platform": platform,
            "source": source,
            "collection": source,
            "path": entry.get("path").and_then(Value::as_str).unwrap_or(""),
            "launch": entry.get("launch").and_then(Value::as_str).unwrap_or(""),
            "install_dir": entry.get("install_dir").and_then(Value::as_str).unwrap_or(""),
            "store_catalog": true,
            "store_installed": installed,
            "owned": true,
            "description": entry.get("description").and_then(Value::as_str).unwrap_or(""),
            "install_uri": entry.get("install_uri").and_then(Value::as_str).unwrap_or(""),
            "repair_uri": entry.get("repair_uri").and_then(Value::as_str).unwrap_or(""),
        });
        if let Some(m) = game.as_object_mut() {
            for k in ["steam_app_id", "heroic_app_id", "lutris_id", "gameyfin_id"] {
                if let Some(v) = entry
                    .get(k)
                    .filter(|v| !v.is_null() && v.as_str() != Some(""))
                {
                    m.insert(k.to_owned(), v.clone());
                }
            }
            if entry
                .get("gameyfin_id")
                .and_then(Value::as_str)
                .is_some_and(|s| !s.is_empty())
            {
                m.insert(
                    "gameyfin_provider".into(),
                    Value::String(
                        entry
                            .get("gameyfin_provider")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_owned(),
                    ),
                );
            }
            if !installed {
                let notes = entry
                    .get("notes")
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
                    .map(ToOwned::to_owned)
                    .unwrap_or_else(|| {
                        format!(
                            "Owned via {source}. Install or open the storefront page to get files locally."
                        )
                    });
                m.insert("notes".into(), Value::String(notes));
            }
        }
        games.push(game);
    }
    games
}
