//! Backlog Radio — habit-aware playlist + abandonment radar (`pkg/parity/parity_radio.py`).

use std::collections::{HashMap, HashSet};

use jiff::Timestamp;
use jiff::civil::{Date, DateTime};
use serde_json::{Map, Value, json};

use crate::constellation::{kind_weight, platform_family};
use crate::picker::estimated_minutes_for_genre;

pub const RADIO_PLAYLIST_NAME: &str = "Backlog Radio";
pub const RADIO_PICK_LIMIT: usize = 5;
pub const RADIO_STALE_DAYS: i64 = 7;
pub const RADIO_WINDOW_DAYS: i64 = 90;
pub const RADIO_HALF_LIFE_DAYS: f64 = 30.0;
pub const MIN_SESSIONS_FOR_HABIT: usize = 8;
pub const RADAR_STALL_DAYS: i64 = 14;
pub const RADAR_PARK_DAYS: i64 = 30;
pub const RADAR_MIN_OBSERVED_SECONDS: f64 = 900.0;
pub const RADAR_MIN_SESSIONS: usize = 3;
pub const RECENTLY_LOVED_DAYS: i64 = 60;
pub const RECENTLY_LOVED_LIMIT: usize = 5;
pub const LOVED_FALLBACK_LIMIT: usize = 3;
pub const LOVED_FALLBACK_MIN_SECONDS: f64 = 3600.0;
pub const INVESTED_WINNABLE_RATIO: f64 = 0.25;
pub const PARK_MAX_INVESTED_RATIO: f64 = 0.15;
pub const PARK_MIN_ESTIMATE_FACTOR: f64 = 1.5;
pub const FRESH_UNPLAYED_DAYS: i64 = 14;
pub const DIVERSITY_SCORE_FRACTION: f64 = 0.6;
pub const COMPLETED_TOKENS: &[&str] = &["beaten", "completed", "mastered"];

fn now_utc(now: Option<Timestamp>) -> Timestamp {
    now.unwrap_or_else(Timestamp::now)
}

fn format_utc_iso(ts: Timestamp) -> String {
    let dt = jiff::tz::TimeZone::UTC.to_datetime(ts);
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}+00:00",
        dt.year(),
        dt.month(),
        dt.day(),
        dt.hour(),
        dt.minute(),
        dt.second()
    )
}

fn parse_utc(value: Option<&Value>) -> Option<Timestamp> {
    let raw = value.and_then(Value::as_str)?.trim();
    if raw.is_empty() {
        return None;
    }
    if let Ok(ts) = raw.parse::<Timestamp>() {
        return Some(ts);
    }
    if let Ok(dt) = raw.replace(' ', "T").parse::<DateTime>() {
        return jiff::tz::TimeZone::UTC.to_timestamp(dt).ok();
    }
    if raw.len() >= 10
        && let Ok(d) = raw[..10].parse::<Date>()
    {
        return jiff::tz::TimeZone::UTC.to_timestamp(d.at(0, 0, 0, 0)).ok();
    }
    None
}

fn day_ordinal(ts: Timestamp) -> i64 {
    ts.as_second().div_euclid(86_400)
}

fn nonneg(v: Option<&Value>) -> f64 {
    match v {
        Some(Value::Number(n)) => n.as_f64().unwrap_or(0.0).max(0.0),
        Some(Value::String(s)) => s.trim().parse::<f64>().unwrap_or(0.0).max(0.0),
        _ => 0.0,
    }
}

pub fn game_key(game: &Value) -> String {
    if let Some(v) = game.get("game_id") {
        match v {
            Value::String(s) if !s.is_empty() => return s.clone(),
            Value::Number(n) => return n.to_string(),
            _ => {}
        }
    }
    match game.get("id") {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) => n.to_string(),
        _ => String::new(),
    }
}

fn genres_of(game: &Value) -> Vec<String> {
    game.get("genre")
        .and_then(Value::as_str)
        .unwrap_or("")
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .collect()
}

fn primary_genre(game: &Value) -> String {
    genres_of(game).into_iter().next().unwrap_or_default()
}

pub fn norm(v: Option<&Value>) -> String {
    match v {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => s.trim().to_lowercase(),
        Some(other) => other.to_string().trim().to_lowercase(),
    }
}

fn daypart(hour: i8) -> &'static str {
    if (5..12).contains(&hour) {
        "morning"
    } else if (12..17).contains(&hour) {
        "afternoon"
    } else if (17..23).contains(&hour) {
        "evening"
    } else {
        "night"
    }
}

pub fn is_completed(game: &Value) -> bool {
    let prog = norm(game.get("progress"));
    COMPLETED_TOKENS.iter().any(|t| prog.contains(t))
}

