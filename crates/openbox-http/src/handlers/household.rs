//! Local-first Household records, challenges, leaderboard, and shared-folder sync handlers (`handlers/household.py`).

use axum::http::StatusCode;
use axum::response::Response;
use openbox_core::ApiError;
use openbox_sync::household::{
    HOUSEHOLD_FORMAT, acknowledge_outbox, challenge_progress, compute_leaderboard,
    household_records, household_state_token, merge_records, publish_household_outbox,
    read_household_records, record_challenge, record_challenge_result, record_member,
    record_stats_share, stats_sharing_enabled,
};
use serde_json::{Map, Value, json};
use uuid::Uuid;

use crate::handlers::shared::{RequestCtx, bool_field};
use crate::middleware::{api_error_response, json_response, state_error_response};

fn required_text(payload: &Map<String, Value>, key: &str, max: usize) -> Result<String, ApiError> {
    let Some(s) = payload.get(key).and_then(Value::as_str) else {
        return Err(
            ApiError::bad_request(format!("{key} must be non-empty text."))
                .with_code("HOUSEHOLD_INVALID_FIELD"),
        );
    };
    let t = s.trim();
    if t.is_empty() || t.chars().count() > max {
        return Err(
            ApiError::bad_request(format!("{key} must be non-empty text."))
                .with_code("HOUSEHOLD_INVALID_FIELD"),
        );
    }
    Ok(t.to_owned())
}

fn optional_text(
    payload: &Map<String, Value>,
    key: &str,
    max: usize,
) -> Result<Option<String>, ApiError> {
    match payload.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) => {
            let t = s.trim();
            if t.is_empty() {
                return Ok(None);
            }
            if t.chars().count() > max {
                return Err(ApiError::bad_request(format!("{key} must be text."))
                    .with_code("HOUSEHOLD_INVALID_FIELD"));
            }
            Ok(Some(t.to_owned()))
        }
        _ => Err(ApiError::bad_request(format!("{key} must be text."))
            .with_code("HOUSEHOLD_INVALID_FIELD")),
    }
}

fn parse_int_field(payload: &Map<String, Value>, key: &str, default: i64) -> Result<i64, ApiError> {
    match payload.get(key) {
        None | Some(Value::Null) => Ok(default),
        Some(Value::Bool(_)) => Err(ApiError::bad_request(format!("{key} must be an integer."))
            .with_code("HOUSEHOLD_INVALID_FIELD")),
        Some(Value::Number(n)) => n.as_i64().ok_or_else(|| {
            ApiError::bad_request(format!("{key} must be an integer."))
                .with_code("HOUSEHOLD_INVALID_FIELD")
        }),
        Some(Value::String(s)) => s.trim().parse::<i64>().map_err(|_| {
            ApiError::bad_request(format!("{key} must be an integer."))
                .with_code("HOUSEHOLD_INVALID_FIELD")
        }),
        _ => Err(ApiError::bad_request(format!("{key} must be an integer."))
            .with_code("HOUSEHOLD_INVALID_FIELD")),
    }
}

fn parse_period(raw: Option<&str>) -> Result<String, ApiError> {
    let p = raw
        .unwrap_or("all_time")
        .trim()
        .to_lowercase()
        .replace('-', "_");
    let p = if p.is_empty() {
        "all_time".to_owned()
    } else {
        p
    };
    if !matches!(p.as_str(), "daily" | "weekly" | "monthly" | "all_time") {
        return Err(
            ApiError::bad_request("period is unsupported.").with_code("HOUSEHOLD_INVALID_FIELD")
        );
    }
    Ok(p)
}

fn check_sync_protocol(payload: &Map<String, Value>) -> Result<(), ApiError> {
    match payload.get("protocol") {
        None | Some(Value::Null) => Ok(()),
        Some(Value::String(s)) if s == "v1" || s == &format!("household-v{HOUSEHOLD_FORMAT}") => {
            Ok(())
        }
        Some(Value::Number(n)) if n.as_i64() == Some(HOUSEHOLD_FORMAT) => Ok(()),
        _ => Err(
            ApiError::bad_request("Unsupported household sync protocol.")
                .with_code("HOUSEHOLD_SYNC_PROTOCOL"),
        ),
    }
}

