//! Stats and achievement provider capability plugins (`pkg/parity/parity_plugin_stats.py`).

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use jiff::Timestamp;
use openbox_io::atomic_write_text;
use regex::Regex;
use serde_json::{Map, Value, json};

use crate::host::{list_plugins, run_plugin_hook};
use crate::storefront::{plugin_manifest, require_network_grant, require_plugin_capability};

pub const STATS_DIRNAME: &str = "plugin-stats";
pub const MAX_CACHE_BYTES: usize = 10 * 1024 * 1024;
pub const MAX_GAMES: usize = 500;
pub const MAX_ITEMS: usize = 500;
pub const MAX_ITEMS_TOTAL: usize = 2000;
pub const MAX_TOTAL: i64 = 10_000;
pub const MAX_PLAYTIME: i64 = 1_000_000_000;
pub const MAX_TEXT: usize = 2000;
pub const RESERVED_SOURCES: &[&str] = &["retroachievements", "steam"];

static SOURCE_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[^a-z0-9._-]+").unwrap());

pub fn stats_dir(data_dir: &Path) -> PathBuf {
    data_dir.join(STATS_DIRNAME)
}

pub fn cache_path(plugin_id: &str, data_dir: &Path) -> PathBuf {
    stats_dir(data_dir).join(format!("{plugin_id}.json"))
}

fn enabled_plugin_ids(plugins_dir: &Path) -> HashSet<String> {
    if !plugins_dir.is_dir() {
        return HashSet::new();
    }
    list_plugins(plugins_dir)
        .into_iter()
        .filter(|m| {
            m.get("valid").and_then(Value::as_bool).unwrap_or(false)
                && m.get("enabled").and_then(Value::as_bool).unwrap_or(false)
        })
        .filter_map(|m| m.get("id").and_then(Value::as_str).map(str::to_owned))
        .collect()
}

pub fn parse_timestamp(value: &str) -> Option<Timestamp> {
    let trimmed = value.trim();
    if trimmed.is_empty() || trimmed.len() > 64 {
        return None;
    }
    if let Ok(ts) = trimmed.parse::<Timestamp>() {
        return Some(ts);
    }
    if let Ok(dt) = trimmed.parse::<jiff::civil::DateTime>()
        && let Ok(ts) = jiff::tz::TimeZone::UTC.to_timestamp(dt)
    {
        return Some(ts);
    }
    None
}

fn bounded_int(val: Option<&Value>, low: i64, high: i64, label: &str) -> Result<i64, String> {
    let Some(v) = val else {
        return Err(format!("{label} must be an integer."));
    };
    if v.is_boolean() {
        return Err(format!("{label} must be an integer."));
    }
    let Some(n) = v.as_i64() else {
        return Err(format!("{label} must be an integer."));
    };
    if n < low || n > high {
        return Err(format!("{label} is out of range."));
    }
    Ok(n)
}

fn source_slug(value: &str) -> String {
    let lowered = value.trim().to_lowercase();
    let replaced = SOURCE_RE.replace_all(&lowered, "-");
    replaced.trim_matches('-').chars().take(64).collect()
}

fn clean_item(raw: &Value) -> Option<Value> {
    let obj = raw.as_object()?;
    let item_id = match obj.get("id") {
        Some(Value::String(s)) if !s.trim().is_empty() => {
            s.trim().chars().take(160).collect::<String>()
        }
        Some(Value::Number(n)) => n
            .as_i64()
            .or_else(|| n.as_f64().map(|f| f as i64))?
            .to_string(),
        _ => return None,
    };
    let title = obj.get("title").and_then(Value::as_str)?.trim();
    if title.is_empty() {
        return None;
    }
    let mut item = Map::new();
    item.insert("id".to_owned(), Value::String(item_id));
    item.insert(
        "title".to_owned(),
        Value::String(title.chars().take(300).collect()),
    );
    item.insert(
        "earned".to_owned(),
        Value::Bool(obj.get("earned").and_then(Value::as_bool).unwrap_or(false)),
    );
    if let Some(desc) = obj.get("description").and_then(Value::as_str) {
        let trimmed = desc.trim();
        if !trimmed.is_empty() {
            item.insert(
                "description".to_owned(),
                Value::String(trimmed.chars().take(MAX_TEXT).collect()),
            );
        }
    }
    if let Some(pts) = obj.get("points")
        && let Some(n) = pts.as_i64().or_else(|| pts.as_f64().map(|f| f as i64))
    {
        item.insert("points".to_owned(), json!(n.clamp(0, 100_000)));
    }
    if let Some(icon) = obj.get("icon_url").and_then(Value::as_str) {
        let trimmed = icon.trim();
        if trimmed.starts_with("https://") && trimmed.len() <= 2000 {
            item.insert("icon_url".to_owned(), Value::String(trimmed.to_owned()));
        }
    }
    Some(Value::Object(item))
}

