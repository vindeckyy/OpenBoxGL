//! Achievement Trail v2 handlers (`handlers/trail.py`, ADR 0110).

use std::sync::Arc;

use axum::http::StatusCode;
use axum::response::Response;
use openbox_core::ApiError;
use openbox_metadata::retroachievements::{RaCredentials, load_credentials};
use openbox_metadata::trail::{
    get_trail, get_trail_game, rebuild_trail_cache, refresh_trail_progress,
};
use serde_json::{Map, Value, json};

use crate::handlers::shared::RequestCtx;
use crate::middleware::{api_error_response, json_response, state_error_response};

fn ra_ready(ctx: &RequestCtx<'_>, state: &Value) -> Option<RaCredentials> {
    let enabled = state
        .get("settings")
        .and_then(|s| s.get("retroachievements_enabled"))
        .and_then(Value::as_bool)
        .unwrap_or(true);
    if !enabled {
        return None;
    }
    load_credentials(&ctx.app.data_dir)
}

fn store_sig_str(ctx: &RequestCtx<'_>) -> String {
    ctx.app
        .store
        .signature()
        .map(|s| format!("{}:{}:{}", s.mtime_ns, s.size, s.ino))
        .unwrap_or_default()
}

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    match ctx.path {
        "/api/v2/ra/trail" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            if ra_ready(ctx, &state).is_none() {
                return Some(json_response(
                    StatusCode::OK,
                    &json!({
                        "available": false,
                        "reason": "ra_disabled",
                        "items": [],
                        "summary": {},
                        "total": 0,
                        "offset": 0,
                        "limit": 50,
                        "stale": false,
                    }),
                    ctx.instance,
                ));
            }
            let sort = ctx
                .query_first("sort")
                .map(|s| s.trim().to_owned())
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| "closest".to_owned());
            let offset: isize = match ctx.query_first("offset").as_deref() {
                None | Some("") => 0,
                Some(s) => match s.parse() {
                    Ok(n) => n,
                    Err(_) => {
                        return Some(api_error_response(
                            &ApiError::bad_request("offset and limit must be integers."),
                            ctx.request_id,
                            ctx.instance,
                        ));
                    }
                },
            };
            let limit: isize = match ctx.query_first("limit").as_deref() {
                None | Some("") => 50,
                Some(s) => match s.parse() {
                    Ok(n) => n,
                    Err(_) => {
                        return Some(api_error_response(
                            &ApiError::bad_request("offset and limit must be integers."),
                            ctx.request_id,
                            ctx.instance,
                        ));
                    }
                },
            };
            let sig = store_sig_str(ctx);
            let app_bg = Arc::clone(&ctx.app);
            let app_bg_jobs = Arc::clone(&ctx.app);
            let st_bg = Arc::clone(&state);
            let sig_bg = sig.clone();
            let result = get_trail(
                &state,
                &ctx.app.data_dir,
                &sig,
                &sort,
                offset,
                limit,
                Some(move || {
                    let _ =
                        app_bg_jobs
                            .jobs
                            .submit("trail-rebuild", true, None, None, move |_ctx| {
                                Ok(rebuild_trail_cache(&st_bg, &app_bg.data_dir, &sig_bg))
                            });
                }),
            );
            Some(json_response(StatusCode::OK, &result, ctx.instance))
        }
        "/api/v2/ra/trail/game" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            if ra_ready(ctx, &state).is_none() {
                return Some(json_response(
                    StatusCode::OK,
                    &json!({"available": false, "reason": "ra_disabled", "game": Value::Null}),
                    ctx.instance,
                ));
            }
            let gid = ctx.query_first("game_id").unwrap_or_default();
            if gid.trim().is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("game_id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            match get_trail_game(&state, &ctx.app.data_dir, gid.trim()) {
                Ok(res) => Some(json_response(StatusCode::OK, &res, ctx.instance)),
                Err(e) => Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            }
        }
        _ => None,
    }
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/ra/trail/refresh" => {
            let game_ids: Option<Vec<String>> = match payload.get("game_ids") {
                None | Some(Value::Null) => None,
                Some(Value::Array(a)) => Some(
                    a.iter()
                        .filter_map(|v| v.as_str().map(str::trim))
                        .filter(|s| !s.is_empty())
                        .map(ToOwned::to_owned)
                        .collect(),
                ),
                _ => {
                    return Some(api_error_response(
                        &ApiError::bad_request("game_ids must be a list."),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let Some(creds) = ra_ready(ctx, &state) else {
                return Some(api_error_response(
                    &ApiError::ra_disabled(
                        "RetroAchievements must be configured and enabled to refresh trail caches.",
                    ),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let app = Arc::clone(&ctx.app);
            let st_c = Arc::clone(&state);
            let sig = store_sig_str(ctx);
            let job = match ctx
                .app
                .jobs
                .submit("trail-refresh", true, None, None, move |jctx| {
                    let res = refresh_trail_progress(
                        &app.data_dir,
                        &st_c,
                        &creds,
                        game_ids.as_deref(),
                        Some(jctx.cancel_flag()),
                    );
                    rebuild_trail_cache(&st_c, &app.data_dir, &sig);
                    Ok(res)
                }) {
                Ok(j) => j,
                Err(e) => {
                    return Some(api_error_response(
                        &ApiError::job_state_conflict(e),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            Some(json_response(
                StatusCode::ACCEPTED,
                &json!({"state": "queued", "job_id": job.get("job_id").cloned().unwrap_or(Value::Null)}),
                ctx.instance,
            ))
        }
        _ => None,
    }
}
