//! Game Night party queue builder & saved decks (`pkg/parity/parity_party.py`).

use std::collections::HashSet;

use rand::rngs::StdRng;
use rand::{Rng, SeedableRng};
use serde_json::{Value, json};

use crate::query::parse_query;

pub const PARTY_QUEUE_LIMIT: usize = 50;
pub const MAX_DECKS: usize = 20;
pub const MAX_DECK_NAME: usize = 60;
pub const MAX_DECK_QUERY: usize = 500;
pub const MAX_DECK_MINUTES: i64 = 1440;

const COUCH_PLATFORMS: &[&str] = &[
    "NES",
    "SNES",
    "Nintendo 64",
    "GameCube",
    "Wii",
    "Wii U",
    "Switch",
    "Nintendo Switch",
    "Game Boy",
    "Game Boy Color",
    "Game Boy Advance",
    "Nintendo DS",
    "Nintendo 3DS",
    "Sega Genesis",
    "Sega Mega Drive",
    "Sega Master System",
    "Sega Game Gear",
    "Sega Saturn",
    "Sega Dreamcast",
    "Sega CD",
    "PlayStation",
    "PlayStation 2",
    "PlayStation 3",
    "PlayStation 4",
    "PlayStation 5",
    "PSP",
    "PlayStation Vita",
    "Xbox",
    "Xbox 360",
    "Xbox One",
    "Xbox Series X",
    "Xbox Series S",
    "Arcade",
    "MAME",
    "Neo Geo",
    "Neo Geo AES",
    "Atari 2600",
    "Atari 5200",
    "Atari 7800",
    "Atari Jaguar",
    "Atari Lynx",
    "Turbografx-16",
    "TurboGrafx-16",
    "PC Engine",
];

fn is_couch_platform(platform: &str) -> bool {
    COUCH_PLATFORMS.contains(&platform)
}

fn stable_id(game: &Value) -> String {
    if let Some(s) = game
        .get("game_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        return s.to_owned();
    }
    match game.get("id") {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) => n.to_string(),
        _ => String::new(),
    }
}

fn clean_deck_name(raw: Option<&Value>) -> Result<String, String> {
    let text = raw.and_then(Value::as_str).map(str::trim).unwrap_or("");
    if text.is_empty() {
        return Err("Deck name is required.".to_owned());
    }
    if text.chars().count() > MAX_DECK_NAME {
        return Err(format!(
            "Deck name must be at most {MAX_DECK_NAME} characters."
        ));
    }
    Ok(text.to_owned())
}

fn clean_deck_query(raw: Option<&Value>) -> Result<String, String> {
    let text = match raw {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => s.split_whitespace().collect::<Vec<_>>().join(" "),
        _ => return Err("Deck filter must be text.".to_owned()),
    };
    if text.chars().count() > MAX_DECK_QUERY {
        return Err(format!(
            "Deck filter must be at most {MAX_DECK_QUERY} characters."
        ));
    }
    Ok(text)
}

fn clean_deck_players(raw: Option<&Value>) -> Result<i64, String> {
    let val = match raw {
        None | Some(Value::Null) => 2,
        Some(Value::Number(n)) => n
            .as_i64()
            .ok_or_else(|| "Deck players must be an integer.".to_owned())?,
        Some(Value::String(s)) => s
            .trim()
            .parse::<i64>()
            .map_err(|_| "Deck players must be an integer.".to_owned())?,
        _ => return Err("Deck players must be an integer.".to_owned()),
    };
    if !(2..=8).contains(&val) {
        return Err("Deck players must be between 2 and 8.".to_owned());
    }
    Ok(val)
}

fn clean_deck_minutes(raw: Option<&Value>) -> Result<i64, String> {
    let val = match raw {
        None | Some(Value::Null) => 0,
        Some(Value::Number(n)) => n
            .as_i64()
            .ok_or_else(|| "Deck minutes must be an integer.".to_owned())?,
        Some(Value::String(s)) if s.trim().is_empty() => 0,
        Some(Value::String(s)) => s
            .trim()
            .parse::<i64>()
            .map_err(|_| "Deck minutes must be an integer.".to_owned())?,
        _ => return Err("Deck minutes must be an integer.".to_owned()),
    };
    if !(0..=MAX_DECK_MINUTES).contains(&val) {
        return Err(format!(
            "Deck minutes must be between 0 and {MAX_DECK_MINUTES}."
        ));
    }
    Ok(val)
}

pub fn list_decks(state: &Value) -> Vec<Value> {
    state
        .get("party_decks")
        .and_then(Value::as_array)
        .map(|arr| arr.iter().filter(|v| v.is_object()).cloned().collect())
        .unwrap_or_default()
}

