//! Static file serving with ETag, Last-Modified, 304 Not Modified, and HTTP Range (206/416).
//! Mirrors `Handler.send_file`, `_api_get_index`, `_api_get_static`, `_api_get_locale`, `_api_get_favicon`.

use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;
use std::time::UNIX_EPOCH;

use axum::body::Body;
use axum::http::header::{
    ACCEPT_RANGES, CONTENT_LENGTH, CONTENT_RANGE, ETAG, IF_MODIFIED_SINCE, IF_NONE_MATCH,
    LAST_MODIFIED, RANGE,
};
use axum::http::{HeaderMap, HeaderValue, StatusCode};
use axum::response::Response;
use openbox_core::ApiError;

use crate::middleware::{api_error_response, apply_security_headers, bytes_response};

fn format_http_date(secs: i64) -> String {
    jiff::Timestamp::from_second(secs)
        .ok()
        .map(|ts| ts.strftime("%a, %d %b %Y %H:%M:%S GMT").to_string())
        .unwrap_or_else(|| "Thu, 01 Jan 1970 00:00:00 GMT".to_owned())
}

fn parse_http_date(raw: &str) -> Option<i64> {
    let trimmed = raw.trim();
    // IMF-fixdate: "Sun, 06 Nov 1994 08:49:37 GMT"
    jiff::civil::DateTime::strptime("%a, %d %b %Y %H:%M:%S GMT", trimmed)
        .ok()
        .and_then(|dt| jiff::tz::TimeZone::UTC.to_timestamp(dt).ok())
        .map(|ts| ts.as_second())
}

pub fn send_file(
    req_headers: &HeaderMap,
    path: &Path,
    content_type_override: Option<&str>,
    cache_control_override: Option<&str>,
    frameable: bool,
    request_id: &str,
    instance: &str,
) -> Response {
    let meta = match std::fs::metadata(path) {
        Ok(m) if m.is_file() => m,
        _ => {
            return api_error_response(
                &ApiError::route_not_found("Not found"),
                request_id,
                instance,
            );
        }
    };
    let size = meta.len();
    let mtime = meta
        .modified()
        .ok()
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .unwrap_or_default();
    let mtime_ns = mtime.as_nanos();
    let mtime_secs = mtime.as_secs() as i64;
    let etag = format!("\"{mtime_ns:x}-{size:x}\"");
    let last_modified = format_http_date(mtime_secs);
    let content_type = content_type_override.map(str::to_owned).unwrap_or_else(|| {
        mime_guess::from_path(path)
            .first_or_octet_stream()
            .essence_str()
            .to_owned()
    });
    let cache_control = cache_control_override.unwrap_or("private, max-age=31536000, immutable");

    let inm = req_headers
        .get(IF_NONE_MATCH)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    if !inm.is_empty() && inm.split(',').any(|item| item.trim() == etag) {
        let mut resp = Response::new(Body::empty());
        *resp.status_mut() = StatusCode::NOT_MODIFIED;
        apply_security_headers(resp.headers_mut(), &content_type, cache_control, frameable);
        if let Ok(v) = HeaderValue::from_str(&etag) {
            resp.headers_mut().insert(ETAG, v);
        }
        if let Ok(v) = HeaderValue::from_str(&last_modified) {
            resp.headers_mut().insert(LAST_MODIFIED, v);
        }
        return resp;
    }
    if inm.is_empty()
        && let Some(ims) = req_headers
            .get(IF_MODIFIED_SINCE)
            .and_then(|v| v.to_str().ok())
        && let Some(since_secs) = parse_http_date(ims)
        && mtime_secs < since_secs + 1
    {
        let mut resp = Response::new(Body::empty());
        *resp.status_mut() = StatusCode::NOT_MODIFIED;
        apply_security_headers(resp.headers_mut(), &content_type, cache_control, frameable);
        if let Ok(v) = HeaderValue::from_str(&etag) {
            resp.headers_mut().insert(ETAG, v);
        }
        if let Ok(v) = HeaderValue::from_str(&last_modified) {
            resp.headers_mut().insert(LAST_MODIFIED, v);
        }
        return resp;
    }

    let mut start: u64 = 0;
    let mut end: u64 = size.saturating_sub(1);
    let mut status = StatusCode::OK;

    if let Some(range_raw) = req_headers
        .get(RANGE)
        .and_then(|v| v.to_str().ok())
        .and_then(|s| s.strip_prefix("bytes="))
    {
        let spec = range_raw.split(',').next().unwrap_or("").trim();
        let Some((left, right)) = spec.split_once('-') else {
            return api_error_response(
                &ApiError::range_parse("Invalid byte range."),
                request_id,
                instance,
            );
        };
        if !left.is_empty() {
            let Ok(l) = left.parse::<u64>() else {
                return api_error_response(
                    &ApiError::range_parse("Invalid byte range."),
                    request_id,
                    instance,
                );
            };
            start = l;
            if !right.is_empty() {
                let Ok(r) = right.parse::<u64>() else {
                    return api_error_response(
                        &ApiError::range_parse("Invalid byte range."),
                        request_id,
                        instance,
                    );
                };
                end = r;
            }
        } else if !right.is_empty() {
            let Ok(suffix) = right.parse::<u64>() else {
                return api_error_response(
                    &ApiError::range_parse("Invalid byte range."),
                    request_id,
                    instance,
                );
            };
            start = size.saturating_sub(suffix);
        }
        if size == 0 || start >= size || end < start {
            let mut resp = Response::new(Body::empty());
            *resp.status_mut() = StatusCode::RANGE_NOT_SATISFIABLE;
            apply_security_headers(resp.headers_mut(), &content_type, cache_control, frameable);
            if let Ok(v) = HeaderValue::from_str(&format!("bytes */{size}")) {
                resp.headers_mut().insert(CONTENT_RANGE, v);
            }
            resp.headers_mut()
                .insert(CONTENT_LENGTH, HeaderValue::from_static("0"));
            return resp;
        }
        end = end.min(size - 1);
        status = StatusCode::PARTIAL_CONTENT;
    }

    let length = if size == 0 { 0 } else { end - start + 1 };
    let mut buf = vec![0u8; length as usize];
    if length > 0 {
        let Ok(mut file) = File::open(path) else {
            return api_error_response(
                &ApiError::route_not_found("Not found"),
                request_id,
                instance,
            );
        };
        if file.seek(SeekFrom::Start(start)).is_err() || file.read_exact(&mut buf).is_err() {
            return api_error_response(
                &ApiError::route_not_found("Not found"),
                request_id,
                instance,
            );
        }
    }

    let mut resp = Response::new(Body::from(buf));
    *resp.status_mut() = status;
    apply_security_headers(resp.headers_mut(), &content_type, cache_control, frameable);
    resp.headers_mut()
        .insert(ACCEPT_RANGES, HeaderValue::from_static("bytes"));
    if let Ok(v) = HeaderValue::from_str(&etag) {
        resp.headers_mut().insert(ETAG, v);
    }
    if let Ok(v) = HeaderValue::from_str(&last_modified) {
        resp.headers_mut().insert(LAST_MODIFIED, v);
    }
    if let Ok(v) = HeaderValue::from_str(&length.to_string()) {
        resp.headers_mut().insert(CONTENT_LENGTH, v);
    }
    if status == StatusCode::PARTIAL_CONTENT
        && let Ok(v) = HeaderValue::from_str(&format!("bytes {start}-{end}/{size}"))
    {
        resp.headers_mut().insert(CONTENT_RANGE, v);
    }
    resp
}

