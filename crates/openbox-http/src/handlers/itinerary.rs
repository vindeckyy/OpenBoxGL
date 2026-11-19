//! Backlog Itinerary HTTP handlers (`handlers/itinerary.py`, ADR 0113).

use std::sync::Arc;

use axum::http::StatusCode;
use axum::response::Response;
use openbox_core::ApiError;
use openbox_discovery::itinerary::{
    VALID_STRATEGIES, build_itinerary, build_own_history_medians, clamp_horizon_days,
    clamp_minutes_per_day, estimate_game_hours, fetch_missing_igdb_estimates,
    is_eligible_for_itinerary, load_ra_progress_map, missing_igdb_games, project_live_itinerary,
    replan_itinerary,
};
use openbox_metadata::igdb;
use serde_json::{Map, Value, json};

use crate::handlers::projection::find_game_index_in_state;
use crate::handlers::shared::{RequestCtx, bool_field};
use crate::middleware::{api_error_response, json_response, state_error_response};

fn query_as_payload(ctx: &RequestCtx<'_>) -> Map<String, Value> {
    let mut m = Map::new();
    for key in ["game_id", "id", "index", "name"] {
        if let Some(val) = ctx.query_first(key) {
            if (key == "id" || key == "index")
                && let Ok(n) = val.trim().parse::<i64>()
            {
                m.insert(key.to_owned(), json!(n));
                continue;
            }
            m.insert(key.to_owned(), Value::String(val));
        }
    }
    m
}

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    match ctx.path {
        "/api/v2/itinerary" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            Some(json_response(
                StatusCode::OK,
                &project_live_itinerary(&state, None),
                ctx.instance,
            ))
        }
        "/api/v2/itinerary/estimate" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let qmap = query_as_payload(ctx);
            let idx = match find_game_index_in_state(&state, &qmap) {
                Ok(i) => i,
                Err(_) => {
                    return Some(api_error_response(
                        &ApiError::game_not_found("Game not found."),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let games = state
                .get("games")
                .and_then(Value::as_array)
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            let game = &games[idx];
            let (plat_meds, global_med) = build_own_history_medians(games);
            let ra_map = load_ra_progress_map(Some(&ctx.app.data_dir));
            let gid = game.get("game_id").and_then(Value::as_str).unwrap_or("");
            let est = estimate_game_hours(
                game,
                Some(&ctx.app.data_dir),
                Some(&plat_meds),
                global_med,
                ra_map.get(gid),
                None,
            );
            Some(json_response(
                StatusCode::OK,
                &json!({"estimate": est}),
                ctx.instance,
            ))
        }
        _ => None,
    }
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/itinerary/plan" => {
            let strategy = payload
                .get("strategy")
                .and_then(Value::as_str)
                .unwrap_or("quick-wins")
                .trim()
                .to_owned();
            if !VALID_STRATEGIES.contains(&strategy.as_str()) {
                return Some(api_error_response(
                    &ApiError::itinerary_invalid_strategy(format!(
                        "Unsupported strategy '{strategy}'."
                    )),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let horizon = clamp_horizon_days(payload.get("horizon_days"), 30);
            let minutes = clamp_minutes_per_day(payload.get("minutes_per_day"), 60);
            let platforms: Option<Vec<String>> = match payload.get("platforms") {
                None | Some(Value::Null) => None,
                Some(Value::Array(arr)) => {
                    let clean: Vec<String> = arr
                        .iter()
                        .filter_map(Value::as_str)
                        .map(|s| s.trim().to_owned())
                        .filter(|s| !s.is_empty())
                        .collect();
                    if clean.is_empty() { None } else { Some(clean) }
                }
                _ => {
                    return Some(api_error_response(
                        &ApiError::bad_request("platforms must be a list of strings."),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let fetch_igdb = bool_field(payload, "fetch_igdb");
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let eligible: Vec<Value> = state
                .get("games")
                .and_then(Value::as_array)
                .map(|arr| {
                    arr.iter()
                        .filter(|g| {
                            g.is_object()
                                && is_eligible_for_itinerary(g, &state, platforms.as_deref(), None)
                        })
                        .cloned()
                        .collect()
                })
                .unwrap_or_default();
            if eligible.is_empty() {
                return Some(api_error_response(
                    &ApiError::itinerary_no_games(
                        "No eligible unfinished games match your Backlog Itinerary filters.",
                    ),
                    ctx.request_id,
                    ctx.instance,
                ));
            }

            let need_igdb = fetch_igdb
                && igdb::is_configured()
                && !missing_igdb_games(&ctx.app.data_dir, &eligible, None).is_empty();
            if need_igdb {
                let app = Arc::clone(&ctx.app);
                let job =
                    match ctx
                        .app
                        .jobs
                        .submit("itinerary-plan", true, None, None, move |jctx| {
                            let mut cb = |current: usize, total: usize| {
                                jctx.progress(json!({"current": current, "total": total}));
                            };
                            fetch_missing_igdb_estimates(
                                &app.data_dir,
                                &eligible,
                                Some(jctx.cancel_flag()),
                                Some(&mut cb),
                            );
                            let (_, res) = app
                                .transact_state(|cur| {
                                    let plan = build_itinerary(
                                        cur,
                                        horizon,
                                        minutes,
                                        &strategy,
                                        platforms.as_deref(),
                                        Some(&app.data_dir),
                                        None,
                                        None,
                                    );
                                    match plan {
                                        Ok(p) => {
                                            if let Some(root) = cur.as_object_mut() {
                                                root.insert("itinerary".into(), p);
                                            }
                                            Ok(Ok(project_live_itinerary(cur, None)))
                                        }
                                        Err(err) => Ok(Err(err.message)),
                                    }
                                })
                                .map_err(|e| e.to_string())?;
                            res
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
                return Some(json_response(
                    StatusCode::ACCEPTED,
                    &json!({"state": "queued", "job_id": job["job_id"]}),
                    ctx.instance,
                ));
            }

            let data_dir = ctx.app.data_dir.clone();
            match ctx.app.transact_state(|cur| {
                let plan = build_itinerary(
                    cur,
                    horizon,
                    minutes,
                    &strategy,
                    platforms.as_deref(),
                    Some(&data_dir),
                    None,
                    None,
                );
                match plan {
                    Ok(p) => {
                        if let Some(root) = cur.as_object_mut() {
                            root.insert("itinerary".into(), p);
                        }
                        Ok(Ok(project_live_itinerary(cur, None)))
                    }
                    Err(err) => Ok(Err(err)),
                }
            }) {
                Ok((_, Ok(res))) => Some(json_response(StatusCode::OK, &res, ctx.instance)),
                Ok((_, Err(err))) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
                Err(e) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/itinerary/replan" => {
            if let Some(s) = payload.get("strategy") {
                let st = s.as_str().map(str::trim).unwrap_or("");
                if !VALID_STRATEGIES.contains(&st) {
                    return Some(api_error_response(
                        &ApiError::itinerary_invalid_strategy(format!(
                            "Unsupported strategy '{st}'."
                        )),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            }
            let body = payload.clone();
            let data_dir = ctx.app.data_dir.clone();
            match ctx
                .app
                .transact_state(|cur| Ok(replan_itinerary(cur, &body, Some(&data_dir), None)))
            {
                Ok((_, Ok(res))) => Some(json_response(StatusCode::OK, &res, ctx.instance)),
                Ok((_, Err(err))) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
                Err(e) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/itinerary/abandon" => match ctx.app.transact_state(|cur| {
            if let Some(root) = cur.as_object_mut() {
                root.insert("itinerary".into(), Value::Null);
            }
            Ok(json!({"abandoned": true}))
        }) {
            Ok((_, res)) => Some(json_response(StatusCode::OK, &res, ctx.instance)),
            Err(e) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
        },
        _ => None,
    }
}
