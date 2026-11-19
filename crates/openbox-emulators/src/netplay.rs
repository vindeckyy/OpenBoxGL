//! RetroArch netplay lobby browser (HTTPS-only, 60s cache, 8 MiB / 500-row cap, never raises).
//! Mirrors `pkg/parity/parity_netplay.py`.

use std::net::IpAddr;
use std::sync::LazyLock;
use std::time::{Duration, Instant};

use openbox_io::build_public_http_client;
use parking_lot::Mutex;
use regex::Regex;
use serde_json::{Value, json};

pub const LOBBY_URL: &str = "https://lobby.retroarch.com/api/lobby";
pub const LOBBY_TIMEOUT_SECS: u64 = 10;
pub const LOBBY_CACHE_SECS: u64 = 60;
pub const LOBBY_MAX_ROWS: usize = 500;
pub const LOBBY_MAX_BYTES: usize = 8 * 1024 * 1024;
pub const LOBBY_ERROR: &str = "Netplay lobby is unavailable.";
const MAX_HOST_LENGTH: usize = 253;
const MAX_PLAYERS: i64 = 10_000;

static HOST_LABEL_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$").unwrap());

type LobbyCacheEntry = Option<(Instant, Vec<Value>)>;
static CACHE: LazyLock<Mutex<LobbyCacheEntry>> = LazyLock::new(|| Mutex::new(None));

pub fn reset_cache() {
    *CACHE.lock() = None;
}

fn clean_text(val: Option<&Value>, limit: usize) -> String {
    let Some(v) = val else {
        return String::new();
    };
    if v.is_null() || v.is_boolean() {
        return String::new();
    }
    let raw = match v {
        Value::String(s) => s.clone(),
        other => other.to_string(),
    };
    raw.chars()
        .filter(|c| !c.is_control())
        .collect::<String>()
        .trim()
        .chars()
        .take(limit)
        .collect()
}

fn is_true(val: Option<&Value>) -> bool {
    match val {
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_i64() == Some(1) || n.as_f64() == Some(1.0),
        Some(Value::String(s)) => matches!(s.trim().to_ascii_lowercase().as_str(), "1" | "true"),
        _ => false,
    }
}

fn valid_host(val: Option<&Value>) -> String {
    let host = clean_text(val, MAX_HOST_LENGTH);
    if host.is_empty() || host.len() > MAX_HOST_LENGTH || host.chars().any(char::is_whitespace) {
        return String::new();
    }
    if host.parse::<IpAddr>().is_ok() {
        return host;
    }
    if host.split('.').all(|label| HOST_LABEL_RE.is_match(label)) {
        return host;
    }
    String::new()
}

fn valid_port(val: Option<&Value>) -> u16 {
    let n = match val {
        Some(Value::Number(n)) => n.as_i64().unwrap_or(0),
        Some(Value::String(s)) => s.trim().parse::<i64>().unwrap_or(0),
        _ => 0,
    };
    if (1..=65535).contains(&n) {
        n as u16
    } else {
        0
    }
}

fn valid_players(val: Option<&Value>) -> i64 {
    let n = match val {
        Some(Value::Number(n)) => n.as_i64().unwrap_or(0),
        Some(Value::String(s)) => s.trim().parse::<i64>().unwrap_or(0),
        _ => 0,
    };
    if (0..=MAX_PLAYERS).contains(&n) { n } else { 0 }
}

pub fn parse_lobby_rows(payload: &Value) -> Vec<Value> {
    let Some(arr) = payload.as_array() else {
        return Vec::new();
    };
    let mut rows = Vec::new();
    for entry in arr {
        let Some(fields) = entry.get("fields").and_then(Value::as_object) else {
            continue;
        };
        if !is_true(fields.get("connectable")) {
            continue;
        }
        let host = {
            let m = valid_host(fields.get("mitm_ip"));
            if !m.is_empty() {
                m
            } else {
                valid_host(fields.get("ip"))
            }
        };
        if host.is_empty() {
            continue;
        }
        let port = {
            let mp = valid_port(fields.get("mitm_port"));
            if mp != 0 {
                mp
            } else {
                valid_port(fields.get("port"))
            }
        };
        if port == 0 {
            continue;
        }
        rows.push(json!({
            "host": host,
            "port": port,
            "nick": clean_text(fields.get("username"), 80),
            "game": clean_text(fields.get("game_name"), 200),
            "core": clean_text(fields.get("core_name"), 100),
            "players": valid_players(fields.get("player_count")),
            "private": is_true(fields.get("has_password")),
        }));
        if rows.len() >= LOBBY_MAX_ROWS {
            break;
        }
    }
    rows
}

pub async fn lobby() -> Value {
    {
        let cache = CACHE.lock();
        if let Some((expires, rows)) = cache.as_ref()
            && Instant::now() < *expires
        {
            return json!({"available": true, "rows": rows});
        }
    }
    let Ok(client) = build_public_http_client(LOBBY_TIMEOUT_SECS) else {
        return json!({"available": false, "error": LOBBY_ERROR});
    };
    let Ok(resp) = client.get(LOBBY_URL).send().await else {
        return json!({"available": false, "error": LOBBY_ERROR});
    };
    if !resp.status().is_success() {
        return json!({"available": false, "error": LOBBY_ERROR});
    }
    let Ok(bytes) = resp.bytes().await else {
        return json!({"available": false, "error": LOBBY_ERROR});
    };
    if bytes.len() > LOBBY_MAX_BYTES {
        return json!({"available": false, "error": LOBBY_ERROR});
    }
    let Ok(payload) = serde_json::from_slice::<Value>(&bytes) else {
        return json!({"available": false, "error": LOBBY_ERROR});
    };
    let rows = parse_lobby_rows(&payload);
    if rows.is_empty() {
        return json!({"available": false, "error": LOBBY_ERROR});
    }
    *CACHE.lock() = Some((
        Instant::now() + Duration::from_secs(LOBBY_CACHE_SECS),
        rows.clone(),
    ));
    json!({"available": true, "rows": rows})
}
