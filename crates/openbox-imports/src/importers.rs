//! Installed storefront importers (`importers.py`): Steam, Heroic, Epic, Lutris.

use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::LazyLock;

use openbox_core::ApiError;
use openbox_platform::{
    IS_MACOS, IS_WINDOWS, epic_manifest_paths, flatpak_app_installed, home_dir, join_command,
    steam_candidate_roots, steam_install_roots, which,
};
use regex::Regex;
use serde_json::{Map, Value, json};

static VDF_KV_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#""([^"]+)"\s+"([^"]*)""#).expect("valid vdf kv regex"));
static VDF_PATH_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#""path"\s+"([^"]+)""#).expect("valid vdf path regex"));

pub fn url_opener_command() -> Option<String> {
    if IS_MACOS {
        which("open").map(|p| p.to_string_lossy().into_owned())
    } else if IS_WINDOWS {
        let root = std::env::var("SYSTEMROOT").unwrap_or_else(|_| r"C:\Windows".to_owned());
        let exe = PathBuf::from(root).join("explorer.exe");
        if exe.is_file() {
            Some(exe.to_string_lossy().into_owned())
        } else {
            which("explorer.exe").map(|p| p.to_string_lossy().into_owned())
        }
    } else {
        which("xdg-open").map(|p| p.to_string_lossy().into_owned())
    }
}

pub fn url_open_command(url: &str, opener: Option<&str>) -> Vec<String> {
    let handler = opener.map(ToOwned::to_owned).or_else(url_opener_command);
    match handler {
        Some(h) if !h.is_empty() => vec![h, url.to_owned()],
        _ => Vec::new(),
    }
}

pub fn vdf_values(text: &str) -> std::collections::HashMap<String, String> {
    let mut map = std::collections::HashMap::new();
    for cap in VDF_KV_RE.captures_iter(text) {
        if let (Some(k), Some(v)) = (cap.get(1), cap.get(2)) {
            map.insert(k.as_str().to_owned(), v.as_str().to_owned());
        }
    }
    map
}

pub fn steam_roots(home: Option<&Path>) -> Vec<PathBuf> {
    let candidates = match home {
        Some(h) => steam_candidate_roots(h),
        None => steam_install_roots(),
    };
    candidates
        .into_iter()
        .filter(|p| p.join("steamapps").is_dir())
        .collect()
}

pub fn steam_command(home: Option<&Path>) -> Result<(String, String), ApiError> {
    if IS_MACOS {
        let found = if let Some(h) = home {
            steam_roots(Some(h))
        } else {
            steam_install_roots()
        };
        if found.is_empty() {
            return Err(ApiError::not_found(
                "The Steam installation could not be found.",
            ));
        }
        let handler = url_opener_command().ok_or_else(|| {
            ApiError::not_found("No application is available to open Steam links.")
        })?;
        let cmd = join_command(&[handler.clone(), "steam://rungameid/{app_id}".to_owned()]);
        return Ok((handler, cmd));
    }
    if IS_WINDOWS {
        for root in steam_roots(home) {
            let exe = root.join("steam.exe");
            if exe.is_file() {
                let s = exe.to_string_lossy().into_owned();
                let cmd = join_command(&[s.clone(), "-applaunch".into(), "{app_id}".into()]);
                return Ok((s, cmd));
            }
        }
    }
    if let Some(binary) = which("steam") {
        return Ok((
            binary.to_string_lossy().into_owned(),
            "steam -applaunch {app_id}".to_owned(),
        ));
    }
    if flatpak_app_installed("com.valvesoftware.Steam")
        && let Some(fp) = which("flatpak")
    {
        return Ok((
            fp.to_string_lossy().into_owned(),
            "flatpak run com.valvesoftware.Steam -applaunch {app_id}".to_owned(),
        ));
    }
    if let Some(handler) = url_opener_command() {
        let cmd = join_command(&[handler.clone(), "steam://rungameid/{app_id}".to_owned()]);
        return Ok((handler, cmd));
    }
    Err(ApiError::not_found(
        "Steam, the Steam Flatpak, or a URL opener is required to launch imported Steam games.",
    ))
}

