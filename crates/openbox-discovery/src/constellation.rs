//! Library relationship graph (`pkg/parity/parity_constellation.py`).

use std::borrow::Cow;
use std::cmp::Ordering;
use std::collections::{HashMap, HashSet};

use jiff::Timestamp;
use jiff::civil::{Date, DateTime};
use serde::{Serialize, Serializer};
use serde_json::Value;

pub const KINDS: &[&str] = &[
    "series",
    "developer",
    "publisher",
    "genre",
    "platform_family",
    "co_played",
];

pub const CO_PLAY_WINDOW_DAYS: i64 = 7;

static NULL_VALUE: Value = Value::Null;

#[derive(Clone, Copy)]
pub enum ValueOrEmpty<'a> {
    Val(&'a Value),
    Empty,
}

impl Serialize for ValueOrEmpty<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        match self {
            Self::Val(v) => v.serialize(serializer),
            Self::Empty => serializer.serialize_str(""),
        }
    }
}

#[derive(Serialize)]
pub struct ConstellationNode<'a> {
    pub cover: ValueOrEmpty<'a>,
    pub degree: usize,
    pub game_id: Cow<'a, str>,
    pub has_cover: bool,
    pub i: usize,
    pub id: &'a Value,
    pub name: &'a str,
    pub platform: ValueOrEmpty<'a>,
    pub playtime_seconds: i64,
    pub year: ValueOrEmpty<'a>,
}

#[derive(Clone, Copy, Serialize)]
pub struct ConstellationEdge {
    pub kind: &'static str,
    pub s: usize,
    pub t: usize,
    pub w: f64,
}

#[derive(Serialize)]
pub struct ConstellationGraph<'a> {
    pub edges: Vec<ConstellationEdge>,
    pub kinds: Vec<&'static str>,
    pub nodes: Vec<ConstellationNode<'a>>,
    pub truncated: bool,
}

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

fn platform_family_id(platform: &str) -> u8 {
    match platform_family(platform) {
        "Computer" => 1,
        "Nintendo" => 2,
        "Sega" => 3,
        "Sony" => 4,
        "Microsoft" => 5,
        "Arcade" => 6,
        "Atari" => 7,
        "Commodore" => 8,
        "Z80" => 9,
        "NEC" => 10,
        _ => 0,
    }
}

fn normalize(v: Option<&Value>) -> String {
    match v {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => s.trim().to_lowercase(),
        Some(other) => other.to_string().trim().to_lowercase(),
    }
}

fn days_in_month(year: i64, month: u8) -> u8 {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 => {
            if (year % 4 == 0 && year % 100 != 0) || year % 400 == 0 {
                29
            } else {
                28
            }
        }
        _ => 0,
    }
}

/// Howard Hinnant civil-to-days algorithm (days since 1970-01-01).
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = y - i64::from(m <= 2);
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = m + if m > 2 { -3 } else { 9 };
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

#[inline]
fn parse_ascii_2(b: &[u8]) -> Option<u8> {
    let d0 = b[0].wrapping_sub(b'0');
    let d1 = b[1].wrapping_sub(b'0');
    if d0 < 10 && d1 < 10 {
        Some(d0 * 10 + d1)
    } else {
        None
    }
}

#[inline]
fn parse_ascii_4(b: &[u8]) -> Option<i64> {
    let hi = parse_ascii_2(&b[0..2])? as i64;
    let lo = parse_ascii_2(&b[2..4])? as i64;
    Some(hi * 100 + lo)
}

