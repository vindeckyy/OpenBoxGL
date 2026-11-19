//! Secure `.env` file discovery and allowlisted credential loading mirroring `env_config.py`.

use std::collections::{BTreeMap, HashSet};
use std::env;
use std::fs::{self, OpenOptions};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

use openbox_platform::{env_file_roots, expand_user, file_is_private};
use parking_lot::RwLock;

pub const ENV_FILE_ENV: &str = "OPENBOX_ENV_FILE";
pub const MAX_ENV_FILE_BYTES: u64 = 1024 * 1024;

pub const ENV_KEYS: &[&str] = &[
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "OPENBOX_GITHUB_TOKEN",
    "IGDB_CLIENT_ID",
    "IGDB_CLIENT_SECRET",
    "RETROACHIEVEMENTS_USERNAME",
    "RA_USERNAME",
    "OPENBOX_RA_USERNAME",
    "RETROACHIEVEMENTS_API_KEY",
    "RA_API_KEY",
    "RETROACHIEVEMENTS_KEY",
    "OPENBOX_RA_API_KEY",
    "EMUMOVIES_USERNAME",
    "OPENBOX_EMUMOVIES_USERNAME",
    "EMUMOVIES_PASSWORD",
    "OPENBOX_EMUMOVIES_PASSWORD",
    "SCREENSCRAPER_USER",
    "OPENBOX_SCREENSCRAPER_USER",
    "SCREENSCRAPER_PASSWORD",
    "OPENBOX_SCREENSCRAPER_PASSWORD",
    "SCREENSCRAPER_DEV_ID",
    "OPENBOX_SCREENSCRAPER_DEV_ID",
    "SCREENSCRAPER_DEV_PASSWORD",
    "OPENBOX_SCREENSCRAPER_DEV_PASSWORD",
    "STEAMGRIDDB_API_KEY",
    "OPENBOX_STEAMGRIDDB_API_KEY",
    "STRIPE_SECRET_KEY",
    "OPENBOX_ALLOW_HTTP_WEBHOOKS",
    "OPENBOX_ALLOW_HTTP_GAMEYFIN",
];

static ENV_BOOTSTRAPPED: AtomicBool = AtomicBool::new(false);
static LOADED_DOTENV: std::sync::LazyLock<RwLock<BTreeMap<String, String>>> =
    std::sync::LazyLock::new(|| RwLock::new(BTreeMap::new()));

fn secure_env_file(path: &Path) -> bool {
    let Ok(meta) = fs::symlink_metadata(path) else {
        return false;
    };
    meta.file_type().is_file() && file_is_private(&meta) && meta.len() <= MAX_ENV_FILE_BYTES
}

pub fn parse_env_line(raw: &str) -> Option<(String, String)> {
    let line = raw.trim();
    if line.is_empty() || line.starts_with('#') {
        return None;
    }
    let (raw_key, raw_val) = line.split_once('=')?;
    let mut key = raw_key.trim();
    if let Some(rest) = key.strip_prefix("export")
        && rest.starts_with(char::is_whitespace)
    {
        key = rest.trim();
    }
    let mut val = raw_val.trim();
    let quote = val.chars().next().filter(|&c| c == '\'' || c == '"');
    let parsed_val = if let Some(q) = quote
        && let Some(end) = val[1..].find(q)
    {
        val[1..1 + end].to_owned()
    } else {
        if quote.is_some() {
            val = &val[1..];
        }
        let mut cutoff = val.len();
        let bytes = val.as_bytes();
        for (i, ch) in val.char_indices() {
            if ch == '#' && i > 0 && bytes[i - 1].is_ascii_whitespace() {
                cutoff = i;
                break;
            }
        }
        val[..cutoff].trim().to_owned()
    };
    if key.is_empty()
        || key.starts_with(|c: char| c.is_ascii_digit())
        || !key.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
        || parsed_val.contains('\0')
    {
        return None;
    }
    Some((key.to_owned(), parsed_val))
}

