//! Launch Doctor: validate launch readiness without spawning.
//! Mirrors `pkg/parity/parity_launch_doctor.py`.

use std::collections::HashMap;
use std::path::Path;
use std::process::Command;

use openbox_core::ApiError;
use openbox_platform::{IS_LINUX, expand_user, flatpak_app_installed, which};
use serde_json::{Map, Value, json};

use crate::defs::{
    Adapter, apply_core_override, detect_adapter_for_platform, detect_adapter_prefix, find_adapter,
    flatpak_core_path, resolve_launch, resolved_startup_args,
};
use crate::tokens::find_invalid_tokens;

fn make_check(
    code: &str,
    severity: &str,
    message: impl Into<String>,
    fix_action: Option<Value>,
) -> Value {
    let msg = message.into();
    let mut obj = json!({
        "code": code,
        "severity": severity,
        "message": msg,
        "remediations": [],
    });
    if let Some(fix) = fix_action {
        obj["fix_action"] = fix;
    } else if severity == "error" {
        obj["fix_action"] = json!({
            "kind": "explain_token",
            "label": "Check launch configuration",
            "payload": {"invalid_tokens": [], "template": msg},
        });
    }
    obj
}

pub fn derive_status(checks: &[Value]) -> &'static str {
    if checks
        .iter()
        .any(|c| c.get("severity").and_then(Value::as_str) == Some("error"))
    {
        "blocked"
    } else if checks
        .iter()
        .any(|c| c.get("severity").and_then(Value::as_str) == Some("warning"))
    {
        "warning"
    } else {
        "ready"
    }
}

fn flatpak_fs_allowed(app_id: &str, rom_path: &Path) -> bool {
    if !IS_LINUX || app_id.is_empty() {
        return true;
    }
    let Some(fp) = which("flatpak") else {
        return true;
    };
    let Ok(out) = Command::new(fp)
        .args(["info", "--show-permissions", app_id])
        .output()
    else {
        return false;
    };
    if !out.status.success() {
        return true;
    }
    let perms = String::from_utf8_lossy(&out.stdout);
    if perms.contains("filesystem=host") || perms.contains("filesystem=home") {
        return true;
    }
    let resolved = std::fs::canonicalize(rom_path).unwrap_or_else(|_| rom_path.to_path_buf());
    for line in perms.lines() {
        let trimmed = line.trim();
        let Some(rest) = trimmed.strip_prefix("filesystem=") else {
            continue;
        };
        let grant = rest
            .strip_suffix(":ro")
            .or_else(|| rest.strip_suffix(":rw"))
            .unwrap_or(rest)
            .trim();
        let grant_path = expand_user(Path::new(grant));
        let canon = std::fs::canonicalize(&grant_path).unwrap_or(grant_path);
        if resolved == canon || resolved.starts_with(&canon) {
            return true;
        }
    }
    false
}

