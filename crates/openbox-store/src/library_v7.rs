//! Library format v7: v6 backup copy, migration report, integrity check, repair, and portable move plans.
//! Mirrors `pkg/state/library_v7.py` (ADR 0089, ADR 0090, ADR 0108).

use std::collections::{BTreeMap, HashSet};
use std::fs;
use std::net::{SocketAddr, TcpStream};
use std::path::Path;
use std::time::Duration;

use openbox_core::{
    APP_VERSION, ApiError, STATE_FORMAT, STATE_SCHEMA_VERSION, is_valid_library_id,
};
use openbox_io::atomic_write_bytes;
use openbox_platform::{IS_MACOS, IS_WINDOWS};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use crate::json_store::{
    JsonStateStore, StateError, commit_migrated_state, normalize_state, prune_trash,
    raw_schema_version, schema_too_old_detail, snapshot_before_migration,
};
use crate::media_manifest;
use crate::portable_paths;

pub const V6_BACKUP_NAME: &str = "library.v6.backup.json";
pub const PRIMARY_NAME: &str = "library.json";
pub const REPORT_NAME: &str = "migration-report.json";
pub const MARKER_NAME: &str = "migration-in-progress.json";
pub const ROLLBACK_REPORT_NAME: &str = "rollback-report.json";
pub const REPORT_FORMAT: u32 = 1;
pub const MOVE_PLAN_FORMAT: u32 = 1;
pub const EXAMPLE_LIMIT: usize = 20;
pub const ROLLBACK_REMOVED: &str = "openbox --rollback-library was removed in 3.0. Use OpenBox 2.1.0 to restore library.v6.backup.json.";

const EXTRA_LISTS: &[&str] = &["applications", "versions", "documents"];

fn platform_rules() -> (bool, bool) {
    (IS_WINDOWS, IS_WINDOWS || IS_MACOS)
}

fn utc_now() -> String {
    jiff::Timestamp::now()
        .strftime("%Y-%m-%dT%H:%M:%SZ")
        .to_string()
}

fn stamp() -> String {
    jiff::Timestamp::now()
        .strftime("%Y%m%dT%H%M%S%6fZ")
        .to_string()
}

fn sha256_hex(data: &[u8]) -> String {
    hex::encode(Sha256::digest(data))
}

fn write_json(path: &Path, payload: &Value) -> std::io::Result<()> {
    let text = format!(
        "{}\n",
        serde_json::to_string_pretty(payload).unwrap_or_else(|_| "{}".to_owned())
    );
    atomic_write_bytes(path, text.as_bytes(), 0o600)?;
    Ok(())
}

fn read_json_obj(path: &Path) -> Option<Map<String, Value>> {
    let text = fs::read_to_string(path).ok()?;
    serde_json::from_str::<Value>(&text)
        .ok()?
        .as_object()
        .cloned()
}

pub fn server_running(data_dir: &Path) -> bool {
    let Ok(port_text) = fs::read_to_string(data_dir.join("server.port")) else {
        return false;
    };
    let Ok(port) = port_text.trim().parse::<u16>() else {
        return false;
    };
    if port == 0 {
        return false;
    }
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    TcpStream::connect_timeout(&addr, Duration::from_millis(500)).is_ok()
}

pub fn counts_for(state: &Value) -> Value {
    let len_of = |key: &str| -> usize {
        match state.get(key) {
            Some(Value::Array(a)) => a.len(),
            Some(Value::Object(o)) => o.len(),
            _ => 0,
        }
    };
    json!({
        "games": len_of("games"),
        "profiles": len_of("profiles"),
        "playlists": len_of("playlists"),
        "sessions_moved": len_of("active_sessions"),
        "paths_tokenized": 0,
        "paths_skipped": 0,
    })
}

struct ReportParams<'a> {
    started_at: &'a str,
    state: &'a Value,
    from_schema: i64,
    backup_sha: &'a str,
    snapshot: Option<String>,
    counts: Value,
    warnings: Vec<String>,
    reconstructed: bool,
}

