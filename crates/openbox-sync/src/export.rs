//! Library export as redaction-safe JSON or CSV (`pkg/parity/parity_export.py`).

use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;
use std::time::UNIX_EPOCH;

use jiff::Timestamp;
use regex::Regex;
use serde_json::{Map, Value, json};

pub const EXPORT_FORMATS: &[&str] = &["json", "csv"];
pub const EXPORT_SCOPES: &[&str] = &["all", "platform", "playlist"];
pub const EXPORT_DIR_NAME: &str = "exports";
pub const EXPORT_PREFIX: &str = "openbox-library-";
pub const EXPORT_KEEP: usize = 10;

static EXPORT_NAME_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^openbox-library-[0-9]{8}-[0-9]{6}(?:-[0-9]+)?\.(json|csv)$").unwrap()
});

pub const EXPORT_GAME_FIELDS: &[&str] = &[
    "game_id",
    "name",
    "sort_title",
    "alternate_names",
    "manual_entry",
    "platform",
    "genre",
    "year",
    "developer",
    "publisher",
    "series",
    "region",
    "esrb",
    "max_players",
    "description",
    "notes",
    "wikipedia_url",
    "progress",
    "rating",
    "user_rating",
    "play_count",
    "playtime_seconds",
    "manual_playtime_seconds",
    "manual_sessions",
    "last_played",
    "favorite",
    "hidden",
    "broken",
    "portable",
    "installed",
    "added_at",
    "source",
    "steam_app_id",
    "heroic_app_id",
    "lutris_id",
    "gameyfin_id",
    "igdb_id",
    "ra_game_id",
    "launchbox_db_id",
    "install_dir",
    "gog_id",
    "ea_id",
    "ubisoft_id",
    "price_paid_cents",
    "price_currency",
    "disc_count",
    "rom_name",
    "clone_of",
    "set_type",
];

pub const EXPORT_MEDIA_FIELDS: &[&str] = &[
    "cover",
    "background",
    "clear_logo",
    "fanart",
    "banner",
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
    "video_snap",
    "video_theme",
    "video_trailer",
    "video_recording",
    "music",
    "screenshots",
];

pub fn build_export_games(
    state: &Value,
    scope: &str,
    scope_name: &str,
) -> Result<Vec<Value>, String> {
    let Some(games) = state.get("games").and_then(Value::as_array) else {
        return Ok(Vec::new());
    };
    if !EXPORT_SCOPES.contains(&scope) {
        return Err("Unknown export scope.".to_owned());
    }
    if scope == "all" {
        return Ok(games.iter().filter(|g| g.is_object()).cloned().collect());
    }
    let key = scope_name.trim();
    if key.is_empty() {
        return Err(format!("Export scope {scope} requires a name."));
    }
    if scope == "platform" {
        return Ok(games
            .iter()
            .filter(|g| g.get("platform").and_then(Value::as_str) == Some(key))
            .cloned()
            .collect());
    }
    let playlist = state
        .get("playlists")
        .and_then(Value::as_array)
        .and_then(|arr| {
            arr.iter()
                .find(|p| p.get("name").and_then(Value::as_str) == Some(key))
        })
        .ok_or_else(|| format!("Playlist not found: {key}"))?;
    let mut member_strs: HashSet<String> = HashSet::new();
    let mut member_ints: HashSet<i64> = HashSet::new();
    if let Some(members) = playlist.get("members").and_then(Value::as_array) {
        for m in members {
            if let Some(s) = m.as_str() {
                member_strs.insert(s.to_owned());
            } else if let Some(n) = m.as_i64() {
                member_ints.insert(n);
            }
        }
    }
    Ok(games
        .iter()
        .filter(|g| {
            g.get("game_id")
                .and_then(Value::as_str)
                .is_some_and(|s| member_strs.contains(s))
                || g.get("id")
                    .and_then(Value::as_i64)
                    .is_some_and(|n| member_ints.contains(&n))
        })
        .cloned()
        .collect())
}

fn format_export_val(v: Option<&Value>) -> Value {
    match v {
        None | Some(Value::Null) => Value::String(String::new()),
        Some(Value::Array(arr)) => {
            let parts: Vec<String> = arr
                .iter()
                .map(|x| match x {
                    Value::String(s) => s.clone(),
                    other => other.to_string(),
                })
                .collect();
            Value::String(parts.join(";"))
        }
        Some(other) => other.clone(),
    }
}

pub fn export_row(game: &Value, include_media_paths: bool) -> Map<String, Value> {
    let mut row = Map::new();
    for field in EXPORT_GAME_FIELDS {
        row.insert((*field).into(), format_export_val(game.get(*field)));
    }
    if include_media_paths {
        for field in EXPORT_MEDIA_FIELDS {
            row.insert((*field).into(), format_export_val(game.get(*field)));
        }
    }
    row
}

pub fn export_rows(games: &[Value], include_media_paths: bool) -> Vec<Value> {
    games
        .iter()
        .filter(|g| g.is_object())
        .map(|g| Value::Object(export_row(g, include_media_paths)))
        .collect()
}

fn csv_cell(v: Option<&Value>) -> String {
    match v {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Bool(b)) => if *b { "True" } else { "False" }.to_owned(),
        Some(other) => other.to_string(),
    }
}

