//! Axum-based HTTP server, security middleware, declarative 417-route contract registry,
//! static Range/304 file serving, SSE streaming, Strangler proxy, and domain handlers.

pub mod contracts;
pub mod handlers;
pub mod middleware;
pub mod router;
pub mod server;
pub mod sse;
pub mod static_files;
pub mod strangler;

pub use contracts::{
    CSP_DEFAULT, CSP_FRAMEABLE, HOST_API_ROUTES, PUBLIC_API_ROUTES, SHELL_ROUTES,
    dump_contracts_json, is_registered_api_route, public_twin_exists,
};
pub use router::{HttpState, build_router};
pub use server::{RunningServer, bind_loopback, serve};
pub use strangler::StranglerProxy;

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::http::{Method, Request, StatusCode};
    use http_body_util::BodyExt;
    use openbox_runtime::{AppState, SSE_MAX_SUBSCRIBERS};
    use serde_json::{Value, json};
    use tower::ServiceExt;

    fn test_router(dir: &std::path::Path) -> (axum::Router, std::sync::Arc<AppState>) {
        std::fs::write(dir.join("index.html"), "<!doctype html><title>OB</title>").unwrap();
        std::fs::create_dir_all(dir.join("static")).unwrap();
        std::fs::write(dir.join("static").join("app.js"), "console.log('hi');").unwrap();
        std::fs::write(dir.join("openbox.svg"), "<svg/>").unwrap();
        let app = AppState::new(
            Some(dir.join("data")),
            dir.to_path_buf(),
            Some("test-token-123".to_owned()),
        );
        app.store.save(&openbox_core::default_state()).unwrap();
        let state = HttpState::new(std::sync::Arc::clone(&app), None);
        (build_router(state), app)
    }

    async fn body_json(resp: axum::response::Response) -> Value {
        let bytes = resp.into_body().collect().await.unwrap().to_bytes();
        serde_json::from_slice(&bytes).unwrap()
    }

    #[tokio::test]
    async fn contracts_match_417_api_routes() {
        assert_eq!(PUBLIC_API_ROUTES.len(), 413);
        assert_eq!(HOST_API_ROUTES.len(), 4);
        let dump = dump_contracts_json();
        assert_eq!(dump["app_version"], json!("3.0.0"));
        assert_eq!(dump["known_settings"].as_array().unwrap().len(), 161);
    }

    #[tokio::test]
    async fn security_host_auth_rate_limit_retired_and_csp() {
        let dir = tempfile::tempdir().unwrap();
        let (router, _app) = test_router(dir.path());

        // 1. Bad Host header -> 403 BAD_HOST + RFC 9457 problem+json
        let resp = router
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/")
                    .header("Host", "evil.example.com:8080")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::FORBIDDEN);
        assert!(
            resp.headers()["content-type"]
                .to_str()
                .unwrap()
                .starts_with("application/problem+json")
        );
        assert_eq!(resp.headers()["x-frame-options"], "DENY");
        assert_eq!(body_json(resp).await["code"], json!("BAD_HOST"));

        // 2. Public index.html works without token and carries CSP_DEFAULT
        let resp = router
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/")
                    .header("Host", "127.0.0.1:8080")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        assert_eq!(resp.headers()["content-security-policy"], CSP_DEFAULT);

        // 3. Static asset ETag / 304 / Range 206 / 416
        let resp = router
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/static/app.js")
                    .header("Host", "127.0.0.1:8080")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        assert_eq!(resp.headers()["cache-control"], "no-cache");
        let etag = resp.headers()["etag"].to_str().unwrap().to_owned();

        let resp304 = router
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/static/app.js")
                    .header("Host", "127.0.0.1:8080")
                    .header("If-None-Match", &etag)
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp304.status(), StatusCode::NOT_MODIFIED);

        let resp206 = router
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/static/app.js")
                    .header("Host", "127.0.0.1:8080")
                    .header("Range", "bytes=0-6")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp206.status(), StatusCode::PARTIAL_CONTENT);
        assert_eq!(resp206.headers()["content-range"], "bytes 0-6/18");

        let resp416 = router
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/static/app.js")
                    .header("Host", "127.0.0.1:8080")
                    .header("Range", "bytes=999-1000")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp416.status(), StatusCode::RANGE_NOT_SATISFIABLE);

        // 4. Retired /api/v1/* -> 410 V1_API_REMOVED
        let resp = router
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/api/v1/library")
                    .header("Host", "127.0.0.1:8080")
                    .header("X-OpenBox-Token", "test-token-123")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::GONE);
        assert_eq!(body_json(resp).await["code"], json!("V1_API_REMOVED"));

        // 5. Legacy host path -> 410 HOST_CHANNEL_RETIRED
        let resp = router
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/api/native/capabilities")
                    .header("Host", "127.0.0.1:8080")
                    .header("X-OpenBox-Token", "test-token-123")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::GONE);
        assert_eq!(body_json(resp).await["code"], json!("HOST_CHANNEL_RETIRED"));

        // 6. Unversioned /api/* with v2 twin -> 410 API_VERSION_REQUIRED
        let resp = router
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/api/log")
                    .header("Host", "127.0.0.1:8080")
                    .header("X-OpenBox-Token", "test-token-123")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::GONE);
        assert_eq!(body_json(resp).await["code"], json!("API_VERSION_REQUIRED"));

        // 7. Host API major version mismatch -> 409 HOST_API_UNSUPPORTED
        let resp = router
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/api/host/capabilities")
                    .header("Host", "127.0.0.1:8080")
                    .header("X-OpenBox-Token", "test-token-123")
                    .header("X-OpenBox-API-Version", "1.0")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::CONFLICT);
        assert_eq!(body_json(resp).await["code"], json!("HOST_API_UNSUPPORTED"));

        // 8. Native capabilities & version & jobs & health endpoints work
        let resp = router
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/api/v2/version")
                    .header("Host", "127.0.0.1:8080")
                    .header("X-OpenBox-Token", "test-token-123")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let ver = body_json(resp).await;
        assert_eq!(ver["version"], json!("3.0.0"));
        assert_eq!(ver["state_schema"], json!(7));

        // 9. Auth rate-limiting: 9 wrong -> 403, 10th wrong -> 429 RATE_LIMITED
        for i in 1..=10 {
            let resp = router
                .clone()
                .oneshot(
                    Request::builder()
                        .method(Method::GET)
                        .uri("/api/v2/jobs")
                        .header("Host", "127.0.0.1:8080")
                        .header("X-OpenBox-Token", "wrong")
                        .body(Body::empty())
                        .unwrap(),
                )
                .await
                .unwrap();
            if i < 10 {
                assert_eq!(resp.status(), StatusCode::FORBIDDEN);
            } else {
                assert_eq!(resp.status(), StatusCode::TOO_MANY_REQUESTS);
                assert!(resp.headers().contains_key("retry-after"));
                assert_eq!(body_json(resp).await["code"], json!("RATE_LIMITED"));
            }
        }
    }

    #[tokio::test]
    async fn sse_returns_503_when_subscriber_cap_full() {
        let dir = tempfile::tempdir().unwrap();
        let (router, app) = test_router(dir.path());
        let mut subs = Vec::new();
        for _ in 0..SSE_MAX_SUBSCRIBERS {
            subs.push(app.sse.subscribe().unwrap());
        }
        let resp = router
            .oneshot(
                Request::builder()
                    .uri("/api/v2/events")
                    .header("Host", "127.0.0.1:8080")
                    .header("X-OpenBox-Token", "test-token-123")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(resp.headers()["retry-after"], "5");
        assert_eq!(body_json(resp).await["code"], json!("SSE_BUSY"));
    }
}