fn clean_source(raw: &Value, plugin_id: &str) -> Result<Option<(String, Value)>, String> {
    let Some(obj) = raw.as_object() else {
        return Ok(None);
    };
    let raw_slug = obj
        .get("source")
        .or_else(|| obj.get("id"))
        .and_then(Value::as_str)
        .unwrap_or("");
    let slug = source_slug(raw_slug);
    if slug.is_empty() {
        return Ok(None);
    }
    if RESERVED_SOURCES.contains(&slug.as_str()) {
        return Err(format!(
            "Achievement source '{slug}' is reserved for the built-in provider."
        ));
    }
    let earned_v = obj.get("earned").cloned().unwrap_or_else(|| json!(0));
    let total_v = obj.get("total").cloned().unwrap_or_else(|| json!(0));
    let earned = bounded_int(Some(&earned_v), 0, MAX_TOTAL, "Achievement earned count")?;
    let total = bounded_int(Some(&total_v), 0, MAX_TOTAL, "Achievement total count")?;
    if earned > total {
        return Err("Achievement earned count cannot exceed the total.".to_owned());
    }
    if total == 0 {
        return Ok(None);
    }
    let items: Vec<Value> = obj
        .get("items")
        .and_then(Value::as_array)
        .map(|a| a.iter().take(MAX_ITEMS).filter_map(clean_item).collect())
        .unwrap_or_default();
    let name = obj
        .get("name")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .unwrap_or(&slug)
        .chars()
        .take(80)
        .collect::<String>();
    let pct = ((earned as f64 / total as f64) * 1000.0).round() / 10.0;
    let summary = json!({
        "name": name,
        "earned": earned,
        "total": total,
        "progress_pct": pct,
        "items": items,
    });
    let key: String = format!("plugin:{plugin_id}:{slug}")
        .chars()
        .take(160)
        .collect();
    Ok(Some((key, summary)))
}

fn clean_achievements(raw: &Value, plugin_id: &str) -> Result<Map<String, Value>, String> {
    let values: Vec<&Value> = match raw {
        Value::Array(a) => a.iter().take(8).collect(),
        other => vec![other],
    };
    let mut sources = Map::new();
    for val in values {
        if let Some((k, v)) = clean_source(val, plugin_id)? {
            sources.insert(k, v);
        }
    }
    Ok(sources)
}

