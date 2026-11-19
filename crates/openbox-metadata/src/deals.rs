//! Deal Radar — opt-in Steam price watch & manual entries (`pkg/parity/parity_deals.py`, ADR 0111).

use std::collections::HashMap;
use std::fs;
use std::path::Path;
use std::sync::LazyLock;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, SystemTime};

use jiff::Timestamp;
use openbox_core::{APP_VERSION, ApiError};
use openbox_io::atomic_write_text;
use openbox_runtime::notifications::{NotificationSpec, add_notification};
use regex::Regex;
use serde_json::{Map, Value, json};
use url::Url;
use uuid::Uuid;

use crate::http::{http_get_raw, quote_plus, urlencode};

pub const ALLOWED_HOST: &str = "store.steampowered.com";
pub const MAX_WATCHLIST: usize = 200;
pub const MAX_HISTORY_POINTS: usize = 50;
pub const MAX_NOTE_LEN: usize = 280;
pub const MAX_NAME_LEN: usize = 200;
pub const MAX_RESPONSE_BYTES: usize = 2 * 1024 * 1024;
pub const DEFAULT_POLL_HOURS: i64 = 168;
pub const MIN_POLL_HOURS: i64 = 24;
pub const MAX_POLL_HOURS: i64 = 720;
pub const DEFAULT_MIN_DROP_PCT: i64 = 10;
pub const DEFAULT_STORE_CC: &str = "us";
pub const DEFAULT_SPACING_SECS: u64 = 1;

static APPID_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\d{1,10}$").unwrap());
static CC_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[a-z]{2}$").unwrap());
static CURRENCY_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[A-Z]{3}$").unwrap());

fn utc_iso(now: Option<Timestamp>) -> String {
    now.unwrap_or_else(Timestamp::now)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
        .to_string()
}

pub fn validate_appid(raw: &str) -> Result<String, ApiError> {
    let text = raw.trim();
    if !APPID_RE.is_match(text) {
        return Err(ApiError::deals_invalid_appid(
            "Steam AppID must be 1–10 digits.",
        ));
    }
    Ok(text.to_owned())
}

pub fn normalize_cc(raw: &str) -> String {
    let text = raw.trim().to_ascii_lowercase();
    if CC_RE.is_match(&text) {
        text
    } else {
        DEFAULT_STORE_CC.to_owned()
    }
}

pub fn clamp_poll_hours(raw: Option<&Value>) -> i64 {
    raw.and_then(Value::as_i64)
        .unwrap_or(DEFAULT_POLL_HOURS)
        .clamp(MIN_POLL_HOURS, MAX_POLL_HOURS)
}

pub fn clamp_min_drop_pct(raw: Option<&Value>) -> i64 {
    raw.and_then(Value::as_i64)
        .unwrap_or(DEFAULT_MIN_DROP_PCT)
        .clamp(1, 90)
}

pub fn validate_allowlisted_url(url: &str) -> Result<String, ApiError> {
    let parsed = Url::parse(url).map_err(|_| {
        ApiError::deals_host_blocked(format!("Outbound Deal Radar host not allowlisted: {url}"))
    })?;
    let host = parsed.host_str().unwrap_or("").to_ascii_lowercase();
    if parsed.scheme() != "https"
        || host != ALLOWED_HOST
        || !parsed.username().is_empty()
        || parsed.password().is_some()
    {
        return Err(ApiError::deals_host_blocked(format!(
            "Outbound Deal Radar host not allowlisted: {url}"
        )));
    }
    Ok(url.to_owned())
}

fn steam_get(url: &str) -> Result<Value, ApiError> {
    let checked = validate_allowlisted_url(url)?;
    let resp = http_get_raw(
        &checked,
        &[
            ("User-Agent", format!("OpenBox/{APP_VERSION}")),
            ("Accept", "application/json".to_owned()),
        ],
        15,
        MAX_RESPONSE_BYTES,
    )
    .map_err(ApiError::bad_request)?;
    if resp.status >= 400 {
        return Err(ApiError::bad_request(format!(
            "Steam returned HTTP {}",
            resp.status
        )));
    }
    serde_json::from_slice(&resp.body)
        .map_err(|e| ApiError::bad_request(format!("Invalid Steam response: {e}")))
}

