//! Named filter presets with optional Big Box quick-switch slots (`pkg/parity/parity_filter_presets.py`).

use std::collections::{HashMap, HashSet};
use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Map, Value, json};

pub const PRESET_RULE_KEYS: &[&str] = &[
    "platform",
    "view",
    "query",
    "esrb",
    "progress",
    "favorite",
    "installed",
    "platform_category",
    "genre",
    "developer",
    "publisher",
    "hidden",
    "has_saves",
    "has_achievements",
    "has_missing_media",
    "has_highscores",
    "source",
    "achievements_source",
    "region",
    "vertical",
    "requires_chd",
    "exclude",
];

pub const EXCLUDE_FIELDS: &[&str] = &[
    "platform",
    "query",
    "esrb",
    "progress",
    "favorite",
    "installed",
    "genre",
    "developer",
    "publisher",
    "hidden",
    "has_saves",
    "has_achievements",
    "has_missing_media",
    "has_highscores",
    "source",
    "achievements_source",
    "region",
    "vertical",
    "requires_chd",
];

pub const TEXT_RULE_FIELDS: &[&str] = &["genre", "developer", "publisher", "region"];
pub const TEXT_OPERATORS: &[&str] = &["contains_any", "not_contains_any"];
pub const EXPLORER_FIELDS: &[&str] = &[
    "genre",
    "developer",
    "publisher",
    "platform",
    "progress",
    "esrb",
];

const BOOL_RULE_FIELDS: &[&str] = &[
    "favorite",
    "hidden",
    "has_saves",
    "has_achievements",
    "has_missing_media",
    "has_highscores",
    "vertical",
    "requires_chd",
];

static ALNUM_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"[A-Za-z0-9]+").expect("valid alnum regex"));

fn val_to_string(v: &Value) -> String {
    match v {
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

fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64().is_some_and(|x| x != 0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(m)) => !m.is_empty(),
    }
}

fn truncate_chars(s: &str, max_chars: usize) -> String {
    s.chars().take(max_chars).collect()
}

fn clean_rule_values(value: Option<&Value>, item_limit: usize) -> Result<Vec<String>, String> {
    let Some(arr) = value.and_then(Value::as_array) else {
        return Err("Filter values must be a list of at most 20 entries.".to_owned());
    };
    if arr.len() > 20 {
        return Err("Filter values must be a list of at most 20 entries.".to_owned());
    }
    let mut values = Vec::new();
    for item in arr {
        if item.is_boolean() || !(item.is_string() || item.is_number()) {
            return Err("Filter values must be strings.".to_owned());
        }
        let text = truncate_chars(val_to_string(item).trim(), item_limit);
        if !text.is_empty() && !values.contains(&text) {
            values.push(text);
        }
    }
    Ok(values)
}

fn clean_source_rule(value: &Value) -> Result<Option<Vec<String>>, String> {
    let wrapped;
    let target = if value.is_string() {
        wrapped = Value::Array(vec![value.clone()]);
        Some(&wrapped)
    } else {
        Some(value)
    };
    let mut values = clean_rule_values(target, 80)?;
    values.sort();
    if values.is_empty() {
        Ok(None)
    } else {
        Ok(Some(values))
    }
}

fn clean_text_rule(value: &Value) -> Result<Option<Value>, String> {
    if let Some(obj) = value.as_object() {
        let operator = obj
            .get("op")
            .map(val_to_string)
            .unwrap_or_default()
            .trim()
            .to_owned();
        if !TEXT_OPERATORS.contains(&operator.as_str()) {
            return Err(
                "Text filter operator must be contains_any or not_contains_any.".to_owned(),
            );
        }
        let values = clean_rule_values(obj.get("values"), 80)?;
        if values.is_empty() {
            return Ok(None);
        }
        return Ok(Some(json!({"op": operator, "values": values})));
    }
    let text = val_to_string(value).trim().to_owned();
    if !text.is_empty() && text != "all" {
        Ok(Some(Value::String(text)))
    } else {
        Ok(None)
    }
}

