//! Cached public library & settings projections (`pkg/state/cache.py`).

use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::Path;
use std::sync::Arc;

use bytes::Bytes;
use openbox_core::{APP_VERSION, ApiError};
use openbox_emulators::{
    defs::list_scan_configs,
    gamescope::{is_gamescope_guest, is_mangohud_available, list_gamescope_presets},
};
use openbox_imports::{
    LIST_COLUMNS_DEFAULT, custom_field_defs, list_exclusions, normalize_manual_sessions,
    normalize_notes, platform_categories, public_tokens,
};
use openbox_runtime::cache::{CachedPayload, available_locales};
use openbox_runtime::{AppState, MEDIA_TYPES_ALL};
use openbox_store::StateError;
use openbox_sync::save_tools::save_tool_status;
use serde::ser::{Serialize, SerializeMap, SerializeSeq, Serializer};
use serde_json::{Map, Value, json};

pub const REGION_PRIORITY_DEFAULT: &[&str] = &["North America", "World", "Europe", "Japan", ""];

pub fn strings_for(locale: &str) -> Value {
    let key = locale
        .to_lowercase()
        .split('-')
        .next()
        .unwrap_or("en")
        .to_owned();
    match key.as_str() {
        "es" => json!({
            "library": "Biblioteca",
            "all_games": "Todos los juegos",
            "import": "Importar",
            "settings": "Ajustes",
            "big_box": "Big Box",
            "list_view": "Vista de lista",
            "grid_view": "Vista de cuadrícula",
            "drop_import": "Suelta carpetas ROM o archivos aquí para importar",
        }),
        "de" => json!({
            "library": "Bibliothek",
            "all_games": "Alle Spiele",
            "import": "Importieren",
            "settings": "Einstellungen",
            "big_box": "Big Box",
            "list_view": "Listenansicht",
            "grid_view": "Rasteransicht",
            "drop_import": "ROM-Ordner oder Spieldateien hier ablegen",
        }),
        "fr" => json!({
            "library": "Bibliothèque",
            "all_games": "Tous les jeux",
            "import": "Importer",
            "settings": "Paramètres",
            "big_box": "Big Box",
            "list_view": "Vue liste",
            "grid_view": "Vue grille",
            "drop_import": "Déposez des dossiers ROM ou des fichiers ici",
        }),
        "pt" => json!({
            "library": "Biblioteca",
            "all_games": "Todos os jogos",
            "import": "Importar",
            "settings": "Configurações",
            "big_box": "Big Box",
            "list_view": "Vista em lista",
            "grid_view": "Vista em grade",
            "drop_import": "Solte pastas ROM ou arquivos aqui",
        }),
        _ => json!({
            "library": "Library",
            "all_games": "All games",
            "import": "Import",
            "settings": "Settings",
            "big_box": "Big Box",
            "list_view": "List view",
            "grid_view": "Grid view",
            "drop_import": "Drop ROM folders or game files here to import",
        }),
    }
}

pub fn list_media_packs(settings: &Value) -> Value {
    let active: HashSet<&str> = settings
        .get("active_media_packs")
        .and_then(Value::as_array)
        .map(|a| a.iter().filter_map(Value::as_str).collect())
        .unwrap_or_default();
    json!([
        {
            "id": "clear-logos-default",
            "name": "Platform Clear Logos",
            "description": "High-contrast platform logos for sidebar and Big Box hybrid views.",
            "kinds": ["platform_logo"],
            "active": active.contains("clear-logos-default"),
        },
        {
            "id": "controller-xbox",
            "name": "Xbox Controller Prompts",
            "description": "Xbox-style button prompts for Big Box status hints.",
            "kinds": ["controller_art"],
            "mapping_hint": "A Play · B Back · X Favorite · Y Menu",
            "active": active.contains("controller-xbox"),
        },
        {
            "id": "controller-playstation",
            "name": "PlayStation Controller Prompts",
            "description": "PlayStation-style button prompts for Big Box status hints.",
            "kinds": ["controller_art"],
            "mapping_hint": "Cross Play · Circle Back · Square Favorite · Triangle Menu",
            "active": active.contains("controller-playstation"),
        },
        {
            "id": "badges-core",
            "name": "Core Status Badges",
            "description": "Favorite, save, document, and progress badge styling pack.",
            "kinds": ["badges"],
            "active": active.contains("badges-core"),
        },
    ])
}

fn media_types_from_settings(settings: &Value) -> Vec<String> {
    let allowed: HashSet<&str> = MEDIA_TYPES_ALL.iter().copied().collect();
    let mut vals: Vec<String> = settings
        .get("auto_import_media_types")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .filter_map(Value::as_str)
                .filter(|s| allowed.contains(s))
                .map(ToOwned::to_owned)
                .collect()
        })
        .unwrap_or_default();
    if vals.is_empty() {
        vals = vec!["background".into(), "cover".into(), "screenshots".into()];
    }
    vals.sort();
    vals.dedup();
    vals
}

fn list_presets(state: &Value) -> Vec<Value> {
    state
        .get("filter_presets")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .filter(|item| {
                    item.as_object()
                        .and_then(|o| o.get("name"))
                        .and_then(Value::as_str)
                        .is_some_and(|s| !s.is_empty())
                })
                .cloned()
                .collect()
        })
        .unwrap_or_default()
}

fn get_or(settings: &Map<String, Value>, key: &str, default: Value) -> Value {
    settings.get(key).cloned().unwrap_or(default)
}

