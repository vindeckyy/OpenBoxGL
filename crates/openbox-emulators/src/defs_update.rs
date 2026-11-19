//! Signed Ed25519 update channel for community emulator definitions (ADR 0060).
//! Mirrors `pkg/parity/parity_emulator_defs_update.py`.

use std::collections::{BTreeMap, HashSet};
use std::fs;
use std::io::{Cursor, Read};
use std::path::{Path, PathBuf};

use base64::Engine;
use ed25519_dalek::{Signature, VerifyingKey};
use flate2::read::GzDecoder;
use openbox_core::APP_VERSION;
use openbox_io::{atomic_write_text, build_public_http_client};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tar::Archive;

use crate::defs::{normalize_adapter, parse_yaml_def};

pub const PACK_BASE: &str =
    "https://github.com/vindeckyy/OpenBoxGL/releases/download/emulator-defs";
pub const PACK_INDEX: &str =
    "https://github.com/vindeckyy/OpenBoxGL/releases/download/emulator-defs/index.json";
pub const PACK_ARCHIVE: &str =
    "https://github.com/vindeckyy/OpenBoxGL/releases/download/emulator-defs/community-defs.tar.gz";
pub const MAX_PACK_BYTES: usize = 8 * 1024 * 1024;
pub const MAX_DEFINITION_BYTES: usize = 256 * 1024;
pub const MAX_DEFINITIONS: usize = 400;
pub const SUPPORTED_SCHEMA_VERSION: u32 = 2;
pub const STATE_FILE: &str = "emulator_defs_update.json";
pub const PLACEHOLDER_PUBLIC_KEY: &str = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";

pub const REQUIRED_KEYS: &[&str] = &[
    "emulator_id",
    "adapter_id",
    "label",
    "platform",
    "extensions",
    "executable_patterns",
    "native_exe",
    "native_exe_windows",
    "native_exe_macos",
    "flatpak_app_id",
    "priority",
    "startup_args",
    "state",
    "schema_version",
    "recommended",
];

#[derive(Debug, thiserror::Error)]
pub enum DefsUpdateError {
    #[error("{0}")]
    Signature(String),
    #[error("{0}")]
    Definition(String),
}

pub fn local_defs_dir(data_dir: &Path) -> PathBuf {
    data_dir.join("emulator_defs")
}

fn read_state(data_dir: &Path) -> Value {
    let path = data_dir.join(STATE_FILE);
    fs::read_to_string(&path)
        .ok()
        .and_then(|s| serde_json::from_str::<Value>(&s).ok())
        .filter(Value::is_object)
        .unwrap_or_else(|| json!({}))
}

fn write_state(data_dir: &Path, payload: &Value) -> Result<(), DefsUpdateError> {
    let text = serde_json::to_string_pretty(payload)
        .map_err(|e| DefsUpdateError::Definition(e.to_string()))?;
    atomic_write_text(&data_dir.join(STATE_FILE), &text, 0o600)
        .map(|_| ())
        .map_err(|e| DefsUpdateError::Definition(e.to_string()))
}

