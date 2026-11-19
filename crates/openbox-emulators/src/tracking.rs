//! Per-game process tracking modes and optional store-client closing.
//! Mirrors `pkg/parity/parity_tracking.py`.

use std::process::{Command, Stdio};

use openbox_platform::which;
use serde_json::Value;

pub const TRACKING_MODES: &[&str] = &[
    "default",
    "process",
    "original_process",
    "folder",
    "process_name",
];

#[derive(Debug, Clone)]
pub struct TrackingConfig {
    pub mode: String,
    pub delay: u64,
    pub frequency: f64,
    pub process_name: String,
}

pub fn resolve_mode(game: &Value, settings: &Value) -> TrackingConfig {
    let raw_mode = game
        .get("tracking_mode")
        .and_then(Value::as_str)
        .filter(|s| !s.trim().is_empty())
        .or_else(|| settings.get("tracking_mode").and_then(Value::as_str))
        .unwrap_or("default")
        .trim()
        .to_ascii_lowercase();
    let mode = if TRACKING_MODES.contains(&raw_mode.as_str()) {
        raw_mode
    } else {
        "default".to_owned()
    };
    let delay = game
        .get("tracking_delay")
        .or_else(|| settings.get("tracking_delay"))
        .and_then(Value::as_i64)
        .unwrap_or(0)
        .clamp(0, 600) as u64;
    let frequency = game
        .get("tracking_frequency")
        .or_else(|| settings.get("tracking_frequency"))
        .and_then(Value::as_f64)
        .unwrap_or(2.0)
        .clamp(0.5, 60.0);
    let process_name = game
        .get("tracking_process_name")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_owned();
    TrackingConfig {
        mode,
        delay,
        frequency,
        process_name,
    }
}

pub fn close_store_client(game: &Value, settings: &Value) {
    if !settings
        .get("auto_close_store_clients")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        return;
    }
    let source = game
        .get("source")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    let pair = match source {
        "Steam" => Some(("steam", "com.valvesoftware.Steam")),
        "Epic" | "GOG" | "Amazon" => Some(("heroic", "com.heroicgameslauncher.hgl")),
        "Lutris" => Some(("lutris", "net.lutris.Lutris")),
        _ => None,
    };
    let Some((binary, flatpak_id)) = pair else {
        return;
    };
    if let Some(bin) = which(binary) {
        let _ = Command::new(bin)
            .arg("-shutdown")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn();
    } else if let Some(fp) = which("flatpak") {
        let _ = Command::new(fp)
            .args(["kill", flatpak_id])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn();
    }
}