pub fn public_settings(app: &AppState, state: &Value) -> Value {
    let sig = app.store.signature();
    if let Some(cached) = app.cache.get_cached_settings(sig) {
        return cached;
    }
    let empty_map = Map::new();
    let settings = state
        .get("settings")
        .and_then(Value::as_object)
        .unwrap_or(&empty_map);
    let settings_val = Value::Object(settings.clone());
    let tok_map = settings
        .get("integration_tokens")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let mut plat_docs = Map::new();
    if let Some(pd) = settings
        .get("platform_documents")
        .and_then(Value::as_object)
    {
        for (plat, docs) in pd {
            plat_docs.insert(
                plat.clone(),
                Value::Array(app.media_probe.sanitize_document_records(docs)),
            );
        }
    }
    let locale = settings
        .get("locale")
        .and_then(Value::as_str)
        .unwrap_or("en");
    let screensaver = settings
        .get("screensaver_seconds")
        .cloned()
        .unwrap_or_else(|| json!(90));
    let prompt_hint = match settings.get("controller_prompt_hint") {
        Some(Value::Bool(true)) => "A Play · B Back · M Menu".to_owned(),
        Some(Value::Bool(false)) | None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(other) => other.to_string(),
    };
    let emumovies_configured = fs::read_to_string(app.data_dir.join("emumovies.json"))
        .ok()
        .and_then(|s| serde_json::from_str::<Value>(&s).ok())
        .and_then(|v| {
            v.get("username")
                .and_then(Value::as_str)
                .map(|s| !s.is_empty())
        })
        .unwrap_or(false);
    let steamgrid_key_configured = fs::read_to_string(app.data_dir.join("steamgrid.json"))
        .ok()
        .and_then(|s| serde_json::from_str::<Value>(&s).ok())
        .and_then(|v| {
            v.get("api_key")
                .and_then(Value::as_str)
                .map(|s| !s.is_empty())
        })
        .unwrap_or(false)
        || !std::env::var("STEAMGRIDDB_API_KEY")
            .unwrap_or_default()
            .trim()
            .is_empty();

    let result = json!({
        "watch_folders": get_or(settings, "watch_folders", json!([])),
        "screensaver_seconds": screensaver,
        "controller_map": get_or(settings, "controller_map", json!({})),
        "controller_types": get_or(settings, "controller_types", json!([])),
        "integration_tokens": Value::Object(public_tokens(&tok_map)),
        "emulator_platform_settings": get_or(settings, "emulator_platform_settings", json!({})),
        "keyboard_map": get_or(settings, "keyboard_map", json!({})),
        "pointer_nav_enabled": get_or(settings, "pointer_nav_enabled", json!(true)),
        "mouse_wheel_nav": get_or(settings, "mouse_wheel_nav", json!(true)),
        "spinner_sensitivity": get_or(settings, "spinner_sensitivity", json!(6)),
        "bigbox_wheel_speed": get_or(settings, "bigbox_wheel_speed", json!(1.0)),
        "bigbox_back_hold_seconds": get_or(settings, "bigbox_back_hold_seconds", json!(0.8)),
        "battery_indicator": get_or(settings, "battery_indicator", json!(true)),
        "bigbox_hidden_platforms": get_or(settings, "bigbox_hidden_platforms", json!([])),
        "bigbox_hidden_categories": get_or(settings, "bigbox_hidden_categories", json!([])),
        "bigbox_hidden_playlists": get_or(settings, "bigbox_hidden_playlists", json!([])),
        "theme": get_or(settings, "theme", json!("")),
        "reader_theme": get_or(settings, "reader_theme", json!("light")),
        "reader_spread": get_or(settings, "reader_spread", json!("single")),
        "cover_model": get_or(settings, "cover_model", json!("box")),
        "cover_model_by_platform": get_or(settings, "cover_model_by_platform", json!({})),
        "model_rotation_lock": get_or(settings, "model_rotation_lock", json!("free")),
        "model_back_by_platform": get_or(settings, "model_back_by_platform", json!({})),
        "model_case_by_platform": get_or(settings, "model_case_by_platform", json!({})),
        "model_rotation_lock_by_platform": get_or(settings, "model_rotation_lock_by_platform", json!({})),
        "bigbox_video_controls": get_or(settings, "bigbox_video_controls", json!(true)),
        "bigbox_video_volume": get_or(settings, "bigbox_video_volume", json!(0.8)),
        "bigbox_video_backdrop": get_or(settings, "bigbox_video_backdrop", json!(false)),
        "bigbox_sounds_enabled": settings.get("bigbox_sounds_enabled").and_then(Value::as_bool).unwrap_or(true),
        "bigbox_startup_video_loop": get_or(settings, "bigbox_startup_video_loop", json!(false)),
        "ra_hardcore": settings.get("ra_hardcore").and_then(Value::as_bool).unwrap_or(false),
        "platform_videos": get_or(settings, "platform_videos", json!({})),
        "badge_visibility": get_or(settings, "badge_visibility", json!(["favorite", "installed", "saves", "documents", "progress", "storefront", "achievements", "rating"])),
        "image_group": get_or(settings, "image_group", json!("cover")),
        "image_group_by_platform": get_or(settings, "image_group_by_platform", json!({})),
        "image_group_by_playlist": get_or(settings, "image_group_by_playlist", json!({})),
        "cloud_folder": get_or(settings, "cloud_folder", json!("")),
        "library_sync_enabled": get_or(settings, "library_sync_enabled", json!(false)),
        "last_cloud_sync": get_or(settings, "last_cloud_sync", json!("")),
        "startup_commands": get_or(settings, "startup_commands", json!([])),
        "shutdown_commands": get_or(settings, "shutdown_commands", json!([])),
        "track_session_history": get_or(settings, "track_session_history", json!(true)),
        "session_recap_enabled": get_or(settings, "session_recap_enabled", json!(true)),
        "quick_resume_enabled": get_or(settings, "quick_resume_enabled", json!(true)),
        "moments_autocapture": get_or(settings, "moments_autocapture", json!(true)),
        "state_retention": get_or(settings, "state_retention", json!(1)),
        "backup_on_close": get_or(settings, "backup_on_close", json!(false)),
        "save_backup_limit": get_or(settings, "save_backup_limit", json!(10)),
        "save_management_enabled": settings.get("save_management_enabled").and_then(Value::as_bool).unwrap_or(true),
        "save_vault_enabled": settings.get("save_vault_enabled").and_then(Value::as_bool).unwrap_or(true),
        "save_vault_keep": get_or(settings, "save_vault_keep", json!(5)),
        "save_vault_on_close": settings.get("save_vault_on_close").and_then(Value::as_bool).unwrap_or(false),
        "save_vault_auto_days": get_or(settings, "save_vault_auto_days", json!(0)),
        "deals_enabled": settings.get("deals_enabled").and_then(Value::as_bool).unwrap_or(false),
        "deals_poll_hours": get_or(settings, "deals_poll_hours", json!(168)),
        "deals_min_drop_pct": get_or(settings, "deals_min_drop_pct", json!(10)),
        "deals_store_cc": get_or(settings, "deals_store_cc", json!("us")),
        "backup_auto_enabled": get_or(settings, "backup_auto_enabled", json!(false)),
        "backup_auto_keep": get_or(settings, "backup_auto_keep", json!(4)),
        "last_auto_backup": get_or(settings, "last_auto_backup", json!("")),
        "progress_automation_enabled": get_or(settings, "progress_automation_enabled", json!(false)),
        "progress_automation_play_minutes": get_or(settings, "progress_automation_play_minutes", json!(30)),
        "progress_automation_idle_days": get_or(settings, "progress_automation_idle_days", json!(30)),
        "welcome_completed": get_or(settings, "welcome_completed", json!(false)),
        "auto_import_media_types": media_types_from_settings(&settings_val),
        "media_download_limit": get_or(settings, "media_download_limit", json!(0)),
        "media_download_limit_per_group": get_or(settings, "media_download_limit_per_group", json!(0)),
        "metadata_provider_priority": get_or(settings, "metadata_provider_priority", json!([])),
        "plugin_metadata_priority": settings.get("plugin_metadata_priority").and_then(Value::as_bool).unwrap_or(false),
        "memories_import_enabled": get_or(settings, "memories_import_enabled", json!(false)),
        "memories_import_roots": get_or(settings, "memories_import_roots", json!([])),
        "steamgrid_enabled": get_or(settings, "steamgrid_enabled", json!(true)),
        "steamgrid_key_configured": steamgrid_key_configured,
        "region_priority": get_or(settings, "region_priority", json!(REGION_PRIORITY_DEFAULT)),
        "video_priority": get_or(settings, "video_priority", json!(["video_snap", "video_theme", "video_trailer", "video_recording"])),
        "library_music": get_or(settings, "library_music", json!("")),
        "video_bgm_mix": get_or(settings, "video_bgm_mix", json!(false)),
        "bigbox_mode": get_or(settings, "bigbox_mode", json!("stage")),
        "bigbox_start_at_launch": get_or(settings, "bigbox_start_at_launch", json!(false)),
        "show_insights": get_or(settings, "show_insights", json!(true)),
        "show_launch_badges": get_or(settings, "show_launch_badges", json!(true)),
        "show_playlist_actions": get_or(settings, "show_playlist_actions", json!(true)),
        "sidebar_sections": get_or(settings, "sidebar_sections", json!(["search", "view", "filters", "platforms", "playlists", "explorer"])),
        "progress_values": get_or(settings, "progress_values", json!(["Unplayed", "Playing", "Paused", "Beaten", "Completed", "Mastered", "Abandoned"])),
        "hidden_sidebar_sections": get_or(settings, "hidden_sidebar_sections", json!([])),
        "storefront_auto_import": get_or(settings, "storefront_auto_import", json!({
            "steam": {"enabled": false, "platform": "", "include_uninstalled": false},
            "heroic": {"enabled": false, "platform": "", "include_uninstalled": false},
            "lutris": {"enabled": false, "platform": "", "include_uninstalled": false},
            "gameyfin": {"enabled": false, "platform": "", "include_uninstalled": false},
        })),
        "auto_import_excluded_platforms": get_or(settings, "auto_import_excluded_platforms", json!([])),
        "emulator_update_checks": get_or(settings, "emulator_update_checks", json!([])),
        "obs_auto_attach": get_or(settings, "obs_auto_attach", json!(true)),
        "obs_recording_path": get_or(settings, "obs_recording_path", json!("")),
        "obs_replay_enabled": settings.get("obs_replay_enabled").and_then(Value::as_bool).unwrap_or(false),
        "obs_websocket_url": get_or(settings, "obs_websocket_url", json!("")),
        "household_stats_sharing": settings.get("household_stats_sharing").and_then(Value::as_bool).unwrap_or(false),
        "museum_kiosk_enabled": settings.get("museum_kiosk_enabled").and_then(Value::as_bool).unwrap_or(false),
        "museum_kiosk_pin_set": settings.get("museum_kiosk_pin_hash").and_then(Value::as_str).is_some_and(|s| !s.is_empty()),
        "dynamic_play_button": get_or(settings, "dynamic_play_button", json!(true)),
        "gameyfin_url": get_or(settings, "gameyfin_url", json!("")),
        "gameyfin_username": get_or(settings, "gameyfin_username", json!("")),
        "gameyfin_password_set": settings.get("gameyfin_password").and_then(Value::as_str).is_some_and(|s| !s.is_empty()),
        "gameyfin_install_dir": get_or(settings, "gameyfin_install_dir", json!("")),
        "gameyfin_provider": get_or(settings, "gameyfin_provider", json!("")),
        "ludusavi_backup_path": get_or(settings, "ludusavi_backup_path", json!("")),
        "save_tools": save_tool_status(),
        "platform_documents": Value::Object(plat_docs),
        "custom_field_defs": custom_field_defs(&settings_val),
        "platform_categories": platform_categories(&settings_val),
        "list_columns": get_or(settings, "list_columns", json!(LIST_COLUMNS_DEFAULT)),
        "library_view": get_or(settings, "library_view", json!("grid")),
        "cover_grouping": get_or(settings, "cover_grouping", json!("shape")),
        "locale": locale,
        "font_family": get_or(settings, "font_family", json!("system")),
        "currency": get_or(settings, "currency", json!("USD")),
        "available_locales": available_locales(),
        "strings": strings_for(locale),
        "attract_mode_seconds": get_or(settings, "attract_mode_seconds", screensaver),
        "bigbox_startup_video": get_or(settings, "bigbox_startup_video", json!("")),
        "bigbox_shutdown_commands": get_or(settings, "bigbox_shutdown_commands", json!([])),
        "tray_enabled": get_or(settings, "tray_enabled", json!(false)),
        "minimize_to_tray": get_or(settings, "minimize_to_tray", json!(false)),
        "media_packs": list_media_packs(&settings_val),
        "controller_prompt_hint": prompt_hint,
        "controller_prompt_pack": get_or(settings, "controller_prompt_pack", json!("xbox")),
        "premium_features_free": true,
        "progress_on_first_play": get_or(settings, "progress_on_first_play", json!("Playing")),
        "tracking_mode": get_or(settings, "tracking_mode", json!("default")),
        "tracking_delay": get_or(settings, "tracking_delay", json!(0)),
        "tracking_frequency": get_or(settings, "tracking_frequency", json!(2)),
        "tracking_process_name": get_or(settings, "tracking_process_name", json!("")),
        "apply_perf": get_or(settings, "apply_perf", json!("auto")),
        "auto_close_store_clients": get_or(settings, "auto_close_store_clients", json!(false)),
        "filter_presets": list_presets(state),
        "import_exclusions": list_exclusions(state),
        "emulator_scan_configs": list_scan_configs(state),
        "safe_mode": !std::env::var("OPENBOX_SAFE_MODE").unwrap_or_default().is_empty(),
        "emumovies_configured": emumovies_configured,
        "version": APP_VERSION,
        "appimage": !std::env::var("APPIMAGE").unwrap_or_default().is_empty(),
        "gamescope_guest": is_gamescope_guest(false),
        "gamescope_presets": list_gamescope_presets(settings.get("gamescope_custom_presets")),
        "mangohud_available": is_mangohud_available(),
        "gamescope_preset": get_or(settings, "gamescope_preset", json!("")),
        "mangohud_enabled": get_or(settings, "mangohud_enabled", json!(false)),
        "mood_match_enabled": get_or(settings, "mood_match_enabled", json!(false)),
        "mood_match_bigbox": get_or(settings, "mood_match_bigbox", json!(false)),
    });
    if app.store.signature() == sig {
        app.cache.set_cached_settings(sig, result.clone());
    }
    result
}