pub fn eligible(game: &Value) -> bool {
    if !game.is_object() || game_key(game).is_empty() {
        return false;
    }
    if game.get("hidden").and_then(Value::as_bool).unwrap_or(false)
        || game
            .get("hide_in_bigbox")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        || game.get("demo").and_then(Value::as_bool).unwrap_or(false)
    {
        return false;
    }
    if game.get("path_exists") == Some(&Value::Bool(false))
        && game.get("store_installed") == Some(&Value::Bool(false))
    {
        return false;
    }
    !is_completed(game)
}

fn days_since_last_play(
    game: &Value,
    days_for: &HashMap<String, HashSet<i64>>,
    now: Timestamp,
) -> Option<i64> {
    if let Some(last) = parse_utc(game.get("last_played")) {
        return Some(
            (now.as_second() - last.as_second())
                .div_euclid(86_400)
                .max(0),
        );
    }
    let days = days_for.get(&game_key(game))?;
    let max_d = *days.iter().max()?;
    Some((day_ordinal(now) - max_d).max(0))
}

fn days_since_added(game: &Value, now: Timestamp) -> Option<i64> {
    let added = parse_utc(game.get("added_at").or_else(|| game.get("added")))?;
    Some(
        (now.as_second() - added.as_second())
            .div_euclid(86_400)
            .max(0),
    )
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

pub struct HabitModel {
    pub now: Timestamp,
    pub sessions: usize,
    pub enough: bool,
    pub genre_seconds: HashMap<String, f64>,
    pub platform_seconds: HashMap<String, f64>,
    pub genre_dayparts: HashMap<String, HashMap<&'static str, f64>>,
    pub median_session_min: f64,
    pub days_for: HashMap<String, HashSet<i64>>,
    pub completion: HashMap<String, f64>,
    pub completion_budget_min: f64,
    pub loved: Vec<String>,
}

fn completion_budget(games: &[Value]) -> f64 {
    let values: Vec<f64> = games
        .iter()
        .filter(|g| g.is_object() && is_completed(g) && nonneg(g.get("playtime_seconds")) > 0.0)
        .map(|g| nonneg(g.get("playtime_seconds")) / 60.0)
        .collect();
    median(&values)
}

fn recently_loved(
    games: &[Value],
    days_for: &HashMap<String, HashSet<i64>>,
    observed: &HashMap<String, f64>,
    now: Timestamp,
) -> Vec<String> {
    let mut loved: Vec<(i64, String)> = Vec::new();
    let mut pool: Vec<(f64, String)> = Vec::new();
    for game in games {
        if !game.is_object() {
            continue;
        }
        let key = game_key(game);
        if key.is_empty() {
            continue;
        }
        let Some(days) = days_since_last_play(game, days_for, now) else {
            continue;
        };
        if days > RECENTLY_LOVED_DAYS {
            continue;
        }
        let rating = nonneg(game.get("rating"));
        if game
            .get("favorite")
            .and_then(Value::as_bool)
            .unwrap_or(false)
            || rating >= 4.0
        {
            loved.push((days, key));
        } else {
            let pt = nonneg(game.get("playtime_seconds"));
            let obs = observed.get(&key).copied().unwrap_or(0.0);
            if pt >= LOVED_FALLBACK_MIN_SECONDS || obs >= LOVED_FALLBACK_MIN_SECONDS {
                pool.push((pt.max(obs), key));
            }
        }
    }
    loved.sort_by(|a, b| a.0.cmp(&b.0).then_with(|| a.1.cmp(&b.1)));
    if !loved.is_empty() {
        return loved
            .into_iter()
            .take(RECENTLY_LOVED_LIMIT)
            .map(|(_, k)| k)
            .collect();
    }
    pool.sort_by(|a, b| {
        b.0.partial_cmp(&a.0)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.1.cmp(&b.1))
    });
    pool.into_iter()
        .take(LOVED_FALLBACK_LIMIT)
        .map(|(_, k)| k)
        .collect()
}

