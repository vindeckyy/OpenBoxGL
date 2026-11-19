//! Exact Python-compatible game identity hashing (`_stable_game_id`), path normalization,
//! and ULID library IDs mirroring `state_store.py`.

use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::sync::LazyLock;
use std::time::{SystemTime, UNIX_EPOCH};

use rand::RngCore;
use regex::Regex;
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

const CROCKFORD_ALPHABET: &[u8; 32] = b"0123456789ABCDEFGHJKMNPQRSTVWXYZ";

static LIBRARY_ID_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[0-9A-HJKMNP-TV-Z]{26}$").expect("valid library_id regex"));
static LEGACY_INDEXED_ID_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^game-[0-9a-f]{24}-\d+$").expect("valid legacy id regex"));

const CORE_IDENTITY_KEYS: &[&str] = &[
    "path",
    "platform",
    "steam_app_id",
    "heroic_app_id",
    "lutris_id",
    "gameyfin_id",
    "launchbox_db_id",
];

const OPTIONAL_STOREFRONT_KEYS: &[&str] = &["gog_id", "ea_id", "ubisoft_id"];

pub fn is_valid_library_id(value: &str) -> bool {
    LIBRARY_ID_RE.is_match(value)
}

pub fn is_legacy_indexed_id(value: &str) -> bool {
    LEGACY_INDEXED_ID_RE.is_match(value)
}

/// Generate a 26-char Crockford Base32 ULID: 48-bit ms timestamp + 80 random bits.
pub fn new_library_id() -> String {
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    let mut rand_bytes = [0u8; 10];
    rand::rngs::OsRng.fill_bytes(&mut rand_bytes);
    let mut rand80: u128 = 0;
    for b in rand_bytes {
        rand80 = (rand80 << 8) | u128::from(b);
    }
    let mut value: u128 = ((millis & 0xFFFF_FFFF_FFFF) << 80) | rand80;
    let mut chars = [0u8; 26];
    for idx in (0..26).rev() {
        chars[idx] = CROCKFORD_ALPHABET[(value & 31) as usize];
        value >>= 5;
    }
    String::from_utf8(chars.to_vec()).expect("ASCII ULID")
}

/// Exact port of Python's `posixpath.normpath`.
pub fn posix_normpath(path: &str) -> String {
    if path.is_empty() {
        return ".".to_owned();
    }
    let mut initial_slashes = usize::from(path.starts_with('/'));
    if initial_slashes == 1 && path.starts_with("//") && !path.starts_with("///") {
        initial_slashes = 2;
    }
    let mut new_comps: Vec<&str> = Vec::new();
    for comp in path.split('/') {
        if comp.is_empty() || comp == "." {
            continue;
        }
        if comp != ".."
            || (initial_slashes == 0 && new_comps.is_empty())
            || new_comps.last() == Some(&"..")
        {
            new_comps.push(comp);
        } else if !new_comps.is_empty() {
            new_comps.pop();
        }
    }
    let mut out = "/".repeat(initial_slashes);
    out.push_str(&new_comps.join("/"));
    if out.is_empty() { ".".to_owned() } else { out }
}

/// Exact port of Python's `ntpath.normpath` + `ntpath.normcase`.
pub fn windows_normcase_normpath(path: &str) -> String {
    let mut s = path.replace('/', "\\");
    let prefix = if s.starts_with("\\\\") {
        // UNC path \\server\share
        let rest = &s[2..];
        let mut parts = rest.splitn(3, '\\');
        let host = parts.next().unwrap_or("");
        let share = parts.next().unwrap_or("");
        let tail = parts.next().unwrap_or("");
        let head = format!("\\\\{host}\\{share}\\");
        s = tail.to_owned();
        head
    } else if s.len() >= 2 && s.as_bytes()[1] == b':' {
        let drive = &s[..2];
        if s.as_bytes().get(2) == Some(&b'\\') {
            let head = format!("{drive}\\");
            s = s[3..].to_owned();
            head
        } else {
            let head = drive.to_owned();
            s = s[2..].to_owned();
            head
        }
    } else if s.starts_with('\\') {
        s = s[1..].to_owned();
        "\\".to_owned()
    } else {
        String::new()
    };

    let rooted = prefix.ends_with('\\');
    let mut comps: Vec<&str> = Vec::new();
    for comp in s.split('\\') {
        if comp.is_empty() || comp == "." {
            continue;
        }
        if comp == ".." {
            if !comps.is_empty() && comps.last() != Some(&"..") {
                comps.pop();
            } else if !rooted {
                comps.push("..");
            }
        } else {
            comps.push(comp);
        }
    }
    let joined = format!("{prefix}{}", comps.join("\\"));
    let final_path = if joined.is_empty() {
        ".".to_owned()
    } else {
        joined
    };
    final_path.to_lowercase()
}