fn ra_cache_record(game_id: &str, data_dir: &std::path::Path) -> Map<String, Value> {
    let gid = game_id.trim();
    if gid.is_empty() {
        return Map::new();
    }
    let path = data_dir
        .join("cache")
        .join("retroachievements")
        .join(format!("{gid}.json"));
    std::fs::read_to_string(&path)
        .ok()
        .and_then(|raw| serde_json::from_str::<Value>(&raw).ok())
        .and_then(|v| v.as_object().cloned())
        .unwrap_or_default()
}

fn achievement_source_entry(
    name: &str,
    earned: i64,
    total: i64,
    items: Option<&Value>,
    mastered: bool,
) -> Option<Map<String, Value>> {
    if total <= 0 {
        return None;
    }
    let earned = earned.clamp(0, total);
    let pct = ((earned as f64) * 100.0 / (total as f64) * 10.0).round() / 10.0;
    let items_val = items
        .and_then(Value::as_array)
        .cloned()
        .map(Value::Array)
        .unwrap_or_else(|| Value::Array(Vec::new()));
    let mut m = Map::new();
    m.insert("name".into(), Value::String(name.to_owned()));
    m.insert("earned".into(), json!(earned));
    m.insert("total".into(), json!(total));
    m.insert("progress_pct".into(), json!(pct));
    m.insert("mastered".into(), json!(mastered || earned >= total));
    m.insert("items".into(), items_val);
    Some(m)
}

