//! Duplicate detection and reversible merge (`pkg/parity/parity_duplicates.py`).

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::LazyLock;

use openbox_imports::identity::detect_duplicate_identities;
use openbox_store::prune_trash;
use regex::Regex;
use serde_json::{Map, Value, json};

use crate::saves::expand_user_path;

pub const MAX_GROUPS: usize = 100;
pub const MAX_GROUP_GAMES: usize = 25;
pub const MAX_LIST_ITEMS: usize = 200;
pub const MAX_TITLE_LENGTH: usize = 200;

pub const KIND_IDENTITY: &str = "identity";
pub const KIND_PATH: &str = "path";
pub const KIND_TITLE: &str = "title";

pub const MEDIA_UNION_FIELDS: &[&str] = &[
    "cover",
    "background",
    "fanart",
    "banner",
    "clear_logo",
    "icon",
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
    "video_snap",
    "video_theme",
    "video_trailer",
];

pub const LIST_UNION_FIELDS: &[&str] = &[
    "tags",
    "save_paths",
    "applications",
    "versions",
    "documents",
    "alternate_names",
    "screenshots",
];

pub const SCALAR_FILL_FIELDS: &[&str] = &[
    "rating",
    "year",
    "developer",
    "publisher",
    "genre",
    "esrb",
    "progress",
    "release_date",
    "notes",
    "description",
    "cover_source",
];

static BRACKET_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"[\(\[\{][^\)\]\}]{0,40}[\)\]\}]").unwrap());
static TRAILING_MARKER_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"(?:[\s._-]+(?:usa|europe|japan|revision|rev(?:\s+\d+)?|v\d+(?:\.\d+)*|disc\s*\d+|disk\s*\d+|en|jp|fr|de|es|pt|beta|proto|demo|unl|prg\d*))+\s*$",
    )
    .unwrap()
});
static NON_ALNUM_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[^a-z0-9]+").unwrap());

pub fn normalize_title(value: &str) -> String {
    let text = value.to_lowercase();
    let text = BRACKET_RE.replace_all(&text, " ");
    let text = TRAILING_MARKER_RE.replace_all(&text, " ");
    let text = NON_ALNUM_RE.replace_all(&text, " ");
    let joined = text.split_whitespace().collect::<Vec<_>>().join(" ");
    joined.chars().take(MAX_TITLE_LENGTH).collect()
}

pub fn normalize_path(value: &str) -> String {
    let raw = value.trim();
    if raw.is_empty() {
        return String::new();
    }
    let exp = expand_user_path(raw);
    // Clean lexical path normalization without hitting the filesystem
    let mut parts: Vec<String> = Vec::new();
    let s = exp.to_string_lossy().replace('\\', "/");
    let leading_slash = s.starts_with('/');
    for seg in s.split('/') {
        match seg {
            "" | "." => {}
            ".." => {
                if !parts.is_empty() && parts.last().map(String::as_str) != Some("..") {
                    parts.pop();
                } else if !leading_slash {
                    parts.push("..".to_owned());
                }
            }
            other => parts.push(other.to_owned()),
        }
    }
    let joined = if leading_slash {
        format!("/{}", parts.join("/"))
    } else if parts.is_empty() {
        ".".to_owned()
    } else {
        parts.join("/")
    };
    joined.to_lowercase()
}

fn sessions_by_game(state: &Value) -> HashMap<String, usize> {
    let mut counts = HashMap::new();
    if let Some(history) = state.get("history").and_then(Value::as_array) {
        for entry in history.iter().take(5000) {
            if let Some(gid) = entry
                .get("game_id")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
            {
                *counts.entry(gid.to_owned()).or_insert(0) += 1;
            }
        }
    }
    counts
}

fn media_count(game: &Value) -> usize {
    MEDIA_UNION_FIELDS
        .iter()
        .filter(|f| {
            game.get(**f)
                .and_then(Value::as_str)
                .is_some_and(|s| !s.trim().is_empty())
        })
        .count()
}

pub fn game_row(index: usize, game: &Value, session_counts: &HashMap<String, usize>) -> Value {
    let gid = game.get("game_id").and_then(Value::as_str).unwrap_or("");
    json!({
        "id": index,
        "game_id": gid,
        "name": game.get("name").and_then(Value::as_str).unwrap_or(""),
        "platform": game.get("platform").and_then(Value::as_str).unwrap_or(""),
        "path": game.get("path").and_then(Value::as_str).unwrap_or(""),
        "playtime_seconds": game.get("playtime_seconds").and_then(Value::as_i64).unwrap_or(0),
        "sessions": session_counts.get(gid).copied().unwrap_or(0),
        "achievements": game.get("ra_achievements_earned").and_then(Value::as_i64).unwrap_or(0),
        "media": media_count(game),
        "favorite": game.get("favorite").and_then(Value::as_bool).unwrap_or(false),
        "progress": game.get("progress").and_then(Value::as_str).unwrap_or(""),
    })
}