fn sync_folder(state: &Value) -> Result<String, ApiError> {
    let folder = state
        .get("settings")
        .and_then(|s| s.get("cloud_folder"))
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("");
    if folder.is_empty() {
        return Err(
            ApiError::bad_request("Configure a mounted cloud sync folder first.")
                .with_code("HOUSEHOLD_SYNC_FOLDER_REQUIRED"),
        );
    }
    Ok(folder.to_owned())
}

fn build_snapshot(state: &Value, period: Option<&str>) -> Result<Map<String, Value>, ApiError> {
    let records = household_records(state, true)
        .map_err(|e| ApiError::bad_request(e.to_string()).with_code("HOUSEHOLD_INVALID_RECORD"))?;
    let visible: Vec<Value> = records
        .iter()
        .filter(|r| r.get("payload").is_some())
        .cloned()
        .collect();
    let results: Vec<Value> = visible
        .iter()
        .filter(|r| r.get("kind").and_then(Value::as_str) == Some("challenge_result"))
        .cloned()
        .collect();
    let mut challenges = Vec::new();
    for item in &visible {
        if item.get("kind").and_then(Value::as_str) != Some("challenge") {
            continue;
        }
        if let Ok(mut prog) = challenge_progress(item, &results, None) {
            if let Some(m) = prog.as_object_mut() {
                m.insert(
                    "record_id".into(),
                    item.get("event_id").cloned().unwrap_or(Value::Null),
                );
            }
            challenges.push(prog);
        }
    }
    let lb = compute_leaderboard(state, period, 50)
        .map_err(|e| ApiError::bad_request(e.to_string()).with_code("HOUSEHOLD_INVALID_RECORD"))?;
    let settings = state.get("settings").and_then(Value::as_object);
    let sync_configured = settings
        .and_then(|s| s.get("cloud_folder"))
        .and_then(Value::as_str)
        .is_some_and(|s| !s.is_empty());
    let hh = state.get("household").and_then(Value::as_object);
    let outbox_count = hh
        .and_then(|h| h.get("outbox"))
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0);
    let device_id = hh
        .and_then(|h| h.get("device_id"))
        .cloned()
        .unwrap_or(Value::Null);
    let sharing_setting = settings
        .and_then(|s| s.get("household_stats_sharing"))
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let token = household_state_token(state).unwrap_or_default();

    let mut out = Map::new();
    out.insert("format".into(), json!(1));
    out.insert("records".into(), Value::Array(records));
    out.insert("challenges".into(), Value::Array(challenges));
    out.insert("leaderboard".into(), lb);
    out.insert(
        "stats_sharing".into(),
        Value::Bool(stats_sharing_enabled(state)),
    );
    out.insert("outbox_count".into(), json!(outbox_count));
    out.insert("device_id".into(), device_id);
    out.insert(
        "settings".into(),
        json!({"household_stats_sharing": sharing_setting}),
    );
    out.insert(
        "household_sync".into(),
        json!({"configured": sync_configured, "format": HOUSEHOLD_FORMAT}),
    );
    out.insert("state_token".into(), Value::String(token));
    Ok(out)
}

