//! Security headers, RFC 9457 error serialization, loopback Host guard,
//! constant-time token auth + 10/60s rate limiter, host API version guard,
//! and retired route guards. Mirrors `web_app.py` and `routes.py`.

use std::collections::HashMap;
use std::net::IpAddr;
use std::time::{Duration, Instant};

use axum::body::Body;
use axum::http::header::{
    CACHE_CONTROL, CONTENT_LENGTH, CONTENT_TYPE, HOST, HeaderName, HeaderValue, RETRY_AFTER,
};
use axum::http::{HeaderMap, Method, StatusCode};
use axum::response::Response;
use openbox_core::{ApiError, problem_payload, sanitize_error_message};
use openbox_store::StateError;
use parking_lot::Mutex;
use rand::RngCore;
use serde_json::{Value, json};
use subtle::ConstantTimeEq;

use crate::contracts::{CSP_DEFAULT, CSP_FRAMEABLE, public_twin_exists};

pub const MAX_BODY: usize = 65_536;
pub const REQUEST_TIMEOUT: Duration = Duration::from_secs(30);
pub const AUTH_MAX_FAILURES: usize = 10;
pub const AUTH_WINDOW: Duration = Duration::from_secs(60);

const LOOPBACK_HOSTS: &[&str] = &["127.0.0.1", "localhost", "::1"];

const RETIRED_HOST_PATHS: &[(&str, &str)] = &[
    ("/api/launch", "/api/host/launch"),
    ("/api/shutdown", "/api/host/shutdown"),
    ("/api/native/capabilities", "/api/host/capabilities"),
];

pub fn new_request_id() -> String {
    let mut buf = [0u8; 4];
    rand::thread_rng().fill_bytes(&mut buf);
    hex::encode(buf)
}

pub fn is_static_asset_path(path: &str) -> bool {
    let Some(name) = path.strip_prefix("/static/") else {
        return false;
    };
    if name.contains('/') || name.is_empty() {
        return false;
    }
    name.ends_with(".js") || name.ends_with(".svg") || name == "app.css" || name == "logo.png"
}

pub fn is_public_get_path(path: &str) -> bool {
    matches!(
        path,
        "/" | "/index.html"
            | "/assets/placeholder-cover.svg"
            | "/locales/en.json"
            | "/locales/es.json"
            | "/locales/de.json"
            | "/locales/fr.json"
            | "/locales/pt.json"
            | "/favicon.ico"
            | "/favicon.svg"
    ) || is_static_asset_path(path)
}

pub fn is_loopback_host(headers: &HeaderMap) -> bool {
    let Some(raw) = headers.get(HOST).and_then(|v| v.to_str().ok()) else {
        return false;
    };
    let host = raw.trim();
    if host.is_empty() {
        return false;
    }
    let hostname = if host.starts_with('[') {
        match host.find(']') {
            Some(end) => &host[1..end],
            None => host.trim_matches(|c| c == '[' || c == ']'),
        }
    } else if host.matches(':').count() == 1 {
        host.rsplit_once(':').map(|(h, _)| h).unwrap_or(host)
    } else if host.contains("::") && host.matches(':').count() > 1 {
        host
    } else if let Some((left, right)) = host.rsplit_once(':')
        && !right.is_empty()
        && right.chars().all(|c| c.is_ascii_digit())
    {
        left.trim_matches(|c| c == '[' || c == ']')
    } else {
        host.trim_matches(|c| c == '[' || c == ']')
    };
    let lower = hostname.trim().to_ascii_lowercase();
    LOOPBACK_HOSTS.contains(&lower.as_str())
}

pub fn apply_security_headers(
    headers: &mut HeaderMap,
    content_type: &str,
    cache_control: &str,
    frameable: bool,
) {
    if let Ok(v) = HeaderValue::from_str(content_type) {
        headers.insert(CONTENT_TYPE, v);
    }
    if let Ok(v) = HeaderValue::from_str(cache_control) {
        headers.insert(CACHE_CONTROL, v);
    }
    headers.insert(
        HeaderName::from_static("x-content-type-options"),
        HeaderValue::from_static("nosniff"),
    );
    headers.insert(
        HeaderName::from_static("referrer-policy"),
        HeaderValue::from_static("no-referrer"),
    );
    headers.insert(
        HeaderName::from_static("content-security-policy"),
        HeaderValue::from_static(if frameable {
            CSP_FRAMEABLE
        } else {
            CSP_DEFAULT
        }),
    );
    headers.insert(
        HeaderName::from_static("x-frame-options"),
        HeaderValue::from_static(if frameable { "SAMEORIGIN" } else { "DENY" }),
    );
}

