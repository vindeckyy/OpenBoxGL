//! Play Insights aggregation (`pkg/parity/parity_insights.py`).
//!
//! Heatmap, streaks, totals, top platforms/genres/games, momentum, spend,
//! mastery breakdown, annual wrapped summary, and grouped history timeline.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::path::Path;

use jiff::civil::{Date, DateTime};
use jiff::{Timestamp, ToSpan};
use openbox_imports::catalog::total_playtime_seconds;
use serde_json::{Map, Value, json};

fn basename(value: Option<&Value>) -> String {
    let raw = value.and_then(Value::as_str).unwrap_or("");
    if raw.is_empty() {
        return String::new();
    }
    Path::new(raw)
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default()
}

pub fn parse_local_date(value: &str) -> Option<Date> {
    let text = value.trim();
    if text.is_empty() {
        return None;
    }
    if text.contains('T') {
        if let Ok(ts) = text.parse::<Timestamp>() {
            return Some(jiff::tz::TimeZone::system().to_datetime(ts).date());
        }
        if let Ok(dt) = text.parse::<DateTime>() {
            return Some(dt.date());
        }
        return None;
    }
    if text.len() >= 10 {
        return text[..10].parse::<Date>().ok();
    }
    None
}

fn today_local() -> Date {
    jiff::tz::TimeZone::system()
        .to_datetime(Timestamp::now())
        .date()
}

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

fn history_daily_buckets(history: &[Value]) -> HashMap<Date, (usize, i64)> {
    let mut buckets: HashMap<Date, (usize, i64)> = HashMap::new();
    for entry in history {
        if !entry.is_object() {
            continue;
        }
        let started = entry
            .get("started")
            .or_else(|| entry.get("date"))
            .and_then(Value::as_str)
            .unwrap_or("");
        let Some(date) = parse_local_date(started) else {
            continue;
        };
        let seconds = int_nonneg(entry.get("seconds"));
        let slot = buckets.entry(date).or_insert((0, 0));
        slot.0 += 1;
        slot.1 += seconds;
    }
    buckets
}

fn level_for_seconds(seconds: i64, sorted_counts: &[i64]) -> i64 {
    if seconds <= 0 {
        return 0;
    }
    if sorted_counts.is_empty() {
        return 1;
    }
    let n = sorted_counts.len();
    if n < 4 {
        return if seconds < 600 {
            1
        } else if seconds < 1800 {
            2
        } else if seconds < 3600 {
            3
        } else {
            4
        };
    }
    let q1 = sorted_counts[n / 4].max(600);
    let q2 = sorted_counts[n / 2].max(1800);
    let q3 = sorted_counts[3 * n / 4].max(3600);
    if seconds < q1 {
        1
    } else if seconds < q2 {
        2
    } else if seconds < q3 {
        3
    } else {
        4
    }
}

pub fn compute_heatmap(history: &[Value], days: usize, end_date: Option<Date>) -> Vec<Value> {
    let end = end_date.unwrap_or_else(today_local);
    let days = days.max(1);
    let buckets = history_daily_buckets(history);
    let mut non_zero: Vec<i64> = buckets
        .values()
        .map(|(_, s)| *s)
        .filter(|&s| s > 0)
        .collect();
    non_zero.sort_unstable();
    let start = end.checked_sub(((days - 1) as i64).days()).unwrap_or(end);
    let mut cells = Vec::with_capacity(days);
    let mut cur = start;
    while cur <= end {
        let (count, seconds) = buckets.get(&cur).copied().unwrap_or((0, 0));
        let level = level_for_seconds(seconds, &non_zero);
        cells.push(json!({
            "date": cur.to_string(),
            "count": count,
            "seconds": seconds,
            "level": level,
        }));
        match cur.tomorrow() {
            Ok(next) => cur = next,
            Err(_) => break,
        }
    }
    cells
}

