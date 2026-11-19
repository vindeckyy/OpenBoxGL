//! Gameyfin self-hosted library client (`pkg/parity/parity_gameyfin.py`).

use std::collections::HashMap;
use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;
use std::time::Duration;

use base64::Engine;
use base64::engine::general_purpose::STANDARD as B64;
use futures_util::StreamExt;
use openbox_io::{fsync_directory, set_mode};
use openbox_metadata::http::{block_on_io, urlencode};
use openbox_platform::{
    expand_user, home_dir, is_group_or_world_writable, owned_by_current_user,
    user_symlink_component,
};
use regex::Regex;
use reqwest::header::{COOKIE, HeaderMap, HeaderValue, SET_COOKIE};
use reqwest::redirect::Policy;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;
use url::Url;

pub const DEFAULT_PROVIDER: &str =
    "org.gameyfin.plugins.download.direct.DirectDownloadPlugin$DirectDownloadProvider";
pub const MAX_DOWNLOAD_BYTES: u64 = 4 * 1024 * 1024 * 1024;

static GAMEYFIN_ID_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[0-9]{1,20}$").unwrap());
static SLUG_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[^a-z0-9]+").unwrap());
static SCHEME_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z][A-Za-z0-9+.-]*://").unwrap());
static DISPOSITION_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#"(?i)filename\*?=(?:UTF-8'')?"?([^";]+)"?"#).unwrap());

fn slug(value: &str) -> String {
    let lowered = value.to_lowercase();
    let replaced = SLUG_RE.replace_all(&lowered, "-");
    let trimmed = replaced.trim_matches('-');
    if trimmed.is_empty() {
        "game".to_owned()
    } else {
        trimmed.to_owned()
    }
}

pub fn validate_gameyfin_id(value: &str) -> Result<String, String> {
    let trimmed = value.trim();
    if !GAMEYFIN_ID_RE.is_match(trimmed) {
        return Err("Gameyfin IDs must be 1 to 20 decimal digits.".to_owned());
    }
    let parsed: u128 = trimmed
        .parse()
        .map_err(|_| "Gameyfin IDs must be 1 to 20 decimal digits.".to_owned())?;
    Ok(parsed.to_string())
}

fn origin_tuple(raw: &str) -> Result<(String, String, u16), String> {
    let parsed =
        Url::parse(raw).map_err(|_| "Gameyfin URL must use HTTP(S) with a host.".to_owned())?;
    let scheme = parsed.scheme().to_lowercase();
    if scheme != "http" && scheme != "https" {
        return Err("Gameyfin URL must use HTTP(S) with a host.".to_owned());
    }
    if !parsed.username().is_empty() || parsed.password().is_some() {
        return Err("Gameyfin URLs may not contain embedded credentials.".to_owned());
    }
    let host = parsed
        .host_str()
        .ok_or_else(|| "Gameyfin URL must use HTTP(S) with a host.".to_owned())?
        .to_lowercase();
    let port = parsed
        .port_or_known_default()
        .ok_or_else(|| "Gameyfin URL has an invalid port.".to_owned())?;
    Ok((scheme, host, port))
}

fn same_origin(base: &str, cand: &str) -> bool {
    match (origin_tuple(base), origin_tuple(cand)) {
        (Ok(a), Ok(b)) => a == b,
        _ => false,
    }
}

pub fn normalize_base_url(url: &str) -> Result<String, String> {
    let mut value = url.trim().trim_end_matches('/').to_owned();
    if value.is_empty() {
        return Err("Gameyfin URL is required.".to_owned());
    }
    if !SCHEME_RE.is_match(&value) {
        value = format!("https://{value}");
    }
    let (scheme, host, _) = origin_tuple(&value)?;
    let allow_http = std::env::var("OPENBOX_ALLOW_HTTP_GAMEYFIN").as_deref() == Ok("1");
    if scheme == "http"
        && !matches!(host.as_str(), "127.0.0.1" | "localhost" | "::1" | "[::1]")
        && !allow_http
    {
        return Err(
            "Gameyfin must use HTTPS unless HTTP is explicitly allowed for this deployment."
                .to_owned(),
        );
    }
    Ok(value)
}

