//! Bulk find/replace, bulk wizard, custom fields, ROM ranking, and premium import helpers
//! (`pkg/parity/parity_bulk_edit.py` + `pkg/parity/parity_premium.py` subset).

use std::collections::{BTreeMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use openbox_core::ApiError;
use openbox_platform::{expand_user, join_command, which};
use regex::Regex;
use serde_json::{Map, Value, json};

pub const BULK_EDIT_FIELDS: &[&str] = &[
    "name",
    "platform",
    "genre",
    "developer",
    "publisher",
    "region",
    "source",
    "notes",
    "progress",
    "esrb",
];

pub const MAX_SELECTION: usize = 50_000;

pub const BULK_WIZARD_FIELDS: &[&str] = &[
    "platform",
    "genre",
    "progress",
    "rating",
    "user_rating",
    "favorite",
    "hidden",
    "esrb",
    "custom_fields",
    "reset_stats",
    "play_count",
    "playtime_seconds",
    "last_played",
];

pub const LIST_COLUMNS_DEFAULT: &[&str] = &[
    "name",
    "platform",
    "genre",
    "esrb",
    "progress",
    "last_played",
    "play_count",
];

pub const DEFAULT_PLATFORM_CATEGORIES: &[(&str, &str)] = &[
    ("Arcade", "Arcade"),
    ("MAME", "Arcade"),
    ("FinalBurn Neo", "Arcade"),
    ("NES", "Nintendo"),
    ("SNES", "Nintendo"),
    ("Nintendo 64", "Nintendo"),
    ("Game Boy", "Nintendo"),
    ("Game Boy Color", "Nintendo"),
    ("Game Boy Advance", "Nintendo"),
    ("Nintendo DS", "Nintendo"),
    ("Nintendo 3DS", "Nintendo"),
    ("GameCube", "Nintendo"),
    ("Wii", "Nintendo"),
    ("WiiWare", "Nintendo"),
    ("Wii U", "Nintendo"),
    ("Nintendo Switch", "Nintendo"),
    ("PlayStation", "Sony"),
    ("PlayStation 2", "Sony"),
    ("PlayStation 3", "Sony"),
    ("PSP", "Sony"),
    ("PlayStation Vita", "Sony"),
    ("Xbox", "Microsoft"),
    ("Xbox 360", "Microsoft"),
    ("PC", "Computer"),
    ("Windows", "Computer"),
    ("Linux", "Computer"),
    ("ScummVM", "Adventure"),
    ("DOS", "Computer"),
];

static BAD_TAGS_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)\((?:beta|proto|demo|sample|unl|pirate|hack|translation)\)")
        .expect("valid bad tags regex")
});
static VITA_ID_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Z0-9]{9}$").expect("valid vita id regex"));

pub fn find_replace(
    games: &mut [Value],
    ids: &[Value],
    field: &str,
    find: &str,
    replacement: &str,
    dry_run: bool,
) -> Result<Value, ApiError> {
    let field = field.trim();
    if !BULK_EDIT_FIELDS.contains(&field) {
        return Err(ApiError::bad_request(
            "That field cannot be edited in bulk.",
        ));
    }
    if find.is_empty() {
        return Err(ApiError::bad_request("Find text is required."));
    }
    if find.chars().count() > 500 || replacement.chars().count() > 500 {
        return Err(ApiError::bad_request(
            "Find and replace text must be 500 characters or fewer.",
        ));
    }
    let selected: HashSet<String> = ids
        .iter()
        .map(|v| match v {
            Value::String(s) => s.clone(),
            Value::Number(n) => n.to_string(),
            other => other.to_string(),
        })
        .collect();
    if selected.is_empty() || selected.len() > MAX_SELECTION {
        return Err(ApiError::bad_request("Select between 1 and 50,000 games."));
    }
    let mut matches = Vec::new();
    let mut changed = 0usize;
    for game in games.iter_mut() {
        let Some(obj) = game.as_object_mut() else {
            continue;
        };
        let identity = obj
            .get("game_id")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .map(ToOwned::to_owned)
            .or_else(|| {
                obj.get("id")
                    .map(|v| v.to_string().trim_matches('"').to_owned())
            })
            .unwrap_or_default();
        if !selected.contains(&identity) {
            continue;
        }
        let before = obj
            .get(field)
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        if !before.contains(find) {
            continue;
        }
        let after = before.replace(find, replacement);
        let name = obj
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        if matches.len() < 100 {
            matches.push(json!({
                "game_id": identity,
                "name": name,
                "before": before,
                "after": after,
            }));
        }
        changed += 1;
        if !dry_run {
            obj.insert(field.to_owned(), Value::String(after));
        }
    }
    Ok(json!({
        "field": field,
        "matched": changed,
        "games": matches,
        "truncated": changed.saturating_sub(100),
        "applied": !dry_run,
    }))
}

