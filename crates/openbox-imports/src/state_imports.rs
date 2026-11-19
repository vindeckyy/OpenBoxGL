//! Import consolidation, folder scan transactions, storefront scan status,
//! explorer facets, filter rule normalization, launcher menu, and integration tokens
//! (`pkg/state/imports.py`, `parity_filter_presets.py` subset, `parity_integration_tokens.py`).

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fs;
use std::path::Path;

use openbox_core::ApiError;
use openbox_emulators::load_adapters;
use openbox_emulators::tokens::build_launch_args;
use openbox_io::atomic_write_text;
use openbox_platform::{expand_user, join_command, which};
use openbox_runtime::AppState;
use serde_json::{Map, Value, json};

use crate::exclusions::filter_imported;
use crate::folder_import::{
    import_multi_platform, import_with_emulator_choice, recommend_emulators,
};
use crate::identity::{cross_source_identity, source_family, source_identities};
use crate::importers::{heroic_bases, steam_roots};
use crate::storefront::{catalog_entries_to_games, storefront_catalog};

pub const STOREFRONT_SOURCES: &[&str] = &["steam", "heroic", "lutris", "gameyfin"];
pub const INTEGRATION_PROVIDERS: &[&str] = &[
    "igdb",
    "screenscraper",
    "steamgriddb",
    "gameyfin",
    "retroachievements",
];
pub const MAX_TOKEN_LENGTH: usize = 4096;

pub fn validate_provider(provider: &str) -> Result<String, ApiError> {
    let val = provider.trim().to_lowercase();
    if !INTEGRATION_PROVIDERS.contains(&val.as_str()) {
        return Err(ApiError::bad_request("Unknown integration provider."));
    }
    Ok(val)
}

pub fn validate_token(token: &str) -> Result<String, ApiError> {
    let val = token.trim();
    if val.is_empty() || val.len() > MAX_TOKEN_LENGTH || val.bytes().any(|b| b < 0x20 || b == 0x7f)
    {
        return Err(ApiError::bad_request("Integration token is invalid."));
    }
    Ok(val.to_owned())
}

pub fn clean_tokens(value: Option<&Value>) -> Result<Map<String, Value>, ApiError> {
    let Some(v) = value else {
        return Ok(Map::new());
    };
    if v.is_null() {
        return Ok(Map::new());
    }
    let Some(obj) = v.as_object() else {
        return Err(ApiError::bad_request(
            "Integration tokens must be an object.",
        ));
    };
    let mut clean = Map::new();
    for (prov, tok) in obj {
        if let (Ok(k), Ok(t)) = (
            validate_provider(prov),
            validate_token(tok.as_str().unwrap_or("")),
        ) {
            clean.insert(k, Value::String(t));
        }
    }
    Ok(clean)
}

pub fn masked_tokens(tokens: &Map<String, Value>) -> Map<String, Value> {
    let mut masked = Map::new();
    for (provider, val) in tokens {
        let tok = val.as_str().unwrap_or("");
        let display = if tok.chars().count() > 8 {
            let head: String = tok.chars().take(4).collect();
            let tail: String = tok
                .chars()
                .rev()
                .take(2)
                .collect::<String>()
                .chars()
                .rev()
                .collect();
            format!("{head}…{tail}")
        } else {
            "••••".to_owned()
        };
        masked.insert(provider.clone(), Value::String(display));
    }
    masked
}

pub fn public_tokens(tokens: &Map<String, Value>) -> Map<String, Value> {
    let mut out = Map::new();
    for &p in INTEGRATION_PROVIDERS {
        let present = tokens
            .get(p)
            .and_then(Value::as_str)
            .is_some_and(|s| !s.is_empty());
        out.insert(p.to_owned(), Value::Bool(present));
    }
    out
}

fn launcher_label(game: &Value) -> String {
    let sp = |k: &str| {
        game.get(k)
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_owned()
    };
    if !sp("steam_app_id").is_empty() {
        return "Steam".to_owned();
    }
    if !sp("heroic_app_id").is_empty() {
        let src = {
            let hs = sp("heroic_source");
            if !hs.is_empty() { hs } else { sp("source") }
        };
        return if src.is_empty() {
            "Heroic".to_owned()
        } else {
            format!("Heroic ({src})")
        };
    }
    for (k, lbl) in [
        ("lutris_id", "Lutris"),
        ("gameyfin_id", "Gameyfin"),
        ("faugus_id", "Faugus"),
        ("gog_id", "GOG"),
        ("ea_id", "EA app"),
        ("ubisoft_id", "Ubisoft Connect"),
    ] {
        if !sp(k).is_empty() {
            return lbl.to_owned();
        }
    }
    let src = sp("source");
    if src.is_empty() {
        "Imported".to_owned()
    } else {
        src
    }
}

