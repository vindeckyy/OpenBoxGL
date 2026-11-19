//! Path containment, identifier/filename validation, symlink detection, and platform data directories.
//! Mirrors `pkg/platform_compat.py`.

use std::env;
use std::fs;
use std::path::{Component, Path, PathBuf};
use std::sync::LazyLock;

use regex::Regex;
use thiserror::Error;
use unicode_normalization::UnicodeNormalization;

use crate::features::{IS_MACOS, IS_WINDOWS};

pub const MAX_FILENAME_COMPONENT: usize = 200;

static SAFE_IDENTIFIER_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$").expect("valid safe id re"));

const WINDOWS_RESERVED_NAMES: &[&str] = &[
    "con", "prn", "aux", "nul", "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8",
    "com9", "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
];

const MACOS_SYSTEM_LINK_ROOTS: &[&str] = &["/private/", "/System/Volumes/Data/"];

#[derive(Debug, Error)]
#[error("{0}")]
pub struct UnsafeIdentifier(pub String);

/// Return `value` if safe to use as a single path component.
pub fn safe_identifier(value: &str, field: &str) -> Result<String, UnsafeIdentifier> {
    let text = value.trim();
    if text.is_empty() {
        return Err(UnsafeIdentifier(format!("{field} is required")));
    }
    if !SAFE_IDENTIFIER_RE.is_match(text) {
        return Err(UnsafeIdentifier(format!(
            "{field} contains characters that are not allowed in a path component"
        )));
    }
    if text == "." || text == ".." || text.starts_with('.') {
        return Err(UnsafeIdentifier(format!(
            "{field} must not start with a dot"
        )));
    }
    Ok(text.to_owned())
}

/// Return `value` if safe as a single filename component (allows spaces/parentheses, rejects traversal/control/Windows device names).
pub fn safe_filename_component(value: &str, field: &str) -> Result<String, UnsafeIdentifier> {
    let text = value.trim();
    if text.is_empty() {
        return Err(UnsafeIdentifier(format!("{field} is required")));
    }
    if text.chars().count() > MAX_FILENAME_COMPONENT {
        return Err(UnsafeIdentifier(format!(
            "{field} is longer than {MAX_FILENAME_COMPONENT} characters"
        )));
    }
    if text.contains('/') || text.contains('\\') {
        return Err(UnsafeIdentifier(format!(
            "{field} must not contain a path separator"
        )));
    }
    if text == "." || text == ".." || text.starts_with("..") {
        return Err(UnsafeIdentifier(format!(
            "{field} must not be a relative path component"
        )));
    }
    if text
        .chars()
        .any(|c| (c as u32) < 0x20 || (c as u32) == 0x7F)
    {
        return Err(UnsafeIdentifier(format!(
            "{field} must not contain control characters"
        )));
    }
    if text != text.trim_matches('.') {
        return Err(UnsafeIdentifier(format!(
            "{field} must not begin or end with a dot"
        )));
    }
    let stem = text.split('.').next().unwrap_or("").to_lowercase();
    if WINDOWS_RESERVED_NAMES.contains(&stem.as_str()) {
        return Err(UnsafeIdentifier(format!(
            "{field} is a reserved device name on Windows"
        )));
    }
    Ok(text.to_owned())
}

/// Resolve a path lexically + via `canonicalize` on the deepest existing ancestor, matching Python's `Path.resolve(strict=False)`.
pub fn resolve_non_strict(path: &Path) -> PathBuf {
    let abs = if path.is_absolute() {
        path.to_path_buf()
    } else {
        env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("."))
            .join(path)
    };
    if let Ok(canon) = fs::canonicalize(&abs) {
        return canon;
    }
    // Walk up until we find an existing ancestor to canonicalize, then append normalized remainder.
    let mut existing = abs.clone();
    let mut remainder = Vec::new();
    while !existing.exists() {
        if let Some(name) = existing.file_name() {
            remainder.push(name.to_os_string());
        } else {
            break;
        }
        if !existing.pop() {
            break;
        }
    }
    let mut base = fs::canonicalize(&existing).unwrap_or(existing);
    for comp in remainder.into_iter().rev() {
        base.push(comp);
    }
    normalize_lexical(&base)
}

fn normalize_lexical(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for comp in path.components() {
        match comp {
            Component::CurDir => {}
            Component::ParentDir => {
                out.pop();
            }
            other => out.push(other.as_os_str()),
        }
    }
    out
}

/// Join `parts` onto `base` and assert the resolved path stays inside `base`.
pub fn contained_path(base: &Path, parts: &[&str]) -> Result<PathBuf, UnsafeIdentifier> {
    let base_resolved = resolve_non_strict(base);
    let mut candidate = base_resolved.clone();
    for part in parts {
        candidate.push(part);
    }
    let resolved = resolve_non_strict(&candidate);
    if resolved != base_resolved && !resolved.starts_with(&base_resolved) {
        return Err(UnsafeIdentifier(
            "resolved path escapes its permitted directory".to_owned(),
        ));
    }
    Ok(resolved)
}

