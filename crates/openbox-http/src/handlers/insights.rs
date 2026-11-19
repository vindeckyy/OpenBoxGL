//! Play Insights, Backlog Radio, Abandonment Radar, Per-Game Story, & Trophies handlers
//! (`handlers/insights.py`).

use axum::http::StatusCode;
use axum::response::Response;
use jiff::civil::Date;
use openbox_core::ApiError;
use openbox_discovery::insights::{compute_heatmap, mastery_summary, summarize, wrapped_summary};
use openbox_discovery::radio::{
    entry_payload, managed_playlist, park_game, parked_ids, playlist_stale, radar, radio_payload,
};
use openbox_discovery::story::game_story;
use openbox_discovery::trophies::{record_new_awards, trophy_case, unrecorded_awards};
use serde_json::{Map, Value, json};

use crate::handlers::projection::find_game_index_in_state;
use crate::handlers::shared::{RequestCtx, str_field};
use crate::middleware::{api_error_response, json_response, state_error_response};

fn parse_end_date_and_days(ctx: &RequestCtx<'_>) -> Result<(Option<Date>, usize), ApiError> {
    let end_date = match ctx.query_first("end_date").filter(|s| !s.trim().is_empty()) {
        None => None,
        Some(raw) => Some(
            raw.trim()
                .parse::<Date>()
                .map_err(|_| ApiError::bad_request("end_date must be YYYY-MM-DD"))?,
        ),
    };
    let days = match ctx.query_first("days").filter(|s| !s.trim().is_empty()) {
        None => 366,
        Some(raw) => {
            let v = raw
                .trim()
                .parse::<i64>()
                .map_err(|_| ApiError::bad_request("days must be an integer"))?;
            if !(1..=366).contains(&v) {
                return Err(ApiError::bad_request("days must be between 1 and 366"));
            }
            v as usize
        }
    };
    Ok((end_date, days))
}

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
        "/api/v2/insights/summary" => {
            let (end_date, days) = match parse_end_date_and_days(ctx) {
                Ok(pair) => pair,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let payload = summarize(&state, end_date, days);
            Some(json_response(StatusCode::OK, &payload, ctx.instance))
        }
        "/api/v2/insights/heatmap" => {
            let (end_date, days) = match parse_end_date_and_days(ctx) {
                Ok(pair) => pair,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let history = state
                .get("history")
                .and_then(Value::as_array)
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            let heatmap = compute_heatmap(history, days, end_date);
            Some(json_response(
                StatusCode::OK,
                &json!({"heatmap": heatmap, "days": days}),
                ctx.instance,
            ))
        }
        "/api/v2/insights/wrapped" => {
            let raw_year = ctx.query_first("year").unwrap_or_default();
            if raw_year.trim().is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("year is required"),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let year: i16 = match raw_year.trim().parse::<i64>() {
                Ok(y) if (1970..=2100).contains(&y) => y as i16,
                Ok(_) => {
                    return Some(api_error_response(
                        &ApiError::bad_request("year must be between 1970 and 2100"),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
                Err(_) => {
                    return Some(api_error_response(
                        &ApiError::bad_request("year must be an integer"),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            Some(json_response(
                StatusCode::OK,
                &wrapped_summary(&state, year),
                ctx.instance,
            ))
        }
        "/api/v2/story" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let qmap = query_as_payload(ctx);
            let idx = match find_game_index_in_state(&state, &qmap) {
                Ok(i) => i,
                Err(_) => {
                    return Some(api_error_response(
                        &ApiError::bad_request("Game not found.").with_code("GAME_NOT_FOUND"),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let game = &state["games"][idx];
            Some(json_response(
                StatusCode::OK,
                &game_story(&state, game),
                ctx.instance,
            ))
        }
        "/api/v2/insights/mastery" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let games = state
                .get("games")
                .and_then(Value::as_array)
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            let ra_enabled = state
                .get("settings")
                .and_then(|s| s.get("retroachievements_enabled"))
                .and_then(Value::as_bool)
                .unwrap_or(false);
            let ra_cache = ctx.app.data_dir.join("cache").join("retroachievements");
            let ra_dir = if ra_enabled && ra_cache.is_dir() {
                Some(ra_cache.as_path())
            } else {
                None
            };
            Some(json_response(
                StatusCode::OK,
                &mastery_summary(games, ra_dir),
                ctx.instance,
            ))
        }
        "/api/v2/insights/radio" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let has_games = state
                .get("games")
                .and_then(Value::as_array)
                .is_some_and(|a| !a.is_empty());
            if let Some(entry) = managed_playlist(&state)
                && !playlist_stale(entry, None)
            {
                let pl = entry_payload(entry, &state);
                if pl
                    .get("picks")
                    .and_then(Value::as_array)
                    .is_some_and(|a| !a.is_empty())
                    || !has_games
                {
                    return Some(json_response(
                        StatusCode::OK,
                        &json!({"playlist": pl}),
                        ctx.instance,
                    ));
                }
            }
            match ctx
                .app
                .transact_state(|cur| Ok(radio_payload(cur, None, false)))
            {
                Ok((_, pl)) => Some(json_response(
                    StatusCode::OK,
                    &json!({"playlist": pl}),
                    ctx.instance,
                )),
                Err(e) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/insights/radar" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let games = state
                .get("games")
                .and_then(Value::as_array)
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            let history = state
                .get("history")
                .and_then(Value::as_array)
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            let parked = parked_ids(&state);
            Some(json_response(
                StatusCode::OK,
                &json!({"radar": radar(games, history, &parked, None)}),
                ctx.instance,
            ))
        }
        "/api/v2/insights/trophies" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            Some(json_response(
                StatusCode::OK,
                &trophy_case(&state),
                ctx.instance,
            ))
        }
        _ => None,
    }
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/insights/radio/refresh" => {
            match ctx
                .app
                .transact_state(|cur| Ok(radio_payload(cur, None, true)))
            {
                Ok((_, pl)) => Some(json_response(
                    StatusCode::OK,
                    &json!({"playlist": pl}),
                    ctx.instance,
                )),
                Err(e) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/insights/radar/park" => {
            let game_id = str_field(payload, "game_id").trim().to_owned();
            if game_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("game_id is required"),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let res = match ctx.app.transact_state(|cur| Ok(park_game(cur, &game_id))) {
                Ok((_, r)) => r,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let Some(result) = res else {
                return Some(api_error_response(
                    &ApiError::game_not_found("Game not found"),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            Some(json_response(
                StatusCode::OK,
                &json!({
                    "parked": result.get("ok").and_then(Value::as_bool).unwrap_or(false),
                    "game_id": result.get("game_id").cloned().unwrap_or(Value::Null),
                    "name": result.get("name").cloned().unwrap_or(Value::Null),
                    "progress": result.get("progress").cloned().unwrap_or(Value::Null),
                    "code": result.get("code").cloned().unwrap_or(Value::Null),
                }),
                ctx.instance,
            ))
        }
        "/api/v2/insights/trophies/evaluate" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            if unrecorded_awards(&state).is_empty() {
                let case = trophy_case(&state);
                return Some(json_response(
                    StatusCode::OK,
                    &json!({
                        "newly_awarded": [],
                        "earned": case["earned"],
                        "total": case["total"],
                    }),
                    ctx.instance,
                ));
            }
            let (committed, newly) = match ctx
                .app
                .transact_state(|cur| Ok(record_new_awards(cur, None)))
            {
                Ok(pair) => pair,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            for award in &newly {
                ctx.app.sse.broadcast_event("trophy.awarded", award);
            }
            let case = trophy_case(&committed);
            Some(json_response(
                StatusCode::OK,
                &json!({
                    "newly_awarded": newly,
                    "earned": case["earned"],
                    "total": case["total"],
                }),
                ctx.instance,
            ))
        }
        _ => None,
    }
}