fn send_snapshot(
    ctx: &RequestCtx<'_>,
    state: &Value,
    period: Option<&str>,
    extra: Vec<(&str, Value)>,
) -> Response {
    match build_snapshot(state, period) {
        Ok(mut snap) => {
            for (k, v) in extra {
                snap.insert(k.to_owned(), v);
            }
            json_response(StatusCode::OK, &Value::Object(snap), ctx.instance)
        }
        Err(err) => api_error_response(&err, ctx.request_id, ctx.instance),
    }
}

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    match ctx.path {
        "/api/v2/household" => {
            let period = match ctx.query_first("period").filter(|s| !s.is_empty()) {
                Some(p) => match parse_period(Some(&p)) {
                    Ok(v) => Some(v),
                    Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
                },
                None => None,
            };
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            Some(send_snapshot(ctx, &state, period.as_deref(), vec![]))
        }
        "/api/v2/household/leaderboard" => {
            let q_period = ctx.query_first("period");
            let period = match parse_period(q_period.as_deref()) {
                Ok(v) => v,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            Some(send_snapshot(
                ctx,
                &state,
                Some(&period),
                vec![("leaderboard_only", Value::Bool(true))],
            ))
        }
        _ => None,
    }
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/household/member" => {
            let member_id = match required_text(payload, "member_id", 256) {
                Ok(v) => v,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let display_name = match required_text(payload, "display_name", 80) {
                Ok(v) => v,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let avatar_color = payload
                .get("avatar_color")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .unwrap_or("blue")
                .to_owned();
            let stats_shared = bool_field(payload, "stats_shared");
            let res = ctx.app.transact_state(|cur| {
                Ok(record_member(
                    cur,
                    &member_id,
                    &display_name,
                    &avatar_color,
                    stats_shared,
                ))
            });
            match res {
                Ok((st, Ok(rec))) => {
                    ctx.app.sse.broadcast_event(
                        "household.changed",
                        &json!({"event": "member", "record_id": rec.get("event_id")}),
                    );
                    Some(send_snapshot(ctx, &st, None, vec![("record", rec)]))
                }
                Ok((_, Err(err))) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()).with_code("HOUSEHOLD_INVALID_RECORD"),
                    ctx.request_id,
                    ctx.instance,
                )),
                Err(e) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/household/challenge" => {
            let challenge_id = match optional_text(payload, "challenge_id", 256) {
                Ok(Some(v)) => v,
                Ok(None) => Uuid::new_v4().simple().to_string(),
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let title = match required_text(payload, "title", 160) {
                Ok(v) => v,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let metric = match optional_text(payload, "metric", 32) {
                Ok(v) => v.unwrap_or_else(|| "completions".to_owned()),
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let target = match parse_int_field(payload, "target", 1) {
                Ok(n) if n >= 1 => n,
                Ok(_) => {
                    return Some(api_error_response(
                        &ApiError::bad_request("target must be positive.")
                            .with_code("HOUSEHOLD_INVALID_FIELD"),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let created_by = match optional_text(payload, "created_by", 256) {
                Ok(v) => v,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let description = match optional_text(payload, "description", 2000) {
                Ok(v) => v.unwrap_or_default(),
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let game_id = match optional_text(payload, "game_id", 256) {
                Ok(v) => v,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let deadline = match optional_text(payload, "deadline", 80) {
                Ok(v) => v,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let participants: Vec<String> = match payload.get("participant_ids") {
                None | Some(Value::Null) => Vec::new(),
                Some(Value::Array(arr)) => arr
                    .iter()
                    .filter_map(Value::as_str)
                    .map(str::to_owned)
                    .collect(),
                _ => {
                    return Some(api_error_response(
                        &ApiError::bad_request("participant_ids must be a list.")
                            .with_code("HOUSEHOLD_INVALID_FIELD"),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let res = ctx.app.transact_state(|cur| {
                Ok(record_challenge(
                    cur,
                    &challenge_id,
                    &title,
                    created_by.as_deref(),
                    &metric,
                    target,
                    &description,
                    game_id.as_deref(),
                    deadline.as_deref(),
                    &participants,
                    "open",
                ))
            });
            match res {
                Ok((st, Ok(rec))) => {
                    ctx.app.sse.broadcast_event(
                        "household.changed",
                        &json!({"event": "challenge", "record_id": rec.get("event_id")}),
                    );
                    Some(send_snapshot(ctx, &st, None, vec![("record", rec)]))
                }
                Ok((_, Err(err))) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()).with_code("HOUSEHOLD_INVALID_RECORD"),
                    ctx.request_id,
                    ctx.instance,
                )),
                Err(e) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/household/challenge/result" => {
            let challenge_id = match required_text(payload, "challenge_id", 256) {
                Ok(v) => v,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let member_id = match required_text(payload, "member_id", 256) {
                Ok(v) => v,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let value = match parse_int_field(payload, "value", 0) {
                Ok(n) if n >= 0 => n,
                Ok(_) => {
                    return Some(api_error_response(
                        &ApiError::bad_request("value must not be negative.")
                            .with_code("HOUSEHOLD_INVALID_FIELD"),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let completed = match payload.get("completed") {
                None | Some(Value::Null) => false,
                Some(Value::Bool(b)) => *b,
                _ => {
                    return Some(api_error_response(
                        &ApiError::bad_request("completed must be boolean.")
                            .with_code("HOUSEHOLD_INVALID_FIELD"),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let game_id = match optional_text(payload, "game_id", 256) {
                Ok(v) => v,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let note = match optional_text(payload, "note", 500) {
                Ok(v) => v,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let res = ctx.app.transact_state(|cur| {
                Ok(record_challenge_result(
                    cur,
                    &challenge_id,
                    &member_id,
                    value,
                    completed,
                    None,
                    game_id.as_deref(),
                    note.as_deref(),
                ))
            });
            match res {
                Ok((st, Ok(rec))) => {
                    ctx.app.sse.broadcast_event(
                        "household.changed",
                        &json!({"event": "challenge_result", "record_id": rec.get("event_id")}),
                    );
                    Some(send_snapshot(ctx, &st, None, vec![("record", rec)]))
                }
                Ok((_, Err(err))) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()).with_code("HOUSEHOLD_INVALID_RECORD"),
                    ctx.request_id,
                    ctx.instance,
                )),
                Err(e) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/household/share" => {
            let member_id = match required_text(payload, "member_id", 256) {
                Ok(v) => v,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let period = match parse_period(payload.get("period").and_then(Value::as_str)) {
                Ok(v) => v,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let stats_override = payload.get("stats").filter(|v| !v.is_null()).cloned();
            let res = ctx.app.transact_state(|cur| {
                Ok(record_stats_share(
                    cur,
                    &member_id,
                    &period,
                    stats_override.as_ref(),
                    None,
                    None,
                ))
            });
            match res {
                Ok((st, Ok(Some(rec)))) => {
                    ctx.app.sse.broadcast_event(
                        "household.changed",
                        &json!({"event": "share", "record_id": rec.get("event_id")}),
                    );
                    Some(send_snapshot(
                        ctx,
                        &st,
                        None,
                        vec![("record", rec), ("shared", Value::Bool(true))],
                    ))
                }
                Ok((_, Ok(None))) => Some(api_error_response(
                    &ApiError::bad_request(
                        "Enable household stats sharing before publishing stats.",
                    )
                    .with_code("HOUSEHOLD_STATS_DISABLED"),
                    ctx.request_id,
                    ctx.instance,
                )),
                Ok((_, Err(err))) => {
                    let msg = err.to_string();
                    let lower = msg.to_lowercase();
                    if lower.contains("opt") || lower.contains("sharing") {
                        Some(api_error_response(
                            &ApiError::bad_request(
                                "Enable household stats sharing before publishing stats.",
                            )
                            .with_code("HOUSEHOLD_STATS_DISABLED"),
                            ctx.request_id,
                            ctx.instance,
                        ))
                    } else {
                        Some(api_error_response(
                            &ApiError::bad_request(msg).with_code("HOUSEHOLD_INVALID_RECORD"),
                            ctx.request_id,
                            ctx.instance,
                        ))
                    }
                }
                Err(e) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/household/sync/publish" => {
            if let Err(e) = check_sync_protocol(payload) {
                return Some(api_error_response(&e, ctx.request_id, ctx.instance));
            }
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let folder = match sync_folder(&state) {
                Ok(f) => f,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let base_token = household_state_token(&state).unwrap_or_default();
            let pending_len = state
                .get("household")
                .and_then(|h| h.get("outbox"))
                .and_then(Value::as_array)
                .map(Vec::len)
                .unwrap_or(0);
            let mut detached = (*state).clone();
            let mut result = match publish_household_outbox(&mut detached, &folder) {
                Ok(r) => r,
                Err(_) => {
                    return Some(api_error_response(
                        &ApiError::bad_request("Household sync data is invalid or unavailable.")
                            .with_code("HOUSEHOLD_SYNC_INVALID"),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let pub_ids: Vec<String> = result
                .get("published_ids")
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .filter_map(Value::as_str)
                        .map(str::to_owned)
                        .collect()
                })
                .unwrap_or_default();
            let mut acknowledged = 0usize;
            if !pub_ids.is_empty() {
                match ctx.app.transact_state(|cur| {
                    if household_state_token(cur).unwrap_or_default() != base_token {
                        return Ok(Err("stale".to_owned()));
                    }
                    Ok(acknowledge_outbox(cur, &pub_ids).map_err(|e| e.to_string()))
                }) {
                    Ok((_, Ok(ack))) => acknowledged = ack,
                    Ok((_, Err(ref msg))) if msg == "stale" => {
                        return Some(api_error_response(
                            &ApiError::conflict(
                                "Household state changed while sync was publishing; retry the operation.",
                            )
                            .with_code("HOUSEHOLD_SYNC_STALE"),
                            ctx.request_id,
                            ctx.instance,
                        ));
                    }
                    _ => {
                        return Some(api_error_response(
                            &ApiError::bad_request(
                                "Household sync data is invalid or unavailable.",
                            )
                            .with_code("HOUSEHOLD_SYNC_INVALID"),
                            ctx.request_id,
                            ctx.instance,
                        ));
                    }
                }
            }
            if let Some(m) = result.as_object_mut() {
                m.insert("acknowledged".into(), json!(acknowledged));
                m.insert(
                    "pending".into(),
                    json!(pending_len.saturating_sub(acknowledged)),
                );
            }
            let st_after = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            ctx.app.sse.broadcast_event(
                "household.changed",
                &json!({"event": "sync_publish", "published": result["published"]}),
            );
            Some(send_snapshot(ctx, &st_after, None, vec![("sync", result)]))
        }
        "/api/v2/household/sync/pull" => {
            if let Err(e) = check_sync_protocol(payload) {
                return Some(api_error_response(&e, ctx.request_id, ctx.instance));
            }
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let folder = match sync_folder(&state) {
                Ok(f) => f,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let incoming = match read_household_records(&folder) {
                Ok(r) => r,
                Err(_) => {
                    return Some(api_error_response(
                        &ApiError::bad_request("Household sync data is invalid or unavailable.")
                            .with_code("HOUSEHOLD_SYNC_INVALID"),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let incoming_ids: Vec<String> = incoming
                .iter()
                .filter_map(|i| i.get("event_id").and_then(Value::as_str).map(str::to_owned))
                .collect();
            let pulled_len = incoming.len();
            let res = ctx.app.transact_state(|cur| {
                let mut merged = match merge_records(cur, &incoming) {
                    Ok(m) => m,
                    Err(e) => return Ok(Err(e)),
                };
                let ack = match acknowledge_outbox(cur, &incoming_ids) {
                    Ok(a) => a,
                    Err(e) => return Ok(Err(e)),
                };
                if let Some(m) = merged.as_object_mut() {
                    m.insert("acknowledged".into(), json!(ack));
                    m.insert("pulled".into(), json!(pulled_len));
                }
                Ok(Ok(merged))
            });
            match res {
                Ok((st_after, Ok(sync_res))) => {
                    ctx.app.sse.broadcast_event(
                        "household.changed",
                        &json!({"event": "sync_pull", "pulled": pulled_len}),
                    );
                    Some(send_snapshot(
                        ctx,
                        &st_after,
                        None,
                        vec![("sync", sync_res)],
                    ))
                }
                _ => Some(api_error_response(
                    &ApiError::bad_request("Household sync data is invalid or unavailable.")
                        .with_code("HOUSEHOLD_SYNC_INVALID"),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/household/merge" => {
            let Some(incoming) = payload.get("records").and_then(Value::as_array) else {
                return Some(api_error_response(
                    &ApiError::bad_request("records must be a list.")
                        .with_code("HOUSEHOLD_INVALID_REQUEST"),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let res = ctx
                .app
                .transact_state(|cur| Ok(merge_records(cur, incoming)));
            match res {
                Ok((st_after, Ok(merge_res))) => {
                    ctx.app.sse.broadcast_event(
                        "household.changed",
                        &json!({"event": "merge", "applied": merge_res["applied"]}),
                    );
                    Some(send_snapshot(
                        ctx,
                        &st_after,
                        None,
                        vec![("merge", merge_res)],
                    ))
                }
                Ok((_, Err(err))) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()).with_code("HOUSEHOLD_INVALID_RECORD"),
                    ctx.request_id,
                    ctx.instance,
                )),
                Err(e) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/household/record" => {
            let Some(rec) = payload.get("record").filter(|v| v.is_object()) else {
                return Some(api_error_response(
                    &ApiError::bad_request("record must be an object.")
                        .with_code("HOUSEHOLD_INVALID_REQUEST"),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let slice = std::slice::from_ref(rec);
            let res = ctx.app.transact_state(|cur| Ok(merge_records(cur, slice)));
            match res {
                Ok((st_after, Ok(_))) => {
                    ctx.app.sse.broadcast_event(
                        "household.changed",
                        &json!({"event": "record", "record_id": rec.get("event_id")}),
                    );
                    Some(send_snapshot(
                        ctx,
                        &st_after,
                        None,
                        vec![("record", rec.clone())],
                    ))
                }
                Ok((_, Err(err))) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()).with_code("HOUSEHOLD_INVALID_RECORD"),
                    ctx.request_id,
                    ctx.instance,
                )),
                Err(e) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            }
        }
        _ => None,
    }
}