pub fn steam_libraries(root: &Path) -> Vec<PathBuf> {
    let mut libs = std::collections::BTreeSet::new();
    libs.insert(root.to_path_buf());
    let file = root.join("steamapps/libraryfolders.vdf");
    if let Ok(text) = fs::read_to_string(&file) {
        for cap in VDF_PATH_RE.captures_iter(&text) {
            if let Some(m) = cap.get(1) {
                let raw = m.as_str().replace("\\\\", "\\");
                let lib = PathBuf::from(raw);
                if lib.join("steamapps").is_dir() {
                    libs.insert(lib);
                }
            }
        }
    }
    libs.into_iter().collect()
}

pub fn import_steam(home: Option<&Path>, errors: &mut Vec<String>) -> Vec<Value> {
    let (executable, command) = steam_command(home).unwrap_or_else(|_| {
        let argv = url_open_command("steam://rungameid/{app_id}", None);
        (String::new(), join_command(&argv))
    });
    let mut games = Vec::new();
    let mut seen = HashSet::new();
    for root in steam_roots(home) {
        for library in steam_libraries(&root) {
            let steamapps = library.join("steamapps");
            if !steamapps.is_dir() {
                continue;
            }
            let rd = match fs::read_dir(&steamapps) {
                Ok(r) => r,
                Err(err) => {
                    errors.push(format!("{}: {err}", steamapps.display()));
                    continue;
                }
            };
            for entry in rd.filter_map(Result::ok) {
                let fname = entry.file_name().to_string_lossy().into_owned();
                if !fname.starts_with("appmanifest_") || !fname.ends_with(".acf") {
                    continue;
                }
                let Ok(content) = fs::read_to_string(entry.path()) else {
                    continue;
                };
                let values = vdf_values(&content);
                let Some(app_id) = values.get("appid").filter(|s| !s.is_empty()) else {
                    continue;
                };
                let Some(name) = values.get("name").filter(|s| !s.is_empty()) else {
                    continue;
                };
                if !seen.insert(app_id.clone()) {
                    continue;
                }
                let install_dir = library
                    .join("steamapps/common")
                    .join(values.get("installdir").map(String::as_str).unwrap_or(""));
                games.push(json!({
                    "name": name,
                    "platform": "PC",
                    "source": "Steam",
                    "collection": "Steam",
                    "path": executable,
                    "launch": command,
                    "steam_app_id": app_id,
                    "install_dir": install_dir.to_string_lossy(),
                    "store_installed": install_dir.is_dir(),
                    "store_catalog": false,
                }));
            }
        }
    }
    games
}

pub fn json_records(path: &Path) -> Vec<(String, Map<String, Value>)> {
    let Ok(text) = fs::read_to_string(path) else {
        return Vec::new();
    };
    let Ok(data) = serde_json::from_str::<Value>(&text) else {
        return Vec::new();
    };
    match data {
        Value::Array(arr) => arr
            .into_iter()
            .enumerate()
            .filter_map(|(i, v)| v.as_object().cloned().map(|o| (i.to_string(), o)))
            .collect(),
        Value::Object(obj) => obj
            .into_iter()
            .filter_map(|(k, v)| v.as_object().cloned().map(|o| (k, o)))
            .collect(),
        _ => Vec::new(),
    }
}

pub fn heroic_bases(home: Option<&Path>) -> Vec<PathBuf> {
    if IS_MACOS {
        return Vec::new();
    }
    if let Some(h) = home {
        return [
            h.join(".config/heroic"),
            h.join(".var/app/com.heroicgameslauncher.hgl/config/heroic"),
        ]
        .into_iter()
        .filter(|p| p.is_dir())
        .collect();
    }
    let mut candidates = Vec::new();
    if IS_WINDOWS {
        if let Ok(appdata) = std::env::var("APPDATA") {
            candidates.push(PathBuf::from(appdata).join("heroic"));
        }
    } else if let Some(h) = home_dir() {
        candidates.push(h.join(".config/heroic"));
        candidates.push(h.join(".var/app/com.heroicgameslauncher.hgl/config/heroic"));
    }
    candidates.into_iter().filter(|p| p.is_dir()).collect()
}

