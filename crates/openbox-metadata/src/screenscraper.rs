//! Optional ScreenScraper metadata/media provider (`pkg/parity/parity_screenscraper.py`).

use std::collections::{BTreeMap, HashMap};
use std::fs::{self, File};
use std::io::Read;
use std::net::IpAddr;
use std::path::Path;
use std::sync::LazyLock;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use crc32fast::Hasher as Crc32Hasher;
use md5::{Digest as _, Md5};
use openbox_core::APP_VERSION;
use openbox_io::{ensure_env_loaded, env_value, is_public_ip, validate_http_url};
use parking_lot::Mutex;
use serde_json::{Map, Value, json};
use sha1::Sha1;

use crate::http::{http_get_raw, py_round, urlencode};

pub const SS_ENDPOINT: &str = "https://www.screenscraper.fr/api2";
pub const SS_CACHE_TTL_SECS: u64 = 30 * 24 * 3600;
const MIN_REQUEST_INTERVAL: Duration = Duration::from_secs(1);
const HASH_SIZE_LIMIT: u64 = 512 * 1024 * 1024;
const MAX_RETRIES: usize = 3;

pub const HASH_TIER_DUAL: &str = "dual";
pub const HASH_TIER_SINGLE: &str = "single";
pub const HASH_TIER_TITLE: &str = "title";
pub const HASH_TIER_NONE: &str = "none";

static REQUEST_LOCK: LazyLock<Mutex<Option<Instant>>> = LazyLock::new(|| Mutex::new(None));

pub fn system_id_for_platform(platform: &str) -> Option<i64> {
    match platform.trim().to_lowercase().as_str() {
        "arcade" => Some(11),
        "dreamcast" => Some(23),
        "game boy" => Some(9),
        "game boy advance" => Some(12),
        "game boy color" => Some(10),
        "gamecube" => Some(13),
        "nes" => Some(3),
        "nintendo 64" => Some(14),
        "playstation" => Some(2),
        "playstation 2" => Some(15),
        "playstation 3" => Some(65),
        "playstation vita" => Some(68),
        "psp" => Some(61),
        "sega saturn" => Some(17),
        "scummvm" => Some(123),
        "snes" => Some(4),
        "switch" | "nintendo switch" => Some(225),
        "wii" => Some(16),
        "wii u" => Some(18),
        "xbox" => Some(29),
        "xbox 360" => Some(32),
        _ => None,
    }
}

fn region_code_for(name: &str) -> Option<&'static str> {
    match name.trim().to_lowercase().as_str() {
        "world" => Some("wor"),
        "north america" => Some("us"),
        "europe" => Some("eu"),
        "japan" => Some("jp"),
        "australia" => Some("au"),
        "brazil" => Some("br"),
        "canada" => Some("ca"),
        "china" => Some("cn"),
        "france" => Some("fr"),
        "germany" => Some("de"),
        "italy" => Some("it"),
        "korea" => Some("ko"),
        "netherlands" => Some("nl"),
        "spain" => Some("es"),
        "sweden" => Some("se"),
        "united kingdom" => Some("uk"),
        _ => None,
    }
}

pub fn region_codes(region_priority: Option<&[String]>) -> Vec<String> {
    let mut codes = Vec::new();
    if let Some(list) = region_priority {
        for name in list {
            if let Some(code) = region_code_for(name)
                && !codes.iter().any(|c| c == code)
            {
                codes.push(code.to_owned());
            }
        }
    }
    if codes.is_empty() {
        vec![
            "wor".to_owned(),
            "us".to_owned(),
            "eu".to_owned(),
            "jp".to_owned(),
        ]
    } else {
        codes
    }
}

fn media_kind_for_ss_type(ss_type: &str) -> &'static str {
    match ss_type {
        "box2D" => "cover",
        "boxback" => "box_back",
        "boxspine" => "box_spine",
        "box3D" => "box_3d",
        "fanart" => "fanart",
        "banner" => "banner",
        "cart" => "cart_front",
        "cart2" => "cart_back",
        "disc" => "disc",
        "image" => "title_screen",
        "logo" => "clear_logo",
        "flyer" => "advertisement",
        "video" => "video_snap",
        "manual" => "manual",
        _ => "",
    }
}

