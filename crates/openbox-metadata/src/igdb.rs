//! Optional IGDB metadata provider (`pkg/parity/parity_igdb.py`).

use std::sync::LazyLock;
use std::time::{SystemTime, UNIX_EPOCH};

use openbox_io::ensure_env_loaded;
use parking_lot::Mutex;
use serde_json::{Map, Value, json};

use crate::http::{http_post_raw, py_round, urlencode};

pub const IGDB_ENDPOINT: &str = "https://api.igdb.com/v4";

struct TokenCache {
    client_id: String,
    value: String,
    expires: f64,
}

static TOKEN_CACHE: LazyLock<Mutex<TokenCache>> = LazyLock::new(|| {
    Mutex::new(TokenCache {
        client_id: String::new(),
        value: String::new(),
        expires: 0.0,
    })
});

pub fn credentials() -> Result<(String, String), String> {
    ensure_env_loaded(None);
    let client_id = std::env::var("IGDB_CLIENT_ID")
        .unwrap_or_default()
        .trim()
        .to_owned();
    let client_secret = std::env::var("IGDB_CLIENT_SECRET")
        .unwrap_or_default()
        .trim()
        .to_owned();
    if client_id.is_empty() || client_secret.is_empty() {
        return Err("Set IGDB_CLIENT_ID and IGDB_CLIENT_SECRET in ~/.env to use IGDB.".to_owned());
    }
    Ok((client_id, client_secret))
}

pub fn is_configured() -> bool {
    credentials().is_ok()
}

fn now_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

pub fn access_token() -> Result<(String, String), String> {
    let (client_id, client_secret) = credentials()?;
    let now = now_secs();
    {
        let cache = TOKEN_CACHE.lock();
        if cache.client_id == client_id && !cache.value.is_empty() && cache.expires > now + 30.0 {
            return Ok((client_id, cache.value.clone()));
        }
    }
    let body = urlencode(&[
        ("client_id", client_id.clone()),
        ("client_secret", client_secret),
        ("grant_type", "client_credentials".to_owned()),
    ]);
    let resp = http_post_raw(
        "https://id.twitch.tv/oauth2/token",
        body.into_bytes(),
        &[(
            "Content-Type",
            "application/x-www-form-urlencoded".to_owned(),
        )],
        30,
        2 * 1024 * 1024,
    )?;
    if resp.status >= 400 {
        return Err(format!("IGDB token request failed (HTTP {}).", resp.status));
    }
    let payload: Value = serde_json::from_slice(&resp.body)
        .map_err(|_| "IGDB token request returned an invalid response.".to_owned())?;
    let token = payload
        .get("access_token")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    if token.is_empty() {
        return Err("IGDB token request failed.".to_owned());
    }
    let expires_in = payload
        .get("expires_in")
        .and_then(Value::as_i64)
        .unwrap_or(3600) as f64;
    let mut cache = TOKEN_CACHE.lock();
    cache.client_id = client_id.clone();
    cache.value = token.clone();
    cache.expires = now + expires_in;
    Ok((client_id, token))
}

pub fn igdb_request(path: &str, query: &str) -> Result<Value, String> {
    let (client_id, token) = access_token()?;
    let url = format!("{IGDB_ENDPOINT}/{path}");
    let resp = http_post_raw(
        &url,
        query.as_bytes().to_vec(),
        &[
            ("Client-ID", client_id),
            ("Authorization", format!("Bearer {token}")),
            ("Accept", "application/json".to_owned()),
        ],
        30,
        8 * 1024 * 1024,
    )?;
    if resp.status >= 400 {
        return Err(format!("IGDB request failed (HTTP {}).", resp.status));
    }
    serde_json::from_slice(&resp.body).map_err(|e| format!("Invalid IGDB JSON: {e}"))
}

fn year_from_timestamp(val: Option<&Value>) -> String {
    let Some(secs) = val.and_then(Value::as_i64) else {
        return String::new();
    };
    jiff::Timestamp::from_second(secs)
        .ok()
        .map(|ts| {
            jiff::tz::TimeZone::UTC
                .to_datetime(ts)
                .date()
                .year()
                .to_string()
        })
        .unwrap_or_default()
}