fn parse_bool_like(v: &Value) -> Option<bool> {
    match v {
        Value::Bool(b) => Some(*b),
        Value::String(s) if s == "true" => Some(true),
        Value::String(s) if s == "false" => Some(false),
        _ => None,
    }
}

fn clean_exclude(exclude: &Value) -> Result<Map<String, Value>, String> {
    if exclude.is_null() || exclude.as_object().is_some_and(Map::is_empty) {
        return Ok(Map::new());
    }
    let Some(obj) = exclude.as_object() else {
        return Err("Preset exclusions must be an object.".to_owned());
    };
    let mut clean = Map::new();
    for (field, raw) in obj {
        if !EXCLUDE_FIELDS.contains(&field.as_str()) {
            continue;
        }
        if field == "query" {
            let text = raw
                .as_str()
                .map(|s| truncate_chars(s.trim(), 500))
                .unwrap_or_default();
            if !text.is_empty() {
                clean.insert(field.clone(), Value::String(text));
            }
            continue;
        }
        if BOOL_RULE_FIELDS.contains(&field.as_str()) {
            if let Some(b) = parse_bool_like(raw) {
                clean.insert(field.clone(), Value::Bool(b));
            }
            continue;
        }
        let was_list = raw.is_array();
        let values: Vec<Value> = match raw.as_array() {
            Some(a) => a.clone(),
            None => vec![raw.clone()],
        };
        if values.len() > 20 {
            return Err("Preset exclusions allow at most 20 values per field.".to_owned());
        }
        let mut cleaned: Vec<Value> = Vec::new();
        for item in &values {
            if let Some(b) = item.as_bool() {
                if field == "installed" {
                    let s = if b { "installed" } else { "uninstalled" }.to_owned();
                    let val = Value::String(s);
                    if !cleaned.contains(&val) {
                        cleaned.push(val);
                    }
                    continue;
                } else if values.len() == 1 {
                    cleaned = vec![Value::Bool(b)];
                    break;
                } else {
                    return Err("Boolean exclusions must be a single value.".to_owned());
                }
            }
            if !(item.is_string() || item.is_number()) {
                return Err(
                    "Preset exclusion values must be strings, numbers, or booleans.".to_owned(),
                );
            }
            let text = truncate_chars(val_to_string(item).trim(), 80);
            if !text.is_empty() {
                let val = Value::String(text);
                if !cleaned.contains(&val) {
                    cleaned.push(val);
                }
            }
        }
        if !cleaned.is_empty() {
            if was_list {
                clean.insert(field.clone(), Value::Array(cleaned));
            } else {
                clean.insert(field.clone(), cleaned.into_iter().next().unwrap());
            }
        }
    }
    Ok(clean)
}