pub fn json_response(status: StatusCode, payload: &Value, instance: &str) -> Response {
    json_response_with_headers(status, payload, instance, &[])
}

pub fn serialized_json_response<T: serde::Serialize>(status: StatusCode, payload: &T) -> Response {
    let bytes = serde_json::to_vec(payload).unwrap_or_else(|_| b"{}".to_vec());
    let len = bytes.len();
    let mut resp = Response::new(Body::from(bytes));
    *resp.status_mut() = status;
    apply_security_headers(
        resp.headers_mut(),
        "application/json; charset=utf-8",
        "no-store",
        false,
    );
    if let Ok(cl) = HeaderValue::from_str(&len.to_string()) {
        resp.headers_mut().insert(CONTENT_LENGTH, cl);
    }
    resp
}

pub fn json_response_with_headers(
    status: StatusCode,
    payload: &Value,
    instance: &str,
    extra: &[(&str, &str)],
) -> Response {
    let (bytes, ct) = if status.as_u16() >= 400
        && let Some(obj) = payload.as_object()
    {
        let req_id = obj.get("request_id").and_then(Value::as_str).unwrap_or("");
        let problem = problem_payload(status.as_u16(), obj, req_id, instance);
        (
            serde_json::to_vec(&problem).unwrap_or_else(|_| b"{}".to_vec()),
            "application/problem+json; charset=utf-8",
        )
    } else {
        (
            serde_json::to_vec(payload).unwrap_or_else(|_| b"{}".to_vec()),
            "application/json; charset=utf-8",
        )
    };
    let len = bytes.len();
    let mut resp = Response::new(Body::from(bytes));
    *resp.status_mut() = status;
    apply_security_headers(resp.headers_mut(), ct, "no-store", false);
    if let Ok(cl) = HeaderValue::from_str(&len.to_string()) {
        resp.headers_mut().insert(CONTENT_LENGTH, cl);
    }
    for (k, v) in extra {
        if let (Ok(hn), Ok(hv)) = (
            HeaderName::from_bytes(k.as_bytes()),
            HeaderValue::from_str(v),
        ) {
            resp.headers_mut().insert(hn, hv);
        }
    }
    resp
}