pub fn verify_ed25519_artifact(
    artifact_bytes: &[u8],
    sig_text: &str,
    key_text: &str,
) -> Result<(), DefsUpdateError> {
    let key_b64 = key_text.trim();
    if key_b64.is_empty() || key_b64 == PLACEHOLDER_PUBLIC_KEY {
        return Err(DefsUpdateError::Signature(
            "Placeholder release signing key cannot verify updates.".to_owned(),
        ));
    }
    let key_bytes = base64::engine::general_purpose::STANDARD
        .decode(key_b64)
        .map_err(|_| {
            DefsUpdateError::Signature("Release public key is not valid base64.".to_owned())
        })?;
    let key_arr: [u8; 32] = key_bytes.try_into().map_err(|_| {
        DefsUpdateError::Signature("Release public key must be 32 bytes.".to_owned())
    })?;
    let vk = VerifyingKey::from_bytes(&key_arr)
        .map_err(|e| DefsUpdateError::Signature(format!("Invalid release public key: {e}")))?;

    let sig_doc: Value = serde_json::from_str(sig_text)
        .map_err(|_| DefsUpdateError::Signature("Signature file is not valid JSON.".to_owned()))?;
    let sig_b64 = sig_doc
        .get("signature")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| {
            DefsUpdateError::Signature("Signature payload missing 'signature'.".to_owned())
        })?;
    let expected_sha = sig_doc
        .get("sha256")
        .and_then(Value::as_str)
        .map(|s| s.trim().to_ascii_lowercase())
        .unwrap_or_default();
    let actual_sha = hex::encode(Sha256::digest(artifact_bytes));
    if !expected_sha.is_empty() && expected_sha != actual_sha {
        return Err(DefsUpdateError::Signature(
            "Artifact SHA-256 digest does not match signature metadata.".to_owned(),
        ));
    }
    let sig_bytes = base64::engine::general_purpose::STANDARD
        .decode(sig_b64)
        .map_err(|_| DefsUpdateError::Signature("Signature is not valid base64.".to_owned()))?;
    let sig_arr: [u8; 64] = sig_bytes
        .try_into()
        .map_err(|_| DefsUpdateError::Signature("Signature must be 64 bytes.".to_owned()))?;
    let signature = Signature::from_bytes(&sig_arr);
    vk.verify_strict(artifact_bytes, &signature)
        .or_else(|_| vk.verify_strict(actual_sha.as_bytes(), &signature))
        .map_err(|e| DefsUpdateError::Signature(format!("Ed25519 verification failed: {e}")))
}

pub fn read_pack(archive_bytes: &[u8]) -> Result<BTreeMap<String, String>, DefsUpdateError> {
    let gz = GzDecoder::new(Cursor::new(archive_bytes));
    let mut tar = Archive::new(gz);
    let mut defs: BTreeMap<String, String> = BTreeMap::new();
    let entries = tar
        .entries()
        .map_err(|e| DefsUpdateError::Definition(format!("Invalid tar.gz pack: {e}")))?;
    for entry_res in entries {
        let mut entry = entry_res
            .map_err(|e| DefsUpdateError::Definition(format!("Corrupt tar entry: {e}")))?;
        if !entry.header().entry_type().is_file() {
            continue;
        }
        let path = entry
            .path()
            .map_err(|e| DefsUpdateError::Definition(format!("Bad member path: {e}")))?
            .into_owned();
        let name_str = path.to_string_lossy().into_owned();
        if name_str.starts_with('/')
            || path
                .components()
                .any(|c| matches!(c, std::path::Component::ParentDir))
        {
            return Err(DefsUpdateError::Definition(format!(
                "The pack contains an unsafe path: {name_str}"
            )));
        }
        if !name_str.ends_with(".yaml") {
            continue;
        }
        if defs.len() >= MAX_DEFINITIONS {
            return Err(DefsUpdateError::Definition(format!(
                "The pack holds more than {MAX_DEFINITIONS} definitions."
            )));
        }
        let basename = path
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        if defs.contains_key(&basename) {
            return Err(DefsUpdateError::Definition(format!(
                "The pack contains two definitions named {basename} ({name_str})."
            )));
        }
        let mut buf = Vec::new();
        (&mut entry)
            .take((MAX_DEFINITION_BYTES + 1) as u64)
            .read_to_end(&mut buf)
            .map_err(|e| DefsUpdateError::Definition(e.to_string()))?;
        if buf.len() > MAX_DEFINITION_BYTES {
            return Err(DefsUpdateError::Definition(format!(
                "Definition {basename} exceeds size limit."
            )));
        }
        let text = String::from_utf8(buf)
            .map_err(|_| DefsUpdateError::Definition(format!("{basename} is not valid UTF-8.")))?;
        defs.insert(basename, text);
    }
    if defs.is_empty() {
        return Err(DefsUpdateError::Definition(
            "The pack contains no definitions.".to_owned(),
        ));
    }
    Ok(defs)
}

