//! Library-level operations shared across OpenBox (`catalog.py`).

use std::collections::{HashMap, HashSet};
use std::sync::LazyLock;

use openbox_core::ApiError;
use regex::Regex;
use serde_json::{Map, Value, json};

pub fn parse_timestamp(text: &str) -> Option<i64> {
    let trimmed = text.trim();
    if trimmed.is_empty() {
        return None;
    }
    if let Ok(ts) = trimmed.parse::<jiff::Timestamp>() {
        return Some(ts.as_second());
    }
    if let Ok(dt) = trimmed.parse::<jiff::civil::DateTime>()
        && let Ok(ts) = jiff::tz::TimeZone::UTC.to_timestamp(dt)
    {
        return Some(ts.as_second());
    }
    if let Ok(d) = trimmed.parse::<jiff::civil::Date>()
        && let Ok(ts) = jiff::tz::TimeZone::UTC.to_timestamp(d.at(0, 0, 0, 0))
    {
        return Some(ts.as_second());
    }
    None
}

pub const PROGRESS: &[&str] = &[
    "",
    "Unplayed",
    "Playing",
    "Paused",
    "Beaten",
    "Completed",
    "Mastered",
    "Abandoned",
];

pub const MEDIA_FIELDS: &[&str] = &[
    "cover",
    "background",
    "clear_logo",
    "fanart",
    "banner",
    "icon",
    "poster",
    "square",
    "box_back",
    "box_spine",
    "box_3d",
    "title_screen",
    "cart_front",
    "cart_back",
    "disc",
    "advertisement",
    "manual",
    "video",
    "music",
];

pub const MAX_TAGS: usize = 50;
pub const MAX_TAG_LENGTH: usize = 64;
pub const MAX_PRICE_CENTS: i64 = 1_000_000_000;

static CURRENCY_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Z]{3}$").expect("valid currency regex"));
static WORD_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\w+").expect("valid word regex"));

pub fn value_to_str(val: &Value) -> String {
    match val {
        Value::Null => String::new(),
        Value::String(s) => s.clone(),
        Value::Bool(b) => {
            if *b {
                "True".to_owned()
            } else {
                "False".to_owned()
            }
        }
        Value::Number(n) => n.to_string(),
        other => other.to_string(),
    }
}

pub fn is_valid_progress(val: &str) -> bool {
    PROGRESS.contains(&val)
}

/// Canonical stored progress: `"Unplayed"` (any case) maps to `""` (unset).
pub fn canon_progress(value: Option<&Value>) -> String {
    let text = value.map(value_to_str).unwrap_or_default();
    let trimmed = text.trim();
    if trimmed.to_lowercase() == "unplayed" {
        String::new()
    } else {
        trimmed.to_owned()
    }
}

pub fn display_progress(value: Option<&Value>) -> String {
    let text = value.map(value_to_str).unwrap_or_default();
    let trimmed = text.trim();
    if trimmed.is_empty() {
        "Unplayed".to_owned()
    } else {
        trimmed.to_owned()
    }
}

pub fn clean_user_rating(value: Option<&Value>) -> Result<i64, ApiError> {
    let rating = match value {
        Some(Value::Number(n)) => n
            .as_i64()
            .or_else(|| n.as_f64().map(|f| f as i64))
            .ok_or_else(|| ApiError::bad_request("User rating must be an integer from 0 to 5."))?,
        Some(Value::String(s)) => s
            .trim()
            .parse::<i64>()
            .map_err(|_| ApiError::bad_request("User rating must be an integer from 0 to 5."))?,
        Some(Value::Bool(b)) => i64::from(*b),
        _ => {
            return Err(ApiError::bad_request(
                "User rating must be an integer from 0 to 5.",
            ));
        }
    };
    if !(0..=5).contains(&rating) {
        return Err(ApiError::bad_request(
            "User rating must be an integer from 0 to 5.",
        ));
    }
    Ok(rating)
}

pub fn clean_price_cents(value: Option<&Value>) -> Result<Option<i64>, ApiError> {
    match value {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) if s.is_empty() => Ok(None),
        Some(Value::Number(n)) => {
            let Some(v) = n.as_i64() else {
                return Err(ApiError::bad_request(
                    "Price paid must be a whole number of cents.",
                ));
            };
            if !(0..=MAX_PRICE_CENTS).contains(&v) {
                return Err(ApiError::bad_request("Price paid is out of range."));
            }
            Ok(Some(v))
        }
        _ => Err(ApiError::bad_request(
            "Price paid must be a whole number of cents.",
        )),
    }
}