fn achievement_sources_for_game(
    g: &Map<String, Value>,
    data_dir: &std::path::Path,
    overrides: Option<&Map<String, Value>>,
) -> Map<String, Value> {
    let mut sources: Map<String, Value> = Map::new();
    let game_id = g.get("game_id").and_then(Value::as_str).unwrap_or("");
    let ra_game_id = g.get("ra_game_id").and_then(Value::as_str).unwrap_or("");
    if !ra_game_id.is_empty() {
        let ra_cache = ra_cache_record(game_id, data_dir);
        let earned = ra_cache
            .get("earned")
            .or_else(|| g.get("ra_achievements_earned"))
            .and_then(Value::as_i64)
            .unwrap_or(0);
        let total = ra_cache
            .get("total")
            .or_else(|| g.get("ra_achievements_total"))
            .and_then(Value::as_i64)
            .unwrap_or(0);
        let items = ra_cache
            .get("achievements")
            .or_else(|| ra_cache.get("items"));
        let mastered = ra_cache
            .get("mastered")
            .or_else(|| g.get("ra_mastered"))
            .and_then(Value::as_bool)
            .unwrap_or(false);
        if let Some(mut entry) =
            achievement_source_entry("RetroAchievements", earned, total, items, mastered)
        {
            entry.insert("id".into(), Value::String("retroachievements".to_owned()));
            sources.insert("retroachievements".into(), Value::Object(entry));
        }
    }
    let steam_app_id = g
        .get("steam_app_id")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("");
    if !steam_app_id.is_empty()
        && let Some(cached) =
            openbox_metadata::steam_achievements::load_cache_any_age(data_dir, steam_app_id)
        && let Some(ach) = cached.get("achievement").and_then(Value::as_object)
    {
        let name = ach.get("name").and_then(Value::as_str).unwrap_or("Steam");
        let earned = ach.get("earned").and_then(Value::as_i64).unwrap_or(0);
        let total = ach.get("total").and_then(Value::as_i64).unwrap_or(0);
        if let Some(mut entry) =
            achievement_source_entry(name, earned, total, ach.get("items"), false)
        {
            entry.insert("id".into(), Value::String("steam".to_owned()));
            sources.insert("steam".into(), Value::Object(entry));
        }
    }
    if let Some(raw_ov) = overrides
        .and_then(|m| m.get(game_id))
        .and_then(Value::as_object)
    {
        for (key, val) in raw_ov {
            let Some(ov) = val.as_object() else {
                continue;
            };
            let existing = sources.get(key).and_then(Value::as_object);
            let name = existing
                .and_then(|e| e.get("name"))
                .and_then(Value::as_str)
                .unwrap_or(key);
            let earned = ov
                .get("earned")
                .and_then(Value::as_i64)
                .or_else(|| {
                    existing
                        .and_then(|e| e.get("earned"))
                        .and_then(Value::as_i64)
                })
                .unwrap_or(0);
            let total = ov
                .get("total")
                .and_then(Value::as_i64)
                .or_else(|| {
                    existing
                        .and_then(|e| e.get("total"))
                        .and_then(Value::as_i64)
                })
                .unwrap_or(0);
            if total <= 0 {
                sources.remove(key);
                continue;
            }
            let items = existing.and_then(|e| e.get("items")).cloned();
            let mastered = ov.get("mastered").and_then(Value::as_bool).unwrap_or(false);
            if let Some(mut corrected) =
                achievement_source_entry(name, earned, total, items.as_ref(), mastered)
            {
                corrected.insert("id".into(), Value::String(key.clone()));
                corrected.insert("manual".into(), Value::Bool(true));
                sources.insert(key.clone(), Value::Object(corrected));
            }
        }
    }
    sources
}