#[derive(Debug, Clone)]
pub struct GameyfinConfig {
    pub url: String,
    pub username: String,
    pub password: String,
    pub install_dir: String,
    pub provider: String,
}

pub fn gameyfin_settings(settings: &Map<String, Value>) -> GameyfinConfig {
    let provider = settings
        .get("gameyfin_provider")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .unwrap_or(DEFAULT_PROVIDER)
        .to_owned();
    GameyfinConfig {
        url: settings
            .get("gameyfin_url")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_owned(),
        username: settings
            .get("gameyfin_username")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_owned(),
        password: settings
            .get("gameyfin_password")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned(),
        install_dir: settings
            .get("gameyfin_install_dir")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_owned(),
        provider,
    }
}

pub struct GameyfinClient {
    base_url: String,
    username: String,
    password: String,
    cookies: HashMap<String, String>,
    logged_in: bool,
    client: reqwest::Client,
}

impl GameyfinClient {
    pub fn new(base_url: &str, username: &str, password: &str) -> Result<Self, String> {
        let norm = normalize_base_url(base_url)?;
        let base_for_redirect = norm.clone();
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(60))
            .redirect(Policy::custom(move |attempt| {
                if attempt.previous().len() >= 10 {
                    return attempt.error("Too many redirects");
                }
                if !same_origin(&base_for_redirect, attempt.url().as_str()) {
                    return attempt.error("Gameyfin redirect leaves the configured origin.");
                }
                attempt.follow()
            }))
            .build()
            .map_err(|e| e.to_string())?;
        Ok(Self {
            base_url: norm,
            username: username.to_owned(),
            password: password.to_owned(),
            cookies: HashMap::new(),
            logged_in: false,
            client,
        })
    }

    fn cookie_header(&self) -> Option<String> {
        if self.cookies.is_empty() {
            return None;
        }
        Some(
            self.cookies
                .iter()
                .map(|(k, v)| format!("{k}={v}"))
                .collect::<Vec<_>>()
                .join("; "),
        )
    }

    fn record_cookies(&mut self, headers: &HeaderMap) {
        for val in headers.get_all(SET_COOKIE) {
            if let Ok(s) = val.to_str()
                && let Some(first) = s.split(';').next()
                && let Some((k, v)) = first.split_once('=')
            {
                self.cookies
                    .insert(k.trim().to_owned(), v.trim().to_owned());
            }
        }
    }

    fn resolve_url(&self, path: &str) -> Result<String, String> {
        if SCHEME_RE.is_match(path) {
            if !same_origin(&self.base_url, path) {
                return Err("Gameyfin request leaves the configured origin.".to_owned());
            }
            return Ok(path.to_owned());
        }
        if path.starts_with('/') {
            return Ok(format!("{}{}", self.base_url, path));
        }
        Err("Gameyfin request paths must be absolute paths.".to_owned())
    }

    fn send_json_or_text(
        &mut self,
        method: reqwest::Method,
        path: &str,
        body: Option<Vec<u8>>,
        content_type: Option<&str>,
    ) -> Result<Value, String> {
        let url = self.resolve_url(path)?;
        let cookie_hdr = self.cookie_header();
        let client = self.client.clone();
        let base_url = self.base_url.clone();
        let (resp_headers, content_type_out, bytes) = block_on_io(async move {
            let mut req = client
                .request(method, &url)
                .header("User-Agent", "OpenBox/Gameyfin")
                .header("Accept", "application/json, */*");
            if let Some(ch) = cookie_hdr
                && let Ok(hv) = HeaderValue::from_str(&ch)
            {
                req = req.header(COOKIE, hv);
            }
            if let Some(ct) = content_type {
                req = req.header("Content-Type", ct);
            }
            if let Some(b) = body {
                req = req.body(b);
            }
            let resp = req
                .send()
                .await
                .map_err(|e| format!("Could not reach Gameyfin: {e}"))?;
            if !same_origin(&base_url, resp.url().as_str()) {
                return Err("Gameyfin response came from an unexpected origin.".to_owned());
            }
            let status = resp.status();
            let headers = resp.headers().clone();
            let ct_resp = headers
                .get("content-type")
                .and_then(|v| v.to_str().ok())
                .unwrap_or("")
                .to_owned();
            if !status.is_success() {
                let text = resp.text().await.unwrap_or_default();
                let detail: String = text.chars().take(400).collect();
                return Err(format!(
                    "Gameyfin request failed ({}): {}",
                    status.as_u16(),
                    if detail.is_empty() {
                        status.canonical_reason().unwrap_or("error").to_owned()
                    } else {
                        detail
                    }
                ));
            }
            let mut out = Vec::new();
            let mut stream = resp.bytes_stream();
            while let Some(chunk) = stream.next().await {
                let data = chunk.map_err(|e| format!("Could not read Gameyfin response: {e}"))?;
                if out.len() + data.len() > 16 * 1024 * 1024 {
                    return Err("Gameyfin response exceeded limit.".to_owned());
                }
                out.extend_from_slice(&data);
            }
            Ok((headers, ct_resp, out))
        })?;
        self.record_cookies(&resp_headers);
        if content_type_out.contains("json")
            || bytes.first().is_some_and(|b| *b == b'{' || *b == b'[')
        {
            let text = String::from_utf8_lossy(&bytes);
            if text.trim().is_empty() {
                return Ok(Value::Null);
            }
            return serde_json::from_str(&text)
                .map_err(|_| "Gameyfin returned invalid JSON.".to_owned());
        }
        Ok(Value::String(String::from_utf8_lossy(&bytes).into_owned()))
    }

    pub fn login(&mut self) -> Result<bool, String> {
        if self.username.is_empty() {
            self.logged_in = true;
            return Ok(false);
        }
        let body = urlencode(&[
            ("username", self.username.clone()),
            ("password", self.password.clone()),
        ]);
        let _ = self.send_json_or_text(
            reqwest::Method::POST,
            "/login",
            Some(body.into_bytes()),
            Some("application/x-www-form-urlencoded"),
        )?;
        self.logged_in = true;
        Ok(true)
    }

    fn ensure_session(&mut self) -> Result<(), String> {
        if !self.logged_in {
            self.login()?;
        }
        Ok(())
    }

    pub fn connect(
        &mut self,
        endpoint: &str,
        method: &str,
        payload: Option<&Value>,
    ) -> Result<Value, String> {
        self.ensure_session()?;
        let path = format!("/connect/{endpoint}/{method}");
        let body = serde_json::to_vec(payload.unwrap_or(&json!({}))).map_err(|e| e.to_string())?;
        match self.send_json_or_text(
            reqwest::Method::POST,
            &path,
            Some(body.clone()),
            Some("application/json"),
        ) {
            Ok(val) => Ok(val),
            Err(_) => {
                self.logged_in = false;
                self.login()?;
                self.send_json_or_text(
                    reqwest::Method::POST,
                    &path,
                    Some(body),
                    Some("application/json"),
                )
            }
        }
    }

    pub fn list_games(&mut self) -> Result<Vec<Value>, String> {
        let res = self.connect("GameEndpoint", "getAll", None)?;
        res.as_array().cloned().ok_or_else(|| {
            "Gameyfin GameEndpoint.getAll returned an unexpected payload.".to_owned()
        })
    }

    pub fn list_providers(&mut self) -> Vec<Value> {
        let default =
            vec![json!({"key": DEFAULT_PROVIDER, "name": "Direct Download", "priority": 1})];
        let Ok(res) = self.connect("DownloadProviderEndpoint", "getProviders", None) else {
            return default;
        };
        let Some(arr) = res.as_array() else {
            return default;
        };
        let providers: Vec<Value> = arr.iter().filter(|v| v.is_object()).cloned().collect();
        if providers.is_empty() {
            default
        } else {
            providers
        }
    }

    pub fn download_game(
        &mut self,
        game_id: &str,
        provider: &str,
        destination: &Path,
    ) -> Result<PathBuf, String> {
        self.ensure_session()?;
        let gid = validate_gameyfin_id(game_id)?;
        let prov = if provider.is_empty() {
            DEFAULT_PROVIDER
        } else {
            provider
        };
        let query = urlencode(&[("provider", prov.to_owned())]);
        let url = format!("{}/download/{}?{}", self.base_url, gid, query);
        let cookie_hdr = self.cookie_header();
        let client = self.client.clone();
        let base_url = self.base_url.clone();
        let dest = destination.to_path_buf();
        fs::create_dir_all(&dest).map_err(|e| e.to_string())?;

        block_on_io(async move {
            let mut req = client
                .get(&url)
                .header("User-Agent", "OpenBox/Gameyfin")
                .header("Accept", "application/octet-stream,*/*");
            if let Some(ch) = cookie_hdr
                && let Ok(hv) = HeaderValue::from_str(&ch)
            {
                req = req.header(COOKIE, hv);
            }
            let resp = req
                .send()
                .await
                .map_err(|e| format!("Could not reach Gameyfin: {e}"))?;
            if !same_origin(&base_url, resp.url().as_str()) {
                return Err("Gameyfin download came from an unexpected origin.".to_owned());
            }
            let status = resp.status();
            if !status.is_success() {
                let text = resp.text().await.unwrap_or_default();
                let detail: String = text.chars().take(400).collect();
                return Err(format!(
                    "Gameyfin request failed ({}): {}",
                    status.as_u16(),
                    detail
                ));
            }
            let headers = resp.headers().clone();
            let declared = headers
                .get("content-length")
                .and_then(|v| v.to_str().ok())
                .map(|s| {
                    s.trim()
                        .parse::<u64>()
                        .map_err(|_| "Gameyfin returned an invalid Content-Length.".to_owned())
                })
                .transpose()?
                .unwrap_or(0);
            if declared > MAX_DOWNLOAD_BYTES {
                return Err("The Gameyfin download is too large.".to_owned());
            }
            let expected_sha = expected_sha256_from_headers(&headers)?;
            let disp = headers
                .get("content-disposition")
                .and_then(|v| v.to_str().ok())
                .unwrap_or("");
            let filename = DISPOSITION_RE
                .captures(disp)
                .and_then(|c| c.get(1))
                .map(|m| {
                    percent_encoding::percent_decode_str(m.as_str())
                        .decode_utf8_lossy()
                        .into_owned()
                })
                .and_then(|s| {
                    Path::new(&s)
                        .file_name()
                        .map(|n| n.to_string_lossy().into_owned())
                })
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| format!("gameyfin-{gid}.bin"));
            let target = dest.join(filename);
            let tmp_path = dest.join(format!(".gameyfin-{gid}.tmp"));

            let mut file = File::create(&tmp_path).map_err(|e| e.to_string())?;
            let mut hasher = Sha256::new();
            let mut total = 0u64;
            let mut stream = resp.bytes_stream();
            while let Some(chunk) = stream.next().await {
                let data = chunk.map_err(|e| {
                    let _ = fs::remove_file(&tmp_path);
                    format!("Gameyfin download failed: {e}")
                })?;
                total += data.len() as u64;
                if total > MAX_DOWNLOAD_BYTES {
                    let _ = fs::remove_file(&tmp_path);
                    return Err("The Gameyfin download is too large.".to_owned());
                }
                hasher.update(&data);
                file.write_all(&data).map_err(|e| {
                    let _ = fs::remove_file(&tmp_path);
                    e.to_string()
                })?;
            }
            if declared > 0 && declared != total {
                let _ = fs::remove_file(&tmp_path);
                return Err("Gameyfin download length did not match its Content-Length.".to_owned());
            }
            let _ = file.sync_all();
            drop(file);
            if !expected_sha.is_empty() {
                let actual = hex::encode(hasher.finalize());
                if !bool::from(actual.as_bytes().ct_eq(expected_sha.as_bytes())) {
                    let _ = fs::remove_file(&tmp_path);
                    return Err("Gameyfin download checksum verification failed.".to_owned());
                }
            }
            let _ = set_mode(&tmp_path, 0o600);
            fs::rename(&tmp_path, &target).map_err(|e| {
                let _ = fs::remove_file(&tmp_path);
                e.to_string()
            })?;
            let _ = set_mode(&target, 0o600);
            fsync_directory(&dest);
            Ok(target)
        })
    }
}

