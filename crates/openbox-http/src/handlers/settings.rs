//! Settings, profiles, performance profiles, and integration token endpoints
//! (`handlers/settings.py` + `handlers/data.py` integration-tokens).

use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::Path;
use std::sync::{Arc, LazyLock};

use axum::http::StatusCode;
use axum::response::Response;
use openbox_core::{ApiError, KNOWN_SETTINGS, PATH_MODES};
use openbox_emulators::defs::valid_core_name;
use openbox_emulators::tracking::TRACKING_MODES;
use openbox_imports::{
    INTEGRATION_PROVIDERS, LIST_COLUMNS_DEFAULT, PROGRESS, clean_currency, clean_tokens,
    custom_field_defs, discover_profiles, masked_tokens, platform_categories, validate_provider,
    validate_token,
};
use openbox_metadata::retroachievements::{
    api_get as ra_api_get, enhanced_ra_profile, game_progress as ra_game_progress,
    inject_retroachievements, load_credentials as load_ra_credentials, match_game as match_ra_game,
    save_credentials as save_ra_credentials, write_ra_game_cache,
};
use openbox_metadata::steam_achievements::{
    credential_status as steam_credential_status, fetch_owned_games, load_steam_credentials,
    merge_steam_stats, save_steam_credentials,
};
use openbox_platform::{IS_WINDOWS, expand_user};
use openbox_runtime::{MEDIA_TYPES_ALL, commands::clean_commands};
use openbox_store::portable_paths::normalize_root;
use regex::Regex;
use serde_json::{Map, Value, json};

use crate::handlers::projection::{
    REGION_PRIORITY_DEFAULT, find_game_index_in_state as find_game_index, public_settings,
};
use crate::handlers::shared::{RequestCtx, str_field};
use crate::middleware::{api_error_response, json_response};

const SIDEBAR_SECTION_IDS: &[&str] = &[
    "search",
    "view",
    "filters",
    "platforms",
    "playlists",
    "explorer",
];
const PROGRESS_VALUES_DEFAULT: &[&str] = &[
    "Unplayed",
    "Playing",
    "Paused",
    "Beaten",
    "Completed",
    "Mastered",
    "Abandoned",
];
const CONTROLLER_ACTIONS: &[&str] = &[
    "play",
    "back",
    "favorite",
    "random",
    "page_left",
    "page_right",
    "pause",
    "menu",
];
const KEYBOARD_ACTIONS: &[&str] = &[
    "play",
    "back",
    "favorite",
    "random",
    "page_left",
    "page_right",
    "pause",
    "menu",
    "search",
    "settings",
    "reader_prev",
    "reader_next",
];
const COVER_MODELS: &[&str] = &[
    "box",
    "jewel",
    "long_jewel",
    "double_jewel",
    "cassette",
    "none",
];
const MODEL_BACKS: &[&str] = &["straight", "angled"];
const MODEL_CASES: &[&str] = &["clear", "solid"];
const MODEL_ROTATION_LOCKS: &[&str] = &["free", "horizontal", "vertical"];
const IMAGE_GROUPS: &[&str] = &[
    "cover",
    "background",
    "screenshot",
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
];
const ALLOWED_BADGES: &[&str] = &[
    "favorite",
    "installed",
    "missing_media",
    "saves",
    "documents",
    "versions",
    "storefront",
    "achievements",
    "highscores",
    "progress",
    "rating",
    "broken",
    "portable",
    "controller",
];
const BUILTIN_PROVIDERS: &[&str] = &["launchbox", "screenscraper", "igdb", "steamgriddb"];

static KEY_SPEC_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^(?:(?:Ctrl|Alt|Shift)\+)*(?:Key[A-Z]|Digit[0-9]|F(?:[1-9]|1[0-2])|Arrow(?:Left|Right|Up|Down)|Page(?:Up|Down)|Home|End|Enter|Escape|Space|Tab|Backspace|[A-Za-z0-9,./;'\[\]\\=-])$")
        .expect("valid key spec regex")
});
static CTRL_ID_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[a-z0-9_-]{1,32}$").expect("valid ctrl id regex"));
static CC_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[a-zA-Z]{2}$").expect("valid country code regex"));

fn safe_int(val: Option<&Value>, default: i64, msg: &str) -> Result<i64, ApiError> {
    match val {
        None | Some(Value::Null) | Some(Value::Array(_)) | Some(Value::Object(_)) => Ok(default),
        Some(Value::Bool(_)) => Err(ApiError::bad_request(msg)),
        Some(Value::Number(n)) => n
            .as_i64()
            .or_else(|| n.as_f64().map(|f| f as i64))
            .ok_or_else(|| ApiError::bad_request(msg)),
        Some(Value::String(s)) => s
            .trim()
            .parse::<i64>()
            .map_err(|_| ApiError::bad_request(msg)),
    }
}

fn safe_float(val: Option<&Value>, default: f64, msg: &str) -> Result<f64, ApiError> {
    match val {
        None | Some(Value::Null) | Some(Value::Array(_)) | Some(Value::Object(_)) => Ok(default),
        Some(Value::Bool(_)) => Err(ApiError::bad_request(msg)),
        Some(Value::Number(n)) => n.as_f64().ok_or_else(|| ApiError::bad_request(msg)),
        Some(Value::String(s)) => s
            .trim()
            .parse::<f64>()
            .map_err(|_| ApiError::bad_request(msg)),
    }
}

fn clean_bool(merged: &Map<String, Value>, key: &str, default: bool) -> Result<bool, ApiError> {
    match merged.get(key) {
        None | Some(Value::Null) => Ok(default),
        Some(Value::Bool(b)) => Ok(*b),
        _ => Err(ApiError::bad_request(format!(
            "{key} must be true or false."
        ))),
    }
}

fn clean_enum(
    merged: &Map<String, Value>,
    key: &str,
    allowed: &[&str],
    default: &str,
) -> Result<String, ApiError> {
    match merged.get(key) {
        None | Some(Value::Null) => Ok(default.to_owned()),
        Some(Value::String(s)) if allowed.contains(&s.as_str()) => Ok(s.clone()),
        _ => {
            let mut choices: Vec<&str> = allowed.to_vec();
            choices.sort_unstable();
            Err(ApiError::bad_request(format!(
                "{key} must be one of: {}.",
                choices.join(", ")
            )))
        }
    }
}

fn clean_bounded_int(
    merged: &Map<String, Value>,
    key: &str,
    default: i64,
    low: i64,
    high: i64,
    msg: &str,
) -> Result<i64, ApiError> {
    let n = safe_int(merged.get(key), default, msg)?;
    if !(low..=high).contains(&n) {
        return Err(ApiError::bad_request(msg));
    }
    Ok(n)
}

fn clean_float_bounded(
    merged: &Map<String, Value>,
    key: &str,
    default: f64,
    low: f64,
    high: f64,
    msg: &str,
) -> Result<f64, ApiError> {
    let n = safe_float(merged.get(key), default, msg)?;
    if !(low..=high).contains(&n) {
        return Err(ApiError::bad_request(msg));
    }
    Ok(n)
}

fn clean_str_list(
    merged: &Map<String, Value>,
    key: &str,
    limit: usize,
    item_limit: usize,
) -> Result<Vec<String>, ApiError> {
    let Some(val) = merged.get(key) else {
        return Ok(Vec::new());
    };
    if val.is_null() {
        return Ok(Vec::new());
    }
    let Some(arr) = val.as_array() else {
        return Err(ApiError::bad_request(format!(
            "{key} must be a list of at most {limit} strings."
        )));
    };
    if arr.len() > limit {
        return Err(ApiError::bad_request(format!(
            "{key} must be a list of at most {limit} strings."
        )));
    }
    let mut cleaned = Vec::new();
    for item in arr {
        let Some(s) = item.as_str() else {
            return Err(ApiError::bad_request(format!(
                "{key} entries must be strings."
            )));
        };
        let text: String = s.trim().chars().take(item_limit).collect();
        if !text.is_empty() && !cleaned.contains(&text) {
            cleaned.push(text);
        }
    }
    Ok(cleaned)
}

fn clean_dict(
    merged: &Map<String, Value>,
    key: &str,
    limit: usize,
) -> Result<Map<String, Value>, ApiError> {
    let Some(val) = merged.get(key) else {
        return Ok(Map::new());
    };
    if val.is_null() {
        return Ok(Map::new());
    }
    let Some(obj) = val.as_object() else {
        return Err(ApiError::bad_request(format!(
            "{key} must be an object with at most {limit} entries."
        )));
    };
    if obj.len() > limit {
        return Err(ApiError::bad_request(format!(
            "{key} must be an object with at most {limit} entries."
        )));
    }
    Ok(obj.clone())
}

