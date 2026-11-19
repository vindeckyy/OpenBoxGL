//! Platform rename/delete with transactional media migration
//! (`pkg/parity/parity_platform_admin.py`).

use std::collections::{HashMap, HashSet};
use std::fs::{self, File};
use std::io::Read;
use std::path::{Path, PathBuf};

use openbox_core::ApiError;
use openbox_platform::resolve_non_strict;
use openbox_runtime::media_probe::{MEDIA_PATH_FIELDS, MediaProbeService};
use rand::RngCore;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

const PLATFORM_SETTING_MAPS: &[&str] = &[
    "platform_categories",
    "theme_by_platform",
    "image_group_by_platform",
    "cover_model_by_platform",
    "model_back_by_platform",
    "model_case_by_platform",
    "model_rotation_lock_by_platform",
    "platform_documents",
    "platform_videos",
    "emulator_platform_settings",
];

fn validate_platform_name(value: &str) -> Result<String, ApiError> {
    let name = value.trim();
    if name.is_empty() || name.chars().count() > 80 {
        return Err(ApiError::bad_request(
            "Platform name is required and must be 80 characters or fewer.",
        ));
    }
    if matches!(name, "." | "..")
        || name
            .chars()
            .any(|ch| matches!(ch, '/' | '\\' | '\0' | '\r' | '\n'))
    {
        return Err(ApiError::bad_request(
            "Platform name may not contain a path separator.",
        ));
    }
    Ok(name.to_owned())
}

fn file_hash(path: &Path) -> Result<String, std::io::Error> {
    let mut f = File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buf = [0u8; 65536];
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hex::encode(hasher.finalize()))
}

fn move_file(source: &Path, dest: &Path) -> Result<(), ApiError> {
    if let Some(parent) = dest.parent() {
        fs::create_dir_all(parent).map_err(|e| ApiError::bad_request(e.to_string()))?;
    }
    if dest.exists() {
        if dest.is_file()
            && source.is_file()
            && let (Ok(h1), Ok(h2)) = (file_hash(source), file_hash(dest))
            && h1 == h2
        {
            let _ = fs::remove_file(source);
            return Ok(());
        }
        return Err(ApiError::bad_request(format!(
            "Destination already exists: {}",
            dest.display()
        )));
    }
    if fs::rename(source, dest).is_err() {
        fs::copy(source, dest).map_err(|e| ApiError::bad_request(e.to_string()))?;
        if let Ok(f) = fs::OpenOptions::new().write(true).open(dest) {
            let _ = f.sync_all();
        }
        fs::remove_file(source).map_err(|e| ApiError::bad_request(e.to_string()))?;
    }
    Ok(())
}

fn migrate_games_paths(
    games: &mut [Value],
    indices: &[usize],
    old: &str,
    new: &str,
    data_dir: &Path,
) -> Result<(usize, usize), ApiError> {
    let media_root = data_dir.join("media");
    let _ = fs::create_dir_all(&media_root);
    let base = fs::canonicalize(&media_root).unwrap_or_else(|_| resolve_non_strict(&media_root));
    let source_root = resolve_non_strict(&media_root.join(old));
    let dest_root = resolve_non_strict(&media_root.join(new));
    if dest_root != base && !dest_root.starts_with(&base) {
        return Err(ApiError::bad_request(
            "Destination platform media path escapes the media root.",
        ));
    }

    let snapshots: Vec<(usize, Map<String, Value>)> = indices
        .iter()
        .filter_map(|&idx| games[idx].as_object().map(|o| (idx, o.clone())))
        .collect();

    let mut moved: Vec<(PathBuf, PathBuf)> = Vec::new();
    let mut moved_count = 0usize;
    let mut skipped = 0usize;

    let res: Result<(), ApiError> = (|| {
        for &idx in indices {
            let Some(obj) = games[idx].as_object_mut() else {
                continue;
            };
            let mut pairs: Vec<(String, String)> = Vec::new();
            for &field in MEDIA_PATH_FIELDS {
                if let Some(s) = obj.get(field).and_then(Value::as_str)
                    && !s.is_empty()
                {
                    pairs.push((field.to_owned(), s.to_owned()));
                }
            }
            if let Some(shots) = obj.get("screenshots").and_then(Value::as_array) {
                for shot in shots {
                    if let Some(s) = shot.as_str()
                        && !s.is_empty()
                    {
                        pairs.push(("screenshots".to_owned(), s.to_owned()));
                    }
                }
            }
            let probe = MediaProbeService::new(data_dir);
            for (field, raw) in pairs {
                let Ok(source) = probe.approved_media_path(&raw, true) else {
                    skipped += 1;
                    continue;
                };
                if !source.starts_with(&source_root) || source == source_root {
                    skipped += 1;
                    continue;
                }
                let Ok(rel) = source.strip_prefix(&source_root) else {
                    skipped += 1;
                    continue;
                };
                let dest = dest_root.join(rel);
                move_file(&source, &dest)?;
                let dest_str = dest.to_string_lossy().into_owned();
                moved.push((source, dest));
                moved_count += 1;
                if field == "screenshots" {
                    if let Some(Value::Array(arr)) = obj.get_mut("screenshots") {
                        for item in arr.iter_mut() {
                            if item.as_str() == Some(&raw) {
                                *item = Value::String(dest_str.clone());
                            }
                        }
                    }
                } else {
                    obj.insert(field, Value::String(dest_str));
                }
            }
        }
        Ok(())
    })();

    if let Err(err) = res {
        for (source, dest) in moved.into_iter().rev() {
            if dest.exists() && !source.exists() {
                let _ = fs::rename(&dest, &source);
            }
        }
        for (idx, snap) in snapshots {
            games[idx] = Value::Object(snap);
        }
        return Err(err);
    }

    Ok((moved_count, skipped))
}