pub fn home_dir() -> Option<PathBuf> {
    #[cfg(windows)]
    {
        env::var_os("USERPROFILE")
            .filter(|v| !v.is_empty())
            .map(PathBuf::from)
            .or_else(|| {
                let drive = env::var_os("HOMEDRIVE")?;
                let path = env::var_os("HOMEPATH")?;
                let mut combined = drive;
                combined.push(path);
                Some(PathBuf::from(combined))
            })
    }
    #[cfg(not(windows))]
    {
        env::var_os("HOME")
            .filter(|v| !v.is_empty())
            .map(PathBuf::from)
    }
}

pub fn expand_user(path: &Path) -> PathBuf {
    let text = path.to_string_lossy();
    if (text == "~" || text.starts_with("~/") || (IS_WINDOWS && text.starts_with("~\\")))
        && let Some(home) = home_dir()
    {
        if text == "~" {
            return home;
        }
        return home.join(&text[2..]);
    }
    path.to_path_buf()
}

pub fn default_data_dir() -> PathBuf {
    if IS_WINDOWS {
        if let Some(base) = env::var_os("LOCALAPPDATA")
            .filter(|v| !v.is_empty())
            .or_else(|| env::var_os("APPDATA").filter(|v| !v.is_empty()))
        {
            return PathBuf::from(base).join("openbox-game-launcher");
        }
        if let Some(home) = home_dir() {
            return home
                .join("AppData")
                .join("Local")
                .join("openbox-game-launcher");
        }
        return env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("."))
            .join("openbox-game-launcher");
    }
    if IS_MACOS {
        if let Some(home) = home_dir() {
            return home
                .join("Library")
                .join("Application Support")
                .join("openbox-game-launcher");
        }
        return env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("."))
            .join("openbox-game-launcher");
    }
    if let Some(home) = home_dir() {
        return home
            .join(".local")
            .join("share")
            .join("openbox-game-launcher");
    }
    env::current_dir()
        .unwrap_or_else(|_| PathBuf::from("."))
        .join("openbox-game-launcher")
}

/// Return OpenBox data dir, honoring `OPENBOX_DATA_DIR`.
pub fn resolved_data_dir() -> PathBuf {
    if let Some(custom) = env::var_os("OPENBOX_DATA_DIR").filter(|v| !v.is_empty()) {
        return expand_user(&PathBuf::from(custom));
    }
    default_data_dir()
}

pub fn env_file_roots() -> Vec<PathBuf> {
    if IS_WINDOWS {
        let mut roots = Vec::new();
        if let Some(home) = home_dir() {
            roots.push(home);
        }
        for var in ["APPDATA", "LOCALAPPDATA"] {
            if let Some(base) = env::var_os(var).filter(|v| !v.is_empty()) {
                roots.push(PathBuf::from(base).join("openbox-game-launcher"));
            }
        }
        return roots;
    }
    if IS_MACOS {
        let Some(home) = home_dir() else {
            return Vec::new();
        };
        return vec![home, default_data_dir()];
    }
    let mut roots = Vec::new();
    if let Some(home) = home_dir() {
        roots.push(home.clone());
        roots.push(home.join(".config").join("openbox-game-launcher"));
    }
    roots
}

pub fn start_menu_programs_dir() -> Option<PathBuf> {
    if !IS_WINDOWS {
        return None;
    }
    let base = env::var_os("APPDATA").filter(|v| !v.is_empty())?;
    Some(
        PathBuf::from(base)
            .join("Microsoft")
            .join("Windows")
            .join("Start Menu")
            .join("Programs"),
    )
}

pub fn windows_install_dir() -> Option<PathBuf> {
    if !IS_WINDOWS {
        return None;
    }
    let base = env::var_os("LOCALAPPDATA").filter(|v| !v.is_empty())?;
    Some(PathBuf::from(base).join("OpenBox"))
}

pub fn path_identity_fold(text: &str) -> String {
    if IS_MACOS {
        text.nfc().collect::<String>().to_lowercase()
    } else if IS_WINDOWS {
        text.replace('/', "\\").to_lowercase()
    } else {
        text.to_owned()
    }
}

pub fn display_name_for_file(name: &str) -> String {
    if IS_MACOS {
        name.nfc().collect()
    } else {
        name.to_owned()
    }
}

fn is_macos_system_link(component: &Path) -> bool {
    if !IS_MACOS {
        return false;
    }
    let Ok(target) = fs::read_link(component) else {
        return false;
    };
    let joined = component.parent().unwrap_or(Path::new("/")).join(target);
    let norm = normalize_lexical(&joined);
    let text = norm.to_string_lossy();
    MACOS_SYSTEM_LINK_ROOTS
        .iter()
        .any(|prefix| text.starts_with(prefix))
}

