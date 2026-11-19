//! Main HTTP request dispatcher applying OpenBox's exact security middleware chain,
//! native Rust handler modules, and optional Strangler-Fig reverse proxy fallback.

use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::sync::Arc;

use axum::Router;
use axum::body::Body;
use axum::extract::{ConnectInfo, State};
use axum::http::{Method, Request, StatusCode};
use axum::response::Response;
use axum::routing::any;
use openbox_core::ApiError;
use openbox_runtime::AppState;
use serde_json::json;

use crate::contracts::is_registered_api_route;
use crate::handlers::{
    arcade, clips, collections, constellation, data, deals, defs, discovery, emulators, export,
    extensions, faugus, health, household, imports, insights, itinerary, jobs, launch, library,
    library_health, library_storage, media, metadata, moments, native, party, picker, playerlog,
    resume, savevault, screenscraper, sessions, settings, setup, shared, steambridge, steamgrid,
    timemachine, trail, wine,
};
use crate::middleware::{
    AuthRateLimiter, MAX_BODY, api_error_response, check_host_api_version, check_retired_route,
    extract_token, is_loopback_host, is_public_get_path, is_static_asset_path, json_response,
    new_request_id,
};
use crate::sse::handle_sse;
use crate::static_files::serve_public_route;
use crate::strangler::StranglerProxy;

pub struct HttpState {
    pub app: Arc<AppState>,
    pub auth: AuthRateLimiter,
    pub strangler: Option<Arc<StranglerProxy>>,
}

impl HttpState {
    pub fn new(app: Arc<AppState>, strangler: Option<Arc<StranglerProxy>>) -> Arc<Self> {
        Arc::new(Self {
            app,
            auth: AuthRateLimiter::new(),
            strangler,
        })
    }
}

pub fn build_router(state: Arc<HttpState>) -> Router {
    Router::new()
        .route("/", any(dispatch_request))
        .route("/{*path}", any(dispatch_request))
        .with_state(state)
}