pub fn clean_settings(merged: &Map<String, Value>) -> Result<Map<String, Value>, ApiError> {
    let watch_folders = {
        let raw = merged.get("watch_folders");
        let mut clean = Vec::new();
        if let Some(val) = raw {
            let Some(arr) = val.as_array() else {
                return Err(ApiError::bad_request(
                    "Watch folders must be a list of at most 50 paths.",
                ));
            };
            if arr.len() > 50 {
                return Err(ApiError::bad_request(
                    "Watch folders must be a list of at most 50 paths.",
                ));
            }
            for item in arr {
                let s = item.as_str().unwrap_or("");
                let p = expand_user(Path::new(s));
                if !p.is_absolute() || !p.is_dir() {
                    return Err(ApiError::bad_request(format!(
                        "Watch folder does not exist: {}",
                        p.display()
                    )));
                }
                let ps = p.to_string_lossy().into_owned();
                if !clean.contains(&ps) {
                    clean.push(ps);
                }
            }
        }
        clean
    };

    let screensaver_seconds = safe_int(
        merged.get("screensaver_seconds"),
        90,
        "Screensaver delay must be 0 or between 30 and 3600 seconds.",
    )?;
    if screensaver_seconds != 0 && !(30..=3600).contains(&screensaver_seconds) {
        return Err(ApiError::bad_request(
            "Screensaver delay must be 0 or between 30 and 3600 seconds.",
        ));
    }

    let controller_map = {
        let mut clean = Map::new();
        let mut by_button: HashMap<i64, String> = HashMap::new();
        if let Some(val) = merged.get("controller_map") {
            let Some(obj) = val.as_object() else {
                return Err(ApiError::bad_request(
                    "Controller mapping must be an object.",
                ));
            };
            for (action, button) in obj {
                if !CONTROLLER_ACTIONS.contains(&action.as_str()) {
                    return Err(ApiError::bad_request(
                        "Controller button mappings must use buttons 0 through 31.",
                    ));
                }
                if button.is_null() || button.as_str() == Some("") {
                    continue;
                }
                let Some(n) = button.as_i64() else {
                    return Err(ApiError::bad_request(
                        "Controller button mappings must use buttons 0 through 31.",
                    ));
                };
                if !(0..=31).contains(&n) {
                    return Err(ApiError::bad_request(
                        "Controller button mappings must use buttons 0 through 31.",
                    ));
                }
                if let Some(prev) = by_button.insert(n, action.clone()) {
                    return Err(ApiError::bad_request(format!(
                        "Button {n} is assigned to both {prev} and {action}."
                    )));
                }
                clean.insert(action.clone(), json!(n));
            }
        }
        clean
    };

    let controller_types = {
        let default_types = json!([
            {"id": "gamepad", "name": "Gamepad", "kind": "gamepad"},
            {"id": "arcade_stick", "name": "Arcade stick", "kind": "arcade_stick"},
            {"id": "racing_wheel", "name": "Racing wheel", "kind": "racing_wheel"},
            {"id": "light_gun", "name": "Light gun", "kind": "light_gun"},
            {"id": "paddle", "name": "Paddle", "kind": "paddle"},
            {"id": "trackball", "name": "Trackball", "kind": "trackball"},
            {"id": "spinner", "name": "Spinner", "kind": "spinner"},
            {"id": "keyboard", "name": "Keyboard", "kind": "keyboard"},
            {"id": "mouse", "name": "Mouse", "kind": "mouse"},
            {"id": "vr", "name": "VR", "kind": "vr"},
        ]);
        let allowed_kinds = [
            "gamepad",
            "arcade_stick",
            "racing_wheel",
            "light_gun",
            "paddle",
            "trackball",
            "spinner",
            "keyboard",
            "mouse",
            "vr",
        ];
        match merged.get("controller_types") {
            None | Some(Value::Null) => default_types,
            Some(Value::Array(arr)) if arr.is_empty() => default_types,
            Some(Value::Array(arr)) => {
                if arr.len() > 40 {
                    return Err(ApiError::bad_request(
                        "Controller types must be a list of at most 40 entries.",
                    ));
                }
                let mut clean = Vec::new();
                let mut seen = HashSet::new();
                for item in arr {
                    let Some(obj) = item.as_object() else {
                        return Err(ApiError::bad_request("Controller types must be objects."));
                    };
                    let eid = obj.get("id").and_then(Value::as_str).unwrap_or("").trim();
                    if !CTRL_ID_RE.is_match(eid) || !seen.insert(eid.to_owned()) {
                        return Err(ApiError::bad_request(
                            "Controller type ids must be unique lowercase identifiers.",
                        ));
                    }
                    let name: String = obj
                        .get("name")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .trim()
                        .chars()
                        .take(60)
                        .collect();
                    let kind = obj
                        .get("kind")
                        .and_then(Value::as_str)
                        .unwrap_or(eid)
                        .trim();
                    if name.is_empty() || !allowed_kinds.contains(&kind) {
                        return Err(ApiError::bad_request(
                            "Controller type name/kind is invalid.",
                        ));
                    }
                    clean.push(json!({"id": eid, "name": name, "kind": kind}));
                }
                Value::Array(clean)
            }
            _ => {
                return Err(ApiError::bad_request(
                    "Controller types must be a list of at most 40 entries.",
                ));
            }
        }
    };

    let integration_tokens = clean_tokens(merged.get("integration_tokens"))?;

    let emulator_platform_settings = {
        let mut clean = Map::new();
        if let Some(val) = merged.get("emulator_platform_settings")
            && !val.is_null()
        {
            let Some(obj) = val.as_object() else {
                return Err(ApiError::bad_request(
                    "Platform emulator settings must be an object.",
                ));
            };
            if obj.len() > 500 {
                return Err(ApiError::bad_request(
                    "Platform emulator settings must be an object.",
                ));
            }
            for (platform, entry) in obj {
                let pname: String = platform.trim().chars().take(120).collect();
                let Some(eobj) = entry.as_object() else {
                    continue;
                };
                if pname.is_empty() {
                    continue;
                }
                let adapter_id = eobj
                    .get("adapter_id")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .trim();
                if adapter_id.len() > 120 {
                    continue;
                }
                let core = eobj
                    .get("retroarch_core")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .trim();
                if !core.is_empty() && !valid_core_name(core) {
                    return Err(ApiError::bad_request(format!(
                        "Invalid RetroArch core for {pname}."
                    )));
                }
                let extra: String = match eobj.get("extra_args") {
                    Some(Value::Array(arr)) => arr
                        .iter()
                        .filter_map(Value::as_str)
                        .collect::<Vec<_>>()
                        .join(" "),
                    Some(Value::String(s)) => s.clone(),
                    _ => String::new(),
                }
                .trim()
                .chars()
                .take(2000)
                .collect();
                clean.insert(
                    pname,
                    json!({
                        "adapter_id": adapter_id,
                        "retroarch_core": core,
                        "extra_args": extra,
                    }),
                );
            }
        }
        clean
    };

    let cloud_folder = {
        let raw = merged
            .get("cloud_folder")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        if !raw.is_empty() {
            let p = expand_user(Path::new(raw));
            if !p.is_absolute() || !p.is_dir() {
                return Err(ApiError::bad_request(format!(
                    "Cloud sync folder does not exist: {}",
                    p.display()
                )));
            }
            p.to_string_lossy().into_owned()
        } else {
            String::new()
        }
    };

    let startup_commands = clean_commands(merged.get("startup_commands").unwrap_or(&json!([])))
        .map_err(ApiError::bad_request)?;
    let shutdown_commands = clean_commands(merged.get("shutdown_commands").unwrap_or(&json!([])))
        .map_err(ApiError::bad_request)?;
    let bigbox_shutdown_commands =
        clean_commands(merged.get("bigbox_shutdown_commands").unwrap_or(&json!([])))
            .map_err(ApiError::bad_request)?;

    let play_minutes = clean_bounded_int(
        merged,
        "progress_automation_play_minutes",
        30,
        0,
        100_000,
        "Progress automation play minutes must be between 0 and 100000.",
    )?;
    let idle_days = clean_bounded_int(
        merged,
        "progress_automation_idle_days",
        30,
        0,
        3650,
        "Progress automation idle days must be between 0 and 3650.",
    )?;
    let image_group = merged
        .get("image_group")
        .and_then(Value::as_str)
        .unwrap_or("cover")
        .to_owned();
    if !IMAGE_GROUPS.contains(&image_group.as_str()) {
        return Err(ApiError::bad_request("Unknown default image group."));
    }

    let badge_visibility = match merged.get("badge_visibility") {
        None => vec![
            "favorite",
            "installed",
            "saves",
            "documents",
            "progress",
            "storefront",
            "achievements",
            "rating",
        ]
        .into_iter()
        .map(ToOwned::to_owned)
        .collect::<Vec<_>>(),
        Some(Value::Array(arr)) => {
            let mut out = Vec::new();
            for item in arr {
                let Some(s) = item.as_str() else {
                    return Err(ApiError::bad_request(
                        "Badge visibility must contain known badge names.",
                    ));
                };
                if !ALLOWED_BADGES.contains(&s) {
                    return Err(ApiError::bad_request(
                        "Badge visibility must contain known badge names.",
                    ));
                }
                out.push(s.to_owned());
            }
            out
        }
        _ => {
            return Err(ApiError::bad_request(
                "Badge visibility must contain known badge names.",
            ));
        }
    };

    let save_backup_limit = clean_bounded_int(
        merged,
        "save_backup_limit",
        10,
        0,
        500,
        "Save backup limit must be between 0 and 500.",
    )?;
    let media_download_limit = clean_bounded_int(
        merged,
        "media_download_limit",
        0,
        0,
        10_000,
        "Media download limit must be between 0 and 10000.",
    )?;
    let backup_auto_keep = clean_bounded_int(
        merged,
        "backup_auto_keep",
        4,
        1,
        52,
        "Automatic backups kept must be between 1 and 52.",
    )?;
    let save_management_enabled = clean_bool(merged, "save_management_enabled", true)?;
    let save_vault_enabled = clean_bool(merged, "save_vault_enabled", true)?;
    let save_vault_keep = clean_bounded_int(
        merged,
        "save_vault_keep",
        5,
        1,
        50,
        "Save Vault retention must be between 1 and 50 snapshots.",
    )?;
    let save_vault_on_close = clean_bool(merged, "save_vault_on_close", false)?;
    let save_vault_auto_days = clean_bounded_int(
        merged,
        "save_vault_auto_days",
        0,
        0,
        365,
        "Save Vault auto-snapshot interval must be between 0 and 365 days.",
    )?;
    let deals_enabled = clean_bool(merged, "deals_enabled", false)?;
    let deals_poll_hours = clean_bounded_int(
        merged,
        "deals_poll_hours",
        168,
        24,
        720,
        "Deal Radar poll interval must be between 24 and 720 hours.",
    )?;
    let deals_min_drop_pct = clean_bounded_int(
        merged,
        "deals_min_drop_pct",
        10,
        1,
        90,
        "Deal Radar minimum drop percentage must be between 1 and 90.",
    )?;
    let deals_store_cc = match merged.get("deals_store_cc") {
        None | Some(Value::Null) => "us".to_owned(),
        Some(Value::String(s)) if s.is_empty() => "us".to_owned(),
        Some(Value::String(s)) if CC_RE.is_match(s.trim()) => s.trim().to_lowercase(),
        _ => {
            return Err(ApiError::bad_request(
                "Deal Radar country code must be a two-letter store code.",
            ));
        }
    };

    let auto_import_media_types = {
        let allowed: HashSet<&str> = MEDIA_TYPES_ALL.iter().copied().collect();
        let mut out = match merged.get("auto_import_media_types") {
            None => Vec::new(),
            Some(Value::Array(arr)) => {
                let mut v = Vec::new();
                for item in arr {
                    let Some(s) = item.as_str() else {
                        return Err(ApiError::bad_request(
                            "Auto-import media types include an unknown media type.",
                        ));
                    };
                    if !allowed.contains(s) {
                        return Err(ApiError::bad_request(
                            "Auto-import media types include an unknown media type.",
                        ));
                    }
                    v.push(s.to_owned());
                }
                v
            }
            _ => {
                return Err(ApiError::bad_request(
                    "Auto-import media types include an unknown media type.",
                ));
            }
        };
        if out.is_empty() {
            out = vec!["background".into(), "cover".into(), "screenshots".into()];
        }
        out.sort();
        out.dedup();
        out
    };

    let media_download_limit_per_group = clean_bounded_int(
        merged,
        "media_download_limit_per_group",
        0,
        0,
        100,
        "Per-group media download limit must be between 0 and 100.",
    )?;
    let metadata_provider_priority: Vec<String> =
        clean_str_list(merged, "metadata_provider_priority", 20, 80)?
            .into_iter()
            .filter(|s| BUILTIN_PROVIDERS.contains(&s.as_str()) || s.starts_with("plugin:"))
            .collect();
    let plugin_metadata_priority = clean_bool(merged, "plugin_metadata_priority", false)?;

    let memories_import_roots = {
        let mut clean = Vec::new();
        if let Some(val) = merged.get("memories_import_roots")
            && !val.is_null()
            && val.as_str() != Some("")
        {
            let Some(arr) = val.as_array() else {
                return Err(ApiError::bad_request(
                    "Memory roots must be a list of at most 32 paths.",
                ));
            };
            if arr.len() > 32 {
                return Err(ApiError::bad_request(
                    "Memory roots must be a list of at most 32 paths.",
                ));
            }
            for item in arr {
                let s = item.as_str().unwrap_or("").trim();
                if s.is_empty() {
                    continue;
                }
                let p = expand_user(Path::new(s));
                if !p.is_absolute() || !p.is_dir() {
                    return Err(ApiError::bad_request(format!(
                        "Memory root does not exist: {}",
                        p.display()
                    )));
                }
                let ps = p.to_string_lossy().into_owned();
                if !clean.contains(&ps) {
                    clean.push(ps);
                }
            }
        }
        clean
    };

    let state_retention = clean_bounded_int(
        merged,
        "state_retention",
        1,
        1,
        20,
        "Resume state retention must be between 1 and 20.",
    )?;

    let region_priority = match merged.get("region_priority") {
        None => REGION_PRIORITY_DEFAULT
            .iter()
            .map(|s| (*s).to_owned())
            .collect::<Vec<_>>(),
        Some(Value::Array(arr)) if !arr.is_empty() => arr
            .iter()
            .map(|v| v.as_str().unwrap_or("").to_owned())
            .collect(),
        _ => {
            return Err(ApiError::bad_request(
                "Region priority must be a non-empty list.",
            ));
        }
    };
    let video_priority = match merged.get("video_priority") {
        None => vec![
            "video_snap".into(),
            "video_theme".into(),
            "video_trailer".into(),
            "video_recording".into(),
        ],
        Some(Value::Array(arr)) => {
            let mut out = Vec::new();
            for item in arr {
                let Some(s) = item.as_str() else {
                    return Err(ApiError::bad_request("Invalid video priority list."));
                };
                if !matches!(
                    s,
                    "video_snap" | "video_theme" | "video_trailer" | "video_recording" | "video"
                ) {
                    return Err(ApiError::bad_request("Invalid video priority list."));
                }
                out.push(s.to_owned());
            }
            out
        }
        _ => return Err(ApiError::bad_request("Invalid video priority list.")),
    };

    let library_music = merged
        .get("library_music")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_owned();
    if !library_music.is_empty() && !expand_user(Path::new(&library_music)).is_file() {
        return Err(ApiError::bad_request(
            "Library music path must point to an existing audio file.",
        ));
    }

    let currency = clean_currency(merged.get("currency").or(Some(&json!("USD"))))?;
    let font_family = clean_enum(
        merged,
        "font_family",
        &["system", "serif", "mono", "humanist"],
        "system",
    )?;
    let bigbox_mode = merged
        .get("bigbox_mode")
        .and_then(Value::as_str)
        .unwrap_or("stage")
        .to_owned();
    if !matches!(bigbox_mode.as_str(), "stage" | "hybrid" | "coverflow") {
        return Err(ApiError::bad_request(
            "Big Box mode must be stage, hybrid, or coverflow.",
        ));
    }

    let storefront_auto_import = {
        let mut clean = Map::new();
        if let Some(val) = merged.get("storefront_auto_import") {
            let Some(obj) = val.as_object() else {
                return Err(ApiError::bad_request(
                    "Storefront auto-import settings must be an object.",
                ));
            };
            if obj.len() > 20 {
                return Err(ApiError::bad_request(
                    "Storefront auto-import settings may list at most 20 stores.",
                ));
            }
            for (k, raw) in obj {
                let key = k.trim().to_lowercase();
                if key.is_empty()
                    || key.len() > 80
                    || (!matches!(
                        key.as_str(),
                        "steam" | "heroic" | "lutris" | "gameyfin" | "gog" | "ea" | "ubisoft"
                    ) && !key.starts_with("plugin:"))
                {
                    continue;
                }
                match raw {
                    Value::Bool(b) => {
                        clean.insert(
                            key,
                            json!({"enabled": b, "platform": "", "include_uninstalled": false}),
                        );
                    }
                    Value::Object(m) => {
                        let plat = m
                            .get("platform")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .trim();
                        if plat.len() > 80 {
                            return Err(ApiError::bad_request(format!(
                                "Storefront platform for {key} must be 80 characters or fewer."
                            )));
                        }
                        clean.insert(
                            key,
                            json!({
                                "enabled": m.get("enabled").and_then(Value::as_bool).unwrap_or(false),
                                "platform": plat,
                                "include_uninstalled": m.get("include_uninstalled").and_then(Value::as_bool).unwrap_or(false),
                            }),
                        );
                    }
                    _ => {
                        return Err(ApiError::bad_request(format!(
                            "Storefront setting for {key} must be an object or boolean."
                        )));
                    }
                }
            }
        }
        clean
    };

    let obs_recording_path = {
        let raw = merged
            .get("obs_recording_path")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        if !raw.is_empty() {
            let p = expand_user(Path::new(raw));
            if !p.is_absolute() || !p.is_dir() {
                return Err(ApiError::bad_request(format!(
                    "OBS recording folder does not exist: {}",
                    p.display()
                )));
            }
            p.to_string_lossy().into_owned()
        } else {
            String::new()
        }
    };

    let gameyfin_url = {
        let raw = merged
            .get("gameyfin_url")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        if !raw.is_empty() && !raw.starts_with("http://") && !raw.starts_with("https://") {
            format!("http://{raw}")
        } else {
            raw.to_owned()
        }
    };
    let gameyfin_install_dir = {
        let raw = merged
            .get("gameyfin_install_dir")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        if !raw.is_empty() {
            let p = expand_user(Path::new(raw));
            let _ = fs::create_dir_all(&p);
            if !p.is_absolute() || !p.is_dir() {
                return Err(ApiError::bad_request(format!(
                    "Gameyfin install folder is invalid: {}",
                    p.display()
                )));
            }
            p.to_string_lossy().into_owned()
        } else {
            String::new()
        }
    };
    let ludusavi_backup_path = {
        let raw = merged
            .get("ludusavi_backup_path")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        if !raw.is_empty() {
            let p = expand_user(Path::new(raw));
            let _ = fs::create_dir_all(&p);
            p.to_string_lossy().into_owned()
        } else {
            String::new()
        }
    };

    let hidden_sidebar_sections = {
        let mut clean = Vec::new();
        if let Some(val) = merged.get("hidden_sidebar_sections") {
            let Some(arr) = val.as_array() else {
                return Err(ApiError::bad_request(
                    "Hidden sidebar sections must be a list.",
                ));
            };
            for item in arr {
                let s = item.as_str().unwrap_or("").trim();
                if !SIDEBAR_SECTION_IDS.contains(&s) || clean.iter().any(|x| x == s) {
                    return Err(ApiError::bad_request(
                        "Hidden sidebar sections must be known, unique section ids.",
                    ));
                }
                clean.push(s.to_owned());
            }
        }
        clean.truncate(20);
        clean
    };

    let tracking_mode = merged
        .get("tracking_mode")
        .and_then(Value::as_str)
        .unwrap_or("default")
        .trim()
        .to_lowercase();
    if !TRACKING_MODES.contains(&tracking_mode.as_str()) {
        return Err(ApiError::bad_request("Unknown tracking mode."));
    }
    let tracking_delay = clean_bounded_int(
        merged,
        "tracking_delay",
        0,
        0,
        600,
        "Tracking delay must be between 0 and 600 seconds.",
    )?;
    let tracking_frequency = clean_float_bounded(
        merged,
        "tracking_frequency",
        2.0,
        0.5,
        60.0,
        "Tracking frequency must be between 0.5 and 60 seconds.",
    )?;

    let progress_on_first_play = merged
        .get("progress_on_first_play")
        .and_then(Value::as_str)
        .unwrap_or("Playing")
        .trim()
        .to_owned();
    if !progress_on_first_play.is_empty() && !PROGRESS.contains(&progress_on_first_play.as_str()) {
        return Err(ApiError::bad_request(
            "Unknown progress value for first play.",
        ));
    }

    let apply_perf = merged
        .get("apply_perf")
        .and_then(Value::as_str)
        .unwrap_or("auto")
        .trim()
        .to_lowercase();
    if !matches!(apply_perf.as_str(), "off" | "auto" | "always") {
        return Err(ApiError::bad_request(
            "Apply performance limits must be off, auto, or always.",
        ));
    }

    let controller_prompt_hint = match merged.get("controller_prompt_hint") {
        None | Some(Value::Null) => String::new(),
        Some(Value::Bool(true)) => "A Play · B Back · M Menu".to_owned(),
        Some(Value::Bool(false)) => String::new(),
        Some(Value::String(s)) => s.trim().chars().take(200).collect(),
        _ => {
            return Err(ApiError::bad_request(
                "controller_prompt_hint must be a string.",
            ));
        }
    };

    let mood_match_enabled = merged
        .get("mood_match_enabled")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let mood_match_bigbox = mood_match_enabled
        && merged
            .get("mood_match_bigbox")
            .and_then(Value::as_bool)
            .unwrap_or(false);

    let (party_queue, party_players, party_index) = {
        let mut q = Vec::new();
        if let Some(val) = merged.get("party_queue")
            && !val.is_null()
        {
            let Some(arr) = val.as_array() else {
                return Err(ApiError::bad_request(
                    "Party queue must be a list of game ids.",
                ));
            };
            for item in arr {
                let s = item.as_str().unwrap_or("").trim();
                if !s.is_empty() && !q.iter().any(|x| x == s) {
                    q.push(s.to_owned());
                }
            }
            q.truncate(50);
        }
        let players = clean_bounded_int(
            merged,
            "party_players",
            2,
            2,
            8,
            "Party players must be between 2 and 8.",
        )?;
        let mut idx = safe_int(
            merged.get("party_index"),
            0,
            "Party index must be an integer.",
        )?;
        if idx < 0 {
            return Err(ApiError::bad_request("Party index must be >= 0."));
        }
        if q.is_empty() {
            idx = 0;
        } else {
            idx = idx.min((q.len() - 1) as i64);
        }
        (q, players, idx)
    };

    let sidebar_sections = match merged.get("sidebar_sections") {
        None => SIDEBAR_SECTION_IDS
            .iter()
            .map(|s| (*s).to_owned())
            .collect::<Vec<_>>(),
        Some(Value::Null) => Vec::new(),
        Some(_) => clean_str_list(merged, "sidebar_sections", 20, 40)?,
    };

    let progress_values = match merged.get("progress_values") {
        None | Some(Value::Null) => PROGRESS_VALUES_DEFAULT
            .iter()
            .map(|s| (*s).to_owned())
            .collect::<Vec<_>>(),
        Some(Value::Array(arr)) => {
            let mut out = Vec::new();
            for item in arr {
                let text = item.as_str().unwrap_or("").trim();
                if text.is_empty() || !PROGRESS.contains(&text) {
                    return Err(ApiError::bad_request(
                        "Progress values must be a list of known progress labels.",
                    ));
                }
                if !out.iter().any(|x| x == text) {
                    out.push(text.to_owned());
                }
            }
            out
        }
        _ => {
            return Err(ApiError::bad_request(
                "Progress values must be a list of known progress labels.",
            ));
        }
    };

    let keyboard_map = {
        let mut clean = Map::new();
        let mut seen: HashMap<String, String> = HashMap::new();
        if let Some(val) = merged.get("keyboard_map") {
            let Some(obj) = val.as_object() else {
                return Err(ApiError::bad_request("Keyboard mapping must be an object."));
            };
            for (action, spec) in obj {
                if !KEYBOARD_ACTIONS.contains(&action.as_str()) {
                    continue;
                }
                if spec.is_null() || spec.as_str() == Some("") {
                    continue;
                }
                let text = spec.as_str().unwrap_or("").trim();
                if text.len() > 40 || !KEY_SPEC_RE.is_match(text) {
                    return Err(ApiError::bad_request(format!(
                        "Keyboard shortcut for {action} is not a supported key."
                    )));
                }
                let lower = text.to_lowercase();
                if let Some(prev) = seen.insert(lower, action.clone()) {
                    return Err(ApiError::bad_request(format!(
                        "Keyboard shortcut {text} is assigned to both {prev} and {action}."
                    )));
                }
                clean.insert(action.clone(), Value::String(text.to_owned()));
            }
        }
        clean
    };

    let pointer_nav_enabled = clean_bool(merged, "pointer_nav_enabled", true)?;
    let mouse_wheel_nav = clean_bool(merged, "mouse_wheel_nav", true)?;
    let spinner_sensitivity = safe_int(
        merged.get("spinner_sensitivity"),
        6,
        "Spinner sensitivity must be a number between 1 and 20.",
    )?
    .clamp(1, 20);
    let bigbox_wheel_speed = clean_float_bounded(
        merged,
        "bigbox_wheel_speed",
        1.0,
        0.25,
        3.0,
        "Big Box wheel speed must be between 0.25 and 3.0.",
    )?;
    let bigbox_back_hold_seconds = clean_float_bounded(
        merged,
        "bigbox_back_hold_seconds",
        0.8,
        0.0,
        2.0,
        "Big Box back hold must be between 0 and 2 seconds.",
    )?;
    let battery_indicator = clean_bool(merged, "battery_indicator", true)?;
    let bigbox_hidden_platforms = clean_str_list(merged, "bigbox_hidden_platforms", 500, 200)?;
    let bigbox_hidden_categories = clean_str_list(merged, "bigbox_hidden_categories", 500, 200)?;
    let bigbox_hidden_playlists = clean_str_list(merged, "bigbox_hidden_playlists", 500, 200)?;
    let reader_theme = clean_enum(merged, "reader_theme", &["light", "dark"], "light")?;
    let reader_spread = clean_enum(merged, "reader_spread", &["single", "spread"], "single")?;

    let cover_model = clean_enum(merged, "cover_model", COVER_MODELS, "box")?;
    let cover_model_by_platform = clean_dict(merged, "cover_model_by_platform", 200)?;
    for (plat, v) in &cover_model_by_platform {
        if !v.as_str().is_some_and(|s| COVER_MODELS.contains(&s)) {
            return Err(ApiError::bad_request(format!(
                "Unknown cover model for {plat}."
            )));
        }
    }
    let model_rotation_lock =
        clean_enum(merged, "model_rotation_lock", MODEL_ROTATION_LOCKS, "free")?;
    let model_back_by_platform = clean_dict(merged, "model_back_by_platform", 200)?;
    for (plat, v) in &model_back_by_platform {
        if !v.as_str().is_some_and(|s| MODEL_BACKS.contains(&s)) {
            return Err(ApiError::bad_request(format!(
                "Unknown cassette back style for {plat}."
            )));
        }
    }
    let model_case_by_platform = clean_dict(merged, "model_case_by_platform", 200)?;
    for (plat, v) in &model_case_by_platform {
        if !v.as_str().is_some_and(|s| MODEL_CASES.contains(&s)) {
            return Err(ApiError::bad_request(format!(
                "Unknown cassette case style for {plat}."
            )));
        }
    }
    let model_rotation_lock_by_platform =
        clean_dict(merged, "model_rotation_lock_by_platform", 200)?;
    for (plat, v) in &model_rotation_lock_by_platform {
        if !v
            .as_str()
            .is_some_and(|s| MODEL_ROTATION_LOCKS.contains(&s))
        {
            return Err(ApiError::bad_request(format!(
                "Unknown rotation lock for {plat}."
            )));
        }
    }

    let bigbox_video_controls = clean_bool(merged, "bigbox_video_controls", true)?;
    let bigbox_video_volume = clean_float_bounded(
        merged,
        "bigbox_video_volume",
        0.8,
        0.0,
        1.0,
        "Big Box video volume must be between 0 and 1.",
    )?;
    let bigbox_video_backdrop = clean_bool(merged, "bigbox_video_backdrop", false)?;
    let bigbox_startup_video_loop = clean_bool(merged, "bigbox_startup_video_loop", false)?;
    let mut platform_videos = clean_dict(merged, "platform_videos", 200)?;
    let pv_keys: Vec<String> = platform_videos.keys().cloned().collect();
    for k in pv_keys {
        let text = platform_videos
            .get(&k)
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_owned();
        if text.is_empty() {
            platform_videos.remove(&k);
            continue;
        }
        let cand = expand_user(Path::new(&text));
        if !cand.is_absolute() || !cand.is_file() {
            return Err(ApiError::bad_request(format!(
                "Platform video does not exist: {text}"
            )));
        }
        platform_videos.insert(k, Value::String(cand.to_string_lossy().into_owned()));
    }
    let bigbox_sounds_enabled = clean_bool(merged, "bigbox_sounds_enabled", true)?;
    let auto_import_excluded_platforms =
        clean_str_list(merged, "auto_import_excluded_platforms", 100, 80)?;
    let emulator_update_checks = clean_str_list(merged, "emulator_update_checks", 100, 100)?;

    let path_mode = merged
        .get("path_mode")
        .and_then(Value::as_str)
        .unwrap_or("absolute")
        .to_owned();
    if !PATH_MODES.contains(&path_mode.as_str()) {
        return Err(ApiError::bad_request(
            "Library path mode must be absolute or portable.",
        ));
    }
    let raw_root = merged
        .get("library_root")
        .and_then(Value::as_str)
        .unwrap_or("");
    let library_root =
        normalize_root(raw_root, IS_WINDOWS).map_err(|e| ApiError::bad_request(e.to_string()))?;
    if !library_root.is_empty() && !Path::new(&library_root).is_dir() {
        return Err(ApiError::bad_request(format!(
            "Library root folder does not exist: {library_root}"
        )));
    }
    let media_manifest_enabled = clean_bool(merged, "media_manifest_enabled", true)?;
    let ra_hardcore = clean_bool(merged, "ra_hardcore", false)?;

    let obs_ws_timeout = safe_float(
        merged.get("obs_websocket_timeout"),
        5.0,
        "OBS WebSocket timeout must be between 0.1 and 30 seconds.",
    )?
    .clamp(0.1, 30.0);
    let attract_mode_seconds = safe_int(
        merged.get("attract_mode_seconds"),
        if screensaver_seconds > 0 {
            screensaver_seconds
        } else {
            90
        },
        "Attract mode delay must be between 0 and 3600 seconds.",
    )?;

    let health_rescan = match merged
        .get("health_rescan")
        .and_then(Value::as_str)
        .unwrap_or("weekly")
        .trim()
        .to_lowercase()
        .as_str()
    {
        "daily" => "daily",
        "weekly" => "weekly",
        "on_startup" => "on_startup",
        "off" => "off",
        _ => "weekly",
    };

    let merged_val = Value::Object(merged.clone());
    let mut out = Map::new();
    let entries = json!({
        "watch_folders": watch_folders,
        "screensaver_seconds": screensaver_seconds,
        "controller_map": Value::Object(controller_map),
        "controller_types": controller_types,
        "integration_tokens": Value::Object(integration_tokens),
        "emulator_platform_settings": Value::Object(emulator_platform_settings),
        "badge_visibility": badge_visibility,
        "cloud_folder": cloud_folder,
        "library_sync_enabled": merged.get("library_sync_enabled").and_then(Value::as_bool).unwrap_or(false),
        "startup_commands": startup_commands,
        "shutdown_commands": shutdown_commands,
        "track_session_history": merged.get("track_session_history").and_then(Value::as_bool).unwrap_or(true),
        "session_recap_enabled": merged.get("session_recap_enabled").and_then(Value::as_bool).unwrap_or(true),
        "quick_resume_enabled": merged.get("quick_resume_enabled").and_then(Value::as_bool).unwrap_or(true),
        "moments_autocapture": merged.get("moments_autocapture").and_then(Value::as_bool).unwrap_or(true),
        "ra_hardcore": ra_hardcore,
        "state_retention": state_retention,
        "backup_on_close": merged.get("backup_on_close").and_then(Value::as_bool).unwrap_or(false),
        "backup_auto_enabled": merged.get("backup_auto_enabled").and_then(Value::as_bool).unwrap_or(false),
        "backup_auto_keep": backup_auto_keep,
        "save_backup_limit": save_backup_limit,
        "save_management_enabled": save_management_enabled,
        "save_vault_enabled": save_vault_enabled,
        "save_vault_keep": save_vault_keep,
        "save_vault_on_close": save_vault_on_close,
        "save_vault_auto_days": save_vault_auto_days,
        "deals_enabled": deals_enabled,
        "deals_poll_hours": deals_poll_hours,
        "deals_min_drop_pct": deals_min_drop_pct,
        "deals_store_cc": deals_store_cc,
        "progress_automation_enabled": merged.get("progress_automation_enabled").and_then(Value::as_bool).unwrap_or(false),
        "progress_automation_play_minutes": play_minutes,
        "progress_automation_idle_days": idle_days,
        "welcome_completed": merged.get("welcome_completed").and_then(Value::as_bool).unwrap_or(false),
        "image_group": image_group,
        "auto_import_media_types": auto_import_media_types,
        "media_download_limit": media_download_limit,
        "media_download_limit_per_group": media_download_limit_per_group,
        "metadata_provider_priority": metadata_provider_priority,
        "plugin_metadata_priority": plugin_metadata_priority,
        "memories_import_enabled": merged.get("memories_import_enabled").and_then(Value::as_bool).unwrap_or(false),
        "memories_import_roots": memories_import_roots,
        "steamgrid_enabled": merged.get("steamgrid_enabled").and_then(Value::as_bool).unwrap_or(true),
        "region_priority": region_priority,
        "video_priority": video_priority,
        "library_music": library_music,
        "video_bgm_mix": merged.get("video_bgm_mix").and_then(Value::as_bool).unwrap_or(false),
        "bigbox_mode": bigbox_mode,
        "bigbox_start_at_launch": merged.get("bigbox_start_at_launch").and_then(Value::as_bool).unwrap_or(false),
        "show_playlist_actions": merged.get("show_playlist_actions").and_then(Value::as_bool).unwrap_or(true),
        "show_insights": merged.get("show_insights").and_then(Value::as_bool).unwrap_or(true),
        "show_launch_badges": merged.get("show_launch_badges").and_then(Value::as_bool).unwrap_or(true),
        "hidden_sidebar_sections": hidden_sidebar_sections,
        "storefront_auto_import": Value::Object(storefront_auto_import),
        "auto_import_excluded_platforms": auto_import_excluded_platforms,
        "emulator_update_checks": emulator_update_checks,
        "path_mode": path_mode,
        "library_root": library_root,
        "media_manifest_enabled": media_manifest_enabled,
        "obs_auto_attach": merged.get("obs_auto_attach").and_then(Value::as_bool).unwrap_or(true),
        "obs_recording_path": obs_recording_path,
        "obs_replay_enabled": merged.get("obs_replay_enabled").and_then(Value::as_bool).unwrap_or(false),
        "obs_websocket_url": merged.get("obs_websocket_url").and_then(Value::as_str).unwrap_or("").trim().chars().take(256).collect::<String>(),
        "obs_websocket_password": merged.get("obs_websocket_password").and_then(Value::as_str).unwrap_or("").chars().take(256).collect::<String>(),
        "obs_websocket_timeout": obs_ws_timeout,
        "dynamic_play_button": merged.get("dynamic_play_button").and_then(Value::as_bool).unwrap_or(true),
        "custom_field_defs": custom_field_defs(&merged_val),
        "platform_categories": platform_categories(&merged_val),
        "list_columns": merged.get("list_columns").and_then(Value::as_array).map(|a| a.iter().take(12).cloned().collect::<Vec<_>>()).unwrap_or_else(|| LIST_COLUMNS_DEFAULT.iter().map(|s| json!(s)).collect()),
        "list_sort": merged.get("list_sort").and_then(Value::as_str).unwrap_or("title").trim().chars().take(20).collect::<String>(),
        "list_sort_dir": merged.get("list_sort_dir").and_then(Value::as_str).unwrap_or("").trim().chars().take(10).collect::<String>(),
        "library_view": merged.get("library_view").and_then(Value::as_str).unwrap_or("grid"),
        "cover_grouping": merged.get("cover_grouping").and_then(Value::as_str).unwrap_or("shape"),
        "locale": merged.get("locale").and_then(Value::as_str).unwrap_or("en").chars().take(5).collect::<String>(),
        "attract_mode_seconds": attract_mode_seconds,
        "bigbox_startup_video": merged.get("bigbox_startup_video").and_then(Value::as_str).unwrap_or("").trim(),
        "bigbox_shutdown_commands": bigbox_shutdown_commands,
        "tray_enabled": merged.get("tray_enabled").and_then(Value::as_bool).unwrap_or(false),
        "minimize_to_tray": merged.get("minimize_to_tray").and_then(Value::as_bool).unwrap_or(false),
        "gameyfin_url": gameyfin_url,
        "gameyfin_username": merged.get("gameyfin_username").and_then(Value::as_str).unwrap_or("").trim(),
        "gameyfin_password": merged.get("gameyfin_password").and_then(Value::as_str).unwrap_or("").trim(),
        "gameyfin_install_dir": gameyfin_install_dir,
        "gameyfin_provider": merged.get("gameyfin_provider").and_then(Value::as_str).unwrap_or("").trim(),
        "ludusavi_backup_path": ludusavi_backup_path,
        "tracking_mode": tracking_mode,
        "tracking_delay": tracking_delay,
        "tracking_frequency": tracking_frequency,
        "tracking_process_name": merged.get("tracking_process_name").and_then(Value::as_str).unwrap_or("").trim().chars().take(100).collect::<String>(),
        "sidebar_sections": sidebar_sections,
        "progress_values": progress_values,
        "keyboard_map": Value::Object(keyboard_map),
        "pointer_nav_enabled": pointer_nav_enabled,
        "mouse_wheel_nav": mouse_wheel_nav,
        "spinner_sensitivity": spinner_sensitivity,
        "bigbox_wheel_speed": bigbox_wheel_speed,
        "bigbox_back_hold_seconds": bigbox_back_hold_seconds,
        "battery_indicator": battery_indicator,
        "bigbox_hidden_platforms": bigbox_hidden_platforms,
        "bigbox_hidden_categories": bigbox_hidden_categories,
        "bigbox_hidden_playlists": bigbox_hidden_playlists,
        "reader_theme": reader_theme,
        "reader_spread": reader_spread,
        "cover_model": cover_model,
        "cover_model_by_platform": Value::Object(cover_model_by_platform),
        "model_rotation_lock": model_rotation_lock,
        "model_back_by_platform": Value::Object(model_back_by_platform),
        "model_case_by_platform": Value::Object(model_case_by_platform),
        "model_rotation_lock_by_platform": Value::Object(model_rotation_lock_by_platform),
        "bigbox_video_controls": bigbox_video_controls,
        "bigbox_video_volume": bigbox_video_volume,
        "bigbox_video_backdrop": bigbox_video_backdrop,
        "bigbox_startup_video_loop": bigbox_startup_video_loop,
        "platform_videos": Value::Object(platform_videos),
        "bigbox_sounds_enabled": bigbox_sounds_enabled,
        "controller_prompt_hint": controller_prompt_hint,
        "controller_prompt_pack": merged.get("controller_prompt_pack").and_then(Value::as_str).unwrap_or("xbox").trim().chars().take(20).collect::<String>(),
        "apply_perf": apply_perf,
        "progress_on_first_play": progress_on_first_play,
        "backlog_progress_suggest": merged.get("backlog_progress_suggest").and_then(Value::as_bool).unwrap_or(true),
        "auto_close_store_clients": merged.get("auto_close_store_clients").and_then(Value::as_bool).unwrap_or(false),
        "gamescope_preset": merged.get("gamescope_preset").and_then(Value::as_str).unwrap_or("").trim().chars().take(30).collect::<String>(),
        "gamescope_custom_presets": merged.get("gamescope_custom_presets").cloned().unwrap_or_else(|| json!([])),
        "mangohud_enabled": merged.get("mangohud_enabled").and_then(Value::as_bool).unwrap_or(false),
        "mood_match_enabled": mood_match_enabled,
        "mood_match_bigbox": mood_match_bigbox,
        "party_queue": party_queue,
        "party_players": party_players,
        "party_index": party_index,
        "currency": currency,
        "font_family": font_family,
        "household_stats_sharing": merged.get("household_stats_sharing").and_then(Value::as_bool).unwrap_or(false),
        "museum_kiosk_enabled": merged.get("museum_kiosk_enabled").and_then(Value::as_bool).unwrap_or(false),
        "museum_kiosk_pin_hash": merged.get("museum_kiosk_pin_hash").and_then(Value::as_str).unwrap_or("").chars().take(512).collect::<String>(),
        "health_rescan": health_rescan,
    });
    if let Value::Object(m) = entries {
        out = m;
    }
    Ok(out)
}