fn game_rows(state: &Value) -> (Vec<Value>, HashSet<String>) {
    let mut rows = Vec::new();
    let mut known = HashSet::new();
    let Some(games) = state.get("games").and_then(Value::as_array) else {
        return (rows, known);
    };
    for game in games {
        let Some(obj) = game.as_object() else {
            continue;
        };
        let game_id = obj
            .get("game_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .chars()
            .take(160)
            .collect::<String>();
        if game_id.is_empty() {
            continue;
        }
        known.insert(game_id.clone());
        let mut row = Map::new();
        row.insert("game_id".to_owned(), Value::String(game_id));
        row.insert(
            "name".to_owned(),
            Value::String(
                obj.get("name")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .chars()
                    .take(300)
                    .collect(),
            ),
        );
        row.insert(
            "platform".to_owned(),
            Value::String(
                obj.get("platform")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .chars()
                    .take(80)
                    .collect(),
            ),
        );
        for key in ["steam_app_id", "gog_id", "ra_game_id", "source"] {
            if let Some(val) = obj.get(key)
                && !val.is_null()
                && val.as_str() != Some("")
            {
                let s = match val {
                    Value::String(st) => st.clone(),
                    other => other.to_string(),
                };
                row.insert(key.to_owned(), Value::String(s.chars().take(120).collect()));
            }
        }
        if let Some(pt) = obj.get("playtime_seconds")
            && let Some(n) = pt.as_i64().or_else(|| pt.as_f64().map(|f| f as i64))
        {
            row.insert("playtime_seconds".to_owned(), json!(n));
        }
        rows.push(Value::Object(row));
        if rows.len() >= MAX_GAMES {
            break;
        }
    }
    (rows, known)
}

fn trim_items(collection: &mut Map<String, Value>, budget: usize) {
    let mut remaining = budget;
    for entry in collection.values_mut() {
        let Some(sources) = entry.get_mut("sources").and_then(Value::as_object_mut) else {
            continue;
        };
        for summary in sources.values_mut() {
            let Some(items) = summary.get_mut("items").and_then(Value::as_array_mut) else {
                continue;
            };
            if remaining == 0 {
                items.clear();
            } else if items.len() > remaining {
                items.truncate(remaining);
                remaining = 0;
            } else {
                remaining -= items.len();
            }
        }
    }
}

fn bounded_payload(payload: &mut Value) -> Result<String, String> {
    let encoded = serde_json::to_string(payload).map_err(|e| e.to_string())?;
    if encoded.len() <= MAX_CACHE_BYTES {
        return Ok(encoded);
    }
    if let Some(games) = payload.get_mut("games").and_then(Value::as_object_mut) {
        for entry in games.values_mut() {
            if let Some(sources) = entry.get_mut("sources").and_then(Value::as_object_mut) {
                for summary in sources.values_mut() {
                    if let Some(items) = summary.get_mut("items").and_then(Value::as_array_mut) {
                        items.clear();
                    }
                }
            }
        }
    }
    let encoded = serde_json::to_string(payload).map_err(|e| e.to_string())?;
    if encoded.len() <= MAX_CACHE_BYTES {
        return Ok(encoded);
    }
    Err("Plugin stats cache is too large.".to_owned())
}

pub fn refresh_plugin_stats(
    plugin_id: &str,
    state: &Value,
    data_dir: &Path,
    plugins_dir: Option<&Path>,
    timeout: Option<u64>,
    metadata_db: Option<&Path>,
    app_root: Option<&Path>,
) -> Result<Vec<Value>, String> {
    let owned_plugins = data_dir.join("plugins");
    let plugin_root = plugins_dir.unwrap_or(&owned_plugins);
    let manifest = plugin_manifest(plugin_root, plugin_id)?;
    require_plugin_capability(&manifest, "stats")?;
    require_network_grant(&manifest, plugin_root)?;
    let mid = manifest["id"].as_str().unwrap_or(plugin_id);
    let (rows, known) = game_rows(state);
    let payload = json!({"action": "fetch", "games": rows});
    let (res_opt, err) = run_plugin_hook(
        plugin_root,
        mid,
        "stats",
        &payload,
        timeout,
        Some(state),
        metadata_db,
        app_root,
    );
    if !err.is_empty() {
        return Err(format!("Plugin stats failed: {err}"));
    }
    let Some(res) = res_opt else {
        return Err("Plugin stats must return a JSON object.".to_owned());
    };
    let Some(game_rows_out) = res.get("games").and_then(Value::as_array) else {
        return Err("Plugin stats must return {games: [...]}.".to_owned());
    };

    let mut collection = Map::new();
    let mut source_games: BTreeMap<String, HashSet<String>> = BTreeMap::new();
    let mut source_names: HashMap<String, String> = HashMap::new();

    for raw in game_rows_out.iter().take(MAX_GAMES) {
        let Some(obj) = raw.as_object() else {
            continue;
        };
        let game_id = obj
            .get("game_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_owned();
        if game_id.is_empty() || !known.contains(&game_id) {
            continue;
        }
        let playtime = match obj.get("playtime_seconds") {
            Some(v) if !v.is_null() => Some(bounded_int(Some(v), 0, MAX_PLAYTIME, "Playtime")?),
            _ => None,
        };
        let last_played = match obj.get("last_played") {
            Some(Value::Null) | None => String::new(),
            Some(Value::String(s)) if s.is_empty() => String::new(),
            Some(Value::String(s)) => {
                if parse_timestamp(s).is_none() {
                    return Err("last_played must be an ISO-8601 timestamp.".to_owned());
                }
                s.trim().chars().take(64).collect()
            }
            Some(_) => return Err("last_played must be an ISO-8601 timestamp.".to_owned()),
        };
        let sources = match obj.get("achievements") {
            Some(v) if !v.is_null() => clean_achievements(v, mid)?,
            _ => Map::new(),
        };
        for (k, summary) in &sources {
            source_games
                .entry(k.clone())
                .or_default()
                .insert(game_id.clone());
            source_names.insert(
                k.clone(),
                summary
                    .get("name")
                    .and_then(Value::as_str)
                    .unwrap_or(k)
                    .to_owned(),
            );
        }

        let entry = collection
            .entry(game_id)
            .or_insert_with(|| Value::Object(Map::new()))
            .as_object_mut()
            .unwrap();
        if let Some(pt) = playtime {
            let cur = entry
                .get("playtime_seconds")
                .and_then(Value::as_i64)
                .unwrap_or(0);
            entry.insert("playtime_seconds".to_owned(), json!(cur.max(pt)));
        }
        if !last_played.is_empty() {
            let existing_ts = entry
                .get("last_played")
                .and_then(Value::as_str)
                .and_then(parse_timestamp);
            let cand_ts = parse_timestamp(&last_played);
            if existing_ts.is_none() || (cand_ts.is_some() && cand_ts > existing_ts) {
                entry.insert("last_played".to_owned(), Value::String(last_played));
            }
        }
        if !sources.is_empty() {
            let s_obj = entry
                .entry("sources")
                .or_insert_with(|| Value::Object(Map::new()))
                .as_object_mut()
                .unwrap();
            for (k, v) in sources {
                s_obj.insert(k, v);
            }
        }
    }

    let updated_at = Timestamp::now()
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
        .to_string();
    let mut source_counts = Map::new();
    for (k, ids) in &source_games {
        let name = source_names.get(k).cloned().unwrap_or_else(|| k.clone());
        source_counts.insert(k.clone(), json!({"name": name, "games": ids.len()}));
    }
    trim_items(&mut collection, MAX_ITEMS_TOTAL);
    let mut cache_doc = json!({
        "plugin_id": mid,
        "updated_at": updated_at,
        "sources": source_counts,
        "games": collection,
    });
    let encoded = bounded_payload(&mut cache_doc)?;
    atomic_write_text(&cache_path(mid, data_dir), &encoded, 0o600).map_err(|e| e.to_string())?;

    let mut out = Vec::new();
    for (k, ids) in source_games {
        let name = source_names.get(&k).cloned().unwrap_or_else(|| k.clone());
        out.push(json!({
            "source": k,
            "name": name,
            "games": ids.len(),
            "updated_at": updated_at,
        }));
    }
    Ok(out)
}

fn cached_summary(raw: &Value) -> Option<Value> {
    let obj = raw.as_object()?;
    let name = obj.get("name").and_then(Value::as_str)?;
    let earned = obj.get("earned").and_then(Value::as_i64)?;
    let total = obj.get("total").and_then(Value::as_i64)?;
    if earned < 0 || earned > total || total > MAX_TOTAL || total == 0 {
        return None;
    }
    let items: Vec<Value> = obj
        .get("items")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .take(MAX_ITEMS)
                .filter(|v| v.is_object())
                .cloned()
                .collect()
        })
        .unwrap_or_default();
    let pct = ((earned as f64 / total as f64) * 1000.0).round() / 10.0;
    Some(json!({
        "name": name.chars().take(80).collect::<String>(),
        "earned": earned,
        "total": total,
        "progress_pct": pct,
        "items": items,
    }))
}

