//! Launcher-level trophies (`pkg/parity/parity_trophies.py`).

use std::collections::{BTreeSet, HashMap, HashSet};

use jiff::Timestamp;
use jiff::civil::{Date, DateTime};
use serde_json::{Map, Value, json};

pub const STATE_KEY: &str = "trophies";
pub const AWARDS_KEY: &str = "awarded";
pub const NIGHT_OWL_LAST_HOUR: i8 = 4;
pub const OLD_SCHOOL_YEAR_LIMIT: i64 = 1990;
pub const MIN_VALID_YEAR: i64 = 1950;
pub const MARATHON_SECONDS: i64 = 4 * 3600;
pub const DEEP_DIVER_SECONDS: i64 = 20 * 3600;
pub const CENTURY_CLUB_SECONDS: i64 = 100 * 3600;
const FINISHED_MARKERS: &[&str] = &["beaten", "completed", "mastered"];

pub struct TrophyRule {
    pub id: &'static str,
    pub stat: &'static str,
    pub target: i64,
}

pub const TROPHY_RULES: &[TrophyRule] = &[
    TrophyRule {
        id: "century_club",
        stat: "total_playtime",
        target: CENTURY_CLUB_SECONDS,
    },
    TrophyRule {
        id: "collector",
        stat: "games",
        target: 100,
    },
    TrophyRule {
        id: "completionist",
        stat: "finished_games",
        target: 5,
    },
    TrophyRule {
        id: "curator",
        stat: "cover_games",
        target: 25,
    },
    TrophyRule {
        id: "decade_tourist",
        stat: "decades_played",
        target: 4,
    },
    TrophyRule {
        id: "deep_diver",
        stat: "max_game_playtime",
        target: DEEP_DIVER_SECONDS,
    },
    TrophyRule {
        id: "first_launch",
        stat: "sessions",
        target: 1,
    },
    TrophyRule {
        id: "marathon",
        stat: "max_session_seconds",
        target: MARATHON_SECONDS,
    },
    TrophyRule {
        id: "night_owl",
        stat: "night_sessions",
        target: 1,
    },
    TrophyRule {
        id: "old_school",
        stat: "pre_limit_played",
        target: 1,
    },
    TrophyRule {
        id: "platform_hopper",
        stat: "platforms_played",
        target: 4,
    },
    TrophyRule {
        id: "week_streak",
        stat: "longest_streak",
        target: 7,
    },
];

fn int_nonneg(v: Option<&Value>) -> i64 {
    match v {
        Some(Value::Number(n)) => n
            .as_i64()
            .or_else(|| n.as_f64().map(|f| f as i64))
            .unwrap_or(0)
            .max(0),
        Some(Value::String(s)) => s.trim().parse::<i64>().unwrap_or(0).max(0),
        _ => 0,
    }
}

fn parse_started(value: Option<&Value>) -> Option<DateTime> {
    let text = value.and_then(Value::as_str)?.trim();
    if text.is_empty() || (!text.contains('T') && !text.contains(' ')) {
        return None;
    }
    let norm = text.replacen(' ', "T", 1);
    if let Ok(ts) = norm.parse::<Timestamp>() {
        return Some(jiff::tz::TimeZone::UTC.to_datetime(ts));
    }
    norm.parse::<DateTime>().ok()
}

fn parse_day(value: Option<&Value>) -> Option<Date> {
    if let Some(dt) = parse_started(value) {
        return Some(dt.date());
    }
    let text = value.and_then(Value::as_str)?.trim();
    if text.len() >= 10 {
        return text[..10].parse::<Date>().ok();
    }
    None
}

fn longest_day_run(days: &BTreeSet<Date>) -> i64 {
    let mut longest = 0i64;
    let mut run = 0i64;
    let mut prev: Option<Date> = None;
    for &day in days {
        let consecutive = prev.is_some_and(|p| p.tomorrow().ok() == Some(day));
        run = if consecutive { run + 1 } else { 1 };
        if run > longest {
            longest = run;
        }
        prev = Some(day);
    }
    longest
}

