//! Smart "What should I play?" suggestions (`pkg/parity/parity_picker.py`).

use std::collections::HashMap;

use jiff::Timestamp;
use jiff::civil::{Date, DateTime};
use rand::Rng;
use serde_json::{Map, Value, json};

pub const VALID_MOODS: &[&str] = &["any", "action", "chill", "story", "retro", "party"];
pub const VALID_FAMILIARITIES: &[&str] = &["any", "new", "favorite"];
pub const VALID_SCOPES: &[&str] = &["all", "platform", "playlist"];

fn mood_keywords(mood: &str) -> &'static [&'static str] {
    match mood {
        "action" => &[
            "action", "shooter", "fps", "fighting", "platform", "beat", "up", "brawler",
        ],
        "chill" => &[
            "puzzle",
            "simulation",
            "sandbox",
            "strategy",
            "turn",
            "card",
            "board",
        ],
        "story" => &[
            "rpg",
            "adventure",
            "visual novel",
            "point and click",
            "narrative",
        ],
        _ => &[],
    }
}

fn norm_text(v: Option<&Value>) -> String {
    match v {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => s.to_lowercase(),
        Some(other) => other.to_string().to_lowercase(),
    }
}

fn clean_user_rating(game: &Value) -> i64 {
    let raw = game
        .get("user_rating")
        .and_then(|v| {
            v.as_f64()
                .or_else(|| v.as_str().and_then(|s| s.trim().parse::<f64>().ok()))
        })
        .unwrap_or(0.0);
    (raw as i64).clamp(0, 5)
}

fn median(values: &[f64]) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    let mut s = values.to_vec();
    s.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let n = s.len();
    if n % 2 == 1 {
        s[n / 2]
    } else {
        (s[n / 2 - 1] + s[n / 2]) / 2.0
    }
}

pub fn estimated_minutes_for_genre(genre_lower: &str) -> f64 {
    if ["rpg", "strategy", "simulation"]
        .iter()
        .any(|k| genre_lower.contains(k))
    {
        return 120.0;
    }
    if ["adventure", "action", "shooter", "platform", "fighting"]
        .iter()
        .any(|k| genre_lower.contains(k))
    {
        return 60.0;
    }
    if ["puzzle", "card", "board"]
        .iter()
        .any(|k| genre_lower.contains(k))
    {
        return 30.0;
    }
    45.0
}

fn parse_utc_secs(v: Option<&Value>) -> Option<i64> {
    let raw = v.and_then(Value::as_str)?.trim();
    if raw.is_empty() {
        return None;
    }
    if let Ok(ts) = raw.parse::<Timestamp>() {
        return Some(ts.as_second());
    }
    let replaced = raw.replace(' ', "T");
    if let Ok(dt) = replaced.parse::<DateTime>()
        && let Ok(ts) = jiff::tz::TimeZone::UTC.to_timestamp(dt)
    {
        return Some(ts.as_second());
    }
    if raw.len() >= 10
        && let Ok(d) = raw[..10].parse::<Date>()
        && let Ok(ts) = jiff::tz::TimeZone::UTC.to_timestamp(d.at(0, 0, 0, 0))
    {
        return Some(ts.as_second());
    }
    None
}

fn key_str(v: &Value) -> Option<String> {
    match v {
        Value::String(s) if !s.is_empty() => Some(s.clone()),
        Value::Number(n) => Some(n.to_string()),
        _ => None,
    }
}

fn history_value<'a, T>(map: &'a HashMap<String, T>, game: &Value) -> Option<&'a T> {
    for k in ["game_id", "id"] {
        if let Some(key) = game.get(k).and_then(key_str)
            && let Some(val) = map.get(&key)
        {
            return Some(val);
        }
    }
    None
}

fn int_val(v: Option<&Value>, default: i64) -> i64 {
    match v {
        Some(Value::Number(n)) => n
            .as_i64()
            .or_else(|| n.as_f64().map(|f| f as i64))
            .unwrap_or(default),
        Some(Value::String(s)) => s.trim().parse::<i64>().unwrap_or(default),
        _ => default,
    }
}

fn float_val(v: Option<&Value>, default: f64) -> f64 {
    match v {
        Some(Value::Number(n)) => n.as_f64().unwrap_or(default),
        Some(Value::String(s)) => s.trim().parse::<f64>().unwrap_or(default),
        _ => default,
    }
}

fn mood_matches(genre_lower: &str, mood: &str) -> bool {
    if matches!(mood, "any" | "retro" | "party") {
        return mood == "any";
    }
    mood_keywords(mood)
        .iter()
        .any(|kw| genre_lower.contains(kw))
}