/// Return the first symlink component in `path` that a user could have created (ignoring macOS `/var`, `/tmp`, `/etc`).
pub fn user_symlink_component(path: &Path) -> Option<PathBuf> {
    let abs = if path.is_absolute() {
        path.to_path_buf()
    } else {
        env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("."))
            .join(path)
    };
    let mut current = PathBuf::new();
    for (idx, comp) in abs.components().enumerate() {
        current.push(comp.as_os_str());
        if idx == 0 {
            continue;
        }
        let Ok(meta) = fs::symlink_metadata(&current) else {
            continue;
        };
        if !meta.file_type().is_symlink() {
            continue;
        }
        if idx == 1 && is_macos_system_link(&current) {
            continue;
        }
        return Some(current);
    }
    None
}

/// File privacy checks mirroring `platform_compat.py`.
pub fn file_is_private(meta: &fs::Metadata) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        // SAFETY: `geteuid` has no preconditions and always succeeds.
        let euid = unsafe { libc::geteuid() };
        meta.uid() == euid && (meta.mode() & 0o077) == 0
    }
    #[cfg(windows)]
    {
        let _ = meta;
        true
    }
}

pub fn is_group_or_world_writable(meta: &fs::Metadata) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        (meta.mode() & 0o022) != 0
    }
    #[cfg(windows)]
    {
        let _ = meta;
        false
    }
}

pub fn owned_by_current_user(meta: &fs::Metadata) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        // SAFETY: `geteuid` has no preconditions.
        let euid = unsafe { libc::geteuid() };
        meta.uid() == euid
    }
    #[cfg(windows)]
    {
        let _ = meta;
        true
    }
}

pub fn libretro_system_core_dir() -> String {
    if IS_MACOS {
        home_dir()
            .map(|h| {
                format!(
                    "{}/",
                    h.join("Library")
                        .join("Application Support")
                        .join("RetroArch")
                        .join("cores")
                        .display()
                )
            })
            .unwrap_or_default()
    } else {
        "/usr/lib/libretro/".to_owned()
    }
}

pub fn libretro_core_filename(name: &str) -> String {
    if IS_MACOS && let Some(stem) = name.strip_suffix(".so") {
        format!("{stem}.dylib")
    } else {
        name.to_owned()
    }
}

const MACOS_TOOL_PATHS: &[&str] = &[
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/local/sbin",
    "/opt/local/bin",
];

pub fn ensure_macos_tool_path(current_path: &str) -> String {
    if !IS_MACOS {
        return current_path.to_owned();
    }
    let sep = if IS_WINDOWS { ';' } else { ':' };
    let entries: Vec<&str> = if current_path.is_empty() {
        Vec::new()
    } else {
        current_path.split(sep).collect()
    };
    let mut prefix: Vec<&str> = MACOS_TOOL_PATHS
        .iter()
        .copied()
        .filter(|p| !entries.contains(p))
        .collect();
    if prefix.is_empty() {
        return current_path.to_owned();
    }
    prefix.extend(entries);
    prefix.join(&sep.to_string())
}

pub fn macos_app_bundle_for(path: &Path) -> Option<PathBuf> {
    let candidate = resolve_non_strict(&expand_user(path));
    for ancestor in candidate.ancestors().skip(1) {
        if ancestor.extension().and_then(|e| e.to_str()) == Some("app")
            && ancestor.join("Contents").join("Info.plist").is_file()
        {
            return Some(ancestor.to_path_buf());
        }
    }
    None
}

/// Extract `CFBundleExecutable` from a macOS `.app/Contents/Info.plist` XML file using `quick-xml`.
pub fn macos_app_executable(bundle: &Path) -> Option<PathBuf> {
    let plist_path = bundle.join("Contents").join("Info.plist");
    let xml = fs::read_to_string(plist_path).ok()?;
    let mut reader = quick_xml::Reader::from_str(&xml);
    let mut in_key = false;
    let mut want_string = false;
    let mut in_string = false;
    let mut exec_name = String::new();
    let mut buf = Vec::new();
    loop {
        match reader.read_event_into(&mut buf) {
            Ok(quick_xml::events::Event::Start(e)) => match e.name().as_ref() {
                "key" => in_key = true,
                "string" if want_string => in_string = true,
                _ => {}
            },
            Ok(quick_xml::events::Event::Text(t)) => {
                let text: &str = t.as_ref();
                if in_key {
                    want_string = text.trim() == "CFBundleExecutable";
                } else if in_string {
                    exec_name.push_str(text);
                }
            }
            Ok(quick_xml::events::Event::End(e)) => match e.name().as_ref() {
                "key" => in_key = false,
                "string" if in_string => break,
                _ => {}
            },
            Ok(quick_xml::events::Event::Eof) | Err(_) => break,
            _ => {}
        }
        buf.clear();
    }
    let name = exec_name.trim();
    if name.is_empty() || name == "." || name == ".." || name.contains('/') || name.contains('\\') {
        return None;
    }
    let executable = bundle.join("Contents").join("MacOS").join(name);
    if !executable.is_file() || !crate::shell::is_executable(&executable) {
        return None;
    }
    Some(executable)
}

pub fn macos_screenshot_command(dest: &Path) -> Option<Vec<String>> {
    if !IS_MACOS {
        return None;
    }
    Some(vec![
        "/usr/sbin/screencapture".to_owned(),
        "-x".to_owned(),
        dest.to_string_lossy().into_owned(),
    ])
}
