//! Redacted diagnostic / crash report builder mirroring `crash_report.py`.

use std::env;
use std::fs;
use std::path::Path;
use std::sync::LazyLock;

use openbox_core::APP_VERSION;
use openbox_platform::{IS_MACOS, IS_WINDOWS, home_dir};
use regex::Regex;
use serde_json::{Map, Value, json};

use crate::logging::read_diagnostic_log;

static HOME_PATH_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#"/(?:home|Users)/[^/\s'"]+"#).expect("home path re"));
static WIN_HOME_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"(?i)[A-Za-z]:(?:\\\\|\\|/)Users(?:\\\\|\\|/)[^\\/\s'"]+"#).expect("win home re")
});
static REQUEST_ID_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\[([0-9a-f]{8})\]").expect("req id re"));

pub fn tokenize_home_str(input: &str) -> String {
    let mut text = input.to_owned();
    if let Some(home) = home_dir() {
        let home_str = home.to_string_lossy();
        let trimmed = home_str.trim_end_matches(['/', '\\']);
        if !trimmed.is_empty() && trimmed != "~" {
            // Replace whole path-segment occurrences of $HOME with ~
            let mut out = String::with_capacity(text.len());
            let mut rest = text.as_str();
            while let Some(pos) = rest.find(trimmed) {
                let after = &rest[pos + trimmed.len()..];
                let next_is_word = after
                    .chars()
                    .next()
                    .is_some_and(|c| c.is_alphanumeric() || c == '_' || c == '.' || c == '-');
                out.push_str(&rest[..pos]);
                if next_is_word {
                    out.push_str(trimmed);
                } else {
                    out.push('~');
                }
                rest = after;
            }
            out.push_str(rest);
            text = out;
        }
    }
    let s1 = HOME_PATH_RE.replace_all(&text, "~");
    WIN_HOME_RE.replace_all(&s1, "~").into_owned()
}

pub fn tokenize_home_paths(value: &Value) -> Value {
    match value {
        Value::String(s) => Value::String(tokenize_home_str(s)),
        Value::Array(arr) => Value::Array(arr.iter().map(tokenize_home_paths).collect()),
        Value::Object(map) => {
            let mut out = Map::with_capacity(map.len());
            for (k, v) in map {
                out.insert(k.clone(), tokenize_home_paths(v));
            }
            Value::Object(out)
        }
        other => other.clone(),
    }
}

pub fn install_channel() -> &'static str {
    if env::var("FLATPAK_ID").ok().is_some_and(|v| !v.is_empty()) {
        "flatpak"
    } else if env::var("APPIMAGE").ok().is_some_and(|v| !v.is_empty()) {
        "appimage"
    } else if IS_WINDOWS {
        "windows"
    } else if IS_MACOS {
        "macos"
    } else {
        "source"
    }
}

pub fn recent_request_ids_from_log(data_dir: &Path, limit: usize) -> Vec<String> {
    let log = read_diagnostic_log(data_dir, 250_000);
    let matches: Vec<String> = REQUEST_ID_RE
        .captures_iter(&log)
        .filter_map(|c| c.get(1).map(|m| m.as_str().to_owned()))
        .collect();
    let mut seen = Vec::new();
    for id in matches.into_iter().rev() {
        if !seen.contains(&id) {
            seen.push(id);
            if seen.len() >= limit {
                break;
            }
        }
    }
    seen.reverse();
    seen
}

fn library_facts(data_dir: &Path) -> (usize, i64, usize) {
    let path = data_dir.join("library.json");
    let Ok(raw) = fs::read_to_string(&path) else {
        return (0, 0, 0);
    };
    let bytes = raw.len();
    let Ok(val) = serde_json::from_str::<Value>(&raw) else {
        return (0, 0, 0);
    };
    let count = val
        .get("games")
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0);
    let schema = val
        .get("schema_version")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    (count, schema, bytes)
}

pub fn build_preview(
    data_dir: &Path,
    recent_job_ids: &[String],
    recent_request_ids: Option<&[String]>,
) -> Value {
    let (library_count, schema_version, library_bytes) = library_facts(data_dir);
    let attached = env::var("OPENBOX_NATIVE_HOST")
        .ok()
        .is_some_and(|v| !v.is_empty());
    let req_ids = recent_request_ids
        .map(|s| s.to_vec())
        .unwrap_or_else(|| recent_request_ids_from_log(data_dir, 10));
    let exe = env::current_exe()
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_default();
    let preview = json!({
        "report": "openbox-diagnostic",
        "version": APP_VERSION,
        "install_channel": install_channel(),
        "architecture": env::consts::ARCH,
        "distro": format!("{}-{}", env::consts::OS, env::consts::ARCH),
        "desktop_session": {
            "type": env::var("XDG_SESSION_TYPE").unwrap_or_default(),
            "desktop": env::var("XDG_CURRENT_DESKTOP")
                .ok()
                .filter(|v| !v.is_empty())
                .or_else(|| env::var("DESKTOP_SESSION").ok())
                .unwrap_or_default(),
        },
        "native_host": { "attached": attached, "webkit": attached },
        "renderer_flags": { "native_host": attached, "browser_fallback": !attached },
        "library_count": library_count,
        "schema_version": schema_version,
        "disk_space": { "total_bytes": 0, "free_bytes": 0 },
        "package_integrity": { "status": "skipped", "reason": "source" },
        "recent_job_ids": recent_job_ids,
        "recent_request_ids": req_ids,
        "system": {
            "python": "rust-native",
            "platform": format!("{}-{}", env::consts::OS, env::consts::ARCH),
            "machine": env::consts::ARCH,
            "user_agent_os": "",
            "data_dir": data_dir.to_string_lossy(),
            "library_bytes": library_bytes,
        },
        "python_executable": exe,
        "diagnostic_log": "",
    });
    tokenize_home_paths(&preview)
}

pub fn build_report(data_dir: &Path, include_log: bool, log_limit: usize) -> String {
    let mut preview = build_preview(data_dir, &[], None);
    if include_log {
        let log = read_diagnostic_log(data_dir, log_limit);
        if let Some(obj) = preview.as_object_mut() {
            obj.insert("diagnostic_log".into(), Value::String(log));
        }
        preview = tokenize_home_paths(&preview);
    }
    serde_json::to_string_pretty(&preview).unwrap_or_else(|_| "{}".to_owned())
}