pub fn migrate_game_media(
    games: &mut [Value],
    indices: &[usize],
    old: &str,
    new: &str,
    data_dir: &Path,
) -> Result<Value, ApiError> {
    let old = validate_platform_name(old)?;
    let new = validate_platform_name(new)?;
    if old == new {
        return Ok(json!({"media_moved": 0, "media_skipped": 0}));
    }
    let (moved, skipped) = migrate_games_paths(games, indices, &old, &new, data_dir)?;
    Ok(json!({"media_moved": moved, "media_skipped": skipped}))
}

pub fn rename_platform(
    state: &mut Value,
    old: &str,
    new: &str,
    migrate_media: bool,
    data_dir: &Path,
) -> Result<Value, ApiError> {
    let old = validate_platform_name(old)?;
    let new = validate_platform_name(new)?;

    let mut existing: HashSet<String> = HashSet::new();
    let mut matching_indices = Vec::new();
    if let Some(games) = state.get("games").and_then(Value::as_array) {
        for (i, g) in games.iter().enumerate() {
            if let Some(p) = g.get("platform").and_then(Value::as_str) {
                existing.insert(p.to_owned());
                if p == old {
                    matching_indices.push(i);
                }
            }
        }
    }
    if !existing.contains(&old) {
        return Err(ApiError::bad_request(format!("Platform not found: {old}")));
    }

    let mut occupied = existing;
    if let Some(profiles) = state.get("profiles").and_then(Value::as_object) {
        occupied.extend(profiles.keys().cloned());
    }
    if let Some(settings) = state.get("settings").and_then(Value::as_object) {
        for &k in PLATFORM_SETTING_MAPS {
            if let Some(m) = settings.get(k).and_then(Value::as_object) {
                occupied.extend(m.keys().cloned());
            }
        }
    }
    if let Some(es) = state.get("emulator_settings").and_then(Value::as_object) {
        for entry in es.values() {
            if let Some(p) = entry.get("platforms").and_then(Value::as_object) {
                occupied.extend(p.keys().cloned());
            }
        }
    }
    if occupied.contains(&new) && new != old {
        return Err(ApiError::bad_request(format!(
            "Platform already exists: {new}"
        )));
    }
    if old == new {
        return Ok(json!({"games": 0, "media_moved": 0, "media_skipped": 0}));
    }

    let (moved, skipped) = if migrate_media {
        if let Some(Value::Array(games)) = state.get_mut("games") {
            migrate_games_paths(games, &matching_indices, &old, &new, data_dir)?
        } else {
            (0, 0)
        }
    } else {
        (0, 0)
    };

    let mut changed = 0usize;
    if let Some(Value::Array(games)) = state.get_mut("games") {
        for g in games.iter_mut() {
            if let Some(obj) = g.as_object_mut()
                && obj.get("platform").and_then(Value::as_str) == Some(old.as_str())
            {
                obj.insert("platform".into(), Value::String(new.clone()));
                changed += 1;
            }
        }
    }
    if let Some(Value::Object(settings)) = state.get_mut("settings") {
        for &k in PLATFORM_SETTING_MAPS {
            if let Some(Value::Object(m)) = settings.get_mut(k)
                && let Some(val) = m.remove(&old)
            {
                m.insert(new.clone(), val);
            }
        }
        if let Some(Value::Array(presets)) = settings.get_mut("filter_presets") {
            for preset in presets.iter_mut() {
                if let Some(Value::Object(rules)) = preset.get_mut("rules")
                    && rules.get("platform").and_then(Value::as_str) == Some(old.as_str())
                {
                    rules.insert("platform".into(), Value::String(new.clone()));
                }
            }
        }
    }
    if let Some(Value::Object(profiles)) = state.get_mut("profiles")
        && let Some(val) = profiles.remove(&old)
    {
        profiles.insert(new.clone(), val);
    }
    if let Some(Value::Object(es)) = state.get_mut("emulator_settings") {
        for entry in es.values_mut() {
            if let Some(Value::Object(p)) = entry.get_mut("platforms")
                && let Some(val) = p.remove(&old)
            {
                p.insert(new.clone(), val);
            }
        }
    }
    if let Some(Value::Array(playlists)) = state.get_mut("playlists") {
        for pl in playlists.iter_mut() {
            if pl.get("type").and_then(Value::as_str) == Some("filter")
                && let Some(Value::Object(rules)) = pl.get_mut("rules")
                && rules.get("platform").and_then(Value::as_str) == Some(old.as_str())
            {
                rules.insert("platform".into(), Value::String(new.clone()));
            }
        }
    }
    Ok(json!({
        "games": changed,
        "media_moved": moved,
        "media_skipped": skipped,
    }))
}