#[derive(Debug, Clone)]
pub struct PickerCriteria {
    pub minutes: i64,
    pub mood: String,
    pub familiarity: String,
    pub players: i64,
    pub limit: usize,
}

impl Default for PickerCriteria {
    fn default() -> Self {
        Self {
            minutes: 0,
            mood: "any".to_owned(),
            familiarity: "any".to_owned(),
            players: 1,
            limit: 3,
        }
    }
}

pub fn pick_games(games: &[Value], history: &[Value], criteria: &PickerCriteria) -> Vec<Value> {
    let now_secs = Timestamp::now().as_second();
    let mut history_sessions: HashMap<String, Vec<f64>> = HashMap::new();
    let mut history_recent: HashMap<String, i64> = HashMap::new();

    for entry in history {
        let Some(obj) = entry.as_object() else {
            continue;
        };
        let Some(gid) = obj.get("game_id").and_then(key_str) else {
            continue;
        };
        let secs = float_val(obj.get("seconds"), 0.0);
        if secs > 0.0 {
            history_sessions.entry(gid.clone()).or_default().push(secs);
        }
        if let Some(ts) = parse_utc_secs(obj.get("started")) {
            let slot = history_recent.entry(gid).or_insert(ts);
            if ts > *slot {
                *slot = ts;
            }
        }
    }

    struct Eligible<'a> {
        game: &'a Value,
        score: f64,
        reason_key: Option<&'static str>,
        days: Option<i64>,
        mood_match: bool,
        fits_minutes: bool,
        play_count: i64,
        rating: f64,
        user_rating: i64,
        unplayed_progress: bool,
        genre_lower: String,
    }

    let mut eligible: Vec<Eligible<'_>> = Vec::new();
    let mood = criteria.mood.as_str();
    let familiarity = criteria.familiarity.as_str();
    let players = criteria.players.max(1);
    let minutes = criteria.minutes.max(0);

    for game in games {
        if !game.is_object() {
            continue;
        }
        if game.get("hidden").and_then(Value::as_bool).unwrap_or(false)
            || game
                .get("hide_in_bigbox")
                .and_then(Value::as_bool)
                .unwrap_or(false)
        {
            continue;
        }
        if game.get("path_exists") == Some(&Value::Bool(false))
            && game.get("store_installed") == Some(&Value::Bool(false))
        {
            continue;
        }
        let max_players = int_val(game.get("max_players"), 1).max(1);
        if players > 1 && max_players < players {
            continue;
        }
        let genre = norm_text(game.get("genre"));
        let is_mood_match = mood_matches(&genre, mood);
        if mood == "party" && max_players <= 1 {
            continue;
        }
        if mood == "retro" {
            let year = int_val(game.get("year"), 0);
            if year >= 2001 {
                continue;
            }
        } else if mood != "any" && mood != "party" && !is_mood_match {
            continue;
        }

        let play_count = int_val(game.get("play_count"), 0);
        if familiarity == "new" && play_count > 0 {
            continue;
        }
        let rating = float_val(game.get("rating"), 0.0);
        let user_rating = clean_user_rating(game);
        let is_fav = game
            .get("favorite")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        if familiarity == "favorite" && !(is_fav || rating >= 4.0 || user_rating >= 4) {
            continue;
        }

        let fits_minutes = if minutes <= 0 {
            true
        } else {
            let target = (minutes as f64) * 60.0;
            if let Some(vals) = history_value(&history_sessions, game)
                && !vals.is_empty()
            {
                median(vals) <= target * 1.5
            } else {
                estimated_minutes_for_genre(&genre) <= (minutes as f64) * 1.5
            }
        };
        if !fits_minutes {
            continue;
        }

        let days = parse_utc_secs(game.get("last_played"))
            .or_else(|| history_value(&history_recent, game).copied())
            .map(|ts| ((now_secs - ts).div_euclid(86_400)).max(0));

        let unplayed_progress = matches!(
            game.get("progress").and_then(Value::as_str).unwrap_or(""),
            "" | "Unplayed"
        );
        let mut reason_key = if play_count == 0 {
            Some("picker.reason.never_played")
        } else if is_fav {
            Some("picker.reason.favorite")
        } else if days.is_some_and(|d| d > 30) {
            Some("picker.reason.long_time")
        } else if is_mood_match && mood != "any" {
            Some("picker.reason.mood")
        } else {
            None
        };
        if reason_key.is_none() && rating >= 4.0 {
            reason_key = Some("picker.reason.rated");
        }
        if reason_key.is_none() && minutes > 0 {
            reason_key = Some("picker.reason.fits_session");
        }

        let mut score = if play_count == 0 {
            12.0
        } else {
            (play_count as f64).ln_1p() * 2.5
        };
        if is_fav {
            score += 10.0;
        }
        if rating != 0.0 {
            score += rating * 3.0;
        }
        if user_rating > 0 {
            score += user_rating as f64 * 4.0;
        }
        if unplayed_progress {
            score += 5.0;
        }
        score += match days {
            Some(d) => (d.min(365) as f64) / 365.0 * 8.0,
            None => 8.0,
        };
        if mood != "any" && is_mood_match {
            score += 6.0;
        }
        if minutes > 0 {
            score += 4.0;
        }
        if players > 1 && max_players >= players {
            score += 3.0;
        }

        eligible.push(Eligible {
            game,
            score,
            reason_key,
            days,
            mood_match: is_mood_match,
            fits_minutes,
            play_count,
            rating,
            user_rating,
            unplayed_progress,
            genre_lower: genre,
        });
    }

    if eligible.is_empty() {
        return Vec::new();
    }

    eligible.sort_by(|a, b| {
        b.score
            .partial_cmp(&a.score)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| int_val(a.game.get("id"), 0).cmp(&int_val(b.game.get("id"), 0)))
    });
    eligible.truncate(12);

    let limit = criteria.limit.clamp(1, 3);
    let mut pool = eligible;
    let mut picks = Vec::new();
    let mut rng = rand::thread_rng();
    while !pool.is_empty() && picks.len() < limit {
        let total: f64 = pool.iter().map(|i| i.score).sum();
        let chosen_idx = if total <= 0.0 {
            rng.gen_range(0..pool.len())
        } else {
            let r: f64 = rng.gen_range(0.0..total);
            let mut cum = 0.0;
            let mut idx = pool.len() - 1;
            for (i, item) in pool.iter().enumerate() {
                cum += item.score;
                if cum >= r {
                    idx = i;
                    break;
                }
            }
            idx
        };
        picks.push(pool.remove(chosen_idx));
    }

    let mut result = Vec::with_capacity(picks.len());
    for item in picks {
        let game = item.game;
        let g_name = game
            .get("name")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .unwrap_or("game");
        let is_fav = game
            .get("favorite")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let mut reason_params = Map::new();
        reason_params.insert("name".into(), Value::String(g_name.to_owned()));

        let reason_key = if item.user_rating >= 4 && item.unplayed_progress {
            let ttb_val = game
                .get("time_to_beat_hours")
                .filter(|v| v.as_f64().is_some_and(|n| n > 0.0))
                .cloned()
                .unwrap_or_else(|| {
                    json!((estimated_minutes_for_genre(&item.genre_lower) / 60.0).round() as i64)
                });
            reason_params.insert("stars".into(), json!(item.user_rating));
            reason_params.insert("ttb".into(), ttb_val);
            "picker.reason.rated_backlog"
        } else if item.play_count == 0 {
            "picker.reason.never_played"
        } else if is_fav && item.days.is_none_or(|d| d > 14) {
            reason_params.insert("days".into(), json!(item.days.unwrap_or(0)));
            "picker.reason.favorite"
        } else if let Some(d) = item.days
            && d > 30
        {
            reason_params.insert("days".into(), json!(d));
            "picker.reason.long_time"
        } else if mood != "any" && item.mood_match {
            reason_params.insert("mood".into(), Value::String(mood.to_owned()));
            "picker.reason.mood"
        } else if minutes > 0 && item.fits_minutes {
            reason_params.insert("minutes".into(), json!(minutes));
            "picker.reason.fits_session"
        } else if item.rating >= 4.0 {
            reason_params.insert("rating".into(), json!(item.rating));
            "picker.reason.rated"
        } else {
            item.reason_key.unwrap_or("picker.reason.never_played")
        };

        let gid = game
            .get("game_id")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .or_else(|| game.get("id").and_then(key_str))
            .unwrap_or_default();

        result.push(json!({
            "id": game.get("id").cloned().unwrap_or(Value::Null),
            "game_id": gid,
            "name": g_name,
            "has_cover": game.get("has_cover").and_then(Value::as_bool).unwrap_or(false),
            "cover": game.get("cover").cloned().unwrap_or_else(|| json!("")),
            "cover_kind": "cover",
            "score": (item.score * 100.0).round() / 100.0,
            "reason_key": reason_key,
            "reason_params": Value::Object(reason_params),
        }));
    }
    result
}