fn expected_sha256_from_headers(headers: &HeaderMap) -> Result<String, String> {
    for name in ["x-checksum-sha256", "x-sha256", "content-sha256"] {
        if let Some(val) = headers.get(name).and_then(|v| v.to_str().ok()) {
            let trimmed = val.trim().to_lowercase();
            if !trimmed.is_empty() {
                if trimmed.len() != 64 || !trimmed.bytes().all(|b| b.is_ascii_hexdigit()) {
                    return Err(format!("Gameyfin returned an invalid {name} checksum."));
                }
                return Ok(trimmed);
            }
        }
    }
    if let Some(digest_hdr) = headers.get("digest").and_then(|v| v.to_str().ok()) {
        for item in digest_hdr.split(',') {
            if let Some((k, v)) = item.split_once('=')
                && k.trim().eq_ignore_ascii_case("sha-256")
            {
                let decoded = B64
                    .decode(v.trim())
                    .map_err(|_| "Gameyfin returned an invalid Digest checksum.".to_owned())?;
                if decoded.len() != 32 {
                    return Err("Gameyfin returned an invalid Digest checksum.".to_owned());
                }
                return Ok(hex::encode(decoded));
            }
        }
    }
    Ok(String::new())
}

fn canonical_install_root(install_root: &Path) -> Result<PathBuf, String> {
    let raw = expand_user(install_root);
    if !raw.is_absolute() {
        return Err("Gameyfin install directory must be absolute.".to_owned());
    }
    if let Some(sym) = user_symlink_component(&raw) {
        return Err(format!(
            "Gameyfin paths may not contain symlinks: {}",
            sym.display()
        ));
    }
    let root = fs::canonicalize(&raw).unwrap_or(raw);
    if root.parent().is_none() {
        return Err("Refusing to use the filesystem root for Gameyfin installs.".to_owned());
    }
    if let Ok(meta) = fs::symlink_metadata(&root)
        && (!meta.is_dir() || !owned_by_current_user(&meta) || is_group_or_world_writable(&meta))
    {
        return Err(
            "Gameyfin install root must be an owner-controlled, non-group-writable directory."
                .to_owned(),
        );
    }
    Ok(root)
}

