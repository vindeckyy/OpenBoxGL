//! One-click Flatpak `:ro` folder access grant & undo for Launch Readiness.
//! Mirrors `pkg/parity/parity_flatpak_grant.py`.

use std::collections::HashSet;
use std::path::Path;
use std::process::Command;

use openbox_core::ApiError;
use openbox_platform::{IS_LINUX, home_dir};
use serde_json::{Value, json};

use crate::defs::Adapter;

pub const GRANT_TIMEOUT_SECONDS: u64 = 30;

pub fn known_flatpak_ids(adapters: &[Adapter]) -> HashSet<String> {
    adapters
        .iter()
        .filter_map(|a| a.flatpak_app_id.clone())
        .collect()
}

pub fn validate_grant(
    adapters: &[Adapter],
    app_id: &str,
    folder: &str,
) -> Result<(String, String), ApiError> {
    let app = app_id.trim();
    if !known_flatpak_ids(adapters).contains(app) {
        return Err(ApiError::bad_request(
            "That Flatpak app is not one OpenBox has a definition for.",
        ));
    }
    let text = folder.trim();
    if text.is_empty() || !Path::new(text).is_absolute() {
        return Err(ApiError::bad_request(
            "The folder must be an absolute path.",
        ));
    }
    let resolved = std::fs::canonicalize(text)
        .map_err(|_| ApiError::bad_request("That folder does not exist."))?;
    let home = home_dir()
        .and_then(|h| std::fs::canonicalize(h).ok())
        .ok_or_else(|| ApiError::bad_request("Could not determine home directory."))?;
    if resolved == home || !resolved.starts_with(&home) {
        return Err(ApiError::bad_request(
            "Only a folder inside your home directory can be granted.",
        ));
    }
    if !resolved.is_dir() {
        return Err(ApiError::bad_request("That folder does not exist."));
    }
    Ok((app.to_owned(), resolved.to_string_lossy().into_owned()))
}

pub fn grant_command(app: &str, folder: &str, undo: bool) -> Vec<String> {
    if undo {
        vec![
            "flatpak".to_owned(),
            "override".to_owned(),
            "--user".to_owned(),
            format!("--nofilesystem={folder}"),
            app.to_owned(),
        ]
    } else {
        vec![
            "flatpak".to_owned(),
            "override".to_owned(),
            "--user".to_owned(),
            format!("--filesystem={folder}:ro"),
            app.to_owned(),
        ]
    }
}

pub fn apply_grant(
    adapters: &[Adapter],
    app_id: &str,
    folder: &str,
    undo: bool,
) -> Result<Value, ApiError> {
    if !IS_LINUX {
        return Err(ApiError::bad_request(
            "Flatpak grants are only available on Linux.",
        ));
    }
    let (app, path) = validate_grant(adapters, app_id, folder)?;
    let cmd = grant_command(&app, &path, undo);
    let output = Command::new(&cmd[0])
        .args(&cmd[1..])
        .output()
        .map_err(|e| ApiError::bad_request(format!("flatpak did not finish the change: {e}")))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        let stdout = String::from_utf8_lossy(&output.stdout);
        let detail = if !stderr.trim().is_empty() {
            stderr.trim()
        } else if !stdout.trim().is_empty() {
            stdout.trim()
        } else {
            "no detail given"
        };
        let snippet: String = detail.chars().take(300).collect();
        return Err(ApiError::bad_request(format!(
            "flatpak refused the change: {snippet}"
        )));
    }
    Ok(json!({
        "applied": true,
        "undone": undo,
        "app_id": app,
        "path": path,
        "command": cmd.join(" "),
    }))
}
