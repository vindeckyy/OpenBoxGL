//! Launch Doctor preflight, Launch Audit, Flatpak folder grants, and RetroArch core routes.
//! Mirrors `handlers/launch.py`.

use std::sync::Arc;

use axum::http::StatusCode;
use axum::response::Response;
use openbox_core::ApiError;
use openbox_emulators::{
    RETROARCH_CORE_CHOICE_KEY, audit, doctor, flatpak_grant, installed_retroarch_cores,
    load_adapters, valid_core_name,
};
use serde_json::{Map, Value, json};

use crate::handlers::shared::{RequestCtx, bool_field, str_field};
use crate::middleware::{api_error_response, json_response, state_error_response};

pub const AUDIT_JOB_NAME: &str = "launch-audit";

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    match ctx.path {
        "/api/v2/launch/audit" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let game_count = state
                .get("games")
                .and_then(Value::as_array)
                .map(|a| a.len())
                .unwrap_or(0);
            let report = audit::load_audit(&ctx.app.data_dir);
            let mut payload = audit::public_summary(report.as_ref(), game_count);
            let job = ctx.app.jobs.snapshot(AUDIT_JOB_NAME);
            payload["job"] = if job.as_object().is_some_and(|o| !o.is_empty()) {
                job
            } else {
                Value::Null
            };
            if let Some(group) = ctx.query_first("group").filter(|s| !s.trim().is_empty()) {
                let offset = ctx
                    .query_first("offset")
                    .and_then(|s| s.parse::<usize>().ok())
                    .unwrap_or(0);
                let limit = ctx
                    .query_first("limit")
                    .and_then(|s| s.parse::<usize>().ok())
                    .unwrap_or(50)
                    .clamp(1, 500);
                payload["members"] =
                    audit::group_members(report.as_ref(), group.trim(), offset, limit);
            }
            Some(json_response(StatusCode::OK, &payload, ctx.instance))
        }
        "/api/v2/launch/audit/status" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let game_count = state
                .get("games")
                .and_then(Value::as_array)
                .map(|a| a.len())
                .unwrap_or(0);
            let report = audit::load_audit(&ctx.app.data_dir);
            let statuses = audit::readiness_statuses(report.as_ref(), game_count);
            Some(json_response(
                StatusCode::OK,
                &json!({"statuses": statuses}),
                ctx.instance,
            ))
        }
        "/api/v2/launch/cores" => Some(json_response(
            StatusCode::OK,
            &json!({"cores": installed_retroarch_cores(None)}),
            ctx.instance,
        )),
        _ => None,
    }
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/launch/preflight" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let adapters = load_adapters(&ctx.app.root_dir, Some(&ctx.app.data_dir));
            let fail_on_blocked = bool_field(payload, "fail_on_blocked");
            match doctor::preflight_single(payload, &state, &adapters, &ctx.app.data_dir) {
                Ok(res) => {
                    if fail_on_blocked && res["status"].as_str() == Some("blocked") {
                        return Some(json_response(
                            StatusCode::CONFLICT,
                            &json!({
                                "code": "LAUNCH_PREFLIGHT_BLOCKED",
                                "request_id": ctx.request_id,
                                "status": "blocked",
                                "game_id": res["game_id"],
                                "candidate_id": res["candidate_id"],
                                "resolved": res["resolved"],
                                "checks": res["checks"],
                            }),
                            ctx.instance,
                        ));
                    }
                    Some(json_response(StatusCode::OK, &res, ctx.instance))
                }
                Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/launch/preflight/batch" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let adapters = load_adapters(&ctx.app.root_dir, Some(&ctx.app.data_dir));
            let fail_on_blocked = bool_field(payload, "fail_on_blocked");
            match doctor::preflight_batch(payload, &state, &adapters, &ctx.app.data_dir) {
                Ok(res) => {
                    if fail_on_blocked && res["totals"]["blocked"].as_u64().unwrap_or(0) > 0 {
                        return Some(json_response(
                            StatusCode::CONFLICT,
                            &json!({
                                "code": "LAUNCH_PREFLIGHT_BLOCKED",
                                "request_id": ctx.request_id,
                                "status": "blocked",
                                "totals": res["totals"],
                                "by_platform": res["by_platform"],
                                "results": res["results"],
                            }),
                            ctx.instance,
                        ));
                    }
                    Some(json_response(StatusCode::OK, &res, ctx.instance))
                }
                Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/launch/audit/scan" => {
            let deep = bool_field(payload, "deep");
            let app = Arc::clone(&ctx.app);
            match ctx.app.jobs.submit(
                AUDIT_JOB_NAME,
                true,
                Some("setup.scan"),
                Some(&json!({"deep": deep})),
                move |_jctx| {
                    let st = app.load_state().map_err(|e| e.to_string())?;
                    let games = st
                        .get("games")
                        .and_then(Value::as_array)
                        .cloned()
                        .unwrap_or_default();
                    let profiles = st
                        .get("profiles")
                        .and_then(Value::as_object)
                        .cloned()
                        .unwrap_or_default();
                    let adapters = load_adapters(&app.root_dir, Some(&app.data_dir));
                    let rep =
                        audit::audit_library(&games, &profiles, &adapters, &app.data_dir, deep);
                    audit::store_audit(&app.data_dir, &rep)?;
                    Ok(json!({
                        "game_count": rep["game_count"],
                        "totals": rep["totals"],
                    }))
                },
            ) {
                Ok(job) => Some(json_response(
                    StatusCode::ACCEPTED,
                    &json!({"state": "queued", "job_id": job["job_id"], "deep": deep}),
                    ctx.instance,
                )),
                Err(err) => Some(api_error_response(
                    &ApiError::bad_request(err),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/launch/audit/refresh" => {
            let group_key = str_field(payload, "group");
            if group_key.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("group is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let games = state
                .get("games")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            let profiles = state
                .get("profiles")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            let Some(prev) = audit::load_audit(&ctx.app.data_dir) else {
                return Some(api_error_response(
                    &ApiError::bad_request(
                        "The library changed since the last check. Check every game first.",
                    ),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            if prev
                .get("game_count")
                .and_then(Value::as_u64)
                .unwrap_or(u64::MAX) as usize
                != games.len()
            {
                return Some(api_error_response(
                    &ApiError::bad_request(
                        "The library changed since the last check. Check every game first.",
                    ),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let deep = prev.get("deep").and_then(Value::as_bool).unwrap_or(false);
            let adapters = load_adapters(&ctx.app.root_dir, Some(&ctx.app.data_dir));
            let refreshed =
                audit::audit_library(&games, &profiles, &adapters, &ctx.app.data_dir, deep);
            let _ = audit::store_audit(&ctx.app.data_dir, &refreshed);
            Some(json_response(
                StatusCode::OK,
                &json!({
                    "ok": true,
                    "rechecked": games.len(),
                    "totals": refreshed["totals"],
                    "computed_at": refreshed["computed_at"],
                }),
                ctx.instance,
            ))
        }
        "/api/v2/launch/grant" | "/api/v2/launch/grant/undo" => {
            let undo = ctx.path.ends_with("/undo");
            let app_id = str_field(payload, "app_id");
            let folder = str_field(payload, "folder");
            let adapters = load_adapters(&ctx.app.root_dir, Some(&ctx.app.data_dir));
            match flatpak_grant::apply_grant(&adapters, app_id, folder, undo) {
                Ok(res) => Some(json_response(StatusCode::OK, &res, ctx.instance)),
                Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/launch/core" => {
            let game_id = str_field(payload, "game_id");
            let core = str_field(payload, "core");
            if game_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("game_id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if !core.is_empty() && !valid_core_name(core) {
                return Some(api_error_response(
                    &ApiError::bad_request("That is not a RetroArch core file name."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let gid = game_id.to_owned();
            let core_owned = core.to_owned();
            match ctx.app.transact_state(move |st| {
                let games = st
                    .get_mut("games")
                    .and_then(Value::as_array_mut)
                    .ok_or_else(|| openbox_store::StateError::Corrupt("missing games".into()))?;
                let game = games
                    .iter_mut()
                    .find(|g| g.get("game_id").and_then(Value::as_str) == Some(gid.as_str()))
                    .and_then(Value::as_object_mut)
                    .ok_or_else(|| openbox_store::StateError::Corrupt("Game not found".into()))?;
                if core_owned.is_empty() {
                    game.remove(RETROARCH_CORE_CHOICE_KEY);
                } else {
                    game.insert(
                        RETROARCH_CORE_CHOICE_KEY.into(),
                        Value::String(core_owned.clone()),
                    );
                }
                Ok(game
                    .get("name")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned())
            }) {
                Ok((_, name)) => Some(json_response(
                    StatusCode::OK,
                    &json!({"ok": true, "game_id": game_id, "core": core, "name": name}),
                    ctx.instance,
                )),
                Err(_) => Some(api_error_response(
                    &ApiError::game_not_found("Game not found"),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        _ => None,
    }
}