fn ensure_deals_block(state: &mut Value) -> &mut Map<String, Value> {
    let obj = state.as_object_mut().expect("state object");
    let entry = obj
        .entry("deals".to_owned())
        .or_insert_with(|| json!({"watchlist": [], "prices": {}, "last_poll_at": ""}));
    if !entry.is_object() {
        *entry = json!({"watchlist": [], "prices": {}, "last_poll_at": ""});
    }
    let d = entry.as_object_mut().unwrap();
    if !d.get("watchlist").is_some_and(Value::is_array) {
        d.insert("watchlist".into(), Value::Array(Vec::new()));
    }
    if !d.get("prices").is_some_and(Value::is_object) {
        d.insert("prices".into(), Value::Object(Map::new()));
    }
    if !d.get("last_poll_at").is_some_and(Value::is_string) {
        d.insert("last_poll_at".into(), Value::String(String::new()));
    }
    d
}

pub fn search_steam(query: &str, cc: &str) -> Result<Vec<Value>, ApiError> {
    let q: String = query.split_whitespace().collect::<Vec<_>>().join(" ");
    if q.is_empty() || q.len() > 100 {
        return Err(ApiError::bad_request(
            "Search query must be 1–100 characters.",
        ));
    }
    let store_cc = normalize_cc(cc);
    let url = format!(
        "https://{ALLOWED_HOST}/api/storesearch/?term={}&cc={store_cc}&l=en",
        quote_plus(&q)
    );
    let payload = steam_get(&url)?;
    let items = payload
        .get("items")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let mut results = Vec::new();
    for item in items.iter().take(10) {
        let Some(m) = item.as_object() else {
            continue;
        };
        let raw_id = match m.get("id") {
            Some(Value::String(s)) => s.clone(),
            Some(Value::Number(n)) => n.to_string(),
            _ => continue,
        };
        let Ok(appid) = validate_appid(&raw_id) else {
            continue;
        };
        let price = m.get("price").and_then(Value::as_object);
        let final_cents = price.and_then(|p| p.get("final")).and_then(Value::as_i64);
        let initial_cents = price
            .and_then(|p| p.get("initial"))
            .and_then(Value::as_i64)
            .or(final_cents);
        let cur_raw = price
            .and_then(|p| p.get("currency"))
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .map(|s| s.trim().to_ascii_uppercase())
            .unwrap_or_else(|| store_cc.to_ascii_uppercase());
        let cur: String = cur_raw.chars().take(3).collect();
        let currency = if CURRENCY_RE.is_match(&cur) {
            cur
        } else {
            String::new()
        };
        let discount_pct = match (initial_cents, final_cents) {
            (Some(init), Some(fin)) if init > 0 && fin < init => {
                (((init - fin) as f64 * 100.0) / (init as f64)).round_ties_even() as i64
            }
            _ => 0,
        };
        let name: String = m
            .get("name")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .unwrap_or(&appid)
            .chars()
            .take(MAX_NAME_LEN)
            .collect();
        let artwork: String = m
            .get("tiny_image")
            .and_then(Value::as_str)
            .unwrap_or("")
            .chars()
            .take(400)
            .collect();
        results.push(json!({
            "appid": appid,
            "name": name,
            "artwork_url": artwork,
            "currency": currency,
            "initial_cents": initial_cents,
            "final_cents": final_cents,
            "discount_pct": discount_pct,
            "free": final_cents == Some(0),
        }));
    }
    Ok(results)
}