fn contained_install_path(root: &Path, path: &Path) -> Result<PathBuf, String> {
    let raw = expand_user(path);
    if !raw.is_absolute() {
        return Err("Gameyfin install paths must be absolute.".to_owned());
    }
    if let Some(sym) = user_symlink_component(&raw) {
        return Err(format!(
            "Gameyfin paths may not contain symlinks: {}",
            sym.display()
        ));
    }
    let candidate = fs::canonicalize(&raw).unwrap_or(raw);
    if !candidate.starts_with(root) {
        return Err(format!(
            "Gameyfin path escapes the configured install root: {}",
            candidate.display()
        ));
    }
    if candidate == root {
        return Err("Refusing to use the configured install root as a game directory.".to_owned());
    }
    Ok(candidate)
}

fn platform_from_gameyfin(platforms: Option<&Value>) -> String {
    let Some(first) = platforms.and_then(Value::as_array).and_then(|a| a.first()) else {
        return "PC".to_owned();
    };
    let name = if let Some(obj) = first.as_object() {
        obj.get("name")
            .or_else(|| obj.get("displayName"))
            .or_else(|| obj.get("id"))
            .and_then(Value::as_str)
            .unwrap_or("PC")
            .to_owned()
    } else {
        first.as_str().unwrap_or("PC").to_owned()
    };
    if name.chars().all(|c| !c.is_lowercase()) || name.contains('_') {
        name.replace('_', " ")
            .split_whitespace()
            .map(|w| {
                let mut ch = w.chars();
                match ch.next() {
                    None => String::new(),
                    Some(f) => f.to_uppercase().collect::<String>() + &ch.as_str().to_lowercase(),
                }
            })
            .collect::<Vec<_>>()
            .join(" ")
    } else {
        name
    }
}

