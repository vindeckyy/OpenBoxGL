//! EmuMovies media provider: credentials, search, and bounded download
//! (`pkg/parity/parity_emumovies.py` + `parity_integrations.py`).

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;
use std::time::{Duration, Instant};

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use openbox_io::{atomic_write_text, emumovies_from_env};
use openbox_platform::contained_path;
use parking_lot::Mutex;
use regex::Regex;
use serde_json::{Value, json};

use crate::http::{download_file_sync, http_get_raw, urlencode};

pub const EMUMOVIES_ENDPOINT: &str = "https://api.emumovies.com/v1/media";
const MAX_RETRIES: usize = 2;
const RETRY_DELAYS_SECS: &[u64] = &[2, 8];
const RATE_LIMIT: Duration = Duration::from_secs(1);

static REQUEST_LOCK: LazyLock<Mutex<Option<Instant>>> = LazyLock::new(|| Mutex::new(None));
static SLUG_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[^a-z0-9_-]+").unwrap());
static SAFE_EXT_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\.[a-z0-9]{1,8}$").unwrap());
static HTTPS_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)^https://[^/\s]+/").unwrap());

pub fn endpoint_kind(kind: &str) -> Option<&'static str> {
    match kind {
        "cover" | "box_front" => Some("box"),
        "box_back" => Some("boxback"),
        "box_spine" => Some("boxspine"),
        "box_3d" => Some("box3d"),
        "screenshots" => Some("snap"),
        "title_screen" => Some("title"),
        "banner" => Some("marquee"),
        "cart_front" => Some("cart"),
        "cart_back" => Some("cartback"),
        "disc" => Some("disc"),
        "clear_logo" => Some("logo"),
        "video_snap" | "video_theme" => Some("video"),
        "manual" => Some("manual"),
        "music" => Some("music"),
        _ => None,
    }
}

pub fn compat_canonical_kind(media_type: &str) -> Option<&'static str> {
    match media_type.trim().to_lowercase().as_str() {
        "cover" | "box_front" | "box" | "boxfront" => Some("cover"),
        "box_back" | "boxback" => Some("box_back"),
        "box_spine" | "boxspine" => Some("box_spine"),
        "box_3d" | "box3d" => Some("box_3d"),
        "screenshots" | "snap" | "screenshot" => Some("screenshots"),
        "title_screen" | "titlescreen" | "title" => Some("title_screen"),
        "banner" | "marquee" => Some("banner"),
        "cart_front" | "cart" | "cartfront" => Some("cart_front"),
        "cart_back" | "cartback" => Some("cart_back"),
        "disc" => Some("disc"),
        "clear_logo" | "logo" | "clearlogo" => Some("clear_logo"),
        "video_snap" | "video" | "snapvideo" => Some("video_snap"),
        "video_theme" => Some("video_theme"),
        "manual" => Some("manual"),
        "music" => Some("music"),
        _ => None,
    }
}

pub fn emumovies_type_field(media_type: &str) -> Option<&'static str> {
    match media_type.trim().to_lowercase().as_str() {
        "box" | "box_front" | "cover" => Some("cover"),
        "boxback" | "box_back" => Some("box_back"),
        "boxspine" | "box_spine" => Some("box_spine"),
        "box3d" | "box_3d" => Some("box_3d"),
        "snap" | "screenshot" | "screenshots" => Some("screenshots"),
        "title" | "titlescreen" | "title_screen" => Some("title_screen"),
        "marquee" | "banner" => Some("banner"),
        "cart" | "cartfront" | "cart_front" => Some("cart_front"),
        "cartback" | "cart_back" => Some("cart_back"),
        "disc" => Some("disc"),
        "logo" | "clearlogo" | "clear_logo" => Some("clear_logo"),
        "fanart" => Some("fanart"),
        "background" => Some("background"),
        "icon" => Some("icon"),
        "manual" => Some("manual"),
        "music" => Some("music"),
        "video" | "video_snap" | "snapvideo" => Some("video_snap"),
        "advertisement" => Some("advertisement"),
        _ => None,
    }
}

pub fn load_emumovies_credentials(data_dir: &Path) -> (String, String) {
    let path = data_dir.join("emumovies.json");
    if path.is_file()
        && let Ok(raw) = fs::read_to_string(&path)
        && let Ok(val) = serde_json::from_str::<Value>(&raw)
    {
        let user = val
            .get("username")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_owned();
        let pass = val
            .get("password")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_owned();
        if !user.is_empty() {
            return (user, pass);
        }
    }
    if let Some((u, p)) = emumovies_from_env() {
        return (u, p);
    }
    (String::new(), String::new())
}

