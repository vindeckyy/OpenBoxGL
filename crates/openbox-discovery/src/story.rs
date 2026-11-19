//! Per-game story — deterministic narrative timeline (`pkg/parity/parity_story.py`).

use std::collections::HashSet;

use jiff::Timestamp;
use jiff::civil::{Date, DateTime};
use serde_json::{Value, json};

pub const MILESTONE_SECONDS: &[i64] =
    &[3600, 5 * 3600, 10 * 3600, 25 * 3600, 50 * 3600, 100 * 3600];

fn stamp(value: &str) -> (i64, String) {
    let trimmed = value.trim();
    if let Ok(ts) = trimmed.parse::<Timestamp>() {
        return (ts.as_second(), value.to_owned());
    }
    if let Ok(dt) = trimmed.replace(' ', "T").parse::<DateTime>()
        && let Ok(ts) = jiff::tz::TimeZone::UTC.to_timestamp(dt)
    {
        return (ts.as_second(), value.to_owned());
    }
    if trimmed.len() >= 10
        && let Ok(d) = trimmed[..10].parse::<Date>()
        && let Ok(ts) = jiff::tz::TimeZone::UTC.to_timestamp(d.at(0, 0, 0, 0))
    {
        return (ts.as_second(), value.to_owned());
    }
    (0, value.to_owned())
}

fn num(v: Option<&Value>) -> f64 {
    match v {
        Some(Value::Number(n)) => n.as_f64().unwrap_or(0.0),
        _ => 0.0,
    }
}

fn format_hours(seconds: f64) -> String {
    let hours = seconds / 3600.0;
    if hours < 10.0 {
        format!("{hours:.1}h")
    } else {
        format!("{}h", hours.round() as i64)
    }
}

fn kind_order(kind: &str) -> usize {
    match kind {
        "added" => 0,
        "first_played" => 1,
        "session" => 2,
        "milestone" => 3,
        "progress" => 4,
        "moment" => 5,
        _ => 9,
    }
}

pub fn game_story(state: &Value, game: &Value) -> Value {
    let game_id = game.get("game_id").and_then(Value::as_str).unwrap_or("");
    let name = game
        .get("name")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .unwrap_or("Untitled");
    let mut events: Vec<Value> = Vec::new();

    let added_at = game.get("added_at").and_then(Value::as_str).unwrap_or("");
    if !added_at.is_empty() {
        events.push(json!({
            "kind": "added",
            "at": added_at,
            "title": "Added to library",
            "detail": game.get("source").and_then(Value::as_str).unwrap_or(""),
        }));
    }

    let mut sessions: Vec<&Value> = Vec::new();
    if let Some(history) = state.get("history").and_then(Value::as_array) {
        for row in history {
            if !row.is_object() {
                continue;
            }
            let row_id = row.get("game_id").and_then(Value::as_str).unwrap_or("");
            if !row_id.is_empty() && row_id != game_id {
                continue;
            }
            if row_id.is_empty() && row.get("game").and_then(Value::as_str).unwrap_or("") != name {
                continue;
            }
            sessions.push(row);
        }
    }
    sessions.sort_by_key(|row| stamp(row.get("started").and_then(Value::as_str).unwrap_or("")));

    if let Some(&first) = sessions.first() {
        let f_secs = num(first.get("seconds"));
        events.push(json!({
            "kind": "first_played",
            "at": first.get("started").and_then(Value::as_str).unwrap_or(""),
            "title": "First played",
            "detail": if f_secs > 0.0 { format_hours(f_secs) } else { String::new() },
        }));
        if let Some(&longest) = sessions.iter().max_by(|a, b| {
            num(a.get("seconds"))
                .partial_cmp(&num(b.get("seconds")))
                .unwrap_or(std::cmp::Ordering::Equal)
        }) {
            let l_secs = num(longest.get("seconds"));
            if l_secs > 0.0 {
                events.push(json!({
                    "kind": "session",
                    "at": longest.get("started").and_then(Value::as_str).unwrap_or(""),
                    "title": "Longest session",
                    "detail": format_hours(l_secs),
                }));
            }
        }
    }

    let playtime = num(game.get("playtime_seconds"));
    let last_played = game
        .get("last_played")
        .and_then(Value::as_str)
        .unwrap_or("");
    if playtime > 0.0 {
        let mut cumulative = 0.0;
        let mut crossed: HashSet<i64> = HashSet::new();
        for &row in &sessions {
            cumulative += num(row.get("seconds"));
            for &mark in MILESTONE_SECONDS {
                if !crossed.contains(&mark) && cumulative >= mark as f64 {
                    crossed.insert(mark);
                    events.push(json!({
                        "kind": "milestone",
                        "at": row.get("started").and_then(Value::as_str).unwrap_or(""),
                        "title": format!("{}h played", mark / 3600),
                        "detail": "",
                    }));
                }
            }
        }
        for &mark in MILESTONE_SECONDS {
            if !crossed.contains(&mark) && playtime >= mark as f64 {
                events.push(json!({
                    "kind": "milestone",
                    "at": last_played,
                    "title": format!("{}h played", mark / 3600),
                    "detail": "",
                }));
            }
        }
    }

    let progress = game.get("progress").and_then(Value::as_str).unwrap_or("");
    if matches!(progress, "Beaten" | "Completed" | "Mastered") {
        events.push(json!({
            "kind": "progress",
            "at": last_played,
            "title": progress,
            "detail": "",
        }));
    }

    if let Some(moments) = game.get("moments").and_then(Value::as_array) {
        for item in moments {
            if !item.is_object() {
                continue;
            }
            let detail: String = item
                .get("note")
                .and_then(Value::as_str)
                .unwrap_or("")
                .chars()
                .take(200)
                .collect();
            events.push(json!({
                "kind": "moment",
                "at": item.get("created_at").and_then(Value::as_str).unwrap_or(""),
                "title": item.get("title").and_then(Value::as_str).filter(|s| !s.is_empty()).unwrap_or("Moment"),
                "detail": detail,
            }));
        }
    }

    events.sort_by_key(|ev| {
        (
            stamp(ev.get("at").and_then(Value::as_str).unwrap_or("")),
            kind_order(ev.get("kind").and_then(Value::as_str).unwrap_or("")),
        )
    });

    let moment_count = events
        .iter()
        .filter(|e| e.get("kind").and_then(Value::as_str) == Some("moment"))
        .count();

    json!({
        "game_id": game_id,
        "name": name,
        "platform": game.get("platform").and_then(Value::as_str).unwrap_or(""),
        "events": events,
        "totals": {
            "sessions": sessions.len(),
            "playtime_seconds": playtime as i64,
            "moments": moment_count,
            "progress": progress,
        },
    })
}
