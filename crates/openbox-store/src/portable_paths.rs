//! Portable library paths: encode, decode, and containment for `@library/...` tokens (ADR 0090).
//! Pure port of `pkg/state/paths.py`.

use thiserror::Error;
use unicode_normalization::UnicodeNormalization;

pub const LIBRARY_TOKEN: &str = "@library";
pub const CODE_TOKEN_ESCAPE: &str = "PATH_TOKEN_ESCAPE";
pub const CODE_TOKEN_INVALID: &str = "PATH_TOKEN_INVALID";
pub const CODE_ROOT_MISSING: &str = "LIBRARY_ROOT_MISSING";
pub const CODE_ROOT_INVALID: &str = "LIBRARY_ROOT_INVALID";

#[derive(Debug, Clone, Error)]
#[error("{message}")]
pub struct PathTokenError {
    pub code: &'static str,
    pub message: String,
}

impl PathTokenError {
    pub fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct Lexical {
    prefix: String,
    parts: Vec<String>,
}

fn fold(text: &str, case_insensitive: bool) -> String {
    let nfc: String = text.nfc().collect();
    if case_insensitive {
        nfc.to_lowercase()
    } else {
        nfc
    }
}

fn has_drive(value: &str) -> bool {
    let b = value.as_bytes();
    b.len() >= 3 && b[0].is_ascii_alphabetic() && b[1] == b':' && (b[2] == b'/' || b[2] == b'\\')
}

pub fn is_absolute(value: &str, windows: bool) -> bool {
    if value.starts_with('/') {
        return true;
    }
    windows && (has_drive(value) || value.starts_with("\\\\"))
}

pub fn is_unc(value: &str, windows: bool) -> bool {
    windows && (value.starts_with("\\\\") || value.starts_with("//"))
}

pub fn is_token(value: &str) -> bool {
    value == LIBRARY_TOKEN || value.starts_with("@library/") || value.starts_with("@library\\")
}

fn lexical(value: &str, windows: bool) -> Option<Lexical> {
    let owned;
    let text = if windows {
        owned = value.replace('\\', "/");
        owned.as_str()
    } else {
        value
    };
    let (prefix, rest) = if windows && has_drive(text) {
        (format!("{}/", &text[..2]), &text[3..])
    } else {
        let r = text.strip_prefix('/')?;
        ("/".to_owned(), r)
    };
    let mut parts: Vec<String> = Vec::new();
    for seg in rest.split('/') {
        if seg.is_empty() || seg == "." {
            continue;
        }
        if seg == ".." {
            parts.pop()?;
            continue;
        }
        parts.push(seg.to_owned());
    }
    Some(Lexical { prefix, parts })
}

fn render(lex: &Lexical, windows: bool) -> String {
    let body = lex.parts.join("/");
    let text = if body.is_empty() {
        lex.prefix.clone()
    } else {
        format!("{}{body}", lex.prefix)
    };
    if windows {
        text.replace('/', "\\")
    } else {
        text
    }
}

pub fn is_within(path: &str, root: &str, windows: bool, case_insensitive: bool) -> bool {
    if !is_absolute(path, windows) || !is_absolute(root, windows) {
        return false;
    }
    if is_unc(path, windows) || is_unc(root, windows) {
        return false;
    }
    let (Some(left), Some(right)) = (lexical(path, windows), lexical(root, windows)) else {
        return false;
    };
    if fold(&left.prefix, windows) != fold(&right.prefix, windows) {
        return false;
    }
    if left.parts.len() < right.parts.len() {
        return false;
    }
    left.parts
        .iter()
        .zip(right.parts.iter())
        .all(|(l, r)| fold(l, case_insensitive) == fold(r, case_insensitive))
}

pub fn classify(value: &str, root: &str, windows: bool, case_insensitive: bool) -> &'static str {
    if is_token(value) {
        return "token";
    }
    if is_unc(value, windows) {
        return "unc";
    }
    if !is_absolute(value, windows) {
        return "relative";
    }
    if !root.is_empty() && is_within(value, root, windows, case_insensitive) {
        return "inside_root";
    }
    "absolute"
}