fn normalize_appdetails(payload: &Value, appid: &str, cc: &str) -> Value {
    let entry = payload.get(appid).and_then(Value::as_object);
    if !entry
        .and_then(|e| e.get("success"))
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        return json!({
            "status": "not_found",
            "appid": appid,
            "cc": cc,
            "name": "",
            "artwork_url": "",
            "currency": "",
            "price_cents": Value::Null,
            "initial_cents": Value::Null,
            "discount_pct": 0,
            "free": false,
        });
    }
    let data = entry.and_then(|e| e.get("data")).and_then(Value::as_object);
    let empty = Map::new();
    let d = data.unwrap_or(&empty);
    let name: String = d
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or("")
        .chars()
        .take(MAX_NAME_LEN)
        .collect();
    let artwork_url: String = d
        .get("header_image")
        .and_then(Value::as_str)
        .unwrap_or("")
        .chars()
        .take(400)
        .collect();
    if d.get("is_free").and_then(Value::as_bool).unwrap_or(false) {
        return json!({
            "status": "free",
            "appid": appid,
            "cc": cc,
            "name": name,
            "artwork_url": artwork_url,
            "currency": "",
            "price_cents": 0,
            "initial_cents": 0,
            "discount_pct": 0,
            "free": true,
        });
    }
    let Some(po) = d.get("price_overview").and_then(Value::as_object) else {
        return json!({
            "status": "no_price",
            "appid": appid,
            "cc": cc,
            "name": name,
            "artwork_url": artwork_url,
            "currency": "",
            "price_cents": Value::Null,
            "initial_cents": Value::Null,
            "discount_pct": 0,
            "free": false,
        });
    };
    let final_cents = po.get("final").and_then(Value::as_i64).unwrap_or(0);
    let initial_cents = po
        .get("initial")
        .and_then(Value::as_i64)
        .unwrap_or(final_cents);
    let discount_pct = po
        .get("discount_percent")
        .and_then(Value::as_i64)
        .unwrap_or(0)
        .clamp(0, 100);
    let currency: String = po
        .get("currency")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_ascii_uppercase()
        .chars()
        .take(3)
        .collect();
    json!({
        "status": "priced",
        "appid": appid,
        "cc": cc,
        "name": name,
        "artwork_url": artwork_url,
        "currency": currency,
        "price_cents": final_cents,
        "initial_cents": initial_cents,
        "discount_pct": discount_pct,
        "free": final_cents == 0,
    })
}

pub fn fetch_steam_appdetails(
    data_dir: &Path,
    appid: &str,
    cc: &str,
    ttl_seconds: u64,
    force: bool,
) -> Result<Value, ApiError> {
    let aid = validate_appid(appid)?;
    let store_cc = normalize_cc(cc);
    let cache_path = data_dir
        .join("cache")
        .join("deals")
        .join(format!("{aid}_{store_cc}.json"));
    if !force
        && cache_path.is_file()
        && let Ok(meta) = fs::metadata(&cache_path)
        && let Ok(mtime) = meta.modified()
        && let Ok(age) = SystemTime::now().duration_since(mtime)
        && age.as_secs() < ttl_seconds
        && let Ok(raw) = fs::read_to_string(&cache_path)
        && let Ok(val) = serde_json::from_str::<Value>(&raw)
        && val.get("appid").and_then(Value::as_str) == Some(aid.as_str())
    {
        return Ok(val);
    }
    let query = urlencode(&[
        ("appids", aid.clone()),
        ("cc", store_cc.clone()),
        ("filters", "name,price_overview,header_image".to_owned()),
    ]);
    let url = format!("https://{ALLOWED_HOST}/api/appdetails?{query}");
    let payload = steam_get(&url)?;
    let normalized = normalize_appdetails(&payload, &aid, &store_cc);
    if let Ok(text) = serde_json::to_string(&normalized) {
        let _ = atomic_write_text(&cache_path, &text, 0o600);
    }
    Ok(normalized)
}

fn normalize_target_cents(raw: Option<&Value>) -> Result<Option<i64>, ApiError> {
    let Some(v) = raw else {
        return Ok(None);
    };
    if v.is_null() || v.as_str() == Some("") {
        return Ok(None);
    }
    if v.is_boolean() {
        return Err(ApiError::bad_request(
            "target_price_cents must be a non-negative integer or null.",
        ));
    }
    let val = match v {
        Value::Number(n) => n.as_i64(),
        Value::String(s) => s.trim().parse::<i64>().ok(),
        _ => None,
    }
    .ok_or_else(|| ApiError::bad_request("target_price_cents must be a non-negative integer."))?;
    if !(0..=100_000_000).contains(&val) {
        return Err(ApiError::bad_request("target_price_cents is out of range."));
    }
    Ok(Some(val))
}