fn build_report(p: ReportParams<'_>) -> Value {
    json!({
        "format": REPORT_FORMAT,
        "app_version": APP_VERSION,
        "started_at": p.started_at,
        "completed_at": utc_now(),
        "from_schema": p.from_schema,
        "to_schema": STATE_SCHEMA_VERSION,
        "library_id": p.state.get("library_id").and_then(Value::as_str).unwrap_or(""),
        "backup": V6_BACKUP_NAME,
        "backup_sha256": p.backup_sha,
        "pre_migration_snapshot": p.snapshot,
        "counts": p.counts,
        "warnings": p.warnings,
        "reconstructed": p.reconstructed,
    })
}

pub fn finish_interrupted_migration(data_dir: &Path, primary: &Path) -> Option<Value> {
    let marker = read_json_obj(&data_dir.join(MARKER_NAME))?;
    if let Some(existing) = read_json_obj(&data_dir.join(REPORT_NAME)) {
        let _ = fs::remove_file(data_dir.join(MARKER_NAME));
        return Some(Value::Object(existing));
    }
    let raw = fs::read_to_string(primary)
        .ok()
        .and_then(|s| serde_json::from_str::<Value>(&s).ok())
        .unwrap_or_else(|| json!({}));
    let now = utc_now();
    let report = build_report(ReportParams {
        started_at: marker
            .get("started_at")
            .and_then(Value::as_str)
            .unwrap_or(&now),
        state: &raw,
        from_schema: marker
            .get("from_schema")
            .and_then(Value::as_i64)
            .unwrap_or(6),
        backup_sha: marker
            .get("backup_sha256")
            .and_then(Value::as_str)
            .unwrap_or(""),
        snapshot: None,
        counts: counts_for(&raw),
        warnings: vec![
            "Written after an interrupted migration; counts describe the library as found."
                .to_owned(),
        ],
        reconstructed: true,
    });
    if write_json(&data_dir.join(REPORT_NAME), &report).is_ok() {
        let _ = fs::remove_file(data_dir.join(MARKER_NAME));
    }
    Some(report)
}

pub(crate) fn migrate_unlocked_inner(
    store: &JsonStateStore,
) -> Result<(Option<Value>, Option<Value>), StateError> {
    let primary = &store.path;
    let data_dir = store.data_dir();
    if !primary.exists() {
        return Ok((None, None));
    }
    let raw_bytes = fs::read(primary)?;
    let raw: Value = serde_json::from_slice(&raw_bytes).map_err(|_| {
        StateError::Corrupt(format!(
            "Unable to read {}. The original file was preserved; restore or inspect it before continuing.",
            primary.display()
        ))
    })?;
    let version = raw_schema_version(&raw);
    if version > i64::from(STATE_SCHEMA_VERSION) {
        return Err(StateError::FutureVersion(format!(
            "This library uses schema {version}; this build supports {STATE_SCHEMA_VERSION}. Install a newer OpenBox."
        )));
    }
    if version == i64::from(STATE_SCHEMA_VERSION) {
        return Ok((None, finish_interrupted_migration(data_dir, primary)));
    }
    if version < 1 {
        return Err(StateError::Corrupt(
            "OpenBox library.json must contain an object or legacy game list. The file was not changed.".to_owned(),
        ));
    }
    if version < 6 {
        return Err(StateError::SchemaTooOld(schema_too_old_detail(version)));
    }

    let started_at = utc_now();
    let (mut state, _) = normalize_state(&raw, true).map_err(|e| {
        StateError::Migration(format!(
            "The library could not be upgraded: {e} The original file was not changed."
        ))
    })?;
    let backup_sha = sha256_hex(&raw_bytes);
    let existing_v6 = data_dir.join(V6_BACKUP_NAME);
    if existing_v6.is_file()
        && let Ok(old_bytes) = fs::read(&existing_v6)
        && sha256_hex(&old_bytes) != backup_sha
    {
        let _ = fs::rename(
            &existing_v6,
            data_dir.join(format!("library.v6.backup.{}.json", stamp())),
        );
    }
    atomic_write_bytes(&existing_v6, &raw_bytes, 0o600)?;
    write_json(
        &data_dir.join(MARKER_NAME),
        &json!({
            "started_at": started_at,
            "from_schema": version,
            "backup_sha256": backup_sha,
        }),
    )?;
    let snap = snapshot_before_migration(store);
    let counts = counts_for(&state);
    commit_migrated_state(store, &mut state)?;
    let report = build_report(ReportParams {
        started_at: &started_at,
        state: &state,
        from_schema: version,
        backup_sha: &backup_sha,
        snapshot: snap,
        counts,
        warnings: Vec::new(),
        reconstructed: false,
    });
    write_json(&data_dir.join(REPORT_NAME), &report)?;
    let _ = fs::remove_file(data_dir.join(MARKER_NAME));
    Ok((Some(state), Some(report)))
}

