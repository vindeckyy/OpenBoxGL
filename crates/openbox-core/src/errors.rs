//! Structured API errors, RFC 9457 problem documents, and error-message path sanitization.
//! Mirrors `api_errors.py` and `web_app.py::_sanitize_error_message`.

use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Map, Value, json};

pub const PROBLEM_TYPE_BASE: &str = "https://openbox.local/problems/";

const RETRYABLE_STATUSES: &[u16] = &[408, 425, 429, 500, 502, 503, 504];

pub fn status_title(status: u16) -> &'static str {
    match status {
        400 => "Bad request",
        401 => "Unauthorized",
        403 => "Forbidden",
        404 => "Not found",
        405 => "Method not allowed",
        409 => "Conflict",
        410 => "Gone",
        413 => "Payload too large",
        415 => "Unsupported media type",
        422 => "Unprocessable content",
        429 => "Too many requests",
        500 => "Internal error",
        503 => "Service unavailable",
        _ => "Request failed",
    }
}

/// Structured HTTP error carrying status code, stable machine code, message, and optional detail.
#[derive(Debug, Clone)]
pub struct ApiError {
    pub status: u16,
    pub code: String,
    pub message: String,
    pub detail: Option<Value>,
    pub extra_headers: Vec<(String, String)>,
}

impl std::fmt::Display for ApiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "[{} {}] {}", self.status, self.code, self.message)
    }
}

impl std::error::Error for ApiError {}

