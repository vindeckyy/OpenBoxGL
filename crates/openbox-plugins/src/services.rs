//! Read-only host services for capability plugin hooks (`pkg/parity/parity_plugin_services.py`).

use std::collections::HashSet;
use std::net::{IpAddr, ToSocketAddrs};
use std::path::Path;

use openbox_io::is_public_ip;
use openbox_metadata::lbdb::search_games as lbdb_search_games;
use openbox_metadata::providers::search_all as metadata_search_all;
use openbox_sync::household::compute_leaderboard;
use serde_json::{Map, Value, json};
use url::Url;

use crate::host::capability_media_kinds;

pub const MAX_LIBRARY_GAMES: usize = 500;
pub const MAX_ACHIEVEMENT_GAMES: usize = 100;
pub const MAX_ACHIEVEMENT_ITEMS: usize = 500;
pub const MAX_ACTIVITY: usize = 100;
pub const MAX_LEADERBOARD: usize = 100;
pub const MAX_CATALOG: usize = 20;
pub const MAX_CATALOG_DEFAULT: usize = 10;
pub const MAX_METADATA: usize = 10;
pub const MAX_METADATA_DEFAULT: usize = 5;
pub const MAX_MEDIA: usize = 20;
pub const MAX_MEDIA_DEFAULT: usize = 10;
pub const MAX_URL_LENGTH: usize = 2000;

const PATH_KEYS: &[&str] = &[
    "path",
    "paths",
    "file",
    "files",
    "install_dir",
    "launch",
    "save_paths",
    "save_sources",
    "media_root",
    "screenshot_paths",
];

pub const SERVICES: &[&str] = &[
    "library.query",
    "achievements",
    "profile",
    "activity",
    "leaderboards",
    "catalog.search",
    "metadata.search",
    "media.search",
];

pub fn describe_services() -> Value {
    json!({
        "library.query": {"version": 1, "max_results": MAX_LIBRARY_GAMES, "read_only": true},
        "achievements": {"version": 1, "max_results": MAX_ACHIEVEMENT_ITEMS, "read_only": true},
        "profile": {"version": 1, "max_results": 1, "read_only": true},
        "activity": {"version": 1, "max_results": MAX_ACTIVITY, "read_only": true},
        "leaderboards": {"version": 1, "max_results": MAX_LEADERBOARD, "read_only": true},
        "catalog.search": {"version": 1, "max_results": MAX_CATALOG, "read_only": true},
        "metadata.search": {"version": 1, "max_results": MAX_METADATA, "read_only": true},
        "media.search": {"version": 1, "max_results": MAX_MEDIA, "read_only": true},
    })
}

fn strip_paths(value: Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut out = Map::new();
            for (k, v) in map {
                if PATH_KEYS.contains(&k.as_str()) {
                    continue;
                }
                out.insert(k, strip_paths(v));
            }
            Value::Object(out)
        }
        Value::Array(arr) => Value::Array(arr.into_iter().map(strip_paths).collect()),
        other => other,
    }
}

fn int_param(
    params: &Map<String, Value>,
    key: &str,
    default: usize,
    maximum: usize,
    minimum: usize,
) -> Result<usize, String> {
    let Some(val) = params.get(key) else {
        return Ok(default);
    };
    if val.is_boolean() {
        return Err(format!("{key} must be a whole number."));
    }
    let n = match val {
        Value::Number(num) => num
            .as_i64()
            .or_else(|| num.as_f64().map(|f| f as i64))
            .ok_or_else(|| format!("{key} must be a whole number."))?,
        Value::String(s) => s
            .trim()
            .parse::<i64>()
            .map_err(|_| format!("{key} must be a whole number."))?,
        _ => return Err(format!("{key} must be a whole number.")),
    };
    let clamped = (n.max(minimum as i64) as usize).min(maximum);
    Ok(clamped)
}

fn str_param(params: &Map<String, Value>, key: &str, maximum: usize) -> Result<String, String> {
    let Some(val) = params.get(key) else {
        return Ok(String::new());
    };
    if val.is_null() {
        return Ok(String::new());
    }
    let Some(s) = val.as_str() else {
        return Err(format!("{key} must be text."));
    };
    Ok(s.trim().chars().take(maximum).collect())
}

fn safe_i64(val: Option<&Value>) -> i64 {
    match val {
        Some(Value::Number(n)) => n
            .as_i64()
            .or_else(|| n.as_f64().map(|f| f as i64))
            .unwrap_or(0),
        Some(Value::String(s)) => s.trim().parse::<i64>().unwrap_or(0),
        _ => 0,
    }
}