fn media_manifest_enabled(raw: &Value) -> bool {
    raw.get("settings")
        .and_then(|s| s.get("media_manifest_enabled"))
        .and_then(Value::as_bool)
        .unwrap_or(true)
}

/// Read-only library integrity report matching `pkg/state/library_v7.py::check_library`.
pub fn check_library(data_dir: &Path) -> Value {
    let primary = data_dir.join(PRIMARY_NAME);
    let mut categories: BTreeMap<String, Vec<String>> = BTreeMap::new();
    let mut has_error = false;
    let mut has_warn = false;

    let mut add_finding = |cat: &str, severity: &str, detail: String| {
        if severity == "error" {
            has_error = true;
        } else {
            has_warn = true;
        }
        let list = categories.entry(cat.to_owned()).or_default();
        if list.len() < EXAMPLE_LIMIT {
            list.push(detail);
        }
    };

    if !primary.is_file() {
        return json!({
            "format": 1,
            "checked_at": utc_now(),
            "library_path": primary.to_string_lossy(),
            "status": "healthy",
            "exit_code": 0,
            "note": "No library.json yet; nothing to check.",
            "categories": {},
        });
    }

    let raw = match fs::read_to_string(&primary)
        .ok()
        .and_then(|s| serde_json::from_str::<Value>(&s).ok())
    {
        Some(v) => v,
        None => {
            add_finding(
                "primary_unreadable",
                "error",
                format!("Unable to read {}", primary.display()),
            );
            return json!({
                "format": 1,
                "checked_at": utc_now(),
                "library_path": primary.to_string_lossy(),
                "status": "errors",
                "exit_code": 2,
                "categories": categories,
            });
        }
    };

    let version = raw_schema_version(&raw);
    if version > i64::from(STATE_SCHEMA_VERSION) {
        add_finding(
            "schema_future",
            "error",
            format!("schema {version}; this build supports {STATE_SCHEMA_VERSION}"),
        );
    } else if version < 6 {
        add_finding(
            "schema_outdated",
            "error",
            format!("schema {version}; open it with OpenBox 2.1 before upgrading"),
        );
    } else if version < i64::from(STATE_SCHEMA_VERSION) {
        add_finding(
            "schema_outdated",
            "warning",
            format!("schema {version}; run --migrate-library"),
        );
    }

    let Some(obj) = raw.as_object() else {
        add_finding(
            "primary_unreadable",
            "error",
            "library.json is not a JSON object".to_owned(),
        );
        return json!({
            "format": 1,
            "checked_at": utc_now(),
            "library_path": primary.to_string_lossy(),
            "schema_version": version,
            "status": "errors",
            "exit_code": 2,
            "categories": categories,
        });
    };

    if version == i64::from(STATE_SCHEMA_VERSION)
        && (obj.get("format").and_then(Value::as_str) != Some(STATE_FORMAT)
            || !obj
                .get("library_id")
                .and_then(Value::as_str)
                .is_some_and(is_valid_library_id))
    {
        add_finding(
            "envelope_incomplete",
            "warning",
            "format or library_id is missing or invalid".to_owned(),
        );
    }

    let path_mode = obj
        .get("path_mode")
        .and_then(Value::as_str)
        .unwrap_or("absolute");
    let library_root = obj
        .get("library_root")
        .and_then(Value::as_str)
        .unwrap_or("");
    if !library_root.is_empty() && !Path::new(library_root).is_dir() {
        add_finding("library_root_missing", "error", library_root.to_owned());
    }

    let (windows, folded) = platform_rules();
    let games = obj
        .get("games")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut seen = HashSet::new();
    for game in &games {
        let Some(g) = game.as_object() else {
            continue;
        };
        let gid = g.get("game_id").and_then(Value::as_str).unwrap_or("");
        if !seen.insert(gid.to_owned()) {
            add_finding("game_duplicate_id", "error", gid.to_owned());
        }
        let Some(p) = g
            .get("path")
            .and_then(Value::as_str)
            .filter(|s| !s.trim().is_empty())
        else {
            continue;
        };
        let kind = portable_paths::classify(p, library_root, windows, folded);
        let resolved = if kind == "token" {
            match portable_paths::decode_path(p, library_root, windows, folded) {
                Ok(dec) => dec,
                Err(err) => {
                    let cat = if err.code == "PATH_TOKEN_ESCAPE" {
                        "path_token_escape"
                    } else {
                        "path_token_invalid"
                    };
                    add_finding(cat, "error", format!("{gid}: {}", err.code));
                    continue;
                }
            }
        } else {
            if kind == "absolute" && !library_root.is_empty() {
                add_finding("game_path_outside_root", "warning", format!("{gid}: {p}"));
            }
            p.to_owned()
        };
        if !Path::new(&resolved).exists() {
            add_finding("game_path_missing", "warning", format!("{gid}: {resolved}"));
        }
    }

    let media_report = if media_manifest_enabled(&raw) {
        let v = media_manifest::verify(&data_dir.join("media"));
        for rel in &v.missing {
            add_finding("media_missing", "warning", rel.clone());
        }
        for rel in &v.changed {
            add_finding("media_changed", "warning", rel.clone());
        }
        for rel in &v.untracked {
            add_finding("media_untracked", "warning", rel.clone());
        }
        json!({
            "enabled": true,
            "tracked": v.tracked,
            "missing": v.missing,
            "changed": v.changed,
            "untracked": v.untracked,
        })
    } else {
        json!({
            "enabled": false,
            "tracked": 0,
            "missing": [],
            "changed": [],
            "untracked": [],
        })
    };

    let (status, exit_code) = if has_error {
        ("errors", 2)
    } else if has_warn {
        ("warnings", 1)
    } else {
        ("healthy", 0)
    };

    json!({
        "format": 1,
        "checked_at": utc_now(),
        "library_path": primary.to_string_lossy(),
        "schema_version": version,
        "library_id": obj.get("library_id").cloned().unwrap_or(Value::Null),
        "path_mode": path_mode,
        "library_root": library_root,
        "games": games.len(),
        "media_manifest": media_report,
        "save_vault": { "snapshots": 0, "bytes": 0, "games": 0 },
        "status": status,
        "exit_code": exit_code,
        "categories": categories,
    })
}