pub fn run_preflight_checks(
    game: &Value,
    profiles: &Map<String, Value>,
    adapters: &[Adapter],
    data_dir: &Path,
    deep: bool,
) -> (Vec<Value>, Value) {
    let mut checks = Vec::new();
    let path_str = game
        .get("path")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("");
    let platform = game
        .get("platform")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("");
    let game_launch = game
        .get("launch")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("");

    let mut resolved = json!({
        "emulator_id": null,
        "adapter_id": null,
        "argv_preview": [],
        "cwd": null,
        "precedence": 5,
    });

    if !game_launch.is_empty() {
        let bad = find_invalid_tokens(game_launch);
        if !bad.is_empty() {
            checks.push(make_check(
                "INVALID_TOKEN",
                "error",
                format!(
                    "Custom launch command contains unknown token(s): {}",
                    bad.join(", ")
                ),
                Some(json!({
                    "kind": "explain_token",
                    "label": format!("Unknown token: {}", bad.join(", ")),
                    "payload": {"invalid_tokens": bad, "template": game_launch},
                })),
            ));
        }
    }

    if path_str.is_empty() {
        checks.push(make_check(
            "PATH_MISSING",
            "error",
            "This game has no launch path configured.",
            Some(json!({
                "kind": "set_path",
                "label": "Set game path",
                "payload": {},
            })),
        ));
        return (checks, resolved);
    }

    let rom_path = expand_user(Path::new(path_str));
    if !rom_path.exists() {
        checks.push(make_check(
            "PATH_NOT_FOUND",
            "error",
            format!(
                "Game file does not exist: {}",
                rom_path.file_name().unwrap_or_default().to_string_lossy()
            ),
            Some(json!({
                "kind": "set_path",
                "label": "Update game path",
                "payload": {"path": path_str},
            })),
        ));
    }

    let aid = game
        .get("emulator_adapter_id")
        .and_then(Value::as_str)
        .unwrap_or("");
    let eid = game
        .get("emulator_id")
        .and_then(Value::as_str)
        .unwrap_or("");
    let adapter = find_adapter(adapters, aid, eid)
        .or_else(|| detect_adapter_for_platform(adapters, platform))
        .or_else(|| adapters.iter().find(|a| a.platform == platform));

    if let Some(a) = adapter {
        let eff = apply_core_override(a, game);
        resolved["emulator_id"] = Value::String(eff.emulator_id.clone());
        resolved["adapter_id"] = Value::String(eff.adapter_id.clone());

        let prefix = detect_adapter_prefix(&eff);
        if prefix.is_empty() && game_launch.is_empty() && !profiles.contains_key(platform) {
            let fp_id = eff.flatpak_app_id.as_deref().unwrap_or("");
            let code = if IS_LINUX && !fp_id.is_empty() && !flatpak_app_installed(fp_id) {
                "FLATPAK_NOT_INSTALLED"
            } else {
                "EMULATOR_MISSING"
            };
            checks.push(make_check(
                code,
                "error",
                format!("{} is not installed.", eff.label),
                Some(json!({
                    "kind": "flatpak_install",
                    "label": "Install emulator",
                    "payload": {
                        "app_id": fp_id,
                        "native_exe": eff.native_exe.as_deref().unwrap_or(""),
                    },
                })),
            ));
        } else if prefix.first().map(String::as_str) == Some("flatpak")
            && let Some(fp_id) = eff.flatpak_app_id.as_deref()
            && rom_path.exists()
            && !flatpak_fs_allowed(fp_id, &rom_path)
        {
            let folder = rom_path
                .parent()
                .map(|p| p.to_string_lossy().into_owned())
                .unwrap_or_default();
            checks.push(make_check(
                "FLATPAK_FS_DENIED",
                "error",
                format!("Flatpak {fp_id} does not have read access to the ROM folder."),
                Some(json!({
                    "kind": "flatpak_grant",
                    "label": "Grant Flatpak access to this folder",
                    "payload": {"app_id": fp_id, "path": folder},
                })),
            ));
        }

        // Check RetroArch core presence when adapter specifies -L <core>
        let startup = resolved_startup_args(&eff);
        for idx in 0..startup.len().saturating_sub(1) {
            if startup[idx] == "-L" {
                let core = flatpak_core_path(&startup[idx + 1], &prefix);
                if core.starts_with('/') && !Path::new(&core).is_file() {
                    checks.push(make_check(
                        "CORE_MISSING",
                        "warning",
                        format!(
                            "RetroArch core not found: {}",
                            Path::new(&core)
                                .file_name()
                                .unwrap_or_default()
                                .to_string_lossy()
                        ),
                        Some(json!({
                            "kind": "pick_core",
                            "label": "Choose emulator / core",
                            "payload": {"adapter_id": eff.adapter_id, "core": core},
                        })),
                    ));
                }
            }
        }

        if deep {
            if let Some(bios) = &eff.bios_path
                && !expand_user(Path::new(bios)).exists()
            {
                checks.push(make_check(
                    "BIOS_MISSING",
                    "warning",
                    format!("BIOS path missing for {}.", eff.label),
                    Some(json!({
                        "kind": "reveal_bios_path",
                        "label": "Show BIOS folder",
                        "payload": {"path": bios, "name": eff.label},
                    })),
                ));
            }
            if let Some(fw) = &eff.firmware_path
                && !expand_user(Path::new(fw)).exists()
            {
                checks.push(make_check(
                    "FIRMWARE_MISSING",
                    "warning",
                    format!("Firmware missing for {}.", eff.label),
                    Some(json!({
                        "kind": "reveal_bios_path",
                        "label": "Show firmware folder",
                        "payload": {"path": fw, "name": eff.label},
                    })),
                ));
            }
        }
    }

    if rom_path.exists()
        && let Ok(plan) = resolve_launch(game, profiles, adapters, data_dir, None)
    {
        resolved["argv_preview"] = Value::Array(plan.args.into_iter().map(Value::String).collect());
        resolved["cwd"] = Value::String(plan.cwd);
        resolved["precedence"] = Value::from(match plan.precedence {
            "game_launch" => 1,
            "game_adapter" => 2,
            "platform_profile" => 3,
            "registry_adapter" => 4,
            _ => 5,
        });
    }

    (checks, resolved)
}