pub fn watch_game(state: &mut Value, payload: &Map<String, Value>) -> Result<Value, ApiError> {
    let deals = ensure_deals_block(state);
    let manual = payload
        .get("manual")
        .and_then(Value::as_bool)
        .unwrap_or(false)
        || (payload.get("appid").is_none_or(Value::is_null)
            && payload.get("store").and_then(Value::as_str) == Some("manual"));
    let mut appid: Option<String> = None;
    let mut store: String = payload
        .get("store")
        .and_then(Value::as_str)
        .unwrap_or(if manual { "manual" } else { "steam" })
        .trim()
        .chars()
        .take(40)
        .collect();
    if store.is_empty() {
        store = "steam".to_owned();
    }
    if !manual {
        let raw_id = payload
            .get("appid")
            .map(|v| match v {
                Value::String(s) => s.clone(),
                Value::Number(n) => n.to_string(),
                _ => String::new(),
            })
            .unwrap_or_default();
        appid = Some(validate_appid(&raw_id)?);
        store = "steam".to_owned();
    }
    let target = normalize_target_cents(payload.get("target_price_cents"))?;
    let note: String = payload
        .get("note")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .chars()
        .take(MAX_NOTE_LEN)
        .collect();
    let fallback_name = match &appid {
        Some(a) => format!("Steam {a}"),
        None => "Watch item".to_owned(),
    };
    let name: String = payload
        .get("name")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .unwrap_or(&fallback_name)
        .chars()
        .take(MAX_NAME_LEN)
        .collect();
    if name.is_empty() {
        return Err(ApiError::bad_request("Watch entry name is required."));
    }
    let artwork_url: String = payload
        .get("artwork_url")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .chars()
        .take(400)
        .collect();

    if let Some(ref aid) = appid
        && let Some(wl) = deals.get_mut("watchlist").and_then(Value::as_array_mut)
        && let Some(existing) = wl
            .iter_mut()
            .find(|row| row.get("appid").and_then(Value::as_str) == Some(aid.as_str()))
    {
        if payload.contains_key("target_price_cents") {
            existing["target_price_cents"] = target.map(|v| json!(v)).unwrap_or(Value::Null);
        }
        if !note.is_empty() {
            existing["note"] = Value::String(note);
        }
        return Ok(existing.clone());
    }

    let wl_len = deals
        .get("watchlist")
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0);
    if wl_len >= MAX_WATCHLIST {
        return Err(ApiError::deals_limit_exceeded(format!(
            "Deal Radar watchlist is capped at {MAX_WATCHLIST} entries."
        )));
    }

    let raw_hex = Uuid::new_v4().simple().to_string();
    let item_id = format!("deal-{}", &raw_hex[..16]);
    let record = json!({
        "id": item_id,
        "name": name,
        "store": store,
        "appid": appid,
        "manual": manual,
        "artwork_url": artwork_url,
        "target_price_cents": target,
        "note": note,
        "added_at": utc_iso(None),
        "last_price_cents": Value::Null,
        "last_discount_pct": 0,
        "last_currency": "",
        "last_seen_at": "",
        "last_notified_price_cents": Value::Null,
        "last_notified_at": "",
        "fetch_error": "",
    });
    deals["watchlist"]
        .as_array_mut()
        .unwrap()
        .push(record.clone());
    deals["prices"]
        .as_object_mut()
        .unwrap()
        .entry(item_id)
        .or_insert_with(|| Value::Array(Vec::new()));
    Ok(record)
}

pub fn unwatch_game(state: &mut Value, item_id: &str) -> Result<Value, ApiError> {
    let did = item_id.trim();
    let deals = ensure_deals_block(state);
    let wl = deals["watchlist"].as_array_mut().unwrap();
    let before = wl.len();
    wl.retain(|row| row.get("id").and_then(Value::as_str) != Some(did));
    if wl.len() == before {
        return Err(ApiError::deals_not_found(format!(
            "Deal watchlist item not found: {did}"
        )));
    }
    deals["prices"].as_object_mut().unwrap().remove(did);
    Ok(json!({"deleted": true, "id": did}))
}