pub fn repair_library(data_dir: &Path, refresh_backup: bool) -> Result<Value, String> {
    let primary = data_dir.join(PRIMARY_NAME);
    if !primary.is_file() {
        return Ok(json!({
            "status": "nothing_to_repair",
            "reason": "No library.json yet.",
        }));
    }
    if server_running(data_dir) {
        return Err("OpenBox is running. Quit OpenBox before repairing the library.".to_owned());
    }
    let raw_bytes = fs::read(&primary).map_err(|e| e.to_string())?;
    let raw: Value = serde_json::from_slice(&raw_bytes).map_err(|e| e.to_string())?;
    let version = raw_schema_version(&raw);
    if version < 6 {
        return Err(format!(
            "This library is schema {version}. Open it with OpenBox 2.1 before repairing it here."
        ));
    }
    if version < i64::from(STATE_SCHEMA_VERSION) {
        return Err("The library needs migration first. Run --migrate-library.".to_owned());
    }
    let raw_trash = raw
        .get("trash")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let trash_removed = raw_trash
        .len()
        .saturating_sub(prune_trash(&raw_trash, None).len());

    let store = JsonStateStore::new(&primary);
    let mut state = store.load().map_err(|e| e.to_string())?;
    if let Some(arr) = state.get("trash").and_then(Value::as_array).cloned() {
        let pruned = prune_trash(&arr, None);
        if pruned.len() != arr.len()
            && let Some(obj) = state.as_object_mut()
        {
            obj.insert("trash".into(), Value::Array(pruned));
            store.save(&state).map_err(|e| e.to_string())?;
        }
    }
    let read_model = data_dir.join("sqlite_readmodel.db");
    let read_model_removed = read_model.exists();
    for suffix in ["", "-wal", "-shm"] {
        let _ = fs::remove_file(data_dir.join(format!("sqlite_readmodel.db{suffix}")));
    }
    if refresh_backup {
        let current = fs::read(&primary).map_err(|e| e.to_string())?;
        atomic_write_bytes(
            &data_dir.join(format!("{PRIMARY_NAME}.bak")),
            &current,
            0o600,
        )
        .map_err(|e| e.to_string())?;
    }
    let media = if media_manifest_enabled(&state) {
        media_manifest::rebuild(&data_dir.join("media"))
    } else {
        json!({ "enabled": false, "added": 0, "refreshed": 0, "dropped": 0 })
    };
    let media_changed = media["added"].as_u64().unwrap_or(0) > 0
        || media["refreshed"].as_u64().unwrap_or(0) > 0
        || media["dropped"].as_u64().unwrap_or(0) > 0;
    let status = if trash_removed > 0 || read_model_removed || refresh_backup || media_changed {
        "repaired"
    } else {
        "nothing_to_repair"
    };
    Ok(json!({
        "status": status,
        "trash_entries_removed": trash_removed,
        "read_model_removed": read_model_removed,
        "backup_refreshed": refresh_backup,
        "media_manifest": media,
    }))
}

