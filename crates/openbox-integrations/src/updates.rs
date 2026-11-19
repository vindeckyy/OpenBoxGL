//! Verified GitHub release updates and desktop entry integration (`updates.py`).

use std::collections::HashMap;
use std::fs::{self, File};
use std::io::Read;
use std::path::Path;
use std::sync::LazyLock;

use base64::Engine;
use base64::engine::general_purpose::STANDARD as B64;
use ed25519_dalek::{Signature, VerifyingKey};
use openbox_core::APP_VERSION as VERSION;
use openbox_io::{
    DownloadOptions, atomic_write_bytes, atomic_write_text, download_file, fsync_directory,
    github_token_from_env, set_mode,
};
use openbox_metadata::http::{block_on_io, http_get_raw};
use openbox_platform::{IS_MACOS, IS_WINDOWS, expand_user, home_dir};
use regex::Regex;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

pub const RELEASE_API: &str = "https://api.github.com/repos/vindeckyy/OpenBoxGL/releases/latest";
pub const TRUSTED_RELEASE_PREFIX: &str =
    "https://github.com/vindeckyy/OpenBoxGL/releases/download/";

/// Legacy bootstrap placeholder public key (must always be rejected).
pub const PLACEHOLDER_PUBLIC_KEY: [u8; 32] = [
    0x9d, 0xf1, 0xf9, 0xe7, 0xcd, 0xba, 0x09, 0x4a, 0xc9, 0xd8, 0x58, 0xd5, 0x41, 0xb7, 0x52, 0x9c,
    0x28, 0x32, 0x9a, 0x30, 0x9f, 0xf7, 0x9a, 0x48, 0x12, 0x45, 0x7e, 0xb3, 0xf2, 0x59, 0xfa, 0x8d,
];

pub const EMBEDDED_PUBLIC_KEY: &[u8] = include_bytes!("../../../openbox-release.pub");

static VERSION_KEY_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^v?(\d+)\.(\d+)\.(\d+)([-+].*)?$").unwrap());
static HEX64_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[0-9a-f]{64}$").unwrap());

pub fn current_arch() -> &'static str {
    match std::env::consts::ARCH {
        "x86_64" | "amd64" => "x86_64",
        "aarch64" | "arm64" => "aarch64",
        other => other,
    }
}

pub fn arch_asset(arch: &str) -> String {
    if IS_WINDOWS {
        let tag = if arch == "aarch64" { "arm64" } else { arch };
        return format!("OpenBox-{tag}-windows.zip");
    }
    if IS_MACOS {
        return "OpenBox-macos.zip".to_owned();
    }
    format!("OpenBox-{arch}.AppImage")
}

pub fn version_key(value: &str) -> Result<(u64, u64, u64, u8), String> {
    let caps = VERSION_KEY_RE
        .captures(value.trim())
        .ok_or_else(|| "The release has an invalid version.".to_owned())?;
    let maj: u64 = caps[1]
        .parse()
        .map_err(|_| "The release has an invalid version.".to_owned())?;
    let min: u64 = caps[2]
        .parse()
        .map_err(|_| "The release has an invalid version.".to_owned())?;
    let pat: u64 = caps[3]
        .parse()
        .map_err(|_| "The release has an invalid version.".to_owned())?;
    let stable = u8::from(caps.get(4).is_none());
    Ok((maj, min, pat, stable))
}

fn github_get(url: &str, max_bytes: usize) -> Result<Vec<u8>, String> {
    let mut headers = vec![
        ("Accept", "application/vnd.github+json".to_owned()),
        ("User-Agent", format!("OpenBox/{VERSION}")),
    ];
    let tok = github_token_from_env();
    if !tok.is_empty() {
        headers.push(("Authorization", format!("Bearer {tok}")));
    }
    let resp = http_get_raw(url, &headers, max_bytes as u64, 30)
        .map_err(|e| format!("Could not reach GitHub releases: {e}"))?;
    if resp.status >= 400 {
        let detail: String = String::from_utf8_lossy(&resp.body)
            .chars()
            .take(200)
            .collect();
        return Err(format!(
            "GitHub releases request failed ({}): {}",
            resp.status, detail
        ));
    }
    Ok(resp.body)
}