pub fn build_habit_model(games: &[Value], history: &[Value], now: Option<Timestamp>) -> HabitModel {
    let now_ts = now_utc(now);
    let by_key: HashMap<String, &Value> = games
        .iter()
        .filter(|g| g.is_object())
        .map(|g| (game_key(g), g))
        .filter(|(k, _)| !k.is_empty())
        .collect();
    let window_start = now_ts.as_second() - RADIO_WINDOW_DAYS * 86_400;
    let max_future = now_ts.as_second() + 86_400;

    let mut genre_seconds: HashMap<String, f64> = HashMap::new();
    let mut platform_seconds: HashMap<String, f64> = HashMap::new();
    let mut genre_dayparts: HashMap<String, HashMap<&'static str, f64>> = HashMap::new();
    let mut lengths: Vec<f64> = Vec::new();
    let mut observed: HashMap<String, f64> = HashMap::new();
    let mut days_for: HashMap<String, HashSet<i64>> = HashMap::new();
    let mut sessions = 0usize;

    for entry in history {
        if !entry.is_object() {
            continue;
        }
        let Some(started) = parse_utc(entry.get("started")) else {
            continue;
        };
        let s_sec = started.as_second();
        if s_sec < window_start || s_sec > max_future {
            continue;
        }
        sessions += 1;
        let seconds = nonneg(entry.get("seconds"));
        if seconds > 0.0 {
            lengths.push(seconds);
        }
        let age_days = ((now_ts.as_second() - s_sec) as f64 / 86_400.0).max(0.0);
        let weight = 0.5f64.powf(age_days / RADIO_HALF_LIFE_DAYS);
        let dt = jiff::tz::TimeZone::UTC.to_datetime(started);
        let part = daypart(dt.hour());
        let key = match entry.get("game_id") {
            Some(Value::String(s)) => s.clone(),
            Some(Value::Number(n)) => n.to_string(),
            _ => String::new(),
        };
        if !key.is_empty() {
            days_for
                .entry(key.clone())
                .or_default()
                .insert(day_ordinal(started));
        }
        let Some(&game) = by_key.get(&key) else {
            continue;
        };
        *observed.entry(key).or_insert(0.0) += seconds;
        for g in genres_of(game) {
            *genre_seconds.entry(g.clone()).or_insert(0.0) += seconds * weight;
            *genre_dayparts
                .entry(g)
                .or_default()
                .entry(part)
                .or_insert(0.0) += weight;
        }
        let plat = game
            .get("platform")
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or("");
        if !plat.is_empty() {
            *platform_seconds.entry(plat.to_owned()).or_insert(0.0) += seconds * weight;
        }
    }

    let mut finished: HashMap<String, usize> = HashMap::new();
    let mut abandoned: HashMap<String, usize> = HashMap::new();
    for game in games {
        if !game.is_object() {
            continue;
        }
        let pt = nonneg(game.get("playtime_seconds"));
        if pt <= 0.0 {
            continue;
        }
        let est_secs = estimated_minutes_for_genre(&norm(game.get("genre"))) * 60.0;
        let days = days_since_last_play(game, &days_for, now_ts);
        if is_completed(game) || (est_secs > 0.0 && pt >= 0.8 * est_secs) {
            for g in genres_of(game) {
                *finished.entry(g).or_insert(0) += 1;
            }
        } else if days.is_some_and(|d| d >= 30) {
            for g in genres_of(game) {
                *abandoned.entry(g).or_insert(0) += 1;
            }
        }
    }
    let all_g: HashSet<String> = finished.keys().chain(abandoned.keys()).cloned().collect();
    let mut completion = HashMap::new();
    for g in all_g {
        let f = finished.get(&g).copied().unwrap_or(0);
        let a = abandoned.get(&g).copied().unwrap_or(0);
        if f + a > 0 {
            completion.insert(g, f as f64 / (f + a) as f64);
        }
    }

    let loved = recently_loved(games, &days_for, &observed, now_ts);
    HabitModel {
        now: now_ts,
        sessions,
        enough: sessions >= MIN_SESSIONS_FOR_HABIT,
        genre_seconds,
        platform_seconds,
        genre_dayparts,
        median_session_min: median(&lengths) / 60.0,
        days_for,
        completion,
        completion_budget_min: completion_budget(games),
        loved,
    }
}

fn shared_edge(a: &Value, b: &Value, days_for: &HashMap<String, HashSet<i64>>) -> f64 {
    let mut best = 0.0f64;
    let sa = norm(a.get("series"));
    if !sa.is_empty() && sa == norm(b.get("series")) {
        best = kind_weight("series");
    }
    let da = norm(a.get("developer"));
    if !da.is_empty() && da == norm(b.get("developer")) {
        best = best.max(kind_weight("developer"));
    }
    let pa = norm(a.get("publisher"));
    if !pa.is_empty() && pa == norm(b.get("publisher")) {
        best = best.max(kind_weight("publisher"));
    }
    let ga: HashSet<String> = genres_of(a).into_iter().map(|s| s.to_lowercase()).collect();
    let gb: HashSet<String> = genres_of(b).into_iter().map(|s| s.to_lowercase()).collect();
    if !ga.is_disjoint(&gb) {
        best = best.max(kind_weight("genre"));
    }
    let fa = platform_family(a.get("platform").and_then(Value::as_str).unwrap_or(""));
    let fb = platform_family(b.get("platform").and_then(Value::as_str).unwrap_or(""));
    if fa == fb && fa != "Other" {
        best = best.max(kind_weight("platform_family"));
    }
    if let (Some(days_a), Some(days_b)) = (days_for.get(&game_key(a)), days_for.get(&game_key(b))) {
        let co = days_a
            .iter()
            .any(|&d| (-7..=7).any(|delta| days_b.contains(&(d + delta))));
        if co {
            best = best.max(kind_weight("co_played"));
        }
    }
    best
}

