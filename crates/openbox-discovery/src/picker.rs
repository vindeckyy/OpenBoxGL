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

#[inline]
fn contains_ascii_icase(haystack: &str, needle_lower: &str) -> bool {
    let h = haystack.as_bytes();
    let n = needle_lower.as_bytes();
    if n.is_empty() {
        return true;
    }
    if n.len() > h.len() {
        return false;
    }
    h.windows(n.len()).any(|w| w.eq_ignore_ascii_case(n))
}

#[inline]
fn clean_user_rating(game: &Map<String, Value>) -> i64 {
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

pub fn estimated_minutes_for_genre(genre: &str) -> f64 {
    if genre.is_empty() {
        return 45.0;
    }
    if ["rpg", "strategy", "simulation"]
        .iter()
        .any(|k| contains_ascii_icase(genre, k))
    {
        return 120.0;
    }
    if ["adventure", "action", "shooter", "platform", "fighting"]
        .iter()
        .any(|k| contains_ascii_icase(genre, k))
    {
        return 60.0;
    }
    if ["puzzle", "card", "board"]
        .iter()
        .any(|k| contains_ascii_icase(genre, k))
    {
        return 30.0;
    }
    45.0
}

#[inline]
fn parse_u32_digits(bytes: &[u8]) -> Option<i64> {
    let mut v = 0i64;
    for &b in bytes {
        if !b.is_ascii_digit() {
            return None;
        }
        v = v * 10 + i64::from(b - b'0');
    }
    Some(v)
}

/// Howard Hinnant's civil-from-ymd days since 1970-01-01 (O(1) integer math).
#[inline]
fn days_from_civil(y: i64, m: i64, d: i64) -> Option<i64> {
    if !(1..=12).contains(&m) || !(1..=31).contains(&d) {
        return None;
    }
    let y = y - i64::from(m <= 2);
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let doy = (153 * (m + if m > 2 { -3 } else { 9 }) + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    Some(era * 146_097 + doe - 719_468)
}

/// Zero-allocation fast ISO-8601 UTC timestamp parser with `jiff` fallback for timezone offsets.
fn parse_utc_secs(v: Option<&Value>) -> Option<i64> {
    let raw = v.and_then(Value::as_str)?.trim();
    if raw.len() < 10 {
        return None;
    }
    let b = raw.as_bytes();
    if b[4] == b'-' && b[7] == b'-' {
        let y = parse_u32_digits(&b[0..4]);
        let m = parse_u32_digits(&b[5..7]);
        let d = parse_u32_digits(&b[8..10]);
        if let (Some(y), Some(m), Some(d)) = (y, m, d)
            && let Some(days) = days_from_civil(y, m, d)
        {
            if b.len() == 10 {
                return Some(days * 86_400);
            }
            if b.len() >= 19
                && (b[10] == b'T' || b[10] == b't' || b[10] == b' ')
                && b[13] == b':'
                && b[16] == b':'
            {
                let rest = &b[19..];
                // Check for explicit non-UTC offset (`+HH:MM` or `-HH:MM`); delegate those to jiff.
                let has_offset = rest.iter().any(|&c| c == b'+' || c == b'-');
                if !has_offset
                    && let (Some(hh), Some(mm), Some(ss)) = (
                        parse_u32_digits(&b[11..13]),
                        parse_u32_digits(&b[14..16]),
                        parse_u32_digits(&b[17..19]),
                    )
                    && hh < 24
                    && mm < 60
                    && ss <= 60
                {
                    return Some(days * 86_400 + hh * 3600 + mm * 60 + ss);
                }
            }
        }
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

#[inline]
fn history_value<'a, T>(
    str_map: &'a HashMap<&'a str, T>,
    num_map: &'a HashMap<i64, T>,
    game: &Map<String, Value>,
) -> Option<&'a T> {
    if str_map.is_empty() && num_map.is_empty() {
        return None;
    }
    for k in ["game_id", "id"] {
        match game.get(k) {
            Some(Value::String(s)) if !s.is_empty() => {
                if let Some(val) = str_map.get(s.as_str()) {
                    return Some(val);
                }
            }
            Some(Value::Number(n)) => {
                if let Some(i) = n.as_i64()
                    && let Some(val) = num_map.get(&i)
                {
                    return Some(val);
                }
            }
            _ => {}
        }
    }
    None
}

#[inline]
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

#[inline]
fn float_val(v: Option<&Value>, default: f64) -> f64 {
    match v {
        Some(Value::Number(n)) => n.as_f64().unwrap_or(default),
        Some(Value::String(s)) => s.trim().parse::<f64>().unwrap_or(default),
        _ => default,
    }
}

#[inline]
fn mood_matches(genre: &str, mood: &str) -> bool {
    if matches!(mood, "any" | "retro" | "party") {
        return mood == "any";
    }
    if genre.is_empty() {
        return false;
    }
    mood_keywords(mood)
        .iter()
        .any(|kw| contains_ascii_icase(genre, kw))
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

struct Eligible<'a> {
    game: &'a Value,
    game_id_num: i64,
    score: f64,
    reason_key: Option<&'static str>,
    days: Option<i64>,
    mood_match: bool,
    fits_minutes: bool,
    play_count: i64,
    rating: f64,
    user_rating: i64,
    unplayed_progress: bool,
    genre: &'a str,
}

#[inline]
fn eligible_cmp(a: &Eligible<'_>, b: &Eligible<'_>) -> std::cmp::Ordering {
    b.score
        .partial_cmp(&a.score)
        .unwrap_or(std::cmp::Ordering::Equal)
        .then_with(|| a.game_id_num.cmp(&b.game_id_num))
}

pub fn pick_games(games: &[Value], history: &[Value], criteria: &PickerCriteria) -> Vec<Value> {
    pick_games_iter(games.iter(), history, criteria)
}

pub fn pick_games_refs(
    games: &[&Value],
    history: &[Value],
    criteria: &PickerCriteria,
) -> Vec<Value> {
    pick_games_iter(games.iter().copied(), history, criteria)
}

fn pick_games_iter<'a, I>(games: I, history: &[Value], criteria: &PickerCriteria) -> Vec<Value>
where
    I: IntoIterator<Item = &'a Value>,
{
    let now_secs = Timestamp::now().as_second();
    let mut hist_sessions_str: HashMap<&str, Vec<f64>> = HashMap::new();
    let mut hist_sessions_num: HashMap<i64, Vec<f64>> = HashMap::new();
    let mut hist_recent_str: HashMap<&str, i64> = HashMap::new();
    let mut hist_recent_num: HashMap<i64, i64> = HashMap::new();

    for entry in history {
        let Some(obj) = entry.as_object() else {
            continue;
        };
        let secs = float_val(obj.get("seconds"), 0.0);
        let ts_opt = parse_utc_secs(obj.get("started"));
        match obj.get("game_id") {
            Some(Value::String(gid)) if !gid.is_empty() => {
                let s = gid.as_str();
                if secs > 0.0 {
                    hist_sessions_str.entry(s).or_default().push(secs);
                }
                if let Some(ts) = ts_opt {
                    let slot = hist_recent_str.entry(s).or_insert(ts);
                    if ts > *slot {
                        *slot = ts;
                    }
                }
            }
            Some(Value::Number(n)) => {
                if let Some(id_num) = n.as_i64() {
                    if secs > 0.0 {
                        hist_sessions_num.entry(id_num).or_default().push(secs);
                    }
                    if let Some(ts) = ts_opt {
                        let slot = hist_recent_num.entry(id_num).or_insert(ts);
                        if ts > *slot {
                            *slot = ts;
                        }
                    }
                }
            }
            _ => {}
        }
    }

    const POOL_CAP: usize = 12;
    let mut top: Vec<Eligible<'_>> = Vec::with_capacity(POOL_CAP + 1);
    let mood = criteria.mood.as_str();
    let familiarity = criteria.familiarity.as_str();
    let players = criteria.players.max(1);
    let minutes = criteria.minutes.max(0);
    let target_secs = (minutes as f64) * 60.0;
    let max_est_mins = (minutes as f64) * 1.5;

    for game in games {
        let Some(g) = game.as_object() else {
            continue;
        };
        if g.get("hidden").and_then(Value::as_bool).unwrap_or(false)
            || g.get("hide_in_bigbox")
                .and_then(Value::as_bool)
                .unwrap_or(false)
        {
            continue;
        }
        if g.get("path_exists") == Some(&Value::Bool(false))
            && g.get("store_installed") == Some(&Value::Bool(false))
        {
            continue;
        }
        let max_players = if players > 1 || mood == "party" {
            int_val(g.get("max_players"), 1).max(1)
        } else {
            1
        };
        if players > 1 && max_players < players {
            continue;
        }
        if mood == "party" && max_players <= 1 {
            continue;
        }

        let genre = g.get("genre").and_then(Value::as_str).unwrap_or("");
        let is_mood_match = mood_matches(genre, mood);
        if mood == "retro" {
            let year = int_val(g.get("year"), 0);
            if year >= 2001 {
                continue;
            }
        } else if mood != "any" && mood != "party" && !is_mood_match {
            continue;
        }

        let play_count = int_val(g.get("play_count"), 0);
        if familiarity == "new" && play_count > 0 {
            continue;
        }
        let rating = float_val(g.get("rating"), 0.0);
        let user_rating = clean_user_rating(g);
        let is_fav = g.get("favorite").and_then(Value::as_bool).unwrap_or(false);
        if familiarity == "favorite" && !(is_fav || rating >= 4.0 || user_rating >= 4) {
            continue;
        }

        let fits_minutes = if minutes <= 0 {
            true
        } else if let Some(vals) = history_value(&hist_sessions_str, &hist_sessions_num, g)
            && !vals.is_empty()
        {
            median(vals) <= target_secs * 1.5
        } else {
            estimated_minutes_for_genre(genre) <= max_est_mins
        };
        if !fits_minutes {
            continue;
        }

        let days = parse_utc_secs(g.get("last_played"))
            .or_else(|| history_value(&hist_recent_str, &hist_recent_num, g).copied())
            .map(|ts| ((now_secs - ts).div_euclid(86_400)).max(0));

        let unplayed_progress = matches!(
            g.get("progress").and_then(Value::as_str).unwrap_or(""),
            "" | "Unplayed"
        );

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

        // Fast reject against the 12th-best candidate before allocating or sorting.
        let game_id_num = int_val(g.get("id"), 0);
        if top.len() == POOL_CAP {
            let worst = &top[POOL_CAP - 1];
            let cmp = worst
                .score
                .partial_cmp(&score)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then_with(|| game_id_num.cmp(&worst.game_id_num));
            if cmp != std::cmp::Ordering::Less {
                continue;
            }
        }

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

        let cand = Eligible {
            game,
            game_id_num,
            score,
            reason_key,
            days,
            mood_match: is_mood_match,
            fits_minutes,
            play_count,
            rating,
            user_rating,
            unplayed_progress,
            genre,
        };
        let pos = top
            .binary_search_by(|probe| eligible_cmp(probe, &cand))
            .unwrap_or_else(|e| e);
        if pos < POOL_CAP {
            top.insert(pos, cand);
            if top.len() > POOL_CAP {
                top.pop();
            }
        }
    }

    if top.is_empty() {
        return Vec::new();
    }

    let limit = criteria.limit.clamp(1, 3);
    let mut pool = top;
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
                    json!((estimated_minutes_for_genre(item.genre) / 60.0).round() as i64)
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