pub fn search_games(name: &str, platform: &str, limit: usize) -> Result<Vec<Value>, String> {
    let trimmed = name.trim();
    if trimmed.is_empty() {
        return Ok(Vec::new());
    }
    let cap = limit.clamp(1, 50);
    let safe_name = trimmed.replace('"', "");
    let query = format!(
        "search \"{safe_name}\"; fields id,name,summary,first_release_date,genres.name,platforms.name,rating,aggregated_rating; limit {cap};"
    );
    let records = igdb_request("games", &query)?;
    let arr = records.as_array().map(Vec::as_slice).unwrap_or(&[]);
    let mut results = Vec::new();
    for rec in arr {
        if !rec.is_object() {
            continue;
        }
        let genres = rec
            .get("genres")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(|it| it.get("name").and_then(Value::as_str))
                    .collect::<Vec<_>>()
                    .join(", ")
            })
            .unwrap_or_default();
        let platforms = rec
            .get("platforms")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(|it| it.get("name").and_then(Value::as_str))
                    .collect::<Vec<_>>()
                    .join(", ")
            })
            .unwrap_or_default();
        results.push(json!({
            "id": rec.get("id").cloned().unwrap_or(Value::Null),
            "name": rec.get("name").and_then(Value::as_str).unwrap_or(""),
            "summary": rec.get("summary").and_then(Value::as_str).unwrap_or(""),
            "year": year_from_timestamp(rec.get("first_release_date")),
            "genres": genres,
            "platforms": platforms,
            "rating": rec.get("rating").cloned().unwrap_or(Value::Null),
            "critic_score": rec.get("aggregated_rating").cloned().unwrap_or(Value::Null),
        }));
    }
    if !platform.is_empty() {
        let p_low = platform.to_lowercase();
        let filtered: Vec<Value> = results
            .iter()
            .filter(|it| {
                it.get("platforms")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_lowercase()
                    .contains(&p_low)
            })
            .cloned()
            .collect();
        if !filtered.is_empty() {
            return Ok(filtered);
        }
    }
    Ok(results)
}

pub fn fetch_time_to_beat(game_id: i64) -> Option<f64> {
    let query = format!("fields normally; where game_id = {game_id}; limit 1;");
    let records = igdb_request("game_time_to_beats", &query).ok()?;
    let first = records.as_array()?.first()?.as_object()?;
    let seconds = first.get("normally").and_then(Value::as_i64)?;
    if seconds <= 0 {
        return None;
    }
    Some(py_round(seconds as f64 / 3600.0, 1))
}

pub fn fetch_game(game_id: i64) -> Result<Value, String> {
    let query = format!(
        "fields name,summary,storyline,first_release_date,genres.name,platforms.name,\
         involved_companies.company.name,involved_companies.developer,involved_companies.publisher,\
         rating,aggregated_rating; where id = {game_id};"
    );
    let records = igdb_request("games", &query)?;
    let Some(record) = records.as_array().and_then(|a| a.first()) else {
        return Err("IGDB game not found.".to_owned());
    };
    let mut developers = Vec::new();
    let mut publishers = Vec::new();
    if let Some(companies) = record.get("involved_companies").and_then(Value::as_array) {
        for entry in companies {
            let name = entry
                .get("company")
                .and_then(|c| c.get("name"))
                .and_then(Value::as_str)
                .unwrap_or("");
            if name.is_empty() {
                continue;
            }
            if entry
                .get("developer")
                .and_then(Value::as_bool)
                .unwrap_or(false)
            {
                developers.push(name.to_owned());
            }
            if entry
                .get("publisher")
                .and_then(Value::as_bool)
                .unwrap_or(false)
            {
                publishers.push(name.to_owned());
            }
        }
    }
    let genres = record
        .get("genres")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(|it| it.get("name").and_then(Value::as_str))
                .collect::<Vec<_>>()
                .join(", ")
        })
        .unwrap_or_default();
    let description = record
        .get("summary")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .or_else(|| record.get("storyline").and_then(Value::as_str))
        .unwrap_or("");
    let rating = record
        .get("rating")
        .and_then(Value::as_f64)
        .filter(|&v| v != 0.0)
        .map(|v| json!(py_round(v / 20.0, 2)))
        .unwrap_or(Value::Null);
    let beat = fetch_time_to_beat(game_id);
    Ok(json!({
        "name": record.get("name").and_then(Value::as_str).unwrap_or(""),
        "description": description,
        "year": year_from_timestamp(record.get("first_release_date")),
        "genre": genres,
        "developer": developers.join(", "),
        "publisher": publishers.join(", "),
        "rating": rating,
        "critic_score": record.get("aggregated_rating").cloned().unwrap_or(Value::Null),
        "time_to_beat_hours": beat,
        "igdb_id": game_id,
    }))
}

pub fn apply_to_game(game: &mut Map<String, Value>, metadata: &Value) {
    for field in [
        "name",
        "description",
        "year",
        "genre",
        "developer",
        "publisher",
    ] {
        if let Some(v) = metadata.get(field) {
            let empty = match v {
                Value::Null => true,
                Value::String(s) => s.is_empty(),
                _ => false,
            };
            if !empty {
                game.insert(field.to_owned(), v.clone());
            }
        }
    }
    if let Some(r) = metadata.get("rating").filter(|v| !v.is_null()) {
        game.insert("rating".into(), r.clone());
    }
    if let Some(id) = metadata.get("igdb_id").filter(|v| !v.is_null()) {
        game.insert("igdb_id".into(), id.clone());
    }
    if let Some(ttb) = metadata
        .get("time_to_beat_hours")
        .filter(|v| v.as_f64().is_some_and(|n| n > 0.0))
    {
        game.insert("time_to_beat_hours".into(), ttb.clone());
    }
}