pub fn bulk_wizard_changes(step_fields: &Value) -> Result<Map<String, Value>, ApiError> {
    let Some(obj) = step_fields.as_object() else {
        return Err(ApiError::bad_request(
            "Bulk wizard payload must be an object.",
        ));
    };
    if !obj.keys().all(|k| BULK_WIZARD_FIELDS.contains(&k.as_str())) {
        return Err(ApiError::bad_request(
            "Bulk wizard contains unsupported fields.",
        ));
    }
    Ok(obj.clone())
}

pub fn custom_field_defs(settings: &Value) -> Vec<Value> {
    let Some(defs) = settings.get("custom_field_defs").and_then(Value::as_array) else {
        return Vec::new();
    };
    let mut clean = Vec::new();
    for item in defs.iter().take(20) {
        let Some(obj) = item.as_object() else {
            continue;
        };
        let name = obj
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_owned();
        if name.is_empty() {
            continue;
        }
        let options: Vec<String> = match obj.get("options") {
            Some(Value::String(s)) => s
                .split(',')
                .map(|p| p.trim().to_owned())
                .filter(|p| !p.is_empty())
                .take(50)
                .collect(),
            Some(Value::Array(arr)) => arr
                .iter()
                .filter_map(Value::as_str)
                .map(|p| p.trim().to_owned())
                .filter(|p| !p.is_empty())
                .take(50)
                .collect(),
            _ => Vec::new(),
        };
        clean.push(json!({"name": name, "options": options}));
    }
    clean
}

pub fn normalize_custom_fields(game: &mut Map<String, Value>, defs: &[Value]) {
    let allowed: HashSet<String> = defs
        .iter()
        .filter_map(|d| d.get("name").and_then(Value::as_str).map(ToOwned::to_owned))
        .collect();
    let current = game
        .get("custom_fields")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let mut cleaned = Map::new();
    for key in allowed {
        if let Some(val) = current.get(&key) {
            let text = match val {
                Value::String(s) => s.trim().to_owned(),
                Value::Null => String::new(),
                other => other.to_string(),
            };
            if !text.is_empty() {
                cleaned.insert(key, Value::String(text));
            }
        }
    }
    game.insert("custom_fields".into(), Value::Object(cleaned));
}

pub fn platform_categories(settings: &Value) -> BTreeMap<String, String> {
    let mut merged: BTreeMap<String, String> = DEFAULT_PLATFORM_CATEGORIES
        .iter()
        .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
        .collect();
    if let Some(mapping) = settings
        .get("platform_categories")
        .and_then(Value::as_object)
    {
        for (k, v) in mapping {
            let kk = k.trim();
            let vv = v.as_str().unwrap_or("").trim();
            if !kk.is_empty() && !vv.is_empty() {
                merged.insert(kk.to_owned(), vv.to_owned());
            }
        }
    }
    merged
}

fn region_score(name: &str) -> i64 {
    let lowered = name.to_lowercase();
    for (token, score) in [
        ("world", 0),
        ("usa", 1),
        ("us", 1),
        ("english", 1),
        ("europe", 2),
        ("eur", 2),
        ("japan", 3),
        ("jpn", 3),
        ("asia", 4),
    ] {
        if lowered.contains(token) {
            return score;
        }
    }
    5
}

pub fn rom_quality_score(path: &Path) -> i64 {
    let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
    let mut score: i64 = 0;
    if BAD_TAGS_RE.is_match(name) {
        score += 100;
    }
    score += region_score(name);
    let ext = path
        .extension()
        .and_then(|e| e.to_str())
        .map(|e| format!(".{}", e.to_lowercase()))
        .unwrap_or_default();
    if matches!(ext.as_str(), ".chd" | ".cue" | ".m3u") {
        score -= 2;
    }
    if matches!(ext.as_str(), ".zip" | ".7z") {
        score += 1;
    }
    match fs::metadata(path) {
        Ok(meta) => {
            score -= ((meta.len() / (64 * 1024 * 1024)) as i64).min(5);
        }
        Err(_) => {
            score += 1000;
        }
    }
    score
}

pub fn rank_rom_group(paths: &[String]) -> Vec<String> {
    let mut ranked: Vec<PathBuf> = paths
        .iter()
        .map(PathBuf::from)
        .filter(|p| p.is_file())
        .collect();
    ranked.sort_by(|a, b| {
        let sa = rom_quality_score(a);
        let sb = rom_quality_score(b);
        let na = a
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("")
            .to_lowercase();
        let nb = b
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("")
            .to_lowercase();
        sa.cmp(&sb).then_with(|| na.cmp(&nb))
    });
    ranked
        .into_iter()
        .map(|p| p.to_string_lossy().into_owned())
        .collect()
}