pub fn credentials() -> Result<(String, String, String, String), String> {
    ensure_env_loaded(None);
    let user = env_value(&["SCREENSCRAPER_USER", "OPENBOX_SCREENSCRAPER_USER"]);
    let password = env_value(&["SCREENSCRAPER_PASSWORD", "OPENBOX_SCREENSCRAPER_PASSWORD"]);
    if user.is_empty() || password.is_empty() {
        return Err(
            "Set SCREENSCRAPER_USER and SCREENSCRAPER_PASSWORD in ~/.env to use ScreenScraper."
                .to_owned(),
        );
    }
    let dev_id = env_value(&["SCREENSCRAPER_DEV_ID", "OPENBOX_SCREENSCRAPER_DEV_ID"]);
    let dev_password = env_value(&[
        "SCREENSCRAPER_DEV_PASSWORD",
        "OPENBOX_SCREENSCRAPER_DEV_PASSWORD",
    ]);
    if dev_id.is_empty() || dev_password.is_empty() {
        return Err(
            "Set SCREENSCRAPER_DEV_ID and SCREENSCRAPER_DEV_PASSWORD in ~/.env. ScreenScraper requires a developer ID and password for its API."
                .to_owned(),
        );
    }
    Ok((user, password, dev_id, dev_password))
}