pub fn clean_currency(value: Option<&Value>) -> Result<String, ApiError> {
    let text = value
        .map(value_to_str)
        .unwrap_or_default()
        .trim()
        .to_uppercase();
    if !CURRENCY_RE.is_match(&text) {
        return Err(ApiError::bad_request(
            "Currency must be a three-letter code such as USD.",
        ));
    }
    Ok(text)
}

pub fn normalize_notes(value: Option<&Value>) -> Vec<Value> {
    match value {
        None | Some(Value::Null) => Vec::new(),
        Some(Value::String(s)) => {
            let text = s.trim();
            if text.is_empty() {
                Vec::new()
            } else {
                vec![json!({"ts": "", "text": text})]
            }
        }
        Some(Value::Array(items)) => {
            let mut out = Vec::new();
            for item in items {
                if let Some(obj) = item.as_object() {
                    let text = obj
                        .get("text")
                        .map(value_to_str)
                        .unwrap_or_default()
                        .trim()
                        .to_owned();
                    if text.is_empty() {
                        continue;
                    }
                    let ts = obj
                        .get("ts")
                        .map(value_to_str)
                        .unwrap_or_default()
                        .trim()
                        .to_owned();
                    out.push(json!({"ts": ts, "text": text}));
                } else {
                    let text = value_to_str(item).trim().to_owned();
                    if !text.is_empty() {
                        out.push(json!({"ts": "", "text": text}));
                    }
                }
            }
            out
        }
        _ => Vec::new(),
    }
}

pub fn normalize_manual_sessions(value: Option<&Value>) -> Vec<Value> {
    let Some(items) = value.and_then(Value::as_array) else {
        return Vec::new();
    };
    let mut sessions = Vec::new();
    for item in items {
        let Some(obj) = item.as_object() else {
            continue;
        };
        let seconds = obj
            .get("seconds")
            .and_then(|v| {
                v.as_i64()
                    .or_else(|| v.as_str().and_then(|s| s.trim().parse().ok()))
            })
            .unwrap_or(0);
        if seconds <= 0 {
            continue;
        }
        let date = obj
            .get("date")
            .map(value_to_str)
            .unwrap_or_default()
            .trim()
            .to_owned();
        let note = obj
            .get("note")
            .map(value_to_str)
            .unwrap_or_default()
            .trim()
            .to_owned();
        sessions.push(json!({"date": date, "seconds": seconds, "note": note}));
    }
    sessions
}

pub fn total_playtime_seconds(game: &Value) -> i64 {
    let Some(obj) = game.as_object() else {
        return 0;
    };
    let observed = obj
        .get("playtime_seconds")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let manual = obj
        .get("manual_playtime_seconds")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    observed.max(0) + manual.max(0)
}

pub fn progress_suggest_due(game: &Value, settings: &Value) -> bool {
    let Some(g) = game.as_object() else {
        return false;
    };
    let s = settings.as_object();
    if s.and_then(|m| m.get("backlog_progress_suggest")) == Some(&Value::Bool(false)) {
        return false;
    }
    let first_play = s
        .and_then(|m| m.get("progress_on_first_play"))
        .map(value_to_str)
        .unwrap_or_else(|| "Playing".to_owned());
    if !first_play.trim().is_empty() {
        return false;
    }
    if !g
        .get("progress")
        .map(value_to_str)
        .unwrap_or_default()
        .trim()
        .is_empty()
    {
        return false;
    }
    if g.get("progress_suggested")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        return false;
    }
    true
}

pub fn normalize_tags(value: Option<&Value>) -> Result<Vec<String>, ApiError> {
    match value {
        None | Some(Value::Null) => Ok(Vec::new()),
        Some(Value::Array(arr)) => {
            let mut clean = Vec::new();
            let mut seen = HashSet::new();
            for raw in arr {
                let text = value_to_str(raw);
                let label: String = text.split_whitespace().collect::<Vec<_>>().join(" ");
                if label.is_empty() {
                    continue;
                }
                if label.chars().count() > MAX_TAG_LENGTH {
                    return Err(ApiError::bad_request(format!(
                        "Tags are limited to {MAX_TAG_LENGTH} characters."
                    )));
                }
                let folded = label.to_lowercase();
                if seen.insert(folded) {
                    clean.push(label);
                    if clean.len() >= MAX_TAGS {
                        break;
                    }
                }
            }
            Ok(clean)
        }
        _ => Err(ApiError::bad_request("Tags must be a list.")),
    }
}