/// Load allowlisted keys from a single private `.env` file without unsafe `std::env::set_var`.
/// Values are merged into our thread-safe `LOADED_DOTENV` overlay (process env always takes priority).
pub fn load_dotenv(path: &Path) -> BTreeMap<String, String> {
    let expanded = expand_user(path);
    let mut opts = OpenOptions::new();
    opts.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC);
    }
    let Ok(mut file) = opts.open(&expanded) else {
        return BTreeMap::new();
    };
    let Ok(meta) = file.metadata() else {
        return BTreeMap::new();
    };
    if !meta.is_file() || !file_is_private(&meta) || meta.len() > MAX_ENV_FILE_BYTES {
        return BTreeMap::new();
    }
    let mut buf = Vec::new();
    if file
        .by_ref()
        .take(MAX_ENV_FILE_BYTES + 1)
        .read_to_end(&mut buf)
        .is_err()
        || (buf.len() as u64) > MAX_ENV_FILE_BYTES
    {
        return BTreeMap::new();
    }
    let Ok(text) = String::from_utf8(buf) else {
        return BTreeMap::new();
    };
    let mut found = BTreeMap::new();
    let mut overlay = LOADED_DOTENV.write();
    for line in text.lines() {
        if let Some((key, val)) = parse_env_line(line) {
            if !ENV_KEYS.contains(&key.as_str()) {
                continue;
            }
            found.insert(key.clone(), val.clone());
            overlay.entry(key).or_insert(val);
        }
    }
    found
}

pub fn discover_env_files(extra_roots: &[&Path]) -> Vec<PathBuf> {
    let mut files = Vec::new();
    let mut seen = HashSet::new();
    if let Ok(explicit) = env::var(ENV_FILE_ENV) {
        let trimmed = explicit.trim();
        if !trimmed.is_empty() {
            let cand = expand_user(Path::new(trimmed));
            if secure_env_file(&cand) {
                seen.insert(cand.clone());
                files.push(cand);
            }
        }
    }
    let mut roots: Vec<PathBuf> = extra_roots.iter().map(|p| expand_user(p)).collect();
    roots.extend(env_file_roots());
    for root in roots {
        let cand = root.join(".env");
        if !seen.contains(&cand) && secure_env_file(&cand) {
            seen.insert(cand.clone());
            files.push(cand);
        }
    }
    files
}

pub fn bootstrap_env(data_dir: Option<&Path>) {
    let mut extra: Vec<&Path> = Vec::new();
    if let Some(d) = data_dir {
        extra.push(d);
        if let Some(p) = d.parent() {
            extra.push(p);
        }
    }
    for path in discover_env_files(&extra) {
        load_dotenv(&path);
    }
    ENV_BOOTSTRAPPED.store(true, Ordering::Release);
}

pub fn ensure_env_loaded(data_dir: Option<&Path>) {
    if !ENV_BOOTSTRAPPED.load(Ordering::Acquire) {
        bootstrap_env(data_dir);
    }
}

pub fn env_value(names: &[&str]) -> String {
    for &name in names {
        if let Ok(val) = env::var(name) {
            let t = val.trim();
            if !t.is_empty() {
                return t.to_owned();
            }
        }
        if let Some(val) = LOADED_DOTENV.read().get(name) {
            let t = val.trim();
            if !t.is_empty() {
                return t.to_owned();
            }
        }
    }
    String::new()
}

pub fn github_token_from_env() -> String {
    ensure_env_loaded(None);
    env_value(&["GITHUB_TOKEN", "GH_TOKEN", "OPENBOX_GITHUB_TOKEN"])
}

pub fn retroachievements_from_env() -> Option<(String, String)> {
    ensure_env_loaded(None);
    let user = env_value(&[
        "RETROACHIEVEMENTS_USERNAME",
        "RA_USERNAME",
        "OPENBOX_RA_USERNAME",
    ]);
    let key = env_value(&[
        "RETROACHIEVEMENTS_API_KEY",
        "RA_API_KEY",
        "RETROACHIEVEMENTS_KEY",
        "OPENBOX_RA_API_KEY",
    ]);
    if !user.is_empty() && !key.is_empty() {
        Some((user, key))
    } else {
        None
    }
}

pub fn emumovies_from_env() -> Option<(String, String)> {
    ensure_env_loaded(None);
    let user = env_value(&["EMUMOVIES_USERNAME", "OPENBOX_EMUMOVIES_USERNAME"]);
    let pass = env_value(&["EMUMOVIES_PASSWORD", "OPENBOX_EMUMOVIES_PASSWORD"]);
    if !user.is_empty() && !pass.is_empty() {
        Some((user, pass))
    } else {
        None
    }
}