pub fn is_configured() -> bool {
    credentials().is_ok()
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

pub fn ss_request(endpoint: &str, params: &BTreeMap<String, String>) -> Result<Value, String> {
    let (user, password, dev_id, dev_password) = credentials()?;
    let mut query: Vec<(&str, String)> = params
        .iter()
        .filter(|(_, v)| !v.is_empty())
        .map(|(k, v)| (k.as_str(), v.clone()))
        .collect();
    query.push(("devid", dev_id));
    query.push(("devpassword", dev_password));
    query.push(("softname", format!("OpenBox-{APP_VERSION}")));
    query.push(("ssid", user));
    query.push(("sspassword", password));
    query.push(("output", "json".to_owned()));
    let url = format!("{SS_ENDPOINT}/{endpoint}?{}", urlencode(&query));
    let ua = format!("OpenBox/{APP_VERSION}");
    let mut last_error = String::new();

    for attempt in 0..MAX_RETRIES {
        throttle();
        match http_get_raw(&url, &[("User-Agent", ua.clone())], 30, 8 * 1024 * 1024) {
            Ok(resp) => {
                if resp.status >= 400 {
                    let snippet = String::from_utf8_lossy(&resp.body[..resp.body.len().min(200)])
                        .trim()
                        .to_owned();
                    last_error = if snippet.is_empty() {
                        format!("HTTP {}", resp.status)
                    } else {
                        format!("HTTP {} ({snippet})", resp.status)
                    };
                    if matches!(resp.status, 429 | 500 | 502 | 503 | 504)
                        && attempt + 1 < MAX_RETRIES
                    {
                        std::thread::sleep(Duration::from_millis(1500 * (attempt as u64 + 1)));
                        continue;
                    }
                    break;
                }
                match serde_json::from_slice::<Value>(&resp.body) {
                    Ok(v) => return Ok(v),
                    Err(_) => {
                        let text = String::from_utf8_lossy(&resp.body);
                        let trimmed = text.trim();
                        let detail: String = trimmed.chars().take(200).collect();
                        return Err(format!(
                            "ScreenScraper request failed: {}",
                            if detail.is_empty() {
                                "empty response"
                            } else {
                                &detail
                            }
                        ));
                    }
                }
            }
            Err(err) => {
                last_error = err;
                if attempt + 1 < MAX_RETRIES {
                    std::thread::sleep(Duration::from_millis(1500 * (attempt as u64 + 1)));
                    continue;
                }
            }
        }
    }
    Err(format!("ScreenScraper request failed: {last_error}"))
}

fn cache_key(params: &BTreeMap<String, String>) -> String {
    let pairs: Vec<(&str, String)> = params
        .iter()
        .map(|(k, v)| (k.as_str(), v.clone()))
        .collect();
    let canonical = urlencode(&pairs);
    let mut h = Sha1::new();
    h.update(canonical.as_bytes());
    hex::encode(h.finalize())
}

pub fn cache_get(cache_dir: &Path, params: &BTreeMap<String, String>) -> Option<Value> {
    let path = cache_dir
        .join("screenscraper")
        .join(format!("{}.json", cache_key(params)));
    let meta = fs::metadata(&path).ok()?;
    let mtime = meta.modified().ok()?;
    let age = SystemTime::now().duration_since(mtime).ok()?.as_secs();
    if age >= SS_CACHE_TTL_SECS {
        return None;
    }
    let raw = fs::read_to_string(&path).ok()?;
    serde_json::from_str(&raw).ok()
}

pub fn cache_put(cache_dir: &Path, params: &BTreeMap<String, String>, payload: &Value) {
    let dir = cache_dir.join("screenscraper");
    if fs::create_dir_all(&dir).is_ok()
        && let Ok(text) = serde_json::to_string(payload)
    {
        let _ = fs::write(dir.join(format!("{}.json", cache_key(params))), text);
    }
}

pub fn cache_size(cache_dir: &Path) -> usize {
    let dir = cache_dir.join("screenscraper");
    fs::read_dir(&dir)
        .map(|rd| rd.flatten().filter(|e| e.path().is_file()).count())
        .unwrap_or(0)
}

pub fn user_info(cache_dir: Option<&Path>) -> Result<Value, String> {
    let mut key = BTreeMap::new();
    key.insert("endpoint".to_owned(), "ssuserInfos.php".to_owned());
    if let Some(cd) = cache_dir
        && let Some(c) = cache_get(cd, &key)
    {
        return Ok(c);
    }
    let payload = ss_request("ssuserInfos.php", &BTreeMap::new())?;
    if let Some(cd) = cache_dir {
        cache_put(cd, &key, &payload);
    }
    Ok(payload)
}

pub fn search_games(
    name: &str,
    system_id: Option<i64>,
    limit: usize,
) -> Result<Vec<Value>, String> {
    let trimmed = name.trim();
    if trimmed.is_empty() {
        return Ok(Vec::new());
    }
    let cap = limit.clamp(1, 50);
    let mut params = BTreeMap::new();
    params.insert("recherche".to_owned(), trimmed.to_owned());
    if let Some(sid) = system_id {
        params.insert("systemeid".to_owned(), sid.to_string());
    }
    params.insert("st".to_owned(), "0".to_owned());
    let payload = ss_request("jeuRecherche.php", &params)?;
    let rows = payload
        .get("response")
        .and_then(|r| r.get("jeux"))
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let mut out = Vec::new();
    for row in rows.iter().take(cap) {
        if !row.is_object() {
            continue;
        }
        out.push(json!({
            "id": row.get("id").cloned().unwrap_or(Value::Null),
            "name": row.get("name").and_then(Value::as_str).unwrap_or(""),
            "system_id": row.get("systemid").cloned().unwrap_or(Value::Null),
            "system_name": row.get("systemname").and_then(Value::as_str).unwrap_or(""),
            "year": row.get("year").map(|v| match v {
                Value::String(s) => s.clone(),
                Value::Null => String::new(),
                other => other.to_string(),
            }).unwrap_or_default(),
        }));
    }
    Ok(out)
}

pub struct RomHashes {
    pub md5: String,
    pub sha1: String,
    pub crc: String,
    pub size: u64,
}

pub fn hash_rom(path: &Path) -> Result<RomHashes, String> {
    if !path.is_file() {
        let name = path
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        return Err(format!("ROM file not found: {name}"));
    }
    let size = fs::metadata(path).map_err(|e| e.to_string())?.len();
    if size > HASH_SIZE_LIMIT {
        return Err("ROM file is too large to hash for scraping.".to_owned());
    }
    let mut file = File::open(path).map_err(|e| e.to_string())?;
    let mut md5 = Md5::new();
    let mut sha1 = Sha1::new();
    let mut crc = Crc32Hasher::new();
    let mut buf = vec![0u8; 1024 * 1024];
    loop {
        let n = file.read(&mut buf).map_err(|e| e.to_string())?;
        if n == 0 {
            break;
        }
        let slice = &buf[..n];
        md5.update(slice);
        sha1.update(slice);
        crc.update(slice);
    }
    Ok(RomHashes {
        md5: hex::encode(md5.finalize()),
        sha1: hex::encode(sha1.finalize()),
        crc: format!("{:08x}", crc.finalize()),
        size,
    })
}

pub fn rom_type_for(path: &Path) -> &'static str {
    let ext = path
        .extension()
        .and_then(|e| e.to_str())
        .map(|s| format!(".{}", s.to_ascii_lowercase()))
        .unwrap_or_default();
    if matches!(ext.as_str(), ".iso" | ".gcm") {
        "iso"
    } else {
        "rom"
    }
}

