//! Plugins 2.0, plugin catalog, plugin stats, plugin storefronts, and CSS themes routes
//! (`handlers/extensions.py` + `handlers/imports.py` plugin storefront routes).

use std::fs::{self, File};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::UNIX_EPOCH;

use axum::http::StatusCode;
use axum::response::Response;
use openbox_core::ApiError;
use openbox_imports::merge_imported_games;
use openbox_integrations::themes::{
    THEME_ASSET_LIMIT, THEME_AUDIO_EXTENSIONS, ensure_stock_themes, import_theme_file,
    theme_folder, theme_sound_config, write_native_background,
};
use openbox_platform::{contained_path, expand_user, open_path};
use openbox_plugins::catalog::{download_plugin_package, fetch_plugin_catalog};
use openbox_plugins::host::{
    MAX_LIBRARY_ENTRIES, PLUGIN_API_VERSION_SUPPORTED, get_plugin_settings,
    get_plugin_settings_public, install_plugin, list_plugins, load_plugin_state, plugin_commands,
    plugin_credentials_summary, plugin_data_values, plugin_trust_status, read_manifest,
    remove_plugin, run_plugin_hook, sandbox_status, save_plugin_state, set_plugin_credentials,
    set_plugin_data, set_plugin_enabled, set_plugin_permissions, set_plugin_settings,
    set_plugin_trust,
};
use openbox_plugins::stats::{plugin_stats_summaries, refresh_plugin_stats};
use openbox_plugins::storefront::{plugin_storefront_catalog, plugin_storefront_import};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use crate::handlers::metadata::metadata_db_path;
use crate::handlers::shared::{RequestCtx, bool_field, str_field};
use crate::middleware::{api_error_response, bytes_response, json_response, state_error_response};
use crate::static_files::send_file;

static PLUGIN_EPOCH: AtomicU64 = AtomicU64::new(0);

fn bump_plugin_epoch() -> u64 {
    PLUGIN_EPOCH.fetch_add(1, Ordering::AcqRel) + 1
}

fn sha256_file(path: &Path) -> Result<String, std::io::Error> {
    let mut file = File::open(path)?;
    let mut digest = Sha256::new();
    let mut buf = [0u8; 65536];
    loop {
        let n = file.read(&mut buf)?;
        if n == 0 {
            break;
        }
        digest.update(&buf[..n]);
    }
    Ok(hex::encode(digest.finalize()))
}

fn remember_plugin_source(root: &Path, plugin_id: &str, source: &str) {
    let expanded = expand_user(Path::new(source));
    let resolved = fs::canonicalize(&expanded).unwrap_or(expanded);
    let mut record = Map::new();
    record.insert("path".into(), json!(resolved.to_string_lossy()));
    if resolved.is_file()
        && let Ok(hash) = sha256_file(&resolved)
    {
        record.insert("sha256".into(), json!(hash));
    }
    let mut state = load_plugin_state(root);
    let sources = state
        .entry("local_sources")
        .or_insert_with(|| Value::Object(Map::new()));
    if !sources.is_object() {
        *sources = Value::Object(Map::new());
    }
    sources
        .as_object_mut()
        .unwrap()
        .insert(plugin_id.to_owned(), Value::Object(record));
    let _ = save_plugin_state(root, &state);
}

fn forget_plugin_source(root: &Path, plugin_id: &str) {
    let mut state = load_plugin_state(root);
    if let Some(sources) = state
        .get_mut("local_sources")
        .and_then(Value::as_object_mut)
        && sources.remove(plugin_id).is_some()
    {
        let _ = save_plugin_state(root, &state);
    }
}

fn installed_local_package(root: &Path, plugin_id: &str) -> Result<PathBuf, ApiError> {
    let state = load_plugin_state(root);
    let record = state
        .get("local_sources")
        .and_then(Value::as_object)
        .and_then(|s| s.get(plugin_id))
        .and_then(Value::as_object)
        .ok_or_else(|| {
            ApiError::bad_request("This plugin has no catalog entry or recorded local package.")
        })?;
    let path_str = record.get("path").and_then(Value::as_str).unwrap_or("");
    let path = expand_user(Path::new(path_str));
    if path_str.is_empty() || !path.exists() {
        return Err(ApiError::bad_request(
            "The recorded local plugin package is no longer available.",
        ));
    }
    let expected = record.get("sha256").and_then(Value::as_str).unwrap_or("");
    if !expected.is_empty() && path.is_file() {
        let actual = sha256_file(&path).map_err(|e| ApiError::bad_request(e.to_string()))?;
        if actual != expected {
            return Err(ApiError::bad_request(
                "The recorded local plugin package changed; reinstall it manually.",
            ));
        }
    }
    Ok(path)
}