pub fn save_deck(
    state: &mut Value,
    name: Option<&Value>,
    query: Option<&Value>,
    players: Option<&Value>,
    minutes: Option<&Value>,
) -> Result<Value, String> {
    let name = clean_deck_name(name)?;
    let query = clean_deck_query(query)?;
    if !query.is_empty() {
        let _ = parse_query(&query, None);
    }
    let players = clean_deck_players(players)?;
    let minutes = clean_deck_minutes(minutes)?;
    let entry = json!({
        "name": name,
        "query": query,
        "players": players,
        "minutes": minutes,
    });
    let Some(root) = state.as_object_mut() else {
        return Err("State must be an object.".to_owned());
    };
    let slot = root
        .entry("party_decks".to_owned())
        .or_insert_with(|| Value::Array(Vec::new()));
    if !slot.is_array() {
        *slot = Value::Array(Vec::new());
    }
    if let Some(arr) = slot.as_array_mut() {
        let name_low = name.to_lowercase();
        for item in arr.iter_mut() {
            if item
                .get("name")
                .and_then(Value::as_str)
                .map(|s| s.trim().to_lowercase())
                .as_deref()
                == Some(name_low.as_str())
            {
                *item = entry.clone();
                return Ok(entry);
            }
        }
        if arr.len() >= MAX_DECKS {
            return Err(format!("At most {MAX_DECKS} decks can be saved."));
        }
        arr.push(entry.clone());
    }
    Ok(entry)
}

pub fn delete_deck(state: &mut Value, name: &str) -> bool {
    let clean = name.trim();
    let Some(arr) = state.get_mut("party_decks").and_then(Value::as_array_mut) else {
        return false;
    };
    let before = arr.len();
    arr.retain(|item| item.get("name").and_then(Value::as_str) != Some(clean));
    arr.len() != before
}

fn max_players(game: &Value) -> i64 {
    game.get("max_players")
        .and_then(|v| {
            v.as_i64()
                .or_else(|| v.as_f64().map(|f| f as i64))
                .or_else(|| v.as_str().and_then(|s| s.trim().parse::<i64>().ok()))
        })
        .unwrap_or(1)
        .max(1)
}

fn path_usable(game: &Value) -> bool {
    !(game.get("path_exists") == Some(&Value::Bool(false))
        && !game
            .get("store_installed")
            .and_then(Value::as_bool)
            .unwrap_or(false))
}

fn avg_session_seconds(game: &Value) -> Option<f64> {
    let plays = game.get("play_count").and_then(Value::as_i64).unwrap_or(0);
    let total = game
        .get("playtime_seconds")
        .and_then(Value::as_f64)
        .unwrap_or(0.0);
    if plays > 0 && total > 0.0 {
        Some(total / plays as f64)
    } else {
        None
    }
}

pub fn eligible_party_games(games: &[Value], players: i64) -> Vec<&Value> {
    games
        .iter()
        .filter(|g| {
            if !g.is_object() {
                return false;
            }
            if g.get("hidden").and_then(Value::as_bool).unwrap_or(false)
                || g.get("hide_in_bigbox")
                    .and_then(Value::as_bool)
                    .unwrap_or(false)
            {
                return false;
            }
            if !path_usable(g) || max_players(g) < players {
                return false;
            }
            let platform = g
                .get("platform")
                .and_then(Value::as_str)
                .map(str::trim)
                .unwrap_or("");
            let has_ctrl = g
                .get("controller_support")
                .and_then(Value::as_str)
                .is_some_and(|s| !s.trim().is_empty());
            has_ctrl || is_couch_platform(platform)
        })
        .collect()
}

pub fn queue_exclusion_breakdown(games: &[Value], players: i64, minutes: i64) -> Value {
    let players = players.clamp(2, 8);
    let budget = if minutes > 0 {
        Some((minutes * 60 * 3) as f64)
    } else {
        None
    };
    let mut total = 0usize;
    let mut hidden = 0usize;
    let mut unusable_path = 0usize;
    let mut too_few_players = 0usize;
    let mut no_controller_or_platform = 0usize;
    let mut over_budget = 0usize;

    for game in games {
        if !game.is_object() {
            continue;
        }
        total += 1;
        if game.get("hidden").and_then(Value::as_bool).unwrap_or(false)
            || game
                .get("hide_in_bigbox")
                .and_then(Value::as_bool)
                .unwrap_or(false)
        {
            hidden += 1;
            continue;
        }
        if !path_usable(game) {
            unusable_path += 1;
            continue;
        }
        if max_players(game) < players {
            too_few_players += 1;
            continue;
        }
        let platform = game
            .get("platform")
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or("");
        let has_ctrl = game
            .get("controller_support")
            .and_then(Value::as_str)
            .is_some_and(|s| !s.trim().is_empty());
        if !has_ctrl && !is_couch_platform(platform) {
            no_controller_or_platform += 1;
            continue;
        }
        if let Some(b) = budget
            && avg_session_seconds(game).is_some_and(|avg| avg > b)
        {
            over_budget += 1;
        }
    }
    json!({
        "total": total,
        "hidden": hidden,
        "unusable_path": unusable_path,
        "too_few_players": too_few_players,
        "no_controller_or_platform": no_controller_or_platform,
        "over_budget": over_budget,
    })
}