fn rom_params(
    rom_path: &Path,
    hashes: &RomHashes,
    hash_kinds: &[&str],
) -> BTreeMap<String, String> {
    let mut params = BTreeMap::new();
    params.insert("romtype".to_owned(), rom_type_for(rom_path).to_owned());
    params.insert(
        "romnom".to_owned(),
        rom_path
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default(),
    );
    params.insert("romtaille".to_owned(), hashes.size.to_string());
    for kind in hash_kinds {
        match *kind {
            "md5" => {
                params.insert("md5".to_owned(), hashes.md5.to_ascii_uppercase());
            }
            "sha1" => {
                params.insert("sha1".to_owned(), hashes.sha1.to_ascii_uppercase());
            }
            "crc" => {
                params.insert("crc".to_owned(), hashes.crc.to_ascii_uppercase());
            }
            _ => {}
        }
    }
    params
}

fn ss_text(node: Option<&Value>) -> String {
    match node {
        Some(Value::Object(m)) => m
            .get("text")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_owned(),
        Some(Value::String(s)) => s.trim().to_owned(),
        Some(Value::Number(n)) => n.to_string(),
        _ => String::new(),
    }
}

fn ss_rating(node: Option<&Value>) -> Value {
    let s = ss_text(node);
    if s.is_empty() {
        return Value::Null;
    }
    match s.parse::<f64>() {
        Ok(v) => json!(py_round(v / 20.0, 2)),
        Err(_) => Value::Null,
    }
}

pub fn normalize_jeu(payload: &Value) -> Value {
    let jeu = payload
        .get("response")
        .and_then(|r| r.get("jeu"))
        .and_then(Value::as_object);
    let empty = Map::new();
    let j = jeu.unwrap_or(&empty);
    let mut media_entries = Vec::new();
    if let Some(medias) = j.get("medias").and_then(Value::as_array) {
        for media in medias {
            let Some(m) = media.as_object() else {
                continue;
            };
            let url = m.get("url").and_then(Value::as_str).unwrap_or("").trim();
            if url.is_empty() {
                continue;
            }
            let mtype = m.get("type").and_then(Value::as_str).unwrap_or("");
            let region = m.get("region").and_then(Value::as_str).unwrap_or("");
            let order = m.get("order").and_then(Value::as_i64).unwrap_or(0);
            media_entries.push(json!({
                "type": mtype,
                "kind": media_kind_for_ss_type(mtype),
                "url": url,
                "region": region,
                "order": order,
            }));
        }
    }
    let extra_ss: Vec<Value> = media_entries
        .iter()
        .filter(|e| e.get("type").and_then(Value::as_str) == Some("ss"))
        .map(|e| {
            let mut copy = e.clone();
            copy["kind"] = Value::String("screenshots".to_owned());
            copy
        })
        .collect();
    media_entries.extend(extra_ss);

    let scraper_id: Value = match j.get("id") {
        Some(Value::Number(n)) => n.as_i64().map(|i| json!(i)).unwrap_or(Value::Null),
        Some(Value::String(s)) => s
            .trim()
            .parse::<i64>()
            .map(|i| json!(i))
            .unwrap_or(Value::Null),
        _ => Value::Null,
    };

    json!({
        "id": scraper_id,
        "name": ss_text(j.get("nom")),
        "description": ss_text(j.get("synopsis")),
        "year": ss_text(j.get("year")),
        "developer": ss_text(j.get("developpeur")),
        "publisher": ss_text(j.get("editeur")),
        "genre": ss_text(j.get("genre")),
        "players": ss_text(j.get("players")),
        "rating": ss_rating(j.get("note")),
        "region": ss_text(j.get("region")),
        "media": media_entries,
    })
}