fn safe_f64(val: Option<&Value>) -> f64 {
    match val {
        Some(Value::Number(n)) => n.as_f64().unwrap_or(0.0),
        Some(Value::String(s)) => s.trim().parse::<f64>().unwrap_or(0.0),
        _ => 0.0,
    }
}

fn games_from_state(state: &Value) -> Vec<&Map<String, Value>> {
    state
        .get("games")
        .and_then(Value::as_array)
        .map(|a| a.iter().filter_map(Value::as_object).collect())
        .unwrap_or_default()
}

fn achievement_sources(game: &Map<String, Value>) -> Map<String, Value> {
    if let Some(sources) = game.get("achievement_sources").and_then(Value::as_object)
        && !sources.is_empty()
    {
        return sources.clone();
    }
    let total = safe_i64(game.get("ra_achievements_total"));
    if total <= 0 {
        return Map::new();
    }
    let mut out = Map::new();
    out.insert(
        "retroachievements".to_owned(),
        json!({
            "name": "RetroAchievements",
            "earned": safe_i64(game.get("ra_achievements_earned")),
            "total": total,
            "progress_pct": safe_f64(game.get("ra_progress_pct")),
            "mastered": game.get("ra_mastered").and_then(Value::as_bool).unwrap_or(false),
            "items": [],
        }),
    );
    out
}

fn clean_achievement_item(item: &Value) -> Option<Value> {
    let obj = item.as_object()?;
    let mut clean = Map::new();
    for key in ["id", "title", "description", "points", "earned", "icon_url"] {
        if let Some(v) = obj.get(key)
            && (v.is_string() || v.is_number() || v.is_boolean())
        {
            clean.insert(key.to_owned(), v.clone());
        }
    }
    if clean.is_empty() {
        None
    } else {
        Some(Value::Object(clean))
    }
}

pub fn safe_media_url(raw_url: &str) -> Result<String, String> {
    let value = raw_url.trim();
    if value.is_empty() || value.len() > MAX_URL_LENGTH {
        return Err("Media URL is empty or too long.".to_owned());
    }
    let parsed = Url::parse(value).map_err(|_| "Media URLs must use HTTPS.".to_owned())?;
    if parsed.scheme() != "https" {
        return Err("Media URLs must use HTTPS.".to_owned());
    }
    if !parsed.username().is_empty() || parsed.password().is_some() {
        return Err("Media URLs may not carry credentials.".to_owned());
    }
    let Some(host) = parsed.host_str() else {
        return Err("Media URL needs a host.".to_owned());
    };
    let port = parsed.port_or_known_default().unwrap_or(443);
    let addrs: Vec<IpAddr> = (host, port)
        .to_socket_addrs()
        .map_err(|_| "Media host could not be resolved.".to_owned())?
        .map(|sa| sa.ip())
        .collect();
    if addrs.is_empty() {
        return Err("Media host could not be resolved.".to_owned());
    }
    for ip in addrs {
        if !is_public_ip(ip) {
            return Err("Media host resolves to a non-public address.".to_owned());
        }
    }
    Ok(value.to_owned())
}

fn svc_library_query(params: &Map<String, Value>, state: &Value) -> Result<Value, String> {
    let limit = int_param(params, "limit", MAX_LIBRARY_GAMES, MAX_LIBRARY_GAMES, 0)?;
    let query = str_param(params, "query", 200)?.to_lowercase();
    let platform = str_param(params, "platform", 80)?.to_lowercase();
    let mut rows = Vec::new();
    for game in games_from_state(state) {
        let name = game.get("name").and_then(Value::as_str).unwrap_or("");
        if !query.is_empty() && !name.to_lowercase().contains(&query) {
            continue;
        }
        let gplat = game.get("platform").and_then(Value::as_str).unwrap_or("");
        if !platform.is_empty() && gplat.to_lowercase() != platform {
            continue;
        }
        rows.push(json!({
            "game_id": game.get("game_id").and_then(Value::as_str).unwrap_or(""),
            "name": name,
            "platform": gplat,
            "progress": game.get("progress").and_then(Value::as_str).unwrap_or(""),
            "favorite": game.get("favorite").and_then(Value::as_bool).unwrap_or(false),
            "playtime_seconds": safe_i64(game.get("playtime_seconds")),
            "last_played": game.get("last_played").and_then(Value::as_str).unwrap_or(""),
            "source": game.get("source").and_then(Value::as_str).unwrap_or(""),
            "ra_progress_pct": safe_f64(game.get("ra_progress_pct")),
        }));
        if rows.len() > limit {
            break;
        }
    }
    let truncated = rows.len() > limit;
    rows.truncate(limit);
    Ok(json!({"games": rows, "truncated": truncated}))
}