fn append_price_point(
    deals: &mut Map<String, Value>,
    item_id: &str,
    price_cents: i64,
    currency: &str,
    discount_pct: i64,
    source: &str,
    now_iso: &str,
) {
    let prices = deals["prices"].as_object_mut().unwrap();
    let hist = prices
        .entry(item_id.to_owned())
        .or_insert_with(|| Value::Array(Vec::new()));
    if !hist.is_array() {
        *hist = Value::Array(Vec::new());
    }
    let arr = hist.as_array_mut().unwrap();
    let cur: String = currency
        .trim()
        .to_ascii_uppercase()
        .chars()
        .take(3)
        .collect();
    arr.push(json!({
        "at": now_iso,
        "price_cents": price_cents,
        "currency": cur,
        "discount_pct": discount_pct.clamp(0, 100),
        "source": source,
    }));
    if arr.len() > MAX_HISTORY_POINTS {
        let drop_n = arr.len() - MAX_HISTORY_POINTS;
        arr.drain(..drop_n);
    }
}

pub fn record_manual_entry(
    state: &mut Value,
    payload: &Map<String, Value>,
) -> Result<Value, ApiError> {
    let did = payload
        .get("id")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_owned();
    let deals = ensure_deals_block(state);
    let idx = deals["watchlist"]
        .as_array()
        .unwrap()
        .iter()
        .position(|row| row.get("id").and_then(Value::as_str) == Some(did.as_str()))
        .ok_or_else(|| {
            ApiError::deals_not_found(format!("Deal watchlist item not found: {did}"))
        })?;

    let target_opt = if payload.contains_key("target_price_cents") {
        Some(normalize_target_cents(payload.get("target_price_cents"))?)
    } else {
        None
    };
    let price_opt = if let Some(pv) = payload.get("price_cents").filter(|v| !v.is_null()) {
        normalize_target_cents(Some(pv))?
    } else {
        None
    };

    let last_cur = deals["watchlist"][idx]
        .get("last_currency")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    let currency: String = payload
        .get("currency")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .unwrap_or(&last_cur)
        .trim()
        .to_ascii_uppercase()
        .chars()
        .take(3)
        .collect();
    if price_opt.is_some() && !currency.is_empty() && !CURRENCY_RE.is_match(&currency) {
        return Err(ApiError::bad_request(
            "currency must be a 3-letter ISO code.",
        ));
    }
    let discount_pct = if price_opt.is_some() {
        match payload.get("discount_pct") {
            None | Some(Value::Null) => 0,
            Some(Value::Number(n)) => n
                .as_i64()
                .ok_or_else(|| ApiError::bad_request("discount_pct must be an integer."))?
                .clamp(0, 100),
            Some(Value::String(s)) => s
                .trim()
                .parse::<i64>()
                .map_err(|_| ApiError::bad_request("discount_pct must be an integer."))?
                .clamp(0, 100),
            _ => return Err(ApiError::bad_request("discount_pct must be an integer.")),
        }
    } else {
        0
    };

    let stamp = utc_iso(None);
    if let Some(t) = target_opt {
        deals["watchlist"][idx]["target_price_cents"] = t.map(|v| json!(v)).unwrap_or(Value::Null);
    }
    if payload.contains_key("note") {
        let note: String = payload
            .get("note")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .chars()
            .take(MAX_NOTE_LEN)
            .collect();
        deals["watchlist"][idx]["note"] = Value::String(note);
    }
    if let Some(price_val) = price_opt {
        append_price_point(
            deals,
            &did,
            price_val,
            &currency,
            discount_pct,
            "manual",
            &stamp,
        );
        let entry = &mut deals["watchlist"][idx];
        entry["last_price_cents"] = json!(price_val);
        entry["last_currency"] = Value::String(currency);
        entry["last_discount_pct"] = json!(discount_pct);
        entry["last_seen_at"] = Value::String(stamp);
        entry["fetch_error"] = Value::String(String::new());
    }
    Ok(deals["watchlist"][idx].clone())
}