fn rewrite_game_paths<F>(game: &Value, mut rewrite: F) -> (Value, Vec<String>)
where
    F: FnMut(&str) -> Result<String, portable_paths::PathTokenError>,
{
    let Some(obj) = game.as_object() else {
        return (game.clone(), Vec::new());
    };
    let mut copy = obj.clone();
    let mut problems = Vec::new();
    if let Some(p) = obj
        .get("path")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        match rewrite(p) {
            Ok(new_p) if new_p != p => {
                copy.insert("path".into(), Value::String(new_p));
            }
            Err(err) => problems.push(err.code.to_owned()),
            _ => {}
        }
    }
    for &list_name in EXTRA_LISTS {
        if let Some(Value::Array(items)) = copy.get_mut(list_name) {
            for item in items.iter_mut() {
                if let Some(item_obj) = item.as_object_mut()
                    && let Some(p) = item_obj
                        .get("path")
                        .and_then(Value::as_str)
                        .filter(|s| !s.is_empty())
                        .map(ToOwned::to_owned)
                {
                    match rewrite(&p) {
                        Ok(new_p) if new_p != p => {
                            item_obj.insert("path".into(), Value::String(new_p));
                        }
                        Err(err) => problems.push(err.code.to_owned()),
                        _ => {}
                    }
                }
            }
        }
    }
    (Value::Object(copy), problems)
}

pub fn expand_games(games: &[Value], root: &str) -> (Vec<Value>, Vec<String>) {
    if root.is_empty() {
        return (games.to_vec(), Vec::new());
    }
    let (windows, folded) = platform_rules();
    let mut out = Vec::with_capacity(games.len());
    let mut problems = Vec::new();
    for game in games {
        let gid = game
            .get("game_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        let (updated, gprobs) = rewrite_game_paths(game, |val| {
            if portable_paths::is_token(val) {
                portable_paths::decode_path(val, root, windows, folded)
            } else {
                Ok(val.to_owned())
            }
        });
        for code in gprobs {
            problems.push(format!("{gid}: {code}"));
        }
        out.push(updated);
    }
    (out, problems)
}

pub fn encode_games(games: &[Value], root: &str) -> Vec<Value> {
    if root.is_empty() {
        return games.to_vec();
    }
    let (windows, folded) = platform_rules();
    games
        .iter()
        .map(|game| {
            rewrite_game_paths(game, |val| {
                Ok(portable_paths::encode_path(val, root, windows, folded))
            })
            .0
        })
        .collect()
}