pub fn trash_entry_for(state: &Value, game: &Value, index: usize) -> Value {
    let game_id = game
        .get("game_id")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    let id_str = game
        .get("id")
        .map(|v| match v {
            Value::String(s) => s.clone(),
            Value::Number(n) => n.to_string(),
            _ => String::new(),
        })
        .unwrap_or_default();
    let mut member_keys = HashSet::new();
    if !game_id.is_empty() {
        member_keys.insert(game_id.clone());
    }
    if !id_str.is_empty() {
        member_keys.insert(id_str);
    }
    let mut playlists = Vec::new();
    if let Some(pls) = state.get("playlists").and_then(Value::as_array) {
        for pl in pls {
            if pl.get("type").and_then(Value::as_str) != Some("manual") {
                continue;
            }
            if let Some(members) = pl.get("members").and_then(Value::as_array)
                && members.iter().any(|m| {
                    let s = match m {
                        Value::String(s) => s.clone(),
                        Value::Number(n) => n.to_string(),
                        _ => String::new(),
                    };
                    member_keys.contains(&s)
                })
            {
                playlists.push(
                    pl.get("name")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned(),
                );
            }
        }
    }
    let mut rnd = [0u8; 6];
    rand::thread_rng().fill_bytes(&mut rnd);
    let trashed_at = jiff::Timestamp::now()
        .strftime("%Y-%m-%dT%H:%M:%S")
        .to_string();
    json!({
        "trash_id": format!("trash-{}", hex::encode(rnd)),
        "trashed_at": trashed_at,
        "index": index,
        "game_id": game_id,
        "name": game.get("name").and_then(Value::as_str).unwrap_or(""),
        "platform": game.get("platform").and_then(Value::as_str).unwrap_or(""),
        "game": game.clone(),
        "playlists": playlists,
    })
}