pub fn apply_tag_changes(
    game: &mut Map<String, Value>,
    replace: Option<&Value>,
    add: Option<&Value>,
    remove: Option<&Value>,
) -> Result<bool, ApiError> {
    let current: Vec<String> = game
        .get("tags")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .filter_map(Value::as_str)
                .map(ToOwned::to_owned)
                .collect()
        })
        .unwrap_or_default();
    let mut result = current.clone();
    if let Some(val) = replace {
        result = normalize_tags(Some(val))?;
    }
    if let Some(val) = add {
        let additions = normalize_tags(Some(val))?;
        let mut existing: HashSet<String> = result.iter().map(|s| s.to_lowercase()).collect();
        for label in additions {
            let folded = label.to_lowercase();
            if existing.insert(folded) {
                result.push(label);
            }
        }
    }
    if let Some(val) = remove {
        let removals = normalize_tags(Some(val))?;
        let drop: HashSet<String> = removals.into_iter().map(|s| s.to_lowercase()).collect();
        result.retain(|label| !drop.contains(&label.to_lowercase()));
    }
    result.truncate(MAX_TAGS);
    if result == current {
        return Ok(false);
    }
    game.insert(
        "tags".into(),
        Value::Array(result.into_iter().map(Value::String).collect()),
    );
    Ok(true)
}

pub fn tag_counts(games: &[Value]) -> Vec<Value> {
    let mut counts: HashMap<String, (String, usize)> = HashMap::new();
    for game in games {
        let Some(obj) = game.as_object() else {
            continue;
        };
        if obj.get("hidden").and_then(Value::as_bool).unwrap_or(false) {
            continue;
        }
        if let Some(tags) = obj.get("tags").and_then(Value::as_array) {
            for label in tags {
                let Some(s) = label.as_str() else {
                    continue;
                };
                let trimmed = s.trim();
                if trimmed.is_empty() {
                    continue;
                }
                let key = trimmed.to_lowercase();
                counts
                    .entry(key)
                    .and_modify(|e| e.1 += 1)
                    .or_insert_with(|| (trimmed.to_owned(), 1));
            }
        }
    }
    let mut items: Vec<(String, usize)> = counts.into_values().collect();
    items.sort_by(|a, b| {
        b.1.cmp(&a.1)
            .then_with(|| a.0.to_lowercase().cmp(&b.0.to_lowercase()))
    });
    items
        .into_iter()
        .map(|(tag, count)| json!({"tag": tag, "count": count}))
        .collect()
}

pub fn related_game_ids(games: &[Value], selected: usize, limit: usize) -> Vec<usize> {
    let Some(base) = games.get(selected).and_then(Value::as_object) else {
        return Vec::new();
    };
    let base_genre = base
        .get("genre")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_lowercase();
    let base_genres: HashSet<String> = WORD_RE
        .find_iter(&base_genre)
        .map(|m| m.as_str().to_owned())
        .collect();
    fn get_s<'a>(o: &'a Map<String, Value>, k: &str) -> &'a str {
        o.get(k).and_then(Value::as_str).unwrap_or("")
    }
    let mut ranked: Vec<(isize, String, usize)> = Vec::new();
    for (index, game) in games.iter().enumerate() {
        let Some(g) = game.as_object() else {
            continue;
        };
        if index == selected || g.get("hidden").and_then(Value::as_bool).unwrap_or(false) {
            continue;
        }
        let g_genre = get_s(g, "genre").to_lowercase();
        let genres: HashSet<String> = WORD_RE
            .find_iter(&g_genre)
            .map(|m| m.as_str().to_owned())
            .collect();
        let mut score: isize = 2 * (base_genres.intersection(&genres).count() as isize);
        if !get_s(base, "series").is_empty() && get_s(base, "series") == get_s(g, "series") {
            score += 8;
        }
        if !get_s(base, "collection").is_empty()
            && get_s(base, "collection") == get_s(g, "collection")
        {
            score += 5;
        }
        if !get_s(base, "developer").is_empty() && get_s(base, "developer") == get_s(g, "developer")
        {
            score += 3;
        }
        if !get_s(base, "platform").is_empty() && get_s(base, "platform") == get_s(g, "platform") {
            score += 2;
        }
        if !get_s(base, "publisher").is_empty() && get_s(base, "publisher") == get_s(g, "publisher")
        {
            score += 1;
        }
        if score > 0 {
            let sort_key = {
                let st = get_s(g, "sort_title");
                if !st.is_empty() { st } else { get_s(g, "name") }
            }
            .to_lowercase();
            ranked.push((-score, sort_key, index));
        }
    }
    ranked.sort();
    ranked
        .into_iter()
        .take(limit)
        .map(|(_, _, idx)| idx)
        .collect()
}