pub fn game_info(
    game_id: Option<i64>,
    rom_path: Option<&Path>,
    system_id: Option<i64>,
    cache_dir: Option<&Path>,
) -> Result<Value, String> {
    let mut params = BTreeMap::new();
    if let Some(gid) = game_id {
        params.insert("gameid".to_owned(), gid.to_string());
    } else if let Some(rp) = rom_path {
        let hashes = hash_rom(rp)?;
        params.extend(rom_params(rp, &hashes, &["md5", "sha1", "crc"]));
    } else {
        return Err("game_info needs a game id or a ROM path.".to_owned());
    }
    if let Some(sid) = system_id {
        params.insert("systemeid".to_owned(), sid.to_string());
    }
    if let Some(cd) = cache_dir
        && let Some(cached) = cache_get(cd, &params)
    {
        return Ok(normalize_jeu(&cached));
    }
    let payload = ss_request("jeuInfos.php", &params)?;
    let has_jeu = payload
        .get("response")
        .and_then(|r| r.get("jeu"))
        .and_then(Value::as_object)
        .is_some_and(|m| !m.is_empty());
    if !has_jeu {
        return Err("Game not found on ScreenScraper.".to_owned());
    }
    if let Some(cd) = cache_dir {
        cache_put(cd, &params, &payload);
    }
    Ok(normalize_jeu(&payload))
}

pub fn auto_apply_allowed(tier: &str) -> bool {
    tier == HASH_TIER_DUAL
}

pub fn hash_lookup(
    rom_path: &Path,
    hashes: &RomHashes,
    hash_kind: &str,
    system_id: Option<i64>,
    cache_dir: Option<&Path>,
) -> Result<Value, String> {
    if !matches!(hash_kind, "md5" | "crc") {
        return Err("hash_kind must be 'md5' or 'crc'.".to_owned());
    }
    let mut params = rom_params(rom_path, hashes, &[hash_kind]);
    if let Some(sid) = system_id {
        params.insert("systemeid".to_owned(), sid.to_string());
    }
    if let Some(cd) = cache_dir
        && let Some(cached) = cache_get(cd, &params)
    {
        return Ok(normalize_jeu(&cached));
    }
    let payload = ss_request("jeuInfos.php", &params)?;
    let has_jeu = payload
        .get("response")
        .and_then(|r| r.get("jeu"))
        .and_then(Value::as_object)
        .is_some_and(|m| !m.is_empty());
    if !has_jeu {
        return Err("Game not found on ScreenScraper.".to_owned());
    }
    if let Some(cd) = cache_dir {
        cache_put(cd, &params, &payload);
    }
    Ok(normalize_jeu(&payload))
}

pub fn hash_tier(md5_meta: Option<&Value>, crc_meta: Option<&Value>) -> &'static str {
    let md5_id = md5_meta.and_then(|v| v.get("id")).and_then(Value::as_i64);
    let crc_id = crc_meta.and_then(|v| v.get("id")).and_then(Value::as_i64);
    match (md5_id, crc_id) {
        (Some(a), Some(b)) if a == b => HASH_TIER_DUAL,
        (Some(_), _) | (_, Some(_)) => HASH_TIER_SINGLE,
        _ => HASH_TIER_NONE,
    }
}

