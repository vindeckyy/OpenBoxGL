//! Session lifecycle, launch, running/history, recap, Big Box mode, and state recovery routes.
//! Mirrors `handlers/sessions.py`.

use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};

use axum::http::StatusCode;
use axum::response::Response;
use openbox_core::ApiError;
use openbox_emulators::{
    control_game_session, resolve_game_from_payload, start_game, tokens::build_launch_args,
};
use openbox_platform::{expand_user, open_path, split_command};
use serde_json::{Map, Value, json};

use crate::handlers::shared::{RequestCtx, bool_field, str_field};
use crate::middleware::{api_error_response, json_response, state_error_response};

static BIGBOX_ENTERED: AtomicBool = AtomicBool::new(false);

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    match ctx.path {
        "/api/v2/running" => {
            let after: u64 = ctx
                .query_first("after")
                .and_then(|s| s.parse().ok())
                .unwrap_or(0);
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let abandoned: Vec<Value> = state
                .get("active_sessions")
                .and_then(Value::as_array)
                .map(|arr| {
                    arr.iter()
                        .filter(|s| s.get("status").and_then(Value::as_str) == Some("abandoned"))
                        .cloned()
                        .collect()
                })
                .unwrap_or_default();
            let reg = ctx.app.registry.lock();
            let running: Vec<Value> = reg.running.values().cloned().collect();
            let events: Vec<Value> = reg
                .session_events
                .iter()
                .filter(|ev| ev.get("id").and_then(Value::as_u64).unwrap_or(0) > after)
                .cloned()
                .collect();
            let last_event = reg.event_sequence;
            drop(reg);
            Some(json_response(
                StatusCode::OK,
                &json!({
                    "running": running,
                    "abandoned": abandoned,
                    "events": events,
                    "last_event": last_event,
                }),
                ctx.instance,
            ))
        }
        "/api/v2/history" => {
            let limit = ctx
                .query_first("limit")
                .and_then(|s| s.parse::<usize>().ok())
                .unwrap_or(100)
                .clamp(1, 500);
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let history: Vec<Value> = state
                .get("history")
                .and_then(Value::as_array)
                .map(|arr| arr.iter().rev().take(limit).cloned().collect())
                .unwrap_or_default();
            let enabled = state
                .get("settings")
                .and_then(|s| s.get("track_session_history"))
                .and_then(Value::as_bool)
                .unwrap_or(true);
            Some(json_response(
                StatusCode::OK,
                &json!({"history": history, "enabled": enabled}),
                ctx.instance,
            ))
        }
        "/api/v2/history/timeline" => {
            let days: usize = match ctx.query_first("days").filter(|s| !s.trim().is_empty()) {
                None => 90,
                Some(raw) => match raw.trim().parse::<i64>() {
                    Ok(v) if (1..=366).contains(&v) => v as usize,
                    Ok(_) => {
                        return Some(api_error_response(
                            &ApiError::bad_request("days must be between 1 and 366"),
                            ctx.request_id,
                            ctx.instance,
                        ));
                    }
                    Err(_) => {
                        return Some(api_error_response(
                            &ApiError::bad_request("days must be an integer"),
                            ctx.request_id,
                            ctx.instance,
                        ));
                    }
                },
            };
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let groups = openbox_discovery::insights::timeline_groups(&state, days);
            Some(json_response(StatusCode::OK, &groups, ctx.instance))
        }
        "/api/v2/sessions/recap" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let settings = state.get("settings").and_then(Value::as_object);
            let track = settings
                .and_then(|s| s.get("track_session_history"))
                .and_then(Value::as_bool)
                .unwrap_or(true);
            let recap_on = settings
                .and_then(|s| s.get("session_recap_enabled"))
                .and_then(Value::as_bool)
                .unwrap_or(true);
            if !track || !recap_on {
                return Some(api_error_response(
                    &ApiError::not_found("Session recap is not available."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if let Some(recap) = ctx.app.sse.last_session_recap() {
                return Some(json_response(StatusCode::OK, &recap, ctx.instance));
            }
            // Fallback: derive partial recap from newest history entry
            let Some(last) = state
                .get("history")
                .and_then(Value::as_array)
                .and_then(|a| a.last())
            else {
                return Some(api_error_response(
                    &ApiError::not_found("No session recap is available."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let recap = json!({
                "launch_id": last.get("launch_id").and_then(Value::as_str).unwrap_or(""),
                "game_id": last.get("game_id").and_then(Value::as_str).unwrap_or(""),
                "name": last.get("game").and_then(Value::as_str).unwrap_or("Untitled"),
                "platform": last.get("platform").and_then(Value::as_str).unwrap_or(""),
                "started_at": last.get("started").and_then(Value::as_str).unwrap_or(""),
                "stopped_at": last.get("stopped").and_then(Value::as_str).unwrap_or(""),
                "seconds": last.get("seconds").and_then(Value::as_i64).unwrap_or(0),
                "exit_code": last.get("exit_code").and_then(Value::as_i64).unwrap_or(0),
                "partial": true,
                "game_missing": false,
                "progress": "",
                "screenshots_taken": 0,
                "ra": {"tracked": false, "available": false, "baseline": false, "earned": 0, "earned_hardcore": 0, "total": 0, "progress_pct": 0.0, "mastered": false, "earned_delta": 0, "earned_hardcore_delta": 0},
                "resume_state_available": false,
                "capture": {"moment": true, "clip": true},
            });
            Some(json_response(StatusCode::OK, &recap, ctx.instance))
        }
        _ => None,
    }
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/launch" | "/api/host/launch" => {
            if payload.get("id").is_none() && str_field(payload, "game_id").is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("Game id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let (idx, game) = match resolve_game_from_payload(&state, payload) {
                Ok(pair) => pair,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let gid = game
                .get("game_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            match start_game(&ctx.app, idx, &gid) {
                Ok(mut res) => {
                    if let Some(obj) = res.as_object_mut() {
                        obj.insert("ok".into(), Value::Bool(true));
                    }
                    Some(json_response(StatusCode::OK, &res, ctx.instance))
                }
                Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/session/control" => {
            let launch_id = str_field(payload, "launch_id");
            let action = str_field(payload, "action");
            match control_game_session(&ctx.app, launch_id, action) {
                Ok(res) => Some(json_response(StatusCode::OK, &res, ctx.instance)),
                Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/session/cleanup" => {
            let lid = str_field(payload, "launch_id").to_owned();
            let _ = ctx.app.transact_state(move |st| {
                if let Some(arr) = st.get_mut("active_sessions").and_then(Value::as_array_mut) {
                    arr.retain(|s| {
                        s.get("launch_id").and_then(Value::as_str) != Some(lid.as_str())
                    });
                }
                Ok(())
            });
            Some(json_response(
                StatusCode::OK,
                &json!({"ok": true}),
                ctx.instance,
            ))
        }
        "/api/v2/bigbox/mode" => {
            if bool_field(payload, "entering") {
                BIGBOX_ENTERED.store(true, Ordering::Release);
                return Some(json_response(
                    StatusCode::OK,
                    &json!({"ok": true, "entering": true}),
                    ctx.instance,
                ));
            }
            let mut commands = 0usize;
            if BIGBOX_ENTERED.swap(false, Ordering::AcqRel)
                && let Ok(state) = ctx.app.load_state_view()
                && let Some(arr) = state
                    .get("settings")
                    .and_then(|s| s.get("bigbox_shutdown_commands"))
                    .and_then(Value::as_array)
            {
                for raw in arr {
                    if let Some(cmd) = raw.as_str()
                        && let Ok(mut parts) = split_command(cmd)
                        && !parts.is_empty()
                    {
                        let exe = expand_user(Path::new(&parts[0]));
                        parts[0] = exe.to_string_lossy().into_owned();
                        if Command::new(&parts[0])
                            .args(&parts[1..])
                            .stdin(Stdio::null())
                            .stdout(Stdio::null())
                            .stderr(Stdio::null())
                            .spawn()
                            .is_ok()
                        {
                            commands += 1;
                        }
                    }
                }
            }
            Some(json_response(
                StatusCode::OK,
                &json!({"ok": true, "entering": false, "commands": commands}),
                ctx.instance,
            ))
        }
        "/api/v2/state/recover" => {
            if bool_field(payload, "dry_run") {
                let games = ctx
                    .app
                    .load_state_view()
                    .ok()
                    .and_then(|s| s.get("games").and_then(Value::as_array).map(|a| a.len()))
                    .unwrap_or(0);
                let snaps = ctx.app.store.snapshots();
                return Some(json_response(
                    StatusCode::OK,
                    &json!({
                        "dry_run": true,
                        "backup_available": ctx.app.store.backup_path.is_file(),
                        "snapshots": snaps,
                        "games": games,
                    }),
                    ctx.instance,
                ));
            }
            let snap_name = str_field(payload, "snapshot");
            if !snap_name.is_empty() {
                match ctx.app.store.restore_snapshot(snap_name) {
                    Ok(st) => {
                        ctx.app.bump_media_epoch();
                        let games = st
                            .get("games")
                            .and_then(Value::as_array)
                            .map(|a| a.len())
                            .unwrap_or(0);
                        return Some(json_response(
                            StatusCode::OK,
                            &json!({"ok": true, "games": games, "snapshot": snap_name}),
                            ctx.instance,
                        ));
                    }
                    Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
                }
            }
            match ctx.app.store.recover() {
                Ok(st) => {
                    ctx.app.invalidate_caches();
                    let games = st
                        .get("games")
                        .and_then(Value::as_array)
                        .map(|a| a.len())
                        .unwrap_or(0);
                    Some(json_response(
                        StatusCode::OK,
                        &json!({"ok": true, "games": games}),
                        ctx.instance,
                    ))
                }
                Err(e) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/extra/launch" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let (_, game) = match resolve_game_from_payload(&state, payload) {
                Ok(pair) => pair,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let kind = str_field(payload, "kind");
            if !matches!(kind, "applications" | "versions" | "documents") {
                return Some(api_error_response(
                    &ApiError::bad_request("Unknown extra type."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let idx = payload
                .get("index")
                .and_then(Value::as_u64)
                .unwrap_or(usize::MAX as u64) as usize;
            let Some(extra) = game
                .get(kind)
                .and_then(Value::as_array)
                .and_then(|a| a.get(idx))
            else {
                return Some(api_error_response(
                    &ApiError::bad_request("Extra entry not found."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let path_str = extra.get("path").and_then(Value::as_str).unwrap_or("");
            let path = Path::new(path_str);
            if !path.exists() {
                return Some(api_error_response(
                    &ApiError::bad_request("File does not exist."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if kind == "documents" {
                if let Err(err) = open_path(path_str) {
                    return Some(api_error_response(
                        &ApiError::bad_request(err.to_string()),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
                return Some(json_response(
                    StatusCode::OK,
                    &json!({"ok": true}),
                    ctx.instance,
                ));
            }
            let args = if let Some(cmd) = extra
                .get("command")
                .and_then(Value::as_str)
                .filter(|s| !s.trim().is_empty())
            {
                build_launch_args(cmd, &json!({}), Some(path_str), "", "")
            } else {
                vec![path_str.to_owned()]
            };
            if args.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("Empty command."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let mut proc = Command::new(&args[0]);
            proc.args(&args[1..])
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null());
            if let Some(parent) = path.parent() {
                proc.current_dir(parent);
            }
            if let Err(err) = proc.spawn() {
                return Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            Some(json_response(
                StatusCode::OK,
                &json!({"ok": true}),
                ctx.instance,
            ))
        }
        _ => None,
    }
}
