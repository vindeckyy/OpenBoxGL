//! Missing-file repair wizard (`pkg/parity/parity_repair.py`).

use std::collections::{HashMap, HashSet, VecDeque};
use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{Value, json};

use crate::saves::{expand_user_path, resolve_non_strict};

pub const MAX_SCAN_ITEMS: usize = 2000;
pub const MAX_CANDIDATES: usize = 20_000;
pub const MAX_FOLDER_DEPTH: usize = 8;
pub const MAX_PATH_LENGTH: usize = 4096;
pub const MAX_AMBIGUOUS_CHOICES: usize = 5;

pub const KIND_GAME: &str = "game";
pub const KIND_MEDIA: &str = "media";

pub const MEDIA_FIELDS: &[&str] = &[
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

pub const REPAIR_FIELDS: &[&str] = &[
    "path",
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

pub fn resolve_folder(raw: &str) -> Result<PathBuf, String> {
    let text = raw.trim();
    if text.is_empty() {
        return Err("A folder is required.".to_owned());
    }
    if text.len() > MAX_PATH_LENGTH {
        return Err("Folder path is too long.".to_owned());
    }
    let folder = expand_user_path(text);
    if !folder.is_absolute() {
        return Err("Folder must be an absolute path.".to_owned());
    }
    let resolved = resolve_non_strict(&folder);
    if resolved.parent().is_none() {
        return Err("The filesystem root is not a repair folder.".to_owned());
    }
    if !resolved.is_dir() {
        return Err("Folder does not exist.".to_owned());
    }
    if resolved
        .symlink_metadata()
        .map(|m| m.file_type().is_symlink())
        .unwrap_or(false)
    {
        return Err("Folder must not be a symlink.".to_owned());
    }
    Ok(resolved)
}

pub fn scan_missing_paths(
    state: &Value,
    include_media: bool,
    limit: usize,
    game_ids: Option<&HashSet<String>>,
) -> Value {
    let mut items = Vec::new();
    let mut truncated = false;
    let games = state
        .get("games")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let fields: &[&str] = if include_media {
        REPAIR_FIELDS
    } else {
        &["path"]
    };

    for (index, game) in games.iter().enumerate() {
        if !game.is_object() {
            continue;
        }
        let gid = game.get("game_id").and_then(Value::as_str).unwrap_or("");
        if let Some(filter) = game_ids
            && !filter.contains(gid)
        {
            continue;
        }
        let name = game.get("name").and_then(Value::as_str).unwrap_or("");
        let platform = game.get("platform").and_then(Value::as_str).unwrap_or("");
        for field in fields {
            let raw = game
                .get(*field)
                .and_then(Value::as_str)
                .map(str::trim)
                .unwrap_or("");
            if raw.is_empty() {
                continue;
            }
            if expand_user_path(raw).is_file() {
                continue;
            }
            if items.len() >= limit {
                truncated = true;
                break;
            }
            items.push(json!({
                "id": index,
                "game_id": gid,
                "name": name,
                "platform": platform,
                "field": field,
                "path": raw,
                "kind": if *field == "path" { KIND_GAME } else { KIND_MEDIA },
            }));
        }
        if truncated {
            break;
        }
    }
    json!({
        "items": items,
        "count": items.len(),
        "truncated": truncated,
    })
}

pub fn scan_candidates(folder: &str, limit: usize, max_depth: usize) -> Result<Vec<Value>, String> {
    let root = resolve_folder(folder)?;
    let mut candidates = Vec::new();
    let mut queue = VecDeque::new();
    queue.push_back((root.clone(), 0usize));
    while let Some((dir, depth)) = queue.pop_front() {
        if candidates.len() >= limit {
            break;
        }
        let Ok(rd) = fs::read_dir(&dir) else {
            continue;
        };
        let mut entries: Vec<(String, PathBuf)> = rd
            .flatten()
            .filter_map(|e| {
                let n = e.file_name().to_str()?.to_owned();
                Some((n, e.path()))
            })
            .collect();
        entries.sort_by(|a, b| a.0.cmp(&b.0));
        for (name, path) in entries {
            if candidates.len() >= limit {
                break;
            }
            if name.starts_with('.') {
                continue;
            }
            let Ok(meta) = fs::symlink_metadata(&path) else {
                continue;
            };
            if meta.file_type().is_symlink() {
                continue;
            }
            if meta.is_dir() {
                if depth < max_depth {
                    queue.push_back((path, depth + 1));
                }
                continue;
            }
            if !meta.is_file() {
                continue;
            }
            let rel = path
                .strip_prefix(&root)
                .ok()
                .map(|p| p.to_string_lossy().into_owned())
                .unwrap_or_else(|| name.clone());
            candidates.push(json!({
                "path": path.to_string_lossy(),
                "name": name,
                "size": meta.len(),
                "relative": rel,
            }));
        }
    }
    Ok(candidates)
}

fn match_keys(item: &Value) -> Vec<String> {
    let raw_path = item.get("path").and_then(Value::as_str).unwrap_or("");
    let missing = Path::new(raw_path)
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("")
        .to_lowercase();
    let mut keys = Vec::new();
    if !missing.is_empty() {
        keys.push(missing.clone());
        if item.get("kind").and_then(Value::as_str) == Some(KIND_GAME) {
            let stem = Path::new(&missing)
                .file_stem()
                .and_then(|s| s.to_str())
                .unwrap_or("")
                .to_owned();
            if !stem.is_empty() && stem != missing {
                keys.push(stem);
            }
        }
    }
    keys
}

pub fn plan_repair(
    items: &[Value],
    candidates: &[Value],
    fields: Option<&HashSet<String>>,
) -> Value {
    let mut by_name: HashMap<String, Vec<&Value>> = HashMap::new();
    for cand in candidates {
        let Some(name_raw) = cand
            .get("name")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
        else {
            continue;
        };
        let name = name_raw.to_lowercase();
        by_name.entry(name.clone()).or_default().push(cand);
        let stem = Path::new(&name)
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_owned();
        if !stem.is_empty() && stem != name {
            by_name.entry(stem).or_default().push(cand);
        }
    }

    let mut matches = Vec::new();
    let mut ambiguous = Vec::new();
    let mut unmatched = Vec::new();
    for item in items {
        if !item.is_object() {
            continue;
        }
        let field = item.get("field").and_then(Value::as_str).unwrap_or("");
        if let Some(allowed) = fields
            && !allowed.contains(field)
        {
            continue;
        }
        let row = json!({
            "id": item.get("id").cloned().unwrap_or(Value::Null),
            "game_id": item.get("game_id").cloned().unwrap_or(Value::Null),
            "name": item.get("name").cloned().unwrap_or(Value::Null),
            "platform": item.get("platform").cloned().unwrap_or(Value::Null),
            "field": field,
            "kind": item.get("kind").cloned().unwrap_or(Value::Null),
            "from": item.get("path").cloned().unwrap_or(Value::Null),
        });
        let mut choices: Vec<&Value> = Vec::new();
        let mut seen_paths: HashSet<&str> = HashSet::new();
        for key in match_keys(item) {
            if let Some(cands) = by_name.get(&key) {
                for c in cands {
                    let cp = c.get("path").and_then(Value::as_str).unwrap_or("");
                    if seen_paths.insert(cp) {
                        choices.push(*c);
                    }
                }
            }
        }
        choices.sort_by(|a, b| {
            let ra = a.get("relative").and_then(Value::as_str).unwrap_or("");
            let rb = b.get("relative").and_then(Value::as_str).unwrap_or("");
            ra.len().cmp(&rb.len()).then_with(|| ra.cmp(rb))
        });
        if choices.is_empty() {
            unmatched.push(row);
        } else if choices.len() == 1 {
            let mut m = row.as_object().cloned().unwrap_or_default();
            m.insert("path".into(), choices[0]["path"].clone());
            m.insert(
                "size".into(),
                choices[0].get("size").cloned().unwrap_or_else(|| json!(0)),
            );
            m.insert(
                "relative".into(),
                choices[0]
                    .get("relative")
                    .cloned()
                    .unwrap_or_else(|| json!("")),
            );
            matches.push(Value::Object(m));
        } else {
            let mut a = row.as_object().cloned().unwrap_or_default();
            let top: Vec<Value> = choices
                .into_iter()
                .take(MAX_AMBIGUOUS_CHOICES)
                .map(|c| {
                    json!({
                        "path": c["path"],
                        "relative": c.get("relative").cloned().unwrap_or_else(|| json!("")),
                        "size": c.get("size").cloned().unwrap_or_else(|| json!(0)),
                    })
                })
                .collect();
            a.insert("choices".into(), Value::Array(top));
            ambiguous.push(Value::Object(a));
        }
    }
    json!({
        "matches": matches,
        "ambiguous": ambiguous,
        "unmatched": unmatched,
        "counts": {
            "matched": matches.len(),
            "ambiguous": ambiguous.len(),
            "unmatched": unmatched.len(),
        },
    })
}

pub fn apply_repair(
    state: &mut Value,
    matches: &[Value],
    selection: Option<&[Value]>,
) -> Result<Value, String> {
    let mut selected_ids: Option<HashSet<usize>> = None;
    let mut selected_pairs: Option<HashSet<(usize, String)>> = None;
    if let Some(sel) = selection {
        let mut ids = HashSet::new();
        let mut pairs = HashSet::new();
        for v in sel {
            if let Some(arr) = v.as_array()
                && arr.len() == 2
                && let Some(idx) = arr[0].as_u64()
            {
                pairs.insert((idx as usize, arr[1].as_str().unwrap_or("").to_owned()));
            } else if let Some(idx) = v.as_u64() {
                ids.insert(idx as usize);
            } else if let Some(s) = v.as_str()
                && let Ok(idx) = s.parse::<usize>()
            {
                ids.insert(idx);
            }
        }
        selected_ids = Some(ids);
        selected_pairs = Some(pairs);
    }

    let Some(games) = state.get_mut("games").and_then(Value::as_array_mut) else {
        return Err("State has no games list.".to_owned());
    };
    let mut updated = 0usize;
    let mut skipped = Vec::new();
    let mut applied = Vec::new();

    for row in matches {
        if !row.is_object() {
            continue;
        }
        let Some(index) = row.get("id").and_then(Value::as_u64).map(|n| n as usize) else {
            skipped.push(json!({"reason": "invalid_id", "row": row}));
            continue;
        };
        let field = row.get("field").and_then(Value::as_str).unwrap_or("");
        if !REPAIR_FIELDS.contains(&field) {
            skipped.push(json!({"reason": "invalid_field", "id": index, "field": field}));
            continue;
        }
        if let Some(ref pairs) = selected_pairs
            && !pairs.contains(&(index, field.to_owned()))
            && !selected_ids
                .as_ref()
                .is_some_and(|ids| ids.contains(&index))
        {
            continue;
        }
        let Some(game) = games.get_mut(index).and_then(Value::as_object_mut) else {
            skipped.push(json!({"reason": "missing_game", "id": index, "field": field}));
            continue;
        };
        let expected_id = row.get("game_id").and_then(Value::as_str).unwrap_or("");
        let actual_id = game.get("game_id").and_then(Value::as_str).unwrap_or("");
        if !expected_id.is_empty() && actual_id != expected_id {
            skipped.push(json!({"reason": "stale_game", "id": index, "field": field}));
            continue;
        }
        let target_raw = row.get("path").and_then(Value::as_str).unwrap_or("");
        let target = expand_user_path(target_raw);
        if !target.is_file() {
            skipped.push(json!({
                "reason": "missing_target",
                "id": index,
                "field": field,
                "path": target.to_string_lossy(),
            }));
            continue;
        }
        let current = game.get(field).and_then(Value::as_str).unwrap_or("");
        let from_val = row.get("from").and_then(Value::as_str).unwrap_or("");
        if !current.is_empty() && current != from_val {
            skipped.push(json!({"reason": "changed_since_preview", "id": index, "field": field}));
            continue;
        }
        let target_str = target.to_string_lossy().into_owned();
        if current == target_str {
            continue;
        }
        game.insert(field.to_owned(), Value::String(target_str.clone()));
        updated += 1;
        applied.push(json!({
            "id": index,
            "game_id": expected_id,
            "field": field,
            "path": target_str,
        }));
    }
    Ok(json!({
        "updated": updated,
        "skipped": skipped,
        "applied": applied,
    }))
}