pub fn normalize_rules(rules: &Value) -> Result<Map<String, Value>, String> {
    let Some(obj) = rules.as_object() else {
        return Err("Preset rules must be an object.".to_owned());
    };
    let mut clean = Map::new();
    for &key in PRESET_RULE_KEYS {
        let Some(value) = obj.get(key) else {
            continue;
        };
        if key == "exclude" {
            let ex = clean_exclude(value)?;
            if !ex.is_empty() {
                clean.insert(key.to_owned(), Value::Object(ex));
            }
            continue;
        }
        if matches!(key, "source" | "achievements_source") {
            if let Some(norm) = clean_source_rule(value)? {
                clean.insert(
                    key.to_owned(),
                    Value::Array(norm.into_iter().map(Value::String).collect()),
                );
            }
            continue;
        }
        if BOOL_RULE_FIELDS.contains(&key) {
            if let Some(b) = parse_bool_like(value) {
                clean.insert(key.to_owned(), Value::Bool(b));
            }
            continue;
        }
        if key == "installed" {
            match value {
                Value::Bool(true) => {
                    clean.insert(key.to_owned(), Value::String("installed".to_owned()));
                }
                Value::Bool(false) => {
                    clean.insert(key.to_owned(), Value::String("uninstalled".to_owned()));
                }
                Value::String(s) if matches!(s.as_str(), "installed" | "uninstalled" | "all") => {
                    clean.insert(key.to_owned(), Value::String(s.clone()));
                }
                _ => {}
            }
            continue;
        }
        if TEXT_RULE_FIELDS.contains(&key) {
            if let Some(norm) = clean_text_rule(value)? {
                clean.insert(key.to_owned(), norm);
            }
            continue;
        }
        let mut text = val_to_string(value).trim().to_owned();
        if key == "query" {
            text = truncate_chars(&text, 500);
        }
        if !text.is_empty() && text != "all" {
            clean.insert(key.to_owned(), Value::String(text));
        }
    }
    if clean.is_empty() {
        return Err("Preset needs at least one filter rule.".to_owned());
    }
    Ok(clean)
}

pub fn list_presets(state: &Value) -> Vec<Value> {
    let Some(presets) = state.get("filter_presets").and_then(Value::as_array) else {
        return Vec::new();
    };
    presets
        .iter()
        .filter(|item| {
            item.is_object()
                && item
                    .get("name")
                    .and_then(Value::as_str)
                    .is_some_and(|s| !s.is_empty())
        })
        .cloned()
        .collect()
}

pub fn save_preset(
    state: &mut Value,
    name: &str,
    rules: &Value,
    bigbox_quick: bool,
) -> Result<String, String> {
    let clean_name = name.trim().to_owned();
    if clean_name.is_empty() {
        return Err("Preset name is required.".to_owned());
    }
    let clean_rules = normalize_rules(rules)?;
    let entry = json!({
        "name": clean_name,
        "rules": Value::Object(clean_rules),
        "bigbox_quick": bigbox_quick,
    });
    let Some(root) = state.as_object_mut() else {
        return Err("State must be an object.".to_owned());
    };
    let presets = root
        .entry("filter_presets".to_owned())
        .or_insert_with(|| Value::Array(Vec::new()));
    if !presets.is_array() {
        *presets = Value::Array(Vec::new());
    }
    if let Some(arr) = presets.as_array_mut() {
        for slot in arr.iter_mut() {
            if slot.get("name").and_then(Value::as_str) == Some(clean_name.as_str()) {
                *slot = entry;
                return Ok(clean_name);
            }
        }
        arr.push(entry);
    }
    Ok(clean_name)
}

pub fn delete_preset(state: &mut Value, name: &str) -> bool {
    let clean_name = name.trim();
    let Some(arr) = state
        .get_mut("filter_presets")
        .and_then(Value::as_array_mut)
    else {
        return false;
    };
    let before = arr.len();
    arr.retain(|item| item.get("name").and_then(Value::as_str) != Some(clean_name));
    arr.len() != before
}

pub fn bigbox_quick_presets(state: &Value, limit: usize) -> Vec<Value> {
    list_presets(state)
        .into_iter()
        .filter(|item| truthy(item.get("bigbox_quick")))
        .take(limit)
        .collect()
}

fn matches_platform(game: &Map<String, Value>, rules: &Map<String, Value>) -> bool {
    let platform = rules
        .get("platform")
        .map(val_to_string)
        .unwrap_or_default()
        .trim()
        .to_owned();
    if !platform.is_empty()
        && platform != "all"
        && game.get("platform").map(val_to_string).unwrap_or_default() != platform
    {
        return false;
    }
    true
}

