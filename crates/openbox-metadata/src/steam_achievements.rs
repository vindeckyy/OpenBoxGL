//! Steam achievements and playtime integration (`pkg/parity/parity_steam_achievements.py`).

use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use openbox_io::atomic_write_text;
use parking_lot::Mutex;
use serde_json::{Map, Value, json};

use crate::http::{http_get_raw, py_round, urlencode};

pub const STEAM_API_ROOT: &str = "https://api.steampowered.com";
pub const STEAM_CACHE_TTL_SECS: u64 = 24 * 3600;
pub const MAX_ACHIEVEMENT_ITEMS: usize = 500;
pub const MAX_OWNED_GAMES: usize = 10_000;
const MAX_RESPONSE_BYTES: usize = 8 * 1024 * 1024;
const MIN_REQUEST_INTERVAL: Duration = Duration::from_secs(1);
const RETRY_DELAYS_SECS: &[u64] = &[2, 8];

static REQUEST_LOCK: LazyLock<Mutex<Option<Instant>>> = LazyLock::new(|| Mutex::new(None));

#[derive(Debug, Clone, Default)]
pub struct SteamCredentials {
    pub api_key: String,
    pub steam_id: String,
}

fn steam_file(data_dir: &Path) -> PathBuf {
    data_dir.join("steam.json")
}