fn svc_achievements(params: &Map<String, Value>, state: &Value) -> Result<Value, String> {
    let Some(arr) = params.get("game_ids").and_then(Value::as_array) else {
        return Err("game_ids must be a list of at most 100 ids.".to_owned());
    };
    if arr.len() > MAX_ACHIEVEMENT_GAMES {
        return Err("game_ids must be a list of at most 100 ids.".to_owned());
    }
    let wanted: HashSet<&str> = arr
        .iter()
        .filter_map(Value::as_str)
        .filter(|s| !s.is_empty())
        .collect();
    let mut games = Vec::new();
    for game in games_from_state(state) {
        let game_id = game.get("game_id").and_then(Value::as_str).unwrap_or("");
        if !wanted.contains(game_id) {
            continue;
        }
        let mut sources = Map::new();
        for (name, entry) in achievement_sources(game) {
            let Some(eobj) = entry.as_object() else {
                continue;
            };
            let mut clean = Map::new();
            for key in ["name", "earned", "total", "progress_pct", "mastered"] {
                if let Some(v) = eobj.get(key)
                    && (v.is_string() || v.is_number() || v.is_boolean())
                {
                    clean.insert(key.to_owned(), v.clone());
                }
            }
            let raw_items = eobj
                .get("items")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            let items: Vec<Value> = raw_items
                .iter()
                .take(MAX_ACHIEVEMENT_ITEMS)
                .filter_map(clean_achievement_item)
                .collect();
            clean.insert("items".to_owned(), Value::Array(items));
            if raw_items.len() > MAX_ACHIEVEMENT_ITEMS {
                clean.insert("truncated".to_owned(), Value::Bool(true));
            }
            sources.insert(name, Value::Object(clean));
        }
        games.push(json!({
            "game_id": game_id,
            "achievement_sources": sources,
        }));
    }
    Ok(json!({"games": games, "truncated": false}))
}

fn svc_profile(state: &Value) -> Value {
    let games = games_from_state(state);
    let mut playtime = 0i64;
    let mut earned = 0i64;
    let mut total = 0i64;
    let mut mastered = 0usize;
    let mut favorites = 0usize;
    for game in &games {
        playtime += safe_i64(game.get("playtime_seconds")).max(0);
        if game
            .get("favorite")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            favorites += 1;
        }
        let mut game_mastered = game
            .get("ra_mastered")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        for entry in achievement_sources(game).values() {
            let Some(eobj) = entry.as_object() else {
                continue;
            };
            earned += safe_i64(eobj.get("earned")).max(0);
            total += safe_i64(eobj.get("total")).max(0);
            if eobj
                .get("mastered")
                .and_then(Value::as_bool)
                .unwrap_or(false)
            {
                game_mastered = true;
            }
        }
        if game_mastered {
            mastered += 1;
        }
    }
    json!({
        "games": games.len(),
        "playtime_seconds": playtime,
        "achievements_earned": earned,
        "achievements_total": total,
        "mastered": mastered,
        "favorite_count": favorites,
    })
}

fn svc_activity(state: &Value) -> Value {
    let history = state
        .get("history")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let total_len = history.len();
    let mut entries = Vec::new();
    for entry in history.iter().rev() {
        let Some(obj) = entry.as_object() else {
            continue;
        };
        let name = obj
            .get("game")
            .or_else(|| obj.get("name"))
            .and_then(Value::as_str)
            .unwrap_or("");
        entries.push(json!({
            "game_id": obj.get("game_id").and_then(Value::as_str).unwrap_or(""),
            "name": name,
            "started": obj.get("started").and_then(Value::as_str).unwrap_or(""),
            "seconds": safe_i64(obj.get("seconds")),
            "exit_code": safe_i64(obj.get("exit_code")),
        }));
        if entries.len() >= MAX_ACTIVITY {
            break;
        }
    }
    json!({"entries": entries, "truncated": total_len > MAX_ACTIVITY})
}

fn svc_leaderboards(params: &Map<String, Value>, state: &Value) -> Result<Value, String> {
    let limit = int_param(params, "limit", MAX_LEADERBOARD, MAX_LEADERBOARD, 0)?;
    let board = compute_leaderboard(state, None, limit.max(1))
        .map_err(|e| format!("Leaderboard is unavailable: {e}"))?;
    let available = board
        .get("available")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let raw_entries = board
        .get("entries")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let truncated = raw_entries.len() > limit;
    let entries: Vec<Value> = raw_entries.into_iter().take(limit).collect();
    Ok(json!({
        "available": available,
        "entries": entries,
        "truncated": truncated,
    }))
}