fn drop_plugin_games(app: &openbox_runtime::AppState, plugin_id: &str) {
    if plugin_id.is_empty() {
        return;
    }
    let pid = plugin_id.to_owned();
    let _ = app.transact_state(move |st| {
        if let Some(games) = st.get_mut("games").and_then(Value::as_array_mut) {
            games.retain(|g| g.get("plugin_source").and_then(Value::as_str).unwrap_or("") != pid);
        }
        Ok(())
    });
}

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    let plugins_dir = ctx.app.data_dir.join("plugins");
    match ctx.path {
        "/api/v2/theme.css" => {
            let name = ctx.query_first("name").unwrap_or_default();
            let stem = Path::new(&name)
                .file_stem()
                .and_then(|s| s.to_str())
                .unwrap_or("");
            let theme = ctx.app.data_dir.join("themes").join(format!("{stem}.css"));
            if name.is_empty() || stem != name || !theme.is_file() {
                return Some(bytes_response(
                    StatusCode::OK,
                    Vec::new(),
                    "text/css; charset=utf-8",
                    "public, max-age=0, must-revalidate",
                    false,
                    &[],
                ));
            }
            let Ok(bytes) = fs::read(&theme) else {
                return Some(bytes_response(
                    StatusCode::OK,
                    Vec::new(),
                    "text/css; charset=utf-8",
                    "public, max-age=0, must-revalidate",
                    false,
                    &[],
                ));
            };
            let mtime_ns = fs::metadata(&theme)
                .ok()
                .and_then(|m| m.modified().ok())
                .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
                .map(|d| d.as_nanos())
                .unwrap_or(0);
            let etag = format!("\"{mtime_ns:x}-{:x}\"", bytes.len());
            Some(bytes_response(
                StatusCode::OK,
                bytes,
                "text/css; charset=utf-8",
                "public, max-age=0, must-revalidate",
                false,
                &[("ETag", &etag)],
            ))
        }
        "/api/v2/theme-sounds" => {
            let name = ctx.query_first("name").unwrap_or_default();
            let payload = match theme_folder(&name, &ctx.app.data_dir, &ctx.app.root_dir) {
                Some(folder) => theme_sound_config(&folder, &name),
                None => json!({"music": "", "sounds": {}}),
            };
            Some(json_response(StatusCode::OK, &payload, ctx.instance))
        }
        "/api/v2/theme-asset" => {
            let name = ctx.query_first("name").unwrap_or_default();
            let relative = ctx.query_first("path").unwrap_or_default();
            let Some(folder) = theme_folder(&name, &ctx.app.data_dir, &ctx.app.root_dir) else {
                return Some(api_error_response(
                    &ApiError::not_found("Theme sound asset not found."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let ext = Path::new(&relative)
                .extension()
                .and_then(|e| e.to_str())
                .map(|e| format!(".{}", e.to_lowercase()))
                .unwrap_or_default();
            if !THEME_AUDIO_EXTENSIONS.contains(&ext.as_str()) {
                return Some(api_error_response(
                    &ApiError::not_found("Theme sound asset not found."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let Ok(asset) = contained_path(&folder, &[relative.as_str()]) else {
                return Some(api_error_response(
                    &ApiError::not_found("Theme sound asset not found."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let Ok(meta) = fs::metadata(&asset) else {
                return Some(api_error_response(
                    &ApiError::not_found("Theme sound asset not found."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            if !meta.is_file() || meta.len() > THEME_ASSET_LIMIT {
                return Some(api_error_response(
                    &ApiError::not_found("Theme sound asset not found."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let mime = mime_guess::from_path(&asset)
                .first_raw()
                .unwrap_or("application/octet-stream");
            Some(send_file(
                ctx.headers,
                &asset,
                Some(mime),
                Some("private, max-age=3600"),
                false,
                ctx.request_id,
                ctx.instance,
            ))
        }
        "/api/v2/themes" => {
            let themes_dir = ctx.app.data_dir.join("themes");
            ensure_stock_themes(&themes_dir, &ctx.app.root_dir);
            let mut themes: Vec<String> = fs::read_dir(&themes_dir)
                .ok()
                .into_iter()
                .flatten()
                .flatten()
                .map(|e| e.path())
                .filter(|p| {
                    p.is_file()
                        && p.extension()
                            .and_then(|e| e.to_str())
                            .is_some_and(|e| e.eq_ignore_ascii_case("css"))
                })
                .filter_map(|p| p.file_stem().and_then(|s| s.to_str()).map(str::to_owned))
                .collect();
            themes.sort();
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let settings = state
                .get("settings")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            let platform = ctx.query_first("platform").unwrap_or_default();
            let global_theme = settings
                .get("theme")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            let mappings = settings
                .get("theme_by_platform")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            let selected = if !platform.is_empty() {
                mappings
                    .get(&platform)
                    .and_then(Value::as_str)
                    .map(str::to_owned)
                    .unwrap_or_else(|| global_theme.clone())
            } else {
                global_theme.clone()
            };
            Some(json_response(
                StatusCode::OK,
                &json!({
                    "themes": themes,
                    "selected": selected,
                    "global": global_theme,
                    "mappings": mappings,
                }),
                ctx.instance,
            ))
        }
        "/api/v2/plugins" => Some(json_response(
            StatusCode::OK,
            &json!({
                "plugins": list_plugins(&plugins_dir),
                "api_version": PLUGIN_API_VERSION_SUPPORTED,
                "sandbox": sandbox_status(),
            }),
            ctx.instance,
        )),
        "/api/v2/plugins/commands" => Some(json_response(
            StatusCode::OK,
            &json!({
                "api_version": PLUGIN_API_VERSION_SUPPORTED,
                "sandbox": sandbox_status(),
                "commands": plugin_commands(&plugins_dir, false),
                "epoch": PLUGIN_EPOCH.load(Ordering::Acquire),
            }),
            ctx.instance,
        )),
        "/api/v2/plugins/catalog" => {
            let installed: std::collections::HashMap<String, Value> = list_plugins(&plugins_dir)
                .into_iter()
                .filter(|m| m.get("valid").and_then(Value::as_bool).unwrap_or(false))
                .filter_map(|m| {
                    let id = m.get("id")?.as_str()?.to_owned();
                    Some((id, m))
                })
                .collect();
            let mut entries = Vec::new();
            for entry in fetch_plugin_catalog(Some(&ctx.app.root_dir)) {
                let Some(mut obj) = entry.as_object().cloned() else {
                    continue;
                };
                let pid = obj
                    .get("id")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned();
                let cat_ver = obj
                    .get("version")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned();
                let cur = installed.get(&pid);
                let cur_ver = cur
                    .and_then(|c| c.get("version"))
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned();
                obj.insert("installed".into(), json!(cur.is_some()));
                obj.insert("installed_version".into(), json!(cur_ver));
                obj.insert(
                    "update_available".into(),
                    json!(cur.is_some() && cat_ver != cur_ver),
                );
                entries.push(Value::Object(obj));
            }
            Some(json_response(
                StatusCode::OK,
                &json!({"catalog": entries, "sandbox": sandbox_status()}),
                ctx.instance,
            ))
        }
        "/api/v2/plugins/trust" => {
            let plugin_id = ctx.query_first("id").unwrap_or_default().trim().to_owned();
            if let Err(err) = read_manifest(&plugins_dir.join(&plugin_id)) {
                return Some(api_error_response(
                    &err.into_api_error(),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let mut body = plugin_trust_status(&plugins_dir, &plugin_id);
            if let Some(obj) = body.as_object_mut() {
                obj.insert("id".into(), json!(plugin_id));
            }
            Some(json_response(StatusCode::OK, &body, ctx.instance))
        }
        "/api/v2/plugins/settings" => {
            let plugin_id = ctx.query_first("id").unwrap_or_default().trim().to_owned();
            let mut body = match get_plugin_settings_public(&plugins_dir, &plugin_id) {
                Ok(v) => v,
                Err(err) => {
                    return Some(api_error_response(
                        &err.into_api_error(),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            if let Some(obj) = body.as_object_mut() {
                obj.insert("id".into(), json!(plugin_id));
                obj.insert(
                    "credentials".into(),
                    plugin_credentials_summary(&plugins_dir, &plugin_id),
                );
            }
            Some(json_response(StatusCode::OK, &body, ctx.instance))
        }
        "/api/v2/plugins/credentials" => {
            let plugin_id = ctx.query_first("id").unwrap_or_default().trim().to_owned();
            Some(json_response(
                StatusCode::OK,
                &json!({
                    "id": plugin_id,
                    "credentials": plugin_credentials_summary(&plugins_dir, &plugin_id),
                }),
                ctx.instance,
            ))
        }
        "/api/v2/plugins/data" => {
            let plugin_id = ctx.query_first("id").unwrap_or_default().trim().to_owned();
            Some(json_response(
                StatusCode::OK,
                &json!({
                    "id": plugin_id,
                    "data": plugin_data_values(&plugins_dir, &plugin_id),
                }),
                ctx.instance,
            ))
        }
        "/api/v2/plugin-stats" => Some(json_response(
            StatusCode::OK,
            &json!({"sources": plugin_stats_summaries(&ctx.app.data_dir)}),
            ctx.instance,
        )),
        _ => None,
    }
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    let plugins_dir = ctx.app.data_dir.join("plugins");
    match ctx.path {
        "/api/v2/plugins/install" => {
            let source = str_field(payload, "path");
            let manifest = match install_plugin(Path::new(source), &plugins_dir) {
                Ok(m) => m,
                Err(err) => {
                    return Some(api_error_response(
                        &err.into_api_error(),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let id = manifest.get("id").and_then(Value::as_str).unwrap_or("");
            remember_plugin_source(&plugins_dir, id, source);
            bump_plugin_epoch();
            Some(json_response(
                StatusCode::OK,
                &json!({"plugin": Value::Object(manifest)}),
                ctx.instance,
            ))
        }
        "/api/v2/plugins/catalog/install" => {
            let plugin_id = str_field(payload, "id").trim();
            let catalog = fetch_plugin_catalog(Some(&ctx.app.root_dir));
            let Some(entry) = catalog
                .iter()
                .find(|e| e.get("id").and_then(Value::as_str) == Some(plugin_id))
            else {
                return Some(api_error_response(
                    &ApiError::bad_request("Unknown catalog plugin."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            if entry
                .get("local_only")
                .and_then(Value::as_bool)
                .unwrap_or(false)
            {
                return Some(api_error_response(
                    &ApiError::bad_request(
                        "This catalog entry is documentation-only. Install local plugin packages manually.",
                    ),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let Ok(tmp) = tempfile::tempdir_in(&ctx.app.data_dir) else {
                return Some(api_error_response(
                    &ApiError::bad_request("Could not create temporary directory."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let archive = match download_plugin_package(entry, tmp.path()) {
                Ok(p) => p,
                Err(err) => {
                    return Some(api_error_response(
                        &ApiError::bad_request(err),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let manifest = match install_plugin(&archive, &plugins_dir) {
                Ok(m) => m,
                Err(err) => {
                    return Some(api_error_response(
                        &err.into_api_error(),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let id = manifest.get("id").and_then(Value::as_str).unwrap_or("");
            forget_plugin_source(&plugins_dir, id);
            bump_plugin_epoch();
            Some(json_response(
                StatusCode::OK,
                &json!({"plugin": Value::Object(manifest)}),
                ctx.instance,
            ))
        }
        "/api/v2/plugins/toggle" => {
            let plugin_id = str_field(payload, "id");
            let enabled_req = bool_field(payload, "enabled");
            let enabled = match set_plugin_enabled(&plugins_dir, plugin_id, enabled_req) {
                Ok(e) => e,
                Err(err) => {
                    return Some(api_error_response(
                        &err.into_api_error(),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            if !enabled {
                drop_plugin_games(&ctx.app, plugin_id);
            }
            bump_plugin_epoch();
            Some(json_response(
                StatusCode::OK,
                &json!({"enabled": enabled}),
                ctx.instance,
            ))
        }
        "/api/v2/plugins/remove" => {
            let plugin_id = str_field(payload, "id");
            let removed = match remove_plugin(&plugins_dir, plugin_id) {
                Ok(id) => id,
                Err(err) => {
                    return Some(api_error_response(
                        &err.into_api_error(),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            forget_plugin_source(&plugins_dir, &removed);
            drop_plugin_games(&ctx.app, &removed);
            bump_plugin_epoch();
            Some(json_response(
                StatusCode::OK,
                &json!({"removed": removed}),
                ctx.instance,
            ))
        }
        "/api/v2/plugins/repair" => {
            let plugin_id = str_field(payload, "id").trim();
            if plugin_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("Plugin id is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            if let Err(err) = read_manifest(&plugins_dir.join(plugin_id)) {
                return Some(api_error_response(
                    &err.into_api_error(),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let catalog = fetch_plugin_catalog(Some(&ctx.app.root_dir));
            let entry = catalog
                .iter()
                .find(|e| e.get("id").and_then(Value::as_str) == Some(plugin_id));
            let manifest = if let Some(ent) = entry
                && !ent
                    .get("local_only")
                    .and_then(Value::as_bool)
                    .unwrap_or(false)
            {
                let Ok(tmp) = tempfile::tempdir_in(&ctx.app.data_dir) else {
                    return Some(api_error_response(
                        &ApiError::bad_request("Could not create temporary directory."),
                        ctx.request_id,
                        ctx.instance,
                    ));
                };
                let archive = match download_plugin_package(ent, tmp.path()) {
                    Ok(p) => p,
                    Err(e) => {
                        return Some(api_error_response(
                            &ApiError::bad_request(e),
                            ctx.request_id,
                            ctx.instance,
                        ));
                    }
                };
                match install_plugin(&archive, &plugins_dir) {
                    Ok(m) => {
                        let id = m.get("id").and_then(Value::as_str).unwrap_or("");
                        forget_plugin_source(&plugins_dir, id);
                        m
                    }
                    Err(err) => {
                        return Some(api_error_response(
                            &err.into_api_error(),
                            ctx.request_id,
                            ctx.instance,
                        ));
                    }
                }
            } else {
                let local_pkg = match installed_local_package(&plugins_dir, plugin_id) {
                    Ok(p) => p,
                    Err(err) => {
                        return Some(api_error_response(&err, ctx.request_id, ctx.instance));
                    }
                };
                match install_plugin(&local_pkg, &plugins_dir) {
                    Ok(m) => m,
                    Err(err) => {
                        return Some(api_error_response(
                            &err.into_api_error(),
                            ctx.request_id,
                            ctx.instance,
                        ));
                    }
                }
            };
            bump_plugin_epoch();
            Some(json_response(
                StatusCode::OK,
                &json!({"plugin": Value::Object(manifest), "repaired": true}),
                ctx.instance,
            ))
        }
        "/api/v2/plugins/trust" => {
            let plugin_id = str_field(payload, "id").trim();
            if !payload.contains_key("trusted") {
                return Some(api_error_response(
                    &ApiError::bad_request("trusted is required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let trusted =
                match set_plugin_trust(&plugins_dir, plugin_id, bool_field(payload, "trusted")) {
                    Ok(t) => t,
                    Err(err) => {
                        return Some(api_error_response(
                            &err.into_api_error(),
                            ctx.request_id,
                            ctx.instance,
                        ));
                    }
                };
            bump_plugin_epoch();
            Some(json_response(
                StatusCode::OK,
                &json!({"id": plugin_id, "trusted": trusted}),
                ctx.instance,
            ))
        }
        "/api/v2/plugins/permissions" => {
            let plugin_id = str_field(payload, "id").trim();
            let perms: Vec<String> = payload
                .get("permissions")
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .map(|v| match v {
                            Value::String(s) => s.clone(),
                            other => other.to_string(),
                        })
                        .collect()
                })
                .unwrap_or_default();
            let granted = match set_plugin_permissions(&plugins_dir, plugin_id, &perms) {
                Ok(g) => g,
                Err(err) => {
                    return Some(api_error_response(
                        &err.into_api_error(),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            bump_plugin_epoch();
            Some(json_response(
                StatusCode::OK,
                &json!({"id": plugin_id, "permissions": granted}),
                ctx.instance,
            ))
        }
        "/api/v2/plugins/settings" => {
            let plugin_id = str_field(payload, "id").trim();
            let mut values = match payload.get("values") {
                None | Some(Value::Null) => Map::new(),
                Some(Value::Object(m)) => m.clone(),
                Some(_) => {
                    return Some(api_error_response(
                        &ApiError::bad_request("values must be an object."),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let clear: Vec<String> = match payload.get("clear_secrets") {
                None | Some(Value::Null) => Vec::new(),
                Some(Value::Array(arr)) => arr
                    .iter()
                    .filter_map(Value::as_str)
                    .map(str::to_owned)
                    .collect(),
                Some(_) => {
                    return Some(api_error_response(
                        &ApiError::bad_request("clear_secrets must be a list."),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let public = match get_plugin_settings_public(&plugins_dir, plugin_id) {
                Ok(v) => v,
                Err(err) => {
                    return Some(api_error_response(
                        &err.into_api_error(),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let current = match get_plugin_settings(&plugins_dir, plugin_id) {
                Ok(v) => v
                    .get("values")
                    .and_then(Value::as_object)
                    .cloned()
                    .unwrap_or_default(),
                Err(err) => {
                    return Some(api_error_response(
                        &err.into_api_error(),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            if let Some(props) = public
                .get("schema")
                .and_then(|s| s.get("properties"))
                .and_then(Value::as_object)
            {
                for (name, field) in props {
                    if field.get("format").and_then(Value::as_str) != Some("password") {
                        continue;
                    }
                    if clear.iter().any(|c| c == name) {
                        values.remove(name);
                    } else {
                        let is_blank = match values.get(name) {
                            None | Some(Value::Null) => true,
                            Some(Value::String(s)) => s.is_empty() || s == "••••",
                            _ => false,
                        };
                        if is_blank && let Some(cur_val) = current.get(name) {
                            values.insert(name.clone(), cur_val.clone());
                        }
                    }
                }
            }
            if let Err(err) = set_plugin_settings(&plugins_dir, plugin_id, &Value::Object(values)) {
                return Some(api_error_response(
                    &err.into_api_error(),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            bump_plugin_epoch();
            let mut body = match get_plugin_settings_public(&plugins_dir, plugin_id) {
                Ok(v) => v,
                Err(err) => {
                    return Some(api_error_response(
                        &err.into_api_error(),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            if let Some(obj) = body.as_object_mut() {
                obj.insert("id".into(), json!(plugin_id));
                obj.insert(
                    "credentials".into(),
                    plugin_credentials_summary(&plugins_dir, plugin_id),
                );
            }
            Some(json_response(StatusCode::OK, &body, ctx.instance))
        }
        "/api/v2/plugins/credentials" => {
            let plugin_id = str_field(payload, "id").trim();
            let values = payload.get("values").cloned().unwrap_or_else(|| json!({}));
            let clear: Vec<String> = payload
                .get("clear")
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .filter_map(Value::as_str)
                        .map(str::to_owned)
                        .collect()
                })
                .unwrap_or_default();
            let keys = match set_plugin_credentials(&plugins_dir, plugin_id, &values, &clear) {
                Ok(k) => k,
                Err(err) => {
                    return Some(api_error_response(
                        &err.into_api_error(),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let set_map: Map<String, Value> = keys
                .iter()
                .map(|k| (k.clone(), Value::Bool(true)))
                .collect();
            Some(json_response(
                StatusCode::OK,
                &json!({
                    "id": plugin_id,
                    "credentials": {"keys": keys, "set": set_map},
                }),
                ctx.instance,
            ))
        }
        "/api/v2/plugins/data" => {
            let plugin_id = str_field(payload, "id").trim();
            let values = payload.get("values").cloned().unwrap_or_else(|| json!({}));
            let merged = match set_plugin_data(&plugins_dir, plugin_id, &values) {
                Ok(m) => m,
                Err(err) => {
                    return Some(api_error_response(
                        &err.into_api_error(),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            bump_plugin_epoch();
            Some(json_response(
                StatusCode::OK,
                &json!({"id": plugin_id, "data": merged}),
                ctx.instance,
            ))
        }
        "/api/v2/plugins/command" => {
            let plugin_id = str_field(payload, "plugin_id").trim();
            let command = str_field(payload, "command").trim();
            if plugin_id.is_empty() || command.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("plugin_id and command are required."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let manifest = match read_manifest(&plugins_dir.join(plugin_id)) {
                Ok(m) => m,
                Err(err) => {
                    return Some(api_error_response(
                        &err.into_api_error(),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let declared = manifest
                .get("commands")
                .and_then(Value::as_array)
                .is_some_and(|a| {
                    a.iter()
                        .any(|c| c.get("id").and_then(Value::as_str) == Some(command))
                });
            if !declared {
                return Some(api_error_response(
                    &ApiError::bad_request("This plugin does not declare that command."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let enabled = list_plugins(&plugins_dir)
                .into_iter()
                .find(|p| p.get("id").and_then(Value::as_str) == Some(plugin_id))
                .and_then(|p| p.get("enabled").and_then(Value::as_bool))
                .unwrap_or(false);
            if std::env::var("OPENBOX_SAFE_MODE").is_ok_and(|v| !v.is_empty()) || !enabled {
                return Some(api_error_response(
                    &ApiError::bad_request("Plugin is disabled."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let library: Vec<Value> = state
                .get("games")
                .and_then(Value::as_array)
                .map(|games| {
                    games
                        .iter()
                        .take(MAX_LIBRARY_ENTRIES)
                        .map(|g| {
                            json!({
                                "game_id": g.get("game_id").and_then(Value::as_str).unwrap_or(""),
                                "name": g.get("name").and_then(Value::as_str).unwrap_or(""),
                                "platform": g.get("platform").and_then(Value::as_str).unwrap_or(""),
                                "progress": g.get("progress").and_then(Value::as_str).unwrap_or(""),
                                "favorite": g.get("favorite").and_then(Value::as_bool).unwrap_or(false),
                                "playtime_seconds": g.get("playtime_seconds").and_then(Value::as_i64).unwrap_or(0),
                            })
                        })
                        .collect()
                })
                .unwrap_or_default();
            let db_path = metadata_db_path(&ctx.app);
            let (res_opt, err) = run_plugin_hook(
                &plugins_dir,
                plugin_id,
                "command",
                &json!({"command": command, "library": library}),
                None,
                Some(&state),
                Some(&db_path),
                Some(&ctx.app.root_dir),
            );
            if res_opt.is_none() && !err.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request(err),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let result = res_opt.map(Value::Object).unwrap_or(Value::Null);
            let notification = result.get("notification").cloned().unwrap_or(Value::Null);
            Some(json_response(
                StatusCode::OK,
                &json!({
                    "ok": true,
                    "plugin_id": plugin_id,
                    "command": command,
                    "result": result,
                    "notification": notification,
                }),
                ctx.instance,
            ))
        }
        "/api/v2/plugin-stats/refresh" => {
            let plugin_ids: Vec<String> = if let Some(arr_val) = payload.get("plugin_ids") {
                let Some(arr) = arr_val.as_array() else {
                    return Some(api_error_response(
                        &ApiError::bad_request("plugin_ids must be a list of at most 64 ids."),
                        ctx.request_id,
                        ctx.instance,
                    ));
                };
                if arr.len() > 64 {
                    return Some(api_error_response(
                        &ApiError::bad_request("plugin_ids must be a list of at most 64 ids."),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
                arr.iter()
                    .filter_map(Value::as_str)
                    .map(|s| s.trim().to_owned())
                    .filter(|s| !s.is_empty())
                    .collect()
            } else {
                let single = str_field(payload, "plugin_id").trim().to_owned();
                if single.is_empty() {
                    Vec::new()
                } else {
                    vec![single]
                }
            };
            let state = match ctx.app.load_state_view() {
                Ok(s) => s,
                Err(e) => return Some(state_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let db_path = metadata_db_path(&ctx.app);
            let mut results = Vec::new();
            for pid in plugin_ids {
                match refresh_plugin_stats(
                    &pid,
                    &state,
                    &ctx.app.data_dir,
                    Some(&plugins_dir),
                    Some(15),
                    Some(&db_path),
                    Some(&ctx.app.root_dir),
                ) {
                    Ok(rows) => results.push(Value::Array(rows)),
                    Err(e) => results.push(json!({"plugin_id": pid, "error": e})),
                }
            }
            if !results.is_empty() {
                ctx.app.invalidate_caches();
            }
            Some(json_response(
                StatusCode::OK,
                &json!({"results": results}),
                ctx.instance,
            ))
        }
        "/api/v2/storefronts/plugin/catalog" => {
            let plugin_id = str_field(payload, "plugin_id").trim();
            let installed_only = bool_field(payload, "installed_only");
            let uninstalled_only = bool_field(payload, "uninstalled_only");
            let state = ctx.app.load_state_view().ok();
            let db_path = metadata_db_path(&ctx.app);
            match plugin_storefront_catalog(
                &plugins_dir,
                plugin_id,
                None,
                "",
                installed_only,
                uninstalled_only,
                30,
                state.as_deref(),
                Some(&db_path),
                Some(&ctx.app.root_dir),
            ) {
                Ok(entries) => Some(json_response(
                    StatusCode::OK,
                    &json!({"plugin_id": plugin_id, "entries": entries}),
                    ctx.instance,
                )),
                Err(err) => Some(api_error_response(
                    &ApiError::bad_request(err),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/storefronts/plugin/import" => {
            let plugin_id = str_field(payload, "plugin_id").trim().to_owned();
            let installed_only = bool_field(payload, "installed_only");
            let uninstalled_only = bool_field(payload, "uninstalled_only");
            let state = ctx.app.load_state_view().ok();
            let db_path = metadata_db_path(&ctx.app);
            let imported = match plugin_storefront_import(
                &plugins_dir,
                &plugin_id,
                None,
                installed_only,
                uninstalled_only,
                30,
                state.as_deref(),
                Some(&db_path),
                Some(&ctx.app.root_dir),
            ) {
                Ok(games) => games,
                Err(err) => {
                    return Some(api_error_response(
                        &ApiError::bad_request(err),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let imported_count = imported.len();
            let pid_clone = plugin_id.clone();
            match merge_imported_games(&ctx.app, imported, |g| {
                vec![format!(
                    "plugin:{}:{}",
                    pid_clone,
                    g.get("game_id").and_then(Value::as_str).unwrap_or("")
                )]
            }) {
                Ok((added, found)) => Some(json_response(
                    StatusCode::OK,
                    &json!({
                        "added": added,
                        "found": found,
                        "imported": imported_count,
                        "plugin_id": plugin_id,
                    }),
                    ctx.instance,
                )),
                Err(e) => Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            }
        }
        "/api/v2/themes/select" => {
            let name = str_field(payload, "name").trim().to_owned();
            let platform = str_field(payload, "platform").trim().to_owned();
            if !name.is_empty() {
                let stem = Path::new(&name)
                    .file_stem()
                    .and_then(|s| s.to_str())
                    .unwrap_or("");
                let candidate = ctx.app.data_dir.join("themes").join(format!("{stem}.css"));
                if !candidate.is_file() {
                    return Some(api_error_response(
                        &ApiError::not_found("Theme not found."),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            }
            let name_c = name.clone();
            let plat_c = platform.clone();
            if let Err(e) = ctx.app.transact_state(move |st| {
                let settings = st
                    .as_object_mut()
                    .unwrap()
                    .entry("settings")
                    .or_insert_with(|| json!({}))
                    .as_object_mut()
                    .unwrap();
                if !plat_c.is_empty() {
                    let mappings = settings
                        .entry("theme_by_platform")
                        .or_insert_with(|| json!({}))
                        .as_object_mut()
                        .unwrap();
                    if name_c.is_empty() {
                        mappings.remove(&plat_c);
                    } else {
                        mappings.insert(plat_c, Value::String(name_c));
                    }
                } else {
                    settings.insert("theme".into(), Value::String(name_c));
                }
                Ok(())
            }) {
                return Some(state_error_response(&e, ctx.request_id, ctx.instance));
            }
            if platform.is_empty() {
                write_native_background(&name, &ctx.app.data_dir, &ctx.app.root_dir);
            }
            Some(json_response(
                StatusCode::OK,
                &json!({"selected": name, "platform": platform}),
                ctx.instance,
            ))
        }
        "/api/v2/themes/import" => {
            let source = str_field(payload, "path");
            match import_theme_file(source, &ctx.app.data_dir) {
                Ok(theme) => Some(json_response(
                    StatusCode::OK,
                    &json!({"theme": theme}),
                    ctx.instance,
                )),
                Err(err) => Some(api_error_response(
                    &ApiError::bad_request(err),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/themes/open-folder" => {
            let folder = ctx.app.data_dir.join("themes");
            ensure_stock_themes(&folder, &ctx.app.root_dir);
            let _ = fs::create_dir_all(&folder);
            if open_path(&folder.to_string_lossy()).is_err() {
                return Some(api_error_response(
                    &ApiError::not_found("No application is available to open folders."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            Some(json_response(
                StatusCode::OK,
                &json!({"path": folder.to_string_lossy()}),
                ctx.instance,
            ))
        }
        _ => None,
    }
}