pub fn load_steam_credentials(data_dir: &Path) -> SteamCredentials {
    let mut creds = SteamCredentials::default();
    let path = steam_file(data_dir);
    if path.is_file()
        && let Ok(raw) = fs::read_to_string(&path)
        && let Ok(val) = serde_json::from_str::<Value>(&raw)
    {
        creds.api_key = val
            .get("api_key")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_owned();
        creds.steam_id = val
            .get("steam_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_owned();
    }
    if creds.api_key.is_empty() {
        creds.api_key = std::env::var("STEAM_API_KEY")
            .unwrap_or_default()
            .trim()
            .to_owned();
    }
    if creds.steam_id.is_empty() {
        creds.steam_id = std::env::var("STEAM_ID")
            .unwrap_or_default()
            .trim()
            .to_owned();
    }
    creds
}

pub fn credential_status(creds: &SteamCredentials) -> Value {
    json!({
        "configured": !creds.api_key.is_empty() && !creds.steam_id.is_empty(),
        "steam_id": creds.steam_id,
        "key_set": !creds.api_key.is_empty(),
    })
}

pub fn save_steam_credentials(
    data_dir: &Path,
    api_key: Option<&str>,
    steam_id: Option<&str>,
) -> Result<Value, String> {
    let existing = load_steam_credentials(data_dir);
    let updated = SteamCredentials {
        api_key: api_key
            .map(|s| s.trim().to_owned())
            .unwrap_or(existing.api_key),
        steam_id: steam_id
            .map(|s| s.trim().to_owned())
            .unwrap_or(existing.steam_id),
    };
    let path = steam_file(data_dir);
    if let Some(p) = path.parent() {
        fs::create_dir_all(p).map_err(|e| e.to_string())?;
    }
    let body = serde_json::to_string_pretty(&json!({
        "api_key": updated.api_key,
        "steam_id": updated.steam_id,
    }))
    .map_err(|e| e.to_string())?
        + "\n";
    atomic_write_text(&path, &body, 0o600).map_err(|e| e.to_string())?;
    Ok(credential_status(&updated))
}

fn validate_steam_id(value: &str) -> Result<String, String> {
    let text = value.trim();
    if text.is_empty() || text.len() > 20 || !text.bytes().all(|b| b.is_ascii_digit()) {
        return Err("A numeric Steam ID is required.".to_owned());
    }
    Ok(text.to_owned())
}

fn validate_app_id(value: &str) -> Result<u64, String> {
    let parsed = value
        .trim()
        .parse::<u64>()
        .map_err(|_| "A numeric Steam app id is required.".to_owned())?;
    if parsed == 0 {
        return Err("A numeric Steam app id is required.".to_owned());
    }
    Ok(parsed)
}

fn throttle() {
    let mut guard = REQUEST_LOCK.lock();
    if let Some(last) = *guard {
        let elapsed = last.elapsed();
        if elapsed < MIN_REQUEST_INTERVAL {
            std::thread::sleep(MIN_REQUEST_INTERVAL - elapsed);
        }
    }
    *guard = Some(Instant::now());
}

fn request_json(url: &str) -> Result<Value, String> {
    if !url.starts_with("https://") {
        return Err("Steam requests must stay on https.".to_owned());
    }
    let mut last_error = String::new();
    let retry_delays: Vec<Option<u64>> = RETRY_DELAYS_SECS
        .iter()
        .copied()
        .map(Some)
        .chain(std::iter::once(None))
        .collect();
    for delay_opt in retry_delays {
        throttle();
        match http_get_raw(
            url,
            &[("User-Agent", "OpenBox/steam-achievements".to_owned())],
            10,
            MAX_RESPONSE_BYTES,
        ) {
            Ok(resp) => {
                if matches!(resp.status, 429 | 500 | 502 | 503 | 504)
                    && let Some(delay) = delay_opt
                {
                    last_error = format!("HTTP {}", resp.status);
                    std::thread::sleep(Duration::from_secs(delay));
                    continue;
                }
                if resp.status >= 400 {
                    last_error = format!("HTTP {}", resp.status);
                    break;
                }
                let payload: Value = serde_json::from_slice(&resp.body)
                    .map_err(|_| "Steam returned an invalid response.".to_owned())?;
                if !payload.is_object() {
                    return Err("Steam returned an invalid response.".to_owned());
                }
                return Ok(payload);
            }
            Err(err) => {
                last_error = err;
                break;
            }
        }
    }
    Err(format!("Steam request failed: {last_error}"))
}

fn achievement_rows(payload: &Value) -> Vec<&Map<String, Value>> {
    if let Some(arr) = payload.as_array() {
        return arr.iter().filter_map(Value::as_object).collect();
    }
    let Some(obj) = payload.as_object() else {
        return Vec::new();
    };
    for container in ["game", "playerstats"] {
        if let Some(cand) = obj.get(container).and_then(Value::as_object) {
            if let Some(arr) = cand.get("achievements").and_then(Value::as_array) {
                return arr.iter().filter_map(Value::as_object).collect();
            }
            if let Some(arr) = cand
                .get("availableGameStats")
                .and_then(|n| n.get("achievements"))
                .and_then(Value::as_array)
            {
                return arr.iter().filter_map(Value::as_object).collect();
            }
        }
    }
    obj.get("achievements")
        .and_then(Value::as_array)
        .map(|a| a.iter().filter_map(Value::as_object).collect())
        .unwrap_or_default()
}

fn player_success(payload: &Value) -> bool {
    if let Some(obj) = payload.as_object() {
        if let Some(b) = obj
            .get("playerstats")
            .and_then(|s| s.get("success"))
            .and_then(Value::as_bool)
        {
            return b;
        }
        if let Some(b) = obj.get("success").and_then(Value::as_bool) {
            return b;
        }
    }
    true
}

pub fn fetch_schema(app_id: &str, api_key: &str) -> Result<Vec<Value>, String> {
    let aid = validate_app_id(app_id)?;
    if api_key.trim().is_empty() {
        return Err("Configure a Steam API key in Settings first.".to_owned());
    }
    let query = urlencode(&[
        ("key", api_key.trim().to_owned()),
        ("appid", aid.to_string()),
    ]);
    let url = format!("{STEAM_API_ROOT}/ISteamUserStats/GetSchemaForGame/v2/?{query}");
    let payload = request_json(&url)?;
    Ok(achievement_rows(&payload)
        .into_iter()
        .map(|row| {
            let apiname = row
                .get("name")
                .or_else(|| row.get("apiname"))
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim();
            let title = row
                .get("displayName")
                .or_else(|| row.get("title"))
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim();
            let hidden = match row.get("hidden") {
                Some(Value::Bool(b)) => *b,
                Some(Value::Number(n)) => n.as_i64().unwrap_or(0) != 0,
                _ => false,
            };
            json!({
                "apiname": apiname,
                "title": title,
                "description": row.get("description").and_then(Value::as_str).unwrap_or("").trim(),
                "hidden": hidden,
                "icon": row.get("icon").and_then(Value::as_str).unwrap_or("").trim(),
                "icon_gray": row.get("icongray").and_then(Value::as_str).unwrap_or("").trim(),
            })
        })
        .collect())
}

pub fn fetch_player(app_id: &str, steam_id: &str, api_key: &str) -> Result<Value, String> {
    let aid = validate_app_id(app_id)?;
    let sid = validate_steam_id(steam_id)?;
    if api_key.trim().is_empty() {
        return Err("Configure a Steam API key in Settings first.".to_owned());
    }
    let query = urlencode(&[
        ("key", api_key.trim().to_owned()),
        ("steamid", sid),
        ("appid", aid.to_string()),
        ("l", "english".to_owned()),
    ]);
    let url = format!("{STEAM_API_ROOT}/ISteamUserStats/GetPlayerAchievements/v1/?{query}");
    let payload = request_json(&url)?;
    let rows: Vec<Value> = achievement_rows(&payload)
        .into_iter()
        .map(|row| {
            let apiname = row
                .get("apiname")
                .or_else(|| row.get("name"))
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim();
            let achieved = if row.get("achieved").and_then(Value::as_i64) == Some(1) {
                1
            } else {
                0
            };
            let unlock_time = row
                .get("unlocktime")
                .and_then(Value::as_i64)
                .unwrap_or(0)
                .max(0);
            json!({
                "apiname": apiname,
                "achieved": achieved,
                "unlock_time": unlock_time,
            })
        })
        .collect();
    Ok(json!({
        "success": player_success(&payload),
        "achievements": rows,
    }))
}

pub fn fetch_owned_games(steam_id: &str, api_key: &str) -> Result<Vec<Value>, String> {
    let sid = validate_steam_id(steam_id)?;
    if api_key.trim().is_empty() {
        return Err("Configure a Steam API key in Settings first.".to_owned());
    }
    let query = urlencode(&[
        ("key", api_key.trim().to_owned()),
        ("steamid", sid),
        ("include_appinfo", "1".to_owned()),
        ("include_played_free_games", "1".to_owned()),
    ]);
    let url = format!("{STEAM_API_ROOT}/IPlayerService/GetOwnedGames/v1/?{query}");
    let payload = request_json(&url)?;
    let games = payload
        .get("response")
        .and_then(|r| r.get("games"))
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let mut owned = Vec::new();
    for entry in games.iter().take(MAX_OWNED_GAMES) {
        let Some(m) = entry.as_object() else {
            continue;
        };
        let appid = m.get("appid").and_then(Value::as_i64).unwrap_or(0);
        if appid <= 0 {
            continue;
        }
        owned.push(json!({
            "appid": appid,
            "name": m.get("name").and_then(Value::as_str).unwrap_or("").trim(),
            "playtime_forever": m.get("playtime_forever").and_then(Value::as_i64).unwrap_or(0).max(0),
            "rtime_last_played": m.get("rtime_last_played").and_then(Value::as_i64).unwrap_or(0).max(0),
        }));
    }
    Ok(owned)
}

pub fn merge_schema_player(schema: &[Value], player: &Value) -> Option<Value> {
    if !player_success(player) {
        return None;
    }
    let mut player_rows: HashMap<String, (bool, i64)> = HashMap::new();
    for row in achievement_rows(player) {
        let apiname = row
            .get("apiname")
            .or_else(|| row.get("name"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        if !apiname.is_empty() && !player_rows.contains_key(apiname) {
            let achieved = row.get("achieved").and_then(Value::as_i64) == Some(1);
            let unlock = row
                .get("unlock_time")
                .or_else(|| row.get("unlocktime"))
                .and_then(Value::as_i64)
                .unwrap_or(0)
                .max(0);
            player_rows.insert(apiname.to_owned(), (achieved, unlock));
        }
    }
    let mut items = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();
    for row in schema {
        let apiname = row
            .get("apiname")
            .or_else(|| row.get("name"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        if apiname.is_empty() || !seen.insert(apiname.to_owned()) {
            continue;
        }
        let title = row
            .get("title")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .unwrap_or(apiname);
        let (earned, unlock_time) = player_rows.get(apiname).copied().unwrap_or((false, 0));
        items.push(json!({
            "id": apiname,
            "title": title,
            "description": row.get("description").and_then(Value::as_str).unwrap_or(""),
            "points": 0,
            "hidden": row.get("hidden").and_then(Value::as_bool).unwrap_or(false),
            "icon_url": row.get("icon").and_then(Value::as_str).unwrap_or(""),
            "icon_gray_url": row.get("icon_gray").and_then(Value::as_str).unwrap_or(""),
            "earned": earned,
            "unlock_time": unlock_time,
        }));
        if items.len() >= MAX_ACHIEVEMENT_ITEMS {
            break;
        }
    }
    let total = items.len();
    if total == 0 {
        return None;
    }
    let earned = items
        .iter()
        .filter(|i| i.get("earned").and_then(Value::as_bool).unwrap_or(false))
        .count();
    Some(json!({
        "source": "steam",
        "name": "Steam",
        "earned": earned,
        "total": total,
        "progress_pct": py_round((earned as f64) * 100.0 / (total as f64), 1),
        "items": items,
    }))
}

pub fn cache_path(data_dir: &Path, app_id: &str) -> Result<PathBuf, String> {
    let aid = validate_app_id(app_id)?;
    Ok(data_dir
        .join("cache")
        .join("steam")
        .join(format!("{aid}.json")))
}

pub fn load_cache_any_age(data_dir: &Path, app_id: &str) -> Option<Value> {
    let path = cache_path(data_dir, app_id).ok()?;
    let raw = fs::read_to_string(&path).ok()?;
    let val: Value = serde_json::from_str(&raw).ok()?;
    val.is_object().then_some(val)
}

pub fn load_cache(data_dir: &Path, app_id: &str) -> Option<Value> {
    let path = cache_path(data_dir, app_id).ok()?;
    let meta = fs::metadata(&path).ok()?;
    let age = SystemTime::now()
        .duration_since(meta.modified().ok()?)
        .ok()?
        .as_secs();
    if age > STEAM_CACHE_TTL_SECS {
        return None;
    }
    let raw = fs::read_to_string(&path).ok()?;
    let val: Value = serde_json::from_str(&raw).ok()?;
    val.is_object().then_some(val)
}

pub fn save_cache(data_dir: &Path, app_id: &str, payload: &Value) -> Result<PathBuf, String> {
    let path = cache_path(data_dir, app_id)?;
    let body = serde_json::to_string_pretty(payload).map_err(|e| e.to_string())? + "\n";
    atomic_write_text(&path, &body, 0o600).map_err(|e| e.to_string())?;
    Ok(path)
}

pub fn get_achievements(
    data_dir: &Path,
    app_id: &str,
    steam_id: &str,
    api_key: &str,
    use_cache: bool,
) -> Result<Option<Value>, String> {
    if use_cache && let Some(cached) = load_cache(data_dir, app_id) {
        return Ok(cached.get("achievement").filter(|v| !v.is_null()).cloned());
    }
    let aid = validate_app_id(app_id)?;
    let schema = fetch_schema(app_id, api_key)?;
    let player = fetch_player(app_id, steam_id, api_key)?;
    let achievement = merge_schema_player(&schema, &player);
    let now = jiff::Timestamp::now()
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
        .to_string();
    let _ = save_cache(
        data_dir,
        app_id,
        &json!({
            "app_id": aid,
            "fetched_at": now,
            "achievement": achievement,
        }),
    );
    Ok(achievement)
}

fn parse_ts_secs(s: &str) -> i64 {
    if let Ok(ts) = s.parse::<jiff::Timestamp>() {
        return ts.as_second();
    }
    if let Ok(dt) = s.parse::<jiff::civil::DateTime>()
        && let Ok(ts) = jiff::tz::TimeZone::UTC.to_timestamp(dt)
    {
        return ts.as_second();
    }
    0
}

pub fn merge_steam_stats(games: &mut [Value], owned: &[Value]) -> usize {
    let mut by_app: HashMap<i64, &Value> = HashMap::new();
    for entry in owned {
        if let Some(aid) = entry
            .get("appid")
            .and_then(Value::as_i64)
            .filter(|&a| a > 0)
        {
            by_app.insert(aid, entry);
        }
    }
    let mut updated = 0usize;
    for game in games.iter_mut() {
        let Some(gm) = game.as_object_mut() else {
            continue;
        };
        let app_id = gm
            .get("steam_app_id")
            .and_then(|v| match v {
                Value::Number(n) => n.as_i64(),
                Value::String(s) => s.trim().parse::<i64>().ok(),
                _ => None,
            })
            .unwrap_or(0);
        if app_id <= 0 {
            continue;
        }
        let Some(&entry) = by_app.get(&app_id) else {
            continue;
        };
        let steam_secs = entry
            .get("playtime_forever")
            .and_then(Value::as_i64)
            .unwrap_or(0)
            .max(0)
            * 60;
        let local_secs = gm
            .get("playtime_seconds")
            .and_then(Value::as_i64)
            .unwrap_or(0)
            .max(0);
        gm.insert("playtime_seconds".into(), json!(local_secs.max(steam_secs)));
        let steam_last = entry
            .get("rtime_last_played")
            .and_then(Value::as_i64)
            .unwrap_or(0);
        let local_ts = parse_ts_secs(gm.get("last_played").and_then(Value::as_str).unwrap_or(""));
        if steam_last > 0
            && steam_last > local_ts
            && let Ok(ts) = jiff::Timestamp::from_second(steam_last)
        {
            gm.insert(
                "last_played".into(),
                Value::String(ts.strftime("%Y-%m-%dT%H:%M:%S").to_string()),
            );
        }
        updated += 1;
    }
    updated
}

pub fn _unix_now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}