pub fn empty_queue_reason(breakdown: &Value, players: i64) -> String {
    let get_u = |k: &str| breakdown.get(k).and_then(Value::as_u64).unwrap_or(0);
    if get_u("total") == 0 {
        return "Your library is empty \u{2014} import games to build a party queue.".to_owned();
    }
    let mut ranked = [
        (
            get_u("too_few_players"),
            format!(
                "No games support {players} players \u{2014} lower the player count or add Max players metadata."
            ),
        ),
        (
            get_u("no_controller_or_platform"),
            "No couch-ready games found \u{2014} add controller support or console platforms."
                .to_owned(),
        ),
        (
            get_u("unusable_path"),
            "Every candidate has missing game files \u{2014} fix paths or reinstall.".to_owned(),
        ),
        (
            get_u("over_budget"),
            "Every candidate runs longer than the session budget \u{2014} raise the minutes."
                .to_owned(),
        ),
        (
            get_u("hidden"),
            "Every game in the library is hidden.".to_owned(),
        ),
    ];
    ranked.sort_by_key(|a| std::cmp::Reverse(a.0));
    for (cnt, msg) in ranked {
        if cnt > 0 {
            return msg;
        }
    }
    "No eligible games for this setup.".to_owned()
}

pub fn build_party_queue(
    games: &[Value],
    players: i64,
    minutes: i64,
    limit: usize,
    seed: Option<u64>,
) -> Vec<String> {
    let players = players.clamp(2, 8);
    let limit = limit.clamp(1, PARTY_QUEUE_LIMIT);
    let mut candidates = eligible_party_games(games, players);
    if minutes > 0 {
        let budget = (minutes * 60 * 3) as f64;
        candidates.retain(|g| avg_session_seconds(g).is_none_or(|avg| avg <= budget));
    }
    if seed.is_some() {
        candidates.sort_by_key(|g| stable_id(g));
    }
    let mut scored: Vec<(f64, f64, String)> = if let Some(s) = seed {
        let mut rng = StdRng::seed_from_u64(s);
        candidates
            .into_iter()
            .map(|g| {
                let rating = g.get("rating").and_then(Value::as_f64).unwrap_or(0.0);
                (-rating, rng.r#gen::<f64>(), stable_id(g))
            })
            .collect()
    } else {
        let mut rng = rand::thread_rng();
        candidates
            .into_iter()
            .map(|g| {
                let rating = g.get("rating").and_then(Value::as_f64).unwrap_or(0.0);
                (-rating, rng.r#gen::<f64>(), stable_id(g))
            })
            .collect()
    };
    scored.sort_by(|a, b| {
        a.0.partial_cmp(&b.0)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.1.partial_cmp(&b.1).unwrap_or(std::cmp::Ordering::Equal))
    });
    scored
        .into_iter()
        .take(limit)
        .filter_map(|(_, _, gid)| if gid.is_empty() { None } else { Some(gid) })
        .collect()
}

pub fn clean_party_settings(settings: &Value) -> (Vec<String>, i64, usize) {
    let mut seen = HashSet::new();
    let queue: Vec<String> = settings
        .get("party_queue")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .filter_map(Value::as_str)
                .map(str::trim)
                .filter(|s| !s.is_empty() && seen.insert((*s).to_owned()))
                .take(PARTY_QUEUE_LIMIT)
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default();
    let players = settings
        .get("party_players")
        .and_then(Value::as_i64)
        .unwrap_or(2)
        .clamp(2, 8);
    let raw_idx = settings
        .get("party_index")
        .and_then(Value::as_i64)
        .unwrap_or(0)
        .max(0) as usize;
    let index = if queue.is_empty() {
        0
    } else {
        raw_idx.min(queue.len() - 1)
    };
    (queue, players, index)
}