fn make_group(
    kind: &str,
    key: &str,
    entries: &[(usize, &Value)],
    session_counts: &HashMap<String, usize>,
) -> Value {
    let first = entries[0].1;
    let rows: Vec<Value> = entries
        .iter()
        .map(|(idx, g)| game_row(*idx, g, session_counts))
        .collect();
    json!({
        "kind": kind,
        "key": key,
        "title": first.get("name").and_then(Value::as_str).unwrap_or(""),
        "platform": first.get("platform").and_then(Value::as_str).unwrap_or(""),
        "games": rows,
    })
}

fn identity_groups(games: &[Value]) -> Vec<(String, Vec<(usize, &Value)>)> {
    let mut id_to_indexes: HashMap<String, Vec<usize>> = HashMap::new();
    for (idx, g) in games.iter().enumerate() {
        if !g.is_object() {
            continue;
        }
        let key = g
            .get("game_id")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .or_else(|| g.get("id").and_then(Value::as_i64).map(|n| n.to_string()))
            .unwrap_or_default();
        if !key.is_empty() {
            id_to_indexes.entry(key).or_default().push(idx);
        }
    }
    let mut groups = Vec::new();
    for entry in detect_duplicate_identities(games, false) {
        let mut members = Vec::new();
        if let Some(arr) = entry.get("games").and_then(Value::as_array) {
            for gid_v in arr {
                let k = match gid_v {
                    Value::String(s) => s.clone(),
                    Value::Number(n) => n.to_string(),
                    _ => continue,
                };
                if let Some(list) = id_to_indexes.get_mut(&k)
                    && !list.is_empty()
                {
                    let idx = list.remove(0);
                    members.push((idx, &games[idx]));
                }
            }
        }
        if members.len() >= 2 {
            let ident = entry
                .get("identity")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            groups.push((ident, members));
        }
    }
    groups
}

pub fn find_duplicates(state: &Value, limit: usize, include_title: bool) -> Value {
    let games = state
        .get("games")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let session_counts = sessions_by_game(state);
    let mut path_buckets: BTreeMap<String, Vec<(usize, &Value)>> = BTreeMap::new();
    let mut title_buckets: BTreeMap<String, Vec<(usize, &Value)>> = BTreeMap::new();

    for (idx, g) in games.iter().enumerate() {
        if !g.is_object() {
            continue;
        }
        let mut pkey = normalize_path(g.get("path").and_then(Value::as_str).unwrap_or(""));
        let rom_name = g
            .get("rom_name")
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or("")
            .to_lowercase();
        if !pkey.is_empty() && !rom_name.is_empty() {
            pkey = format!("{pkey}\0{rom_name}");
        }
        if !pkey.is_empty() {
            path_buckets.entry(pkey).or_default().push((idx, g));
        }
        if include_title {
            let norm_name = normalize_title(g.get("name").and_then(Value::as_str).unwrap_or(""));
            if !norm_name.is_empty() {
                let plat = g
                    .get("platform")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_lowercase();
                let tkey = format!("{norm_name}\0{plat}");
                title_buckets.entry(tkey).or_default().push((idx, g));
            }
        }
    }

    let id_groups = identity_groups(games);
    let mut groups: Vec<Value> = Vec::new();
    let mut claimed: HashSet<usize> = HashSet::new();
    let mut truncated = false;

    let mut claim = |kind: &str, key: &str, entries: &[(usize, &Value)]| -> bool {
        let filtered: Vec<(usize, &Value)> = entries
            .iter()
            .copied()
            .filter(|(i, _)| !claimed.contains(i))
            .collect();
        if filtered.len() < 2 {
            return false;
        }
        if groups.len() >= limit {
            return true;
        }
        let slice = &filtered[..filtered.len().min(MAX_GROUP_GAMES)];
        groups.push(make_group(kind, key, slice, &session_counts));
        for (i, _) in slice {
            claimed.insert(*i);
        }
        false
    };

    for (ident_key, members) in &id_groups {
        if claim(KIND_IDENTITY, ident_key, members) {
            truncated = true;
            break;
        }
    }
    if !truncated {
        for (k, v) in &path_buckets {
            if claim(KIND_PATH, k, v) {
                truncated = true;
                break;
            }
        }
    }
    if !truncated && include_title {
        for (k, v) in &title_buckets {
            if claim(KIND_TITLE, k, v) {
                truncated = true;
                break;
            }
        }
    }

    let oversized = id_groups.iter().any(|(_, m)| m.len() > MAX_GROUP_GAMES)
        || path_buckets.values().any(|v| v.len() > MAX_GROUP_GAMES)
        || title_buckets.values().any(|v| v.len() > MAX_GROUP_GAMES);
    json!({
        "groups": groups,
        "count": groups.len(),
        "truncated": truncated || oversized,
    })
}