pub fn import_heroic(home: Option<&Path>) -> Result<Vec<Value>, ApiError> {
    if IS_MACOS {
        return Err(ApiError::not_found(
            "Heroic import is not available on macOS.",
        ));
    }
    let home_path = home
        .map(Path::to_path_buf)
        .or_else(home_dir)
        .unwrap_or_else(|| PathBuf::from("."));
    let opener = url_opener_command()
        .ok_or_else(|| ApiError::not_found("No application is available to open Heroic links."))?;
    let mut manifests: Vec<(&str, &str, PathBuf)> = Vec::new();
    for base in heroic_bases(home) {
        manifests.push((
            "Epic",
            "legendary",
            base.join("legendaryConfig/legendary/installed.json"),
        ));
        manifests.push(("GOG", "gog", base.join("gog_store/installed.json")));
        manifests.push(("Amazon", "nile", base.join("nile_config/installed.json")));
    }
    manifests.push((
        "Epic",
        "legendary",
        home_path.join(".config/legendary/installed.json"),
    ));
    manifests.push((
        "Epic",
        "legendary",
        home_path.join(".var/app/com.heroicgameslauncher.hgl/config/legendary/installed.json"),
    ));
    let mut games = Vec::new();
    let mut seen = HashSet::new();
    for (source, runner, manifest) in manifests {
        for (key, record) in json_records(&manifest) {
            if record
                .get("is_dlc")
                .and_then(Value::as_bool)
                .unwrap_or(false)
            {
                continue;
            }
            let app_id = ["app_name", "appName", "product_id", "id"]
                .iter()
                .find_map(|k| {
                    record
                        .get(*k)
                        .and_then(Value::as_str)
                        .filter(|s| !s.is_empty())
                })
                .map(ToOwned::to_owned)
                .unwrap_or(key);
            let Some(title) = ["title", "app_title", "name"].iter().find_map(|k| {
                record
                    .get(*k)
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
            }) else {
                continue;
            };
            if !seen.insert((source.to_owned(), app_id.clone())) {
                continue;
            }
            let install_dir = ["install_path", "installPath", "path"]
                .iter()
                .find_map(|k| record.get(*k).and_then(Value::as_str))
                .unwrap_or("");
            games.push(json!({
                "name": title,
                "platform": "PC",
                "source": source,
                "collection": source,
                "path": opener,
                "launch": format!("xdg-open heroic://launch/{runner}/{{heroic_app_id}}"),
                "heroic_app_id": app_id,
                "install_dir": install_dir,
            }));
        }
    }
    Ok(games)
}