pub fn load_plugin_stats(data_dir: &Path) -> Map<String, Value> {
    let mut merged: Map<String, Value> = Map::new();
    let root = stats_dir(data_dir);
    if !root.is_dir() {
        return merged;
    }
    let enabled = enabled_plugin_ids(&data_dir.join("plugins"));
    let mut files: Vec<PathBuf> = fs::read_dir(&root)
        .ok()
        .into_iter()
        .flatten()
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().and_then(|e| e.to_str()) == Some("json"))
        .collect();
    files.sort();

    for path in files {
        let Ok(meta) = fs::metadata(&path) else {
            continue;
        };
        if meta.len() as usize > MAX_CACHE_BYTES {
            continue;
        }
        let Ok(raw_text) = fs::read_to_string(&path) else {
            continue;
        };
        let Ok(raw) = serde_json::from_str::<Value>(&raw_text) else {
            continue;
        };
        let Some(plugin_id) = raw.get("plugin_id").and_then(Value::as_str) else {
            continue;
        };
        if !enabled.contains(plugin_id) {
            continue;
        }
        let Some(games) = raw.get("games").and_then(Value::as_object) else {
            continue;
        };
        let prefix = format!("plugin:{plugin_id}:");
        for (game_id, entry) in games {
            let Some(eobj) = entry.as_object() else {
                continue;
            };
            let proj = merged
                .entry(game_id.clone())
                .or_insert_with(|| {
                    json!({
                        "playtime_seconds": 0,
                        "last_played": "",
                        "sources": {},
                    })
                })
                .as_object_mut()
                .unwrap();
            if let Some(pt) = eobj.get("playtime_seconds").and_then(Value::as_i64)
                && pt >= 0
            {
                let cur = proj
                    .get("playtime_seconds")
                    .and_then(Value::as_i64)
                    .unwrap_or(0);
                proj.insert("playtime_seconds".to_owned(), json!(cur.max(pt)));
            }
            if let Some(lp) = eobj.get("last_played").and_then(Value::as_str)
                && let Some(cand_ts) = parse_timestamp(lp)
            {
                let cur_ts = proj
                    .get("last_played")
                    .and_then(Value::as_str)
                    .and_then(parse_timestamp);
                if cur_ts.is_none() || Some(cand_ts) > cur_ts {
                    proj.insert(
                        "last_played".to_owned(),
                        Value::String(lp.chars().take(64).collect()),
                    );
                }
            }
            if let Some(sources) = eobj.get("sources").and_then(Value::as_object) {
                let target_sources = proj
                    .entry("sources")
                    .or_insert_with(|| Value::Object(Map::new()))
                    .as_object_mut()
                    .unwrap();
                for (k, v) in sources {
                    if !k.starts_with(&prefix) {
                        continue;
                    }
                    if let Some(summary) = cached_summary(v) {
                        target_sources.insert(k.clone(), summary);
                    }
                }
            }
        }
    }
    merged
}

