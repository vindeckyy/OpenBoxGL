//! Bundled stock CSS themes, sound sidecars, native background color, and user theme import
//! (`stock_themes.py` + `handlers/extensions.py` theme helpers).

use std::fs;
use std::path::{Component, Path, PathBuf};
use std::sync::LazyLock;

use openbox_io::atomic_write_text;
use openbox_platform::{expand_user, user_symlink_component};
use regex::Regex;
use serde_json::{Map, Value, json};

pub const STOCK_MARKER: &str = "/* OpenBox Stock Theme";
pub const NATIVE_BACKGROUND_FILE: &str = "native-background";
pub const DEFAULT_NATIVE_BACKGROUND: &str = "#0F0E0C";
pub const THEME_ASSET_LIMIT: u64 = 20 * 1024 * 1024;
pub const THEME_AUDIO_EXTENSIONS: &[&str] = &[".ogg", ".mp3", ".wav"];
const THEME_SOUND_KEYS: &[&str] = &["move", "select", "back"];

static VERSION_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^/\* OpenBox Stock Theme v(\d+):").unwrap());
static THEME_BG_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"--bg\s*:\s*(#[0-9a-fA-F]{6})\b").unwrap());

pub fn stock_theme_sources(app_root: &Path) -> Vec<PathBuf> {
    let mut dir = app_root.join("themes");
    if !dir.is_dir()
        && let Some(parent) = app_root.parent()
        && parent.join("themes").is_dir()
    {
        dir = parent.join("themes");
    }
    let Ok(rd) = fs::read_dir(&dir) else {
        return Vec::new();
    };
    let mut files: Vec<PathBuf> = rd
        .flatten()
        .map(|e| e.path())
        .filter(|p| {
            p.is_file()
                && p.extension()
                    .and_then(|e| e.to_str())
                    .is_some_and(|e| e.eq_ignore_ascii_case("css"))
        })
        .collect();
    files.sort();
    files
}

pub fn is_stock_theme(path: &Path) -> bool {
    let Ok(bytes) = fs::read(path) else {
        return false;
    };
    let head = String::from_utf8_lossy(&bytes[..bytes.len().min(120)]);
    head.trim_start().starts_with(STOCK_MARKER)
}

fn stock_version(path: &Path) -> Option<u32> {
    let bytes = fs::read(path).ok()?;
    let head = String::from_utf8_lossy(&bytes[..bytes.len().min(120)]);
    let trimmed = head.trim_start();
    Some(
        VERSION_RE
            .captures(trimmed)
            .and_then(|c| c.get(1))
            .and_then(|m| m.as_str().parse::<u32>().ok())
            .unwrap_or(0),
    )
}

pub fn ensure_stock_themes(destination: &Path, app_root: &Path) -> Vec<String> {
    let _ = fs::create_dir_all(destination);
    let mut installed = Vec::new();
    for source in stock_theme_sources(app_root) {
        let Some(fname) = source.file_name() else {
            continue;
        };
        let target = destination.join(fname);
        if target.exists() && !is_stock_theme(&target) {
            continue;
        }
        if target.exists()
            && stock_version(&target).unwrap_or(0) >= stock_version(&source).unwrap_or(0)
        {
            continue;
        }
        if fs::copy(&source, &target).is_ok() {
            let sidecar = source.with_extension("json");
            if sidecar.is_file() {
                let _ = fs::copy(&sidecar, target.with_extension("json"));
            }
            if let Some(stem) = target.file_stem().and_then(|s| s.to_str()) {
                installed.push(stem.to_owned());
            }
        }
    }
    installed
}

pub fn theme_folder(name: &str, data_parent: &Path, app_root: &Path) -> Option<PathBuf> {
    let clean = Path::new(name).file_stem()?.to_str()?;
    if clean.is_empty() || clean != name {
        return None;
    }
    for folder in [data_parent.join("themes"), app_root.join("themes")] {
        if folder.join(format!("{clean}.css")).is_file() {
            return Some(folder);
        }
    }
    None
}