pub fn delete_platform(
    state: &mut Value,
    name: &str,
    delete_media: bool,
    data_dir: &Path,
) -> Result<Value, ApiError> {
    let name = validate_platform_name(name)?;
    let games_snapshot = state
        .get("games")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut new_entries = Vec::new();
    let mut kept = Vec::new();
    for (i, g) in games_snapshot.iter().enumerate() {
        if g.get("platform").and_then(Value::as_str) == Some(name.as_str()) {
            new_entries.push(trash_entry_for(state, g, i));
        } else {
            kept.push(g.clone());
        }
    }
    let removed_count = new_entries.len();
    if removed_count == 0 {
        return Err(ApiError::bad_request(format!("Platform not found: {name}")));
    }
    if let Some(obj) = state.as_object_mut() {
        obj.insert("games".into(), Value::Array(kept));
        let trash = obj
            .entry("trash".to_owned())
            .or_insert_with(|| Value::Array(Vec::new()));
        if let Some(arr) = trash.as_array_mut() {
            arr.extend(new_entries);
        }
        if let Some(Value::Object(settings)) = obj.get_mut("settings") {
            for &k in PLATFORM_SETTING_MAPS {
                if let Some(Value::Object(m)) = settings.get_mut(k) {
                    m.remove(&name);
                }
            }
        }
        if let Some(Value::Object(profiles)) = obj.get_mut("profiles") {
            profiles.remove(&name);
        }
        if let Some(Value::Object(es)) = obj.get_mut("emulator_settings") {
            for entry in es.values_mut() {
                if let Some(Value::Object(p)) = entry.get_mut("platforms") {
                    p.remove(&name);
                }
            }
        }
    }

    let mut media_deleted = 0usize;
    if delete_media {
        let media_root = data_dir.join("media");
        let media_base =
            fs::canonicalize(&media_root).unwrap_or_else(|_| resolve_non_strict(&media_root));
        let root = media_base.join(&name);
        if fs::symlink_metadata(&root)
            .map(|m| m.file_type().is_symlink())
            .unwrap_or(false)
        {
            return Err(ApiError::bad_request(
                "Platform media root is a symlink; refusing to delete outside the media root.",
            ));
        }
        let resolved = resolve_non_strict(&root);
        if resolved.parent() != Some(media_base.as_path()) {
            return Err(ApiError::bad_request(
                "Platform media root escapes the media root.",
            ));
        }
        if root.is_dir() {
            let mut dirs = Vec::new();
            let mut stack = vec![root.clone()];
            while let Some(d) = stack.pop() {
                dirs.push(d.clone());
                if let Ok(rd) = fs::read_dir(&d) {
                    for entry in rd.filter_map(Result::ok) {
                        let p = entry.path();
                        if p.is_dir() {
                            stack.push(p);
                        } else if p.is_file() && fs::remove_file(&p).is_ok() {
                            media_deleted += 1;
                        }
                    }
                }
            }
            for d in dirs.into_iter().rev() {
                let _ = fs::remove_dir(&d);
            }
        }
    }

    Ok(json!({
        "games": removed_count,
        "media_deleted": media_deleted,
    }))
}

pub fn bulk_update_with_media(
    state: &mut Value,
    ids: Option<&Value>,
    changes: Option<&Value>,
    migrate_media: bool,
    data_dir: &Path,
) -> Result<(usize, usize, Vec<Value>), ApiError> {
    let old_platforms: HashMap<usize, String> = state
        .get("games")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .enumerate()
                .filter_map(|(i, g)| {
                    g.get("platform")
                        .and_then(Value::as_str)
                        .map(|p| (i, p.to_owned()))
                })
                .collect()
        })
        .unwrap_or_default();

    let changed = {
        let Some(Value::Array(games)) = state.get_mut("games") else {
            return Err(ApiError::bad_request("Select at least one game."));
        };
        crate::catalog::bulk_update(games, ids, changes)?
    };

    let new_platform = changes
        .and_then(|c| c.get("platform"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_owned();

    let mut moved = 0usize;
    let mut errors = Vec::new();
    if migrate_media
        && !new_platform.is_empty()
        && let Some(Value::Array(games)) = state.get_mut("games")
    {
        let mut groups: HashMap<String, Vec<usize>> = HashMap::new();
        for (i, g) in games.iter().enumerate() {
            let old = old_platforms.get(&i).map(String::as_str).unwrap_or("");
            if !old.is_empty()
                && old != new_platform
                && g.get("platform").and_then(Value::as_str) == Some(new_platform.as_str())
            {
                groups.entry(old.to_owned()).or_default().push(i);
            }
        }
        for (old, idxs) in groups {
            match migrate_game_media(games, &idxs, &old, &new_platform, data_dir) {
                Ok(res) => {
                    moved += res.get("media_moved").and_then(Value::as_u64).unwrap_or(0) as usize;
                }
                Err(err) => {
                    errors.push(json!({"platform": old, "error": err.message}));
                }
            }
        }
    }
    Ok((changed, moved, errors))
}
