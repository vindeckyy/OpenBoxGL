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

pub const GAMESCOPE_PRESETS: &[(&str, &str, &[&str])] = &[
    (
        "deck",
        "Steam Deck",
        &[
            "-W",
            "1280",
            "-H",
            "800",
            "-w",
            "1280",
            "-h",
            "800",
            "-F",
            "fsr",
            "--fsr-sharpness",
            "2",
        ],
    ),
    (
        "deck_hd",
        "Steam Deck (HD)",
        &[
            "-W",
            "1280",
            "-H",
            "800",
            "-w",
            "1920",
            "-h",
            "1080",
            "-F",
            "fsr",
            "--fsr-sharpness",
            "2",
        ],
    ),
    (
        "1080p",
        "1080p Full HD",
        &[
            "-W",
            "1920",
            "-H",
            "1080",
            "-w",
            "1920",
            "-h",
            "1080",
            "-F",
            "fsr",
            "--fsr-sharpness",
            "2",
        ],
    ),
    (
        "1440p",
        "1440p QHD",
        &[
            "-W",
            "2560",
            "-H",
            "1440",
            "-w",
            "2560",
            "-h",
            "1440",
            "-F",
            "fsr",
            "--fsr-sharpness",
            "2",
        ],
    ),
    (
        "4k",
        "4K UHD",
        &[
            "-W",
            "3840",
            "-H",
            "2160",
            "-w",
            "3840",
            "-h",
            "2160",
            "-F",
            "fsr",
            "--fsr-sharpness",
            "2",
        ],
    ),
    ("integer", "Integer Scale", &["-F", "integer"]),
    ("stretch", "Stretch to Fit", &["-F", "stretch"]),
    ("borderless", "Borderless Window", &["-b"]),
];

fn custom_preset_args(preset: &Value) -> Vec<String> {
    let mut args = Vec::new();
    let int_field = |k: &str| -> i64 {
        match preset.get(k) {
            Some(Value::Number(n)) => n.as_i64().unwrap_or(0),
            Some(Value::String(s)) => s.trim().parse().unwrap_or(0),
            _ => 0,
        }
    };
    let width = int_field("width");
    let height = int_field("height");
    if width > 0 && height > 0 {
        args.push("-W".into());
        args.push(width.to_string());
        args.push("-H".into());
        args.push(height.to_string());
        let iw = int_field("inner_width");
        let ih = int_field("inner_height");
        if iw > 0 && ih > 0 {
            args.push("-w".into());
            args.push(iw.to_string());
            args.push("-h".into());
            args.push(ih.to_string());
        }
    }
    let refresh = int_field("refresh");
    if refresh > 0 {
        args.push("-r".into());
        args.push(refresh.to_string());
    }
    if preset
        .get("integer")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        args.push("-F".into());
        args.push("integer".into());
    } else if preset
        .get("stretch")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        args.push("-F".into());
        args.push("stretch".into());
    } else if preset.get("fsr").and_then(Value::as_bool).unwrap_or(false) {
        args.push("-F".into());
        args.push("fsr".into());
        let sharp = int_field("fsr_sharpness");
        let sharp = if sharp == 0 && preset.get("fsr_sharpness").is_none() {
            2
        } else {
            sharp.clamp(0, 5)
        };
        args.push("--fsr-sharpness".into());
        args.push(sharp.to_string());
    }
    if preset
        .get("borderless")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        args.push("-b".into());
    }
    if let Some(extra) = preset.get("extra_args").and_then(Value::as_array) {
        for a in extra {
            let s = a.as_str().unwrap_or("").trim();
            if !s.is_empty() && s != "--" {
                args.push(s.to_owned());
            }
        }
    }
    args
}

pub fn get_gamescope_preset(name: &str, custom: Option<&Value>) -> Option<Value> {
    let key = name.trim().to_lowercase();
    if key.is_empty() {
        return None;
    }
    if let Some(arr) = custom.and_then(Value::as_array) {
        for preset in arr {
            if let Some(pname) = preset.get("name").and_then(Value::as_str)
                && pname.trim().to_lowercase() == key
            {
                let label = pname.trim();
                return Some(json!({
                    "label": if label.is_empty() { key.as_str() } else { label },
                    "args": custom_preset_args(preset),
                    "custom": true,
                }));
            }
        }
    }
    for (id, label, args) in GAMESCOPE_PRESETS {
        if *id == key {
            return Some(json!({
                "label": label,
                "args": args,
                "custom": false,
            }));
        }
    }
    None
}

pub fn list_gamescope_presets(custom: Option<&Value>) -> Vec<Value> {
    let mut items: Vec<Value> = GAMESCOPE_PRESETS
        .iter()
        .map(|(name, label, _)| json!([name, label]))
        .collect();
    if let Some(arr) = custom.and_then(Value::as_array) {
        for item in arr {
            if let Some(name) = item.get("name").and_then(Value::as_str) {
                let trimmed = name.trim();
                if !trimmed.is_empty() {
                    items.push(json!([trimmed, format!("{trimmed} (custom)")]));
                }
            }
        }
    }
    items
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
        && let Some(preset) =
            get_gamescope_preset(preset_id, settings.get("gamescope_custom_presets"))
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
    wrapped
}
