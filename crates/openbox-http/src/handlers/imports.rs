//! Importer & storefront endpoints (`handlers/imports.py` + `/api/v2/steam/action`).

use std::collections::{BTreeMap, HashSet};
use std::path::Path;
use std::sync::Arc;

use axum::http::StatusCode;
use axum::response::Response;
use openbox_core::ApiError;
use openbox_emulators::emulators::install_emulator;
use openbox_emulators::load_adapters;
use openbox_imports::{
    STOREFRONT_SOURCES, add_exclusion, catalog_entries_to_games, esde_import, import_arcade,
    import_ea, import_epic, import_folder_path, import_gog, import_heroic, import_loose_arcade,
    import_lutris, import_msu1, import_pico8, import_rpcs3_hdd, import_scummvm, import_steam,
    import_ubisoft, import_vita3k, import_whdload, import_xbox360_folder, launchbox_import,
    list_exclusions, merge_imported_games, read_storefront_status, remove_exclusion,
    storefront_catalog, storefront_scan_worker,
};
use openbox_platform::{open_path, platform_features};
use serde_json::{Map, Value, json};

use crate::handlers::shared::{RequestCtx, bool_field, str_field};
use crate::middleware::{api_error_response, json_response};

fn required_folder_path(payload: &Map<String, Value>) -> Result<String, ApiError> {
    let val = payload
        .get("folder")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if val.is_empty() {
        return Err(ApiError::bad_request("Folder path is required."));
    }
    Ok(val.to_owned())
}