fn matches_view(game: &Map<String, Value>, rules: &Map<String, Value>) -> bool {
    let view = rules
        .get("view")
        .map(val_to_string)
        .unwrap_or_default()
        .trim()
        .to_owned();
    if !view.is_empty() && view != "all" {
        if view == "favorites" && !truthy(game.get("favorite")) {
            return false;
        }
        if view == "hidden" && !truthy(game.get("hidden")) {
            return false;
        }
        if view == "installed" && game.get("store_installed") == Some(&Value::Bool(false)) {
            return false;
        }
        if view == "uninstalled" && game.get("store_installed") != Some(&Value::Bool(false)) {
            return false;
        }
    }
    true
}

fn matches_esrb_progress(game: &Map<String, Value>, rules: &Map<String, Value>) -> bool {
    let esrb = rules
        .get("esrb")
        .map(val_to_string)
        .unwrap_or_default()
        .trim()
        .to_owned();
    if !esrb.is_empty() && game.get("esrb").map(val_to_string).unwrap_or_default() != esrb {
        return false;
    }
    let progress = rules
        .get("progress")
        .map(val_to_string)
        .unwrap_or_default()
        .trim()
        .to_owned();
    if !progress.is_empty() {
        let stored = game.get("progress").map(val_to_string).unwrap_or_default();
        if progress.to_lowercase() == "unplayed" {
            if !stored.is_empty() && stored != "Unplayed" {
                return false;
            }
        } else if stored != progress {
            return false;
        }
    }
    true
}

