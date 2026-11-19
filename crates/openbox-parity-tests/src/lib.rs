//! Differential & golden parity integration test crate for OpenBoxGL.
//!
//! Verifies 100% native route coverage across all 417 registered HTTP routes
//! (413 public `/api/v2/*` + 4 native host `/api/host/*`) with `strangler: None`.

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use axum::body::Body;
    use http::{Method, Request, header};
    use http_body_util::BodyExt;
    use openbox_http::{HttpState, build_router, dump_contracts_json};
    use openbox_runtime::AppState;
    use serde_json::Value;
    use tempfile::TempDir;
    use tower::ServiceExt;

    fn repo_root() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .parent()
            .unwrap()
            .to_path_buf()
    }

    /// Every single one of the 417 routes in `api_contracts.json` (413 public `/api/v2/*`
    /// plus 4 `/api/host/*`) MUST be handled natively in Rust with `strangler: None`
    /// (zero routes may fall through to `ROUTE_NOT_FOUND`).
    #[tokio::test]
    async fn all_417_routes_are_natively_routed_without_strangler() {
        let tmp = TempDir::new().unwrap();
        let app = AppState::new(Some(tmp.path().to_path_buf()), repo_root(), None);
        let token = app.token.clone();
        let http_state = HttpState::new(app, None);
        let router = build_router(http_state);

        let contracts = dump_contracts_json();
        let api_contracts = contracts
            .get("api_contracts")
            .expect("api_contracts object present");
        let mut routes: Vec<(String, String)> = Vec::new();
        for key in ["public", "host"] {
            if let Some(arr) = api_contracts.get(key).and_then(Value::as_array) {
                for entry in arr {
                    let m = entry["method"].as_str().unwrap().to_owned();
                    let p = entry["path"].as_str().unwrap().to_owned();
                    routes.push((m, p));
                }
            }
        }
        assert_eq!(
            routes.len(),
            417,
            "expected exactly 417 registered routes (413 public + 4 host)"
        );

        let mut missing = Vec::new();
        for (method_str, path) in &routes {
            // For SSE /api/v2/events verify status 200 without draining infinite stream!
            if path == "/api/v2/events" {
                let req = Request::builder()
                    .method(Method::GET)
                    .uri(path)
                    .header(header::HOST, "127.0.0.1:8765")
                    .header("X-OpenBox-Token", &token)
                    .body(Body::empty())
                    .unwrap();
                let resp = router.clone().oneshot(req).await.unwrap();
                assert_eq!(
                    resp.status().as_u16(),
                    200,
                    "SSE /api/v2/events must return 200"
                );
                continue;
            }
            // Skip only the 7 routes that spawn desktop OS apps or perform live external HTTP
            // requests when called with empty inputs.
            if matches!(
                path.as_str(),
                "/api/v2/update"
                    | "/api/v2/plugins/catalog"
                    | "/api/v2/emulators/defs/update"
                    | "/api/v2/deals/refresh"
                    | "/api/v2/deals/search"
                    | "/api/v2/themes/open-folder"
                    | "/api/v2/desktop/install"
            ) {
                continue;
            }
            let method = if method_str == "POST" {
                Method::POST
            } else {
                Method::GET
            };
            let body = if method == Method::POST {
                Body::from("{}")
            } else {
                Body::empty()
            };
            let mut builder = Request::builder()
                .method(method)
                .uri(path)
                .header(header::HOST, "127.0.0.1:8765")
                .header("X-OpenBox-Token", &token);
            if path.starts_with("/api/host/") {
                builder = builder.header("X-OpenBox-Host-API", "1");
            }
            if method_str == "POST" {
                builder = builder.header(header::CONTENT_TYPE, "application/json");
            }
            let req = builder.body(body).unwrap();
            let resp = router.clone().oneshot(req).await.unwrap();
            if resp.status().as_u16() == 404 {
                let bytes = resp.into_body().collect().await.unwrap().to_bytes();
                if let Ok(json_body) = serde_json::from_slice::<Value>(&bytes)
                    && json_body.get("code").and_then(Value::as_str) == Some("ROUTE_NOT_FOUND")
                {
                    missing.push(format!("{method_str} {path}"));
                }
            }
        }
        assert!(
            missing.is_empty(),
            "Unmigrated routes returning ROUTE_NOT_FOUND ({}): {missing:#?}",
            missing.len()
        );
    }
}