async fn dispatch_request(State(state): State<Arc<HttpState>>, req: Request<Body>) -> Response {
    let request_id = new_request_id();
    let method = req.method().clone();
    let uri = req.uri().clone();
    let path = uri.path().to_owned();
    let query = uri.query().unwrap_or("").to_owned();
    let instance = uri
        .path_and_query()
        .map(|pq| pq.as_str().to_owned())
        .unwrap_or_else(|| path.clone());
    let headers = req.headers().clone();
    let client_ip = req
        .extensions()
        .get::<ConnectInfo<SocketAddr>>()
        .map(|ci| ci.0.ip())
        .unwrap_or(IpAddr::V4(Ipv4Addr::LOCALHOST));

    tracing::debug!("HTTP {} {} started [{}]", method, path, request_id);

    // 1. Loopback Host header guard (closes DNS rebinding)
    if !is_loopback_host(&headers) {
        return json_response(
            StatusCode::FORBIDDEN,
            &json!({
                "error": "Request host is not allowed.",
                "code": "BAD_HOST",
                "request_id": request_id,
            }),
            &instance,
        );
    }

    // Only GET and POST are served by OpenBox
    if method != Method::GET && method != Method::POST {
        return api_error_response(
            &ApiError::route_not_found("Method not allowed."),
            &request_id,
            &instance,
        );
    }

    // 2. Non-asset /static/* fails 404 before auth (matches routes.py:646)
    if method == Method::GET && path.starts_with("/static/") && !is_static_asset_path(&path) {
        return api_error_response(
            &ApiError::route_not_found("Not found"),
            &request_id,
            &instance,
        );
    }

    // 3. Token authentication + per-IP 10/60s sliding-window rate limiter
    let is_public = method == Method::GET && is_public_get_path(&path);
    if !is_public {
        let provided = extract_token(&headers, if query.is_empty() { None } else { Some(&query) });
        if let Some(resp) =
            state
                .auth
                .verify_token(&state.app.token, &provided, client_ip, &instance)
        {
            return resp;
        }
    }

    // 4. Native host API major-version guard (/api/host/*)
    if let Err(err) = check_host_api_version(&headers, &path) {
        return api_error_response(&err, &request_id, &instance);
    }

    // 5. Retired route guards (/api/v1/*, legacy host paths, unversioned /api/*)
    if let Err(err) = check_retired_route(&method, &path) {
        return api_error_response(&err, &request_id, &instance);
    }

    let ctx = shared::RequestCtx {
        app: Arc::clone(&state.app),
        path: &path,
        query: &query,
        instance: &instance,
        request_id: &request_id,
        headers: &headers,
    };

    if method == Method::GET {
        // Public shell, static assets, locales, favicon
        if let Some(resp) =
            serve_public_route(&state.app.root_dir, &path, &headers, &request_id, &instance)
        {
            return resp;
        }
        // SSE event stream
        if path == "/api/v2/events" {
            return handle_sse(Arc::clone(&state.app), &instance);
        }
        // Native Rust GET handlers
        if let Some(resp) = native::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = jobs::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = health::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = launch::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = sessions::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = emulators::handle_get(&ctx).await {
            return resp;
        }
        if let Some(resp) = defs::handle_get(&ctx).await {
            return resp;
        }
        if let Some(resp) = wine::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = faugus::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = arcade::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = library::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = library_storage::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = settings::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = imports::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = setup::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = metadata::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = media::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = screenscraper::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = steamgrid::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = trail::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = deals::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = data::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = savevault::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = timemachine::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = library_health::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = household::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = export::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = discovery::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = constellation::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = insights::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = party::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = collections::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = playerlog::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = itinerary::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = extensions::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = steambridge::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = clips::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = resume::handle_get(&ctx) {
            return resp;
        }
        if let Some(resp) = moments::handle_get(&ctx) {
            return resp;
        }
        // Optional Strangler Proxy for unmigrated registered routes during Phases 3–9
        if is_registered_api_route("GET", &path)
            && let Some(proxy) = &state.strangler
        {
            return proxy
                .forward(&method, &instance, &headers, Vec::new())
                .await;
        }
        return api_error_response(
            &ApiError::route_not_found("Not found"),
            &request_id,
            &instance,
        );
    }

    // POST: enforce MAX_BODY and JSON object contract
    let body_bytes = match read_bounded_body(req.into_body(), MAX_BODY).await {
        Ok(bytes) => bytes,
        Err(msg) => {
            return api_error_response(&ApiError::bad_request(msg), &request_id, &instance);
        }
    };
    let payload = match shared::parse_json_object(&body_bytes) {
        Ok(obj) => obj,
        Err(err) => return api_error_response(&err, &request_id, &instance),
    };

    // Native Rust POST handlers
    if let Some(resp) = native::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = jobs::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = health::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = launch::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = sessions::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = emulators::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = defs::handle_post(&ctx, &payload).await {
        return resp;
    }
    if let Some(resp) = faugus::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = arcade::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = library::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = library_storage::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = settings::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = imports::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = setup::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = metadata::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = media::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = screenscraper::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = steamgrid::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = trail::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = deals::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = data::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = savevault::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = timemachine::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = library_health::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = household::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = export::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = discovery::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = picker::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = insights::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = party::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = collections::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = playerlog::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = itinerary::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = extensions::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = steambridge::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = clips::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = resume::handle_post(&ctx, &payload) {
        return resp;
    }
    if let Some(resp) = moments::handle_post(&ctx, &payload) {
        return resp;
    }

    // Optional Strangler Proxy for unmigrated registered POST routes during Phases 3–9
    if is_registered_api_route("POST", &path)
        && let Some(proxy) = &state.strangler
    {
        return proxy
            .forward(&method, &instance, &headers, body_bytes)
            .await;
    }

    api_error_response(
        &ApiError::route_not_found("Not found"),
        &request_id,
        &instance,
    )
}

async fn read_bounded_body(body: Body, limit: usize) -> Result<Vec<u8>, &'static str> {
    let mut out = Vec::new();
    let mut stream = body.into_data_stream();
    use futures_util::StreamExt;
    while let Some(chunk) = stream.next().await {
        let bytes = chunk.map_err(|_| "Request body was truncated.")?;
        if out.len().saturating_add(bytes.len()) > limit {
            return Err("Request is too large.");
        }
        out.extend_from_slice(&bytes);
    }
    Ok(out)
}