fn parse_date_ordinal(value: Option<&Value>) -> Option<i64> {
    let raw = value.and_then(Value::as_str)?.trim();
    if raw.is_empty() {
        return None;
    }
    let bytes = raw.as_bytes();
    // Fast zero-allocation path for YYYY-MM-DD, YYYY-MM-DDTHH:MM:SSZ, YYYY-MM-DD HH:MM:SS, etc.
    // Falls through to jiff if a timezone offset (+/-/[) appears after the date prefix.
    if bytes.len() >= 10 && bytes[4] == b'-' && bytes[7] == b'-' {
        let has_tz_offset =
            bytes.len() > 10 && bytes[10..].iter().any(|&c| matches!(c, b'+' | b'-' | b'['));
        if !has_tz_offset
            && let (Some(y), Some(m), Some(d)) = (
                parse_ascii_4(&bytes[0..4]),
                parse_ascii_2(&bytes[5..7]),
                parse_ascii_2(&bytes[8..10]),
            )
            && (1..=12).contains(&m)
            && d >= 1
            && d <= days_in_month(y, m)
        {
            return Some(days_from_civil(y, i64::from(m), i64::from(d)));
        }
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

#[inline]
fn int_field(g: &Value, key: &str) -> i64 {
    g.get(key)
        .and_then(|v| {
            v.as_i64()
                .or_else(|| v.as_f64().map(|f| f as i64))
                .or_else(|| v.as_str().and_then(|s| s.trim().parse::<i64>().ok()))
        })
        .unwrap_or(0)
}

fn game_id_cow(g: &Value) -> Cow<'_, str> {
    if let Some(s) = g
        .get("game_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        return Cow::Borrowed(s);
    }
    match g.get("id") {
        Some(Value::String(s)) => Cow::Borrowed(s.as_str()),
        Some(Value::Number(n)) => Cow::Owned(n.to_string()),
        _ => Cow::Borrowed("None"),
    }
}

/// Counts days in `a_days` that have at least one day in `b_days` within `±CO_PLAY_WINDOW_DAYS`,
/// capped at 5. Both slices must be sorted and deduplicated.
#[inline]
fn shared_co_play_days(a_days: &[i64], b_days: &[i64]) -> usize {
    let mut shared = 0usize;
    let mut j = 0usize;
    let b_len = b_days.len();
    for &d in a_days {
        let lo = d.saturating_sub(CO_PLAY_WINDOW_DAYS);
        let hi = d.saturating_add(CO_PLAY_WINDOW_DAYS);
        while j < b_len && b_days[j] < lo {
            j += 1;
        }
        if j < b_len && b_days[j] <= hi {
            shared += 1;
            if shared >= 5 {
                return 5;
            }
        }
    }
    shared
}

struct CandidateKey<'a> {
    playtime: i64,
    play_count: i64,
    favorite: bool,
    name: &'a str,
    orig_idx: usize,
    game: &'a Value,
}

#[inline]
fn cmp_candidate(a: &CandidateKey<'_>, b: &CandidateKey<'_>) -> Ordering {
    b.playtime
        .cmp(&a.playtime)
        .then_with(|| b.play_count.cmp(&a.play_count))
        .then_with(|| b.favorite.cmp(&a.favorite))
        .then_with(|| a.name.cmp(b.name))
        .then_with(|| a.orig_idx.cmp(&b.orig_idx))
}

#[derive(Default)]
struct Interner {
    map: HashMap<String, u32>,
}

impl Interner {
    fn intern(&mut self, value: Option<&Value>) -> u32 {
        let norm = normalize(value);
        if norm.is_empty() {
            return 0;
        }
        if let Some(&id) = self.map.get(&norm) {
            return id;
        }
        let next = (self.map.len() as u32) + 1;
        self.map.insert(norm, next);
        next
    }
}

struct NodeFeatures {
    series: u32,
    developer: u32,
    publisher: u32,
    genre: u32,
    platform_family: u8,
}

