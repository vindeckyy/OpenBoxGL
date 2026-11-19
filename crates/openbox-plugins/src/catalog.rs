//! Bundled and pinned community plugin catalog (`plugin_catalog.py`).

use std::fs;
use std::path::{Path, PathBuf};

use openbox_io::{DownloadOptions, download_file};
use openbox_metadata::http::{block_on_io, http_get_raw};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use url::Url;

use crate::host::is_valid_plugin_id;

pub const REMOTE_CATALOG: &str = "https://raw.githubusercontent.com/vindeckyy/OpenBoxGL/566f57e276cd5fffb587675c970bc86f50dfbccb/plugins/catalog.json";
pub const REMOTE_CATALOG_SHA256: &str =
    "8569fa78415cdd5b0042cfef36ed8ef40fe9df5cea3f536f1c6e5d79f5d95a78";
const BUNDLED_CATALOG_JSON: &str = include_str!("../../../plugins/catalog.json");

fn is_sha256_hex(s: &str) -> bool {
    s.len() == 64 && s.bytes().all(|b| b.is_ascii_hexdigit())
}

pub fn validate_download_url(raw_url: &str, plugin_id: &str) -> Result<Url, String> {
    let parsed = Url::parse(raw_url)
        .map_err(|_| format!("Catalog entry {plugin_id} has an invalid download URL."))?;
    if !parsed.scheme().eq_ignore_ascii_case("https")
        || parsed.host_str().is_none()
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || parsed.fragment().is_some()
    {
        return Err(format!(
            "Catalog entry {plugin_id} must use an HTTPS download URL."
        ));
    }
    Ok(parsed)
}

pub fn validate_entry(entry: &Value) -> Result<Value, String> {
    let Some(obj) = entry.as_object() else {
        return Err("The plugin catalog contains a malformed entry.".to_owned());
    };
    let plugin_id = obj
        .get("id")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_owned();
    if !is_valid_plugin_id(&plugin_id) {
        return Err("The plugin catalog contains an invalid plugin id.".to_owned());
    }
    let name = obj.get("name").and_then(Value::as_str).unwrap_or("").trim();
    let version = obj
        .get("version")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if name.is_empty() || version.is_empty() {
        return Err(format!(
            "Catalog entry {plugin_id} is missing a name or version."
        ));
    }
    let url = obj
        .get("url")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_owned();
    let local_only = obj
        .get("local_only")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if !url.is_empty() {
        validate_download_url(&url, &plugin_id)?;
        let sha256 = obj
            .get("sha256")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        if !is_sha256_hex(sha256) {
            return Err(format!(
                "Catalog entry {plugin_id} is missing a valid sha256 checksum."
            ));
        }
    } else if !local_only {
        return Err(format!("Catalog entry {plugin_id} has no download URL."));
    }
    let author = obj
        .get("author")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_owned();
    if author.len() > 80 {
        return Err(format!(
            "Catalog entry {plugin_id} has an author longer than 80 characters."
        ));
    }
    let homepage = obj
        .get("homepage")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_owned();
    if !homepage.is_empty() {
        validate_download_url(&homepage, &plugin_id)?;
        if homepage.len() > 300 {
            return Err(format!(
                "Catalog entry {plugin_id} has a homepage URL longer than 300 characters."
            ));
        }
    }
    let mut cleaned: Map<String, Value> = obj.clone();
    cleaned.insert("id".to_owned(), Value::String(plugin_id));
    cleaned.insert("url".to_owned(), Value::String(url));
    cleaned.insert("author".to_owned(), Value::String(author));
    cleaned.insert("homepage".to_owned(), Value::String(homepage));
    Ok(Value::Object(cleaned))
}

pub fn validate_catalog(payload: &Value) -> Result<Vec<Value>, String> {
    let Some(arr) = payload.as_array() else {
        return Err("The plugin catalog must be a list.".to_owned());
    };
    arr.iter().map(validate_entry).collect()
}

pub fn load_local_catalog(app_root: Option<&Path>) -> Vec<Value> {
    if let Some(root) = app_root {
        let on_disk = root.join("plugins").join("catalog.json");
        if let Ok(raw) = fs::read_to_string(&on_disk)
            && let Ok(parsed) = serde_json::from_str::<Value>(&raw)
            && let Ok(valid) = validate_catalog(&parsed)
        {
            return valid;
        }
    }
    serde_json::from_str::<Value>(BUNDLED_CATALOG_JSON)
        .ok()
        .and_then(|v| validate_catalog(&v).ok())
        .unwrap_or_default()
}

pub fn fetch_plugin_catalog(app_root: Option<&Path>) -> Vec<Value> {
    let headers = [("User-Agent", "OpenBox/1".to_owned())];
    if let Ok(resp) = http_get_raw(REMOTE_CATALOG, &headers, 4 * 1024 * 1024, 20)
        && resp.status == 200
    {
        let digest = hex::encode(Sha256::digest(&resp.body));
        if digest == REMOTE_CATALOG_SHA256
            && let Ok(parsed) = serde_json::from_slice::<Value>(&resp.body)
            && let Ok(valid) = validate_catalog(&parsed)
            && !valid.is_empty()
        {
            return valid;
        }
    }
    load_local_catalog(app_root)
}

pub fn download_plugin_package(entry: &Value, dest_dir: &Path) -> Result<PathBuf, String> {
    let Some(obj) = entry.as_object() else {
        return Err("The catalog entry is invalid.".to_owned());
    };
    let plugin_id = obj
        .get("id")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_owned();
    if !is_valid_plugin_id(&plugin_id) {
        return Err("The catalog entry has an invalid plugin id.".to_owned());
    }
    let url = obj
        .get("url")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_owned();
    if url.is_empty() {
        return Err("This catalog entry has no download URL.".to_owned());
    }
    validate_download_url(&url, &plugin_id)?;
    let sha256 = obj
        .get("sha256")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_owned();
    if !is_sha256_hex(&sha256) {
        return Err(format!(
            "Catalog entry {plugin_id} is missing a valid sha256 checksum; refusing to download."
        ));
    }
    fs::create_dir_all(dest_dir).map_err(|e| e.to_string())?;
    let archive = dest_dir.join(format!("{plugin_id}.zip"));
    let opts = DownloadOptions {
        max_bytes: 128 * 1024 * 1024,
        timeout_secs: 120,
        expected_sha256: Some(sha256.as_str()),
        ..DownloadOptions::default()
    };
    block_on_io(async { download_file(&url, &archive, &opts).await }).map_err(|e| e.to_string())?;
    Ok(archive)
}