fn svc_catalog_search(
    params: &Map<String, Value>,
    metadata_db: Option<&Path>,
) -> Result<Value, String> {
    let query = str_param(params, "query", 200)?;
    if query.is_empty() {
        return Err("catalog.search needs a query.".to_owned());
    }
    let platform = str_param(params, "platform", 80)?;
    let limit = int_param(params, "limit", MAX_CATALOG_DEFAULT, MAX_CATALOG, 0)?;
    let Some(db) = metadata_db.filter(|p| p.is_file()) else {
        return Err("Metadata database is not available.".to_owned());
    };
    let results = lbdb_search_games(db, &query, &platform, limit.max(1))
        .map_err(|e| format!("Catalog search failed: {e}"))?;
    let truncated = results.len() > limit;
    let mut candidates = Vec::new();
    for item in results.into_iter().take(limit) {
        let Some(obj) = item.as_object() else {
            continue;
        };
        let mut row = Map::new();
        for key in [
            "database_id",
            "name",
            "platform",
            "year",
            "developer",
            "publisher",
            "genre",
            "overview",
            "image_url",
        ] {
            if let Some(v) = obj.get(key) {
                row.insert(key.to_owned(), v.clone());
            }
        }
        candidates.push(Value::Object(row));
    }
    Ok(json!({"candidates": candidates, "truncated": truncated}))
}

fn svc_metadata_search(
    params: &Map<String, Value>,
    state: &Value,
    metadata_db: Option<&Path>,
) -> Result<Value, String> {
    let limit = int_param(params, "limit", MAX_METADATA_DEFAULT, MAX_METADATA, 0)?;
    let game = match params.get("game") {
        Some(v) if !v.is_null() => {
            if !v.is_object() {
                return Err("game must be an object.".to_owned());
            }
            v.clone()
        }
        _ => {
            let query = str_param(params, "query", 200)?;
            if query.is_empty() {
                return Err("metadata.search needs a game or query.".to_owned());
            }
            json!({"name": query})
        }
    };
    let fallback = Path::new("/nonexistent-launchbox.db");
    let db = metadata_db.unwrap_or(fallback);
    let results = metadata_search_all(state, db, &game, None, limit.max(1));
    let truncated = results.len() > limit;
    let mut candidates = Vec::new();
    for cand in results.into_iter().take(limit) {
        let Some(obj) = cand.as_object() else {
            continue;
        };
        let mut clean_media = Map::new();
        if let Some(mobj) = obj.get("media").and_then(Value::as_object) {
            for (kind, urls) in mobj {
                if let Some(uarr) = urls.as_array() {
                    let list: Vec<Value> = uarr
                        .iter()
                        .filter(|v| v.is_string())
                        .take(10)
                        .cloned()
                        .collect();
                    clean_media.insert(kind.clone(), Value::Array(list));
                }
            }
        }
        candidates.push(json!({
            "id": obj.get("id").cloned().unwrap_or(Value::Null),
            "source": obj.get("source").cloned().unwrap_or(Value::Null),
            "name": obj.get("name").cloned().unwrap_or(Value::Null),
            "platform": obj.get("platform").cloned().unwrap_or(Value::Null),
            "year": obj.get("year").cloned().unwrap_or(Value::Null),
            "fields": obj.get("fields").filter(|v| v.is_object()).cloned().unwrap_or_else(|| json!({})),
            "media": clean_media,
        }));
    }
    Ok(json!({"candidates": candidates, "truncated": truncated}))
}