const BULK_ALLOWED: &[&str] = &[
    "platform",
    "genre",
    "progress",
    "rating",
    "user_rating",
    "favorite",
    "hidden",
    "esrb",
    "custom_fields",
    "tags",
    "tags_add",
    "tags_remove",
    "reset_stats",
    "play_count",
    "playtime_seconds",
    "last_played",
    "price_paid_cents",
    "price_currency",
];

fn validate_bulk_changes(ids: Option<&Value>, changes: Option<&Value>) -> Result<(), ApiError> {
    let Some(id_arr) = ids.and_then(Value::as_array) else {
        return Err(ApiError::bad_request("Select at least one game."));
    };
    if id_arr.is_empty() {
        return Err(ApiError::bad_request("Select at least one game."));
    }
    let Some(ch_obj) = changes.and_then(Value::as_object) else {
        return Err(ApiError::bad_request(
            "No valid bulk changes were supplied.",
        ));
    };
    if ch_obj.is_empty() || !ch_obj.keys().all(|k| BULK_ALLOWED.contains(&k.as_str())) {
        return Err(ApiError::bad_request(
            "No valid bulk changes were supplied.",
        ));
    }
    if ch_obj.contains_key("tags")
        && (ch_obj.contains_key("tags_add") || ch_obj.contains_key("tags_remove"))
    {
        return Err(ApiError::bad_request(
            "Tags cannot be replaced and adjusted in the same request.",
        ));
    }
    if let (Some(add), Some(rem)) = (ch_obj.get("tags_add"), ch_obj.get("tags_remove")) {
        let additions: HashSet<String> = normalize_tags(Some(add))?
            .into_iter()
            .map(|s| s.to_lowercase())
            .collect();
        let removals: HashSet<String> = normalize_tags(Some(rem))?
            .into_iter()
            .map(|s| s.to_lowercase())
            .collect();
        if !additions.is_disjoint(&removals) {
            return Err(ApiError::bad_request(
                "A tag cannot be added and removed in the same request.",
            ));
        }
    }
    Ok(())
}