pub fn write_export(
    data_dir: &Path,
    rows: &[Value],
    fmt: &str,
    include_media_paths: bool,
) -> Result<PathBuf, String> {
    if !EXPORT_FORMATS.contains(&fmt) {
        return Err("Unknown export format.".to_owned());
    }
    let export_dir = data_dir.join(EXPORT_DIR_NAME);
    fs::create_dir_all(&export_dir).map_err(|e| e.to_string())?;
    let now = Timestamp::now();
    let dt = jiff::tz::TimeZone::UTC.to_datetime(now);
    let stamp = format!(
        "{:04}{:02}{:02}-{:02}{:02}{:02}",
        dt.year(),
        dt.month(),
        dt.day(),
        dt.hour(),
        dt.minute(),
        dt.second()
    );
    let iso = format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
        dt.year(),
        dt.month(),
        dt.day(),
        dt.hour(),
        dt.minute(),
        dt.second()
    );
    let mut path = export_dir.join(format!("{EXPORT_PREFIX}{stamp}.{fmt}"));
    let mut counter = 1usize;
    while path.exists() {
        counter += 1;
        path = export_dir.join(format!("{EXPORT_PREFIX}{stamp}-{counter}.{fmt}"));
    }
    if fmt == "json" {
        let payload = json!({
            "application": "OpenBox",
            "kind": "library-export",
            "exported_at": iso,
            "count": rows.len(),
            "include_media_paths": include_media_paths,
            "games": rows,
        });
        let text = serde_json::to_string_pretty(&payload).map_err(|e| e.to_string())?;
        fs::write(&path, text).map_err(|e| e.to_string())?;
    } else {
        let mut fields: Vec<&str> = EXPORT_GAME_FIELDS.to_vec();
        if include_media_paths {
            fields.extend_from_slice(EXPORT_MEDIA_FIELDS);
        }
        let mut wtr = csv::Writer::from_writer(Vec::new());
        wtr.write_record(&fields).map_err(|e| e.to_string())?;
        for r in rows {
            let record: Vec<String> = fields.iter().map(|f| csv_cell(r.get(*f))).collect();
            wtr.write_record(&record).map_err(|e| e.to_string())?;
        }
        let data = wtr.into_inner().map_err(|e| e.to_string())?;
        fs::write(&path, data).map_err(|e| e.to_string())?;
    }
    prune_exports(&export_dir, EXPORT_KEEP);
    Ok(path)
}

pub fn prune_exports(export_dir: &Path, keep: usize) {
    let Ok(rd) = fs::read_dir(export_dir) else {
        return;
    };
    let mut files: Vec<(u64, String, PathBuf)> = rd
        .flatten()
        .map(|e| e.path())
        .filter(|p| {
            p.is_file()
                && p.file_name()
                    .and_then(|s| s.to_str())
                    .is_some_and(|n| EXPORT_NAME_RE.is_match(n))
        })
        .map(|p| {
            let mtime = fs::metadata(&p)
                .ok()
                .and_then(|m| m.modified().ok())
                .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
                .map(|d| d.as_secs())
                .unwrap_or(0);
            let name = p
                .file_name()
                .and_then(|s| s.to_str())
                .unwrap_or("")
                .to_owned();
            (mtime, name, p)
        })
        .collect();
    files.sort_by(|a, b| a.0.cmp(&b.0).then_with(|| a.1.cmp(&b.1)));
    let drop_count = files.len().saturating_sub(keep);
    for (_, _, stale) in files.into_iter().take(drop_count) {
        let _ = fs::remove_file(&stale);
    }
}

pub fn list_exports(data_dir: &Path) -> Vec<Value> {
    let export_dir = data_dir.join(EXPORT_DIR_NAME);
    let Ok(rd) = fs::read_dir(&export_dir) else {
        return Vec::new();
    };
    let mut items: Vec<(String, u64, i64)> = Vec::new();
    for entry in rd.flatten() {
        let p = entry.path();
        if !p.is_file() {
            continue;
        }
        let Some(name) = p.file_name().and_then(|s| s.to_str()) else {
            continue;
        };
        if !EXPORT_NAME_RE.is_match(name) {
            continue;
        }
        let Ok(meta) = fs::metadata(&p) else {
            continue;
        };
        let mtime = meta
            .modified()
            .ok()
            .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
            .map(|d| d.as_secs() as i64)
            .unwrap_or(0);
        items.push((name.to_owned(), meta.len(), mtime));
    }
    items.sort_by(|a, b| b.0.cmp(&a.0));
    items
        .into_iter()
        .map(|(name, bytes, modified)| json!({"name": name, "bytes": bytes, "modified": modified}))
        .collect()
}

pub fn approved_export_file(data_dir: &Path, name: &str) -> Option<PathBuf> {
    let candidate = name.trim();
    if !EXPORT_NAME_RE.is_match(candidate) {
        return None;
    }
    let dir = data_dir.join(EXPORT_DIR_NAME);
    let path = dir.join(candidate);
    let resolved_dir = fs::canonicalize(&dir).ok()?;
    let resolved_parent = fs::canonicalize(path.parent()?).ok()?;
    if resolved_parent != resolved_dir || !path.is_file() {
        return None;
    }
    Some(path)
}