pub fn apply_poll_observation(
    state: &mut Value,
    item_id: &str,
    obs: &Value,
    settings: &Value,
) -> Value {
    let stamp = utc_iso(None);
    let min_drop = clamp_min_drop_pct(settings.get("deals_min_drop_pct"));
    let mut notify_info: Option<(String, i64, String, i64)> = None;

    {
        let deals = ensure_deals_block(state);
        let Some(idx) = deals["watchlist"]
            .as_array()
            .unwrap()
            .iter()
            .position(|r| r.get("id").and_then(Value::as_str) == Some(item_id))
        else {
            return json!({"updated": false, "notified": false});
        };
        if let Some(name) = obs
            .get("name")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
        {
            let cur = deals["watchlist"][idx]
                .get("name")
                .and_then(Value::as_str)
                .unwrap_or("");
            if cur.is_empty() || cur.starts_with("Steam ") {
                deals["watchlist"][idx]["name"] =
                    Value::String(name.chars().take(MAX_NAME_LEN).collect());
            }
        }
        if let Some(art) = obs
            .get("artwork_url")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            && deals["watchlist"][idx]
                .get("artwork_url")
                .and_then(Value::as_str)
                .is_none_or(str::is_empty)
        {
            deals["watchlist"][idx]["artwork_url"] = Value::String(art.chars().take(400).collect());
        }
        let status = obs.get("status").and_then(Value::as_str).unwrap_or("");
        if status == "not_found" {
            deals["watchlist"][idx]["fetch_error"] = Value::String("not_found".to_owned());
            deals["watchlist"][idx]["last_seen_at"] = Value::String(stamp);
            return json!({"updated": true, "notified": false});
        }
        let Some(new_price) = obs.get("price_cents").and_then(Value::as_i64) else {
            deals["watchlist"][idx]["fetch_error"] = Value::String(String::new());
            deals["watchlist"][idx]["last_seen_at"] = Value::String(stamp);
            return json!({"updated": true, "notified": false});
        };
        if status == "no_price" {
            deals["watchlist"][idx]["fetch_error"] = Value::String(String::new());
            deals["watchlist"][idx]["last_seen_at"] = Value::String(stamp);
            return json!({"updated": true, "notified": false});
        }
        let currency: String = obs
            .get("currency")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_ascii_uppercase()
            .chars()
            .take(3)
            .collect();
        let discount_pct = obs.get("discount_pct").and_then(Value::as_i64).unwrap_or(0);
        let prev_price = deals["watchlist"][idx]
            .get("last_price_cents")
            .and_then(Value::as_i64);

        append_price_point(
            deals,
            item_id,
            new_price,
            &currency,
            discount_pct,
            "steam",
            &stamp,
        );
        let entry = &mut deals["watchlist"][idx];
        entry["last_price_cents"] = json!(new_price);
        entry["last_discount_pct"] = json!(discount_pct);
        entry["last_currency"] = Value::String(currency.clone());
        entry["last_seen_at"] = Value::String(stamp.clone());
        entry["fetch_error"] = Value::String(String::new());

        if let Some(prev) = prev_price {
            let target = entry.get("target_price_cents").and_then(Value::as_i64);
            let drop_hit = discount_pct >= min_drop && new_price < prev;
            let target_hit = target.is_some_and(|t| new_price <= t);
            let last_notified = entry
                .get("last_notified_price_cents")
                .and_then(Value::as_i64);
            if (drop_hit || target_hit) && last_notified != Some(new_price) {
                entry["last_notified_price_cents"] = json!(new_price);
                entry["last_notified_at"] = Value::String(stamp.clone());
                let entry_name = entry
                    .get("name")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned();
                notify_info = Some((entry_name, new_price, currency, discount_pct));
            }
        }
    }

    let mut notified = false;
    if let Some((entry_name, new_price, currency, discount_pct)) = notify_info {
        let cur = if currency.is_empty() {
            "USD"
        } else {
            &currency
        };
        let price_label = format!("{cur} {:.2}", new_price as f64 / 100.0);
        let title = format!("Price alert: {entry_name}");
        let body = format!("{entry_name} is now {price_label} (-{discount_pct}%).");
        let dedupe = format!("deal:{item_id}:{new_price}");
        let params = json!({
            "name": entry_name,
            "price": price_label,
            "discount": discount_pct,
        });
        add_notification(
            state,
            NotificationSpec {
                kind: "deal",
                level: "info",
                title: &title,
                body: &body,
                source: "deals",
                dedupe_key: &dedupe,
                title_key: "notify.server.deal_drop_title",
                body_key: "notify.server.deal_drop_body",
                params: params.as_object(),
                now: Some(&stamp),
                ..Default::default()
            },
        );
        notified = true;
    }
    json!({"updated": true, "notified": notified})
}