fn has_any_achievement_source(game: &Map<String, Value>) -> bool {
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

fn matches_booleans(game: &Map<String, Value>, rules: &Map<String, Value>) -> bool {
    if let Some(v) = rules.get("favorite")
        && truthy(game.get("favorite")) != truthy(Some(v))
    {
        return false;
    }
    if let Some(v) = rules.get("hidden")
        && truthy(game.get("hidden")) != truthy(Some(v))
    {
        return false;
    }
    if let Some(v) = rules.get("has_saves") {
        let actual = truthy(game.get("save_paths")) || truthy(game.get("has_saves"));
        if actual != truthy(Some(v)) {
            return false;
        }
    }
    if let Some(v) = rules.get("has_achievements") {
        let actual = truthy(game.get("has_achievements")) || has_any_achievement_source(game);
        if actual != truthy(Some(v)) {
            return false;
        }
    }
    if let Some(v) = rules.get("has_missing_media") {
        let actual = truthy(game.get("has_missing_media"))
            || !truthy(game.get("cover"))
            || !truthy(game.get("background"));
        if actual != truthy(Some(v)) {
            return false;
        }
    }
    if let Some(v) = rules.get("has_highscores")
        && truthy(game.get("has_highscores")) != truthy(Some(v))
    {
        return false;
    }
    if let Some(v) = rules.get("vertical")
        && truthy(game.get("vertical")) != truthy(Some(v))
    {
        return false;
    }
    if let Some(v) = rules.get("requires_chd")
        && truthy(game.get("requires_chd")) != truthy(Some(v))
    {
        return false;
    }
    true
}

fn matches_source(game: &Map<String, Value>, rules: &Map<String, Value>) -> bool {
    let Some(sources) = rules.get("source") else {
        return true;
    };
    if !truthy(Some(sources)) {
        return true;
    }
    let values: Vec<String> = match sources.as_array() {
        Some(a) => a
            .iter()
            .map(|v| val_to_string(v).trim().to_lowercase())
            .collect(),
        None => vec![val_to_string(sources).trim().to_lowercase()],
    };
    let actual = game
        .get("source")
        .map(val_to_string)
        .unwrap_or_default()
        .trim()
        .to_lowercase();
    !actual.is_empty() && values.contains(&actual)
}

fn matches_achievements_source(game: &Map<String, Value>, rules: &Map<String, Value>) -> bool {
    let Some(sources) = rules.get("achievements_source") else {
        return true;
    };
    if !truthy(Some(sources)) {
        return true;
    }
    let values: Vec<String> = match sources.as_array() {
        Some(a) => a.iter().map(val_to_string).collect(),
        None => vec![val_to_string(sources)],
    };
    let Some(entries) = game.get("achievement_sources").and_then(Value::as_object) else {
        return false;
    };
    for source in values {
        if let Some(entry) = entries.get(&source).and_then(Value::as_object)
            && (entry.get("total").and_then(Value::as_i64).unwrap_or(0) > 0
                || entry.get("earned").and_then(Value::as_i64).unwrap_or(0) > 0)
        {
            return true;
        }
    }
    false
}

fn matches_installed_state(game: &Map<String, Value>, rules: &Map<String, Value>) -> bool {
    match rules.get("installed").and_then(Value::as_str) {
        Some("installed") if game.get("store_installed") == Some(&Value::Bool(false)) => false,
        Some("uninstalled") if game.get("store_installed") != Some(&Value::Bool(false)) => false,
        _ => true,
    }
}

fn text_matches(value: Option<&Value>, rule: &Value) -> bool {
    let haystack = value.map(val_to_string).unwrap_or_default().to_lowercase();
    if let Some(obj) = rule.as_object() {
        let operator = obj.get("op").and_then(Value::as_str).unwrap_or("");
        let matched = obj
            .get("values")
            .and_then(Value::as_array)
            .is_some_and(|vals| {
                vals.iter()
                    .any(|cand| haystack.contains(&val_to_string(cand).to_lowercase()))
            });
        return if operator == "contains_any" {
            matched
        } else {
            !matched
        };
    }
    haystack.contains(&val_to_string(rule).trim().to_lowercase())
}

fn matches_text_fields(game: &Map<String, Value>, rules: &Map<String, Value>) -> bool {
    for field in ["genre", "developer", "publisher", "region"] {
        if let Some(rule) = rules.get(field)
            && !text_matches(game.get(field), rule)
        {
            return false;
        }
    }
    true
}

fn matches_query(game: &Map<String, Value>, rules: &Map<String, Value>) -> bool {
    let query = rules
        .get("query")
        .map(val_to_string)
        .unwrap_or_default()
        .trim()
        .to_lowercase();
    if query.is_empty() {
        return true;
    }
    let haystack = [
        "name",
        "sort_title",
        "platform",
        "genre",
        "developer",
        "publisher",
        "series",
        "notes",
    ]
    .iter()
    .map(|f| game.get(*f).map(val_to_string).unwrap_or_default())
    .collect::<Vec<_>>()
    .join(" ")
    .to_lowercase();
    if haystack.contains(&query) {
        return true;
    }
    let qlen = query.chars().count();
    if (2..=8).contains(&qlen) && query.chars().all(|c| c.is_alphanumeric()) {
        let name = game
            .get("name")
            .map(val_to_string)
            .unwrap_or_default()
            .trim()
            .to_owned();
        if !name.is_empty() {
            let words: Vec<&str> = ALNUM_RE.find_iter(&name).map(|m| m.as_str()).collect();
            let acronym: String = words
                .iter()
                .filter_map(|w| w.chars().next())
                .collect::<String>()
                .to_lowercase();
            if query == acronym || acronym.contains(&query) {
                return true;
            }
            if let Some(first) = words.first()
                && matches!(first.to_lowercase().as_str(), "the" | "a" | "an")
            {
                let sub_acronym: String = words[1..]
                    .iter()
                    .filter_map(|w| w.chars().next())
                    .collect::<String>()
                    .to_lowercase();
                if query == sub_acronym || sub_acronym.contains(&query) {
                    return true;
                }
            }
        }
    }
    false
}

fn matches_rule_value(game: &Map<String, Value>, field: &str, value: &Value) -> bool {
    let single = |k: &str, v: Value| {
        let mut m = Map::new();
        m.insert(k.to_owned(), v);
        m
    };
    match field {
        "query" => matches_query(game, &single("query", value.clone())),
        "source" => matches_source(game, &single("source", Value::Array(vec![value.clone()]))),
        "achievements_source" => matches_achievements_source(
            game,
            &single("achievements_source", Value::Array(vec![value.clone()])),
        ),
        "region" | "genre" | "developer" | "publisher" => text_matches(game.get(field), value),
        "platform" => {
            game.get("platform")
                .map(val_to_string)
                .unwrap_or_default()
                .to_lowercase()
                == val_to_string(value).to_lowercase()
        }
        "esrb" => {
            game.get("esrb")
                .map(val_to_string)
                .unwrap_or_default()
                .to_lowercase()
                == val_to_string(value).to_lowercase()
        }
        "progress" => matches_esrb_progress(game, &single("progress", value.clone())),
        "installed" => matches_installed_state(game, &single("installed", value.clone())),
        "favorite" | "hidden" | "has_saves" | "has_achievements" | "has_missing_media"
        | "has_highscores" => matches_booleans(game, &single(field, value.clone())),
        "vertical" | "requires_chd" => truthy(game.get(field)) == truthy(Some(value)),
        _ => false,
    }
}

pub fn game_matches_exclusions(game: &Value, exclude: Option<&Value>) -> bool {
    let Some(gobj) = game.as_object() else {
        return false;
    };
    let Some(eobj) = exclude.and_then(Value::as_object) else {
        return false;
    };
    for (field, raw) in eobj {
        let values: Vec<&Value> = match raw.as_array() {
            Some(a) => a.iter().collect(),
            None => vec![raw],
        };
        for val in values {
            if matches_rule_value(gobj, field, val) {
                return true;
            }
        }
    }
    false
}

pub fn game_matches_rules(game: &Value, rules: &Value) -> bool {
    let (Some(gobj), Some(robj)) = (game.as_object(), rules.as_object()) else {
        return true;
    };
    let matched = matches_platform(gobj, robj)
        && matches_view(gobj, robj)
        && matches_esrb_progress(gobj, robj)
        && matches_booleans(gobj, robj)
        && matches_installed_state(gobj, robj)
        && matches_text_fields(gobj, robj)
        && matches_source(gobj, robj)
        && matches_achievements_source(gobj, robj)
        && matches_query(gobj, robj);
    matched && !game_matches_exclusions(game, robj.get("exclude"))
}

pub fn filter_games(
    games: &[Value],
    rules: &Value,
    platform_category_fn: Option<&dyn Fn(&Value) -> String>,
) -> Vec<Value> {
    let category = rules
        .get("platform_category")
        .map(val_to_string)
        .unwrap_or_default()
        .trim()
        .to_owned();
    let mut out = Vec::new();
    for game in games {
        if !category.is_empty()
            && category != "all"
            && let Some(cat_fn) = platform_category_fn
            && cat_fn(game) != category
        {
            continue;
        }
        if game_matches_rules(game, rules) {
            out.push(game.clone());
        }
    }
    out
}

pub fn explorer_facets(games: &[Value], field: &str, limit: usize) -> Vec<Value> {
    if !EXPLORER_FIELDS.contains(&field) {
        return Vec::new();
    }
    let mut counts: HashMap<String, usize> = HashMap::new();
    for game in games {
        if truthy(game.get("hidden")) {
            continue;
        }
        match field {
            "genre" => {
                let raw = game.get("genre").map(val_to_string).unwrap_or_default();
                for part in raw.split(',') {
                    let label = part.trim();
                    if !label.is_empty() {
                        *counts.entry(label.to_owned()).or_insert(0) += 1;
                    }
                }
            }
            "developer" | "publisher" => {
                let label = game
                    .get(field)
                    .map(val_to_string)
                    .unwrap_or_default()
                    .trim()
                    .to_owned();
                if !label.is_empty() {
                    *counts.entry(label).or_insert(0) += 1;
                }
            }
            "platform" => {
                let raw = game
                    .get("platform")
                    .map(val_to_string)
                    .unwrap_or_default()
                    .trim()
                    .to_owned();
                let label = if raw.is_empty() {
                    "Unspecified".to_owned()
                } else {
                    raw
                };
                *counts.entry(label).or_insert(0) += 1;
            }
            "progress" => {
                let raw = game
                    .get("progress")
                    .map(val_to_string)
                    .unwrap_or_default()
                    .trim()
                    .to_owned();
                let label = if raw.is_empty() {
                    "Unplayed".to_owned()
                } else {
                    raw
                };
                *counts.entry(label).or_insert(0) += 1;
            }
            "esrb" => {
                let raw = game
                    .get("esrb")
                    .map(val_to_string)
                    .unwrap_or_default()
                    .trim()
                    .to_owned();
                let label = if raw.is_empty() {
                    "Unrated".to_owned()
                } else {
                    raw
                };
                *counts.entry(label).or_insert(0) += 1;
            }
            _ => {}
        }
    }
    let mut items: Vec<(String, usize)> = counts.into_iter().collect();
    items.sort_by(|a, b| {
        b.1.cmp(&a.1)
            .then_with(|| a.0.to_lowercase().cmp(&b.0.to_lowercase()))
    });
    items
        .into_iter()
        .take(limit)
        .map(|(value, count)| json!({"value": value, "count": count}))
        .collect()
}

pub fn chip_label(key: &str) -> Option<&'static str> {
    match key {
        "platform" => Some("Platform"),
        "genre" => Some("Genre"),
        "view" => Some("View"),
        "esrb" => Some("ESRB"),
        "progress" => Some("Progress"),
        "developer" => Some("Developer"),
        "publisher" => Some("Publisher"),
        "favorite" => Some("Favorite"),
        "hidden" => Some("Hidden"),
        "installed" => Some("Installed"),
        "has_saves" => Some("Has saves"),
        "has_achievements" => Some("Has achievements"),
        "has_missing_media" => Some("Missing media"),
        "has_highscores" => Some("Has high scores"),
        "vertical" => Some("Vertical"),
        "requires_chd" => Some("Requires CHD"),
        "query" => Some("Search"),
        _ => None,
    }
}