pub fn save_emumovies_credentials(
    data_dir: &Path,
    username: &str,
    password: &str,
) -> Result<Value, String> {
    let path = data_dir.join("emumovies.json");
    let body = serde_json::to_string_pretty(&json!({
        "username": username,
        "password": password,
    }))
    .map_err(|e| e.to_string())?;
    atomic_write_text(&path, &body, 0o600).map_err(|e| e.to_string())?;
    Ok(json!({"configured": true}))
}

fn throttle() {
    let mut guard = REQUEST_LOCK.lock();
    if let Some(last) = *guard {
        let elapsed = last.elapsed();
        if elapsed < RATE_LIMIT {
            std::thread::sleep(RATE_LIMIT - elapsed);
        }
    }
    *guard = Some(Instant::now());
}

fn basic_auth(username: &str, password: &str) -> Result<String, String> {
    let u = username.trim();
    let p = password.trim();
    if u.is_empty() || p.is_empty() {
        return Err("Configure EmuMovies credentials in Settings first.".to_owned());
    }
    Ok(format!("Basic {}", STANDARD.encode(format!("{u}:{p}"))))
}

pub fn extension_for(content_type: &str, url: &str, kind: &str) -> String {
    match content_type.trim().to_ascii_lowercase().as_str() {
        "image/jpeg" | "image/jpg" => return ".jpg".to_owned(),
        "image/png" => return ".png".to_owned(),
        "image/webp" => return ".webp".to_owned(),
        "image/gif" => return ".gif".to_owned(),
        "video/mp4" => return ".mp4".to_owned(),
        "video/webm" => return ".webm".to_owned(),
        "video/x-matroska" => return ".mkv".to_owned(),
        "audio/mpeg" | "audio/mp3" => return ".mp3".to_owned(),
        "audio/ogg" | "application/ogg" => return ".ogg".to_owned(),
        "application/pdf" => return ".pdf".to_owned(),
        _ => {}
    }
    let suffix = Path::new(url.split('?').next().unwrap_or(url))
        .extension()
        .and_then(|e| e.to_str())
        .map(|s| format!(".{}", s.to_ascii_lowercase()))
        .unwrap_or_default();
    if SAFE_EXT_RE.is_match(&suffix) {
        return suffix;
    }
    match kind {
        "video_snap" | "video_theme" => ".mp4".to_owned(),
        "music" => ".mp3".to_owned(),
        "manual" => ".pdf".to_owned(),
        _ => ".jpg".to_owned(),
    }
}

fn slug(val: &str, fallback: &str) -> String {
    let lower = val.to_lowercase();
    let cleaned = SLUG_RE.replace_all(&lower, "-");
    let trimmed = cleaned.trim_matches(|c| c == '-' || c == '_');
    let chosen = if trimmed.is_empty() {
        fallback
    } else {
        trimmed
    };
    chosen.chars().take(80).collect()
}