pub fn theme_relative_audio(value: Option<&Value>) -> String {
    let text = value.and_then(Value::as_str).unwrap_or("").trim();
    if text.is_empty() || text.len() > 300 {
        return String::new();
    }
    let cand = Path::new(text);
    if cand.is_absolute() || cand.components().any(|c| matches!(c, Component::ParentDir)) {
        return String::new();
    }
    let ext = cand
        .extension()
        .and_then(|e| e.to_str())
        .map(|e| format!(".{}", e.to_lowercase()))
        .unwrap_or_default();
    if !THEME_AUDIO_EXTENSIONS.contains(&ext.as_str()) {
        return String::new();
    }
    cand.to_string_lossy().replace('\\', "/")
}

pub fn theme_sound_config(folder: &Path, name: &str) -> Value {
    let sidecar = folder.join(format!("{name}.json"));
    let Ok(meta) = fs::metadata(&sidecar) else {
        return json!({"music": "", "sounds": {}});
    };
    if !meta.is_file() || meta.len() > 64 * 1024 {
        return json!({"music": "", "sounds": {}});
    }
    let Ok(raw_text) = fs::read_to_string(&sidecar) else {
        return json!({"music": "", "sounds": {}});
    };
    let Ok(Value::Object(raw)) = serde_json::from_str::<Value>(&raw_text) else {
        return json!({"music": "", "sounds": {}});
    };
    let music = theme_relative_audio(raw.get("music"));
    let mut sounds = Map::new();
    if let Some(rs) = raw.get("sounds").and_then(Value::as_object) {
        for key in THEME_SOUND_KEYS {
            let rel = theme_relative_audio(rs.get(*key));
            if !rel.is_empty() {
                sounds.insert((*key).to_owned(), Value::String(rel));
            }
        }
    }
    json!({"music": music, "sounds": sounds})
}

pub fn write_native_background(theme_name: &str, data_parent: &Path, app_root: &Path) -> String {
    let mut color = DEFAULT_NATIVE_BACKGROUND.to_owned();
    let stem = Path::new(theme_name)
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("");
    if !stem.is_empty() {
        for folder in [data_parent.join("themes"), app_root.join("themes")] {
            let p = folder.join(format!("{stem}.css"));
            if !p.is_file() {
                continue;
            }
            if let Ok(text) = fs::read_to_string(&p)
                && let Some(caps) = THEME_BG_RE.captures(&text)
                && let Some(m) = caps.get(1)
            {
                color = m.as_str().to_lowercase();
            }
            break;
        }
    }
    let _ = atomic_write_text(
        &data_parent.join(NATIVE_BACKGROUND_FILE),
        &format!("{color}\n"),
        0o644,
    );
    color
}

pub fn import_theme_file(source_raw: &str, data_parent: &Path) -> Result<String, String> {
    let source = expand_user(Path::new(source_raw));
    let is_css = source
        .extension()
        .and_then(|e| e.to_str())
        .is_some_and(|e| e.eq_ignore_ascii_case("css"));
    if !source.is_file() || !is_css {
        return Err("Theme path must point to a CSS file.".to_owned());
    }
    if user_symlink_component(&source).is_some() {
        return Err("Theme path may not contain symlinks.".to_owned());
    }
    let size = fs::metadata(&source)
        .map_err(|_| "Could not inspect theme file.".to_owned())?
        .len();
    if size > 256 * 1024 {
        return Err("Theme file is too large (max 256 KiB).".to_owned());
    }
    let text =
        fs::read_to_string(&source).map_err(|_| "Theme file is not valid UTF-8 CSS.".to_owned())?;
    let lowered = text.to_lowercase();
    if lowered.contains("@import") && lowered.contains("http") {
        return Err("Theme CSS may not contain remote @import.".to_owned());
    }
    if text.len() > 256 * 1024 {
        return Err("Theme file is too large (max 256 KiB).".to_owned());
    }
    let fname = source
        .file_name()
        .ok_or_else(|| "Theme path must point to a CSS file.".to_owned())?;
    let dest = data_parent.join("themes").join(fname);
    if fs::symlink_metadata(&dest)
        .ok()
        .is_some_and(|m| m.file_type().is_symlink())
    {
        return Err("Theme destination may not be a symlink.".to_owned());
    }
    if let Some(parent) = dest.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    fs::copy(&source, &dest).map_err(|e| e.to_string())?;
    Ok(dest
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("")
        .to_owned())
}