pub fn verify_ed25519_strict(
    public_key_bytes: &[u8],
    signature_bytes: &[u8],
    message: &[u8],
) -> Result<(), String> {
    let key_arr: [u8; 32] = public_key_bytes
        .try_into()
        .map_err(|_| "The release public key is invalid.".to_owned())?;
    if key_arr == PLACEHOLDER_PUBLIC_KEY {
        return Err(
            "The committed OpenBox release public key is still the placeholder.".to_owned(),
        );
    }
    let verifying_key = VerifyingKey::from_bytes(&key_arr)
        .map_err(|_| "The release public key is invalid.".to_owned())?;
    if verifying_key.is_weak() {
        return Err("Invalid Ed25519 point: small-order point.".to_owned());
    }
    let sig_arr: [u8; 64] = signature_bytes
        .try_into()
        .map_err(|_| "The release signature has an invalid length.".to_owned())?;
    let sig = Signature::from_bytes(&sig_arr);
    verifying_key
        .verify_strict(message, &sig)
        .map_err(|_| "The release signature verification failed.".to_owned())
}

pub fn parse_signature_payload(raw: &[u8]) -> Result<(String, Vec<u8>), String> {
    let payload: Value =
        serde_json::from_slice(raw).map_err(|_| "The release signature is invalid.".to_owned())?;
    let Some(obj) = payload.as_object() else {
        return Err("The release signature is invalid.".to_owned());
    };
    let algo = obj.get("algorithm").and_then(Value::as_str).unwrap_or("");
    if algo != "ed25519" {
        return Err(format!(
            "The release signature uses an unsupported algorithm: '{algo}'"
        ));
    }
    if obj.get("digest_algorithm").and_then(Value::as_str) != Some("sha256") {
        return Err("The release signature is missing a SHA-256 digest.".to_owned());
    }
    let digest = obj
        .get("digest")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_lowercase();
    if !HEX64_RE.is_match(&digest) {
        return Err("The release signature is missing a valid digest.".to_owned());
    }
    let sig_b64 = obj.get("signature").and_then(Value::as_str).unwrap_or("");
    let sig = B64
        .decode(sig_b64)
        .map_err(|_| "The release signature is missing a valid signature.".to_owned())?;
    if sig.len() != 64 {
        return Err("The release signature has an invalid length.".to_owned());
    }
    Ok((digest, sig))
}

pub fn verify_artifact(
    artifact: &Path,
    signature_file: &Path,
    public_key_bytes: &[u8],
) -> Result<String, String> {
    let sig_raw = fs::read(signature_file).map_err(|e| e.to_string())?;
    let (expected_hex, sig) = parse_signature_payload(&sig_raw)?;
    let mut file = File::open(artifact).map_err(|e| e.to_string())?;
    let mut hasher = Sha256::new();
    let mut buf = [0u8; 65536];
    loop {
        let n = file.read(&mut buf).map_err(|e| e.to_string())?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    let actual_bytes = hasher.finalize();
    let actual_hex = hex::encode(actual_bytes);
    if actual_hex != expected_hex {
        return Err("The artifact does not match the signed release digest.".to_owned());
    }
    verify_ed25519_strict(public_key_bytes, &sig, &actual_bytes)?;
    Ok(actual_hex)
}

pub fn release_public_key(app_root: Option<&Path>) -> Option<Vec<u8>> {
    if let Some(root) = app_root {
        let on_disk = root.join("openbox-release.pub");
        if let Ok(bytes) = fs::read(on_disk)
            && bytes.len() == 32
        {
            return Some(bytes);
        }
    }
    if EMBEDDED_PUBLIC_KEY.len() == 32 {
        return Some(EMBEDDED_PUBLIC_KEY.to_vec());
    }
    None
}

pub fn check_update() -> Result<Value, String> {
    let body = github_get(RELEASE_API, 8 * 1024 * 1024)?;
    let release: Value = serde_json::from_slice(&body)
        .map_err(|_| "The GitHub releases payload is invalid.".to_owned())?;
    let Some(obj) = release.as_object() else {
        return Err("The GitHub releases payload is invalid.".to_owned());
    };
    let version = obj.get("tag_name").and_then(Value::as_str).unwrap_or("");
    let asset_name = arch_asset(current_arch());
    let sig_asset = format!("{asset_name}.sig");
    let sha_asset = format!("{asset_name}.sha256");

    let mut urls: HashMap<String, String> = HashMap::new();
    let mut digests: HashMap<String, String> = HashMap::new();
    if let Some(assets) = obj.get("assets").and_then(Value::as_array) {
        for asset in assets {
            let Some(aobj) = asset.as_object() else {
                continue;
            };
            let name = aobj
                .get("name")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim()
                .to_owned();
            if name.is_empty() {
                continue;
            }
            if let Some(u) = aobj
                .get("browser_download_url")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|s| !s.is_empty())
            {
                urls.insert(name.clone(), u.to_owned());
            }
            if let Some(d) = aobj.get("digest").and_then(Value::as_str)
                && let Some(hex_val) = d.trim().strip_prefix("sha256:")
            {
                let lower = hex_val.to_lowercase();
                if HEX64_RE.is_match(&lower) {
                    digests.insert(name, lower);
                }
            }
        }
    }

    let artifact = urls.get(&asset_name).cloned().unwrap_or_default();
    let checksum = digests.get(&asset_name).cloned().unwrap_or_default();
    let checksum_url = urls.get(&sha_asset).cloned().unwrap_or_default();
    let sig_url = urls.get(&sig_asset).cloned().unwrap_or_default();

    let mut available = match (version_key(version), version_key(VERSION)) {
        (Ok(latest), Ok(cur)) => latest > cur,
        _ => false,
    };
    if available && (version.contains('-') || version.contains('+')) {
        available = false;
    }
    if available && !artifact.starts_with(TRUSTED_RELEASE_PREFIX) {
        return Err("The release is missing verified OpenBox update assets.".to_owned());
    }
    if available && checksum.is_empty() && checksum_url.is_empty() {
        return Err("The release is missing a SHA-256 checksum for the download.".to_owned());
    }
    if available && sig_url.is_empty() {
        return Err("The release is missing an Ed25519 signature.".to_owned());
    }
    if available && !sig_url.starts_with(TRUSTED_RELEASE_PREFIX) {
        return Err("The release signature URL is not a trusted OpenBox release asset.".to_owned());
    }
    let notes: String = obj
        .get("body")
        .and_then(Value::as_str)
        .unwrap_or("")
        .chars()
        .take(4000)
        .collect();
    Ok(json!({
        "current": VERSION,
        "latest": version.trim_start_matches('v'),
        "available": available,
        "notes": notes,
        "artifact": artifact,
        "checksum": checksum,
        "checksum_url": checksum_url,
        "sig": !sig_url.is_empty(),
        "sig_url": sig_url,
        "page": obj.get("html_url").and_then(Value::as_str).unwrap_or(""),
    }))
}