fn effective_minutes(game: &Value) -> f64 {
    let est = estimated_minutes_for_genre(&norm(game.get("genre")));
    let pt_min = nonneg(game.get("playtime_seconds")) / 60.0;
    if pt_min > 0.0 {
        (est - pt_min).max(15.0)
    } else {
        est
    }
}

fn freshness(game: &Value, days_for: &HashMap<String, HashSet<i64>>, now: Timestamp) -> f64 {
    if (nonneg(game.get("play_count")) as i64) == 0 {
        return 1.0;
    }
    let Some(days) = days_since_last_play(game, days_for, now) else {
        return 1.0;
    };
    0.3 + 0.7 * ((days as f64) / 120.0).min(1.0)
}

fn genre_daypart_fit(model: &HabitModel, game: &Value, now: Timestamp) -> f64 {
    let dt = jiff::tz::TimeZone::UTC.to_datetime(now);
    let part = daypart(dt.hour());
    let mut best: Option<f64> = None;
    for g in genres_of(game) {
        if let Some(counts) = model.genre_dayparts.get(&g) {
            let total: f64 = counts.values().sum();
            if total > 0.0 {
                let share = counts.get(part).copied().unwrap_or(0.0) / total;
                best = Some(best.map_or(share, |b| b.max(share)));
            }
        }
    }
    best.unwrap_or(0.5)
}

struct Candidate<'a> {
    game: &'a Value,
    key: String,
    score: f64,
    similarity: f64,
    similar_to: Option<&'a Value>,
    genre_aff: f64,
    platform_aff: f64,
    completion: f64,
    session_fit: f64,
    effective_minutes: f64,
}

fn score_candidates<'a>(
    games: &'a [Value],
    model: &HabitModel,
    now: Timestamp,
    exclude: &HashSet<String>,
) -> Vec<Candidate<'a>> {
    let by_key: HashMap<String, &'a Value> = games
        .iter()
        .filter(|g| g.is_object())
        .map(|g| (game_key(g), g))
        .filter(|(k, _)| !k.is_empty())
        .collect();
    let loved_games: Vec<&'a Value> = model
        .loved
        .iter()
        .filter_map(|k| by_key.get(k).copied())
        .collect();
    let top_genre = model
        .genre_seconds
        .values()
        .copied()
        .fold(0.0f64, f64::max)
        .max(1.0);
    let top_plat = model
        .platform_seconds
        .values()
        .copied()
        .fold(0.0f64, f64::max)
        .max(1.0);

    let mut out = Vec::new();
    for game in games {
        let key = game_key(game);
        if exclude.contains(&key) || !eligible(game) {
            continue;
        }
        let mut best_sim = 0.0f64;
        let mut source: Option<&'a Value> = None;
        for &loved in &loved_games {
            if game_key(loved) == key {
                continue;
            }
            let w = shared_edge(game, loved, &model.days_for);
            if w > best_sim {
                best_sim = w;
                source = Some(loved);
            }
        }
        let genres = genres_of(game);
        let genre_aff = genres
            .iter()
            .map(|g| model.genre_seconds.get(g).copied().unwrap_or(0.0) / top_genre)
            .fold(0.0f64, f64::max);
        let plat = game
            .get("platform")
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or("");
        let plat_aff = if plat.is_empty() {
            0.0
        } else {
            model.platform_seconds.get(plat).copied().unwrap_or(0.0) / top_plat
        };
        let comp = model
            .completion
            .get(&primary_genre(game))
            .copied()
            .unwrap_or(0.5);
        let minutes = effective_minutes(game);
        let session_fit = if model.median_session_min > 0.0 && minutes > 0.0 {
            (4.0 * model.median_session_min / minutes).min(1.0)
        } else {
            0.7
        };
        let dp_fit = genre_daypart_fit(model, game, now);
        let habit_fit =
            0.30 * genre_aff + 0.15 * plat_aff + 0.25 * comp + 0.20 * session_fit + 0.10 * dp_fit;
        let fresh = freshness(game, &model.days_for, now);
        let score = (0.15 + 0.85 * best_sim) * (0.25 + 0.75 * habit_fit) * (0.25 + 0.75 * fresh);
        out.push(Candidate {
            game,
            key,
            score: (score * 10_000.0).round() / 10_000.0,
            similarity: best_sim,
            similar_to: source,
            genre_aff,
            platform_aff: plat_aff,
            completion: comp,
            session_fit,
            effective_minutes: minutes,
        });
    }
    out.sort_by(|a, b| {
        b.score
            .partial_cmp(&a.score)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.key.cmp(&b.key))
    });
    out
}