fn project_game(
    app: &AppState,
    game: &Value,
    index: usize,
    plat_cats: &std::collections::BTreeMap<String, String>,
    overrides: Option<&Map<String, Value>>,
) -> Value {
    let empty = Map::new();
    let g = game.as_object().unwrap_or(&empty);
    let str_k = |k: &str| g.get(k).and_then(Value::as_str).unwrap_or("");
    let san = |k: &str| {
        let raw = str_k(k);
        if raw.is_empty() {
            String::new()
        } else {
            app.media_probe.sanitize_media_path(raw)
        }
    };
    let raw_path = str_k("path");
    let shelf_entry = g
        .get("manual_entry")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let path_exists = !raw_path.is_empty() && app.media_probe.probe_path(raw_path, false);
    let steam_id = str_k("steam_app_id");
    let heroic_id = str_k("heroic_app_id");
    let lutris_id = str_k("lutris_id");
    let gameyfin_id = str_k("gameyfin_id");
    let store_catalog = g
        .get("store_catalog")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let store_installed = g
        .get("store_installed")
        .and_then(Value::as_bool)
        .unwrap_or(!store_catalog || path_exists);
    let playable = !shelf_entry && (path_exists || !str_k("launch").is_empty());

    let cov = san("cover");
    let bg = san("background");
    let logo = san("clear_logo");
    let fanart = san("fanart");
    let banner = san("banner");
    let icon = san("icon");
    let bback = san("box_back");
    let bspine = san("box_spine");
    let b3d = san("box_3d");
    let tscreen = san("title_screen");
    let cfront = san("cart_front");
    let cback = san("cart_back");
    let disc = san("disc");
    let ad = san("advertisement");
    let poster = san("poster");
    let square = san("square");
    let man = san("manual");
    let mus = san("music");
    let vid = san("video");
    let vsnap = san("video_snap");
    let vtheme = san("video_theme");
    let vtrail = san("video_trailer");
    let vrec = san("video_recording");

    let has = |p: &str| !p.is_empty() && app.media_probe.probe_path(p, true);
    let has_cov = has(&cov);
    let (vfield, vpath_clean) = [
        ("video_snap", &vsnap),
        ("video_theme", &vtheme),
        ("video_trailer", &vtrail),
        ("video_recording", &vrec),
        ("video", &vid),
    ]
    .into_iter()
    .find(|(_, p)| has(p))
    .map(|(f, p)| (f, p.as_str()))
    .unwrap_or(("", ""));

    let docs = app
        .media_probe
        .sanitize_document_records(g.get("documents").unwrap_or(&Value::Null));
    let screenshots: Vec<String> = g
        .get("screenshots")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .filter_map(Value::as_str)
                .map(|p| app.media_probe.sanitize_media_path(p))
                .filter(|p| !p.is_empty())
                .collect()
        })
        .unwrap_or_default();
    let available_screenshots: Vec<usize> = screenshots
        .iter()
        .enumerate()
        .filter(|(_, p)| has(p))
        .map(|(i, _)| i)
        .collect();

    let manual_sessions = normalize_manual_sessions(g.get("manual_sessions"));
    let manual_seconds: i64 = manual_sessions
        .iter()
        .filter_map(|s| s.get("seconds").and_then(Value::as_i64))
        .sum();
    let observed_seconds = g
        .get("playtime_seconds")
        .and_then(Value::as_i64)
        .unwrap_or(0)
        .max(0);
    let note_entries = normalize_notes(g.get("notes"));
    let user_rating = g
        .get("user_rating")
        .and_then(Value::as_i64)
        .unwrap_or(0)
        .clamp(0, 5);
    let platform = str_k("platform");
    let platform_cat = plat_cats
        .get(if platform.is_empty() {
            "Unspecified"
        } else {
            platform
        })
        .map(String::as_str)
        .unwrap_or("Other");

    let launch_env: Map<String, Value> = g
        .get("launch_env")
        .and_then(Value::as_object)
        .map(|m| {
            m.iter()
                .map(|(k, v)| {
                    (
                        k.clone(),
                        Value::String(v.as_str().unwrap_or("").to_owned()),
                    )
                })
                .collect()
        })
        .unwrap_or_default();
    let tags: Vec<Value> = g
        .get("tags")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let alt: Vec<Value> = g
        .get("alternate_names")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let custom = g
        .get("custom_fields")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let memories: Vec<Value> = g
        .get("memories")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .filter_map(|m| {
                    let o = m.as_object()?;
                    let sp = app
                        .media_probe
                        .sanitize_media_path(o.get("path").and_then(Value::as_str).unwrap_or(""));
                    if sp.is_empty() {
                        return None;
                    }
                    Some(json!({
                        "path": sp,
                        "sha256": o.get("sha256").and_then(Value::as_str).unwrap_or(""),
                        "bytes": o.get("bytes").and_then(Value::as_i64).unwrap_or(0),
                        "source": o.get("source").and_then(Value::as_str).unwrap_or(""),
                        "taken_at": o.get("taken_at").and_then(Value::as_str).unwrap_or(""),
                        "imported_at": o.get("imported_at").and_then(Value::as_str).unwrap_or(""),
                    }))
                })
                .collect()
        })
        .unwrap_or_default();
    let moments_count = g
        .get("moments")
        .and_then(Value::as_array)
        .map(|a| a.iter().filter(|m| m.is_object()).count())
        .unwrap_or(0);
    let clips: Vec<Value> = g
        .get("clips")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .rev()
                .take(100)
                .rev()
                .filter_map(|c| {
                    let o = c.as_object()?;
                    let sp = app
                        .media_probe
                        .sanitize_media_path(o.get("path").and_then(Value::as_str).unwrap_or(""));
                    if sp.is_empty() {
                        return None;
                    }
                    Some(json!({
                        "path": sp,
                        "clip_id": o.get("clip_id").and_then(Value::as_str).unwrap_or(""),
                        "created_at": o.get("created_at").and_then(Value::as_str).unwrap_or(""),
                        "launch_id": o.get("launch_id").and_then(Value::as_str).unwrap_or(""),
                        "title": o.get("title").and_then(Value::as_str).unwrap_or("Replay clip"),
                        "source": o.get("source").and_then(Value::as_str).unwrap_or(""),
                        "fallback": o.get("fallback").and_then(Value::as_bool).unwrap_or(false),
                    }))
                })
                .collect()
        })
        .unwrap_or_default();

    let ach_sources = achievement_sources_for_game(g, &app.data_dir, overrides);

    json!({
        "name": str_k("name"),
        "platform": platform,
        "genre": str_k("genre"),
        "year": str_k("year"),
        "developer": str_k("developer"),
        "publisher": str_k("publisher"),
        "series": str_k("series"),
        "collection": str_k("collection"),
        "description": str_k("description"),
        "path": raw_path,
        "manual_entry": shelf_entry,
        "entry_type": if shelf_entry { "shelf" } else { "playable" },
        "playable": playable,
        "steam_installable": !steam_id.is_empty() && !store_installed,
        "steam_repairable": !steam_id.is_empty() && store_installed,
        "launch": str_k("launch"),
        "launch_profile": str_k("launch_profile"),
        "gamescope_preset": str_k("gamescope_preset"),
        "launch_env": Value::Object(launch_env),
        "cover": cov,
        "background": bg,
        "clear_logo": logo,
        "fanart": fanart,
        "banner": banner,
        "icon": icon,
        "box_back": bback,
        "box_spine": bspine,
        "box_3d": b3d,
        "title_screen": tscreen,
        "cart_front": cfront,
        "cart_back": cback,
        "disc": disc,
        "advertisement": ad,
        "manual": man,
        "source": str_k("source"),
        "cover_model": str_k("cover_model"),
        "steam_app_id": steam_id,
        "lutris_id": lutris_id,
        "gog_id": str_k("gog_id"),
        "ea_id": str_k("ea_id"),
        "ubisoft_id": str_k("ubisoft_id"),
        "install_dir": str_k("install_dir"),
        "import_batch_id": str_k("import_batch_id"),
        "heroic_app_id": heroic_id,
        "rom_name": str_k("rom_name"),
        "clone_of": str_k("clone_of"),
        "set_type": str_k("set_type"),
        "ra_game_id": str_k("ra_game_id"),
        "ra_achievements_earned": g.get("ra_achievements_earned").and_then(Value::as_i64).unwrap_or(0),
        "ra_achievements_earned_hardcore": g.get("ra_achievements_earned_hardcore").and_then(Value::as_i64).unwrap_or(0),
        "ra_achievements_total": g.get("ra_achievements_total").and_then(Value::as_i64).unwrap_or(0),
        "ra_progress_pct": g.get("ra_progress_pct").and_then(Value::as_f64).unwrap_or(0.0),
        "ra_mastered": g.get("ra_mastered").and_then(Value::as_bool).unwrap_or(false),
        "ra_hash": str_k("ra_hash"),
        "launchbox_db_id": str_k("launchbox_db_id"),
        "archive_member": str_k("archive_member"),
        "video": vid,
        "music": mus,
        "video_snap": vsnap,
        "video_theme": vtheme,
        "video_trailer": vtrail,
        "video_recording": vrec,
        "progress": str_k("progress"),
        "rating": g.get("rating").cloned().unwrap_or_else(|| json!("")),
        "notes": note_entries,
        "user_rating": user_rating,
        "price_paid_cents": g.get("price_paid_cents").cloned().unwrap_or(Value::Null),
        "price_currency": str_k("price_currency"),
        "manual_playtime_seconds": manual_seconds,
        "manual_sessions": manual_sessions,
        "total_playtime_seconds": observed_seconds + manual_seconds,
        "region": str_k("region"),
        "play_mode": str_k("play_mode"),
        "sort_title": str_k("sort_title"),
        "added_at": str_k("added_at"),
        "alternate_names": alt,
        "max_players": str_k("max_players"),
        "wikipedia_url": str_k("wikipedia_url"),
        "video_url": str_k("video_url"),
        "hide_in_bigbox": g.get("hide_in_bigbox").and_then(Value::as_bool).unwrap_or(false),
        "esrb": str_k("esrb"),
        "broken": g.get("broken").and_then(Value::as_bool).unwrap_or(false),
        "portable": g.get("portable").and_then(Value::as_bool).unwrap_or(false),
        "controller_support": str_k("controller_support"),
        "disc_count": g.get("disc_count").cloned().unwrap_or_else(|| json!("")),
        "gameyfin_id": gameyfin_id,
        "gameyfin_provider": str_k("gameyfin_provider"),
        "store_catalog": store_catalog,
        "store_installed": store_installed,
        "owned": g.get("owned").and_then(Value::as_bool).unwrap_or(false)
            || store_catalog
            || !steam_id.is_empty()
            || !heroic_id.is_empty()
            || !lutris_id.is_empty()
            || !gameyfin_id.is_empty(),
        "tracking_mode": str_k("tracking_mode"),
        "tracking_delay": g.get("tracking_delay").cloned().unwrap_or_else(|| json!("")),
        "tracking_frequency": g.get("tracking_frequency").cloned().unwrap_or_else(|| json!("")),
        "tracking_process_name": str_k("tracking_process_name"),
        "igdb_id": str_k("igdb_id"),
        "id": index,
        "game_id": str_k("game_id"),
        "favorite": g.get("favorite").and_then(Value::as_bool).unwrap_or(false),
        "hidden": g.get("hidden").and_then(Value::as_bool).unwrap_or(false),
        "last_played": str_k("last_played"),
        "play_count": g.get("play_count").and_then(Value::as_i64).unwrap_or(0),
        "playtime_seconds": observed_seconds,
        "time_to_beat_hours": g.get("time_to_beat_hours").cloned().unwrap_or(Value::Null),
        "path_exists": path_exists,
        "has_cover": has_cov,
        "has_background": has(&bg),
        "has_clear_logo": has(&logo),
        "has_fanart": has(&fanart),
        "has_banner": has(&banner),
        "has_icon": has(&icon),
        "has_box_back": has(&bback),
        "has_box_spine": has(&bspine),
        "has_box_3d": has(&b3d),
        "has_title_screen": has(&tscreen),
        "has_cart_front": has(&cfront),
        "has_cart_back": has(&cback),
        "has_disc": has(&disc),
        "has_advertisement": has(&ad),
        "has_poster": has(&poster),
        "has_square": has(&square),
        "has_manual": has(&man),
        "has_video": !vpath_clean.is_empty(),
        "active_video_field": vfield,
        "has_music": has(&mus),
        "has_saves": g.get("save_paths").and_then(Value::as_array).is_some_and(|a| !a.is_empty()),
        "has_documents": !docs.is_empty(),
        "has_versions": g.get("versions").and_then(Value::as_array).is_some_and(|a| !a.is_empty()),
        "has_achievements": !ach_sources.is_empty(),
        "achievement_sources": Value::Object(ach_sources),
        "has_highscores": !str_k("rom_name").is_empty()
            && matches!(platform.to_lowercase().as_str(), "arcade" | "mame" | "finalburn neo"),
        "has_missing_media": !has_cov,
        "extract_archive": g.get("extract_archive").and_then(Value::as_bool).unwrap_or(false),
        "launch_confirm": g.get("launch_confirm").and_then(Value::as_bool).unwrap_or(false),
        "applications": g.get("applications").cloned().unwrap_or_else(|| json!([])),
        "versions": g.get("versions").cloned().unwrap_or_else(|| json!([])),
        "documents": docs,
        "save_paths": g.get("save_paths").cloned().unwrap_or_else(|| json!([])),
        "screenshots": screenshots,
        "available_screenshots": available_screenshots,
        "memories": memories,
        "has_memories": !memories.is_empty(),
        "moments_count": moments_count,
        "has_moments": moments_count > 0,
        "clips": clips,
        "has_clips": !clips.is_empty(),
        "custom_fields": Value::Object(custom),
        "resume_slot": str_k("resume_slot"),
        "platform_category": platform_cat,
        "tags": tags,
        "installable": !gameyfin_id.is_empty() && !store_installed,
        "legacy_game_ids": g.get("legacy_game_ids").cloned().unwrap_or_else(|| json!([])),
    })
}