pub fn install_update(update: &Value, app_root: Option<&Path>) -> Result<Value, String> {
    if !update
        .get("available")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        return Err("OpenBox is already up to date.".to_owned());
    }
    if IS_MACOS {
        return Err("Automatic updates require the OpenBox.app bundle.".to_owned());
    }
    if IS_WINDOWS {
        return Err("Automatic updates require an installed OpenBox copy.".to_owned());
    }
    let raw_dest = std::env::var("APPIMAGE").unwrap_or_default();
    if raw_dest.is_empty() {
        return Err("Automatic updates require the OpenBox AppImage.".to_owned());
    }
    let dest = fs::canonicalize(expand_user(Path::new(&raw_dest)))
        .map_err(|_| "Automatic updates require the OpenBox AppImage.".to_owned())?;
    if !dest.is_file() {
        return Err("Automatic updates require the OpenBox AppImage.".to_owned());
    }
    let artifact = update
        .get("artifact")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if !artifact.starts_with(TRUSTED_RELEASE_PREFIX) {
        return Err("The update URLs are not trusted OpenBox release assets.".to_owned());
    }
    let mut expected = update
        .get("checksum")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_lowercase();
    if !HEX64_RE.is_match(&expected) {
        let c_url = update
            .get("checksum_url")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        if !c_url.starts_with(TRUSTED_RELEASE_PREFIX) {
            return Err("The release checksum is unavailable.".to_owned());
        }
        let raw = github_get(c_url, 4096)?;
        let token = String::from_utf8_lossy(&raw)
            .split_whitespace()
            .next()
            .unwrap_or("")
            .to_lowercase();
        if !HEX64_RE.is_match(&token) {
            return Err("The release checksum is invalid.".to_owned());
        }
        expected = token;
    }

    // Verify Ed25519 signature BEFORE downloading artifact
    let sig_url = update
        .get("sig_url")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if sig_url.is_empty() {
        return Err("The release is unsigned; refusing to install it.".to_owned());
    }
    if !sig_url.starts_with(TRUSTED_RELEASE_PREFIX) {
        return Err("The release signature URL is not a trusted OpenBox release asset.".to_owned());
    }
    let pubkey = release_public_key(app_root).ok_or_else(|| {
        "The committed OpenBox release public key is unavailable or invalid.".to_owned()
    })?;
    let sig_raw = github_get(sig_url, 64 * 1024)?;
    let (sig_digest, sig_bytes) = parse_signature_payload(&sig_raw)?;
    if sig_digest != expected {
        return Err("The release signature does not match the artifact digest.".to_owned());
    }
    let digest_bytes = hex::decode(&expected).map_err(|_| "Invalid checksum hex.".to_owned())?;
    verify_ed25519_strict(&pubkey, &sig_bytes, &digest_bytes)?;

    let fname = dest
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("OpenBox.AppImage");
    let temporary = dest.with_file_name(format!(".{fname}.update"));
    let opts = DownloadOptions {
        max_bytes: 512 * 1024 * 1024,
        timeout_secs: 60,
        expected_sha256: Some(expected.as_str()),
        ..DownloadOptions::default()
    };
    if let Err(err) = block_on_io(async { download_file(artifact, &temporary, &opts).await }) {
        let _ = fs::remove_file(&temporary);
        return Err(err.to_string());
    }
    let _ = set_mode(&temporary, 0o755);
    let stem = dest
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("OpenBox");
    let ext = dest
        .extension()
        .and_then(|e| e.to_str())
        .map(|e| format!(".{e}"))
        .unwrap_or_default();
    let backup = dest.with_file_name(format!("{stem}.previous{ext}"));
    if backup.exists() {
        let _ = fs::remove_file(&backup);
    }
    if let Err(err) = fs::rename(&dest, &backup) {
        let _ = fs::remove_file(&temporary);
        return Err(err.to_string());
    }
    if let Err(err) = fs::rename(&temporary, &dest) {
        let _ = fs::rename(&backup, &dest);
        let _ = fs::remove_file(&temporary);
        return Err(err.to_string());
    }
    if let Some(parent) = dest.parent() {
        fsync_directory(parent);
    }
    Ok(json!({
        "installed": update.get("latest").and_then(Value::as_str).unwrap_or(""),
        "backup": backup.to_string_lossy(),
    }))
}