fn application_for_game(game: &Value, data_dir: &Path) -> Option<Value> {
    let path = game
        .get("path")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if path.is_empty() {
        return None;
    }
    let launch = game
        .get("launch")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    let command = if launch.is_empty() {
        String::new()
    } else {
        let argv = build_launch_args(launch, game, None, &data_dir.to_string_lossy(), "");
        join_command(&argv)
    };
    Some(json!({
        "name": format!("Launch with {}", launcher_label(game)),
        "path": path,
        "command": command,
    }))
}

pub fn merge_imported_game(
    target: &mut Value,
    source: &Value,
    add_launcher: bool,
    data_dir: &Path,
) {
    let mut source_ids: BTreeSet<String> = source_identities(target).into_iter().collect();
    source_ids.extend(source_identities(source));
    let Some(t) = target.as_object_mut() else {
        return;
    };
    if add_launcher && let Some(app) = application_for_game(source, data_dir) {
        let key = (
            app.get("path")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned(),
            app.get("command")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned(),
        );
        let apps = t
            .entry("applications".to_owned())
            .or_insert_with(|| Value::Array(Vec::new()));
        if let Some(arr) = apps.as_array_mut() {
            let exists = arr.iter().any(|e| {
                e.get("path").and_then(Value::as_str).unwrap_or("") == key.0
                    && e.get("command").and_then(Value::as_str).unwrap_or("") == key.1
            });
            if !exists {
                arr.push(app);
            }
        }
    }
    if !source_ids.is_empty() {
        t.insert(
            "source_identities".into(),
            Value::Array(source_ids.into_iter().map(Value::String).collect()),
        );
    }
    if source
        .get("heroic_app_id")
        .and_then(Value::as_str)
        .is_some_and(|s| !s.is_empty())
        && let Some(src) = source
            .get("source")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
        && t.get("heroic_source")
            .and_then(Value::as_str)
            .is_none_or(str::is_empty)
    {
        t.insert("heroic_source".into(), Value::String(src.to_owned()));
    }
    for field in [
        "steam_app_id",
        "heroic_app_id",
        "lutris_id",
        "gameyfin_id",
        "gameyfin_provider",
        "faugus_id",
        "gog_id",
        "ea_id",
        "ubisoft_id",
        "install_dir",
        "cover",
        "background",
        "clear_logo",
        "fanart",
        "banner",
        "icon",
    ] {
        if let Some(sv) = source.get(field)
            && !sv.is_null()
            && sv.as_str() != Some("")
            && t.get(field)
                .is_none_or(|v| v.is_null() || v.as_str() == Some(""))
        {
            t.insert(field.to_owned(), sv.clone());
        }
    }
    for flag in ["owned", "store_catalog", "store_installed"] {
        if let Some(sv) = source.get(flag)
            && !sv.is_null()
        {
            t.insert(flag.to_owned(), sv.clone());
        }
    }
    if let Some(sname) = source.get("name").and_then(Value::as_str).map(str::trim)
        && !sname.is_empty()
        && t.get("name").and_then(Value::as_str) != Some(sname)
    {
        let mut alt: Vec<String> = t
            .get("alternate_names")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .map(ToOwned::to_owned)
                    .collect()
            })
            .unwrap_or_default();
        if !alt.iter().any(|x| x == sname) && alt.len() < 20 {
            alt.push(sname.to_owned());
            t.insert(
                "alternate_names".into(),
                Value::Array(alt.into_iter().map(Value::String).collect()),
            );
        }
    }
    if source.get("store_installed") == Some(&Value::Bool(true))
        && t.get("store_installed") != Some(&Value::Bool(true))
    {
        for field in ["path", "launch", "platform", "install_dir"] {
            if let Some(sv) = source.get(field)
                && sv.as_str().is_some_and(|s| !s.is_empty())
            {
                t.insert(field.to_owned(), sv.clone());
            }
        }
        t.insert("store_installed".into(), Value::Bool(true));
    }
}