pub fn plugin_stats_summaries(data_dir: &Path) -> Vec<Value> {
    let mut rows = Vec::new();
    let root = stats_dir(data_dir);
    if !root.is_dir() {
        return rows;
    }
    let enabled = enabled_plugin_ids(&data_dir.join("plugins"));
    let mut files: Vec<PathBuf> = fs::read_dir(&root)
        .ok()
        .into_iter()
        .flatten()
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().and_then(|e| e.to_str()) == Some("json"))
        .collect();
    files.sort();

    for path in files {
        let Ok(meta) = fs::metadata(&path) else {
            continue;
        };
        if meta.len() as usize > MAX_CACHE_BYTES {
            continue;
        }
        let Ok(raw_text) = fs::read_to_string(&path) else {
            continue;
        };
        let Ok(raw) = serde_json::from_str::<Value>(&raw_text) else {
            continue;
        };
        let Some(plugin_id) = raw.get("plugin_id").and_then(Value::as_str) else {
            continue;
        };
        if !enabled.contains(plugin_id) {
            continue;
        }
        let Some(sources) = raw.get("sources").and_then(Value::as_object) else {
            continue;
        };
        let updated_at = raw.get("updated_at").and_then(Value::as_str).unwrap_or("");
        let mut keys: Vec<&String> = sources.keys().collect();
        keys.sort();
        for key in keys {
            if !key.starts_with("plugin:") {
                continue;
            }
            let Some(val) = sources.get(key).and_then(Value::as_object) else {
                continue;
            };
            let games = val.get("games").and_then(Value::as_i64).unwrap_or(0).max(0);
            let name = val
                .get("name")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .unwrap_or(key)
                .chars()
                .take(80)
                .collect::<String>();
            rows.push(json!({
                "source": key,
                "name": name,
                "games": games,
                "updated_at": updated_at,
            }));
        }
    }
    rows
}