pub fn build_graph_typed<'a>(
    games: &'a [Value],
    history: &[Value],
    kinds: Option<&HashSet<String>>,
    limit: usize,
) -> ConstellationGraph<'a> {
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

    let active_series = active_kinds.contains("series");
    let active_developer = active_kinds.contains("developer");
    let active_publisher = active_kinds.contains("publisher");
    let active_genre = active_kinds.contains("genre");
    let active_pf = active_kinds.contains("platform_family");
    let active_co_played = active_kinds.contains("co_played");

    let mut candidates: Vec<CandidateKey<'a>> = Vec::with_capacity(games.len());
    for (orig_idx, g) in games.iter().enumerate() {
        if !g.is_object() {
            continue;
        }
        candidates.push(CandidateKey {
            playtime: int_field(g, "playtime_seconds"),
            play_count: int_field(g, "play_count"),
            favorite: g.get("favorite").and_then(Value::as_bool).unwrap_or(false),
            name: g.get("name").and_then(Value::as_str).unwrap_or(""),
            orig_idx,
            game: g,
        });
    }
    let truncated = candidates.len() > limit;
    if limit == 0 {
        candidates.clear();
    } else if candidates.len() > limit {
        candidates.select_nth_unstable_by(limit, cmp_candidate);
        candidates.truncate(limit);
        candidates.sort_unstable_by(cmp_candidate);
    } else {
        candidates.sort_unstable_by(cmp_candidate);
    }

    let n = candidates.len();
    let mut nodes: Vec<ConstellationNode<'a>> = Vec::with_capacity(n);
    let mut features: Vec<NodeFeatures> = Vec::with_capacity(n);
    let mut series_intern = Interner::default();
    let mut dev_intern = Interner::default();
    let mut pub_intern = Interner::default();
    let mut genre_intern = Interner::default();

    for (i, cand) in candidates.iter().enumerate() {
        let game = cand.game;
        let gid = game_id_cow(game);
        features.push(NodeFeatures {
            series: if active_series {
                series_intern.intern(game.get("series"))
            } else {
                0
            },
            developer: if active_developer {
                dev_intern.intern(game.get("developer"))
            } else {
                0
            },
            publisher: if active_publisher {
                pub_intern.intern(game.get("publisher"))
            } else {
                0
            },
            genre: if active_genre {
                genre_intern.intern(game.get("genre"))
            } else {
                0
            },
            platform_family: if active_pf {
                platform_family_id(game.get("platform").and_then(Value::as_str).unwrap_or(""))
            } else {
                0
            },
        });
        nodes.push(ConstellationNode {
            i,
            game_id: gid,
            id: game.get("id").unwrap_or(&NULL_VALUE),
            name: cand.name,
            platform: game
                .get("platform")
                .map_or(ValueOrEmpty::Empty, ValueOrEmpty::Val),
            year: game
                .get("year")
                .map_or(ValueOrEmpty::Empty, ValueOrEmpty::Val),
            playtime_seconds: cand.playtime,
            degree: 0,
            cover: game
                .get("cover")
                .map_or(ValueOrEmpty::Empty, ValueOrEmpty::Val),
            has_cover: game
                .get("has_cover")
                .and_then(Value::as_bool)
                .unwrap_or(false),
        });
    }

    // Build sorted, deduplicated play-day lists restricted to the top-`limit` nodes only.
    let mut days_by_node: Vec<Vec<i64>> = Vec::new();
    if active_co_played && !history.is_empty() && !nodes.is_empty() {
        let mut gid_to_indices: HashMap<&str, Vec<usize>> = HashMap::with_capacity(n);
        for (idx, node) in nodes.iter().enumerate() {
            gid_to_indices
                .entry(node.game_id.as_ref())
                .or_default()
                .push(idx);
        }
        days_by_node.resize_with(n, Vec::new);
        for entry in history {
            let Some(gid_val) = entry.get("game_id").filter(|v| !v.is_null()) else {
                continue;
            };
            let indices = if let Some(s) = gid_val.as_str() {
                gid_to_indices.get(s)
            } else {
                let owned = gid_val.to_string();
                gid_to_indices.get(owned.as_str())
            };
            let Some(indices) = indices else {
                continue;
            };
            let Some(day) = parse_date_ordinal(entry.get("started")) else {
                continue;
            };
            for &idx in indices {
                days_by_node[idx].push(day);
            }
        }
        for days in &mut days_by_node {
            if days.len() > 1 {
                days.sort_unstable();
                days.dedup();
            }
        }
    }

    let has_co_play = !days_by_node.is_empty();
    let mut degrees = vec![0usize; n];
    let mut edges: Vec<ConstellationEdge> = Vec::new();

    for a_idx in 0..n {
        let fa = &features[a_idx];
        let a_days: &[i64] = if has_co_play {
            &days_by_node[a_idx]
        } else {
            &[]
        };
        for b_idx in (a_idx + 1)..n {
            let fb = &features[b_idx];

            // Strict descending weight order:
            // series (1.0) > developer (0.8) > co_played (0.6 max) > publisher (0.5)
            // > genre (0.4) > platform_family (0.3)
            let (mut best_kind, mut best_weight) = if fa.series != 0 && fa.series == fb.series {
                (Some("series"), 1.0f64)
            } else if fa.developer != 0 && fa.developer == fb.developer {
                (Some("developer"), 0.8f64)
            } else if fa.publisher != 0 && fa.publisher == fb.publisher {
                (Some("publisher"), 0.5f64)
            } else if fa.genre != 0 && fa.genre == fb.genre {
                (Some("genre"), 0.4f64)
            } else if fa.platform_family != 0 && fa.platform_family == fb.platform_family {
                (Some("platform_family"), 0.3f64)
            } else {
                (None, 0.0f64)
            };

            if best_weight < 0.6 && !a_days.is_empty() {
                let b_days = &days_by_node[b_idx];
                if !b_days.is_empty() {
                    let cp = shared_co_play_days(a_days, b_days);
                    if cp > 0 {
                        let w = (cp as f64 / 5.0).min(1.0) * 0.6;
                        if w > best_weight {
                            best_kind = Some("co_played");
                            best_weight = w;
                        }
                    }
                }
            }

            if let Some(kind) = best_kind {
                let rounded = (best_weight * 100.0).round() / 100.0;
                edges.push(ConstellationEdge {
                    kind,
                    s: a_idx,
                    t: b_idx,
                    w: rounded,
                });
                degrees[a_idx] += 1;
                degrees[b_idx] += 1;
            }
        }
    }

    for (node, deg) in nodes.iter_mut().zip(degrees) {
        node.degree = deg;
    }

    let mut kinds_out: Vec<&'static str> = active_kinds.into_iter().collect();
    kinds_out.sort_unstable();

    ConstellationGraph {
        edges,
        kinds: kinds_out,
        nodes,
        truncated,
    }
}

pub fn build_graph(
    games: &[Value],
    history: &[Value],
    kinds: Option<&HashSet<String>>,
    limit: usize,
) -> Value {
    let graph = build_graph_typed(games, history, kinds, limit);
    serde_json::to_value(graph).unwrap_or(Value::Null)
}