pub fn handle_get(ctx: &RequestCtx<'_>) -> Option<Response> {
    match ctx.path {
        "/api/v2/settings" => match ctx.app.load_state_view() {
            Ok(state) => {
                let pub_s = public_settings(&ctx.app, &state);
                Some(json_response(StatusCode::OK, &pub_s, ctx.instance))
            }
            Err(err) => Some(api_error_response(
                &ApiError::state_unavailable(err.to_string()),
                ctx.request_id,
                ctx.instance,
            )),
        },
        "/api/v2/profiles" => match ctx.app.load_state_view() {
            Ok(state) => {
                let detected = discover_profiles(&ctx.app.root_dir, &ctx.app.data_dir);
                Some(json_response(
                    StatusCode::OK,
                    &json!({
                        "profiles": state.get("profiles").cloned().unwrap_or_else(|| json!({})),
                        "detected": detected,
                    }),
                    ctx.instance,
                ))
            }
            Err(err) => Some(api_error_response(
                &ApiError::state_unavailable(err.to_string()),
                ctx.request_id,
                ctx.instance,
            )),
        },
        "/api/v2/perf_profiles" => match ctx.app.load_state_view() {
            Ok(state) => Some(json_response(
                StatusCode::OK,
                &json!({
                    "perf_profiles": state.get("perf_profiles").cloned().unwrap_or_else(|| json!({})),
                }),
                ctx.instance,
            )),
            Err(err) => Some(api_error_response(
                &ApiError::state_unavailable(err.to_string()),
                ctx.request_id,
                ctx.instance,
            )),
        },
        "/api/v2/integration-tokens" => match ctx.app.load_state_view() {
            Ok(state) => {
                let toks = state
                    .get("settings")
                    .and_then(|s| s.get("integration_tokens"))
                    .and_then(Value::as_object)
                    .cloned()
                    .unwrap_or_default();
                Some(json_response(
                    StatusCode::OK,
                    &json!({
                        "providers": INTEGRATION_PROVIDERS,
                        "tokens": Value::Object(masked_tokens(&toks)),
                    }),
                    ctx.instance,
                ))
            }
            Err(err) => Some(api_error_response(
                &ApiError::state_unavailable(err.to_string()),
                ctx.request_id,
                ctx.instance,
            )),
        },
        "/api/v2/ra/settings" => {
            let Some(creds) = load_ra_credentials(&ctx.app.data_dir) else {
                return Some(json_response(
                    StatusCode::OK,
                    &json!({"configured": false}),
                    ctx.instance,
                ));
            };
            match ra_api_get(
                "API_GetUserProfile.php",
                &[("u", creds.username.clone())],
                &creds,
            ) {
                Ok(profile) => Some(json_response(
                    StatusCode::OK,
                    &json!({
                        "configured": true,
                        "username": profile.get("User").and_then(Value::as_str).unwrap_or(&creds.username),
                        "points": profile.get("TotalPoints").and_then(Value::as_i64).unwrap_or(0),
                        "motto": profile.get("Motto").and_then(Value::as_str).unwrap_or(""),
                    }),
                    ctx.instance,
                )),
                Err(e) => Some(api_error_response(
                    &ApiError::bad_request(e),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/integrations/steam" => {
            let creds = load_steam_credentials(&ctx.app.data_dir);
            Some(json_response(
                StatusCode::OK,
                &steam_credential_status(&creds),
                ctx.instance,
            ))
        }
        _ => None,
    }
}

pub fn handle_post(ctx: &RequestCtx<'_>, payload: &Map<String, Value>) -> Option<Response> {
    match ctx.path {
        "/api/v2/settings" => {
            let current = match ctx.app.load_state() {
                Ok(s) => s,
                Err(err) => {
                    return Some(api_error_response(
                        &ApiError::state_unavailable(err.to_string()),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let existing = current
                .get("settings")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            let mut merged = existing.clone();
            for (k, v) in payload {
                if matches!(k.as_str(), "gameyfin_password" | "obs_websocket_password")
                    && v.as_str().unwrap_or("").trim().is_empty()
                {
                    continue;
                }
                merged.insert(k.clone(), v.clone());
            }
            let normalized = match clean_settings(&merged) {
                Ok(n) => n,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let old_pm = existing
                .get("path_mode")
                .and_then(Value::as_str)
                .unwrap_or("absolute");
            let old_lr = existing
                .get("library_root")
                .and_then(Value::as_str)
                .unwrap_or("");
            let new_pm = normalized
                .get("path_mode")
                .and_then(Value::as_str)
                .unwrap_or("absolute");
            let new_lr = normalized
                .get("library_root")
                .and_then(Value::as_str)
                .unwrap_or("");
            if (old_pm, old_lr) != (new_pm, new_lr) {
                return Some(api_error_response(
                    &ApiError::bad_request(
                        "Use Move library to change the library folder or path mode; it previews the change first.",
                    ),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let known: HashSet<&str> = KNOWN_SETTINGS.iter().copied().collect();
            let incoming: HashSet<String> = payload
                .iter()
                .filter(|(k, v)| {
                    known.contains(k.as_str())
                        && (!matches!(k.as_str(), "gameyfin_password" | "obs_websocket_password")
                            || !v.as_str().unwrap_or("").trim().is_empty())
                })
                .map(|(k, _)| k.clone())
                .collect();
            match ctx.app.transact_state(|state| {
                let settings = state.as_object_mut().and_then(|o| {
                    o.entry("settings".to_owned())
                        .or_insert_with(|| Value::Object(Map::new()))
                        .as_object_mut()
                });
                if let Some(s) = settings {
                    for (k, v) in &normalized {
                        if incoming.contains(k)
                            || !s.contains_key(k)
                            || k == "storefront_auto_import"
                        {
                            s.insert(k.clone(), v.clone());
                        }
                    }
                }
                if openbox_sync::library_sync::sync_enabled(state) {
                    let _ = openbox_sync::library_sync::bootstrap_local_catalog(state, None, None);
                }
                Ok(())
            }) {
                Ok((committed, _)) => {
                    let pub_s = public_settings(&ctx.app, &committed);
                    Some(json_response(StatusCode::OK, &pub_s, ctx.instance))
                }
                Err(err) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/profiles" => {
            let Some(profiles) = payload.get("profiles").and_then(Value::as_object) else {
                return Some(api_error_response(
                    &ApiError::bad_request("Profiles must be an object."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let mut clean = Map::new();
            for (k, v) in profiles {
                let p = k.trim();
                let cmd = v.as_str().unwrap_or("").trim();
                if !p.is_empty() && !cmd.is_empty() {
                    clean.insert(p.to_owned(), Value::String(cmd.to_owned()));
                }
            }
            let saved = clean.len();
            match ctx.app.transact_state(|state| {
                if let Some(obj) = state.as_object_mut() {
                    obj.insert("profiles".into(), Value::Object(clean.clone()));
                }
                Ok(())
            }) {
                Ok(_) => Some(json_response(
                    StatusCode::OK,
                    &json!({"saved": saved}),
                    ctx.instance,
                )),
                Err(err) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/perf_profiles" => {
            let Some(raw) = payload.get("perf_profiles").and_then(Value::as_object) else {
                return Some(api_error_response(
                    &ApiError::bad_request("Performance profiles must be an object."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let mut clean = Map::new();
            for (name, entry) in raw {
                let key = name.trim();
                let Some(eobj) = entry.as_object() else {
                    continue;
                };
                if key.is_empty() {
                    continue;
                }
                let tdp = match safe_float(
                    eobj.get("tdp_w"),
                    0.0,
                    &format!("Invalid TDP value for profile {key}."),
                ) {
                    Ok(v) => v.max(0.0),
                    Err(err) => {
                        return Some(api_error_response(&err, ctx.request_id, ctx.instance));
                    }
                };
                let restore = match safe_float(
                    eobj.get("restore_tdp_w"),
                    0.0,
                    &format!("Invalid TDP value for profile {key}."),
                ) {
                    Ok(v) => v.max(0.0),
                    Err(err) => {
                        return Some(api_error_response(&err, ctx.request_id, ctx.instance));
                    }
                };
                if tdp > 0.0 || restore > 0.0 {
                    clean.insert(
                        key.to_owned(),
                        json!({
                            "enabled": eobj.get("enabled").and_then(Value::as_bool).unwrap_or(false),
                            "tdp_w": tdp,
                            "restore_tdp_w": restore,
                        }),
                    );
                }
            }
            let saved = clean.len();
            match ctx.app.transact_state(|state| {
                if let Some(obj) = state.as_object_mut() {
                    obj.insert("perf_profiles".into(), Value::Object(clean.clone()));
                }
                Ok(())
            }) {
                Ok(_) => Some(json_response(
                    StatusCode::OK,
                    &json!({"saved": saved}),
                    ctx.instance,
                )),
                Err(err) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/integration-tokens" => {
            let provider = match validate_provider(str_field(payload, "provider")) {
                Ok(p) => p,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let token = match validate_token(str_field(payload, "token")) {
                Ok(t) => t,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let p_clone = provider.clone();
            match ctx.app.transact_state(|state| {
                if let Some(obj) = state.as_object_mut() {
                    let s = obj
                        .entry("settings".to_owned())
                        .or_insert_with(|| Value::Object(Map::new()));
                    if let Some(sm) = s.as_object_mut() {
                        let t = sm
                            .entry("integration_tokens".to_owned())
                            .or_insert_with(|| Value::Object(Map::new()));
                        if let Some(tm) = t.as_object_mut() {
                            tm.insert(p_clone.clone(), Value::String(token.clone()));
                        }
                    }
                }
                Ok(())
            }) {
                Ok(_) => Some(json_response(
                    StatusCode::OK,
                    &json!({"provider": provider, "saved": true}),
                    ctx.instance,
                )),
                Err(err) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/integration-tokens/remove" => {
            let provider = match validate_provider(str_field(payload, "provider")) {
                Ok(p) => p,
                Err(err) => return Some(api_error_response(&err, ctx.request_id, ctx.instance)),
            };
            let p_clone = provider.clone();
            match ctx.app.transact_state(|state| {
                if let Some(tm) = state
                    .get_mut("settings")
                    .and_then(|s| s.get_mut("integration_tokens"))
                    .and_then(Value::as_object_mut)
                {
                    tm.remove(&p_clone);
                }
                Ok(())
            }) {
                Ok(_) => Some(json_response(
                    StatusCode::OK,
                    &json!({"provider": provider, "removed": true}),
                    ctx.instance,
                )),
                Err(err) => Some(api_error_response(
                    &ApiError::bad_request(err.to_string()),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/integrations/steam" => {
            let api_key = payload.get("api_key").map(|v| v.as_str().unwrap_or(""));
            let steam_id = payload.get("steam_id").map(|v| v.as_str().unwrap_or(""));
            match save_steam_credentials(&ctx.app.data_dir, api_key, steam_id) {
                Ok(st) => Some(json_response(StatusCode::OK, &st, ctx.instance)),
                Err(e) => Some(api_error_response(
                    &ApiError::bad_request(e),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/integrations/steam/test" => {
            let creds = load_steam_credentials(&ctx.app.data_dir);
            if creds.api_key.is_empty() || creds.steam_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("Save a Steam API key and Steam ID first."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            match fetch_owned_games(&creds.steam_id, &creds.api_key) {
                Ok(owned) => Some(json_response(
                    StatusCode::OK,
                    &json!({"ok": true, "games": owned.len()}),
                    ctx.instance,
                )),
                Err(e) => Some(api_error_response(
                    &ApiError::bad_request(e),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/integrations/steam/refresh" => {
            let creds = load_steam_credentials(&ctx.app.data_dir);
            if creds.api_key.is_empty() || creds.steam_id.is_empty() {
                return Some(api_error_response(
                    &ApiError::bad_request("Save a Steam API key and Steam ID first."),
                    ctx.request_id,
                    ctx.instance,
                ));
            }
            let app = Arc::clone(&ctx.app);
            let job = match ctx
                .app
                .jobs
                .submit("steam.stats", false, None, None, move |_ctx| {
                    let owned = fetch_owned_games(&creds.steam_id, &creds.api_key)?;
                    let owned_count = owned.len();
                    let (_, updated_games) = app
                        .transact_state(|st| {
                            let cnt = if let Some(gs) =
                                st.get_mut("games").and_then(Value::as_array_mut)
                            {
                                merge_steam_stats(gs, &owned)
                            } else {
                                0
                            };
                            Ok(cnt)
                        })
                        .map_err(|e| e.to_string())?;
                    Ok(json!({"games": updated_games, "owned": owned_count}))
                }) {
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
                &json!({"state": "queued", "job_id": job.get("job_id").cloned().unwrap_or(Value::Null)}),
                ctx.instance,
            ))
        }
        "/api/v2/ra/settings" => {
            let existing = load_ra_credentials(&ctx.app.data_dir);
            let username = payload
                .get("username")
                .and_then(Value::as_str)
                .unwrap_or("");
            let api_key_in = payload.get("api_key").and_then(Value::as_str).unwrap_or("");
            let api_key = if !api_key_in.is_empty() {
                api_key_in
            } else {
                existing.as_ref().map(|c| c.api_key.as_str()).unwrap_or("")
            };
            match save_ra_credentials(&ctx.app.data_dir, username, api_key) {
                Ok(profile) => Some(json_response(
                    StatusCode::OK,
                    &json!({
                        "configured": true,
                        "username": profile.get("User").and_then(Value::as_str).unwrap_or(""),
                        "points": profile.get("TotalPoints").and_then(Value::as_i64).unwrap_or(0),
                        "motto": profile.get("Motto").and_then(Value::as_str).unwrap_or(""),
                    }),
                    ctx.instance,
                )),
                Err(e) => Some(api_error_response(
                    &ApiError::bad_request(e),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/ra/inject" => {
            let Some(creds) = load_ra_credentials(&ctx.app.data_dir) else {
                return Some(api_error_response(
                    &ApiError::bad_request("Configure RetroAchievements first."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let hardcore = ctx
                .app
                .load_state_view()
                .ok()
                .and_then(|s| s.get("settings")?.get("ra_hardcore")?.as_bool())
                .unwrap_or(false);
            match inject_retroachievements(&creds, hardcore) {
                Ok(res) => Some(json_response(StatusCode::OK, &res, ctx.instance)),
                Err(e) => Some(api_error_response(
                    &ApiError::bad_request(e),
                    ctx.request_id,
                    ctx.instance,
                )),
            }
        }
        "/api/v2/ra/game" => {
            let Some(creds) = load_ra_credentials(&ctx.app.data_dir) else {
                return Some(api_error_response(
                    &ApiError::bad_request("Configure RetroAchievements first."),
                    ctx.request_id,
                    ctx.instance,
                ));
            };
            let state = match ctx.app.load_state() {
                Ok(s) => s,
                Err(e) => {
                    return Some(api_error_response(
                        &ApiError::state_unavailable(e.to_string()),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let idx = match find_game_index(&state, payload) {
                Ok(i) => i,
                Err(e) => return Some(api_error_response(&e, ctx.request_id, ctx.instance)),
            };
            let game = state["games"][idx].clone();
            let stable_gid = game
                .get("game_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            let ra_cache_dir = ctx.app.data_dir.join("cache").join("retroachievements");
            let (ra_id, digest) = match match_ra_game(&game, &creds, &ra_cache_dir) {
                Ok(pair) => pair,
                Err(e) => {
                    return Some(api_error_response(
                        &ApiError::bad_request(e),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            let _ = ctx.app.transact_state(|st| {
                if let Some(gs) = st.get_mut("games").and_then(Value::as_array_mut)
                    && let Some(t) = gs
                        .iter_mut()
                        .find(|x| x.get("game_id").and_then(Value::as_str) == Some(&stable_gid))
                    && let Some(m) = t.as_object_mut()
                {
                    m.insert("ra_game_id".into(), Value::String(ra_id.to_string()));
                    m.insert("ra_hash".into(), Value::String(digest.clone()));
                }
                Ok(())
            });
            let mut progress = match ra_game_progress(ra_id, &creds) {
                Ok(p) => p,
                Err(e) => {
                    return Some(api_error_response(
                        &ApiError::bad_request(e),
                        ctx.request_id,
                        ctx.instance,
                    ));
                }
            };
            progress["game_id"] = json!(ra_id);
            let enhanced = enhanced_ra_profile(&progress, &creds);
            write_ra_game_cache(&ctx.app.data_dir, &stable_gid, &enhanced);
            ctx.app.invalidate_caches();
            Some(json_response(StatusCode::OK, &enhanced, ctx.instance))
        }
        _ => None,
    }
}
