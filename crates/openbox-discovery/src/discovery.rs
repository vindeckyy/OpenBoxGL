//! Discovery Center lists and richer related-game scoring (`pkg/parity/parity_discovery.py`).

use std::collections::HashSet;
use std::sync::LazyLock;
use std::time::{Duration, Instant};

use jiff::Timestamp;
use parking_lot::Mutex;
use rand::seq::SliceRandom;
use regex::Regex;
use serde_json::{Value, json};

static WORD_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\w+").expect("valid word regex"));

type DiscoverySig = (usize, Vec<(String, String, bool)>, usize);

struct DiscoveryCache {
    at: Option<Instant>,
    signature: Option<DiscoverySig>,
    result: Option<Value>,
}

static DISCOVERY_CACHE: LazyLock<Mutex<DiscoveryCache>> = LazyLock::new(|| {
    Mutex::new(DiscoveryCache {
        at: None,
        signature: None,
        result: None,
    })
});

const DISCOVERY_TTL: Duration = Duration::from_secs(30);

pub fn clear_discovery_cache() {
    let mut cache = DISCOVERY_CACHE.lock();
    cache.at = None;
    cache.signature = None;
    cache.result = None;
}

fn val_str(v: Option<&Value>) -> String {
    match v {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) => n.to_string(),
        Some(Value::Bool(b)) => {
            if *b {
                "True".to_owned()
            } else {
                "False".to_owned()
            }
        }
        Some(other) => other.to_string(),
    }
}

fn number(v: Option<&Value>) -> f64 {
    match v {
        Some(Value::Number(n)) => n.as_f64().unwrap_or(0.0),
        Some(Value::String(s)) => s.trim().parse::<f64>().unwrap_or(0.0),
        _ => 0.0,
    }
}

pub fn item_rating(game: &Value) -> f64 {
    number(game.get("rating"))
}

fn has_any_achievements(game: &Value) -> bool {
    if game
        .get("has_achievements")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        return true;
    }
    game.get("achievement_sources")
        .and_then(Value::as_object)
        .is_some_and(|sources| {
            sources.values().any(|entry| {
                entry.as_object().is_some_and(|e| {
                    e.get("total").and_then(Value::as_i64).unwrap_or(0) > 0
                        || e.get("earned").and_then(Value::as_i64).unwrap_or(0) > 0
                })
            })
        })
}

fn now_iso_seconds() -> String {
    let now = Timestamp::now();
    let dt = jiff::tz::TimeZone::system().to_datetime(now);
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}",
        dt.year(),
        dt.month(),
        dt.day(),
        dt.hour(),
        dt.minute(),
        dt.second()
    )
}

pub fn discovery_lists(games: &[Value], limit: usize) -> Value {
    let mut sample = Vec::new();
    if !games.is_empty() {
        let indices = [0, games.len() / 2, games.len() - 1];
        for idx in indices {
            let g = &games[idx];
            sample.push((
                val_str(g.get("game_id")),
                val_str(g.get("rating")),
                g.get("favorite").and_then(Value::as_bool).unwrap_or(false),
            ));
        }
    }
    let sig: DiscoverySig = (games.len(), sample, limit);
    let now = Instant::now();
    {
        let cache = DISCOVERY_CACHE.lock();
        if cache.signature.as_ref() == Some(&sig)
            && cache
                .at
                .is_some_and(|t| now.saturating_duration_since(t) < DISCOVERY_TTL)
            && let Some(res) = &cache.result
        {
            return res.clone();
        }
    }
    let res = discovery_lists_uncached(games, limit);
    let mut cache = DISCOVERY_CACHE.lock();
    cache.at = Some(now);
    cache.signature = Some(sig);
    cache.result = Some(res.clone());
    res
}