pub fn consolidate_existing_games(games: &mut Vec<Value>, data_dir: &Path) -> Vec<String> {
    let mut exact: HashMap<String, usize> = HashMap::new();
    let mut cross: HashMap<String, usize> = HashMap::new();
    let mut kept: Vec<Value> = Vec::new();
    let mut removed: Vec<String> = Vec::new();
    for game in games.drain(..) {
        let source_keys = source_identities(&game);
        let exact_target = source_keys.iter().find_map(|k| exact.get(k).copied());
        let title_ident = cross_source_identity(&game);
        let cross_target = if exact_target.is_none() {
            title_ident.as_ref().and_then(|ti| {
                let &idx = cross.get(ti)?;
                if source_family(&kept[idx]) != source_family(&game) {
                    Some(idx)
                } else {
                    None
                }
            })
        } else {
            None
        };
        if let Some(tidx) = exact_target.or(cross_target) {
            merge_imported_game(&mut kept[tidx], &game, exact_target.is_none(), data_dir);
            removed.push(
                game.get("name")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned(),
            );
            for k in source_identities(&kept[tidx]) {
                exact.entry(k).or_insert(tidx);
            }
            if let Some(ti) = cross_source_identity(&kept[tidx]) {
                cross.entry(ti).or_insert(tidx);
            }
        } else {
            let idx = kept.len();
            for k in source_keys {
                exact.entry(k).or_insert(idx);
            }
            if let Some(ti) = title_ident {
                cross.entry(ti).or_insert(idx);
            }
            kept.push(game);
        }
    }
    *games = kept;
    removed
}

pub fn import_folder_path(
    app: &AppState,
    folder: &str,
    recommend: bool,
    chosen_emulators: Option<&Map<String, Value>>,
) -> Result<(usize, usize, crate::folder_import::RecommendationsMap), ApiError> {
    let expanded = expand_user(Path::new(folder));
    if !expanded.is_dir() {
        return Err(ApiError::bad_request(format!(
            "Folder does not exist: {}",
            expanded.display()
        )));
    }
    let adapters = load_adapters(&app.root_dir, Some(&app.data_dir));
    let (candidates, mut recommendations) = if let Some(chosen) = chosen_emulators
        && !chosen.is_empty()
    {
        import_with_emulator_choice(folder, &adapters, chosen, &app.data_dir)?
    } else {
        let cands = import_multi_platform(folder, &adapters, true, None, &app.data_dir)?;
        let mut recs = BTreeMap::new();
        for item in &cands {
            let plat = item
                .get("platform")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            recs.entry(plat.clone())
                .or_insert_with(|| recommend_emulators(&plat, &adapters));
        }
        (cands, recs)
    };
    let found = candidates.len();
    if candidates.is_empty() {
        if !recommend {
            recommendations.clear();
        }
        return Ok((0, 0, recommendations));
    }
    let mut added_count = 0usize;
    let _ = app
        .transact_state(|state| {
            let existing: HashSet<String> = state
                .get("games")
                .and_then(Value::as_array)
                .map(|arr| {
                    arr.iter()
                        .filter_map(|g| {
                            g.get("path").and_then(Value::as_str).map(ToOwned::to_owned)
                        })
                        .collect()
                })
                .unwrap_or_default();
            let mut additions = Vec::new();
            for item in &candidates {
                let p = item.get("path").and_then(Value::as_str).unwrap_or("");
                if existing.contains(p) {
                    continue;
                }
                additions.push(item.clone());
            }
            added_count = additions.len();
            if !additions.is_empty()
                && let Some(Value::Array(games)) = state.get_mut("games")
            {
                games.extend(additions);
            }
            Ok(())
        })
        .map_err(|e| ApiError::bad_request(e.to_string()))?;
    if !recommend {
        recommendations.clear();
    }
    Ok((added_count, found, recommendations))
}