pub fn primary_choice(rows: &[Value]) -> Option<Value> {
    rows.iter()
        .filter(|r| r.is_object())
        .max_by_key(|r| {
            (
                r.get("sessions").and_then(Value::as_i64).unwrap_or(0),
                r.get("playtime_seconds")
                    .and_then(Value::as_i64)
                    .unwrap_or(0),
                r.get("achievements").and_then(Value::as_i64).unwrap_or(0),
                r.get("media").and_then(Value::as_i64).unwrap_or(0),
                -(r.get("id").and_then(Value::as_i64).unwrap_or(0)),
            )
        })
        .cloned()
}

fn as_list(val: Option<&Value>) -> Vec<Value> {
    match val {
        None | Some(Value::Null) => Vec::new(),
        Some(Value::String(s)) if s.is_empty() => Vec::new(),
        Some(Value::Array(arr)) => arr.clone(),
        Some(Value::Object(m)) if m.is_empty() => Vec::new(),
        Some(other) => vec![other.clone()],
    }
}

fn union_lists(groups: &[Vec<Value>]) -> Vec<Value> {
    let mut merged = Vec::new();
    let mut seen = HashSet::new();
    for group in groups {
        for item in group {
            let key = if let Some(m) = item.as_object() {
                let mut parts: Vec<String> = ["path", "name", "id", "url"]
                    .iter()
                    .map(|k| m.get(*k).and_then(Value::as_str).unwrap_or("").to_owned())
                    .collect();
                parts.sort();
                parts.join("|")
            } else {
                match item {
                    Value::String(s) => s.clone(),
                    other => other.to_string(),
                }
            };
            if seen.insert(key) {
                merged.push(item.clone());
                if merged.len() >= MAX_LIST_ITEMS {
                    return merged;
                }
            }
        }
    }
    merged
}

pub fn merge_plan(state: &Value, indexes: &[usize]) -> Result<Value, String> {
    let Some(games) = state.get("games").and_then(Value::as_array) else {
        return Err("State has no games list.".to_owned());
    };
    let mut unique = Vec::new();
    for &idx in indexes {
        if idx < games.len() && !unique.contains(&idx) {
            unique.push(idx);
        }
    }
    if unique.len() < 2 {
        return Err("A merge needs at least two records.".to_owned());
    }
    let session_counts = sessions_by_game(state);
    let rows: Vec<Value> = unique
        .iter()
        .filter(|&&i| games[i].is_object())
        .map(|&i| game_row(i, &games[i], &session_counts))
        .collect();
    let primary_row =
        primary_choice(&rows).ok_or_else(|| "A merge needs two valid records.".to_owned())?;
    let primary_index = primary_row["id"].as_u64().unwrap_or(0) as usize;
    let primary = &games[primary_index];
    let duplicates: Vec<usize> = unique.into_iter().filter(|&i| i != primary_index).collect();

    let mut changes = Map::new();
    for field in MEDIA_UNION_FIELDS {
        let cur = primary
            .get(*field)
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or("");
        if !cur.is_empty() {
            continue;
        }
        for &idx in &duplicates {
            let val = games[idx]
                .get(*field)
                .and_then(Value::as_str)
                .map(str::trim)
                .unwrap_or("");
            if !val.is_empty() {
                changes.insert((*field).into(), json!({"value": val, "source": idx}));
                break;
            }
        }
    }
    for field in LIST_UNION_FIELDS {
        let orig = as_list(primary.get(*field));
        let mut vals = union_lists(std::slice::from_ref(&orig));
        for &idx in &duplicates {
            let raw = as_list(games[idx].get(*field));
            if !raw.is_empty() {
                vals = union_lists(&[vals, raw]);
            }
        }
        if !vals.is_empty() && vals != orig {
            changes.insert(
                (*field).into(),
                json!({"value": vals, "source": primary_index, "merged": true}),
            );
        }
    }
    for field in SCALAR_FILL_FIELDS {
        let has_cur = match primary.get(*field) {
            None | Some(Value::Null) => false,
            Some(Value::String(s)) => !s.trim().is_empty(),
            Some(Value::Number(n)) => n.as_f64().is_some_and(|f| f != 0.0),
            Some(Value::Bool(b)) => *b,
            Some(Value::Array(a)) => !a.is_empty(),
            Some(Value::Object(m)) => !m.is_empty(),
        };
        if has_cur {
            continue;
        }
        for &idx in &duplicates {
            if let Some(val) = games[idx].get(*field) {
                let empty = val.is_null()
                    || val.as_str().is_some_and(str::is_empty)
                    || val.as_array().is_some_and(Vec::is_empty)
                    || val.as_object().is_some_and(Map::is_empty);
                if !empty {
                    changes.insert((*field).into(), json!({"value": val, "source": idx}));
                    break;
                }
            }
        }
    }

    let mut changed_fields: Vec<String> = changes.keys().cloned().collect();
    changed_fields.sort();
    let absorbed: Vec<Value> = rows
        .into_iter()
        .filter(|r| r["id"].as_u64() != Some(primary_index as u64))
        .collect();

    Ok(json!({
        "primary": primary_row,
        "absorbed": absorbed,
        "changes": Value::Object(changes),
        "changed_fields": changed_fields,
        "reason": {
            "sessions": primary_row["sessions"],
            "playtime_seconds": primary_row["playtime_seconds"],
            "achievements": primary_row["achievements"],
            "media": primary_row["media"],
        },
    }))
}

