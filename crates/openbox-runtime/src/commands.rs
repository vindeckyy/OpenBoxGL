//! Command validation and execution helpers. Mirrors `pkg/state/commands.py`.

use std::path::Path;
use std::process::{Command, Stdio};

use openbox_platform::{expand_user, split_command};
use serde_json::Value;

pub fn clean_commands(commands: &Value) -> Result<Vec<String>, String> {
    let Some(arr) = commands.as_array() else {
        return Err("Application commands must be a list of at most 25 entries.".to_owned());
    };
    if arr.len() > 25 {
        return Err("Application commands must be a list of at most 25 entries.".to_owned());
    }
    let mut clean = Vec::new();
    for item in arr {
        let cmd = match item {
            Value::String(s) => s.trim().to_owned(),
            other => other.to_string().trim().to_owned(),
        };
        if !cmd.is_empty() {
            let parts =
                split_command(&cmd).map_err(|e| format!("Invalid application command: {e}"))?;
            if parts.is_empty() {
                return Err("Application command is empty.".to_owned());
            }
            clean.push(cmd);
        }
    }
    Ok(clean)
}

pub fn run_configured_commands(state: &Value, key: &str) {
    let Some(commands) = state
        .get("settings")
        .and_then(|s| s.get(key))
        .and_then(Value::as_array)
    else {
        return;
    };
    for raw in commands {
        let Some(cmd) = raw.as_str() else {
            continue;
        };
        let Ok(mut args) = split_command(cmd) else {
            continue;
        };
        if args.is_empty() {
            continue;
        }
        let exe = expand_user(Path::new(&args[0]));
        args[0] = exe.to_string_lossy().into_owned();
        let mut proc = Command::new(&args[0]);
        proc.args(&args[1..])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        if let Err(err) = proc.spawn() {
            tracing::warn!("run_configured_commands({key}): {err}");
        }
    }
}