pub fn merge_imported_games<F>(
    app: &AppState,
    imported: Vec<Value>,
    identity_fn: F,
) -> Result<(usize, usize), ApiError>
where
    F: Fn(&Value) -> Vec<String>,
{
    let mut added = 0usize;
    let mut found = 0usize;
    let data_dir = app.data_dir.clone();
    let _ = app
        .transact_state(|state| {
            let filtered = filter_imported(imported.clone(), state);
            found = filtered.len();
            let default_progress = state
                .get("settings")
                .and_then(|s| s.get("progress_on_first_play"))
                .and_then(Value::as_str)
                .unwrap_or("Playing")
                .to_owned();
            let Some(Value::Array(games)) = state.get_mut("games") else {
                return Ok(());
            };
            let mut exact: HashMap<String, usize> = HashMap::new();
            let mut cross: HashMap<String, usize> = HashMap::new();
            let mut legacy_existing: HashSet<Vec<String>> = HashSet::new();
            for (idx, g) in games.iter().enumerate() {
                for k in source_identities(g) {
                    exact.entry(k).or_insert(idx);
                }
                if let Some(ti) = cross_source_identity(g) {
                    cross.entry(ti).or_insert(idx);
                }
                legacy_existing.insert(identity_fn(g));
            }
            let timestamp = jiff::Timestamp::now()
                .strftime("%Y-%m-%dT%H:%M:%S")
                .to_string();
            for mut game in filtered {
                let source_keys = source_identities(&game);
                let exact_idx = source_keys.iter().find_map(|k| exact.get(k).copied());
                let lid = identity_fn(&game);
                if exact_idx.is_some() || legacy_existing.contains(&lid) {
                    if let Some(idx) = exact_idx {
                        merge_imported_game(&mut games[idx], &game, false, &data_dir);
                        for k in source_identities(&games[idx]) {
                            exact.entry(k).or_insert(idx);
                        }
                    }
                    continue;
                }
                let title_ident = cross_source_identity(&game);
                if let Some(ref ti) = title_ident
                    && let Some(&idx) = cross.get(ti)
                    && source_family(&games[idx]) != source_family(&game)
                {
                    merge_imported_game(&mut games[idx], &game, true, &data_dir);
                    for k in source_identities(&games[idx]) {
                        exact.entry(k).or_insert(idx);
                    }
                    continue;
                }
                if let Some(m) = game.as_object_mut() {
                    m.insert("added_at".into(), Value::String(timestamp.clone()));
                    if !default_progress.is_empty()
                        && m.get("progress")
                            .and_then(Value::as_str)
                            .is_none_or(str::is_empty)
                    {
                        m.insert("progress".into(), Value::String(default_progress.clone()));
                    }
                }
                let new_idx = games.len();
                legacy_existing.insert(lid);
                for k in source_identities(&game) {
                    exact.entry(k).or_insert(new_idx);
                }
                if let Some(ti) = title_ident {
                    cross.entry(ti).or_insert(new_idx);
                }
                games.push(game);
                added += 1;
            }
            Ok(())
        })
        .map_err(|e| ApiError::bad_request(e.to_string()))?;
    Ok((added, found))
}

pub fn read_storefront_status(data_dir: &Path) -> Value {
    let path = data_dir.join("storefront-status.json");
    let Ok(text) = fs::read_to_string(&path) else {
        return json!({"sources": []});
    };
    let Ok(val) = serde_json::from_str::<Value>(&text) else {
        return json!({"sources": []});
    };
    if val.get("sources").is_some_and(Value::is_array) {
        val
    } else {
        json!({"sources": []})
    }
}

fn write_storefront_status(data_dir: &Path, sources: &[Value]) {
    let payload = json!({"sources": sources});
    let text = format!(
        "{}\n",
        serde_json::to_string_pretty(&payload).unwrap_or_else(|_| "{\"sources\":[]}".to_owned())
    );
    let _ = atomic_write_text(&data_dir.join("storefront-status.json"), &text, 0o600);
}

fn storefront_present(source: &str, settings: &Value) -> bool {
    match source {
        "steam" => !steam_roots(None).is_empty(),
        "heroic" => !heroic_bases(None).is_empty(),
        "lutris" => which("lutris").is_some() || which("flatpak").is_some(),
        "gameyfin" => settings
            .get("gameyfin_url")
            .and_then(Value::as_str)
            .is_some_and(|s| !s.trim().is_empty()),
        _ => false,
    }
}