pub fn compute_streak(heatmap: &[Value]) -> Value {
    let mut longest = 0i64;
    let mut running = 0i64;
    let mut last_played = String::new();
    for cell in heatmap {
        if cell.get("count").and_then(Value::as_i64).unwrap_or(0) > 0 {
            running += 1;
            last_played = cell
                .get("date")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            if running > longest {
                longest = running;
            }
        } else {
            running = 0;
        }
    }
    let mut trailing = 0i64;
    for cell in heatmap.iter().rev() {
        if cell.get("count").and_then(Value::as_i64).unwrap_or(0) > 0 {
            trailing += 1;
        } else {
            break;
        }
    }
    json!({
        "current": trailing,
        "longest": longest,
        "last_played": last_played,
    })
}

pub fn compute_totals(games: &[Value], history: &[Value]) -> Value {
    let mut played = 0usize;
    let mut total_playtime = 0i64;
    for game in games {
        if !game.is_object() {
            continue;
        }
        let pt = total_playtime_seconds(game).max(0);
        total_playtime += pt;
        let has_lp = game
            .get("last_played")
            .and_then(Value::as_str)
            .is_some_and(|s| !s.is_empty());
        if pt > 0 || has_lp {
            played += 1;
        }
    }
    let total_sessions = history.iter().filter(|h| h.is_object()).count();
    json!({
        "games": games.len(),
        "played": played,
        "total_playtime_seconds": total_playtime,
        "total_sessions": total_sessions,
    })
}

pub fn compute_top_platforms(games: &[Value], limit: usize) -> Vec<Value> {
    let mut counts: HashMap<String, usize> = HashMap::new();
    let mut playtime: HashMap<String, i64> = HashMap::new();
    for game in games {
        if !game.is_object() {
            continue;
        }
        let raw = game
            .get("platform")
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or("");
        let plat = if raw.is_empty() {
            "Unspecified".to_owned()
        } else {
            raw.to_owned()
        };
        *counts.entry(plat.clone()).or_insert(0) += 1;
        *playtime.entry(plat).or_insert(0) += total_playtime_seconds(game).max(0);
    }
    let mut items: Vec<(String, usize, i64)> = counts
        .into_iter()
        .map(|(p, c)| {
            let pt = playtime.get(&p).copied().unwrap_or(0);
            (p, c, pt)
        })
        .collect();
    items.sort_by(|a, b| {
        b.1.cmp(&a.1)
            .then_with(|| b.2.cmp(&a.2))
            .then_with(|| a.0.cmp(&b.0))
    });
    items
        .into_iter()
        .take(limit)
        .map(|(p, c, pt)| json!({"platform": p, "count": c, "playtime_seconds": pt}))
        .collect()
}

pub fn compute_top_genres(games: &[Value], limit: usize) -> Vec<Value> {
    let mut counts: HashMap<String, usize> = HashMap::new();
    for game in games {
        if !game.is_object() {
            continue;
        }
        let raw = game
            .get("genre")
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or("");
        if raw.is_empty() {
            continue;
        }
        for part in raw.split(',') {
            let label = part.trim();
            if !label.is_empty() {
                *counts.entry(label.to_owned()).or_insert(0) += 1;
            }
        }
    }
    let mut items: Vec<(String, usize)> = counts.into_iter().collect();
    items.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
    items
        .into_iter()
        .take(limit)
        .map(|(g, c)| json!({"genre": g, "count": c}))
        .collect()
}

pub fn compute_top_games(games: &[Value], limit: usize) -> Vec<Value> {
    let mut items: Vec<(i64, i64, String, Value)> = Vec::new();
    for game in games {
        if !game.is_object() {
            continue;
        }
        let pt = total_playtime_seconds(game).max(0);
        if pt <= 0 {
            continue;
        }
        let plays = int_nonneg(game.get("play_count"));
        let name = game
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        let row = json!({
            "game_id": game.get("game_id").and_then(Value::as_str).unwrap_or(""),
            "name": name,
            "platform": game.get("platform").and_then(Value::as_str).unwrap_or(""),
            "playtime_seconds": pt,
            "play_count": plays,
            "last_played": game.get("last_played").and_then(Value::as_str).unwrap_or(""),
        });
        items.push((pt, plays, name, row));
    }
    items.sort_by(|a, b| {
        b.0.cmp(&a.0)
            .then_with(|| b.1.cmp(&a.1))
            .then_with(|| a.2.cmp(&b.2))
    });
    items
        .into_iter()
        .take(limit)
        .map(|(_, _, _, r)| r)
        .collect()
}