pub fn apply_merge(
    state: &mut Value,
    indexes: &[usize],
    mut trash_factory: Option<&mut dyn FnMut(&Value) -> Value>,
) -> Result<Value, String> {
    let plan = merge_plan(state, indexes)?;
    let primary_index = plan["primary"]["id"].as_u64().unwrap_or(0) as usize;
    if let Some(changes) = plan.get("changes").and_then(Value::as_object)
        && let Some(primary) = state
            .get_mut("games")
            .and_then(Value::as_array_mut)
            .and_then(|g| g.get_mut(primary_index))
            .and_then(Value::as_object_mut)
    {
        for (field, change) in changes {
            if let Some(val) = change.get("value") {
                primary.insert(field.clone(), val.clone());
            }
        }
    }

    let mut dup_indexes: Vec<usize> = plan
        .get("absorbed")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(|r| r.get("id").and_then(Value::as_u64).map(|n| n as usize))
                .collect()
        })
        .unwrap_or_default();
    dup_indexes.sort_by(|a, b| b.cmp(a));

    let mut trashed = Vec::new();
    for &idx in &dup_indexes {
        let game = match state
            .get("games")
            .and_then(Value::as_array)
            .and_then(|g| g.get(idx))
            .cloned()
        {
            Some(g) => g,
            None => continue,
        };
        if let Some(factory) = trash_factory.as_deref_mut() {
            let entry = factory(&game);
            if entry.is_object() {
                trashed.push(entry);
            }
        }
        let mut member_keys = HashSet::new();
        if let Some(gid) = game
            .get("game_id")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
        {
            member_keys.insert(gid.to_owned());
        }
        if let Some(id_n) = game.get("id").and_then(Value::as_i64) {
            member_keys.insert(id_n.to_string());
        }
        if let Some(playlists) = state.get_mut("playlists").and_then(Value::as_array_mut) {
            for pl in playlists.iter_mut() {
                if pl.get("type").and_then(Value::as_str) != Some("manual") {
                    continue;
                }
                if let Some(members) = pl.get_mut("members").and_then(Value::as_array_mut) {
                    members.retain(|m| {
                        let s = match m {
                            Value::String(s) => s.clone(),
                            other => other.to_string(),
                        };
                        !member_keys.contains(&s)
                    });
                }
            }
        }
        if let Some(games) = state.get_mut("games").and_then(Value::as_array_mut)
            && idx < games.len()
        {
            games.remove(idx);
        }
    }

    let trashed_names: Vec<Value> = trashed
        .iter()
        .map(|e| {
            Value::String(
                e.get("name")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned(),
            )
        })
        .collect();
    if !trashed.is_empty()
        && let Some(root) = state.as_object_mut()
    {
        let mut trash_list = root
            .get("trash")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        trash_list.extend(trashed);
        let pruned = prune_trash(&trash_list, None);
        root.insert("trash".into(), Value::Array(pruned));
    }

    Ok(json!({
        "merged": dup_indexes.len(),
        "primary": plan["primary"],
        "trashed": trashed_names,
        "undo": "trash",
        "changed_fields": plan["changed_fields"],
    }))
}