fn discovery_lists_uncached(games: &[Value], limit: usize) -> Value {
    let visible: Vec<(usize, &Value)> = games
        .iter()
        .enumerate()
        .filter(|(_, g)| !g.get("hidden").and_then(Value::as_bool).unwrap_or(false))
        .collect();

    let take = |rows: &[(usize, &Value)]| -> Vec<usize> {
        rows.iter().take(limit).map(|(idx, _)| *idx).collect()
    };

    let mut recently_added = visible.clone();
    recently_added.sort_by_key(|a| std::cmp::Reverse(val_str(a.1.get("added_at"))));

    let never_played: Vec<(usize, &Value)> = visible
        .iter()
        .copied()
        .filter(|(_, g)| g.get("play_count").and_then(Value::as_i64).unwrap_or(0) == 0)
        .collect();

    let mut short_sessions: Vec<(usize, &Value)> = visible
        .iter()
        .copied()
        .filter(|(_, g)| {
            g.get("playtime_seconds")
                .and_then(Value::as_i64)
                .unwrap_or(0)
                > 0
        })
        .collect();
    short_sessions.sort_by_key(|(_, g)| {
        g.get("playtime_seconds")
            .and_then(Value::as_i64)
            .unwrap_or(0)
    });

    let mut highly_rated: Vec<(usize, &Value)> = visible
        .iter()
        .copied()
        .filter(|(_, g)| item_rating(g) >= 4.0)
        .collect();
    highly_rated.sort_by(|a, b| {
        item_rating(b.1)
            .partial_cmp(&item_rating(a.1))
            .unwrap_or(std::cmp::Ordering::Equal)
    });

    let mut continue_playing: Vec<(usize, &Value)> = visible
        .iter()
        .copied()
        .filter(|(_, g)| {
            g.get("progress").and_then(Value::as_str) == Some("Playing")
                || !val_str(g.get("last_played")).is_empty()
        })
        .collect();
    continue_playing.sort_by_key(|a| std::cmp::Reverse(val_str(a.1.get("last_played"))));

    let mut achievements: Vec<(usize, &Value)> = visible
        .iter()
        .copied()
        .filter(|(_, g)| has_any_achievements(g))
        .collect();
    achievements.sort_by(|a, b| {
        number(b.1.get("ra_progress_pct"))
            .partial_cmp(&number(a.1.get("ra_progress_pct")))
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| {
                val_str(a.1.get("name"))
                    .to_lowercase()
                    .cmp(&val_str(b.1.get("name")).to_lowercase())
            })
    });

    let mut random_pool = visible;
    let mut rng = rand::thread_rng();
    random_pool.shuffle(&mut rng);

    json!({
        "recently_added": take(&recently_added),
        "never_played": take(&never_played),
        "short_sessions": take(&short_sessions),
        "highly_rated": take(&highly_rated),
        "continue_playing": take(&continue_playing),
        "achievements": take(&achievements),
        "random_picks": take(&random_pool),
        "generated_at": now_iso_seconds(),
    })
}

pub fn related_with_reasons(games: &[Value], selected: usize, limit: usize) -> Vec<Value> {
    let Some(base) = games.get(selected) else {
        return Vec::new();
    };
    let base_genre_str = val_str(base.get("genre")).to_lowercase();
    let base_genres: HashSet<&str> = WORD_RE
        .find_iter(&base_genre_str)
        .map(|m| m.as_str())
        .collect();
    let base_series = val_str(base.get("series"));
    let base_collection = val_str(base.get("collection"));
    let base_dev = val_str(base.get("developer"));
    let base_plat = val_str(base.get("platform"));
    let base_pub = val_str(base.get("publisher"));

    let mut ranked: Vec<(i64, String, usize, Vec<&'static str>, i64)> = Vec::new();
    for (index, game) in games.iter().enumerate() {
        if index == selected || game.get("hidden").and_then(Value::as_bool).unwrap_or(false) {
            continue;
        }
        let mut reasons: Vec<&'static str> = Vec::new();
        let mut score: i64 = 0;
        let g_genre_str = val_str(game.get("genre")).to_lowercase();
        let g_genres: HashSet<&str> = WORD_RE
            .find_iter(&g_genre_str)
            .map(|m| m.as_str())
            .collect();
        let shared = base_genres.intersection(&g_genres).count() as i64;
        if shared > 0 {
            score += 2 * shared;
            reasons.push("shared genre");
        }
        if !base_series.is_empty() && base_series == val_str(game.get("series")) {
            score += 8;
            reasons.push("same series");
        }
        if !base_collection.is_empty() && base_collection == val_str(game.get("collection")) {
            score += 5;
            reasons.push("same collection");
        }
        if !base_dev.is_empty() && base_dev == val_str(game.get("developer")) {
            score += 3;
            reasons.push("same developer");
        }
        if !base_plat.is_empty() && base_plat == val_str(game.get("platform")) {
            score += 2;
            reasons.push("same platform");
        }
        if !base_pub.is_empty() && base_pub == val_str(game.get("publisher")) {
            score += 1;
            reasons.push("same publisher");
        }
        if score > 0 {
            let sort_name = game
                .get("sort_title")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .or_else(|| game.get("name").and_then(Value::as_str))
                .unwrap_or("")
                .to_lowercase();
            ranked.push((-score, sort_name, index, reasons, score));
        }
    }
    ranked.sort_by(|a, b| a.0.cmp(&b.0).then_with(|| a.1.cmp(&b.1)));
    ranked
        .into_iter()
        .take(limit)
        .map(|(_, _, id, reasons, score)| json!({"id": id, "score": score, "reasons": reasons}))
        .collect()
}