pub fn rules_to_chips(rules: &Map<String, Value>) -> Vec<Value> {
    let mut chips = Vec::new();
    for (key, value) in rules {
        let Some(label) = chip_label(key) else {
            continue;
        };
        let display = match value {
            Value::Bool(b) => {
                if *b {
                    "Yes".to_owned()
                } else {
                    "No".to_owned()
                }
            }
            other => val_to_string(other),
        };
        chips.push(json!({
            "key": key,
            "label": label,
            "value": value,
            "display": display,
        }));
    }
    chips
}

pub fn chips_to_rules(chips: &[Value]) -> Map<String, Value> {
    let mut rules = Map::new();
    let bool_set: HashSet<&str> = BOOL_RULE_FIELDS.iter().copied().collect();
    for chip in chips {
        let Some(obj) = chip.as_object() else {
            continue;
        };
        let key = obj
            .get("key")
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or("");
        if key.is_empty() || chip_label(key).is_none() {
            continue;
        }
        let value = obj.get("value");
        if bool_set.contains(key) {
            rules.insert(key.to_owned(), Value::Bool(truthy(value)));
        } else if key == "installed" {
            let text = value
                .map(val_to_string)
                .unwrap_or_default()
                .trim()
                .to_owned();
            rules.insert(
                key.to_owned(),
                Value::String(if text.is_empty() {
                    "all".to_owned()
                } else {
                    text
                }),
            );
        } else {
            let text = value
                .map(val_to_string)
                .unwrap_or_default()
                .trim()
                .to_owned();
            if !text.is_empty() && text != "all" {
                rules.insert(key.to_owned(), Value::String(text));
            }
        }
    }
    rules
}
