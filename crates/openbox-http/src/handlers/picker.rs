//! Picker handler (`handlers/picker.py`): `POST /api/v2/library/pick`.

use std::collections::{HashMap, HashSet};

use axum::http::StatusCode;
use axum::response::Response;
use openbox_core::ApiError;
use openbox_discovery::dna::parse_dna_query;
use openbox_discovery::picker::{
    PickerCriteria, VALID_FAMILIARITIES, VALID_MOODS, VALID_SCOPES, pick_games,
};
use serde_json::{Map, Value, json};

use crate::handlers::discovery::dna_index_for_search;
use crate::handlers::shared::RequestCtx;
use crate::middleware::{api_error_response, json_response, state_error_response};

fn parse_opt_int(val: Option<&Value>, default: i64, err_msg: &str) -> Result<i64, ApiError> {
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

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    if ctx.path != "/api/v2/library/pick" {
        return None;
    }

    let minutes = match parse_opt_int(payload.get("minutes"), 0, "minutes must be an integer") {
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

    let mood = payload
        .get("mood")
        .and_then(Value::as_str)
        .unwrap_or("any")
        .to_lowercase();
    if !VALID_MOODS.contains(&mood.as_str()) {
        return Some(api_error_response(
            &ApiError::bad_request(format!("mood must be one of {VALID_MOODS:?}")),
            ctx.request_id,
            ctx.instance,
        ));
    }

    let familiarity = payload
        .get("familiarity")
        .and_then(Value::as_str)
        .unwrap_or("any")
        .to_lowercase();
    if !VALID_FAMILIARITIES.contains(&familiarity.as_str()) {
        return Some(api_error_response(
            &ApiError::bad_request(format!(
                "familiarity must be one of {VALID_FAMILIARITIES:?}"
            )),
            ctx.request_id,
            ctx.instance,
        ));
    }

    let players = match parse_opt_int(payload.get("players"), 1, "players must be an integer") {
        Ok(p) if (1..=8).contains(&p) => p,
        Ok(_) => {
            return Some(api_error_response(
                &ApiError::bad_request("players must be between 1 and 8"),
                ctx.request_id,
                ctx.instance,
            ));
        }
        Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
    };

    let scope = payload
        .get("scope")
        .and_then(Value::as_str)
        .unwrap_or("all")
        .to_lowercase();
    if !VALID_SCOPES.contains(&scope.as_str()) {
        return Some(api_error_response(
            &ApiError::bad_request(format!("scope must be one of {VALID_SCOPES:?}")),
            ctx.request_id,
            ctx.instance,
        ));
    }
    let scope_name = payload
        .get("scope_name")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("");

    let seed_query = payload
        .get("seed_query")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("");
    let dna_boost: f64 = match payload.get("dna_boost") {
        None | Some(Value::Null) => 0.0,
        Some(Value::Number(n)) => match n.as_f64() {
            Some(v) if v.is_finite() && v >= 0.0 => v,
            _ => {
                return Some(api_error_response(
                    &ApiError::bad_request("dna_boost must be a finite number >= 0"),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
        },
        Some(Value::String(s)) => match s.trim().parse::<f64>() {
            Ok(v) if v.is_finite() && v >= 0.0 => v,
            Ok(_) => {
                return Some(api_error_response(
                    &ApiError::bad_request("dna_boost must be a finite number >= 0"),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            Err(_) => {
                return Some(api_error_response(
                    &ApiError::bad_request("dna_boost must be a number"),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
        },
        _ => {
            return Some(api_error_response(
                &ApiError::bad_request("dna_boost must be a number"),
                ctx.request_id,
                ctx.instance,
            ));
        }
    };

    let state = match ctx.app.load_state_view() {
        Ok(s) => s,
        Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
    };
    let raw_games = state
        .get("games")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);

    let games: Vec<Value> = if scope == "platform" {
        raw_games
            .iter()
            .filter(|g| {
                g.is_object() && g.get("platform").and_then(Value::as_str) == Some(scope_name)
            })
            .cloned()
            .collect()
    } else if scope == "playlist" {
        let playlist = state
            .get("playlists")
            .and_then(Value::as_array)
            .and_then(|arr| {
                arr.iter()
                    .find(|p| p.get("name").and_then(Value::as_str) == Some(scope_name))
            });
        let Some(pl) = playlist else {
            return Some(api_error_response(
                &ApiError::bad_request(format!("playlist not found: {scope_name}")),
                ctx.request_id,
                ctx.instance,
            ));
        };
        let members: HashSet<String> = pl
            .get("members")
            .and_then(Value::as_array)
            .map(|arr| {
                arr.iter()
                    .map(|m| {
                        m.as_str()
                            .map(str::to_owned)
                            .unwrap_or_else(|| m.to_string())
                    })
                    .collect()
            })
            .unwrap_or_default();
        raw_games
            .iter()
            .filter(|g| {
                g.is_object()
                    && (g
                        .get("id")
                        .map(|v| {
                            v.as_str()
                                .map(str::to_owned)
                                .unwrap_or_else(|| v.to_string())
                        })
                        .is_some_and(|s| members.contains(&s))
                        || g.get("game_id")
                            .and_then(Value::as_str)
                            .is_some_and(|s| members.contains(s)))
            })
            .cloned()
            .collect()
    } else {
        raw_games
            .iter()
            .filter(|g| g.is_object())
            .cloned()
            .collect()
    };

    let history = state
        .get("history")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let criteria = PickerCriteria {
        minutes,
        mood,
        familiarity,
        players,
        limit: 3,
    };
    let mut picks = pick_games(&games, history, &criteria);
    if !seed_query.is_empty() && dna_boost > 0.0 {
        let locale: String = state
            .get("settings")
            .and_then(|s| s.get("locale"))
            .and_then(Value::as_str)
            .unwrap_or("en")
            .chars()
            .take(5)
            .collect();
        let (index_opt, _, _) = dna_index_for_search(&ctx.app, &state);
        if let Some(index) = index_opt {
            let outcome = parse_dna_query(seed_query, &games, &index, &locale, 100);
            let dna_scores: HashMap<String, f64> = outcome
                .get("results")
                .and_then(Value::as_array)
                .map(|arr| {
                    arr.iter()
                        .filter_map(|r| {
                            Some((
                                r.get("game_id")?.as_str()?.to_owned(),
                                r.get("score")?.as_f64()?,
                            ))
                        })
                        .collect()
                })
                .unwrap_or_default();
            if !dna_scores.is_empty() {
                let top = dna_scores.values().copied().fold(0.0f64, f64::max).max(1.0);
                for pick in &mut picks {
                    let gid = pick.get("game_id").and_then(Value::as_str).unwrap_or("");
                    if let Some(&ds) = dna_scores.get(gid)
                        && ds > 0.0
                        && let Some(m) = pick.as_object_mut()
                    {
                        let base = m.get("score").and_then(Value::as_f64).unwrap_or(0.0);
                        let next = ((base * (1.0 + dna_boost * ds / top)) * 100.0).round() / 100.0;
                        m.insert("score".into(), json!(next));
                        m.insert("dna_seed".into(), Value::Bool(true));
                    }
                }
                picks.sort_by(|a, b| {
                    let sa = a.get("score").and_then(Value::as_f64).unwrap_or(0.0);
                    let sb = b.get("score").and_then(Value::as_f64).unwrap_or(0.0);
                    let ga = a.get("game_id").and_then(Value::as_str).unwrap_or("");
                    let gb = b.get("game_id").and_then(Value::as_str).unwrap_or("");
                    sb.partial_cmp(&sa)
                        .unwrap_or(std::cmp::Ordering::Equal)
                        .then_with(|| ga.cmp(gb))
                });
            }
        }
    }

    Some(json_response(
        StatusCode::OK,
        &json!({"picks": picks}),
        ctx.instance,
    ))
}