pub fn compute_momentum(heatmap: &[Value]) -> Value {
    let (last_30, prev_30) = if heatmap.len() < 60 {
        let start = heatmap.len().saturating_sub(30);
        (&heatmap[start..], &heatmap[0..0])
    } else {
        (
            &heatmap[heatmap.len() - 30..],
            &heatmap[heatmap.len() - 60..heatmap.len() - 30],
        )
    };
    let last_secs: i64 = last_30
        .iter()
        .map(|c| c.get("seconds").and_then(Value::as_i64).unwrap_or(0))
        .sum();
    let prev_secs: i64 = prev_30
        .iter()
        .map(|c| c.get("seconds").and_then(Value::as_i64).unwrap_or(0))
        .sum();
    json!({
        "last_30_days_seconds": last_secs,
        "previous_30_days_seconds": prev_secs,
        "delta_seconds": last_secs - prev_secs,
    })
}

pub fn compute_spend(games: &[Value]) -> Value {
    struct Bucket {
        total_cents: i64,
        games: usize,
        seconds: i64,
    }
    let mut totals: BTreeMap<String, Bucket> = BTreeMap::new();
    let mut ranked: Vec<(i64, String, Value)> = Vec::new();
    for game in games {
        if !game.is_object() {
            continue;
        }
        let Some(cents) = game.get("price_paid_cents").and_then(|v| {
            if v.is_boolean() {
                None
            } else {
                v.as_i64().filter(|&n| n >= 0)
            }
        }) else {
            continue;
        };
        let raw_cur = game
            .get("price_currency")
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or("")
            .to_uppercase();
        let currency = if raw_cur.len() == 3 && raw_cur.bytes().all(|b| b.is_ascii_alphabetic()) {
            raw_cur
        } else {
            "USD".to_owned()
        };
        let seconds = total_playtime_seconds(game).max(0);
        let b = totals.entry(currency.clone()).or_insert(Bucket {
            total_cents: 0,
            games: 0,
            seconds: 0,
        });
        b.total_cents += cents;
        b.games += 1;
        b.seconds += seconds;
        if cents > 0 && seconds >= 60 {
            let rate = ((cents as f64) / (seconds as f64 / 3600.0)).round() as i64;
            let name = game.get("name").and_then(Value::as_str).unwrap_or("");
            ranked.push((
                rate,
                name.to_lowercase(),
                json!({
                    "game_id": game.get("game_id").and_then(Value::as_str).unwrap_or(""),
                    "name": name,
                    "currency": currency,
                    "cost_per_hour_cents": rate,
                }),
            ));
        }
    }
    let currencies: Vec<Value> = totals
        .into_iter()
        .map(|(currency, b)| {
            let rate = if b.total_cents > 0 && b.seconds >= 60 {
                json!(((b.total_cents as f64) / (b.seconds as f64 / 3600.0)).round() as i64)
            } else {
                Value::Null
            };
            json!({
                "currency": currency,
                "total_cents": b.total_cents,
                "games": b.games,
                "seconds": b.seconds,
                "cost_per_hour_cents": rate,
            })
        })
        .collect();
    ranked.sort_by(|a, b| a.0.cmp(&b.0).then_with(|| a.1.cmp(&b.1)));
    let best_value: Vec<Value> = ranked.iter().take(5).map(|(_, _, v)| v.clone()).collect();
    let worst_start = ranked.len().saturating_sub(5);
    let worst_value: Vec<Value> = ranked[worst_start..]
        .iter()
        .rev()
        .map(|(_, _, v)| v.clone())
        .collect();
    json!({
        "currencies": currencies,
        "best_value": best_value,
        "worst_value": worst_value,
    })
}

