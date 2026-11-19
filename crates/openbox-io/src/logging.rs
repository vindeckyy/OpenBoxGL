//! Local diagnostic logging with secret redaction mirroring `openbox_logging.py`.

use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use regex::Regex;

pub const LOG_FILE: &str = "openbox.log";
pub const MAX_LOG_BYTES: u64 = 2 * 1024 * 1024;
pub const BACKUP_COUNT: usize = 4;

static BEARER_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)(authorization\s*[:=]\s*bearer\s+)\S+").expect("bearer re"));
static JSON_SECRET_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"(?i)("(?:token|password|secret|api[_-]?key|authorization|client[_-]?secret)"\s*:\s*")[^"]*"#)
        .expect("json secret re")
});
static PYTHON_SECRET_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"(?i)(['"]?(?:token|password|secret|api[_-]?key|authorization|client[_-]?secret)['"]?\s*[:=]\s*['"]?)[^\s,}\]"']+"#)
        .expect("python secret re")
});
static SECRET_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"(?i)(token|password|secret|api[_-]?key|authorization|client[_-]?secret|ra[\s]+(?:api[\s]+)?key)([=:]\s*)([^\s,&"']+)"#)
        .expect("secret re")
});

/// Redact bearer tokens, JSON secrets, and key=value credentials from log text.
pub fn redact_log_text(input: &str) -> String {
    let s1 = BEARER_RE.replace_all(input, "${1}<redacted>");
    let s2 = JSON_SECRET_RE.replace_all(&s1, "${1}<redacted>");
    let s3 = PYTHON_SECRET_RE.replace_all(&s2, "${1}<redacted>");
    SECRET_RE
        .replace_all(&s3, "${1}${2}<redacted>")
        .into_owned()
}

pub fn diagnostic_log_path(data_dir: &Path) -> PathBuf {
    data_dir.join(LOG_FILE)
}

pub fn read_diagnostic_log(data_dir: &Path, limit: usize) -> String {
    let path = diagnostic_log_path(data_dir);
    let Ok(mut file) = File::open(&path) else {
        return "No diagnostic log has been written yet.".to_owned();
    };
    let Ok(len) = file.seek(SeekFrom::End(0)) else {
        return "No diagnostic log has been written yet.".to_owned();
    };
    let start = len.saturating_sub(limit as u64);
    let _ = file.seek(SeekFrom::Start(start));
    let mut buf = Vec::new();
    if file.read_to_end(&mut buf).is_err() {
        return "No diagnostic log has been written yet.".to_owned();
    }
    redact_log_text(&String::from_utf8_lossy(&buf))
}

/// Append a redacted line to `openbox.log`, rotating up to `BACKUP_COUNT` files at `MAX_LOG_BYTES`.
pub fn append_diagnostic_line(data_dir: &Path, level: &str, message: &str) {
    let path = diagnostic_log_path(data_dir);
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    if fs::metadata(&path).map(|m| m.len()).unwrap_or(0) >= MAX_LOG_BYTES {
        for idx in (1..=BACKUP_COUNT).rev() {
            let dst = data_dir.join(format!("{LOG_FILE}.{idx}"));
            let src = if idx == 1 {
                path.clone()
            } else {
                data_dir.join(format!("{LOG_FILE}.{}", idx - 1))
            };
            if src.exists() {
                let _ = fs::rename(&src, &dst);
            }
        }
    }
    if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(&path) {
        let now = jiff::Timestamp::now();
        let clean = redact_log_text(message);
        let _ = writeln!(f, "{now} {level} openbox: {clean}");
    }
}