pub fn collect_stats(games: &[Value], history: &[Value]) -> HashMap<&'static str, i64> {
    let mut stats: HashMap<&'static str, i64> = HashMap::from([
        ("games", 0),
        ("cover_games", 0),
        ("finished_games", 0),
        ("decades_played", 0),
        ("platforms_played", 0),
        ("genres_played", 0),
        ("pre_limit_played", 0),
        ("max_game_playtime", 0),
        ("total_playtime", 0),
        ("sessions", 0),
        ("max_session_seconds", 0),
        ("night_sessions", 0),
        ("longest_streak", 0),
    ]);

    let mut history_ids: HashSet<String> = HashSet::new();
    let mut played_days: BTreeSet<Date> = BTreeSet::new();
    for entry in history {
        if !entry.is_object() {
            continue;
        }
        *stats.get_mut("sessions").unwrap() += 1;
        if let Some(gid) = entry
            .get("game_id")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
        {
            history_ids.insert(gid.to_owned());
        }
        let secs = int_nonneg(entry.get("seconds"));
        let max_s = stats.get_mut("max_session_seconds").unwrap();
        if secs > *max_s {
            *max_s = secs;
        }
        if let Some(started) = parse_started(entry.get("started")) {
            played_days.insert(started.date());
            if started.hour() <= NIGHT_OWL_LAST_HOUR {
                *stats.get_mut("night_sessions").unwrap() += 1;
            }
        } else if let Some(day) = parse_day(entry.get("started")) {
            played_days.insert(day);
        }
    }
    stats.insert("longest_streak", longest_day_run(&played_days));

    let mut decades: HashSet<i64> = HashSet::new();
    let mut platforms: HashSet<String> = HashSet::new();
    let mut genres: HashSet<String> = HashSet::new();
    for game in games {
        if !game.is_object() {
            continue;
        }
        *stats.get_mut("games").unwrap() += 1;
        let playtime = int_nonneg(game.get("playtime_seconds"));
        *stats.get_mut("total_playtime").unwrap() += playtime;
        let max_g = stats.get_mut("max_game_playtime").unwrap();
        if playtime > *max_g {
            *max_g = playtime;
        }
        if game
            .get("has_cover")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            *stats.get_mut("cover_games").unwrap() += 1;
        }
        let prog = game
            .get("progress")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_lowercase();
        if FINISHED_MARKERS.iter().any(|m| prog.contains(m)) {
            *stats.get_mut("finished_games").unwrap() += 1;
        }
        let gid = game
            .get("game_id")
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or("");
        let played = playtime > 0
            || int_nonneg(game.get("play_count")) > 0
            || game
                .get("last_played")
                .and_then(Value::as_str)
                .is_some_and(|s| !s.trim().is_empty())
            || (!gid.is_empty() && history_ids.contains(gid));
        if !played {
            continue;
        }
        let year = int_nonneg(game.get("year"));
        if year >= MIN_VALID_YEAR {
            decades.insert(year / 10);
            if year < OLD_SCHOOL_YEAR_LIMIT {
                *stats.get_mut("pre_limit_played").unwrap() += 1;
            }
        }
        let plat = game
            .get("platform")
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or("");
        if !plat.is_empty() {
            platforms.insert(plat.to_owned());
        }
        for part in game
            .get("genre")
            .and_then(Value::as_str)
            .unwrap_or("")
            .split(',')
        {
            let label = part.trim();
            if !label.is_empty() {
                genres.insert(label.to_owned());
            }
        }
    }
    stats.insert("decades_played", decades.len() as i64);
    stats.insert("platforms_played", platforms.len() as i64);
    stats.insert("genres_played", genres.len() as i64);
    stats
}

