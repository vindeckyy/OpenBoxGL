//! SSE streaming endpoint (`GET /api/v2/events`) with subscriber cap (`503 SSE_BUSY`),
//! 15s `: ping\n\n` heartbeat, bounded event bytes, and shared security headers.
//! Mirrors `web_app.py::_api_get_api_events`.

use std::convert::Infallible;
use std::sync::Arc;
use std::time::Duration;

use axum::body::Body;
use axum::http::StatusCode;
use axum::http::header::{CONNECTION, HeaderName, HeaderValue, RETRY_AFTER};
use axum::response::Response;
use bytes::Bytes;
use openbox_runtime::{AppState, SSE_BUSY_RETRY_AFTER, SSE_MAX_EVENT_BYTES};
use serde_json::json;

use crate::middleware::{apply_security_headers, json_response_with_headers};

pub fn handle_sse(app: Arc<AppState>, instance: &str) -> Response {
    let Some(sub) = app.sse.subscribe() else {
        let retry = SSE_BUSY_RETRY_AFTER.to_string();
        return json_response_with_headers(
            StatusCode::SERVICE_UNAVAILABLE,
            &json!({
                "error": "Server is busy. Close an unused tab and try again.",
                "code": "SSE_BUSY",
                "retry_after": SSE_BUSY_RETRY_AFTER,
            }),
            instance,
            &[(RETRY_AFTER.as_str(), retry.as_str())],
        );
    };

    let stream = futures_util::stream::unfold(sub, |mut sub| async move {
        match tokio::time::timeout(Duration::from_secs(15), sub.rx.recv()).await {
            Ok(Some(msg)) => {
                let frame = format!("event: {}\ndata: {}\n\n", msg.kind, msg.data);
                let chunk = if frame.len() > SSE_MAX_EVENT_BYTES {
                    Bytes::from_static(b": ping\n\n")
                } else {
                    Bytes::from(frame)
                };
                Some((Ok::<Bytes, Infallible>(chunk), sub))
            }
            Ok(None) => None,
            Err(_) => Some((
                Ok::<Bytes, Infallible>(Bytes::from_static(b": ping\n\n")),
                sub,
            )),
        }
    });

    let mut resp = Response::new(Body::from_stream(stream));
    *resp.status_mut() = StatusCode::OK;
    apply_security_headers(resp.headers_mut(), "text/event-stream", "no-cache", false);
    resp.headers_mut()
        .insert(CONNECTION, HeaderValue::from_static("close"));
    resp.headers_mut().insert(
        HeaderName::from_static("x-accel-buffering"),
        HeaderValue::from_static("no"),
    );
    resp
}