pub fn import_epic() -> Vec<Value> {
    let mut games = Vec::new();
    let mut seen = HashSet::new();
    for manifest in epic_manifest_paths() {
        let Ok(text) = fs::read_to_string(&manifest) else {
            continue;
        };
        let Ok(Value::Object(record)) = serde_json::from_str::<Value>(&text) else {
            continue;
        };
        if record
            .get("bIsIncompleteInstall")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            continue;
        }
        let app_id = record
            .get("AppName")
            .or_else(|| record.get("CatalogItemId"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_owned();
        let title = record
            .get("DisplayName")
            .or_else(|| record.get("AppName"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_owned();
        let install_dir = record
            .get("InstallLocation")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_owned();
        let executable = record
            .get("LaunchExecutable")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_owned();
        if app_id.is_empty() || title.is_empty() || install_dir.is_empty() || executable.is_empty()
        {
            continue;
        }
        if !seen.insert(app_id.clone()) {
            continue;
        }
        let launch_path = PathBuf::from(&install_dir).join(&executable);
        if !launch_path.is_file() {
            continue;
        }
        games.push(json!({
            "name": title,
            "platform": "PC",
            "source": "Epic",
            "collection": "Epic",
            "path": launch_path.to_string_lossy(),
            "launch": "",
            "heroic_app_id": app_id,
            "install_dir": install_dir,
        }));
    }
    games
}

pub fn lutris_command() -> Result<(Vec<String>, Option<String>), ApiError> {
    if let Some(binary) = which("lutris") {
        let s = binary.to_string_lossy().into_owned();
        return Ok((vec![s.clone()], Some(s)));
    }
    if let Some(fp) = which("flatpak")
        && flatpak_app_installed("net.lutris.Lutris")
    {
        return Ok((
            vec![
                fp.to_string_lossy().into_owned(),
                "run".into(),
                "net.lutris.Lutris".into(),
            ],
            None,
        ));
    }
    Err(ApiError::not_found(
        "Lutris or the Lutris Flatpak is required to import Lutris games.",
    ))
}

pub fn load_lutris_records(
    command: &[String],
    installed_only: bool,
) -> Result<Vec<Value>, ApiError> {
    let mut cmd = Command::new(&command[0]);
    for arg in &command[1..] {
        cmd.arg(arg);
    }
    cmd.arg("--list-games");
    if installed_only {
        cmd.arg("--installed");
    }
    cmd.arg("--json");
    let output = cmd
        .output()
        .map_err(|e| ApiError::bad_request(format!("Could not run Lutris: {e}")))?;
    if !output.status.success() {
        return Err(ApiError::bad_request(
            "Lutris returned an invalid game list.",
        ));
    }
    let text = String::from_utf8_lossy(&output.stdout).trim().to_owned();
    let slice = match (text.find('['), text.rfind(']')) {
        (Some(s), Some(e)) if e > s => &text[s..=e],
        _ => text.as_str(),
    };
    let parsed: Value = serde_json::from_str(slice)
        .map_err(|_| ApiError::bad_request("Lutris returned an invalid game list."))?;
    match parsed {
        Value::Array(arr) => Ok(arr),
        Value::Object(obj) => obj
            .get("games")
            .and_then(Value::as_array)
            .cloned()
            .ok_or_else(|| ApiError::bad_request("Lutris returned an invalid game list.")),
        _ => Err(ApiError::bad_request(
            "Lutris returned an invalid game list.",
        )),
    }
}

fn lutris_record_source(record: &Map<String, Value>) -> &'static str {
    let origin = format!(
        "{} {}",
        record.get("service").and_then(Value::as_str).unwrap_or(""),
        record.get("source").and_then(Value::as_str).unwrap_or("")
    )
    .to_lowercase();
    if origin.contains("xbox") || origin.contains("game pass") {
        "Xbox"
    } else if origin.contains("origin") || origin.contains("ea app") {
        "EA"
    } else if origin.contains("ubisoft") || origin.contains("uplay") {
        "Ubisoft"
    } else {
        "Lutris"
    }
}

fn lutris_cover_path(home: &Path, slug: &str) -> String {
    if slug.is_empty() {
        return String::new();
    }
    for base in [
        home.join(".local/share/lutris/coverart"),
        home.join(".var/app/net.lutris.Lutris/data/lutris/coverart"),
    ] {
        for suffix in [".jpg", ".png", ".webp"] {
            let p = base.join(format!("{slug}{suffix}"));
            if p.is_file() {
                return p.to_string_lossy().into_owned();
            }
        }
    }
    String::new()
}

pub fn import_lutris(home: Option<&Path>) -> Result<Vec<Value>, ApiError> {
    let home_path = home
        .map(Path::to_path_buf)
        .or_else(home_dir)
        .unwrap_or_else(|| PathBuf::from("."));
    let (command, binary) = lutris_command()?;
    let records = load_lutris_records(&command, true)?;
    let mut launch_argv = command.clone();
    launch_argv.push("lutris:rungameid/{lutris_id}".into());
    let launch_cmd = join_command(&launch_argv);
    let mut games = Vec::new();
    for rec in records {
        let Some(obj) = rec.as_object() else {
            continue;
        };
        if obj.get("installed") == Some(&Value::Bool(false)) {
            continue;
        }
        let game_id = match obj.get("id") {
            Some(Value::Number(n)) => n.to_string(),
            Some(Value::String(s)) => s.trim().to_owned(),
            _ => String::new(),
        };
        let name = obj
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_owned();
        if game_id.is_empty() || !game_id.chars().all(|c| c.is_ascii_digit()) || name.is_empty() {
            continue;
        }
        let source = lutris_record_source(obj);
        let runner = obj
            .get("runner")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        let slug = obj
            .get("slug")
            .or_else(|| obj.get("game_slug"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        let cover = lutris_cover_path(&home_path, slug);
        let install_dir = obj
            .get("directory")
            .or_else(|| obj.get("path"))
            .and_then(Value::as_str)
            .unwrap_or("");
        let platform = obj
            .get("platform")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .unwrap_or(if matches!(runner, "wine" | "winesteam") {
                "Windows"
            } else {
                "PC"
            });
        let path_val = binary
            .clone()
            .or_else(|| {
                if !install_dir.is_empty() {
                    Some(install_dir.to_owned())
                } else {
                    None
                }
            })
            .unwrap_or_else(|| command[0].clone());
        let mut game = json!({
            "name": name,
            "platform": platform,
            "source": source,
            "collection": source,
            "path": path_val,
            "launch": launch_cmd,
            "lutris_id": game_id,
            "install_dir": install_dir,
        });
        if !cover.is_empty()
            && let Some(m) = game.as_object_mut()
        {
            m.insert("cover".into(), Value::String(cover));
        }
        games.push(game);
    }
    Ok(games)
}