fn clean_bulk_fields(changes: &Map<String, Value>) -> Result<Map<String, Value>, ApiError> {
    let mut clean = Map::new();
    for (field, value) in changes {
        match field.as_str() {
            "favorite" | "hidden" => {
                let Some(b) = value.as_bool() else {
                    let title = if field == "favorite" {
                        "Favorite"
                    } else {
                        "Hidden"
                    };
                    return Err(ApiError::bad_request(format!(
                        "{title} must be true or false."
                    )));
                };
                clean.insert(field.clone(), Value::Bool(b));
            }
            "reset_stats" => {
                let Some(b) = value.as_bool() else {
                    return Err(ApiError::bad_request("Reset stats must be true or false."));
                };
                clean.insert(field.clone(), Value::Bool(b));
            }
            "play_count" | "playtime_seconds" => {
                let num = value
                    .as_i64()
                    .or_else(|| value.as_str().and_then(|s| s.trim().parse::<i64>().ok()))
                    .ok_or_else(|| {
                        ApiError::bad_request(format!("{field} must be a valid integer."))
                    })?;
                if num < 0 {
                    return Err(ApiError::bad_request(format!(
                        "{field} must be non-negative."
                    )));
                }
                clean.insert(field.clone(), json!(num));
            }
            "last_played" => {
                let text = value_to_str(value).trim().to_owned();
                if !text.is_empty() && parse_timestamp(&text).is_none() {
                    return Err(ApiError::bad_request(
                        "last_played must be a date or date and time.",
                    ));
                }
                clean.insert(field.clone(), Value::String(text));
            }
            "progress" => {
                let val = canon_progress(Some(value));
                if !is_valid_progress(&val) {
                    return Err(ApiError::bad_request("Unknown progress value."));
                }
                clean.insert(field.clone(), Value::String(val));
            }
            "rating" => {
                let rating = value
                    .as_f64()
                    .or_else(|| value.as_str().and_then(|s| s.trim().parse::<f64>().ok()))
                    .ok_or_else(|| ApiError::bad_request("Rating must be between 0 and 5."))?;
                if !(0.0..=5.0).contains(&rating) {
                    return Err(ApiError::bad_request("Rating must be between 0 and 5."));
                }
                clean.insert(field.clone(), json!(rating));
            }
            "price_paid_cents" => {
                match clean_price_cents(Some(value))? {
                    Some(v) => clean.insert(field.clone(), json!(v)),
                    None => clean.insert(field.clone(), Value::Null),
                };
            }
            "price_currency" => {
                let c = clean_currency(Some(value))?;
                clean.insert(field.clone(), Value::String(c));
            }
            "user_rating" => {
                let r = clean_user_rating(Some(value))?;
                clean.insert(field.clone(), json!(r));
            }
            "esrb" => {
                clean.insert(
                    field.clone(),
                    Value::String(value_to_str(value).trim().to_owned()),
                );
            }
            "custom_fields" => {
                let Some(obj) = value.as_object() else {
                    return Err(ApiError::bad_request("Custom fields must be an object."));
                };
                let mut m = Map::new();
                for (k, v) in obj {
                    let kk = k.trim();
                    if !kk.is_empty() {
                        m.insert(
                            kk.to_owned(),
                            Value::String(value_to_str(v).trim().to_owned()),
                        );
                    }
                }
                clean.insert(field.clone(), Value::Object(m));
            }
            "tags" | "tags_add" | "tags_remove" => {
                let tags = normalize_tags(Some(value))?;
                clean.insert(
                    field.clone(),
                    Value::Array(tags.into_iter().map(Value::String).collect()),
                );
            }
            _ => {
                clean.insert(
                    field.clone(),
                    Value::String(value_to_str(value).trim().to_owned()),
                );
            }
        }
    }
    if let Some(price_val) = clean.get("price_paid_cents") {
        if price_val.is_null() {
            clean.remove("price_currency");
        } else if !clean.contains_key("price_currency") {
            return Err(ApiError::bad_request(
                "Currency is required when a price is set.",
            ));
        }
    } else if clean.contains_key("price_currency") {
        return Err(ApiError::bad_request(
            "Currency cannot be set without a price.",
        ));
    }
    Ok(clean)
}

fn resolve_bulk_indexes(games: &[Value], ids: &[Value]) -> Result<Vec<usize>, ApiError> {
    let mut stable_indexes: HashMap<String, usize> = HashMap::new();
    for (index, game) in games.iter().enumerate() {
        let Some(obj) = game.as_object() else {
            continue;
        };
        if let Some(gid) = obj.get("game_id").and_then(Value::as_str)
            && !gid.is_empty()
        {
            stable_indexes.insert(gid.to_owned(), index);
        }
        if let Some(aliases) = obj.get("legacy_game_ids").and_then(Value::as_array) {
            for alias in aliases {
                let s = value_to_str(alias);
                if !s.trim().is_empty() {
                    stable_indexes.insert(s, index);
                }
            }
        }
    }
    let mut selected: Vec<usize> = Vec::new();
    let mut seen = HashSet::new();
    for val in ids {
        let key = value_to_str(val);
        let idx: isize = if let Some(&i) = stable_indexes.get(&key) {
            i as isize
        } else if let Some(n) = val.as_i64() {
            n as isize
        } else {
            key.trim()
                .parse::<isize>()
                .map_err(|_| ApiError::game_not_found("A selected game no longer exists."))?
        };
        if idx < 0 || (idx as usize) >= games.len() {
            return Err(ApiError::game_not_found(
                "A selected game no longer exists.",
            ));
        }
        let u = idx as usize;
        if seen.insert(u) {
            selected.push(u);
        }
    }
    selected.sort_unstable();
    Ok(selected)
}