fn build_reasons(cand: &Candidate<'_>, model: &HabitModel, now: Timestamp) -> Vec<Value> {
    let game = cand.game;
    let mut reasons = Vec::new();
    let median_min = model.median_session_min;
    if cand.session_fit >= 0.9
        && median_min > 0.0
        && cand.effective_minutes <= 2.0 * median_min
        && cand.effective_minutes <= 90.0
    {
        reasons.push(json!({
            "key": "radio.reason.short",
            "params": {"minutes": cand.effective_minutes.round() as i64},
        }));
    }
    let genre = primary_genre(game);
    if !genre.is_empty() && cand.genre_aff >= 0.3 {
        if cand.completion >= 0.5 && model.completion.contains_key(&genre) {
            reasons.push(json!({"key": "radio.reason.genre_finish", "params": {"genre": genre}}));
        } else {
            reasons.push(json!({"key": "radio.reason.genre_habit", "params": {"genre": genre}}));
        }
    }
    if cand.similarity >= 0.3
        && let Some(src) = cand.similar_to
    {
        reasons.push(json!({
            "key": "radio.reason.similar_to",
            "params": {
                "name": src.get("name").and_then(Value::as_str).unwrap_or(""),
                "source_id": game_key(src),
            },
        }));
    }
    let plat = game
        .get("platform")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("");
    if !plat.is_empty() && cand.platform_aff >= 0.25 {
        reasons.push(json!({"key": "radio.reason.platform_habit", "params": {"platform": plat}}));
    }
    if (nonneg(game.get("play_count")) as i64) == 0
        && let Some(days) = days_since_added(game, now)
        && days >= FRESH_UNPLAYED_DAYS
    {
        reasons.push(json!({"key": "radio.reason.fresh", "params": {"days": days}}));
    }
    if reasons.is_empty() {
        if !genre.is_empty() {
            reasons.push(json!({"key": "radio.reason.genre_habit", "params": {"genre": genre}}));
        } else {
            reasons.push(json!({
                "key": "radio.reason.fresh",
                "params": {"days": days_since_added(game, now).unwrap_or(0)},
            }));
        }
    }
    reasons.truncate(3);
    reasons
}

fn diverse<'a>(cands: Vec<Candidate<'a>>, limit: usize) -> Vec<Candidate<'a>> {
    if cands.len() <= limit {
        return cands;
    }
    let floor = cands[0].score * DIVERSITY_SCORE_FRACTION;
    let mut picked = Vec::with_capacity(limit);
    let mut seen: HashSet<(String, String)> = HashSet::new();
    let mut rest = Vec::new();
    for cand in cands {
        let sig = (
            primary_genre(cand.game),
            cand.game
                .get("platform")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned(),
        );
        if picked.len() < limit && cand.score >= floor && seen.insert(sig) {
            picked.push(cand);
        } else {
            rest.push(cand);
        }
    }
    for cand in rest {
        if picked.len() >= limit {
            break;
        }
        picked.push(cand);
    }
    picked.truncate(limit);
    picked
}

fn public_pick(cand: &Candidate<'_>, model: &HabitModel, now: Timestamp) -> Value {
    let g = cand.game;
    json!({
        "game_id": cand.key,
        "id": g.get("id").cloned().unwrap_or(Value::Null),
        "name": g.get("name").and_then(Value::as_str).unwrap_or(""),
        "platform": g.get("platform").and_then(Value::as_str).unwrap_or(""),
        "genre": g.get("genre").and_then(Value::as_str).unwrap_or(""),
        "has_cover": g.get("has_cover").and_then(Value::as_bool).unwrap_or(false),
        "cover": g.get("cover").cloned().unwrap_or_else(|| json!("")),
        "score": cand.score,
        "estimated_minutes": cand.effective_minutes.round() as i64,
        "reasons": build_reasons(cand, model, now),
    })
}