fn svc_media_search(
    params: &Map<String, Value>,
    state: &Value,
    metadata_db: Option<&Path>,
) -> Result<Value, String> {
    let game_id = str_param(params, "game_id", 160)?;
    if game_id.is_empty() {
        return Err("media.search needs a game_id.".to_owned());
    }
    let mut kind = str_param(params, "kind", 40)?;
    if kind.is_empty() {
        kind = "cover".to_owned();
    }
    let allowed = capability_media_kinds();
    if !allowed.contains(kind.as_str()) {
        return Err(format!("Unsupported media kind: '{kind}'."));
    }
    let limit = int_param(params, "limit", MAX_MEDIA_DEFAULT, MAX_MEDIA, 0)?;
    let Some(game) = games_from_state(state)
        .into_iter()
        .find(|g| g.get("game_id").and_then(Value::as_str) == Some(game_id.as_str()))
    else {
        return Err("Unknown game_id.".to_owned());
    };
    let fallback = Path::new("/nonexistent-launchbox.db");
    let db = metadata_db.unwrap_or(fallback);
    let results = metadata_search_all(state, db, &Value::Object(game.clone()), None, limit.max(1));
    let mut urls = Vec::new();
    for cand in results {
        let title = cand
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        if let Some(arr) = cand
            .get("media")
            .and_then(|m| m.get(&kind))
            .and_then(Value::as_array)
        {
            for u in arr {
                if let Some(s) = u.as_str() {
                    urls.push((s.to_owned(), title.clone()));
                }
            }
        }
    }
    let truncated = urls.len() > limit;
    let mut clean = Vec::new();
    for (u, title) in urls.into_iter().take(limit) {
        let Ok(safe) = safe_media_url(&u) else {
            continue;
        };
        clean.push(json!({"url": safe, "kind": kind, "title": title}));
    }
    Ok(json!({"candidates": clean, "truncated": truncated}))
}

pub fn handle_service(
    service: &str,
    params: Option<&Value>,
    state: &Value,
    version: Option<&Value>,
    metadata_db: Option<&Path>,
) -> Result<Value, String> {
    if !SERVICES.contains(&service) {
        return Err(format!("Unknown plugin service: '{service}'."));
    }
    if let Some(ver) = version
        && !ver.is_null()
    {
        let ver_str = match ver {
            Value::String(s) => s.clone(),
            other => other.to_string(),
        };
        if ver_str != "1" {
            return Err(format!(
                "Plugin service '{service}' has no version {ver_str}."
            ));
        }
    }
    let empty = Map::new();
    let pmap = match params {
        None | Some(Value::Null) => &empty,
        Some(Value::Object(m)) => m,
        Some(_) => return Err("Service parameters must be an object.".to_owned()),
    };
    let result = match service {
        "library.query" => svc_library_query(pmap, state)?,
        "achievements" => svc_achievements(pmap, state)?,
        "profile" => svc_profile(state),
        "activity" => svc_activity(state),
        "leaderboards" => svc_leaderboards(pmap, state)?,
        "catalog.search" => svc_catalog_search(pmap, metadata_db)?,
        "metadata.search" => svc_metadata_search(pmap, state, metadata_db)?,
        "media.search" => svc_media_search(pmap, state, metadata_db)?,
        _ => return Err(format!("Unknown plugin service: '{service}'.")),
    };
    if !result.is_object() {
        return Err(format!("Service {service} produced an invalid result."));
    }
    Ok(strip_paths(result))
}

pub fn execute_service_requests(
    requests: &Value,
    state: &Value,
    metadata_db: Option<&Path>,
) -> Result<Map<String, Value>, String> {
    let Some(arr) = requests.as_array() else {
        return Err("service_requests must be a list of at most 8 requests.".to_owned());
    };
    if arr.len() > 8 {
        return Err("service_requests must be a list of at most 8 requests.".to_owned());
    }
    let mut results = Map::new();
    for req in arr {
        let Some(obj) = req.as_object() else {
            return Err("Every service request must be an object.".to_owned());
        };
        let Some(req_id) = obj.get("id").and_then(Value::as_str) else {
            return Err("Service requests need a short id.".to_owned());
        };
        if req_id.is_empty() || req_id.len() > 64 {
            return Err("Service requests need a short id.".to_owned());
        }
        if results.contains_key(req_id) {
            return Err("Service request ids must be unique.".to_owned());
        }
        let Some(action) = obj.get("action").and_then(Value::as_str) else {
            return Err("Service requests need an action.".to_owned());
        };
        if action.is_empty() {
            return Err("Service requests need an action.".to_owned());
        }
        let params = obj.get("params");
        if let Some(p) = params
            && !p.is_object()
        {
            return Err("Service request params must be an object.".to_owned());
        }
        let version = obj.get("version");
        match handle_service(action, params, state, version, metadata_db) {
            Ok(val) => {
                results.insert(req_id.to_owned(), val);
            }
            Err(err) => {
                tracing::warn!("Plugin service {} failed: {}", action, err);
                let msg = if err.is_empty() {
                    "Service failed.".to_owned()
                } else {
                    err.chars().take(500).collect()
                };
                results.insert(req_id.to_owned(), json!({"error": msg}));
            }
        }
    }
    Ok(results)
}