fn build_source_labels(raw_games: &[Value]) -> Map<String, Value> {
    let mut source_labels = Map::new();
    source_labels.insert("Local".into(), Value::String("Local".to_owned()));
    for g in raw_games {
        if let Some(src) = g.get("source").and_then(Value::as_str).map(str::trim)
            && !src.is_empty()
        {
            let lbl = g
                .get("plugin_source_name")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .unwrap_or(src);
            source_labels.insert(src.to_owned(), Value::String(lbl.to_owned()));
        }
    }
    source_labels
}

fn cached_source_labels(
    app: &AppState,
    sig: Option<openbox_store::FileSignature>,
    raw_games: &[Value],
) -> Arc<Map<String, Value>> {
    if let Some(hit) = app.cache.get_cached_source_labels(sig) {
        return hit;
    }
    app.cache
        .set_cached_source_labels(sig, build_source_labels(raw_games))
}

struct StreamingGames<'a> {
    app: &'a AppState,
    raw_games: &'a [Value],
    start_index: usize,
    plat_cats: &'a std::collections::BTreeMap<String, String>,
    overrides: Option<&'a Map<String, Value>>,
}

impl Serialize for StreamingGames<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut seq = serializer.serialize_seq(Some(self.raw_games.len()))?;
        for (i, g) in self.raw_games.iter().enumerate() {
            let projected = project_game(
                self.app,
                g,
                self.start_index + i,
                self.plat_cats,
                self.overrides,
            );
            seq.serialize_element(&projected)?;
        }
        seq.end()
    }
}