impl ApiError {
    pub fn new(status: u16, code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            status,
            code: code.into(),
            message: message.into(),
            detail: None,
            extra_headers: Vec::new(),
        }
    }

    pub fn with_detail(mut self, detail: Value) -> Self {
        self.detail = Some(detail);
        self
    }

    pub fn with_header(mut self, name: impl Into<String>, value: impl Into<String>) -> Self {
        self.extra_headers.push((name.into(), value.into()));
        self
    }

    pub fn to_payload(&self, request_id: &str) -> Map<String, Value> {
        let mut map = Map::new();
        map.insert("error".into(), Value::String(self.message.clone()));
        map.insert("code".into(), Value::String(self.code.clone()));
        if let Some(detail) = &self.detail {
            map.insert("detail".into(), detail.clone());
        }
        if !request_id.is_empty() {
            map.insert("request_id".into(), Value::String(request_id.to_owned()));
        }
        map
    }

    pub fn to_problem(&self, request_id: &str, instance: &str) -> Value {
        problem_payload(
            self.status,
            &self.to_payload(request_id),
            request_id,
            instance,
        )
    }

    // Standard constructors mirroring api_errors.py classes
    pub fn bad_request(msg: impl Into<String>) -> Self {
        Self::new(400, "BAD_REQUEST", msg)
    }
    pub fn unauthorized(msg: impl Into<String>) -> Self {
        Self::new(403, "HTTP_403", msg)
    }
    pub fn v1_api_removed(msg: impl Into<String>) -> Self {
        Self::new(410, "V1_API_REMOVED", msg)
    }
    pub fn api_version_required(msg: impl Into<String>) -> Self {
        Self::new(410, "API_VERSION_REQUIRED", msg)
    }
    pub fn host_channel_retired(msg: impl Into<String>) -> Self {
        Self::new(410, "HOST_CHANNEL_RETIRED", msg)
    }
    pub fn not_found(msg: impl Into<String>) -> Self {
        Self::new(404, "NOT_FOUND", msg)
    }
    pub fn game_not_found(msg: impl Into<String>) -> Self {
        Self::new(404, "GAME_NOT_FOUND", msg)
    }
    pub fn media_not_found(msg: impl Into<String>) -> Self {
        Self::new(404, "MEDIA_NOT_FOUND", msg)
    }
    pub fn document_not_found(msg: impl Into<String>) -> Self {
        Self::new(404, "DOCUMENT_NOT_FOUND", msg)
    }
    pub fn badge_not_found(msg: impl Into<String>) -> Self {
        Self::new(404, "BADGE_NOT_FOUND", msg)
    }
    pub fn platform_document_not_found(msg: impl Into<String>) -> Self {
        Self::new(404, "PLATFORM_DOCUMENT_NOT_FOUND", msg)
    }
    pub fn route_not_found(msg: impl Into<String>) -> Self {
        Self::new(404, "ROUTE_NOT_FOUND", msg)
    }
    pub fn conflict(msg: impl Into<String>) -> Self {
        Self::new(409, "CONFLICT", msg)
    }
    pub fn job_state_conflict(msg: impl Into<String>) -> Self {
        Self::new(409, "JOB_STATE_CONFLICT", msg)
    }
    pub fn job_not_cancellable(msg: impl Into<String>) -> Self {
        Self::new(409, "JOB_NOT_CANCELLABLE", msg)
    }
    pub fn job_not_resumable(msg: impl Into<String>) -> Self {
        Self::new(409, "JOB_NOT_RESUMABLE", msg)
    }
    pub fn preview_not_found(msg: impl Into<String>) -> Self {
        Self::new(404, "PREVIEW_NOT_FOUND", msg)
    }
    pub fn preview_expired(msg: impl Into<String>) -> Self {
        Self::new(409, "PREVIEW_EXPIRED", msg)
    }
    pub fn preview_stale(msg: impl Into<String>) -> Self {
        Self::new(409, "PREVIEW_STALE", msg)
    }
    pub fn preview_library_changed(msg: impl Into<String>) -> Self {
        Self::new(409, "PREVIEW_LIBRARY_CHANGED", msg)
    }
    pub fn unresolved_candidates(msg: impl Into<String>) -> Self {
        Self::new(409, "UNRESOLVED_CANDIDATES", msg)
    }
    pub fn preview_limit_exceeded(msg: impl Into<String>) -> Self {
        Self::new(400, "PREVIEW_LIMIT_EXCEEDED", msg)
    }
    pub fn preview_entry_limit_exceeded(msg: impl Into<String>) -> Self {
        Self::new(400, "PREVIEW_ENTRY_LIMIT_EXCEEDED", msg)
    }
    pub fn host_api_unsupported(msg: impl Into<String>) -> Self {
        Self::new(409, "HOST_API_UNSUPPORTED", msg)
    }
    pub fn backup_format_unsupported(msg: impl Into<String>) -> Self {
        Self::new(400, "BACKUP_FORMAT_UNSUPPORTED", msg)
    }
    pub fn backup_format_retired(msg: impl Into<String>) -> Self {
        Self::new(410, "BACKUP_FORMAT_RETIRED", msg)
    }
    pub fn move_plan_stale(msg: impl Into<String>) -> Self {
        Self::new(409, "MOVE_PLAN_STALE", msg)
    }
    pub fn vault_snapshot_missing(msg: impl Into<String>) -> Self {
        Self::new(404, "VAULT_SNAPSHOT_MISSING", msg)
    }
    pub fn vault_game_mismatch(msg: impl Into<String>) -> Self {
        Self::new(409, "VAULT_GAME_MISMATCH", msg)
    }
    pub fn vault_path_escape(msg: impl Into<String>) -> Self {
        Self::new(400, "VAULT_PATH_ESCAPE", msg)
    }
    pub fn vault_limit_exceeded(msg: impl Into<String>) -> Self {
        Self::new(400, "VAULT_LIMIT_EXCEEDED", msg)
    }
    pub fn vault_no_saves(msg: impl Into<String>) -> Self {
        Self::new(409, "NO_SAVES", msg)
    }
    pub fn vault_safety_snapshot_failed(msg: impl Into<String>) -> Self {
        Self::new(409, "VAULT_SAFETY_SNAPSHOT_FAILED", msg)
    }
    pub fn ra_disabled(msg: impl Into<String>) -> Self {
        Self::new(409, "RA_DISABLED", msg)
    }
    pub fn ra_not_linked(msg: impl Into<String>) -> Self {
        Self::new(409, "RA_NOT_LINKED", msg)
    }
    pub fn trail_game_unknown(msg: impl Into<String>) -> Self {
        Self::new(404, "TRAIL_GAME_UNKNOWN", msg)
    }
    pub fn deals_disabled(msg: impl Into<String>) -> Self {
        Self::new(409, "DEALS_DISABLED", msg)
    }
    pub fn deals_invalid_appid(msg: impl Into<String>) -> Self {
        Self::new(400, "DEALS_INVALID_APPID", msg)
    }
    pub fn deals_not_found(msg: impl Into<String>) -> Self {
        Self::new(404, "DEALS_NOT_FOUND", msg)
    }
    pub fn deals_limit_exceeded(msg: impl Into<String>) -> Self {
        Self::new(409, "DEALS_LIMIT", msg)
    }
    pub fn deals_host_blocked(msg: impl Into<String>) -> Self {
        Self::new(400, "DEALS_HOST_BLOCKED", msg)
    }
    pub fn capsule_not_found(msg: impl Into<String>) -> Self {
        Self::new(404, "CAPSULE_NOT_FOUND", msg)
    }
    pub fn capsule_locked(msg: impl Into<String>) -> Self {
        Self::new(409, "CAPSULE_LOCKED", msg)
    }
    pub fn capsule_invalid_date(msg: impl Into<String>) -> Self {
        Self::new(400, "CAPSULE_INVALID_DATE", msg)
    }
    pub fn capsule_body_too_large(msg: impl Into<String>) -> Self {
        Self::new(400, "CAPSULE_BODY_TOO_LARGE", msg)
    }
    pub fn itinerary_no_games(msg: impl Into<String>) -> Self {
        Self::new(409, "ITINERARY_NO_GAMES", msg)
    }
    pub fn itinerary_invalid_strategy(msg: impl Into<String>) -> Self {
        Self::new(400, "ITINERARY_INVALID_STRATEGY", msg)
    }
    pub fn internal(msg: impl Into<String>) -> Self {
        Self::new(500, "INTERNAL_ERROR", msg)
    }
}