pub fn preflight_single(
    payload: &Map<String, Value>,
    state: &Value,
    adapters: &[Adapter],
    data_dir: &Path,
) -> Result<Value, ApiError> {
    let game_id = payload
        .get("game_id")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty());
    let candidate = payload.get("candidate").and_then(Value::as_object);
    if game_id.is_some() == candidate.is_some() {
        return Err(ApiError::bad_request(
            "Provide exactly one of game_id or candidate.",
        ));
    }
    let profiles = state
        .get("profiles")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let deep = payload
        .get("deep")
        .and_then(Value::as_bool)
        .unwrap_or(false);

    let (game, out_gid, out_cid) = if let Some(gid) = game_id {
        let found = state
            .get("games")
            .and_then(Value::as_array)
            .and_then(|arr| {
                arr.iter()
                    .find(|g| g.get("game_id").and_then(Value::as_str) == Some(gid))
            })
            .cloned()
            .ok_or_else(|| ApiError::game_not_found("Game not found"))?;
        (found, Value::String(gid.to_owned()), Value::Null)
    } else {
        let cand = candidate.unwrap();
        let path = cand.get("path").and_then(Value::as_str).unwrap_or("");
        let cid = cand
            .get("candidate_id")
            .and_then(Value::as_str)
            .unwrap_or("");
        let g = json!({
            "name": Path::new(path).file_stem().map(|s| s.to_string_lossy().into_owned()).unwrap_or_else(|| "Candidate".to_owned()),
            "path": path,
            "platform": cand.get("platform").cloned().unwrap_or(Value::Null),
            "emulator_id": cand.get("emulator_id").cloned().unwrap_or(Value::Null),
            "emulator_adapter_id": cand.get("adapter_id").cloned().unwrap_or(Value::Null),
            "archive_member": cand.get("archive_member").cloned().unwrap_or(Value::Null),
        });
        (g, Value::Null, Value::String(cid.to_owned()))
    };

    let (checks, resolved) = run_preflight_checks(&game, &profiles, adapters, data_dir, deep);
    let status = derive_status(&checks);
    Ok(json!({
        "status": status,
        "game_id": out_gid,
        "candidate_id": out_cid,
        "resolved": resolved,
        "checks": checks,
    }))
}

pub fn preflight_batch(
    payload: &Map<String, Value>,
    state: &Value,
    adapters: &[Adapter],
    data_dir: &Path,
) -> Result<Value, ApiError> {
    let items = payload
        .get("items")
        .or_else(|| payload.get("game_ids"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut results = Vec::with_capacity(items.len());
    let mut ready = 0usize;
    let mut warning = 0usize;
    let mut blocked = 0usize;
    let mut by_platform: HashMap<String, [usize; 3]> = HashMap::new();

    for item in items {
        let req_obj = if let Some(s) = item.as_str() {
            let mut m = Map::new();
            m.insert("game_id".into(), Value::String(s.to_owned()));
            m
        } else if let Some(obj) = item.as_object() {
            obj.clone()
        } else {
            continue;
        };
        if let Ok(res) = preflight_single(&req_obj, state, adapters, data_dir) {
            let st = res["status"].as_str().unwrap_or("ready");
            let idx = match st {
                "blocked" => {
                    blocked += 1;
                    2
                }
                "warning" => {
                    warning += 1;
                    1
                }
                _ => {
                    ready += 1;
                    0
                }
            };
            let plat = req_obj
                .get("platform")
                .and_then(Value::as_str)
                .unwrap_or("Other")
                .to_owned();
            by_platform.entry(plat).or_insert([0, 0, 0])[idx] += 1;
            results.push(res);
        }
    }

    let mut bp_map = Map::new();
    for (p, counts) in by_platform {
        bp_map.insert(
            p,
            json!({"ready": counts[0], "warning": counts[1], "blocked": counts[2]}),
        );
    }
    Ok(json!({
        "totals": {"ready": ready, "warning": warning, "blocked": blocked},
        "by_platform": bp_map,
        "results": results,
    }))
}