pub fn encode_path(value: &str, root: &str, windows: bool, case_insensitive: bool) -> String {
    if root.is_empty() || !is_absolute(root, windows) || is_unc(root, windows) {
        return value.to_owned();
    }
    if !is_absolute(value, windows) || is_unc(value, windows) {
        return value.to_owned();
    }
    let Some(lex) = lexical(value, windows) else {
        return value.to_owned();
    };
    if !is_within(value, root, windows, case_insensitive) {
        return render(&lex, false);
    }
    let Some(root_lex) = lexical(root, windows) else {
        return value.to_owned();
    };
    let rel = &lex.parts[root_lex.parts.len()..];
    if rel.is_empty() {
        LIBRARY_TOKEN.to_owned()
    } else {
        format!("{LIBRARY_TOKEN}/{}", rel.join("/"))
    }
}

pub fn decode_path(
    value: &str,
    root: &str,
    windows: bool,
    case_insensitive: bool,
) -> Result<String, PathTokenError> {
    if !is_token(value) {
        return Ok(value.to_owned());
    }
    if value.contains('\0') {
        return Err(PathTokenError::new(
            CODE_TOKEN_INVALID,
            "Path tokens cannot contain NUL bytes.",
        ));
    }
    let rel = if value.len() > LIBRARY_TOKEN.len() {
        &value[LIBRARY_TOKEN.len() + 1..]
    } else {
        ""
    };
    if root.is_empty() {
        return Err(PathTokenError::new(
            CODE_ROOT_MISSING,
            "This path is relative to the library root, but no library root is set.",
        ));
    }
    if !is_absolute(root, windows) || is_unc(root, windows) {
        return Err(PathTokenError::new(
            CODE_ROOT_INVALID,
            "The library root must be an absolute local path.",
        ));
    }
    if !rel.is_empty()
        && (rel.starts_with('/')
            || rel.starts_with('\\')
            || is_absolute(rel, windows)
            || (windows && has_drive(rel)))
    {
        return Err(PathTokenError::new(
            CODE_TOKEN_ESCAPE,
            "A library token cannot start with a separator or a drive.",
        ));
    }
    let normalized_rel = if windows {
        rel.replace('\\', "/")
    } else {
        rel.to_owned()
    };
    let segments: Vec<&str> = if normalized_rel.is_empty() {
        Vec::new()
    } else {
        normalized_rel.split('/').collect()
    };
    if segments.contains(&"..") {
        return Err(PathTokenError::new(
            CODE_TOKEN_ESCAPE,
            "A library token cannot contain '..'.",
        ));
    }
    if windows && segments.iter().any(|s| s.contains(':')) {
        return Err(PathTokenError::new(
            CODE_TOKEN_ESCAPE,
            "A library token cannot contain ':' on Windows.",
        ));
    }
    let base = lexical(root, windows).ok_or_else(|| {
        PathTokenError::new(CODE_ROOT_INVALID, "The library root cannot be normalized.")
    })?;
    let mut parts = base.parts;
    for seg in segments {
        if !seg.is_empty() && seg != "." {
            parts.push(seg.to_owned());
        }
    }
    let resolved = render(
        &Lexical {
            prefix: base.prefix,
            parts,
        },
        windows,
    );
    if !is_within(&resolved, root, windows, case_insensitive) {
        return Err(PathTokenError::new(
            CODE_TOKEN_ESCAPE,
            "The resolved path leaves the library root.",
        ));
    }
    Ok(resolved)
}

pub fn normalize_root(root: &str, windows: bool) -> Result<String, PathTokenError> {
    let text = root.trim();
    if text.is_empty() {
        return Ok(String::new());
    }
    if !is_absolute(text, windows) || is_unc(text, windows) {
        return Err(PathTokenError::new(
            CODE_ROOT_INVALID,
            "The library root must be an absolute local path.",
        ));
    }
    let lex = lexical(text, windows).ok_or_else(|| {
        PathTokenError::new(CODE_ROOT_INVALID, "The library root cannot be normalized.")
    })?;
    Ok(render(&lex, false))
}
