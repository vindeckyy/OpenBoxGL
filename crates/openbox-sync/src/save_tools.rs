//! Optional Ludusavi and Hoard save-tool integrations (`pkg/parity/parity_save_tools.py`).

use std::path::PathBuf;
use std::process::Command;

use openbox_platform::is_executable;
use serde_json::{Value, json};

use crate::saves::expand_user_path;

fn which_executable(name: &str) -> Option<PathBuf> {
    let path_var = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path_var) {
        let cand = dir.join(name);
        if is_executable(&cand) {
            return Some(cand);
        }
        #[cfg(windows)]
        {
            let exe = dir.join(format!("{name}.exe"));
            if is_executable(&exe) {
                return Some(exe);
            }
        }
    }
    None
}

pub fn save_tool_status() -> Value {
    json!({
        "ludusavi": which_executable("ludusavi").is_some(),
        "hoard": which_executable("hoard").is_some(),
    })
}

pub fn run_ludusavi(
    action: &str,
    game_name: &str,
    path: &str,
    force: bool,
) -> Result<Value, (u16, String)> {
    let Some(binary) = which_executable("ludusavi") else {
        return Err((
            404,
            "ludusavi is not installed. Install it from https://github.com/mtkennerly/ludusavi"
                .to_owned(),
        ));
    };
    if !matches!(action, "backup" | "restore" | "backups" | "find") {
        return Err((
            400,
            "Ludusavi action must be backup, restore, backups, or find.".to_owned(),
        ));
    }
    let mut cmd = Command::new(&binary);
    cmd.arg(action).arg("--api");
    if force && matches!(action, "backup" | "restore") {
        cmd.arg("--force");
    }
    if !path.trim().is_empty() {
        cmd.arg("--path").arg(expand_user_path(path));
    }
    if !game_name.trim().is_empty() && matches!(action, "backup" | "restore" | "find") {
        cmd.arg("--").arg(game_name);
    }
    let out = cmd
        .output()
        .map_err(|e| (500, format!("ludusavi failed: {e}")))?;
    let stdout = String::from_utf8_lossy(&out.stdout).trim().to_owned();
    let stderr = String::from_utf8_lossy(&out.stderr).trim().to_owned();
    let payload: Value = if stdout.is_empty() {
        json!({})
    } else {
        serde_json::from_str(&stdout).unwrap_or_else(|_| json!({"raw": stdout}))
    };
    if !out.status.success() && payload.as_object().is_some_and(|m| m.is_empty()) {
        let msg = if !stderr.is_empty() {
            stderr
        } else if !stdout.is_empty() {
            stdout
        } else {
            "ludusavi failed".to_owned()
        };
        return Err((500, msg));
    }
    Ok(json!({
        "ok": out.status.success(),
        "action": action,
        "result": payload,
        "stderr": stderr,
    }))
}

pub fn run_hoard(action: &str, game_name: &str) -> Result<Value, (u16, String)> {
    let Some(binary) = which_executable("hoard") else {
        return Err((
            404,
            "hoard is not installed. Install it from https://github.com/rleeon/hoard".to_owned(),
        ));
    };
    if !matches!(action, "backup" | "restore" | "list") {
        return Err((
            400,
            "Hoard action must be backup, restore, or list.".to_owned(),
        ));
    }
    let mut cmd = Command::new(&binary);
    if action == "list" {
        cmd.arg("list");
    } else if !game_name.trim().is_empty() {
        cmd.arg(action).arg(game_name);
    } else {
        cmd.arg(action);
    }
    let out = cmd
        .output()
        .map_err(|e| (500, format!("hoard failed: {e}")))?;
    let stdout = String::from_utf8_lossy(&out.stdout).trim().to_owned();
    let stderr = String::from_utf8_lossy(&out.stderr).trim().to_owned();
    if !out.status.success() {
        let msg = if !stderr.is_empty() {
            stderr
        } else if !stdout.is_empty() {
            stdout
        } else {
            "hoard failed".to_owned()
        };
        return Err((500, msg));
    }
    Ok(json!({
        "ok": true,
        "action": action,
        "output": stdout,
    }))
}