pub fn pick_best_rom(paths: &[String]) -> String {
    rank_rom_group(paths).into_iter().next().unwrap_or_default()
}

pub fn resolve_vita_title(title: &str, title_id: &str, meta_title: &str) -> String {
    let t = title.trim();
    let tid = title_id.trim();
    if !t.is_empty() && t.to_lowercase() != tid.to_lowercase() && !VITA_ID_RE.is_match(t) {
        return t.to_owned();
    }
    if !meta_title.trim().is_empty() {
        return meta_title.trim().to_owned();
    }
    if !t.is_empty() {
        t.to_owned()
    } else if !tid.is_empty() {
        tid.to_owned()
    } else {
        "PlayStation Vita Game".to_owned()
    }
}

pub fn import_loose_arcade(folder: &str, command: &str) -> Result<Vec<Value>, ApiError> {
    let root = expand_user(Path::new(folder));
    if !root.is_dir() {
        return Err(ApiError::not_found("Arcade folder does not exist."));
    }
    let binary = which("hypseus")
        .or_else(|| which("singe"))
        .map(|p| p.to_string_lossy().into_owned());
    let default = if !command.is_empty() {
        command.to_owned()
    } else if let Some(b) = binary {
        join_command(&[b, "{path}".to_owned()])
    } else {
        String::new()
    };
    let mut entries: Vec<PathBuf> = fs::read_dir(&root)
        .map_err(|e| ApiError::bad_request(e.to_string()))?
        .filter_map(Result::ok)
        .map(|e| e.path())
        .collect();
    entries.sort();
    let mut games = Vec::new();
    for path in entries {
        if !path.is_file() {
            continue;
        }
        let ext = path
            .extension()
            .and_then(|e| e.to_str())
            .map(|e| format!(".{}", e.to_lowercase()))
            .unwrap_or_default();
        if !matches!(ext.as_str(), ".zip" | ".7z" | ".singe" | ".rom") {
            continue;
        }
        let stem = path
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_owned();
        let fname_lower = path
            .file_name()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_lowercase();
        let path_str = path.to_string_lossy().into_owned();
        let launch = if default.is_empty() {
            path_str.clone()
        } else {
            default.clone()
        };
        games.push(json!({
            "name": stem.replace('_', " "),
            "platform": "Arcade",
            "source": if fname_lower.contains("singe") { "Hypseus Singe" } else { "Loose Arcade" },
            "collection": "Arcade",
            "path": path_str,
            "launch": launch,
            "rom_name": stem,
        }));
    }
    Ok(games)
}

pub fn import_xbox360_folder(folder: &str, command: &str) -> Result<Vec<Value>, ApiError> {
    let root = expand_user(Path::new(folder));
    if !root.is_dir() {
        return Err(ApiError::not_found("Xbox 360 folder does not exist."));
    }
    let binary = which("xenia")
        .or_else(|| which("xenia-canary"))
        .map(|p| p.to_string_lossy().into_owned());
    let default = if !command.is_empty() {
        command.to_owned()
    } else if let Some(b) = binary {
        join_command(&[b, "--launch".to_owned(), "{path}".to_owned()])
    } else {
        String::new()
    };
    let mut all_files = Vec::new();
    let mut stack = vec![root];
    while let Some(dir) = stack.pop() {
        let Ok(rd) = fs::read_dir(&dir) else {
            continue;
        };
        for entry in rd.filter_map(Result::ok) {
            let p = entry.path();
            if p.is_dir() {
                stack.push(p);
            } else if p.is_file() {
                all_files.push(p);
            }
        }
    }
    all_files.sort();
    let mut games = Vec::new();
    for path in all_files {
        let fname = path
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("")
            .to_lowercase();
        let ext = path
            .extension()
            .and_then(|e| e.to_str())
            .map(|e| format!(".{}", e.to_lowercase()))
            .unwrap_or_default();
        if fname != "default.xex" && !matches!(ext.as_str(), ".xex" | ".xbe") {
            continue;
        }
        let title = if fname == "default.xex" {
            path.parent()
                .and_then(|p| p.file_name())
                .and_then(|n| n.to_str())
                .unwrap_or("")
        } else {
            path.file_stem().and_then(|s| s.to_str()).unwrap_or("")
        };
        let path_str = path.to_string_lossy().into_owned();
        let launch = if default.is_empty() {
            path_str.clone()
        } else {
            default.clone()
        };
        games.push(json!({
            "name": title.replace('_', " "),
            "platform": "Xbox 360",
            "source": "Xbox 360",
            "collection": "Xbox 360",
            "path": path_str,
            "launch": launch,
        }));
    }
    Ok(games)
}
