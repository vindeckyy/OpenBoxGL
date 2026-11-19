//! Command quoting/splitting, executable lookup (`which`), desktop openers, and Flatpak probing.
//! Mirrors `pkg/platform_compat.py`.

use std::env;
use std::io;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use crate::features::{IS_MACOS, IS_WINDOWS};
use crate::paths::{expand_user, home_dir, macos_app_executable};

pub const EXTERNAL_OPEN_DISABLED_ENV: &str = "OPENBOX_DISABLE_EXTERNAL_OPEN";
pub const FLATPAK_PROBE_TIMEOUT_SECONDS: u64 = 5;
#[cfg(windows)]
const WINDOWS_EXECUTABLE_SUFFIXES: &[&str] = &[".exe", ".bat", ".cmd", ".com", ".lnk", ".ps1"];

/// Split a command line using MSVCRT quoting rules (cross-platform testable).
pub fn split_windows_command(command: &str) -> Vec<String> {
    let chars: Vec<char> = command.chars().collect();
    let len = chars.len();
    let mut args = Vec::new();
    let mut current = String::new();
    let mut in_quotes = false;
    let mut started = false;
    let mut idx = 0;

    while idx < len {
        let ch = chars[idx];
        if (ch == ' ' || ch == '\t') && !in_quotes {
            if started {
                args.push(std::mem::take(&mut current));
                started = false;
            }
            idx += 1;
            continue;
        }
        if ch == '"' {
            if in_quotes && idx + 1 < len && chars[idx + 1] == '"' {
                current.push('"');
                idx += 2;
                continue;
            }
            in_quotes = !in_quotes;
            started = true;
            idx += 1;
            continue;
        }
        if ch == '\\' {
            let mut bs = 0;
            while idx < len && chars[idx] == '\\' {
                bs += 1;
                idx += 1;
            }
            if idx < len && chars[idx] == '"' {
                for _ in 0..(bs / 2) {
                    current.push('\\');
                }
                if bs % 2 == 1 {
                    current.push('"');
                } else {
                    in_quotes = !in_quotes;
                }
                idx += 1;
            } else {
                for _ in 0..bs {
                    current.push('\\');
                }
            }
            started = true;
            continue;
        }
        current.push(ch);
        started = true;
        idx += 1;
    }
    if in_quotes || started {
        args.push(current);
    }
    args
}

/// Split a POSIX shell-quoted command line (matching `shlex.split`).
pub fn split_posix_command(command: &str) -> Result<Vec<String>, String> {
    let mut args = Vec::new();
    let mut current = String::new();
    let mut in_single = false;
    let mut in_double = false;
    let mut token_started = false;
    let mut chars = command.chars().peekable();

    while let Some(ch) = chars.next() {
        if in_single {
            if ch == '\'' {
                in_single = false;
            } else {
                current.push(ch);
            }
            continue;
        }
        if in_double {
            match ch {
                '"' => in_double = false,
                '\\' => {
                    if let Some(&next) = chars.peek() {
                        if matches!(next, '"' | '\\' | '$' | '`' | '\n') {
                            let c = chars.next().expect("peeked");
                            if c != '\n' {
                                current.push(c);
                            }
                        } else {
                            current.push('\\');
                        }
                    } else {
                        return Err("No closing quotation".to_owned());
                    }
                }
                _ => current.push(ch),
            }
            continue;
        }
        match ch {
            c if c.is_whitespace() => {
                if token_started {
                    args.push(std::mem::take(&mut current));
                    token_started = false;
                }
            }
            '\'' => {
                in_single = true;
                token_started = true;
            }
            '"' => {
                in_double = true;
                token_started = true;
            }
            '\\' => {
                if let Some(next) = chars.next() {
                    if next != '\n' {
                        current.push(next);
                        token_started = true;
                    }
                } else {
                    return Err("No escaped character".to_owned());
                }
            }
            _ => {
                current.push(ch);
                token_started = true;
            }
        }
    }
    if in_single || in_double {
        return Err("No closing quotation".to_owned());
    }
    if token_started {
        args.push(current);
    }
    Ok(args)
}

pub fn split_command(command: &str) -> Result<Vec<String>, String> {
    if command.trim().is_empty() {
        return Ok(Vec::new());
    }
    if IS_WINDOWS {
        Ok(split_windows_command(command))
    } else {
        split_posix_command(command)
    }
}

