//! Emulator discovery, status, launch, Flatpak install & update. Mirrors `emulators.py`.

use std::process::{Command, Stdio};

use openbox_platform::{
    flatpak_app_installed, join_command, resolve_native_executable, split_command, which,
};
use serde_json::{Map, Value, json};

use crate::defs::{Adapter, build_emulators_map, build_platform_emulators};

pub fn emulator_status(adapters: &[Adapter]) -> Vec<Value> {
    let emulators = build_emulators_map(adapters);
    let recommendations = build_platform_emulators(adapters);
    let rec_json: Map<String, Value> = recommendations
        .into_iter()
        .map(|(plat, pairs)| {
            (
                plat,
                Value::Array(
                    pairs
                        .into_iter()
                        .map(|(id, name)| json!([id, name]))
                        .collect(),
                ),
            )
        })
        .collect();
    let flatpak = which("flatpak");
    let mut out = Vec::with_capacity(emulators.len());

    for (app_id, emu) in &emulators {
        let native_name = emu.get("native").and_then(Value::as_str).unwrap_or("");
        let native = if !native_name.is_empty() {
            resolve_native_executable(native_name)
        } else {
            None
        };
        let fp_installed = flatpak.is_some() && native.is_none() && flatpak_app_installed(app_id);
        let mode = if native.is_some() {
            "native"
        } else if fp_installed {
            "flatpak"
        } else {
            ""
        };
        let prefix: Vec<String> = if let Some(ref n) = native {
            vec![n.clone()]
        } else if fp_installed {
            vec![
                flatpak
                    .as_ref()
                    .map(|p| p.to_string_lossy().into_owned())
                    .unwrap_or_else(|| "flatpak".to_owned()),
                "run".to_owned(),
                app_id.clone(),
            ]
        } else {
            Vec::new()
        };

        let mut profiles_out = Map::new();
        let mut platforms = Vec::new();
        if let Some(profs) = emu.get("profiles").and_then(Value::as_object) {
            for (plat, arg_val) in profs {
                platforms.push(Value::String(plat.clone()));
                if !prefix.is_empty() {
                    let arg_str = arg_val.as_str().unwrap_or("");
                    let mut full = prefix.clone();
                    if let Ok(parts) = split_command(arg_str) {
                        full.extend(parts);
                    }
                    profiles_out.insert(plat.clone(), Value::String(join_command(&full)));
                }
            }
        }
        out.push(json!({
            "app_id": app_id,
            "name": emu.get("name").and_then(Value::as_str).unwrap_or(app_id),
            "platforms": platforms,
            "installed": !mode.is_empty(),
            "mode": mode,
            "profiles": profiles_out,
            "can_install": flatpak.is_some(),
            "recommendations": rec_json,
        }));
    }
    out
}

pub fn recommendations_for_platform(adapters: &[Adapter], platform: &str) -> Vec<Value> {
    let map = build_platform_emulators(adapters);
    let Some(list) = map.get(platform) else {
        return Vec::new();
    };
    list.iter()
        .map(|(app_id, name)| json!({"app_id": app_id, "name": name}))
        .collect()
}

pub fn launch_emulator(adapters: &[Adapter], app_id: &str) -> Result<Value, String> {
    let emulators = build_emulators_map(adapters);
    let Some(emu) = emulators.get(app_id) else {
        return Err("Unknown emulator.".to_owned());
    };
    let native_name = emu.get("native").and_then(Value::as_str).unwrap_or("");
    if !native_name.is_empty()
        && let Some(resolved) = resolve_native_executable(native_name)
    {
        Command::new(&resolved)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| e.to_string())?;
        return Ok(json!({"mode": "native", "command": resolved}));
    }
    if let Some(fp) = which("flatpak")
        && flatpak_app_installed(app_id)
    {
        Command::new(&fp)
            .args(["run", app_id])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| e.to_string())?;
        return Ok(json!({"mode": "flatpak", "command": format!("flatpak run {app_id}")}));
    }
    let name = emu.get("name").and_then(Value::as_str).unwrap_or(app_id);
    Err(format!("{name} is not installed."))
}

pub fn install_emulator(adapters: &[Adapter], app_id: &str) -> Result<Map<String, Value>, String> {
    let emulators = build_emulators_map(adapters);
    let Some(emu) = emulators.get(app_id) else {
        return Err("Unknown emulator.".to_owned());
    };
    let Some(fp) = which("flatpak") else {
        return Err("Flatpak is required for automatic emulator installation.".to_owned());
    };
    let remotes = Command::new(&fp)
        .args(["remotes", "--user"])
        .output()
        .map_err(|e| e.to_string())?;
    let rem_txt = format!(
        "{}{}",
        String::from_utf8_lossy(&remotes.stdout),
        String::from_utf8_lossy(&remotes.stderr)
    )
    .to_ascii_lowercase();
    if !rem_txt.contains("flathub") {
        let add = Command::new(&fp)
            .args([
                "remote-add",
                "--user",
                "--if-not-exists",
                "flathub",
                "https://flathub.org/repo/flathub.flatpakrepo",
            ])
            .output()
            .map_err(|e| e.to_string())?;
        if !add.status.success() {
            let detail = String::from_utf8_lossy(&add.stderr).trim().to_owned();
            return Err(format!("Could not add the Flathub remote. {detail}"));
        }
    }
    let inst = Command::new(&fp)
        .args([
            "install",
            "--user",
            "--noninteractive",
            "-y",
            "flathub",
            app_id,
        ])
        .output()
        .map_err(|e| e.to_string())?;
    if !inst.status.success() {
        let name = emu.get("name").and_then(Value::as_str).unwrap_or(app_id);
        let detail = String::from_utf8_lossy(&inst.stderr).trim().to_owned();
        return Err(format!("Flatpak could not install {name}. {detail}"));
    }
    let prefix = vec![
        fp.to_string_lossy().into_owned(),
        "run".to_owned(),
        app_id.to_owned(),
    ];
    let mut profiles_out = Map::new();
    if let Some(profs) = emu.get("profiles").and_then(Value::as_object) {
        for (plat, arg_val) in profs {
            let mut full = prefix.clone();
            if let Ok(parts) = split_command(arg_val.as_str().unwrap_or("")) {
                full.extend(parts);
            }
            profiles_out.insert(plat.clone(), Value::String(join_command(&full)));
        }
    }
    Ok(profiles_out)
}

pub fn update_emulator(adapters: &[Adapter], app_id: &str) -> Result<Value, String> {
    let emulators = build_emulators_map(adapters);
    let Some(emu) = emulators.get(app_id) else {
        return Err("Unknown emulator.".to_owned());
    };
    let Some(fp) = which("flatpak") else {
        return Err("Flatpak is required for emulator updates.".to_owned());
    };
    let out = Command::new(&fp)
        .args(["update", "--user", "--noninteractive", "-y", app_id])
        .output()
        .map_err(|e| e.to_string())?;
    if !out.status.success() {
        let detail = String::from_utf8_lossy(&out.stderr).trim().to_owned();
        return Err(format!("Flatpak update failed: {detail}"));
    }
    Ok(json!({"updated": emu.get("name").and_then(Value::as_str).unwrap_or(app_id)}))
}