pub fn confident_hash_match(
    rom_path: &Path,
    system_id: Option<i64>,
    cache_dir: Option<&Path>,
) -> Result<(Value, &'static str), String> {
    let hashes = hash_rom(rom_path)?;
    let md5_meta = hash_lookup(rom_path, &hashes, "md5", system_id, cache_dir).ok();
    let crc_meta = hash_lookup(rom_path, &hashes, "crc", system_id, cache_dir).ok();
    let tier = hash_tier(md5_meta.as_ref(), crc_meta.as_ref());
    let chosen = md5_meta
        .or(crc_meta)
        .ok_or_else(|| "Game not found on ScreenScraper.".to_owned())?;
    Ok((chosen, tier))
}

pub fn choose_media(
    metadata: &Value,
    media_kinds: &[String],
    region_priority: Option<&[String]>,
    limit: usize,
) -> HashMap<String, Value> {
    let codes = region_codes(region_priority);
    let order: HashMap<&str, usize> = codes
        .iter()
        .enumerate()
        .map(|(i, c)| (c.as_str(), i))
        .collect();
    let mut chosen: HashMap<String, ((usize, i64), String)> = HashMap::new();
    let mut screenshots: Vec<(usize, i64, String)> = Vec::new();

    if let Some(arr) = metadata.get("media").and_then(Value::as_array) {
        for entry in arr {
            let kind = entry.get("kind").and_then(Value::as_str).unwrap_or("");
            if !media_kinds.iter().any(|k| k == kind) && kind != "screenshots" {
                continue;
            }
            let region = entry.get("region").and_then(Value::as_str).unwrap_or("");
            let ord = entry.get("order").and_then(Value::as_i64).unwrap_or(0);
            let url = entry.get("url").and_then(Value::as_str).unwrap_or("");
            let rank = (*order.get(region).unwrap_or(&99), ord);
            if kind == "screenshots" {
                screenshots.push((rank.0, rank.1, url.to_owned()));
                continue;
            }
            match chosen.get(kind) {
                Some((cur_rank, _)) if rank >= *cur_rank => {}
                _ => {
                    chosen.insert(kind.to_owned(), (rank, url.to_owned()));
                }
            }
        }
    }

    let mut result: HashMap<String, Value> = chosen
        .into_iter()
        .map(|(k, (_, u))| (k, Value::String(u)))
        .collect();
    if media_kinds.iter().any(|k| k == "screenshots") {
        screenshots.sort_by_key(|(r, o, _)| (*r, *o));
        let urls: Vec<Value> = screenshots
            .into_iter()
            .take(limit)
            .map(|(_, _, u)| Value::String(u))
            .collect();
        result.insert("screenshots".to_owned(), Value::Array(urls));
    }
    result
}

pub fn apply_to_game(game: &mut Map<String, Value>, metadata: &Value, fields: &[String]) {
    for field in fields {
        if let Some(v) = metadata.get(field) {
            let empty = match v {
                Value::Null => true,
                Value::String(s) => s.is_empty(),
                _ => false,
            };
            if !empty {
                game.insert(field.clone(), v.clone());
            }
        }
    }
    if let Some(r) = metadata.get("rating").filter(|v| !v.is_null()) {
        game.insert("rating".into(), r.clone());
    }
    if let Some(id) = metadata.get("id").filter(|v| !v.is_null()) {
        game.insert("screenscraper_id".into(), id.clone());
    }
}

pub fn clean_media_url(url: &str) -> String {
    let candidate = url.trim();
    if !candidate.to_ascii_lowercase().starts_with("https://") {
        return String::new();
    }
    let Ok(parsed) = validate_http_url(candidate) else {
        return String::new();
    };
    let host = parsed.host_str().unwrap_or("").trim().to_ascii_lowercase();
    let host = host.trim_end_matches('.');
    if host.is_empty() || host == "localhost" || host.ends_with(".localhost") {
        return String::new();
    }
    let bare = host.trim_start_matches('[').trim_end_matches(']');
    if let Ok(ip) = bare.split('%').next().unwrap_or(bare).parse::<IpAddr>() {
        if is_public_ip(ip) {
            parsed.to_string()
        } else {
            String::new()
        }
    } else {
        parsed.to_string()
    }
}

pub fn now_unix_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}
