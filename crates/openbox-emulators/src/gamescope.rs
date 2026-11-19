//! Portable Steam gamescope guest helpers and MangoHud / gamescope preset wrappers.
//! Mirrors `pkg/parity/parity_gamescope.py`.

use std::env;

use openbox_platform::{IS_LINUX, which};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

pub const OPENBOX_STEAM_GAME_ID: u64 = 413_091_001;

pub fn is_gamescope_guest(force: bool) -> bool {
    if force {
        return true;
    }
    if env::var("GAMESCOPE_WAYLAND_DISPLAY").is_ok_and(|v| !v.trim().is_empty()) {
        return true;
    }
    if env::var("STEAM_GAMESCOPE_RESTRICTED").is_ok_and(|v| !v.trim().is_empty()) {
        return true;
    }
    let desktop = format!(
        "{} {} {}",
        env::var("XDG_CURRENT_DESKTOP").unwrap_or_default(),
        env::var("XDG_SESSION_DESKTOP").unwrap_or_default(),
        env::var("DESKTOP_SESSION").unwrap_or_default(),
    )
    .to_ascii_lowercase();
    desktop.contains("gamescope")
}

pub fn should_nest_gamescope(force: bool) -> bool {
    !is_gamescope_guest(force)
}

pub fn is_mangohud_available() -> bool {
    IS_LINUX && which("mangohud").is_some()
}

pub fn is_gamescope_available() -> bool {
    IS_LINUX && which("gamescope").is_some()
}

pub fn builtin_presets() -> Vec<Value> {
    vec![
        json!({"id": "720p_60", "label": "720p @ 60 Hz", "args": ["-W", "1280", "-H", "720", "-r", "60", "-f"]}),
        json!({"id": "1080p_60", "label": "1080p @ 60 Hz", "args": ["-W", "1920", "-H", "1080", "-r", "60", "-f"]}),
        json!({"id": "1440p_60", "label": "1440p @ 60 Hz", "args": ["-W", "2560", "-H", "1440", "-r", "60", "-f"]}),
        json!({"id": "4k_60", "label": "4K @ 60 Hz", "args": ["-W", "3840", "-H", "2160", "-r", "60", "-f"]}),
        json!({"id": "steamdeck", "label": "Steam Deck (800p)", "args": ["-W", "1280", "-H", "800", "-r", "60", "-f"]}),
        json!({"id": "crt_4_3", "label": "CRT 4:3 (1080p integer)", "args": ["-w", "640", "-h", "480", "-W", "1920", "-H", "1080", "-S", "integer", "-f"]}),
    ]
}

pub fn list_gamescope_presets(custom: Option<&Value>) -> Vec<Value> {
    let mut presets = builtin_presets();
    if let Some(arr) = custom.and_then(Value::as_array) {
        for item in arr {
            if item.is_object() {
                presets.push(item.clone());
            }
        }
    }
    presets
}

pub fn steam_game_id_for(game: &Value) -> u64 {
    if let Some(raw) = game.get("steam_app_id").and_then(Value::as_str)
        && let Ok(id) = raw.trim().parse::<u64>()
        && id > 0
    {
        return id;
    }
    let key = format!(
        "{}|{}|{}",
        game.get("platform").and_then(Value::as_str).unwrap_or(""),
        game.get("name").and_then(Value::as_str).unwrap_or(""),
        game.get("path").and_then(Value::as_str).unwrap_or(""),
    );
    let digest = Sha256::digest(key.as_bytes());
    let n = u32::from_be_bytes([digest[0], digest[1], digest[2], digest[3]]) & 0x7FFF_FFFF;
    u64::from(n.max(1000))
}

pub fn wrap_launch_command(args: &[String], game: &Value, settings: &Value) -> Vec<String> {
    let mut wrapped = args.to_vec();
    if !IS_LINUX || wrapped.is_empty() {
        return wrapped;
    }
    let mangohud = settings
        .get("mangohud_enabled")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if mangohud && is_mangohud_available() && wrapped[0] != "mangohud" {
        wrapped.insert(0, "mangohud".to_owned());
    }
    let preset_id = game
        .get("gamescope_preset")
        .and_then(Value::as_str)
        .filter(|s| !s.trim().is_empty())
        .or_else(|| {
            settings
                .get("gamescope_preset")
                .and_then(Value::as_str)
                .filter(|s| !s.trim().is_empty())
        })
        .unwrap_or("");
    if !preset_id.is_empty()
        && should_nest_gamescope(false)
        && let Some(gs_bin) = which("gamescope")
    {
        let presets = list_gamescope_presets(settings.get("gamescope_custom_presets"));
        if let Some(preset) = presets
            .iter()
            .find(|p| p.get("id").and_then(Value::as_str) == Some(preset_id))
        {
            let mut prefix = vec![gs_bin.to_string_lossy().into_owned()];
            if let Some(gs_args) = preset.get("args").and_then(Value::as_array) {
                for a in gs_args {
                    if let Some(s) = a.as_str() {
                        prefix.push(s.to_owned());
                    }
                }
            }
            prefix.push("--".to_owned());
            prefix.extend(wrapped);
            wrapped = prefix;
        }
    }
    wrapped
}