pub fn search(game: &Value, kind: &str, username: &str, password: &str) -> Result<Value, String> {
    let Some(ep_kind) = endpoint_kind(kind) else {
        return Err(format!("Unsupported EmuMovies media kind: {kind}"));
    };
    let auth = basic_auth(username, password)?;
    let platform = game
        .get("platform")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .unwrap_or("Arcade");
    let name = game
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    let query = urlencode(&[("system", platform.to_owned()), ("search", name.to_owned())]);
    let url = format!("{EMUMOVIES_ENDPOINT}/{ep_kind}?{query}");
    let mut last_error = String::new();

    for attempt in 0..=MAX_RETRIES {
        throttle();
        match http_get_raw(
            &url,
            &[
                ("Authorization", auth.clone()),
                ("User-Agent", "OpenBox/1".to_owned()),
            ],
            30,
            64 * 1024,
        ) {
            Ok(resp) => {
                if (400..500).contains(&resp.status) {
                    let excerpt = String::from_utf8_lossy(&resp.body[..resp.body.len().min(300)])
                        .trim()
                        .to_owned();
                    return Err(format!(
                        "EmuMovies request failed (HTTP {}): {}",
                        resp.status,
                        if excerpt.is_empty() {
                            "permanent error"
                        } else {
                            &excerpt
                        }
                    ));
                }
                if resp.status >= 500 {
                    last_error = format!("HTTP {}", resp.status);
                    if attempt < MAX_RETRIES {
                        std::thread::sleep(Duration::from_secs(
                            RETRY_DELAYS_SECS[attempt.min(RETRY_DELAYS_SECS.len() - 1)],
                        ));
                        continue;
                    }
                    return Err(format!("EmuMovies request failed: {last_error}"));
                }
                return Ok(json!({
                    "kind": kind,
                    "endpoint_kind": ep_kind,
                    "url": url,
                    "platform": platform,
                    "name": name,
                    "content_type": resp.content_type,
                    "headers": {"Authorization": auth},
                }));
            }
            Err(err) => {
                last_error = err;
                if attempt < MAX_RETRIES {
                    std::thread::sleep(Duration::from_secs(
                        RETRY_DELAYS_SECS[attempt.min(RETRY_DELAYS_SECS.len() - 1)],
                    ));
                    continue;
                }
                return Err(format!("EmuMovies request failed: {last_error}"));
            }
        }
    }
    Err(format!(
        "EmuMovies request failed: {}",
        if last_error.is_empty() {
            "unknown error"
        } else {
            &last_error
        }
    ))
}

fn kind_limits(kind: &str) -> (usize, &'static [&'static str]) {
    match kind {
        "video_snap" | "video_theme" => (512 * 1024 * 1024, &["video/"]),
        "music" => (32 * 1024 * 1024, &["audio/", "application/ogg"]),
        "manual" => (64 * 1024 * 1024, &["application/pdf"]),
        _ => (64 * 1024 * 1024, &["image/"]),
    }
}

pub fn destination_for(candidate: &Value, destination_root: &Path) -> Result<PathBuf, String> {
    let kind = candidate
        .get("kind")
        .and_then(Value::as_str)
        .unwrap_or("cover");
    let platform = slug(
        candidate
            .get("platform")
            .and_then(Value::as_str)
            .unwrap_or(""),
        "unknown-platform",
    );
    let name = slug(
        candidate.get("name").and_then(Value::as_str).unwrap_or(""),
        "unknown-game",
    );
    let ext = extension_for(
        candidate
            .get("content_type")
            .and_then(Value::as_str)
            .unwrap_or(""),
        candidate.get("url").and_then(Value::as_str).unwrap_or(""),
        kind,
    );
    let file_name = format!("{}{ext}", slug(kind, "media"));
    let dest = contained_path(
        destination_root,
        &["emumovies", &platform, &name, &file_name],
    )
    .map_err(|e| e.to_string())?;
    Ok(dest)
}

pub fn download(candidate: &Value, destination_root: &Path) -> Result<String, String> {
    let url = candidate
        .get("url")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if !HTTPS_RE.is_match(url) {
        return Err("EmuMovies media URLs must use HTTPS.".to_owned());
    }
    let kind = candidate
        .get("kind")
        .and_then(Value::as_str)
        .unwrap_or("cover");
    if endpoint_kind(kind).is_none() {
        return Err(format!("Unsupported EmuMovies media kind: {kind}"));
    }
    let dest = destination_for(candidate, destination_root)?;
    let mut headers = vec![("User-Agent", "OpenBox/1".to_owned())];
    if let Some(auth) = candidate
        .get("headers")
        .and_then(|h| h.get("Authorization"))
        .and_then(Value::as_str)
        .map(str::trim)
        && auth.starts_with("Basic ")
    {
        headers.push(("Authorization", auth.to_owned()));
    }
    let (max_bytes, expected) = kind_limits(kind);
    download_file_sync(url, &dest, expected, max_bytes, 30, &headers)
        .map_err(|e| format!("EmuMovies download failed: {e}"))?;
    Ok(dest.to_string_lossy().into_owned())
}

pub fn download_emumovies_media(
    game: &Value,
    username: &str,
    password: &str,
    media_root: &Path,
    media_type: &str,
) -> Result<String, String> {
    let Some(canonical) = compat_canonical_kind(media_type) else {
        return Err(format!("Unsupported EmuMovies media type: {media_type}"));
    };
    let candidate = search(game, canonical, username, password)?;
    download(&candidate, media_root)
}