pub fn summarize(state: &Value, end_date: Option<Date>, days: usize) -> Value {
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
    let days = if (1..=366).contains(&days) { days } else { 366 };
    let heatmap = compute_heatmap(history, days, end_date);
    let streak = compute_streak(&heatmap);
    let totals = compute_totals(games, history);
    let top_platforms = compute_top_platforms(games, 8);
    let top_genres = compute_top_genres(games, 8);
    let top_games = compute_top_games(games, 10);
    let momentum = compute_momentum(&heatmap);
    let last_30_start = heatmap.len().saturating_sub(30);
    let last_30_days = heatmap[last_30_start..].to_vec();
    json!({
        "heatmap": heatmap,
        "days": days,
        "streak": streak,
        "totals": totals,
        "top_platforms": top_platforms,
        "top_genres": top_genres,
        "top_games": top_games,
        "momentum": momentum,
        "last_30_days": last_30_days,
        "spend": compute_spend(games),
    })
}

fn load_ra_cache(ra_cache_dir: Option<&Path>) -> HashMap<String, Value> {
    let mut by_game = HashMap::new();
    let Some(dir) = ra_cache_dir.filter(|d| d.is_dir()) else {
        return by_game;
    };
    let Ok(rd) = std::fs::read_dir(dir) else {
        return by_game;
    };
    for ent in rd.flatten() {
        let path = ent.path();
        if path.extension().and_then(|s| s.to_str()) != Some("json") {
            continue;
        }
        if let Ok(raw) = std::fs::read_to_string(&path)
            && let Ok(val) = serde_json::from_str::<Value>(&raw)
            && let Some(gid) = val
                .get("game_id")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|s| !s.is_empty())
        {
            by_game.insert(gid.to_owned(), val);
        }
    }
    by_game
}

pub fn mastery_summary(games: &[Value], ra_cache_dir: Option<&Path>) -> Value {
    let ra_cache = load_ra_cache(ra_cache_dir);
    let bucket = |items: &[&Value]| -> Value {
        let mut never = 0usize;
        let mut played = 0usize;
        let mut beaten = 0usize;
        let mut completed = 0usize;
        let mut mastered = 0usize;
        let mut ra_tracked = 0usize;
        let mut ra_mastered = 0usize;
        let mut ra_prog_total = 0.0f64;
        for &g in items {
            let prog = g
                .get("progress")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_lowercase();
            if prog.contains("mastered") {
                mastered += 1;
            } else if prog.contains("completed") {
                completed += 1;
            } else if prog.contains("beaten") {
                beaten += 1;
            } else if int_nonneg(g.get("playtime_seconds")) > 0
                || int_nonneg(g.get("play_count")) > 0
            {
                played += 1;
            } else {
                never += 1;
            }
            let gid = g.get("game_id").and_then(Value::as_str).unwrap_or("");
            if let Some(ra) = ra_cache.get(gid)
                && ra
                    .get("game_id")
                    .and_then(Value::as_str)
                    .is_some_and(|s| !s.is_empty())
            {
                ra_tracked += 1;
                if ra.get("mastered").and_then(Value::as_bool).unwrap_or(false) {
                    ra_mastered += 1;
                }
                ra_prog_total += ra
                    .get("progress_pct")
                    .and_then(Value::as_f64)
                    .unwrap_or(0.0);
            }
        }
        let ra_avg = if ra_tracked > 0 {
            ((ra_prog_total / ra_tracked as f64) * 100.0).round() / 100.0
        } else {
            0.0
        };
        json!({
            "never": never,
            "played": played,
            "beaten": beaten,
            "completed": completed,
            "mastered": mastered,
            "ra_tracked": ra_tracked,
            "ra_mastered": ra_mastered,
            "ra_avg_progress": ra_avg,
            "total": items.len(),
        })
    };

    let valid_games: Vec<&Value> = games.iter().filter(|g| g.is_object()).collect();
    let mut by_platform: HashMap<String, Vec<&Value>> = HashMap::new();
    let mut by_decade: HashMap<String, Vec<&Value>> = HashMap::new();
    for &g in &valid_games {
        let raw_plat = g
            .get("platform")
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or("");
        let plat = if raw_plat.is_empty() {
            "Unspecified".to_owned()
        } else {
            raw_plat.to_owned()
        };
        by_platform.entry(plat).or_default().push(g);

        let year = int_nonneg(g.get("year"));
        let dkey = if year < 1980 {
            "pre-1980".to_owned()
        } else {
            format!("{}s", year / 10 * 10)
        };
        by_decade.entry(dkey).or_default().push(g);
    }

    let mut platforms_map = Map::new();
    for (p, items) in by_platform {
        platforms_map.insert(p, bucket(&items));
    }
    let mut decades_map = Map::new();
    for (d, items) in by_decade {
        decades_map.insert(d, bucket(&items));
    }

    json!({
        "platforms": Value::Object(platforms_map),
        "overall": bucket(&valid_games),
        "decades": Value::Object(decades_map),
    })
}