pub fn game_from_gameyfin(
    record: &Map<String, Value>,
    install_root: &Path,
    provider: &str,
) -> Result<Map<String, Value>, String> {
    let raw_id = match record.get("id") {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) => n.to_string(),
        _ => return Err("Gameyfin returned an invalid game record.".to_owned()),
    };
    let gid = validate_gameyfin_id(&raw_id)?;
    let root = canonical_install_root(install_root)?;
    let title = record
        .get("title")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .unwrap_or_else(|| format!("Gameyfin {gid}"));
    let platform = platform_from_gameyfin(record.get("platforms"));
    let prefix: String = slug(&title).chars().take(120).collect();
    let prefix = prefix.trim_matches('-');
    let dir_name = format!("{}-{gid}", if prefix.is_empty() { "game" } else { prefix });
    let folder = contained_install_path(&root, &root.join(dir_name))?;
    let installed = if folder.is_dir() {
        fs::read_dir(&folder)
            .ok()
            .is_some_and(|mut rd| rd.next().is_some())
    } else {
        folder.is_file()
    };
    let devs = record
        .get("developers")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>()
                .join(", ")
        })
        .unwrap_or_default();
    let pubs = record
        .get("publishers")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>()
                .join(", ")
        })
        .unwrap_or_default();
    let year: String = record
        .get("release")
        .and_then(Value::as_str)
        .unwrap_or("")
        .chars()
        .take(4)
        .collect();
    let cover = record
        .get("cover")
        .and_then(Value::as_object)
        .and_then(|c| c.get("id"))
        .map(|id| match id {
            Value::String(s) => format!("cover:{s}"),
            other => format!("cover:{other}"),
        })
        .unwrap_or_default();
    let folder_str = folder.to_string_lossy().into_owned();
    let mut out = Map::new();
    out.insert("name".into(), json!(title));
    out.insert("platform".into(), json!(platform));
    out.insert("source".into(), json!("Gameyfin"));
    out.insert("collection".into(), json!("Gameyfin"));
    out.insert(
        "description".into(),
        json!(record.get("summary").and_then(Value::as_str).unwrap_or("")),
    );
    out.insert("developer".into(), json!(devs));
    out.insert("publisher".into(), json!(pubs));
    out.insert("year".into(), json!(year));
    out.insert("path".into(), json!(folder_str));
    out.insert(
        "launch".into(),
        json!(if installed { &folder_str } else { "" }),
    );
    out.insert("install_dir".into(), json!(folder_str));
    out.insert("gameyfin_id".into(), json!(gid));
    out.insert("gameyfin_provider".into(), json!(provider));
    out.insert("store_catalog".into(), json!(true));
    out.insert("store_installed".into(), json!(installed));
    out.insert("owned".into(), json!(true));
    out.insert(
        "notes".into(),
        json!(if installed {
            ""
        } else {
            "Owned on Gameyfin. Install to download locally."
        }),
    );
    out.insert("cover".into(), json!(cover));
    Ok(out)
}

