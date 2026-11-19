//! Handheld performance profiles: per-profile TDP limits via `ryzenadj`.
//! Mirrors `pkg/parity/parity_perf.py`.

use std::process::Command;

use openbox_platform::{IS_LINUX, battery_status, which};
use serde_json::{Map, Value, json};

use crate::gamescope::is_gamescope_guest;

pub fn effective_profile_name(game: &Value, profiles: &Map<String, Value>) -> String {
    let selected = game
        .get("launch_profile")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("");
    if !selected.is_empty() && profiles.contains_key(selected) {
        return selected.to_owned();
    }
    game.get("platform")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("")
        .to_owned()
}

pub fn perf_should_apply(state: &Value) -> bool {
    if !IS_LINUX {
        return false;
    }
    let mode = state
        .get("settings")
        .and_then(|s| s.get("apply_perf"))
        .and_then(Value::as_str)
        .unwrap_or("auto")
        .trim()
        .to_ascii_lowercase();
    match mode.as_str() {
        "off" => false,
        "always" => true,
        _ => is_gamescope_guest(false) || battery_status(None).present,
    }
}

fn run_ryzenadj(tdp_w: f64) -> Result<(), String> {
    let Some(bin) = which("ryzenadj") else {
        return Err("ryzenadj not installed".to_owned());
    };
    let mw = (tdp_w * 1000.0).round() as i64;
    let out = Command::new(bin)
        .arg(format!("-stapm-limit={mw}"))
        .output()
        .map_err(|e| e.to_string())?;
    if !out.status.success() {
        let stderr = String::from_utf8_lossy(&out.stderr);
        return Err(if stderr.trim().is_empty() {
            "ryzenadj exited nonzero".to_owned()
        } else {
            stderr.trim().to_owned()
        });
    }
    Ok(())
}

pub fn apply_perf_profile(profile_name: &str, state: &Value) -> bool {
    if !perf_should_apply(state) {
        return false;
    }
    let Some(perf) = state
        .get("perf_profiles")
        .and_then(|p| p.get(profile_name))
        .and_then(Value::as_object)
    else {
        return false;
    };
    if !perf
        .get("enabled")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        return false;
    }
    let Some(tdp) = perf
        .get("tdp_w")
        .and_then(Value::as_f64)
        .filter(|&v| v > 0.0)
    else {
        return false;
    };
    match run_ryzenadj(tdp) {
        Ok(()) => true,
        Err(err) => {
            tracing::warn!("apply_perf: {profile_name} ({tdp} W): {err}");
            false
        }
    }
}

pub fn restore_perf_profile(profile_name: &str, state: &Value) -> Value {
    if !perf_should_apply(state) {
        return json!({"profile": profile_name, "applied": false, "reason": "auto-skipped", "tdp_w": 0});
    }
    let Some(tdp) = state
        .get("perf_profiles")
        .and_then(|p| p.get(profile_name))
        .and_then(|p| p.get("restore_tdp_w"))
        .and_then(Value::as_f64)
        .filter(|&v| v > 0.0)
    else {
        return json!({"profile": profile_name, "applied": false, "reason": "no-restore", "tdp_w": 0});
    };
    match run_ryzenadj(tdp) {
        Ok(()) => json!({"profile": profile_name, "applied": true, "reason": "ok", "tdp_w": tdp}),
        Err(err) => {
            tracing::warn!("restore_perf: {profile_name} ({tdp} W): {err}");
            json!({"profile": profile_name, "applied": false, "reason": err, "tdp_w": tdp})
        }
    }
}
