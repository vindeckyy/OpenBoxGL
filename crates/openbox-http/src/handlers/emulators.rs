//! Emulator status, registry, settings, install/update/open/scan, netplay lobby, and highscores.
//! Mirrors `handlers/emulators.py`.

use std::collections::HashSet;
use std::path::Path;
use std::sync::Arc;

use axum::http::StatusCode;
use axum::response::Response;
use openbox_core::ApiError;
use openbox_emulators::{
    build_emulators_map, emulators as emu_ops, highscores, list_scan_configs, load_adapters,
    load_definitions, load_registry, netplay, resolve_game_from_payload, save_scan_config,
    scan_folder, settings as emu_settings,
};
use serde_json::{Map, Value, json};

use crate::handlers::shared::{RequestCtx, bool_field, str_field};
use crate::middleware::{api_error_response, json_response, state_error_response};

pub async fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    match ctx.path {
        "/api/v2/emulators" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let adapters = load_adapters(&ctx.app.root_dir, Some(&ctx.app.data_dir));
            let emu_map = build_emulators_map(&adapters);
            let skip_checks: HashSet<String> = state
                .get("settings")
                .and_then(|s| s.get("emulator_update_checks"))
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str().map(|s| s.trim().to_owned()))
                        .collect()
                })
                .unwrap_or_default();
            let mut list = emu_ops::emulator_status(&adapters);
            for item in &mut list {
                let app_id = item["app_id"].as_str().unwrap_or("").to_owned();
                let skipped = skip_checks.contains(&app_id);
                item["update_checked"] = Value::Bool(!skipped);
                item["skip_update_check"] = Value::Bool(skipped);
                item["version"] = Value::String(String::new());
                item["latest_version"] = Value::String(String::new());
                item["update_available"] = Value::Bool(false);
                if item["installed"].as_bool().unwrap_or(false) && !skipped {
                    let native = emu_map
                        .get(&app_id)
                        .and_then(|e| e.get("native"))
                        .and_then(Value::as_str)
                        .unwrap_or("");
                    let probe = emu_settings::version_probe(&app_id, native);
                    item["version"] = probe["version"].clone();
                    item["latest_version"] = probe["latest_version"].clone();
                    item["update_available"] = probe["update_available"].clone();
                }
                let job_upd = ctx.app.jobs.snapshot(&format!("update:{app_id}"));
                let job_inst = ctx.app.jobs.snapshot(&format!("emulator-install:{app_id}"));
                item["job"] = if job_upd.as_object().is_some_and(|o| !o.is_empty()) {
                    job_upd
                } else {
                    job_inst
                };
            }
            Some(json_response(
                StatusCode::OK,
                &json!({
                    "emulators": list,
                    "install_all": ctx.app.jobs.snapshot("emulator-install-all"),
                    "update_all": ctx.app.jobs.snapshot("emulator-update-all"),
                }),
                ctx.instance,
            ))
        }
        "/api/v2/emulators/recommend" => {
            let platform = ctx.query_first("platform").unwrap_or_default();
            let adapters = load_adapters(&ctx.app.root_dir, Some(&ctx.app.data_dir));
            let recs = emu_ops::recommendations_for_platform(&adapters, &platform);
            Some(json_response(
                StatusCode::OK,
                &json!({"recommendations": recs}),
                ctx.instance,
            ))
        }
        "/api/v2/emulators/dependencies" => {
            let name = ctx.query_first("name").unwrap_or_default();
            Some(json_response(
                StatusCode::OK,
                &json!({"name": name, "required": [], "found": [], "missing": []}),
                ctx.instance,
            ))
        }
        "/api/v2/emulators/definitions" => {
            let defs = load_definitions(&ctx.app.root_dir, Some(&ctx.app.data_dir));
            Some(json_response(
                StatusCode::OK,
                &json!({"definitions": defs}),
                ctx.instance,
            ))
        }
        "/api/v2/emulators/scan-configs" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            Some(json_response(
                StatusCode::OK,
                &json!({"configs": list_scan_configs(&state)}),
                ctx.instance,
            ))
        }
        "/api/v2/emulators/registry" => {
            let health = ctx.query_first("health").is_some_and(|v| {
                matches!(v.to_ascii_lowercase().as_str(), "1" | "true" | "yes" | "on")
            });
            let reg = load_registry(&ctx.app.root_dir, Some(&ctx.app.data_dir), health);
            Some(json_response(StatusCode::OK, &reg, ctx.instance))
        }
        "/api/v2/emulators/settings" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let adapters = load_adapters(&ctx.app.root_dir, Some(&ctx.app.data_dir));
            let emu_map = build_emulators_map(&adapters);
            let schemas = emu_settings::settings_schemas();
            let skip_checks: HashSet<String> = state
                .get("settings")
                .and_then(|s| s.get("emulator_update_checks"))
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str().map(str::to_owned))
                        .collect()
                })
                .unwrap_or_default();
            let mut entries = Vec::new();
            for (emulator_id, schema) in schemas.as_object().unwrap() {
                let app_id = emu_settings::app_id_for_canonical(emulator_id);
                let native = emu_map
                    .get(app_id)
                    .and_then(|e| e.get("native"))
                    .and_then(Value::as_str)
                    .unwrap_or("");
                let stored = state
                    .get("emulator_settings")
                    .and_then(|e| e.get(emulator_id))
                    .and_then(Value::as_object);
                let platforms = stored
                    .and_then(|o| o.get("platforms"))
                    .cloned()
                    .filter(Value::is_object)
                    .unwrap_or_else(|| json!({}));
                entries.push(json!({
                    "emulator_id": emulator_id,
                    "app_id": app_id,
                    "label": schema["label"],
                    "managed": emu_settings::is_managed(&state, emulator_id),
                    "schema": schema,
                    "global": emu_settings::effective_values(&state, emulator_id, ""),
                    "platforms": platforms,
                    "skip_update_check": skip_checks.contains(app_id),
                    "version": emu_settings::version_probe(app_id, native),
                }));
            }
            Some(json_response(
                StatusCode::OK,
                &json!({"emulators": entries, "plugins": []}),
                ctx.instance,
            ))
        }
        "/api/v2/netplay/lobby" => {
            let res = netplay::lobby().await;
            Some(json_response(StatusCode::OK, &res, ctx.instance))
        }
        "/api/v2/highscores" => Some(json_response(
            StatusCode::OK,
            &highscores::discover(None),
            ctx.instance,
        )),
        "/api/v2/highscores/table" => {
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let mut req_map = Map::new();
            if let Some(gid) = ctx.query_first("game_id") {
                req_map.insert("game_id".into(), Value::String(gid));
            }
            if let Some(id) = ctx.query_first("id") {
                req_map.insert("id".into(), Value::String(id));
            }
            let game = resolve_game_from_payload(&state, &req_map)
                .map(|(_, g)| g)
                .unwrap_or_else(|_| json!({}));
            Some(json_response(
                StatusCode::OK,
                &highscores::table_for_game(&game, None),
                ctx.instance,
            ))
        }
        _ => None,
    }
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/emulators/settings" => {
            let emu_raw = str_field(payload, "emulator_id");
            let emulator_id = match emu_settings::canonical_emulator_id(emu_raw) {
                Ok(c) => c,
                Err(err) => {
                    return Some(api_error_response(
                        &ApiError::bad_request(err),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let platform = str_field(payload, "platform");
            if platform.len() > 80 {
                return Some(api_error_response(
                    &ApiError::bad_request("Platform must be 80 characters or fewer."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let clean = match emu_settings::normalize_values(emulator_id, payload.get("values")) {
                Ok(c) => c,
                Err(err) => {
                    return Some(api_error_response(
                        &ApiError::bad_request(err),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let managed = payload.get("managed").and_then(Value::as_bool);
            let reset = bool_field(payload, "reset");
            let skip_update = payload.get("skip_update_check").and_then(Value::as_bool);
            let app_id = emu_settings::app_id_for_canonical(emulator_id).to_owned();
            let plat_owned = platform.to_owned();

            match ctx.app.transact_state(move |st| {
                if let Some(skip) = skip_update
                    && !app_id.is_empty()
                    && let Some(settings) = st.get_mut("settings").and_then(Value::as_object_mut)
                {
                    let arr = settings
                        .entry("emulator_update_checks")
                        .or_insert_with(|| Value::Array(Vec::new()));
                    if let Some(list) = arr.as_array_mut() {
                        if skip {
                            if !list.iter().any(|v| v.as_str() == Some(app_id.as_str())) {
                                list.push(Value::String(app_id.clone()));
                            }
                        } else {
                            list.retain(|v| v.as_str() != Some(app_id.as_str()));
                        }
                    }
                }
                let root = st.as_object_mut().unwrap();
                let emu_settings_map = root
                    .entry("emulator_settings")
                    .or_insert_with(|| Value::Object(Map::new()))
                    .as_object_mut()
                    .unwrap();
                let entry = emu_settings_map
                    .entry(emulator_id)
                    .or_insert_with(|| Value::Object(Map::new()))
                    .as_object_mut()
                    .unwrap();
                if let Some(m) = managed {
                    entry.insert("managed".into(), Value::Bool(m));
                }
                if reset {
                    if !plat_owned.is_empty() {
                        if let Some(plats) =
                            entry.get_mut("platforms").and_then(Value::as_object_mut)
                        {
                            plats.remove(&plat_owned);
                        }
                    } else {
                        entry.remove("platforms");
                    }
                } else if !plat_owned.is_empty() {
                    let plats = entry
                        .entry("platforms")
                        .or_insert_with(|| Value::Object(Map::new()))
                        .as_object_mut()
                        .unwrap();
                    plats.insert(plat_owned.clone(), Value::Object(clean));
                } else {
                    entry.insert("global".into(), Value::Object(clean));
                }
                let platforms_out = entry.get("platforms").cloned().unwrap_or_else(|| json!({}));
                Ok(json!({
                    "emulator_id": emulator_id,
                    "managed": emu_settings::is_managed(st, emulator_id),
                    "global": emu_settings::effective_values(st, emulator_id, ""),
                    "platforms": platforms_out,
                }))
            }) {
                Ok((_, res)) => Some(json_response(StatusCode::OK, &res, ctx.instance)),
                Err(e) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/emulators/open" => {
            let app_id = str_field(payload, "app_id");
            let adapters = load_adapters(&ctx.app.root_dir, Some(&ctx.app.data_dir));
            match emu_ops::launch_emulator(&adapters, app_id) {
                Ok(res) => Some(json_response(StatusCode::OK, &res, ctx.instance)),
                Err(err) => Some(api_error_response(
                    &ApiError::bad_request(err),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/emulators/install" => {
            let app_id = str_field(payload, "app_id").to_owned();
            let app = Arc::clone(&ctx.app);
            match ctx.app.jobs.submit(
                &format!("emulator-install:{app_id}"),
                false,
                Some("emulator.install"),
                Some(&json!({"app_id": app_id})),
                move |_jctx| {
                    let adapters = load_adapters(&app.root_dir, Some(&app.data_dir));
                    let profiles = emu_ops::install_emulator(&adapters, &app_id)?;
                    let _ = app.transact_state(|st| {
                        if let Some(p) = st.get_mut("profiles").and_then(Value::as_object_mut) {
                            for (k, v) in profiles {
                                p.insert(k, v);
                            }
                        }
                        Ok(())
                    });
                    Ok(json!({"installed": app_id}))
                },
            ) {
                Ok(job) => Some(json_response(
                    StatusCode::ACCEPTED,
                    &json!({"state": "queued", "job_id": job["job_id"]}),
                    ctx.instance,
                )),
                Err(err) => Some(api_error_response(
                    &ApiError::bad_request(err),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/emulators/install-all" => {
            let app = Arc::clone(&ctx.app);
            match ctx.app.jobs.submit(
                "emulator-install-all",
                false,
                Some("emulator.install"),
                None,
                move |_jctx| {
                    let adapters = load_adapters(&app.root_dir, Some(&app.data_dir));
                    let status = emu_ops::emulator_status(&adapters);
                    let mut installed = Vec::new();
                    let mut errors = Vec::new();
                    for item in status {
                        if item["installed"].as_bool().unwrap_or(false) {
                            continue;
                        }
                        let aid = item["app_id"].as_str().unwrap_or("");
                        match emu_ops::install_emulator(&adapters, aid) {
                            Ok(_) => installed.push(item["name"].clone()),
                            Err(e) => errors.push(Value::String(format!(
                                "{}: {e}",
                                item["name"].as_str().unwrap_or(aid)
                            ))),
                        }
                    }
                    Ok(json!({"installed": installed, "errors": errors}))
                },
            ) {
                Ok(job) => Some(json_response(
                    StatusCode::ACCEPTED,
                    &json!({"state": "queued", "job_id": job["job_id"]}),
                    ctx.instance,
                )),
                Err(err) => Some(api_error_response(
                    &ApiError::bad_request(err),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/emulators/update" => {
            let app_id = str_field(payload, "app_id").to_owned();
            let app = Arc::clone(&ctx.app);
            match ctx.app.jobs.submit(
                &format!("update:{app_id}"),
                false,
                Some("emulator.update"),
                Some(&json!({"app_id": app_id})),
                move |_jctx| {
                    let adapters = load_adapters(&app.root_dir, Some(&app.data_dir));
                    emu_ops::update_emulator(&adapters, &app_id)
                },
            ) {
                Ok(job) => Some(json_response(
                    StatusCode::ACCEPTED,
                    &json!({"state": "queued", "job_id": job["job_id"]}),
                    ctx.instance,
                )),
                Err(err) => Some(api_error_response(
                    &ApiError::bad_request(err),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/emulators/update-all" => {
            let app = Arc::clone(&ctx.app);
            match ctx.app.jobs.submit(
                "emulator-update-all",
                false,
                Some("emulator.update"),
                None,
                move |_jctx| {
                    let adapters = load_adapters(&app.root_dir, Some(&app.data_dir));
                    let status = emu_ops::emulator_status(&adapters);
                    let mut updated = Vec::new();
                    let mut errors = Vec::new();
                    for item in status {
                        if item["mode"].as_str() != Some("flatpak") {
                            continue;
                        }
                        let aid = item["app_id"].as_str().unwrap_or("");
                        match emu_ops::update_emulator(&adapters, aid) {
                            Ok(_) => updated.push(item["name"].clone()),
                            Err(e) => errors.push(Value::String(format!(
                                "{}: {e}",
                                item["name"].as_str().unwrap_or(aid)
                            ))),
                        }
                    }
                    Ok(json!({"updated": updated, "errors": errors}))
                },
            ) {
                Ok(job) => Some(json_response(
                    StatusCode::ACCEPTED,
                    &json!({"state": "queued", "job_id": job["job_id"]}),
                    ctx.instance,
                )),
                Err(err) => Some(api_error_response(
                    &ApiError::bad_request(err),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/emulators/scan-configs" => {
            let folder = str_field(payload, "folder").to_owned();
            let emulator_id = str_field(payload, "emulator_id").to_owned();
            let auto_update = bool_field(payload, "auto_update");
            match ctx.app.transact_state(move |st| {
                save_scan_config(st, &folder, &emulator_id, auto_update)
                    .map_err(openbox_store::StateError::Corrupt)
            }) {
                Ok((_, entry)) => Some(json_response(
                    StatusCode::OK,
                    &json!({"config": entry}),
                    ctx.instance,
                )),
                Err(e) => Some(api_error_response(
                    &ApiError::bad_request(e.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/emulators/scan" => {
            let folder = str_field(payload, "folder");
            let emulator_id = str_field(payload, "emulator_id");
            let recursive = payload
                .get("recursive")
                .and_then(Value::as_bool)
                .unwrap_or(true);
            let adapters = load_adapters(&ctx.app.root_dir, Some(&ctx.app.data_dir));
            let scanned = match scan_folder(
                Path::new(folder),
                &adapters,
                if emulator_id.is_empty() {
                    None
                } else {
                    Some(emulator_id)
                },
                recursive,
            ) {
                Ok(g) => g,
                Err(err) => {
                    return Some(api_error_response(
                        &ApiError::bad_request(err),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let found = scanned.len();
            let to_add = scanned.clone();
            match ctx.app.transact_state(move |st| {
                let games = st.get_mut("games").and_then(Value::as_array_mut).unwrap();
                let mut existing: HashSet<String> = games
                    .iter()
                    .filter_map(|g| g.get("path").and_then(Value::as_str).map(str::to_owned))
                    .collect();
                let mut added = 0usize;
                for item in to_add {
                    let p = item.get("path").and_then(Value::as_str).unwrap_or("");
                    if existing.insert(p.to_owned()) {
                        games.push(item);
                        added += 1;
                    }
                }
                Ok(added)
            }) {
                Ok((_, added)) => Some(json_response(
                    StatusCode::OK,
                    &json!({"found": found, "added": added}),
                    ctx.instance,
                )),
                Err(e) => Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            }
        }
        _ => None,
    }
}