fn resolve_provider(conf: &GameyfinConfig, providers: &[Value]) -> String {
    if providers
        .iter()
        .any(|p| p.get("key").and_then(Value::as_str) == Some(conf.provider.as_str()))
    {
        return conf.provider.clone();
    }
    providers
        .first()
        .and_then(|p| p.get("key").and_then(Value::as_str))
        .unwrap_or(DEFAULT_PROVIDER)
        .to_owned()
}

pub fn default_install_root(conf: &GameyfinConfig) -> PathBuf {
    if conf.install_dir.is_empty() {
        home_dir()
            .unwrap_or_else(|| PathBuf::from("."))
            .join("Games")
            .join("Gameyfin")
    } else {
        PathBuf::from(&conf.install_dir)
    }
}

pub fn catalog_gameyfin(settings: &Map<String, Value>) -> Result<(Vec<Value>, Vec<Value>), String> {
    let conf = gameyfin_settings(settings);
    if conf.url.is_empty() {
        return Err("Configure Gameyfin URL in Settings or Storefronts first.".to_owned());
    }
    let install_root = canonical_install_root(&default_install_root(&conf))?;
    let mut client = GameyfinClient::new(&conf.url, &conf.username, &conf.password)?;
    let providers = client.list_providers();
    let provider = resolve_provider(&conf, &providers);
    let mut catalog = Vec::new();
    for item in client.list_games()? {
        let Some(rec) = item.as_object() else {
            continue;
        };
        if rec.get("id").is_none_or(Value::is_null) {
            continue;
        }
        let Ok(entry) = game_from_gameyfin(rec, &install_root, &provider) else {
            continue;
        };
        let gid = entry["gameyfin_id"].as_str().unwrap_or("");
        let launch = entry["launch"].as_str().unwrap_or("");
        let path = entry["path"].as_str().unwrap_or("");
        catalog.push(json!({
            "id": gid,
            "name": entry["name"],
            "source": "Gameyfin",
            "installed": entry["store_installed"],
            "install_uri": format!("gameyfin://install/{gid}"),
            "path": path,
            "launch": if launch.is_empty() { path } else { launch },
            "install_dir": entry["install_dir"],
            "gameyfin_id": gid,
            "gameyfin_provider": provider,
            "description": entry["description"],
            "platform": entry["platform"],
        }));
    }
    Ok((catalog, providers))
}