fn apply_bulk_patch(
    game: &mut Map<String, Value>,
    mut patch: Map<String, Value>,
) -> Result<(), ApiError> {
    if patch.get("price_paid_cents") == Some(&Value::Null) {
        patch.remove("price_paid_cents");
        patch.remove("price_currency");
        game.remove("price_paid_cents");
        game.remove("price_currency");
    }
    if patch
        .remove("reset_stats")
        .and_then(|v| v.as_bool())
        .unwrap_or(false)
    {
        game.insert("play_count".into(), json!(0));
        game.insert("playtime_seconds".into(), json!(0));
        game.insert("last_played".into(), Value::String(String::new()));
    }
    if let Some(Value::Object(cf)) = patch.remove("custom_fields") {
        let existing = game
            .entry("custom_fields".to_owned())
            .or_insert_with(|| Value::Object(Map::new()));
        if !existing.is_object() {
            *existing = Value::Object(Map::new());
        }
        if let Some(m) = existing.as_object_mut() {
            for (k, v) in cf {
                m.insert(k, v);
            }
        }
    }
    let tags = patch.remove("tags");
    let tags_add = patch.remove("tags_add");
    let tags_remove = patch.remove("tags_remove");
    if tags.is_some() || tags_add.is_some() || tags_remove.is_some() {
        apply_tag_changes(game, tags.as_ref(), tags_add.as_ref(), tags_remove.as_ref())?;
    }
    for (k, v) in patch {
        game.insert(k, v);
    }
    Ok(())
}

pub fn bulk_update(
    games: &mut [Value],
    ids: Option<&Value>,
    changes: Option<&Value>,
) -> Result<usize, ApiError> {
    validate_bulk_changes(ids, changes)?;
    let ch_obj = changes.and_then(Value::as_object).unwrap();
    let id_arr = ids.and_then(Value::as_array).unwrap();
    let clean = clean_bulk_fields(ch_obj)?;
    let selected = resolve_bulk_indexes(games, id_arr)?;
    for idx in &selected {
        if let Some(obj) = games[*idx].as_object_mut() {
            apply_bulk_patch(obj, clean.clone())?;
        }
    }
    Ok(selected.len())
}

pub fn apply_progress_automation(game: &mut Map<String, Value>, settings: &Value) {
    let Some(s) = settings.as_object() else {
        return;
    };
    if !s
        .get("progress_automation_enabled")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        return;
    }
    let allowed: HashSet<String> = s
        .get("progress_values")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .filter_map(Value::as_str)
                .map(ToOwned::to_owned)
                .collect()
        })
        .unwrap_or_else(|| PROGRESS.iter().map(|p| (*p).to_owned()).collect());
    let play_minutes = s
        .get("progress_automation_play_minutes")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let idle_days = s
        .get("progress_automation_idle_days")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let playtime = game
        .get("playtime_seconds")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let cur_prog = game
        .get("progress")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    if play_minutes > 0
        && allowed.contains("Playing")
        && playtime >= play_minutes * 60
        && (cur_prog.is_empty() || cur_prog == "Paused")
    {
        game.insert("progress".into(), Value::String("Playing".to_owned()));
    }
    if idle_days > 0
        && allowed.contains("Paused")
        && game.get("progress").and_then(Value::as_str) == Some("Playing")
        && let Some(lp) = game.get("last_played").and_then(Value::as_str)
        && let Some(ts) = parse_timestamp(lp)
    {
        let now = jiff::Timestamp::now().as_second();
        if (now - ts) / 86400 >= idle_days {
            game.insert("progress".into(), Value::String("Paused".to_owned()));
        }
    }
}

pub fn game_media_paths(game: &Value) -> Vec<String> {
    let mut paths = Vec::new();
    let Some(obj) = game.as_object() else {
        return paths;
    };
    for &field in MEDIA_FIELDS {
        if let Some(p) = obj.get(field).and_then(Value::as_str) {
            let trimmed = p.trim();
            if !trimmed.is_empty() {
                paths.push(trimmed.to_owned());
            }
        }
    }
    if let Some(shots) = obj.get("screenshots").and_then(Value::as_array) {
        for s in shots {
            if let Some(p) = s.as_str() {
                let trimmed = p.trim();
                if !trimmed.is_empty() {
                    paths.push(trimmed.to_owned());
                }
            }
        }
    }
    paths
}