fn fallback_picks(
    games: &[Value],
    exclude: &HashSet<String>,
    now: Timestamp,
    limit: usize,
) -> Vec<Value> {
    let mut pool: Vec<&Value> = games
        .iter()
        .filter(|g| eligible(g) && !exclude.contains(&game_key(g)))
        .collect();
    pool.sort_by(|a, b| {
        let ra = nonneg(a.get("rating"));
        let rb = nonneg(b.get("rating"));
        let pa = (nonneg(a.get("play_count")) as i64) > 0;
        let pb = (nonneg(b.get("play_count")) as i64) > 0;
        let da = days_since_added(a, now).unwrap_or(-1);
        let db = days_since_added(b, now).unwrap_or(-1);
        rb.partial_cmp(&ra)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| pa.cmp(&pb))
            .then_with(|| db.cmp(&da))
            .then_with(|| game_key(a).cmp(&game_key(b)))
    });
    pool.into_iter()
        .take(limit)
        .map(|g| {
            let rating = nonneg(g.get("rating"));
            let reasons = if rating >= 4.0 {
                json!([{"key": "radio.reason.fallback_rating", "params": {"rating": rating}}])
            } else {
                json!([{"key": "radio.reason.fallback_fresh", "params": {}}])
            };
            json!({
                "game_id": game_key(g),
                "id": g.get("id").cloned().unwrap_or(Value::Null),
                "name": g.get("name").and_then(Value::as_str).unwrap_or(""),
                "platform": g.get("platform").and_then(Value::as_str).unwrap_or(""),
                "genre": g.get("genre").and_then(Value::as_str).unwrap_or(""),
                "has_cover": g.get("has_cover").and_then(Value::as_bool).unwrap_or(false),
                "cover": g.get("cover").cloned().unwrap_or_else(|| json!("")),
                "score": 0.0,
                "estimated_minutes": effective_minutes(g).round() as i64,
                "reasons": reasons,
            })
        })
        .collect()
}

pub fn build_playlist(
    games: &[Value],
    history: &[Value],
    previous_ids: &[String],
    now: Option<Timestamp>,
    limit: usize,
) -> Value {
    let now_ts = now_utc(now);
    let limit = limit.max(1);
    let exclude: HashSet<String> = previous_ids.iter().cloned().collect();
    let model = build_habit_model(games, history, Some(now_ts));
    let (picks, fallback, notice) = if model.enough {
        let cands = score_candidates(games, &model, now_ts, &exclude);
        let picks: Vec<Value> = diverse(cands, limit)
            .into_iter()
            .map(|c| public_pick(&c, &model, now_ts))
            .collect();
        let notice = if picks.is_empty() {
            "radio.notice.empty"
        } else {
            "radio.notice.ok"
        };
        (picks, false, notice)
    } else {
        let picks = fallback_picks(games, &exclude, now_ts, limit);
        let notice = if picks.is_empty() {
            "radio.notice.empty"
        } else {
            "radio.notice.not_enough_history"
        };
        (picks, true, notice)
    };
    json!({
        "name": RADIO_PLAYLIST_NAME,
        "generated_at": format_utc_iso(now_ts),
        "picks": picks,
        "fallback": fallback,
        "enough_history": model.enough,
        "notice_key": notice,
    })
}