pub fn join_command(parts: &[String]) -> String {
    if IS_WINDOWS {
        parts
            .iter()
            .map(|p| quote_windows_arg(p))
            .collect::<Vec<_>>()
            .join(" ")
    } else {
        parts
            .iter()
            .map(|p| quote_posix_arg(p))
            .collect::<Vec<_>>()
            .join(" ")
    }
}

fn quote_posix_arg(arg: &str) -> String {
    if arg.is_empty() {
        return "''".to_owned();
    }
    if arg.chars().all(|c| {
        c.is_ascii_alphanumeric()
            || matches!(c, '@' | '%' | '+' | '=' | ':' | ',' | '.' | '/' | '-' | '_')
    }) {
        return arg.to_owned();
    }
    format!("'{}'", arg.replace('\'', "'\"'\"'"))
}

fn quote_windows_arg(arg: &str) -> String {
    if !arg.is_empty() && !arg.contains([' ', '\t', '"']) {
        return arg.to_owned();
    }
    let mut out = String::from("\"");
    let mut bs = 0;
    for ch in arg.chars() {
        if ch == '\\' {
            bs += 1;
        } else if ch == '"' {
            for _ in 0..(bs * 2 + 1) {
                out.push('\\');
            }
            out.push('"');
            bs = 0;
        } else {
            for _ in 0..bs {
                out.push('\\');
            }
            bs = 0;
            out.push(ch);
        }
    }
    for _ in 0..(bs * 2) {
        out.push('\\');
    }
    out.push('"');
    out
}

pub fn is_executable(path: &Path) -> bool {
    if !path.is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::metadata(path)
            .map(|m| (m.permissions().mode() & 0o111) != 0)
            .unwrap_or(false)
    }
    #[cfg(windows)]
    {
        let ext = path
            .extension()
            .and_then(|e| e.to_str())
            .map(|s| format!(".{}", s.to_lowercase()))
            .unwrap_or_default();
        WINDOWS_EXECUTABLE_SUFFIXES.contains(&ext.as_str())
    }
}

/// Pure-Rust `shutil.which` equivalent.
pub fn which(binary: &str) -> Option<PathBuf> {
    let trimmed = binary.trim();
    if trimmed.is_empty() {
        return None;
    }
    let as_path = Path::new(trimmed);
    if as_path.components().count() > 1 {
        return is_executable(as_path).then(|| as_path.to_path_buf());
    }
    let path_var = env::var_os("PATH")?;
    for dir in env::split_paths(&path_var) {
        let candidate = dir.join(trimmed);
        if is_executable(&candidate) {
            return Some(candidate);
        }
        #[cfg(windows)]
        {
            for ext in WINDOWS_EXECUTABLE_SUFFIXES {
                let with_ext = dir.join(format!("{trimmed}{ext}"));
                if is_executable(&with_ext) {
                    return Some(with_ext);
                }
            }
        }
    }
    None
}

pub fn executable_paths(commands: &[&str]) -> Vec<String> {
    commands
        .iter()
        .filter_map(|cmd| which(cmd).map(|p| p.to_string_lossy().into_owned()))
        .collect()
}

pub fn resolve_native_executable(name: &str) -> Option<String> {
    let text = name.trim();
    if text.is_empty() {
        return None;
    }
    if text.ends_with(".app") && IS_MACOS {
        let mut folders = vec![PathBuf::from("/Applications")];
        if let Some(home) = home_dir() {
            folders.push(home.join("Applications"));
        }
        for folder in folders {
            if let Some(prefix) = text.strip_suffix("*.app") {
                if let Ok(entries) = std::fs::read_dir(&folder) {
                    let mut matches: Vec<PathBuf> = entries
                        .flatten()
                        .map(|e| e.path())
                        .filter(|p| {
                            p.file_name()
                                .and_then(|n| n.to_str())
                                .is_some_and(|n| n.starts_with(prefix) && n.ends_with(".app"))
                        })
                        .collect();
                    matches.sort();
                    for bundle in matches {
                        if let Some(exec) = macos_app_executable(&bundle) {
                            return Some(exec.to_string_lossy().into_owned());
                        }
                    }
                }
            } else if let Some(exec) = macos_app_executable(&folder.join(text)) {
                return Some(exec.to_string_lossy().into_owned());
            }
        }
        return None;
    }
    which(text).map(|p| p.to_string_lossy().into_owned())
}