pub fn install_desktop_entry(app_root: &Path) -> Result<String, String> {
    if IS_MACOS {
        return Err(
            "macOS integrates through the app bundle; no desktop entry is needed.".to_owned(),
        );
    }
    if IS_WINDOWS {
        return Err("Desktop integration requires the OpenBox launcher.".to_owned());
    }
    let raw_appimage = std::env::var("APPIMAGE").unwrap_or_default();
    let appimage = expand_user(Path::new(&raw_appimage));
    if raw_appimage.is_empty() || !appimage.is_file() {
        return Err("Desktop integration requires the OpenBox AppImage.".to_owned());
    }
    let raw_exec = appimage.to_string_lossy().into_owned();
    if raw_exec.contains('\n') {
        return Err("The AppImage path is not valid for a desktop entry.".to_owned());
    }
    let escaped = raw_exec
        .replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('`', "\\`")
        .replace('$', "\\$")
        .replace('%', "%%");
    let home = home_dir().unwrap_or_else(|| std::path::PathBuf::from("."));
    let applications = home.join(".local/share/applications");
    let icons = home.join(".local/share/icons/hicolor/scalable/apps");
    fs::create_dir_all(&applications).map_err(|e| e.to_string())?;
    fs::create_dir_all(&icons).map_err(|e| e.to_string())?;

    let svg_src = app_root.join("openbox.svg");
    if let Ok(svg_bytes) = fs::read(&svg_src) {
        let _ = atomic_write_bytes(
            &icons.join("io.openbox.GameLauncher.svg"),
            &svg_bytes,
            0o644,
        );
    }
    let desktop = applications.join("io.openbox.GameLauncher.desktop");
    let entry = format!(
        "[Desktop Entry]\n\
         Name=OpenBox\n\
         Comment=Local-first Linux game library and launcher\n\
         Exec=\"{escaped}\"\n\
         Icon=io.openbox.GameLauncher\n\
         Terminal=false\n\
         Type=Application\n\
         Categories=Game;Emulator;\n\
         Keywords=games;launcher;emulator;rom;\n"
    );
    atomic_write_text(&desktop, &entry, 0o755).map_err(|e| e.to_string())?;
    let _ = set_mode(&desktop, 0o755);
    Ok(desktop.to_string_lossy().into_owned())
}