pub fn radar(
    games: &[Value],
    history: &[Value],
    parked: &HashSet<String>,
    now: Option<Timestamp>,
) -> Value {
    let now_ts = now_utc(now);
    let model = build_habit_model(games, history, Some(now_ts));
    let finished_any = games.iter().any(|g| g.is_object() && is_completed(g));
    let enough = model.sessions >= RADAR_MIN_SESSIONS || finished_any;
    if !enough {
        return json!({"winnable": [], "park": [], "enough_history": false});
    }
    let budget = if model.completion_budget_min > 0.0 {
        model.completion_budget_min
    } else {
        (4.0 * model.median_session_min).max(360.0)
    };
    let mut winnable: Vec<(i64, String, Value)> = Vec::new();
    let mut park: Vec<(i64, String, Value)> = Vec::new();
    for game in games {
        if !game.is_object() || game.get("hidden").and_then(Value::as_bool).unwrap_or(false) {
            continue;
        }
        let key = game_key(game);
        if key.is_empty() || parked.contains(&key) || is_completed(game) {
            continue;
        }
        let observed = nonneg(game.get("playtime_seconds"));
        if observed < RADAR_MIN_OBSERVED_SECONDS {
            continue;
        }
        let Some(days) = days_since_last_play(game, &model.days_for, now_ts) else {
            continue;
        };
        if days < RADAR_STALL_DAYS {
            continue;
        }
        let est_min = estimated_minutes_for_genre(&norm(game.get("genre")));
        let invested = if est_min > 0.0 {
            observed / (est_min * 60.0)
        } else {
            0.0
        };
        let is_winnable = invested >= INVESTED_WINNABLE_RATIO || est_min <= budget;
        let deep_stall = days >= RADAR_PARK_DAYS
            && invested < PARK_MAX_INVESTED_RATIO
            && est_min > budget * PARK_MIN_ESTIMATE_FACTOR;
        let reason_key = if deep_stall || !is_winnable {
            "radio.reason.park"
        } else {
            "radio.reason.winnable"
        };
        let entry = json!({
            "game_id": key,
            "id": game.get("id").cloned().unwrap_or(Value::Null),
            "name": game.get("name").and_then(Value::as_str).unwrap_or(""),
            "platform": game.get("platform").and_then(Value::as_str).unwrap_or(""),
            "has_cover": game.get("has_cover").and_then(Value::as_bool).unwrap_or(false),
            "days_stalled": days,
            "observed_seconds": observed as i64,
            "estimated_minutes": est_min.round() as i64,
            "reasons": [{"key": reason_key, "params": {"days": days}}],
        });
        if deep_stall || !is_winnable {
            park.push((days, key, entry));
        } else {
            winnable.push((days, key, entry));
        }
    }
    winnable.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| a.1.cmp(&b.1)));
    park.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| a.1.cmp(&b.1)));
    json!({
        "winnable": winnable.into_iter().map(|(_, _, v)| v).collect::<Vec<_>>(),
        "park": park.into_iter().map(|(_, _, v)| v).collect::<Vec<_>>(),
        "enough_history": true,
    })
}

pub fn parked_ids(state: &Value) -> HashSet<String> {
    state
        .get("radio")
        .and_then(|r| r.get("parked"))
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .map(|v| {
                    v.as_str()
                        .map(str::to_owned)
                        .unwrap_or_else(|| v.to_string())
                })
                .collect()
        })
        .unwrap_or_default()
}

pub fn managed_playlist(state: &Value) -> Option<&Value> {
    state
        .get("playlists")
        .and_then(Value::as_array)?
        .iter()
        .find(|p| p.get("managed").and_then(Value::as_str) == Some("radio"))
}

pub fn playlist_stale(entry: &Value, now: Option<Timestamp>) -> bool {
    let now_ts = now_utc(now);
    let Some(generated) = parse_utc(entry.get("generated_at")) else {
        return true;
    };
    now_ts.as_second() - generated.as_second() >= RADIO_STALE_DAYS * 86_400
}

pub fn materialize_playlist(state: &mut Value, playlist: &Value, now: Option<Timestamp>) -> Value {
    let now_ts = now_utc(now);
    let picks = playlist
        .get("picks")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let members: Vec<Value> = picks
        .iter()
        .map(|p| {
            Value::String(
                p.get("game_id")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned(),
            )
        })
        .collect();
    let stored_picks: Vec<Value> = picks
        .iter()
        .map(|p| {
            json!({
                "game_id": p.get("game_id").and_then(Value::as_str).unwrap_or(""),
                "score": p.get("score").cloned().unwrap_or(json!(0.0)),
                "reasons": p.get("reasons").cloned().unwrap_or_else(|| json!([])),
            })
        })
        .collect();
    let entry = json!({
        "name": RADIO_PLAYLIST_NAME,
        "type": "manual",
        "managed": "radio",
        "generated_at": format_utc_iso(now_ts),
        "members": members,
        "picks": stored_picks,
        "fallback": playlist.get("fallback").and_then(Value::as_bool).unwrap_or(false),
        "notice_key": playlist.get("notice_key").and_then(Value::as_str).unwrap_or("radio.notice.ok"),
        "rules": {},
    });
    if let Some(root) = state.as_object_mut() {
        let pls = root
            .entry("playlists".to_owned())
            .or_insert_with(|| Value::Array(Vec::new()));
        if !pls.is_array() {
            *pls = Value::Array(Vec::new());
        }
        if let Some(arr) = pls.as_array_mut() {
            for slot in arr.iter_mut() {
                if slot.get("managed").and_then(Value::as_str) == Some("radio") {
                    *slot = entry.clone();
                    return entry;
                }
            }
            arr.push(entry.clone());
        }
    }
    entry
}