pub fn evaluate(games: &[Value], history: &[Value]) -> Vec<Value> {
    let stats = collect_stats(games, history);
    TROPHY_RULES
        .iter()
        .map(|rule| {
            let cur = stats.get(rule.stat).copied().unwrap_or(0);
            json!({
                "id": rule.id,
                "awarded": cur >= rule.target,
                "progress": {"current": cur.min(rule.target), "target": rule.target},
            })
        })
        .collect()
}

fn stored_awards(state: &Value) -> HashMap<String, String> {
    let valid_ids: HashSet<&str> = TROPHY_RULES.iter().map(|r| r.id).collect();
    state
        .get(STATE_KEY)
        .and_then(|b| b.get(AWARDS_KEY))
        .and_then(Value::as_object)
        .map(|m| {
            m.iter()
                .filter(|(k, v)| {
                    valid_ids.contains(k.as_str()) && v.as_str().is_some_and(|s| !s.is_empty())
                })
                .map(|(k, v)| (k.clone(), v.as_str().unwrap_or("").to_owned()))
                .collect()
        })
        .unwrap_or_default()
}

pub fn trophy_case(state: &Value) -> Value {
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
    let evaluated = evaluate(games, history);
    let stored = stored_awards(state);
    let mut trophies = Vec::with_capacity(evaluated.len());
    let mut earned = 0usize;
    for entry in evaluated {
        let id = entry.get("id").and_then(Value::as_str).unwrap_or("");
        let awarded_at = stored.get(id).cloned();
        let awarded = entry
            .get("awarded")
            .and_then(Value::as_bool)
            .unwrap_or(false)
            || awarded_at.is_some();
        if awarded {
            earned += 1;
        }
        trophies.push(json!({
            "id": id,
            "awarded": awarded,
            "awarded_at": awarded_at,
            "progress": entry["progress"],
        }));
    }
    json!({
        "trophies": trophies,
        "earned": earned,
        "total": TROPHY_RULES.len(),
    })
}

pub fn unrecorded_awards(state: &Value) -> Vec<String> {
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
    let stored = stored_awards(state);
    evaluate(games, history)
        .into_iter()
        .filter_map(|entry| {
            let id = entry.get("id")?.as_str()?;
            if entry
                .get("awarded")
                .and_then(Value::as_bool)
                .unwrap_or(false)
                && !stored.contains_key(id)
            {
                Some(id.to_owned())
            } else {
                None
            }
        })
        .collect()
}

fn now_local_iso() -> String {
    let dt = jiff::tz::TimeZone::system().to_datetime(Timestamp::now());
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

pub fn record_new_awards(state: &mut Value, now: Option<&str>) -> Vec<Value> {
    let stamp = now.map(str::to_owned).unwrap_or_else(now_local_iso);
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
    let evaluated = evaluate(&games, &history);
    let stored = stored_awards(state);
    let mut newly = Vec::new();
    for entry in evaluated {
        let id = entry.get("id").and_then(Value::as_str).unwrap_or("");
        if entry
            .get("awarded")
            .and_then(Value::as_bool)
            .unwrap_or(false)
            && !stored.contains_key(id)
        {
            newly.push(json!({"id": id, "awarded_at": stamp}));
        }
    }
    if !newly.is_empty()
        && let Some(root) = state.as_object_mut()
    {
        let bucket = root
            .entry(STATE_KEY.to_owned())
            .or_insert_with(|| Value::Object(Map::new()));
        if !bucket.is_object() {
            *bucket = Value::Object(Map::new());
        }
        if let Some(bmap) = bucket.as_object_mut() {
            let awarded = bmap
                .entry(AWARDS_KEY.to_owned())
                .or_insert_with(|| Value::Object(Map::new()));
            if !awarded.is_object() {
                *awarded = Value::Object(Map::new());
            }
            if let Some(amap) = awarded.as_object_mut() {
                for item in &newly {
                    if let (Some(id), Some(at)) = (
                        item.get("id").and_then(Value::as_str),
                        item.get("awarded_at").and_then(Value::as_str),
                    ) {
                        amap.insert(id.to_owned(), Value::String(at.to_owned()));
                    }
                }
            }
        }
    }
    newly
}