pub fn list_deals(state: &Value) -> Value {
    let deals = state.get("deals").and_then(Value::as_object);
    let watchlist = deals
        .and_then(|d| d.get("watchlist"))
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let prices = deals
        .and_then(|d| d.get("prices"))
        .and_then(Value::as_object);
    let mut games_by_appid: HashMap<String, &Value> = HashMap::new();
    if let Some(games) = state.get("games").and_then(Value::as_array) {
        for g in games {
            if let Some(aid) = g.get("steam_app_id").and_then(|v| match v {
                Value::String(s) if !s.trim().is_empty() => Some(s.trim().to_owned()),
                Value::Number(n) => Some(n.to_string()),
                _ => None,
            }) {
                games_by_appid.insert(aid, g);
            }
        }
    }

    let now_ts = Timestamp::now();
    let mut enriched = Vec::new();
    for item in watchlist {
        let Some(im) = item.as_object() else {
            continue;
        };
        let did = im.get("id").and_then(Value::as_str).unwrap_or("");
        let hist: Vec<Value> = prices
            .and_then(|p| p.get(did))
            .and_then(Value::as_array)
            .map(|a| a.iter().filter(|x| x.is_object()).cloned().collect())
            .unwrap_or_default();
        let lowest = hist
            .iter()
            .filter_map(|p| p.get("price_cents").and_then(Value::as_i64))
            .min();
        let age_hours = im
            .get("last_seen_at")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .and_then(|s| s.parse::<Timestamp>().ok())
            .map(|ts| ((now_ts.as_second() - ts.as_second()) / 3600).max(0));
        let linked = im
            .get("appid")
            .and_then(Value::as_str)
            .and_then(|a| games_by_appid.get(a).copied());
        let paid_cents = linked
            .and_then(|g| g.get("price_paid_cents"))
            .and_then(Value::as_i64);
        let paid_currency = linked
            .and_then(|g| g.get("price_currency"))
            .and_then(Value::as_str)
            .map(|s| s.trim().to_ascii_uppercase())
            .unwrap_or_default();
        let last_cur = im
            .get("last_currency")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_ascii_uppercase();
        let paid_delta = match (
            paid_cents,
            im.get("last_price_cents").and_then(Value::as_i64),
        ) {
            (Some(paid), Some(last)) if !paid_currency.is_empty() && paid_currency == last_cur => {
                Some(last - paid)
            }
            _ => None,
        };
        let mut row = im.clone();
        row.insert("history".into(), Value::Array(hist));
        row.insert(
            "lowest_retained_cents".into(),
            lowest.map(|v| json!(v)).unwrap_or(Value::Null),
        );
        row.insert(
            "price_age_hours".into(),
            age_hours.map(|v| json!(v)).unwrap_or(Value::Null),
        );
        row.insert(
            "library_game_id".into(),
            Value::String(
                linked
                    .and_then(|g| g.get("game_id"))
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned(),
            ),
        );
        row.insert(
            "library_paid_cents".into(),
            paid_cents.map(|v| json!(v)).unwrap_or(Value::Null),
        );
        row.insert("library_paid_currency".into(), Value::String(paid_currency));
        row.insert(
            "paid_delta_cents".into(),
            paid_delta.map(|v| json!(v)).unwrap_or(Value::Null),
        );
        enriched.push(Value::Object(row));
    }
    json!({
        "available": true,
        "watchlist": enriched,
        "last_poll_at": deals.and_then(|d| d.get("last_poll_at")).and_then(Value::as_str).unwrap_or(""),
    })
}