struct StreamingPublicState<'a> {
    games: StreamingGames<'a>,
    playlists: &'a Value,
    filter_presets: Vec<Value>,
    smart_collections: Vec<Value>,
    ra_configured: bool,
    ra_hardcore: bool,
    settings: Value,
    discovery: Value,
    media_epoch: u64,
    startup_errors: Vec<Value>,
    source_labels: Arc<Map<String, Value>>,
}

impl Serialize for StreamingPublicState<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        // Emit keys in sorted order matching serde_json::Map / json! output.
        let mut map = serializer.serialize_map(Some(11))?;
        map.serialize_entry("discovery", &self.discovery)?;
        map.serialize_entry("filter_presets", &self.filter_presets)?;
        map.serialize_entry("games", &self.games)?;
        map.serialize_entry("media_epoch", &self.media_epoch)?;
        map.serialize_entry("playlists", self.playlists)?;
        map.serialize_entry("ra_configured", &self.ra_configured)?;
        map.serialize_entry("ra_hardcore", &self.ra_hardcore)?;
        map.serialize_entry("settings", &self.settings)?;
        map.serialize_entry("smart_collections", &self.smart_collections)?;
        map.serialize_entry("source_labels", self.source_labels.as_ref())?;
        map.serialize_entry("startup_errors", &self.startup_errors)?;
        map.end()
    }
}

struct StreamingLibraryPage<'a> {
    games: StreamingGames<'a>,
    total_count: usize,
    offset: usize,
    limit: usize,
    playlists: &'a Value,
    filter_presets: Vec<Value>,
    settings: Value,
    media_epoch: u64,
    ra_configured: bool,
    startup_errors: Vec<Value>,
    source_labels: Arc<Map<String, Value>>,
}

impl Serialize for StreamingLibraryPage<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut map = serializer.serialize_map(Some(11))?;
        map.serialize_entry("filter_presets", &self.filter_presets)?;
        map.serialize_entry("games", &self.games)?;
        map.serialize_entry("limit", &self.limit)?;
        map.serialize_entry("media_epoch", &self.media_epoch)?;
        map.serialize_entry("offset", &self.offset)?;
        map.serialize_entry("playlists", self.playlists)?;
        map.serialize_entry("ra_configured", &self.ra_configured)?;
        map.serialize_entry("settings", &self.settings)?;
        map.serialize_entry("source_labels", self.source_labels.as_ref())?;
        map.serialize_entry("startup_errors", &self.startup_errors)?;
        map.serialize_entry("total_count", &self.total_count)?;
        map.end()
    }
}

/// Stream the full public library JSON directly into a byte buffer (never retaining a
/// `Vec<Value>` of projected games in RAM) and cache the `Bytes` + gzip representation.
pub fn public_state_cached(app: &AppState) -> Result<Arc<CachedPayload>, StateError> {
    let sig = app.store.signature();
    if let Some(cached) = app.cache.get_cached_state(sig) {
        return Ok(cached);
    }
    let state = app.load_state_view()?;
    let empty_obj = json!({});
    let empty_arr = json!([]);
    let settings = state.get("settings").unwrap_or(&empty_obj);
    let plat_cats = platform_categories(settings);
    let raw_games = state
        .get("games")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let overrides = state
        .get("achievement_overrides")
        .and_then(Value::as_object);
    let source_labels = cached_source_labels(app, sig, raw_games);
    let startup_errors: Vec<Value> = app
        .startup_errors
        .lock()
        .iter()
        .take(20)
        .cloned()
        .map(Value::String)
        .collect();
    let pub_set = public_settings(app, &state);
    let ra_configured =
        openbox_metadata::retroachievements::load_credentials(&app.data_dir).is_some();
    let ra_hardcore = settings
        .get("ra_hardcore")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let mut discovery = openbox_discovery::discovery::discovery_lists(raw_games, 12);
    openbox_plugins::discovery::merge_plugin_discovery(
        &mut discovery,
        raw_games,
        &app.data_dir.join("plugins"),
        12,
        Some(&state),
        None,
        Some(&app.root_dir),
    );
    let playlists = state.get("playlists").unwrap_or(&empty_arr);
    let streaming = StreamingPublicState {
        games: StreamingGames {
            app,
            raw_games,
            start_index: 0,
            plat_cats: &plat_cats,
            overrides,
        },
        playlists,
        filter_presets: openbox_discovery::filter_presets::list_presets(&state),
        smart_collections: openbox_discovery::collections::list_collections(&state),
        ra_configured,
        ra_hardcore,
        settings: pub_set,
        discovery,
        media_epoch: app.cache.media_epoch(),
        startup_errors,
        source_labels,
    };
    let mut buf = Vec::with_capacity(raw_games.len().saturating_mul(2048).min(32 * 1024 * 1024));
    let _ = serde_json::to_writer(&mut buf, &streaming);
    Ok(app.cache.set_cached_state_bytes(sig, buf))
}