fn require_native_storefronts(source: &str) -> Result<(), ApiError> {
    if !platform_features().native_storefronts {
        return Err(ApiError::bad_request(format!(
            "{} import is only available on Windows.",
            source.to_uppercase()
        )));
    }
    Ok(())
}

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    match ctx.path {
        "/api/v2/storefronts" => {
            let features = platform_features();
            let mut sources = vec!["steam", "gameyfin"];
            if features.heroic {
                sources.push("heroic");
            }
            if features.lutris {
                sources.push("lutris");
            }
            if features.native_storefronts {
                sources.extend(["gog", "ea", "ubisoft"]);
            }
            let builtins: Vec<Value> = sources
                .into_iter()
                .map(|src| {
                    let mut chars = src.chars();
                    let name = match chars.next() {
                        Some(first) => format!("{}{}", first.to_uppercase(), chars.as_str()),
                        None => String::new(),
                    };
                    json!({
                        "id": src,
                        "name": name,
                        "kind": "builtin",
                        "installed": true,
                    })
                })
                .collect();
            Some(json_response(
                StatusCode::OK,
                &json!({"storefronts": builtins}),
                ctx.instance,
            ))
        }
        "/api/v2/storefront/status" => {
            let st = read_storefront_status(&ctx.app.data_dir);
            Some(json_response(StatusCode::OK, &st, ctx.instance))
        }
        "/api/v2/storefront/catalog" => {
            let source = ctx.query_first("source").unwrap_or_default();
            let settings = ctx
                .app
                .load_state_view()
                .ok()
                .and_then(|s| s.get("settings").cloned())
                .unwrap_or_else(|| json!({}));
            match storefront_catalog(&source, &settings) {
                Ok(catalog) => Some(json_response(
                    StatusCode::OK,
                    &json!({"catalog": catalog}),
                    ctx.instance,
                )),
                Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/import/exclusions" => match ctx.app.load_state_view() {
            Ok(state) => Some(json_response(
                StatusCode::OK,
                &json!({"exclusions": list_exclusions(&state)}),
                ctx.instance,
            )),
            Err(err) => Some(api_error_response(
                &ApiError::state_unavailable(err.to_string()),
                ctx.request_id,
                ctx.instance,
            )),
        },
        _ => None,
    }
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/steam/action" => {
            let app_id = str_field(payload, "app_id");
            let valid_id = app_id.chars().all(|c| c.is_ascii_digit())
                && app_id
                    .parse::<u64>()
                    .is_ok_and(|n| n > 0 && n < 100_000_000);
            if !valid_id {
                return Some(api_error_response(
                    &ApiError::bad_request("A numeric Steam app id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let action = {
                let a = str_field(payload, "action");
                if a.is_empty() { "install" } else { a }
            };
            if !matches!(action, "install" | "repair") {
                return Some(api_error_response(
                    &ApiError::bad_request("Steam action must be install or repair."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let verb = if action == "repair" {
                "validate"
            } else {
                "install"
            };
            if let Err(err) = open_path(&format!("steam://{verb}/{app_id}")) {
                return Some(api_error_response(
                    &ApiError::bad_request(format!("Steam did not open: {err}")),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            Some(json_response(
                StatusCode::OK,
                &json!({"app_id": app_id, "action": action}),
                ctx.instance,
            ))
        }
        "/api/v2/storefront/scan" => {
            let known: Vec<String> = STOREFRONT_SOURCES.iter().map(|s| (*s).to_owned()).collect();
            let sources: Vec<String> = match payload.get("sources") {
                None | Some(Value::Null) => known.clone(),
                Some(Value::Array(arr)) if arr.len() <= 20 => {
                    let mut out = Vec::new();
                    for v in arr {
                        let s = v.as_str().unwrap_or("").trim().to_lowercase();
                        if !known.contains(&s) {
                            let disp = if s.is_empty() { "(blank)" } else { s.as_str() };
                            return Some(api_error_response(
                                &ApiError::bad_request(format!(
                                    "Unknown storefront source: {disp}"
                                )),
                                ctx.request_id,
                                ctx.instance,
                            ));
                        }
                        if !out.contains(&s) {
                            out.push(s);
                        }
                    }
                    out
                }
                _ => {
                    return Some(api_error_response(
                        &ApiError::bad_request("sources must be a list of storefront ids."),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            if sources.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("At least one storefront source is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let include_uninstalled = payload
                .get("include_uninstalled")
                .and_then(Value::as_bool)
                .unwrap_or(true);
            let app = Arc::clone(&ctx.app);
            let src_clone = sources.clone();
            let job = match ctx.app.jobs.submit(
                "storefront-scan",
                true,
                Some("storefront.scan"),
                None,
                move |_ctx| {
                    storefront_scan_worker(&app, &src_clone, include_uninstalled)
                        .map_err(|e| e.message)
                },
            ) {
                Ok(j) => j,
                Err(e) => {
                    return Some(api_error_response(
                        &ApiError::job_state_conflict(e),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            Some(json_response(
                StatusCode::ACCEPTED,
                &json!({
                    "job_id": job.get("job_id").cloned().unwrap_or(Value::Null),
                    "sources": sources,
                }),
                ctx.instance,
            ))
        }
        "/api/v2/storefront/import" => {
            let source = str_field(payload, "source");
            if source.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("source is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let source_key = source.to_lowercase();
            if !STOREFRONT_SOURCES.contains(&source_key.as_str()) {
                return Some(api_error_response(
                    &ApiError::bad_request(
                        "Storefront source must be steam, heroic, lutris, or gameyfin.",
                    ),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let settings = ctx
                .app
                .load_state_view()
                .ok()
                .and_then(|s| s.get("settings").cloned())
                .unwrap_or_else(|| json!({}));
            let catalog = match storefront_catalog(&source_key, &settings) {
                Ok(c) => c,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let imported = catalog_entries_to_games(
                &catalog,
                bool_field(payload, "uninstalled_only"),
                bool_field(payload, "installed_only"),
            );
            let imp_len = imported.len();
            let sk = source_key.clone();
            match merge_imported_games(&ctx.app, imported, |g| match sk.as_str() {
                "steam" => vec![
                    "steam".into(),
                    g.get("steam_app_id")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned(),
                ],
                "heroic" => vec![
                    "heroic".into(),
                    g.get("source")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned(),
                    g.get("heroic_app_id")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned(),
                ],
                "lutris" => vec![
                    "lutris".into(),
                    g.get("lutris_id")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned(),
                ],
                _ => vec![
                    "gameyfin".into(),
                    g.get("gameyfin_id")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned(),
                ],
            }) {
                Ok((added, found)) => {
                    ctx.app.media_probe.clear_caches();
                    Some(json_response(
                        StatusCode::OK,
                        &json!({"added": added, "found": found, "imported": imp_len}),
                        ctx.instance,
                    ))
                }
                Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/import" => {
            let folder = match required_folder_path(payload) {
                Ok(f) => f,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let chosen = payload.get("chosen_emulators").and_then(Value::as_object);
            ctx.app.sse.broadcast_event(
                "job.progress",
                &json!({"job": "import", "folder": folder, "state": "running"}),
            );
            match import_folder_path(&ctx.app, &folder, true, chosen) {
                Ok((added, found, recommendations)) => {
                    ctx.app.media_probe.clear_caches();
                    ctx.app.sse.broadcast_event(
                        "job.progress",
                        &json!({"job": "import", "folder": folder, "added": added, "found": found, "state": "done"}),
                    );
                    Some(json_response(
                        StatusCode::OK,
                        &json!({"added": added, "found": found, "recommendations": recommendations}),
                        ctx.instance,
                    ))
                }
                Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/import/wizard" => {
            let folder = match required_folder_path(payload) {
                Ok(f) => f,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let chosen = match payload.get("chosen_emulators") {
                None => Map::new(),
                Some(Value::Object(m)) => m.clone(),
                _ => {
                    return Some(api_error_response(
                        &ApiError::bad_request("chosen_emulators must be an object."),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let (added, found, recommendations) =
                match import_folder_path(&ctx.app, &folder, true, Some(&chosen)) {
                    Ok(r) => r,
                    Err(err) => {
                        return Some(api_error_response(&err, ctx.request_id, ctx.instance));
                    }
                };
            let mut installs = Vec::new();
            let mut profiles: BTreeMap<String, String> = BTreeMap::new();
            let mut managed = HashSet::new();
            let adapters = load_adapters(&ctx.app.root_dir, Some(&ctx.app.data_dir));
            for val in chosen.values() {
                let app_id = val.as_str().unwrap_or("").trim();
                if app_id.is_empty() {
                    continue;
                }
                if let Ok(installed_profiles) = install_emulator(&adapters, app_id) {
                    for (k, v) in installed_profiles {
                        if let Some(s) = v.as_str() {
                            profiles.insert(k, s.to_owned());
                        }
                    }
                    if app_id == "org.mamedev.MAME" {
                        managed.insert("mame".to_owned());
                    } else if app_id == "org.libretro.RetroArch" {
                        managed.insert("retroarch".to_owned());
                    }
                    installs.push(app_id.to_owned());
                }
            }
            if !profiles.is_empty() || !managed.is_empty() {
                let _ = ctx.app.transact_state(|state| {
                    if let Some(pm) = state.get_mut("profiles").and_then(Value::as_object_mut) {
                        for (k, v) in &profiles {
                            pm.entry(k.clone())
                                .or_insert_with(|| Value::String(v.clone()));
                        }
                    }
                    if let Some(obj) = state.as_object_mut() {
                        let es = obj
                            .entry("emulator_settings".to_owned())
                            .or_insert_with(|| Value::Object(Map::new()));
                        if let Some(esm) = es.as_object_mut() {
                            for eid in &managed {
                                let entry = esm
                                    .entry(eid.clone())
                                    .or_insert_with(|| Value::Object(Map::new()));
                                if let Some(em) = entry.as_object_mut() {
                                    em.insert("managed".into(), Value::Bool(true));
                                }
                            }
                        }
                    }
                    Ok(())
                });
            }
            ctx.app.media_probe.clear_caches();
            Some(json_response(
                StatusCode::OK,
                &json!({
                    "added": added,
                    "found": found,
                    "recommendations": recommendations,
                    "installed": installs,
                }),
                ctx.instance,
            ))
        }
        "/api/v2/import/xbox360" => {
            let folder = match required_folder_path(payload) {
                Ok(f) => f,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            match import_xbox360_folder(&folder, str_field(payload, "command")).and_then(|imp| {
                merge_imported_games(&ctx.app, imp, |g| {
                    vec![
                        "path".into(),
                        g.get("path")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_owned(),
                    ]
                })
            }) {
                Ok((added, found)) => {
                    ctx.app.media_probe.clear_caches();
                    Some(json_response(
                        StatusCode::OK,
                        &json!({"added": added, "found": found}),
                        ctx.instance,
                    ))
                }
                Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/import/loose-arcade" => {
            let folder = match required_folder_path(payload) {
                Ok(f) => f,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            match import_loose_arcade(&folder, str_field(payload, "command")).and_then(|imp| {
                merge_imported_games(&ctx.app, imp, |g| {
                    vec![
                        "path".into(),
                        g.get("path")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_owned(),
                    ]
                })
            }) {
                Ok((added, found)) => {
                    ctx.app.media_probe.clear_caches();
                    Some(json_response(
                        StatusCode::OK,
                        &json!({"added": added, "found": found}),
                        ctx.instance,
                    ))
                }
                Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/import/watch" => {
            let folders: Vec<String> = ctx
                .app
                .load_state_view()
                .ok()
                .and_then(|s| {
                    s.get("settings")
                        .and_then(|st| st.get("watch_folders"))
                        .and_then(Value::as_array)
                        .map(|a| {
                            a.iter()
                                .filter_map(Value::as_str)
                                .map(ToOwned::to_owned)
                                .collect()
                        })
                })
                .unwrap_or_default();
            let mut added = 0usize;
            let mut found = 0usize;
            let mut errors = Vec::new();
            for folder in folders {
                match import_folder_path(&ctx.app, &folder, false, None) {
                    Ok((a, f, _)) => {
                        added += a;
                        found += f;
                    }
                    Err(err) => errors.push(err.message),
                }
            }
            Some(json_response(
                StatusCode::OK,
                &json!({"added": added, "found": found, "errors": errors}),
                ctx.instance,
            ))
        }
        "/api/v2/import/steam" => {
            let mut errors = Vec::new();
            let imported = import_steam(None, &mut errors);
            match merge_imported_games(&ctx.app, imported, |g| {
                vec![
                    "steam".into(),
                    g.get("steam_app_id")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned(),
                ]
            }) {
                Ok((added, found)) => {
                    ctx.app.media_probe.clear_caches();
                    Some(json_response(
                        StatusCode::OK,
                        &json!({"added": added, "found": found, "errors": errors}),
                        ctx.instance,
                    ))
                }
                Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/import/heroic" => match import_heroic(None).and_then(|imp| {
            merge_imported_games(&ctx.app, imp, |g| {
                vec![
                    "heroic".into(),
                    g.get("source")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned(),
                    g.get("heroic_app_id")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned(),
                ]
            })
        }) {
            Ok((added, found)) => {
                ctx.app.media_probe.clear_caches();
                Some(json_response(
                    StatusCode::OK,
                    &json!({"added": added, "found": found}),
                    ctx.instance,
                ))
            }
            Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
        },
        "/api/v2/import/lutris" => match import_lutris(None).and_then(|imp| {
            merge_imported_games(&ctx.app, imp, |g| {
                vec![
                    "lutris".into(),
                    g.get("lutris_id")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned(),
                ]
            })
        }) {
            Ok((added, found)) => {
                ctx.app.media_probe.clear_caches();
                Some(json_response(
                    StatusCode::OK,
                    &json!({"added": added, "found": found}),
                    ctx.instance,
                ))
            }
            Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
        },
        "/api/v2/import/epic" => {
            let imported = import_epic();
            match merge_imported_games(&ctx.app, imported, |g| {
                vec![
                    "epic".into(),
                    g.get("heroic_app_id")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned(),
                ]
            }) {
                Ok((added, found)) => {
                    ctx.app.media_probe.clear_caches();
                    Some(json_response(
                        StatusCode::OK,
                        &json!({"added": added, "found": found}),
                        ctx.instance,
                    ))
                }
                Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/import/gog" | "/api/v2/import/ea" | "/api/v2/import/ubisoft" => {
            let (source, id_field) = match ctx.path {
                "/api/v2/import/gog" => ("gog", "gog_id"),
                "/api/v2/import/ea" => ("ea", "ea_id"),
                _ => ("ubisoft", "ubisoft_id"),
            };
            if let Err(err) = require_native_storefronts(source) {
                return Some(api_error_response(&err, ctx.request_id, ctx.instance));
            }
            let mut errors = Vec::new();
            let imported = match source {
                "gog" => import_gog(&mut errors),
                "ea" => import_ea(&mut errors),
                _ => import_ubisoft(&mut errors),
            };
            match merge_imported_games(&ctx.app, imported, |g| {
                vec![
                    source.to_owned(),
                    g.get(id_field)
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned(),
                ]
            }) {
                Ok((added, found)) => {
                    ctx.app.media_probe.clear_caches();
                    Some(json_response(
                        StatusCode::OK,
                        &json!({"added": added, "found": found, "errors": errors}),
                        ctx.instance,
                    ))
                }
                Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/import/arcade" => {
            let folder = match required_folder_path(payload) {
                Ok(f) => f,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let skip_inputs: Vec<String> = match payload.get("skip_inputs") {
                None | Some(Value::Null) => Vec::new(),
                Some(Value::Array(arr)) if arr.len() <= 20 => arr
                    .iter()
                    .filter_map(Value::as_str)
                    .map(ToOwned::to_owned)
                    .collect(),
                _ => {
                    return Some(api_error_response(
                        &ApiError::bad_request("skip_inputs must be a list of input types."),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let players = {
                let p = str_field(payload, "players");
                if p.is_empty() { "any" } else { p }
            };
            let source = {
                let s = str_field(payload, "source");
                if s.is_empty() { "MAME" } else { s }
            };
            let imported = match import_arcade(
                &folder,
                str_field(payload, "dat"),
                str_field(payload, "command"),
                source,
                players,
                bool_field(payload, "skip_chd"),
                &skip_inputs,
            ) {
                Ok(g) => g,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let make_playlists = bool_field(payload, "make_playlists");
            let found = imported.len();
            let mut counts = Map::new();
            for kind in ["parent", "merged", "split", "non-merged"] {
                let c = imported
                    .iter()
                    .filter(|g| g.get("set_type").and_then(Value::as_str) == Some(kind))
                    .count();
                counts.insert(kind.to_owned(), json!(c));
            }
            let mut added = 0usize;
            let _ = ctx.app.transact_state(|state| {
                let existing: HashSet<(String, String)> = state
                    .get("games")
                    .and_then(Value::as_array)
                    .map(|arr| {
                        arr.iter()
                            .filter_map(|g| {
                                let rn = g.get("rom_name").and_then(Value::as_str)?;
                                if rn.is_empty() {
                                    return None;
                                }
                                let src = g.get("source").and_then(Value::as_str).unwrap_or("");
                                Some((src.to_owned(), rn.to_owned()))
                            })
                            .collect()
                    })
                    .unwrap_or_default();
                let ts = jiff::Timestamp::now()
                    .strftime("%Y-%m-%dT%H:%M:%S")
                    .to_string();
                let mut new_games = Vec::new();
                for g in &imported {
                    let key = (
                        g.get("source").and_then(Value::as_str).unwrap_or("").to_owned(),
                        g.get("rom_name").and_then(Value::as_str).unwrap_or("").to_owned(),
                    );
                    if !existing.contains(&key) {
                        let mut item = g.clone();
                        if let Some(m) = item.as_object_mut() {
                            m.insert("added_at".into(), Value::String(ts.clone()));
                        }
                        new_games.push(item);
                    }
                }
                added = new_games.len();
                if let Some(Value::Array(games)) = state.get_mut("games") {
                    games.extend(new_games);
                }
                if make_playlists
                    && let Some(obj) = state.as_object_mut()
                {
                    let pls = obj
                        .entry("playlists".to_owned())
                        .or_insert_with(|| Value::Array(Vec::new()));
                    if let Some(arr) = pls.as_array_mut() {
                        let names: HashSet<String> = arr
                            .iter()
                            .filter_map(|i| i.get("name").and_then(Value::as_str).map(ToOwned::to_owned))
                            .collect();
                        if !names.contains("Vertical") {
                            arr.push(json!({"name": "Vertical", "type": "filter", "rules": {"vertical": true}}));
                        }
                        if !names.contains("Requires CHD") {
                            arr.push(json!({"name": "Requires CHD", "type": "filter", "rules": {"requires_chd": true}}));
                        }
                    }
                }
                Ok(())
            });
            ctx.app.media_probe.clear_caches();
            let playlists = if make_playlists {
                json!(["Vertical", "Requires CHD"])
            } else {
                json!([])
            };
            Some(json_response(
                StatusCode::OK,
                &json!({"added": added, "found": found, "sets": Value::Object(counts), "playlists": playlists}),
                ctx.instance,
            ))
        }
        "/api/v2/import/scummvm" => {
            let imp = import_scummvm(None);
            match merge_imported_games(&ctx.app, imp, |g| {
                vec![
                    "scummvm".into(),
                    g.get("scummvm_id")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned(),
                ]
            }) {
                Ok((added, found)) => {
                    ctx.app.media_probe.clear_caches();
                    Some(json_response(
                        StatusCode::OK,
                        &json!({"added": added, "found": found}),
                        ctx.instance,
                    ))
                }
                Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/import/rpcs3" => {
            let mut errs = Vec::new();
            let imp = import_rpcs3_hdd(None, &mut errs);
            match merge_imported_games(&ctx.app, imp, |g| {
                vec![
                    "rpcs3".into(),
                    g.get("path")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned(),
                ]
            }) {
                Ok((added, found)) => {
                    ctx.app.media_probe.clear_caches();
                    Some(json_response(
                        StatusCode::OK,
                        &json!({"added": added, "found": found}),
                        ctx.instance,
                    ))
                }
                Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/import/vita3k" => {
            let imp = import_vita3k(None);
            match merge_imported_games(&ctx.app, imp, |g| {
                vec![
                    "vita3k".into(),
                    g.get("path")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned(),
                ]
            }) {
                Ok((added, found)) => {
                    ctx.app.media_probe.clear_caches();
                    Some(json_response(
                        StatusCode::OK,
                        &json!({"added": added, "found": found}),
                        ctx.instance,
                    ))
                }
                Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/import/pico8" | "/api/v2/import/msu1" | "/api/v2/import/whdload" => {
            let folder = match required_folder_path(payload) {
                Ok(f) => f,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let mut errors = Vec::new();
            let (label, res) = match ctx.path {
                "/api/v2/import/pico8" => ("pico-8", import_pico8(&folder, &mut errors)),
                "/api/v2/import/msu1" => ("msu-1", import_msu1(&folder, &mut errors)),
                _ => ("whdload", import_whdload(&folder, &mut errors)),
            };
            match res.and_then(|imp| {
                merge_imported_games(&ctx.app, imp, |g| {
                    vec![
                        label.to_owned(),
                        g.get("path")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_owned(),
                    ]
                })
            }) {
                Ok((added, found)) => {
                    errors.truncate(100);
                    Some(json_response(
                        StatusCode::OK,
                        &json!({"added": added, "found": found, "errors": errors}),
                        ctx.instance,
                    ))
                }
                Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/import/exclusions" => {
            let source = str_field(payload, "source").to_owned();
            let external_id = str_field(payload, "external_id").to_owned();
            if source.is_empty() || external_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("source and external_id are required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let heroic_source = str_field(payload, "heroic_source").to_owned();
            let mut entry_res: Result<Value, ApiError> = Ok(Value::Null);
            let tx = ctx.app.transact_state(|state| {
                entry_res = add_exclusion(state, &source, &external_id, &heroic_source);
                if let Err(ref err) = entry_res {
                    return Err(openbox_store::StateError::Corrupt(err.message.clone()));
                }
                Ok(())
            });
            match (tx, entry_res) {
                (_, Err(err)) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
                (Err(err), _) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
                (Ok(_), Ok(entry)) => Some(json_response(
                    StatusCode::OK,
                    &json!({"exclusion": entry}),
                    ctx.instance,
                )),
            }
        }
        "/api/v2/import/exclusions/delete" => {
            let source = str_field(payload, "source").to_owned();
            let external_id = str_field(payload, "external_id").to_owned();
            if source.is_empty() || external_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("source and external_id are required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let mut count = 0usize;
            match ctx.app.transact_state(|state| {
                count = remove_exclusion(state, &source, &external_id);
                Ok(())
            }) {
                Ok(_) => Some(json_response(
                    StatusCode::OK,
                    &json!({"removed": count > 0, "count": count}),
                    ctx.instance,
                )),
                Err(err) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/import/launchbox/preview" => {
            let xml_path = str_field(payload, "xml_path");
            if xml_path.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("xml_path is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if !Path::new(xml_path).is_file() {
                return Some(api_error_response(
                    &ApiError::bad_request(format!("LaunchBox XML not found: {xml_path}")),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(err) => {
                    return Some(api_error_response(
                        &ApiError::state_unavailable(err.to_string()),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let options = payload.get("options").cloned().unwrap_or_else(|| json!({}));
            match launchbox_import::preview_import(Path::new(xml_path), &state, &options) {
                Ok(report) => Some(json_response(StatusCode::OK, &report, ctx.instance)),
                Err(err) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/import/launchbox/apply" => {
            let xml_path = str_field(payload, "xml_path");
            if xml_path.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("xml_path is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if !Path::new(xml_path).is_file() {
                return Some(api_error_response(
                    &ApiError::bad_request(format!("LaunchBox XML not found: {xml_path}")),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let parsed = match launchbox_import::parse_launchbox_xml(Path::new(xml_path)) {
                Ok(p) => p,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let supplied_plan = payload.get("plan").cloned();
            let options = payload
                .get("options")
                .cloned()
                .or_else(|| {
                    supplied_plan
                        .as_ref()
                        .and_then(|p| p.get("options").cloned())
                })
                .unwrap_or_else(|| json!({}));
            if let Some(ref sp) = supplied_plan
                && parsed.get("source_digest") != sp.get("source_digest")
            {
                return Some(api_error_response(
                    &ApiError::bad_request(
                        "LaunchBox XML changed since the preview; review it again.",
                    ),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let req_token = payload
                .get("preview_token")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            let mut apply_res: Result<Value, ApiError> = Ok(Value::Null);
            let tx = ctx.app.transact_state(|state| {
                let plan_to_use = match &supplied_plan {
                    Some(p) => p.clone(),
                    None => launchbox_import::build_import_plan(&parsed, state, &options),
                };
                let token = req_token
                    .as_deref()
                    .or_else(|| plan_to_use.get("preview_token").and_then(Value::as_str));
                apply_res = launchbox_import::apply_import_plan(
                    &plan_to_use,
                    state,
                    token,
                    parsed.get("source_digest").and_then(Value::as_str),
                    Some(&parsed),
                    &options,
                );
                if let Err(ref err) = apply_res {
                    return Err(openbox_store::StateError::Corrupt(err.message.clone()));
                }
                Ok(())
            });
            match (tx, apply_res) {
                (_, Err(err)) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
                (Err(err), _) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
                (Ok(_), Ok(counts)) => {
                    ctx.app.media_probe.clear_caches();
                    ctx.app.sse.broadcast_event(
                        "library.imported",
                        &json!({"source": "launchbox", "added": counts.get("added").and_then(Value::as_u64).unwrap_or(0)}),
                    );
                    Some(json_response(StatusCode::OK, &counts, ctx.instance))
                }
            }
        }
        "/api/v2/import/esde/preview" => {
            let xml_path = {
                let x = str_field(payload, "xml_path");
                if !x.is_empty() {
                    x
                } else {
                    str_field(payload, "gamelist")
                }
            };
            if xml_path.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("xml_path is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if !Path::new(xml_path).is_file() {
                return Some(api_error_response(
                    &ApiError::bad_request(format!("ES-DE gamelist not found: {xml_path}")),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let parsed = match esde_import::parse_gamelist(Path::new(xml_path)) {
                Ok(p) => p,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(err) => {
                    return Some(api_error_response(
                        &ApiError::state_unavailable(err.to_string()),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let options = payload.get("options").cloned().unwrap_or_else(|| json!({}));
            let plan = esde_import::build_import_plan(&parsed, &state, &options);
            Some(json_response(StatusCode::OK, &plan, ctx.instance))
        }
        "/api/v2/import/esde/apply" => {
            let xml_path = {
                let x = str_field(payload, "xml_path");
                if !x.is_empty() {
                    x
                } else {
                    str_field(payload, "gamelist")
                }
            };
            if xml_path.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("xml_path is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if !Path::new(xml_path).is_file() {
                return Some(api_error_response(
                    &ApiError::bad_request(format!("ES-DE gamelist not found: {xml_path}")),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let parsed = match esde_import::parse_gamelist(Path::new(xml_path)) {
                Ok(p) => p,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let submitted = payload.get("plan").cloned();
            let options = payload
                .get("options")
                .cloned()
                .or_else(|| submitted.as_ref().and_then(|p| p.get("options").cloned()))
                .unwrap_or_else(|| json!({}));
            if let Some(ref sp) = submitted
                && sp.get("source_digest") != parsed.get("source_digest")
            {
                return Some(api_error_response(
                    &ApiError::bad_request(
                        "ES-DE gamelist changed since the preview; review it again.",
                    )
                    .with_code("ESDE_STALE_PLAN"),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let req_token = payload
                .get("preview_token")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            let mut apply_res: Result<Value, ApiError> = Ok(Value::Null);
            let tx = ctx.app.transact_state(|state| {
                let plan_to_use = match &submitted {
                    Some(p) => p.clone(),
                    None => esde_import::build_import_plan(&parsed, state, &options),
                };
                let token = req_token
                    .as_deref()
                    .or_else(|| plan_to_use.get("preview_token").and_then(Value::as_str));
                apply_res = esde_import::apply_import_plan(
                    &plan_to_use,
                    state,
                    token,
                    parsed.get("source_digest").and_then(Value::as_str),
                    Some(&parsed),
                    &options,
                );
                if let Err(ref err) = apply_res {
                    return Err(openbox_store::StateError::Corrupt(err.message.clone()));
                }
                Ok(())
            });
            match (tx, apply_res) {
                (_, Err(err)) => Some(api_error_response(&err, ctx.request_id, ctx.instance)),
                (Err(err), _) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
                (Ok(_), Ok(counts)) => {
                    ctx.app.media_probe.clear_caches();
                    ctx.app.sse.broadcast_event(
                        "library.imported",
                        &json!({"source": "esde", "added": counts.get("added").and_then(Value::as_u64).unwrap_or(0)}),
                    );
                    Some(json_response(StatusCode::OK, &counts, ctx.instance))
                }
            }
        }
        _ => None,
    }
}
