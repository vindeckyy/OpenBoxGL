//! Library relationship graph (`pkg/parity/parity_constellation.py`).

use std::collections::{HashMap, HashSet};

use jiff::Timestamp;
use jiff::civil::{Date, DateTime};
use serde_json::{Value, json};

pub const KINDS: &[&str] = &[
    "series",
    "developer",
    "publisher",
    "genre",
    "platform_family",
    "co_played",
];

pub const CO_PLAY_WINDOW_DAYS: i64 = 7;

pub fn kind_weight(kind: &str) -> f64 {
    match kind {
        "series" => 1.0,
        "developer" => 0.8,
        "publisher" => 0.5,
        "genre" => 0.4,
        "platform_family" => 0.3,
        "co_played" => 0.6,
        _ => 0.0,
    }
}

pub fn platform_family(platform: &str) -> &'static str {
    match platform {
        "PC" | "MS-DOS" | "Windows" | "Linux" | "macOS" | "Steam" => "Computer",
        "SNES" | "NES" | "Nintendo 64" | "GameCube" | "Wii" | "Wii U" | "Switch" | "Game Boy"
        | "Game Boy Color" | "Game Boy Advance" | "Nintendo DS" | "Nintendo 3DS" => "Nintendo",
        "Sega Genesis" | "Sega Mega Drive" | "Sega Master System" | "Sega Game Gear"
        | "Sega Saturn" | "Sega Dreamcast" | "Sega CD" => "Sega",
        "PlayStation" | "PlayStation 2" | "PlayStation 3" | "PlayStation 4" | "PlayStation 5"
        | "PSP" | "PlayStation Vita" => "Sony",
        "Xbox" | "Xbox 360" | "Xbox One" | "Xbox Series X" => "Microsoft",
        "Arcade" | "MAME" | "Neo Geo" | "Neo Geo AES" => "Arcade",
        "Atari 2600" | "Atari 5200" | "Atari 7800" | "Atari Jaguar" | "Atari Lynx" | "Atari ST" => {
            "Atari"
        }
        "Amiga" | "Commodore 64" => "Commodore",
        "Amstrad CPC" | "ZX Spectrum" => "Z80",
        "Turbografx-16" | "PC Engine" => "NEC",
        _ => "Other",
    }
}

fn normalize(v: Option<&Value>) -> String {
    match v {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => s.trim().to_lowercase(),
        Some(other) => other.to_string().trim().to_lowercase(),
    }
}

fn parse_date_ordinal(value: Option<&Value>) -> Option<i64> {
    let raw = value.and_then(Value::as_str)?.trim();
    if raw.is_empty() {
        return None;
    }
    let d: Date = if let Ok(ts) = raw.parse::<Timestamp>() {
        jiff::tz::TimeZone::UTC.to_datetime(ts).date()
    } else if let Ok(dt) = raw.replace(' ', "T").parse::<DateTime>() {
        dt.date()
    } else if raw.len() >= 10 {
        raw[..10].parse::<Date>().ok()?
    } else {
        return None;
    };
    let ts = jiff::tz::TimeZone::UTC
        .to_timestamp(d.at(0, 0, 0, 0))
        .ok()?;
    Some(ts.as_second().div_euclid(86_400))
}

fn int_field(g: &Value, key: &str) -> i64 {
    g.get(key)
        .and_then(|v| {
            v.as_i64()
                .or_else(|| v.as_f64().map(|f| f as i64))
                .or_else(|| v.as_str().and_then(|s| s.trim().parse::<i64>().ok()))
        })
        .unwrap_or(0)
}

fn game_id_str(g: &Value) -> String {
    if let Some(s) = g
        .get("game_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        return s.to_owned();
    }
    match g.get("id") {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) => n.to_string(),
        _ => "None".to_owned(),
    }
}

fn co_play_days(history: &[Value], game_ids: &[String]) -> HashMap<(String, String), usize> {
    let mut days_for: HashMap<String, HashSet<i64>> = HashMap::new();
    for entry in history {
        let Some(gid_val) = entry.get("game_id").filter(|v| !v.is_null()) else {
            continue;
        };
        let gid = gid_val
            .as_str()
            .map(str::to_owned)
            .unwrap_or_else(|| gid_val.to_string());
        let Some(day) = parse_date_ordinal(entry.get("started")) else {
            continue;
        };
        days_for.entry(gid).or_default().insert(day);
    }
    let mut pairs = HashMap::new();
    for i in 0..game_ids.len() {
        let a = &game_ids[i];
        let Some(a_days) = days_for.get(a) else {
            continue;
        };
        for b in &game_ids[i + 1..] {
            let Some(b_days) = days_for.get(b) else {
                continue;
            };
            let mut shared = 0usize;
            for &d in a_days {
                if (-CO_PLAY_WINDOW_DAYS..=CO_PLAY_WINDOW_DAYS)
                    .any(|delta| b_days.contains(&(d + delta)))
                {
                    shared += 1;
                }
            }
            if shared > 0 {
                let key = if a <= b {
                    (a.clone(), b.clone())
                } else {
                    (b.clone(), a.clone())
                };
                pairs.insert(key, shared.min(5));
            }
        }
    }
    pairs
}