pub fn storefront_scan_worker(
    app: &AppState,
    sources: &[String],
    include_uninstalled: bool,
) -> Result<Value, ApiError> {
    let state = app
        .load_state_view()
        .map_err(|e| ApiError::bad_request(e.to_string()))?;
    let settings = state.get("settings").cloned().unwrap_or_else(|| json!({}));
    let now = jiff::Timestamp::now()
        .strftime("%Y-%m-%dT%H:%M:%S")
        .to_string();
    let mut summary = Vec::new();
    for source in sources {
        let name = match source.as_str() {
            "steam" => "Steam",
            "heroic" => "Heroic",
            "lutris" => "Lutris",
            "gameyfin" => "Gameyfin",
            other => other,
        };
        let present = storefront_present(source, &settings);
        let mut status = json!({
            "id": source,
            "name": name,
            "status": "pending",
            "installed": present,
            "last_scan": now,
            "error": "",
            "games": 0,
        });
        if !present {
            status["status"] = json!("skipped");
            summary.push(status);
            continue;
        }
        match storefront_catalog(source, &settings) {
            Err(err) => {
                status["status"] = json!("error");
                status["error"] = json!(err.message);
                summary.push(status);
            }
            Ok(entries) => {
                let candidates = catalog_entries_to_games(&entries, false, !include_uninstalled);
                let mut map: HashMap<String, Value> = HashMap::new();
                for g in candidates {
                    let k = match source.as_str() {
                        "steam" => g
                            .get("steam_app_id")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_owned(),
                        "heroic" => format!(
                            "{}:{}",
                            g.get("source").and_then(Value::as_str).unwrap_or(""),
                            g.get("heroic_app_id").and_then(Value::as_str).unwrap_or("")
                        ),
                        "lutris" => g
                            .get("lutris_id")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_owned(),
                        _ => g
                            .get("gameyfin_id")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_owned(),
                    };
                    if !k.is_empty() {
                        map.insert(k, g);
                    }
                }
                let mut changed = 0usize;
                let _ = app.transact_state(|cur| {
                    if let Some(Value::Array(games)) = cur.get_mut("games") {
                        for g in games.iter_mut() {
                            let k = match source.as_str() {
                                "steam" => g
                                    .get("steam_app_id")
                                    .and_then(Value::as_str)
                                    .unwrap_or("")
                                    .to_owned(),
                                "heroic" => format!(
                                    "{}:{}",
                                    g.get("source").and_then(Value::as_str).unwrap_or(""),
                                    g.get("heroic_app_id").and_then(Value::as_str).unwrap_or("")
                                ),
                                "lutris" => g
                                    .get("lutris_id")
                                    .and_then(Value::as_str)
                                    .unwrap_or("")
                                    .to_owned(),
                                _ => g
                                    .get("gameyfin_id")
                                    .and_then(Value::as_str)
                                    .unwrap_or("")
                                    .to_owned(),
                            };
                            if let Some(fresh) = map.get(&k)
                                && let Some(obj) = g.as_object_mut()
                            {
                                for f in [
                                    "store_installed",
                                    "install_dir",
                                    "path",
                                    "launch",
                                    "install_uri",
                                    "repair_uri",
                                ] {
                                    if let Some(v) = fresh.get(f) {
                                        obj.insert(f.to_owned(), v.clone());
                                    }
                                }
                                changed += 1;
                            }
                        }
                    }
                    Ok(())
                });
                status["status"] = json!(if changed > 0 { "ok" } else { "skipped" });
                status["games"] = json!(changed);
                summary.push(status);
            }
        }
    }
    write_storefront_status(&app.data_dir, &summary);
    Ok(json!({"sources": summary, "checked": summary.len()}))
}