pub fn entry_payload(entry: &Value, state: &Value) -> Value {
    let games = state
        .get("games")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let by_key: HashMap<String, &Value> = games
        .iter()
        .filter(|g| g.is_object())
        .map(|g| (game_key(g), g))
        .filter(|(k, _)| !k.is_empty())
        .collect();
    let mut picks = Vec::new();
    if let Some(arr) = entry.get("picks").and_then(Value::as_array) {
        for stored in arr {
            let gid = stored.get("game_id").and_then(Value::as_str).unwrap_or("");
            let Some(&game) = by_key.get(gid) else {
                continue;
            };
            picks.push(json!({
                "game_id": gid,
                "id": game.get("id").cloned().unwrap_or(Value::Null),
                "name": game.get("name").and_then(Value::as_str).unwrap_or(""),
                "platform": game.get("platform").and_then(Value::as_str).unwrap_or(""),
                "genre": game.get("genre").and_then(Value::as_str).unwrap_or(""),
                "has_cover": game.get("has_cover").and_then(Value::as_bool).unwrap_or(false),
                "cover": game.get("cover").cloned().unwrap_or_else(|| json!("")),
                "score": stored.get("score").cloned().unwrap_or(json!(0.0)),
                "estimated_minutes": effective_minutes(game).round() as i64,
                "reasons": stored.get("reasons").cloned().unwrap_or_else(|| json!([])),
            }));
        }
    }
    let fallback = entry
        .get("fallback")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    json!({
        "name": entry.get("name").and_then(Value::as_str).unwrap_or(RADIO_PLAYLIST_NAME),
        "generated_at": entry.get("generated_at").and_then(Value::as_str).unwrap_or(""),
        "stale": playlist_stale(entry, None),
        "picks": picks,
        "fallback": fallback,
        "enough_history": !fallback,
        "notice_key": entry.get("notice_key").and_then(Value::as_str).unwrap_or("radio.notice.ok"),
        "managed": true,
    })
}

pub fn radio_payload(state: &mut Value, now: Option<Timestamp>, force: bool) -> Value {
    let now_ts = now_utc(now);
    let has_games = state
        .get("games")
        .and_then(Value::as_array)
        .is_some_and(|a| !a.is_empty());
    let prev: Vec<String> = if let Some(entry) = managed_playlist(state) {
        if !force && !playlist_stale(entry, Some(now_ts)) {
            let payload = entry_payload(entry, state);
            if payload
                .get("picks")
                .and_then(Value::as_array)
                .is_some_and(|a| !a.is_empty())
                || !has_games
            {
                return payload;
            }
        }
        entry
            .get("members")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .map(str::to_owned)
                    .collect()
            })
            .unwrap_or_default()
    } else {
        Vec::new()
    };
    let games = state
        .get("games")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let history = state
        .get("history")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let playlist = build_playlist(&games, &history, &prev, Some(now_ts), RADIO_PICK_LIMIT);
    let entry = materialize_playlist(state, &playlist, Some(now_ts));
    entry_payload(&entry, state)
}

pub fn park_game(state: &mut Value, target_id: &str) -> Option<Value> {
    let target = target_id.trim();
    if target.is_empty() {
        return None;
    }
    let mut found_key: Option<String> = None;
    let mut found_name = String::new();
    let mut already_done = false;
    if let Some(games) = state.get_mut("games").and_then(Value::as_array_mut) {
        for game in games.iter_mut() {
            if !game.is_object() {
                continue;
            }
            let key = game_key(game);
            let id_str = match game.get("id") {
                Some(Value::String(s)) => s.clone(),
                Some(Value::Number(n)) => n.to_string(),
                _ => String::new(),
            };
            if target != key && target != id_str {
                continue;
            }
            if is_completed(game) {
                found_key = Some(key);
                already_done = true;
                break;
            }
            if let Some(obj) = game.as_object_mut() {
                obj.insert("progress".into(), Value::String("Paused".to_owned()));
                found_name = obj
                    .get("name")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned();
            }
            found_key = Some(key);
            break;
        }
    }
    let key = found_key?;
    if already_done {
        return Some(json!({"ok": false, "code": "already_completed", "game_id": key}));
    }
    if let Some(root) = state.as_object_mut() {
        let radio = root
            .entry("radio".to_owned())
            .or_insert_with(|| Value::Object(Map::new()));
        if !radio.is_object() {
            *radio = Value::Object(Map::new());
        }
        if let Some(rmap) = radio.as_object_mut() {
            let parked = rmap
                .entry("parked".to_owned())
                .or_insert_with(|| Value::Array(Vec::new()));
            if let Some(arr) = parked.as_array_mut()
                && !arr.iter().any(|x| x.as_str() == Some(key.as_str()))
            {
                arr.push(Value::String(key.clone()));
            }
        }
    }
    Some(json!({
        "ok": true,
        "game_id": key,
        "name": found_name,
        "progress": "Paused",
    }))
}