pub fn build_graph(
    games: &[Value],
    history: &[Value],
    kinds: Option<&HashSet<String>>,
    limit: usize,
) -> Value {
    let active_kinds: HashSet<&'static str> = match kinds {
        Some(ks) => {
            let filtered: HashSet<&'static str> =
                KINDS.iter().copied().filter(|k| ks.contains(*k)).collect();
            if filtered.is_empty() {
                KINDS.iter().copied().collect()
            } else {
                filtered
            }
        }
        None => KINDS.iter().copied().collect(),
    };

    let mut candidates: Vec<&Value> = games.iter().filter(|g| g.is_object()).collect();
    candidates.sort_by(|a, b| {
        int_field(b, "playtime_seconds")
            .cmp(&int_field(a, "playtime_seconds"))
            .then_with(|| int_field(b, "play_count").cmp(&int_field(a, "play_count")))
            .then_with(|| {
                let fa = a.get("favorite").and_then(Value::as_bool).unwrap_or(false);
                let fb = b.get("favorite").and_then(Value::as_bool).unwrap_or(false);
                fb.cmp(&fa)
            })
            .then_with(|| {
                let na = a.get("name").and_then(Value::as_str).unwrap_or("");
                let nb = b.get("name").and_then(Value::as_str).unwrap_or("");
                na.cmp(nb)
            })
    });
    let truncated = candidates.len() > limit;
    candidates.truncate(limit);

    let mut nodes: Vec<Value> = Vec::with_capacity(candidates.len());
    let mut node_gids: Vec<String> = Vec::with_capacity(candidates.len());
    for (i, game) in candidates.iter().enumerate() {
        let gid = game_id_str(game);
        node_gids.push(gid.clone());
        nodes.push(json!({
            "i": i,
            "game_id": gid,
            "id": game.get("id").cloned().unwrap_or(Value::Null),
            "name": game.get("name").and_then(Value::as_str).unwrap_or(""),
            "platform": game.get("platform").cloned().unwrap_or_else(|| json!("")),
            "year": game.get("year").cloned().unwrap_or_else(|| json!("")),
            "playtime_seconds": int_field(game, "playtime_seconds"),
            "degree": 0,
            "cover": game.get("cover").cloned().unwrap_or_else(|| json!("")),
            "has_cover": game.get("has_cover").and_then(Value::as_bool).unwrap_or(false),
        }));
    }

    let co_play = if active_kinds.contains("co_played") {
        co_play_days(history, &node_gids)
    } else {
        HashMap::new()
    };

    let mut degrees = vec![0usize; nodes.len()];
    let mut edges: Vec<(usize, usize, &'static str, f64)> = Vec::new();
    for a_idx in 0..candidates.len() {
        let a = candidates[a_idx];
        for b_idx in (a_idx + 1)..candidates.len() {
            let b = candidates[b_idx];
            let mut best_kind: Option<&'static str> = None;
            let mut best_weight = 0.0f64;

            for &kind in KINDS {
                if kind == "co_played" || !active_kinds.contains(kind) {
                    continue;
                }
                if kind == "platform_family" {
                    let fa =
                        platform_family(a.get("platform").and_then(Value::as_str).unwrap_or(""));
                    let fb =
                        platform_family(b.get("platform").and_then(Value::as_str).unwrap_or(""));
                    if fa == fb && fa != "Other" {
                        let w = kind_weight("platform_family");
                        if w > best_weight {
                            best_kind = Some("platform_family");
                            best_weight = w;
                        }
                    }
                } else {
                    let va = normalize(a.get(kind));
                    let vb = normalize(b.get(kind));
                    if !va.is_empty() && va == vb {
                        let w = kind_weight(kind);
                        if w > best_weight {
                            best_kind = Some(kind);
                            best_weight = w;
                        }
                    }
                }
            }

            if active_kinds.contains("co_played") {
                let ga = &node_gids[a_idx];
                let gb = &node_gids[b_idx];
                let key = if ga <= gb {
                    (ga.clone(), gb.clone())
                } else {
                    (gb.clone(), ga.clone())
                };
                if let Some(&cp) = co_play.get(&key) {
                    let w = (cp as f64 / 5.0).min(1.0) * kind_weight("co_played");
                    if w > best_weight {
                        best_kind = Some("co_played");
                        best_weight = w;
                    }
                }
            }

            if let Some(kind) = best_kind {
                let rounded = (best_weight * 100.0).round() / 100.0;
                edges.push((a_idx, b_idx, kind, rounded));
                degrees[a_idx] += 1;
                degrees[b_idx] += 1;
            }
        }
    }

    for (node, deg) in nodes.iter_mut().zip(degrees) {
        if let Some(m) = node.as_object_mut() {
            m.insert("degree".into(), json!(deg));
        }
    }
    edges.sort_by(|a, b| {
        a.0.cmp(&b.0)
            .then_with(|| a.1.cmp(&b.1))
            .then_with(|| a.2.cmp(b.2))
    });
    let edge_vals: Vec<Value> = edges
        .into_iter()
        .map(|(s, t, kind, w)| json!({"s": s, "t": t, "kind": kind, "w": w}))
        .collect();

    let mut kinds_out: Vec<&str> = active_kinds.into_iter().collect();
    kinds_out.sort_unstable();

    json!({
        "nodes": nodes,
        "edges": edge_vals,
        "kinds": kinds_out,
        "truncated": truncated,
    })
}