pub fn plan_move(games: &[Value], old_root: &str, new_root: &str) -> Result<Value, ApiError> {
    let (windows, folded) = platform_rules();
    let old = portable_paths::normalize_root(old_root, windows)
        .map_err(|e| ApiError::bad_request(e.message))?;
    let new = portable_paths::normalize_root(new_root, windows)
        .map_err(|e| ApiError::bad_request(e.message))?;
    if old.is_empty() || new.is_empty() {
        return Err(ApiError::bad_request("Both library folders are required."));
    }
    let mut tokenized = 0usize;
    let mut inside_old = 0usize;
    let mut missing = 0usize;
    let mut outside = 0usize;
    let mut ex_missing = Vec::new();
    let mut ex_outside = Vec::new();
    let mut rebased: BTreeMap<String, String> = BTreeMap::new();

    for game in games {
        let Some(obj) = game.as_object() else {
            continue;
        };
        let gid = obj.get("game_id").and_then(Value::as_str).unwrap_or("");
        let mut slots: Vec<(String, String)> = Vec::new();
        if let Some(p) = obj
            .get("path")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
        {
            slots.push(("path".to_owned(), p.to_owned()));
        }
        for &list_name in EXTRA_LISTS {
            if let Some(Value::Array(items)) = obj.get(list_name) {
                for (i, item) in items.iter().enumerate() {
                    if let Some(p) = item
                        .get("path")
                        .and_then(Value::as_str)
                        .filter(|s| !s.is_empty())
                    {
                        slots.push((format!("{list_name}.{i}"), p.to_owned()));
                    }
                }
            }
        }
        for (slot, p) in slots {
            match portable_paths::classify(&p, &old, windows, folded) {
                "token" => tokenized += 1,
                "inside_root" => {
                    inside_old += 1;
                    let rel = portable_paths::encode_path(&p, &old, windows, folded);
                    if let Ok(target) = portable_paths::decode_path(&rel, &new, windows, folded) {
                        if !Path::new(&target).exists() {
                            missing += 1;
                            if ex_missing.len() < EXAMPLE_LIMIT {
                                ex_missing.push(target.clone());
                            }
                        }
                        rebased.insert(format!("{gid}|{slot}"), target);
                    }
                }
                _ => {
                    outside += 1;
                    if ex_outside.len() < EXAMPLE_LIMIT {
                        ex_outside.push(format!("{gid}: {p}"));
                    }
                }
            }
        }
    }

    let pairs: Vec<(&String, &String)> = rebased.iter().collect();
    let digest_input = serde_json::to_vec(&json!({
        "new": new,
        "old": old,
        "rebased": pairs,
    }))
    .unwrap_or_default();
    let digest = sha256_hex(&digest_input);

    Ok(json!({
        "format": MOVE_PLAN_FORMAT,
        "plan_id": format!("mv-{}", &digest[..24]),
        "old_root": old,
        "new_root": new,
        "counts": {
            "tokenized": tokenized,
            "absolute_inside_old": inside_old,
            "missing": missing,
            "outside": outside,
            "symlink_outside": 0,
        },
        "examples": {
            "missing": ex_missing,
            "outside": ex_outside,
        },
        "rebased": rebased,
    }))
}

pub fn apply_move(
    state: &mut Value,
    plan_id: &str,
    old_root: &str,
    new_root: &str,
) -> Result<Value, ApiError> {
    let games = state
        .get("games")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let plan = plan_move(&games, old_root, new_root)?;
    if plan["plan_id"].as_str() != Some(plan_id) {
        return Err(ApiError::move_plan_stale(
            "The library changed since the preview. Run the preview again.",
        ));
    }
    let rebased = plan["rebased"].as_object().cloned().unwrap_or_default();
    let mut moved = 0usize;
    if let Some(Value::Array(game_list)) = state.get_mut("games") {
        for game in game_list.iter_mut() {
            let Some(obj) = game.as_object_mut() else {
                continue;
            };
            let gid = obj
                .get("game_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            if let Some(new_val) = rebased.get(&format!("{gid}|path")).and_then(Value::as_str) {
                obj.insert("path".into(), Value::String(new_val.to_owned()));
                moved += 1;
            }
            for &list_name in EXTRA_LISTS {
                if let Some(Value::Array(items)) = obj.get_mut(list_name) {
                    for (i, item) in items.iter_mut().enumerate() {
                        if let Some(new_val) = rebased
                            .get(&format!("{gid}|{list_name}.{i}"))
                            .and_then(Value::as_str)
                            && let Some(item_obj) = item.as_object_mut()
                        {
                            item_obj.insert("path".into(), Value::String(new_val.to_owned()));
                            moved += 1;
                        }
                    }
                }
            }
        }
    }
    let new_root_str = plan["new_root"].as_str().unwrap_or("").to_owned();
    if let Some(obj) = state.as_object_mut() {
        let settings = obj
            .entry("settings".to_owned())
            .or_insert_with(|| Value::Object(Map::new()));
        if let Some(s) = settings.as_object_mut() {
            s.insert("library_root".into(), Value::String(new_root_str.clone()));
            s.insert("path_mode".into(), Value::String("portable".to_owned()));
        }
    }
    Ok(json!({
        "moved": moved,
        "counts": plan["counts"],
        "library_root": new_root_str,
        "path_mode": "portable",
    }))
}