pub fn api_error_response(err: &ApiError, request_id: &str, instance: &str) -> Response {
    let status = StatusCode::from_u16(err.status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    let payload = Value::Object(err.to_payload(request_id));
    let extra: Vec<(&str, &str)> = err
        .extra_headers
        .iter()
        .map(|(k, v)| (k.as_str(), v.as_str()))
        .collect();
    json_response_with_headers(status, &payload, instance, &extra)
}

pub fn state_error_response(err: &StateError, request_id: &str, instance: &str) -> Response {
    match err.code() {
        "STATE_FUTURE_VERSION" => json_response(
            StatusCode::CONFLICT,
            &json!({"error": err.to_string(), "code": "STATE_FUTURE_VERSION", "request_id": request_id}),
            instance,
        ),
        "STATE_SCHEMA_TOO_OLD" => json_response(
            StatusCode::CONFLICT,
            &json!({"error": err.to_string(), "code": "STATE_SCHEMA_TOO_OLD", "request_id": request_id}),
            instance,
        ),
        "LIBRARY_FROZEN" => json_response(
            StatusCode::CONFLICT,
            &json!({"error": err.to_string(), "code": "LIBRARY_FROZEN", "request_id": request_id}),
            instance,
        ),
        _ => json_response(
            StatusCode::SERVICE_UNAVAILABLE,
            &json!({
                "error": "OpenBox library data needs recovery before this operation can continue.",
                "code": "STATE_UNAVAILABLE",
                "request_id": request_id,
            }),
            instance,
        ),
    }
}

pub fn bad_request_response(msg: &str, request_id: &str, instance: &str) -> Response {
    let clean = sanitize_error_message(msg);
    api_error_response(&ApiError::bad_request(clean), request_id, instance)
}

pub fn bytes_response(
    status: StatusCode,
    bytes: Vec<u8>,
    content_type: &str,
    cache_control: &str,
    frameable: bool,
    extra: &[(&str, &str)],
) -> Response {
    let len = bytes.len();
    let mut resp = Response::new(Body::from(bytes));
    *resp.status_mut() = status;
    apply_security_headers(resp.headers_mut(), content_type, cache_control, frameable);
    if let Ok(cl) = HeaderValue::from_str(&len.to_string()) {
        resp.headers_mut().insert(CONTENT_LENGTH, cl);
    }
    for (k, v) in extra {
        if let (Ok(hn), Ok(hv)) = (
            HeaderName::from_bytes(k.as_bytes()),
            HeaderValue::from_str(v),
        ) {
            resp.headers_mut().insert(hn, hv);
        }
    }
    resp
}

pub fn check_host_api_version(headers: &HeaderMap, path: &str) -> Result<(), ApiError> {
    if !path.starts_with("/api/host/") {
        return Ok(());
    }
    let requested = headers
        .get("x-openbox-api-version")
        .and_then(|v| v.to_str().ok())
        .map(str::trim)
        .unwrap_or("");
    if !requested.is_empty() && requested.split('.').next() != Some("2") {
        return Err(
            ApiError::host_api_unsupported(
                "This OpenBox window is from a different version. Restart OpenBox and the window together.",
            )
            .with_detail(json!({"requested": requested, "supported": "2"})),
        );
    }
    Ok(())
}

pub fn check_retired_route(method: &Method, path: &str) -> Result<(), ApiError> {
    if let Some(rest) = path.strip_prefix("/api/v1")
        && (rest.is_empty() || rest.starts_with('/'))
    {
        return Err(ApiError::v1_api_removed(
            "The /api/v1 API was removed in OpenBox 2.1. Use /api/v2 instead.",
        )
        .with_detail(json!({
            "retired": path,
            "use": format!("/api/v2{rest}"),
            "since": "2.1.0",
        })));
    }
    for &(retired, replacement) in RETIRED_HOST_PATHS {
        if path == retired {
            return Err(
                ApiError::host_channel_retired(
                    "This native host path was removed in OpenBox 2.1. Update the native window to use /api/host.",
                )
                .with_detail(json!({
                    "retired": path,
                    "use": replacement,
                    "since": "2.1.0",
                })),
            );
        }
    }
    if path.starts_with("/api/") && !path.starts_with("/api/v2/") && !path.starts_with("/api/host/")
    {
        let m = method.as_str();
        if public_twin_exists(m, path) {
            let suffix = path.strip_prefix("/api").unwrap_or("");
            return Err(ApiError::api_version_required(
                "This API path is no longer served unversioned. Use /api/v2 instead.",
            )
            .with_detail(json!({
                "retired": path,
                "use": format!("/api/v2{suffix}"),
                "since": "2.1.0",
            })));
        }
        return Err(ApiError::route_not_found("Not found"));
    }
    Ok(())
}

pub struct AuthRateLimiter {
    failures: Mutex<HashMap<IpAddr, Vec<Instant>>>,
}

impl Default for AuthRateLimiter {
    fn default() -> Self {
        Self::new()
    }
}

impl AuthRateLimiter {
    pub fn new() -> Self {
        Self {
            failures: Mutex::new(HashMap::new()),
        }
    }

    pub fn verify_token(
        &self,
        expected: &str,
        provided: &str,
        client_ip: IpAddr,
        instance: &str,
    ) -> Option<Response> {
        let eq = provided.len() == expected.len()
            && bool::from(provided.as_bytes().ct_eq(expected.as_bytes()));
        if eq {
            self.failures.lock().remove(&client_ip);
            return None;
        }
        let retry_after = self.record_failure(client_ip);
        if let Some(secs) = retry_after {
            let s = secs.to_string();
            let resp = json_response_with_headers(
                StatusCode::TOO_MANY_REQUESTS,
                &json!({
                    "error": "Too many authentication failures. Try again later.",
                    "code": "RATE_LIMITED",
                    "retry_after": secs,
                }),
                instance,
                &[(RETRY_AFTER.as_str(), s.as_str())],
            );
            return Some(resp);
        }
        Some(json_response(
            StatusCode::FORBIDDEN,
            &json!({"error": "Unauthorized"}),
            instance,
        ))
    }

    fn record_failure(&self, ip: IpAddr) -> Option<u64> {
        let now = Instant::now();
        let mut guard = self.failures.lock();
        let attempts = guard.entry(ip).or_default();
        attempts.retain(|t| now.duration_since(*t) < AUTH_WINDOW);
        attempts.push(now);
        if attempts.len() >= AUTH_MAX_FAILURES {
            let oldest = attempts.first().copied().unwrap_or(now);
            let elapsed = now.duration_since(oldest).as_secs_f64();
            let remaining = (AUTH_WINDOW.as_secs_f64() - elapsed).max(0.0) as u64 + 1;
            return Some(remaining);
        }
        None
    }
}

pub fn extract_token(headers: &HeaderMap, query: Option<&str>) -> String {
    if let Some(tok) = headers
        .get("x-openbox-token")
        .and_then(|v| v.to_str().ok())
        .filter(|s| !s.is_empty())
    {
        return tok.to_owned();
    }
    if let Some(q) = query {
        for (k, v) in url::form_urlencoded::parse(q.as_bytes()) {
            if k == "token" && !v.is_empty() {
                return v.into_owned();
            }
        }
    }
    String::new()
}