/// Build an RFC 9457 problem document matching `api_errors.py::problem_payload`.
pub fn problem_payload(
    status: u16,
    payload: &Map<String, Value>,
    request_id: &str,
    instance: &str,
) -> Value {
    let title = status_title(status);
    let detail_val = payload.get("detail");
    let error_str = payload.get("error").and_then(Value::as_str).unwrap_or("");
    let message = if !error_str.is_empty() {
        error_str.to_owned()
    } else if let Some(s) = detail_val.and_then(Value::as_str).filter(|s| !s.is_empty()) {
        s.to_owned()
    } else {
        title.to_owned()
    };

    let code = payload
        .get("code")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(ToOwned::to_owned)
        .unwrap_or_else(|| format!("HTTP_{status}"));

    let mut body = payload.clone();
    body.insert(
        "type".into(),
        Value::String(format!(
            "{PROBLEM_TYPE_BASE}{}",
            code.to_ascii_lowercase().replace('_', "-")
        )),
    );
    body.insert("title".into(), Value::String(title.to_owned()));
    body.insert("status".into(), json!(status));
    body.insert("detail".into(), Value::String(message.clone()));
    body.insert("code".into(), Value::String(code));
    body.insert("error".into(), Value::String(message));
    body.insert(
        "retryable".into(),
        Value::Bool(RETRYABLE_STATUSES.contains(&status) || payload.contains_key("retry_after")),
    );
    if let Some(Value::Object(ctx)) = detail_val {
        body.insert("context".into(), Value::Object(ctx.clone()));
    }
    if !request_id.is_empty() {
        body.insert("request_id".into(), Value::String(request_id.to_owned()));
    }
    if !instance.is_empty() {
        body.insert("instance".into(), Value::String(instance.to_owned()));
    }
    Value::Object(body)
}

static PATH_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"(?:~)?/[^\s"']*|[A-Za-z]:\\[^\s"']*|\\\\[^\s"']+"#).expect("valid path regex")
});
static PREFIX_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^([^:]*?):\s+(.*)$").expect("valid prefix regex"));
static DUP_PATH_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\[path\](?:\s*\[path\])+").expect("valid dup path regex"));
static COLON_PATH_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\s*:\s*\[path\].*").expect("valid colon path regex"));

/// Exact port of `web_app.py::_sanitize_error_message`.
pub fn sanitize_error_message(raw: &str) -> String {
    if raw.is_empty() || !PATH_RE.is_match(raw) {
        return raw.to_owned();
    }
    if let Some(caps) = PREFIX_RE.captures(raw) {
        let rest = caps.get(2).map(|m| m.as_str()).unwrap_or("");
        if PATH_RE.is_match(rest) {
            let mut prefix = caps
                .get(1)
                .map(|m| m.as_str())
                .unwrap_or("")
                .trim()
                .to_owned();
            if !prefix.ends_with('.') {
                prefix.push('.');
            }
            return prefix;
        }
    }
    let step1 = PATH_RE.replace_all(raw, "[path]");
    let step2 = DUP_PATH_RE.replace_all(&step1, "[path]");
    let step3 = COLON_PATH_RE.replace_all(&step2, ".");
    let trimmed = step3.trim();
    if trimmed.is_empty() {
        raw.to_owned()
    } else {
        trimmed.to_owned()
    }
}