pub fn serve_public_route(
    root_dir: &Path,
    path: &str,
    req_headers: &HeaderMap,
    request_id: &str,
    instance: &str,
) -> Option<Response> {
    match path {
        "/" | "/index.html" => {
            let index = root_dir.join("index.html");
            match std::fs::read(&index) {
                Ok(bytes) => Some(bytes_response(
                    StatusCode::OK,
                    bytes,
                    "text/html; charset=utf-8",
                    "no-store",
                    false,
                    &[],
                )),
                Err(_) => Some(api_error_response(
                    &ApiError::route_not_found("Not found"),
                    request_id,
                    instance,
                )),
            }
        }
        "/assets/placeholder-cover.svg" => Some(send_file(
            req_headers,
            &root_dir.join("assets").join("placeholder-cover.svg"),
            Some("image/svg+xml"),
            Some("public, max-age=31536000, immutable"),
            false,
            request_id,
            instance,
        )),
        "/favicon.ico" | "/favicon.svg" => {
            let icon = root_dir.join("openbox.svg");
            match std::fs::read(&icon) {
                Ok(bytes) => Some(bytes_response(
                    StatusCode::OK,
                    bytes,
                    "image/svg+xml",
                    "no-store",
                    false,
                    &[],
                )),
                Err(_) => Some(api_error_response(
                    &ApiError::route_not_found("Not found"),
                    request_id,
                    instance,
                )),
            }
        }
        p if p.starts_with("/locales/") => {
            let name = p.strip_prefix("/locales/")?;
            if name.contains('/') || !name.ends_with(".json") {
                return Some(api_error_response(
                    &ApiError::route_not_found("Not found"),
                    request_id,
                    instance,
                ));
            }
            Some(send_file(
                req_headers,
                &root_dir.join("locales").join(name),
                Some("application/json; charset=utf-8"),
                None,
                false,
                request_id,
                instance,
            ))
        }
        p if p.starts_with("/static/") => {
            let name = p.strip_prefix("/static/")?;
            if name.contains('/') || name.is_empty() {
                return Some(api_error_response(
                    &ApiError::route_not_found("Not found"),
                    request_id,
                    instance,
                ));
            }
            if name == "logo.png" {
                return Some(send_file(
                    req_headers,
                    &root_dir.join("assets").join("openbox-logo.png"),
                    Some("image/png"),
                    None,
                    false,
                    request_id,
                    instance,
                ));
            }
            let ct = if name.ends_with(".js") {
                "text/javascript; charset=utf-8"
            } else if name.ends_with(".svg") {
                "image/svg+xml"
            } else if name == "app.css" {
                "text/css; charset=utf-8"
            } else {
                return Some(api_error_response(
                    &ApiError::route_not_found("Not found"),
                    request_id,
                    instance,
                ));
            };
            Some(send_file(
                req_headers,
                &root_dir.join("static").join(name),
                Some(ct),
                Some("no-cache"),
                false,
                request_id,
                instance,
            ))
        }
        _ => None,
    }
}