pub fn external_open_disabled() -> bool {
    env::var(EXTERNAL_OPEN_DISABLED_ENV).ok().is_some_and(|v| {
        matches!(
            v.trim().to_lowercase().as_str(),
            "1" | "true" | "yes" | "on"
        )
    })
}

pub fn open_path(target: &str) -> io::Result<()> {
    if external_open_disabled() {
        return Err(io::Error::new(
            io::ErrorKind::NotFound,
            format!("External opening is disabled ({EXTERNAL_OPEN_DISABLED_ENV} is set)."),
        ));
    }
    if IS_MACOS {
        Command::new("/usr/bin/open").arg(target).spawn()?;
        return Ok(());
    }
    if IS_WINDOWS {
        Command::new("cmd")
            .args(["/C", "start", "", target])
            .spawn()?;
        return Ok(());
    }
    let opener = which("xdg-open").ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::NotFound,
            "xdg-open is required to open files and folders.",
        )
    })?;
    Command::new(opener).arg(target).spawn()?;
    Ok(())
}

pub fn reveal_path(target: &Path) -> io::Result<()> {
    let path = expand_user(target);
    if external_open_disabled() {
        return Err(io::Error::new(
            io::ErrorKind::NotFound,
            format!("External opening is disabled ({EXTERNAL_OPEN_DISABLED_ENV} is set)."),
        ));
    }
    if IS_WINDOWS {
        if path.is_file() {
            Command::new("explorer")
                .args(["/select,", &path.to_string_lossy()])
                .spawn()?;
        } else {
            open_path(&path.to_string_lossy())?;
        }
        return Ok(());
    }
    if IS_MACOS && path.exists() {
        Command::new("/usr/bin/open")
            .args(["-R", &path.to_string_lossy()])
            .spawn()?;
        return Ok(());
    }
    let dir = if path.is_dir() {
        path.as_path()
    } else {
        path.parent().unwrap_or(path.as_path())
    };
    open_path(&dir.to_string_lossy())
}

pub fn open_url(url: &str) -> io::Result<()> {
    open_path(url)
}

pub fn browser_application_commands() -> Vec<String> {
    if IS_WINDOWS {
        let mut out = Vec::new();
        for var in ["ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"] {
            if let Some(base) = env::var_os(var).filter(|v| !v.is_empty()) {
                let b = PathBuf::from(base);
                out.push(
                    b.join("Microsoft/Edge/Application/msedge.exe")
                        .to_string_lossy()
                        .into_owned(),
                );
                out.push(
                    b.join("Google/Chrome/Application/chrome.exe")
                        .to_string_lossy()
                        .into_owned(),
                );
                out.push(
                    b.join("BraveSoftware/Brave-Browser/Application/brave.exe")
                        .to_string_lossy()
                        .into_owned(),
                );
            }
        }
        out.extend(
            [
                "msedge.exe",
                "chrome.exe",
                "brave.exe",
                "chromium.exe",
                "firefox.exe",
            ]
            .into_iter()
            .map(String::from),
        );
        return out;
    }
    if IS_MACOS {
        return vec![
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "firefox",
        ]
        .into_iter()
        .map(String::from)
        .collect();
    }
    vec![
        "chromium",
        "chromium-browser",
        "google-chrome",
        "google-chrome-stable",
        "brave-browser",
        "microsoft-edge",
        "microsoft-edge-stable",
        "firefox",
    ]
    .into_iter()
    .map(String::from)
    .collect()
}

pub fn resolve_browser_application() -> Option<String> {
    for cand in browser_application_commands() {
        let p = Path::new(&cand);
        if p.is_absolute() || cand.contains(std::path::MAIN_SEPARATOR) {
            if p.is_file() {
                return Some(cand);
            }
            continue;
        }
        if let Some(found) = which(&cand) {
            return Some(found.to_string_lossy().into_owned());
        }
    }
    None
}

/// Probe `flatpak info <app_id>` with a 5-second timeout.
pub fn flatpak_app_installed(app_id: &str) -> bool {
    let Ok(mut child) = Command::new("flatpak")
        .args(["info", app_id])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
    else {
        return false;
    };
    let deadline = Instant::now() + Duration::from_secs(FLATPAK_PROBE_TIMEOUT_SECONDS);
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return status.success(),
            Ok(None) => {
                if Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    return false;
                }
                std::thread::sleep(Duration::from_millis(25));
            }
            Err(_) => return false,
        }
    }
}