/// Project only the requested `[offset..offset+limit)` page of games directly to cached
/// serialized JSON `Bytes` (bounded 8-page LRU cache invalidated on every state/epoch change).
pub fn public_library_page_bytes(
    app: &AppState,
    offset: usize,
    limit: usize,
) -> Result<Bytes, StateError> {
    let sig = app.store.signature();
    if let Some(cached) = app.cache.get_cached_page(sig, offset, limit) {
        return Ok(cached);
    }
    let state = app.load_state_view()?;
    let empty_obj = json!({});
    let empty_arr = json!([]);
    let settings = state.get("settings").unwrap_or(&empty_obj);
    let plat_cats = platform_categories(settings);
    let raw_games = state
        .get("games")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let overrides = state
        .get("achievement_overrides")
        .and_then(Value::as_object);
    let total_count = raw_games.len();
    let start = offset.min(total_count);
    let end = start.saturating_add(limit).min(total_count);
    let source_labels = cached_source_labels(app, sig, raw_games);
    let startup_errors: Vec<Value> = app
        .startup_errors
        .lock()
        .iter()
        .take(20)
        .cloned()
        .map(Value::String)
        .collect();
    let pub_set = public_settings(app, &state);
    let ra_configured =
        openbox_metadata::retroachievements::load_credentials(&app.data_dir).is_some();
    let playlists = state.get("playlists").unwrap_or(&empty_arr);
    let page = StreamingLibraryPage {
        games: StreamingGames {
            app,
            raw_games: &raw_games[start..end],
            start_index: start,
            plat_cats: &plat_cats,
            overrides,
        },
        total_count,
        offset,
        limit,
        playlists,
        filter_presets: openbox_discovery::filter_presets::list_presets(&state),
        settings: pub_set,
        media_epoch: app.cache.media_epoch(),
        ra_configured,
        startup_errors,
        source_labels,
    };
    let mut buf = Vec::with_capacity((end - start).saturating_mul(2048).min(4 * 1024 * 1024));
    let _ = serde_json::to_writer(&mut buf, &page);
    Ok(app.cache.set_cached_page(sig, offset, limit, buf))
}

/// Project only the explicitly requested game IDs in O(k) using the store's `games_by_id` index.
pub fn public_library_delta(app: &AppState, ids: &[&str]) -> Result<Value, StateError> {
    let state = app.load_state_view()?;
    let empty_obj = json!({});
    let settings = state.get("settings").unwrap_or(&empty_obj);
    let plat_cats = platform_categories(settings);
    let raw_games = state
        .get("games")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let overrides = state
        .get("achievement_overrides")
        .and_then(Value::as_object);
    let mut seen = HashSet::new();
    let mut delta_games = Vec::with_capacity(ids.len());
    let mut fallback_map: Option<HashMap<String, usize>> = None;
    for &id_val in ids {
        let mut found_idx = app.store.get_game_index_by_id(id_val).ok().flatten();
        if found_idx.is_none()
            && let Ok(num) = id_val.parse::<usize>()
            && num < raw_games.len()
        {
            found_idx = Some(num);
        }
        if found_idx.is_none() {
            let map = fallback_map.get_or_insert_with(|| games_by_id_map(raw_games));
            found_idx = map.get(id_val).copied();
        }
        if let Some(idx) = found_idx
            && idx < raw_games.len()
            && seen.insert(idx)
        {
            delta_games.push(project_game(
                app,
                &raw_games[idx],
                idx,
                &plat_cats,
                overrides,
            ));
        }
    }
    Ok(json!({
        "games": delta_games,
        "media_epoch": app.cache.media_epoch(),
    }))
}

pub fn public_state_etag(app: &AppState) -> String {
    let sig = app.store.signature();
    let (mtime, size) = sig.map(|s| (s.mtime_ns, s.size)).unwrap_or((0, 0));
    format!(
        "\"{mtime:x}-{size:x}-{}-{}\"",
        app.cache.media_epoch(),
        app.cache.plugin_epoch()
    )
}

pub fn clean_extras(items: Option<&Value>, command: bool) -> Result<Vec<Value>, ApiError> {
    let Some(val) = items else {
        return Ok(Vec::new());
    };
    let Some(arr) = val.as_array() else {
        return Err(ApiError::bad_request("Game extras must be a list."));
    };
    let mut clean = Vec::new();
    for item in arr.iter().take(100) {
        let Some(obj) = item.as_object() else {
            continue;
        };
        let path = obj
            .get("path")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_owned();
        if path.is_empty() {
            continue;
        }
        let default_name = Path::new(&path)
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_owned();
        let name = obj
            .get("name")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(ToOwned::to_owned)
            .unwrap_or(default_name);
        let mut rec = Map::new();
        rec.insert("name".into(), Value::String(name));
        rec.insert("path".into(), Value::String(path));
        if command {
            let cmd = obj
                .get("command")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim()
                .to_owned();
            rec.insert("command".into(), Value::String(cmd));
        }
        clean.push(Value::Object(rec));
    }
    Ok(clean)
}

pub fn find_game_index_in_state(
    state: &Value,
    payload: &Map<String, Value>,
) -> Result<usize, ApiError> {
    let games = state
        .get("games")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    find_game_index(games, payload)
}

pub fn find_game_index(games: &[Value], payload: &Map<String, Value>) -> Result<usize, ApiError> {
    if let Some(gid) = payload
        .get("game_id")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
    {
        for (i, g) in games.iter().enumerate() {
            if g.get("game_id").and_then(Value::as_str) == Some(gid) {
                return Ok(i);
            }
            if let Some(aliases) = g.get("legacy_game_ids").and_then(Value::as_array)
                && aliases.iter().any(|a| a.as_str() == Some(gid))
            {
                return Ok(i);
            }
        }
        return Err(ApiError::game_not_found("Game not found."));
    }
    if let Some(id_val) = payload.get("id") {
        let idx = id_val
            .as_u64()
            .map(|n| n as usize)
            .or_else(|| id_val.as_str().and_then(|s| s.trim().parse().ok()));
        if let Some(i) = idx
            && i < games.len()
        {
            return Ok(i);
        }
    }
    Err(ApiError::game_not_found("Game not found."))
}

pub fn games_by_id_map(games: &[Value]) -> HashMap<String, usize> {
    let mut map = HashMap::new();
    for (i, g) in games.iter().enumerate() {
        if let Some(gid) = g
            .get("game_id")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
        {
            map.insert(gid.to_owned(), i);
        }
        if let Some(aliases) = g.get("legacy_game_ids").and_then(Value::as_array) {
            for a in aliases {
                if let Some(s) = a.as_str().filter(|s| !s.is_empty()) {
                    map.insert(s.to_owned(), i);
                }
            }
        }
        map.insert(i.to_string(), i);
    }
    map
}