/// Expand leading `~` matching Python's `os.path.expanduser` for current user.
pub fn expand_user_tilde(path: &str) -> String {
    if path == "~" || path.starts_with("~/") || (cfg!(windows) && path.starts_with("~\\")) {
        let home = std::env::var("HOME")
            .ok()
            .filter(|v| !v.is_empty())
            .or_else(|| std::env::var("USERPROFILE").ok().filter(|v| !v.is_empty()));
        if let Some(h) = home {
            let h_trimmed = h.trim_end_matches(['/', '\\']);
            if path == "~" {
                return h_trimmed.to_owned();
            }
            return format!("{h_trimmed}{}", &path[1..]);
        }
    }
    path.to_owned()
}

pub fn normcase_normpath_expanduser(path: &str) -> String {
    let expanded = expand_user_tilde(path);
    if cfg!(windows) {
        windows_normcase_normpath(&expanded)
    } else {
        posix_normpath(&expanded)
    }
}

/// Exact port of Python's `json.dumps(map, sort_keys=True, separators=(",", ":"), ensure_ascii=True)`.
pub fn python_canonical_ascii_json(map: &BTreeMap<String, String>) -> String {
    let mut out = String::with_capacity(256);
    out.push('{');
    for (idx, (k, v)) in map.iter().enumerate() {
        if idx > 0 {
            out.push(',');
        }
        write_py_json_str(&mut out, k);
        out.push(':');
        write_py_json_str(&mut out, v);
    }
    out.push('}');
    out
}

fn write_py_json_str(out: &mut String, s: &str) {
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\x08' => out.push_str("\\b"),
            '\x0c' => out.push_str("\\f"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c if (c as u32) <= 0x7F => out.push(c),
            c if (c as u32) <= 0xFFFF => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => {
                let mut buf = [0u16; 2];
                for unit in c.encode_utf16(&mut buf) {
                    let _ = write!(out, "\\u{:04x}", unit);
                }
            }
        }
    }
    out.push('"');
}

fn value_as_trimmed_string(val: Option<&Value>) -> String {
    match val {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => s.trim().to_owned(),
        Some(Value::Bool(true)) => "True".to_owned(),
        Some(Value::Bool(false)) => String::new(), // Python `str(game.get(k) or "")` turns False into ""
        Some(Value::Number(n)) => {
            if n.as_i64() == Some(0) || n.as_f64() == Some(0.0) {
                String::new()
            } else {
                n.to_string()
            }
        }
        Some(other) => other.to_string().trim().to_owned(),
    }
}

/// Build the exact identity map matching `state_store.py::_identity_payload`.
pub fn identity_payload(game: &Map<String, Value>) -> BTreeMap<String, String> {
    let mut identity = BTreeMap::new();
    for &key in CORE_IDENTITY_KEYS {
        identity.insert(key.to_owned(), value_as_trimmed_string(game.get(key)));
    }
    for &key in OPTIONAL_STOREFRONT_KEYS {
        let val = value_as_trimmed_string(game.get(key));
        if !val.is_empty() {
            identity.insert(key.to_owned(), val);
        }
    }
    if let Some(p) = identity.get("path").cloned()
        && !p.is_empty()
    {
        identity.insert("path".to_owned(), normcase_normpath_expanduser(&p));
    }

    let has_path = identity.get("path").is_some_and(|s| !s.is_empty());
    let has_storefront = [
        "steam_app_id",
        "heroic_app_id",
        "lutris_id",
        "gameyfin_id",
        "launchbox_db_id",
        "gog_id",
        "ea_id",
        "ubisoft_id",
    ]
    .iter()
    .any(|&k| identity.get(k).is_some_and(|s| !s.is_empty()));

    if !has_path && !has_storefront {
        identity.insert("name".to_owned(), value_as_trimmed_string(game.get("name")));
    }
    identity
}

/// Return a durable game ID (`game-<24hex>`) byte-identical to `state_store.py::_stable_game_id`.
pub fn stable_game_id(game: &Map<String, Value>) -> String {
    let payload = identity_payload(game);
    let canonical = python_canonical_ascii_json(&payload);
    let digest = Sha256::digest(canonical.as_bytes());
    let hex_full = hex::encode(digest);
    format!("game-{}", &hex_full[..24])
}