pub fn validate_pack_defs(defs: &BTreeMap<String, String>) -> Result<(), DefsUpdateError> {
    for (fname, text) in defs {
        let parsed = parse_yaml_def(text)
            .map_err(|e| DefsUpdateError::Definition(format!("{fname}: {e}")))?;
        for &req in REQUIRED_KEYS {
            if !parsed.contains_key(req) {
                return Err(DefsUpdateError::Definition(format!(
                    "{fname} is missing required key '{req}'."
                )));
            }
        }
        let adapter = normalize_adapter(&parsed)
            .map_err(|e| DefsUpdateError::Definition(format!("{fname}: {e}")))?;
        if adapter.schema_version > SUPPORTED_SCHEMA_VERSION {
            return Err(DefsUpdateError::Definition(format!(
                "{fname} declares unsupported schema_version {}.",
                adapter.schema_version
            )));
        }
    }
    Ok(())
}

pub fn status(root_dir: &Path, data_dir: &Path) -> Value {
    let bundled_dir = root_dir.join("emulator_defs");
    let local_dir = local_defs_dir(data_dir);
    let state = read_state(data_dir);
    let installed_set: HashSet<String> = state
        .get("installed")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .filter_map(|v| v.as_str().map(str::to_owned))
                .collect()
        })
        .unwrap_or_default();

    let bundled_count = fs::read_dir(&bundled_dir)
        .ok()
        .map(|rd| {
            rd.flatten()
                .filter(|e| e.path().extension().and_then(|s| s.to_str()) == Some("yaml"))
                .count()
        })
        .unwrap_or(0);

    let mut installed = Vec::new();
    let mut user_owned = Vec::new();
    if let Ok(rd) = fs::read_dir(&local_dir) {
        let mut names: Vec<String> = rd
            .flatten()
            .filter(|e| e.path().extension().and_then(|s| s.to_str()) == Some("yaml"))
            .map(|e| e.file_name().to_string_lossy().into_owned())
            .collect();
        names.sort();
        for name in names {
            if installed_set.contains(&name) {
                installed.push(name);
            } else {
                user_owned.push(name);
            }
        }
    }

    json!({
        "bundled_count": bundled_count,
        "installed": installed,
        "user_owned": user_owned,
        "pack_version": state.get("pack_version").and_then(Value::as_str).unwrap_or(""),
        "installed_at": state.get("installed_at").and_then(Value::as_str).unwrap_or(""),
        "can_rollback": !installed.is_empty(),
        "local_dir": local_dir.to_string_lossy(),
    })
}

pub async fn check(data_dir: &Path) -> Value {
    let state = read_state(data_dir);
    let current = state
        .get("pack_version")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    let Ok(client) = build_public_http_client(20) else {
        return json!({
            "available": false,
            "current": current,
            "latest": "",
            "published": false,
            "error": "Could not initialize HTTP client.",
        });
    };
    let resp = client
        .get(PACK_INDEX)
        .header(
            "User-Agent",
            format!("OpenBox/{APP_VERSION} definition channel"),
        )
        .send()
        .await;
    match resp {
        Ok(r) if r.status().as_u16() == 404 => json!({
            "available": false,
            "current": current,
            "latest": "",
            "published": false,
            "message": "No community definition pack has been published yet.",
        }),
        Ok(r) if r.status().is_success() => {
            let doc = r.json::<Value>().await.unwrap_or_else(|_| json!({}));
            let latest = doc
                .get("version")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            let available = !latest.is_empty() && latest != current;
            json!({
                "available": available,
                "current": current,
                "latest": latest,
                "published": true,
            })
        }
        Ok(r) => json!({
            "available": false,
            "current": current,
            "latest": "",
            "published": false,
            "error": format!("HTTP {}", r.status().as_u16()),
        }),
        Err(err) => json!({
            "available": false,
            "current": current,
            "latest": "",
            "published": false,
            "error": err.to_string(),
        }),
    }
}