pub fn explorer_facets(games: &[Value], field: &str, limit: usize) -> Vec<Value> {
    if !matches!(
        field,
        "genre" | "developer" | "publisher" | "platform" | "progress" | "esrb"
    ) {
        return Vec::new();
    }
    let mut counts: HashMap<String, usize> = HashMap::new();
    for game in games {
        let Some(obj) = game.as_object() else {
            continue;
        };
        if obj.get("hidden").and_then(Value::as_bool).unwrap_or(false) {
            continue;
        }
        match field {
            "genre" => {
                for part in obj
                    .get("genre")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .split(',')
                {
                    let label = part.trim();
                    if !label.is_empty() {
                        *counts.entry(label.to_owned()).or_insert(0) += 1;
                    }
                }
            }
            "developer" | "publisher" => {
                let label = obj.get(field).and_then(Value::as_str).unwrap_or("").trim();
                if !label.is_empty() {
                    *counts.entry(label.to_owned()).or_insert(0) += 1;
                }
            }
            "platform" => {
                let raw = obj
                    .get("platform")
                    .and_then(Value::as_str)
                    .unwrap_or("Unspecified")
                    .trim();
                let label = if raw.is_empty() { "Unspecified" } else { raw };
                *counts.entry(label.to_owned()).or_insert(0) += 1;
            }
            "progress" => {
                let raw = obj
                    .get("progress")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .trim();
                let label = if raw.is_empty() { "Unplayed" } else { raw };
                *counts.entry(label.to_owned()).or_insert(0) += 1;
            }
            "esrb" => {
                let raw = obj.get("esrb").and_then(Value::as_str).unwrap_or("").trim();
                let label = if raw.is_empty() { "Unrated" } else { raw };
                *counts.entry(label.to_owned()).or_insert(0) += 1;
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

pub fn normalize_filter_rules(rules: &Value) -> Result<Map<String, Value>, ApiError> {
    let Some(obj) = rules.as_object() else {
        return Err(ApiError::bad_request("Preset rules must be an object."));
    };
    const KEYS: &[&str] = &[
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
    let mut clean = Map::new();
    for &key in KEYS {
        let Some(val) = obj.get(key) else {
            continue;
        };
        match key {
            "exclude" => {
                if let Some(m) = val.as_object()
                    && !m.is_empty()
                {
                    clean.insert(key.to_owned(), Value::Object(m.clone()));
                }
            }
            "source" | "achievements_source" => {
                let list: Vec<String> = match val {
                    Value::String(s) if !s.trim().is_empty() => vec![s.trim().to_owned()],
                    Value::Array(arr) => arr
                        .iter()
                        .filter_map(Value::as_str)
                        .map(|s| s.trim().to_owned())
                        .filter(|s| !s.is_empty())
                        .take(20)
                        .collect(),
                    _ => Vec::new(),
                };
                if !list.is_empty() {
                    let mut sorted = list;
                    sorted.sort();
                    clean.insert(
                        key.to_owned(),
                        Value::Array(sorted.into_iter().map(Value::String).collect()),
                    );
                }
            }
            "favorite" | "hidden" | "has_saves" | "has_achievements" | "has_missing_media"
            | "has_highscores" | "vertical" | "requires_chd" => match val {
                Value::Bool(b) => {
                    clean.insert(key.to_owned(), Value::Bool(*b));
                }
                Value::String(s) if s == "true" || s == "false" => {
                    clean.insert(key.to_owned(), Value::Bool(s == "true"));
                }
                _ => {}
            },
            "installed" => match val {
                Value::Bool(b) => {
                    clean.insert(
                        key.to_owned(),
                        Value::String(if *b { "installed" } else { "uninstalled" }.to_owned()),
                    );
                }
                Value::String(s) if matches!(s.as_str(), "installed" | "uninstalled" | "all") => {
                    clean.insert(key.to_owned(), Value::String(s.clone()));
                }
                _ => {}
            },
            "genre" | "developer" | "publisher" | "region" => match val {
                Value::Object(m) => {
                    let op = m.get("op").and_then(Value::as_str).unwrap_or("");
                    if !matches!(op, "contains_any" | "not_contains_any") {
                        return Err(ApiError::bad_request(
                            "Text filter operator must be contains_any or not_contains_any.",
                        ));
                    }
                    let vals: Vec<Value> = m
                        .get("values")
                        .and_then(Value::as_array)
                        .map(|a| {
                            a.iter()
                                .filter_map(Value::as_str)
                                .map(|s| Value::String(s.trim().chars().take(80).collect()))
                                .take(20)
                                .collect()
                        })
                        .unwrap_or_default();
                    if !vals.is_empty() {
                        clean.insert(key.to_owned(), json!({"op": op, "values": vals}));
                    }
                }
                _ => {
                    let text = val.as_str().unwrap_or("").trim();
                    if !text.is_empty() && text != "all" {
                        clean.insert(key.to_owned(), Value::String(text.to_owned()));
                    }
                }
            },
            _ => {
                let text = val.as_str().unwrap_or("").trim();
                let capped: String = if key == "query" {
                    text.chars().take(500).collect()
                } else {
                    text.to_owned()
                };
                if !capped.is_empty() && capped != "all" {
                    clean.insert(key.to_owned(), Value::String(capped));
                }
            }
        }
    }
    if clean.is_empty() {
        return Err(ApiError::bad_request(
            "Preset needs at least one filter rule.",
        ));
    }
    Ok(clean)
}

pub fn launcher_menu_items(games: &[Value]) -> Vec<Value> {
    let mut items = vec![
        json!({"id": "bigbox", "label": "Open Big Box"}),
        json!({"id": "settings", "label": "Open Settings"}),
        json!({"id": "search", "label": "Search library"}),
    ];
    for game in games.iter().take(40) {
        if let Some(name) = game.get("name").and_then(Value::as_str)
            && !name.is_empty()
        {
            let id = game.get("id").and_then(Value::as_i64).unwrap_or(0);
            items.push(json!({
                "id": format!("launch:{id}"),
                "label": name,
            }));
        }
    }
    items
}