pub fn deals_due(state: &Value, settings: &Value) -> bool {
    if !settings
        .get("deals_enabled")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        return false;
    }
    let has_steam_watch = state
        .get("deals")
        .and_then(|d| d.get("watchlist"))
        .and_then(Value::as_array)
        .is_some_and(|wl| {
            wl.iter().any(|r| {
                !r.get("manual").and_then(Value::as_bool).unwrap_or(false)
                    && r.get("appid")
                        .and_then(Value::as_str)
                        .is_some_and(|s| !s.is_empty())
            })
        });
    if !has_steam_watch {
        return false;
    }
    let poll_secs = clamp_poll_hours(settings.get("deals_poll_hours")) * 3600;
    let raw_last = state
        .get("deals")
        .and_then(|d| d.get("last_poll_at"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if raw_last.is_empty() {
        return true;
    }
    let Ok(last_ts) = raw_last.parse::<Timestamp>() else {
        return true;
    };
    (Timestamp::now().as_second() - last_ts.as_second()) >= poll_secs
}

pub fn refresh_deals_pass<F>(
    data_dir: &Path,
    state_snapshot: &Value,
    settings: &Value,
    cancel: Option<&AtomicBool>,
    mut commit_fn: F,
) -> Value
where
    F: FnMut(Box<dyn FnOnce(&mut Value) -> Value + Send>) -> Result<Value, String>,
{
    if !settings
        .get("deals_enabled")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        return json!({"polled": 0, "notified": 0, "errors": []});
    }
    let entries: Vec<Value> = state_snapshot
        .get("deals")
        .and_then(|d| d.get("watchlist"))
        .and_then(Value::as_array)
        .map(|wl| {
            wl.iter()
                .filter(|r| {
                    r.is_object()
                        && !r.get("manual").and_then(Value::as_bool).unwrap_or(false)
                        && r.get("appid")
                            .and_then(Value::as_str)
                            .is_some_and(|s| !s.is_empty())
                })
                .cloned()
                .collect()
        })
        .unwrap_or_default();
    let cc = normalize_cc(
        settings
            .get("deals_store_cc")
            .and_then(Value::as_str)
            .unwrap_or(DEFAULT_STORE_CC),
    );
    let ttl = (clamp_poll_hours(settings.get("deals_poll_hours")) * 3600) as u64;
    let total = entries.len();
    let mut polled = 0usize;
    let mut notified = 0usize;
    let mut errors: Vec<String> = Vec::new();
    let mut cancelled = false;

    for (idx, item) in entries.iter().enumerate() {
        if cancel.is_some_and(|c| c.load(Ordering::Acquire)) {
            cancelled = true;
            break;
        }
        let did = item
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        let appid = item
            .get("appid")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        let name = item
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or(&appid)
            .to_owned();
        match fetch_steam_appdetails(data_dir, &appid, &cc, ttl, true) {
            Ok(obs) => {
                let did_c = did.clone();
                let cfg_c = settings.clone();
                let res = commit_fn(Box::new(move |current| {
                    let cur_settings = current.get("settings").cloned().unwrap_or(cfg_c);
                    apply_poll_observation(current, &did_c, &obs, &cur_settings)
                }));
                if let Ok(outcome) = res {
                    polled += 1;
                    if outcome
                        .get("notified")
                        .and_then(Value::as_bool)
                        .unwrap_or(false)
                    {
                        notified += 1;
                    }
                }
            }
            Err(err) => {
                let msg: String = err.message.chars().take(200).collect();
                errors.push(format!("{name}: {msg}"));
                let did_c = did.clone();
                let _ = commit_fn(Box::new(move |current| {
                    let deals = ensure_deals_block(current);
                    if let Some(row) = deals["watchlist"]
                        .as_array_mut()
                        .unwrap()
                        .iter_mut()
                        .find(|r| r.get("id").and_then(Value::as_str) == Some(did_c.as_str()))
                    {
                        row["fetch_error"] = Value::String(msg);
                    }
                    Value::Bool(false)
                }));
            }
        }
        if idx + 1 < total {
            std::thread::sleep(Duration::from_secs(DEFAULT_SPACING_SECS));
        }
    }
    if !cancelled {
        let stamp = utc_iso(None);
        let _ = commit_fn(Box::new(move |current| {
            let deals = ensure_deals_block(current);
            deals.insert("last_poll_at".into(), Value::String(stamp.clone()));
            Value::String(stamp)
        }));
    }
    errors.truncate(20);
    json!({
        "polled": polled,
        "notified": notified,
        "errors": errors,
        "cancelled": cancelled,
    })
}

pub fn deals_compat_section(state: &Value, settings: &Value) -> Value {
    let count = state
        .get("deals")
        .and_then(|d| d.get("watchlist"))
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0);
    json!({
        "enabled": settings.get("deals_enabled").and_then(Value::as_bool).unwrap_or(false),
        "host_allowlist": [ALLOWED_HOST],
        "payload": "appid_and_search_query_only",
        "watchlist_count": count,
    })
}