pub fn wrapped_summary(state: &Value, year: i16) -> Value {
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
    let game_by_id: HashMap<String, &Value> = games
        .iter()
        .filter(|g| g.is_object())
        .map(|g| {
            (
                g.get("game_id")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned(),
                g,
            )
        })
        .collect();

    let in_year: Vec<&Value> = history
        .iter()
        .filter(|e| {
            e.is_object()
                && e.get("started")
                    .and_then(Value::as_str)
                    .and_then(parse_local_date)
                    .is_some_and(|d| d.year() == year)
        })
        .collect();

    let mut played_ids: HashSet<String> = HashSet::new();
    let mut new_games = 0usize;
    let mut playtime = 0i64;
    let mut sessions = 0usize;
    let mut month_seconds = [0i64; 12];
    let mut weekday_seconds = [0i64; 7];
    let mut first_date: Option<Date> = None;
    let mut first_entry: Option<Value> = None;
    let mut oldest_year: Option<i64> = None;
    let mut oldest_game: Option<Value> = None;
    let mut progress_beaten = 0usize;
    let mut progress_completed = 0usize;
    let mut progress_mastered = 0usize;
    let mut by_day: HashMap<Date, BTreeSet<String>> = HashMap::new();

    for &entry in &in_year {
        let gid = entry
            .get("game_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        let game = game_by_id.get(&gid).copied();
        let started = entry.get("started").and_then(Value::as_str).unwrap_or("");
        let Some(d) = parse_local_date(started) else {
            continue;
        };
        let seconds = int_nonneg(entry.get("seconds"));
        playtime += seconds;
        sessions += 1;
        played_ids.insert(gid.clone());
        let m_idx = (d.month() as usize).saturating_sub(1).min(11);
        month_seconds[m_idx] += seconds;
        let wd_idx = d.weekday().to_monday_zero_offset() as usize;
        weekday_seconds[wd_idx] += seconds;
        by_day.entry(d).or_default().insert(gid.clone());

        let g_name = game
            .and_then(|g| g.get("name"))
            .and_then(Value::as_str)
            .unwrap_or("");
        if first_date.is_none_or(|fd| d < fd) {
            first_date = Some(d);
            first_entry = Some(json!({"date": started, "game_id": gid, "name": g_name}));
        }
        if let Some(g) = game {
            if let Some(added_d) = g
                .get("added_at")
                .and_then(Value::as_str)
                .and_then(parse_local_date)
                && added_d.year() == year
            {
                new_games += 1;
            }
            let gy = int_nonneg(g.get("year"));
            if gy > 0 && oldest_year.is_none_or(|oy| gy < oy) {
                oldest_year = Some(gy);
                oldest_game = Some(json!({"game_id": gid, "name": g_name, "year": gy}));
            }
        }
        let note = entry
            .get("note")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_lowercase();
        if note.contains("beaten") {
            progress_beaten += 1;
        }
        if note.contains("completed") {
            progress_completed += 1;
        }
        if note.contains("mastered") {
            progress_mastered += 1;
        }
    }

    if in_year.is_empty() {
        for g in games {
            let p = g
                .get("progress")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_lowercase();
            if p.contains("beaten") {
                progress_beaten += 1;
            }
            if p.contains("completed") {
                progress_completed += 1;
            }
            if p.contains("mastered") {
                progress_mastered += 1;
            }
        }
    }

    let year_games: Vec<Value> = played_ids
        .iter()
        .filter_map(|gid| game_by_id.get(gid).map(|g| (*g).clone()))
        .collect();
    let top_game = compute_top_games(&year_games, 1).into_iter().next();
    let top_platform = compute_top_platforms(&year_games, 1).into_iter().next();
    let top_genre = compute_top_genres(&year_games, 1).into_iter().next();

    let start = Date::constant(year, 1, 1);
    let end = Date::constant(year, 12, 31);
    let span_days = (start.until(end).map(|s| s.get_days()).unwrap_or(364) + 1) as usize;
    let in_year_owned: Vec<Value> = in_year.into_iter().cloned().collect();
    let hm = compute_heatmap(&in_year_owned, span_days, Some(end));
    let streak = compute_streak(&hm);

    let mut pairs: HashSet<(String, String)> = HashSet::new();
    for gids in by_day.values() {
        let local: Vec<&String> = gids.iter().collect();
        for i in 0..local.len() {
            for j in (i + 1)..local.len() {
                pairs.insert((local[i].clone(), local[j].clone()));
            }
        }
    }

    let busiest_month = if month_seconds.iter().any(|&s| s > 0) {
        let m_idx = (0..12).max_by_key(|&i| month_seconds[i]).unwrap_or(0);
        json!({"month": m_idx + 1, "seconds": month_seconds[m_idx]})
    } else {
        Value::Null
    };

    let wd_names = [
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    ];
    let busiest_weekday = if weekday_seconds.iter().any(|&s| s > 0) {
        let wd = (0..7).max_by_key(|&i| weekday_seconds[i]).unwrap_or(0);
        json!({"weekday": wd_names[wd], "seconds": weekday_seconds[wd]})
    } else {
        Value::Null
    };

    json!({
        "year": year,
        "totals": {
            "playtime_seconds": playtime,
            "sessions": sessions,
            "games_played": played_ids.len(),
            "new_games": new_games,
        },
        "progress": {
            "beaten": progress_beaten,
            "completed": progress_completed,
            "mastered": progress_mastered,
        },
        "streak": {"longest": streak["longest"]},
        "top": {
            "game": top_game.unwrap_or(Value::Null),
            "platform": top_platform.unwrap_or(Value::Null),
            "genre": top_genre.unwrap_or(Value::Null),
        },
        "oldest_played": oldest_game.unwrap_or(Value::Null),
        "first_play": first_entry.unwrap_or(Value::Null),
        "busiest_month": busiest_month,
        "busiest_weekday": busiest_weekday,
        "per_month": month_seconds.to_vec(),
        "co_play_pairs": pairs.len(),
    })
}

pub fn timeline_groups(state: &Value, days: usize) -> Value {
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
    let game_by_id: HashMap<String, &Value> = games
        .iter()
        .filter(|g| g.is_object())
        .map(|g| {
            (
                g.get("game_id")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned(),
                g,
            )
        })
        .collect();

    let end = today_local();
    let days = days.max(1);
    let start = end.checked_sub(((days - 1) as i64).days()).unwrap_or(end);
    let mut by_date: BTreeMap<Date, Vec<Value>> = BTreeMap::new();
    for entry in history {
        if !entry.is_object() {
            continue;
        }
        let started = entry.get("started").and_then(Value::as_str).unwrap_or("");
        let Some(d) = parse_local_date(started) else {
            continue;
        };
        if d < start || d > end {
            continue;
        }
        let gid = entry.get("game_id").and_then(Value::as_str).unwrap_or("");
        let game = game_by_id.get(gid).copied();
        let seconds = int_nonneg(entry.get("seconds"));
        let recording = basename(entry.get("recording"));
        by_date.entry(d).or_default().push(json!({
            "game_id": gid,
            "name": game.and_then(|g| g.get("name")).and_then(Value::as_str).unwrap_or(""),
            "cover": game.and_then(|g| g.get("has_cover")).and_then(Value::as_bool).unwrap_or(false),
            "seconds": seconds,
            "started": started,
            "recording": recording,
        }));
    }

    let groups: Vec<Value> = by_date
        .into_iter()
        .rev()
        .map(|(d, mut entries)| {
            entries.sort_by(|a, b| {
                let sa = a.get("started").and_then(Value::as_str).unwrap_or("");
                let sb = b.get("started").and_then(Value::as_str).unwrap_or("");
                sb.cmp(sa)
            });
            json!({"date": d.to_string(), "entries": entries})
        })
        .collect();
    json!({"days": days, "groups": groups})
}