pub fn test_gameyfin_connection(settings: &Map<String, Value>) -> Result<Value, String> {
    let conf = gameyfin_settings(settings);
    let mut client = GameyfinClient::new(&conf.url, &conf.username, &conf.password)?;
    let games = client.list_games()?;
    let providers = client.list_providers();
    Ok(json!({
        "ok": true,
        "games": games.len(),
        "providers": providers,
    }))
}

pub fn install_gameyfin_game(
    settings: &Map<String, Value>,
    game_id: &str,
) -> Result<Map<String, Value>, String> {
    let gid = validate_gameyfin_id(game_id)?;
    let conf = gameyfin_settings(settings);
    if conf.url.is_empty() {
        return Err("Configure Gameyfin URL first.".to_owned());
    }
    let install_root = canonical_install_root(&default_install_root(&conf))?;
    fs::create_dir_all(&install_root).map_err(|e| e.to_string())?;
    let _ = set_mode(&install_root, 0o700);

    let mut client = GameyfinClient::new(&conf.url, &conf.username, &conf.password)?;
    let games = client.list_games()?;
    let record = games
        .iter()
        .filter_map(Value::as_object)
        .find(|rec| {
            let rid = match rec.get("id") {
                Some(Value::String(s)) => s.clone(),
                Some(Value::Number(n)) => n.to_string(),
                _ => return false,
            };
            validate_gameyfin_id(&rid).ok().as_deref() == Some(gid.as_str())
        })
        .ok_or_else(|| format!("Gameyfin game {gid} was not found."))?;
    let providers = client.list_providers();
    let provider = resolve_provider(&conf, &providers);
    let mut entry = game_from_gameyfin(record, &install_root, &provider)?;
    let destination = contained_install_path(
        &install_root,
        Path::new(entry["install_dir"].as_str().unwrap_or("")),
    )?;
    let dname = destination
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("game");
    let previous = contained_install_path(
        &install_root,
        &install_root.join(format!(".{dname}.openbox-previous")),
    )?;
    let staging = tempfile::Builder::new()
        .prefix(&format!(".{dname}.openbox-"))
        .tempdir_in(&install_root)
        .map_err(|e| e.to_string())?;
    let staging_path = staging.keep();
    if previous.exists() {
        let _ = if previous.is_dir() {
            fs::remove_dir_all(&previous)
        } else {
            fs::remove_file(&previous)
        };
    }

    let dl_res = client.download_game(&gid, &provider, &staging_path);
    let downloaded = match dl_res {
        Ok(p) => p,
        Err(e) => {
            let _ = fs::remove_dir_all(&staging_path);
            return Err(e);
        }
    };
    let dl_name = downloaded
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("game.bin")
        .to_owned();
    let swap_res: Result<(), std::io::Error> = (|| {
        if destination.exists() {
            fs::rename(&destination, &previous)?;
        }
        fs::rename(&staging_path, &destination)?;
        Ok(())
    })();
    if let Err(err) = swap_res {
        let _ = fs::remove_dir_all(&staging_path);
        if !destination.exists() && previous.exists() {
            let _ = fs::rename(&previous, &destination);
        }
        return Err(err.to_string());
    }
    if previous.exists() {
        let _ = if previous.is_dir() {
            fs::remove_dir_all(&previous)
        } else {
            fs::remove_file(&previous)
        };
    }

    let launch_cand = destination.join(&dl_name);
    let launch_path = if launch_cand.is_file() {
        launch_cand
    } else {
        destination.clone()
    };
    let lp_str = launch_path.to_string_lossy().into_owned();
    entry.insert("path".into(), json!(lp_str));
    entry.insert("launch".into(), json!(lp_str));
    entry.insert("store_installed".into(), json!(true));
    entry.insert(
        "notes".into(),
        json!(format!(
            "Installed from Gameyfin to {}",
            destination.display()
        )),
    );
    Ok(entry)
}