pub async fn install(root_dir: &Path, data_dir: &Path) -> Result<Value, DefsUpdateError> {
    let key_path = root_dir.join("openbox-release.pub");
    let key_text = fs::read_to_string(&key_path).map_err(|_| {
        DefsUpdateError::Signature(format!(
            "The release key {} is missing.",
            key_path.file_name().unwrap_or_default().to_string_lossy()
        ))
    })?;
    let client =
        build_public_http_client(20).map_err(|e| DefsUpdateError::Definition(e.to_string()))?;
    let ua = format!("OpenBox/{APP_VERSION} definition channel");
    let archive_resp = client
        .get(PACK_ARCHIVE)
        .header("User-Agent", &ua)
        .send()
        .await
        .map_err(|e| DefsUpdateError::Definition(format!("Could not fetch pack: {e}")))?;
    if !archive_resp.status().is_success() {
        return Err(DefsUpdateError::Definition(format!(
            "Pack download failed with HTTP {}.",
            archive_resp.status().as_u16()
        )));
    }
    let archive_bytes = archive_resp
        .bytes()
        .await
        .map_err(|e| DefsUpdateError::Definition(e.to_string()))?;
    if archive_bytes.len() > MAX_PACK_BYTES {
        return Err(DefsUpdateError::Definition(
            "The definition pack is larger than the allowed size.".to_owned(),
        ));
    }
    let sig_resp = client
        .get(format!("{PACK_ARCHIVE}.sig"))
        .header("User-Agent", &ua)
        .send()
        .await
        .map_err(|e| DefsUpdateError::Signature(format!("Could not fetch signature: {e}")))?;
    if !sig_resp.status().is_success() {
        return Err(DefsUpdateError::Signature(format!(
            "Signature download failed with HTTP {}.",
            sig_resp.status().as_u16()
        )));
    }
    let sig_text = sig_resp.text().await.map_err(|_| {
        DefsUpdateError::Signature("The pack signature is not valid UTF-8.".to_owned())
    })?;

    verify_ed25519_artifact(&archive_bytes, &sig_text, &key_text)?;
    let defs = read_pack(&archive_bytes)?;
    validate_pack_defs(&defs)?;

    let target_dir = local_defs_dir(data_dir);
    fs::create_dir_all(&target_dir).map_err(|e| DefsUpdateError::Definition(e.to_string()))?;
    let prev = read_state(data_dir);
    let prev_owned: HashSet<String> = prev
        .get("installed")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(|v| v.as_str().map(str::to_owned))
                .collect()
        })
        .unwrap_or_default();

    let mut installed = Vec::new();
    let mut skipped_user = Vec::new();
    for (fname, text) in &defs {
        let dst = target_dir.join(fname);
        if dst.exists() && !prev_owned.contains(fname) {
            skipped_user.push(fname.clone());
            continue;
        }
        atomic_write_text(&dst, text, 0o644)
            .map_err(|e| DefsUpdateError::Definition(e.to_string()))?;
        installed.push(fname.clone());
    }
    let state_payload = json!({
        "installed": installed,
        "installed_at": jiff::Timestamp::now().to_string(),
        "pack_version": hex::encode(&Sha256::digest(&archive_bytes)[..8]),
    });
    write_state(data_dir, &state_payload)?;
    Ok(json!({
        "ok": true,
        "installed": installed,
        "skipped_user_owned": skipped_user,
    }))
}

pub fn rollback(data_dir: &Path) -> Value {
    let state = read_state(data_dir);
    let local_dir = local_defs_dir(data_dir);
    let installed: Vec<String> = state
        .get("installed")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(|v| v.as_str().map(str::to_owned))
                .collect()
        })
        .unwrap_or_default();
    let mut removed = Vec::new();
    for fname in installed {
        if let Ok(clean) = openbox_platform::safe_identifier(&fname, "definition") {
            let path = local_dir.join(clean);
            if path.is_file() && fs::remove_file(&path).is_ok() {
                removed.push(fname);
            }
        }
    }
    let _ = fs::remove_file(data_dir.join(STATE_FILE));
    json!({
        "ok": true,
        "removed": removed,
        "count": removed.len(),
    })
}
