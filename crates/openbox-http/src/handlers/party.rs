//! Game Night party queue & decks handlers (`handlers/party.py`).

use std::sync::LazyLock;

use axum::http::StatusCode;
use axum::response::Response;
use openbox_core::ApiError;
use openbox_discovery::party::{
    PARTY_QUEUE_LIMIT, build_party_queue, clean_party_settings, delete_deck, empty_queue_reason,
    list_decks, queue_exclusion_breakdown, save_deck,
};
use openbox_discovery::query::{filter_games_by_query, parse_query};
use regex::Regex;
use serde_json::{Map, Value, json};
use uuid::Uuid;

use crate::handlers::shared::RequestCtx;
use crate::middleware::{api_error_response, json_response, state_error_response};

static SEED_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[0-9a-f]{8}$").unwrap());

fn parse_int_val(val: Option<&Value>, default: i64, err_msg: &str) -> Result<i64, ApiError> {
    match val {
        None | Some(Value::Null) => Ok(default),
        Some(Value::Number(n)) => n.as_i64().ok_or_else(|| ApiError::bad_request(err_msg)),
        Some(Value::String(s)) => s
            .trim()
            .parse::<i64>()
            .map_err(|_| ApiError::bad_request(err_msg)),
        _ => Err(ApiError::bad_request(err_msg)),
    }
}

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    match ctx.path {
        "/api/v2/party/queue" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let settings = state.get("settings").cloned().unwrap_or_else(|| json!({}));
            let (queue, _players, index) = clean_party_settings(&settings);
            let idx = if queue.is_empty() { 0 } else { index };
            Some(json_response(
                StatusCode::OK,
                &json!({"queue": queue, "index": idx}),
                ctx.instance,
            ))
        }
        "/api/v2/party/decks" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            Some(json_response(
                StatusCode::OK,
                &json!({"decks": list_decks(&state)}),
                ctx.instance,
            ))
        }
        _ => None,
    }
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/party/queue" => {
            let players =
                match parse_int_val(payload.get("players"), 2, "players must be an integer") {
                    Ok(p) if (2..=8).contains(&p) => p,
                    Ok(_) => {
                        return Some(api_error_response(
                            &ApiError::bad_request("players must be between 2 and 8"),
                            ctx.request_id,
                            ctx.instance,
                        ));
                    }
                    Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
                };
            let minutes =
                match parse_int_val(payload.get("minutes"), 0, "minutes must be an integer") {
                    Ok(m) if m >= 0 => m,
                    Ok(_) => {
                        return Some(api_error_response(
                            &ApiError::bad_request("minutes must be >= 0"),
                            ctx.request_id,
                            ctx.instance,
                        ));
                    }
                    Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
                };
            let query = match payload.get("query") {
                None | Some(Value::Null) => String::new(),
                Some(Value::String(s)) => s.trim().to_owned(),
                _ => {
                    return Some(api_error_response(
                        &ApiError::bad_request("query must be text")
                            .with_code("PARTY_QUERY_INVALID"),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            if query.chars().count() > 500 {
                return Some(api_error_response(
                    &ApiError::bad_request("query must be at most 500 characters")
                        .with_code("PARTY_QUERY_INVALID"),
                    ctx.request_id,
                    ctx.instance,
                ));
            }

            let seed = match payload.get("seed") {
                None | Some(Value::Null) => Uuid::new_v4().simple().to_string()[..8].to_owned(),
                Some(Value::String(s)) if s.is_empty() => {
                    Uuid::new_v4().simple().to_string()[..8].to_owned()
                }
                Some(Value::String(s)) if SEED_RE.is_match(s) => s.clone(),
                _ => {
                    return Some(api_error_response(
                        &ApiError::bad_request("seed must be 8 lowercase hex characters")
                            .with_code("PARTY_SEED_INVALID"),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let seed_num = u64::from_str_radix(&seed, 16).unwrap_or(0);

            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let mut games: Vec<Value> = state
                .get("games")
                .and_then(Value::as_array)
                .map(|a| a.iter().filter(|g| g.is_object()).cloned().collect())
                .unwrap_or_default();
            if !query.is_empty() {
                let parsed = parse_query(&query, None);
                games = filter_games_by_query(&games, &parsed, None);
            }
            let queue =
                build_party_queue(&games, players, minutes, PARTY_QUEUE_LIMIT, Some(seed_num));
            let queue_for_save = queue.clone();
            if let Err(e) = ctx.app.transact_state(|live| {
                if let Some(root) = live.as_object_mut() {
                    let settings = root
                        .entry("settings".to_owned())
                        .or_insert_with(|| Value::Object(Map::new()));
                    if let Some(sm) = settings.as_object_mut() {
                        sm.insert("party_queue".into(), json!(queue_for_save));
                        sm.insert("party_players".into(), json!(players));
                        sm.insert("party_index".into(), json!(0));
                    }
                }
                Ok(())
            }) {
                return Some(state_error_response(&e, ctx.request_id, ctx.instance));
            }
            let breakdown = queue_exclusion_breakdown(&games, players, minutes);
            let empty_reason = if queue.is_empty() {
                Value::String(empty_queue_reason(&breakdown, players))
            } else {
                Value::Null
            };
            let count = queue.len();
            Some(json_response(
                StatusCode::OK,
                &json!({
                    "queue": queue,
                    "count": count,
                    "seed": seed,
                    "query": query,
                    "matched": games.len(),
                    "empty_reason": empty_reason,
                    "excluded": breakdown,
                }),
                ctx.instance,
            ))
        }
        "/api/v2/party/next" => {
            let outcome = match ctx.app.transact_state(|live| {
                let settings_val = live.get("settings").cloned().unwrap_or_else(|| json!({}));
                let (queue, _players, mut idx) = clean_party_settings(&settings_val);
                if queue.is_empty() {
                    return Ok(None);
                }
                idx = (idx + 1) % queue.len();
                if let Some(root) = live.as_object_mut() {
                    let settings = root
                        .entry("settings".to_owned())
                        .or_insert_with(|| Value::Object(Map::new()));
                    if let Some(sm) = settings.as_object_mut() {
                        sm.insert("party_index".into(), json!(idx));
                    }
                }
                let game_id = queue[idx].clone();
                let mut name = game_id.clone();
                if let Some(games) = live.get("games").and_then(Value::as_array) {
                    for g in games {
                        if g.get("game_id").and_then(Value::as_str) == Some(game_id.as_str())
                            || g.get("id")
                                .map(|v| {
                                    v.as_str()
                                        .map(str::to_owned)
                                        .unwrap_or_else(|| v.to_string())
                                })
                                .as_deref()
                                == Some(game_id.as_str())
                        {
                            if let Some(n) = g
                                .get("name")
                                .and_then(Value::as_str)
                                .filter(|s| !s.is_empty())
                            {
                                name = n.to_owned();
                            }
                            break;
                        }
                    }
                }
                Ok(Some((game_id, name, idx)))
            }) {
                Ok((_, res)) => res,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let Some((game_id, name, index)) = outcome else {
                return Some(api_error_response(
                    &ApiError::bad_request("party queue is empty — build one first"),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            Some(json_response(
                StatusCode::OK,
                &json!({"game_id": game_id, "name": name, "index": index}),
                ctx.instance,
            ))
        }
        "/api/v2/party/decks" => {
            let name = payload.get("name").cloned();
            let query = payload.get("query").cloned();
            let players = payload.get("players").cloned();
            let minutes = payload.get("minutes").cloned();
            let res = ctx.app.transact_state(|cur| {
                Ok(save_deck(
                    cur,
                    name.as_ref(),
                    query.as_ref(),
                    players.as_ref(),
                    minutes.as_ref(),
                ))
            });
            match res {
                Ok((_, Ok(deck))) => Some(json_response(
                    StatusCode::OK,
                    &json!({"ok": true, "deck": deck}),
                    ctx.instance,
                )),
                Ok((_, Err(msg))) => Some(api_error_response(
                    &ApiError::bad_request(msg).with_code("PARTY_DECK_INVALID"),
                    ctx.request_id,
                    ctx.instance,
                )),
                Err(e) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/party/decks/delete" => {
            let name = payload
                .get("name")
                .and_then(Value::as_str)
                .map(str::trim)
                .unwrap_or("")
                .to_owned();
            if name.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("name is required").with_code("PARTY_DECK_INVALID"),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let name_for_mut = name.clone();
            match ctx
                .app
                .transact_state(|cur| Ok(delete_deck(cur, &name_for_mut)))
            {
                Ok((_, true)) => Some(json_response(
                    StatusCode::OK,
                    &json!({"ok": true, "deleted": name}),
                    ctx.instance,
                )),
                Ok((_, false)) => Some(api_error_response(
                    &ApiError::not_found("Deck not found.").with_code("PARTY_DECK_NOT_FOUND"),
                    ctx.request_id,
                    ctx.instance,
                )),
                Err(e) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            }
        }
        _ => None,
    }
}