pub fn uninstall_gameyfin_game(
    game: &mut Map<String, Value>,
    install_root: &Path,
) -> Result<Value, String> {
    let root = canonical_install_root(install_root)?;
    let install_dir_text = game
        .get("install_dir")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_owned();
    if install_dir_text.is_empty() {
        return Err(
            "Refusing to uninstall a Gameyfin game without an install directory.".to_owned(),
        );
    }
    let install_dir = contained_install_path(&root, Path::new(&install_dir_text))?;
    let path_text = game
        .get("path")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    let path_opt = if path_text.is_empty() {
        None
    } else {
        Some(contained_install_path(&root, Path::new(path_text))?)
    };

    let mut removed = Vec::new();
    let mut targets = vec![install_dir.clone()];
    if let Some(p) = path_opt
        && p != install_dir
    {
        targets.push(p);
    }
    for cand in targets {
        if !cand.exists() {
            continue;
        }
        if cand.is_dir() {
            fs::remove_dir_all(&cand).map_err(|e| e.to_string())?;
        } else {
            fs::remove_file(&cand).map_err(|e| e.to_string())?;
        }
        removed.push(cand.to_string_lossy().into_owned());
    }
    game.insert("store_installed".into(), json!(false));
    game.insert("path".into(), json!(install_dir.to_string_lossy()));
    game.insert("launch".into(), json!(""));
    game.insert(
        "notes".into(),
        json!("Owned on Gameyfin. Install to download locally."),
    );
    Ok(json!({"removed": removed, "game": Value::Object(game.clone())}))
}
