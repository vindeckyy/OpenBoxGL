//! RetroAchievements account, exact per-console ROM MD5/SHA1 hashing, matching, progress,
//! and emulator credential injection (`retroachievements.py`, `parity_integrations.py`,
//! `parity_premium.py`).

use std::collections::{BTreeMap, HashSet};
use std::fs::{self, File};
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::LazyLock;
use std::time::{SystemTime, UNIX_EPOCH};

use md5::{Digest as _, Md5};
use openbox_io::{atomic_write_text, retroachievements_from_env};
use openbox_platform::{home_dir, which};
use regex::Regex;
use serde_json::{Map, Value, json};
use tempfile::TempDir;
use zip::ZipArchive;

use crate::http::{http_get_raw, urlencode};

pub const MAX_ROM_BYTES: u64 = 512 * 1024 * 1024;

static CUE_FILE_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#"(?i)^\s*FILE\s+"([^"]+)""#).unwrap());
static SHA1_HEX_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\b([0-9a-fA-F]{40})\b").unwrap());
static BADGE_STRIP_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[^A-Za-z0-9_-]").unwrap());

pub fn system_names(platform_lower: &str) -> Option<&'static [&'static str]> {
    match platform_lower {
        "nes" | "nintendo entertainment system" => {
            Some(&["Nintendo Entertainment System", "NES/Famicom"])
        }
        "snes" | "super nintendo entertainment system" => {
            Some(&["Super Nintendo Entertainment System", "SNES/Super Famicom"])
        }
        "game boy" => Some(&["Game Boy"]),
        "game boy color" => Some(&["Game Boy Color"]),
        "game boy advance" => Some(&["Game Boy Advance"]),
        "nintendo 64" => Some(&["Nintendo 64"]),
        "arcade" => Some(&["Arcade"]),
        "sega genesis" | "mega drive" => Some(&["Mega Drive"]),
        "master system" => Some(&["Master System"]),
        "game gear" => Some(&["Game Gear"]),
        "atari 2600" => Some(&["Atari 2600"]),
        "atari 7800" => Some(&["Atari 7800"]),
        "atari lynx" => Some(&["Atari Lynx"]),
        "pc engine" | "turbografx-16" => Some(&["PC Engine"]),
        "playstation" | "sony playstation" | "ps1" => Some(&["PlayStation"]),
        "gamecube" | "nintendo gamecube" => Some(&["GameCube"]),
        "wii" | "nintendo wii" | "wiiware" | "nintendo wiiware" => Some(&["Wii"]),
        _ => None,
    }
}

fn is_raw_md5_platform(platform_lower: &str) -> bool {
    matches!(
        platform_lower,
        "game boy"
            | "game boy color"
            | "game boy advance"
            | "sega genesis"
            | "mega drive"
            | "master system"
            | "game gear"
            | "atari 2600"
            | "playstation"
            | "sony playstation"
            | "ps1"
    )
}

#[derive(Debug, Clone)]
pub struct RaCredentials {
    pub username: String,
    pub api_key: String,
}

pub fn api_get(
    endpoint: &str,
    params: &[(&str, String)],
    credentials: &RaCredentials,
) -> Result<Value, String> {
    let mut query = params.to_vec();
    query.push(("y", credentials.api_key.clone()));
    let url = format!(
        "https://retroachievements.org/API/{endpoint}?{}",
        urlencode(&query)
    );
    let resp = http_get_raw(
        &url,
        &[("User-Agent", "OpenBox/1".to_owned())],
        20,
        8 * 1024 * 1024,
    )?;
    if resp.status == 401 || resp.status == 403 {
        return Err("RetroAchievements rejected those credentials.".to_owned());
    }
    if resp.status >= 400 {
        return Err(format!("RetroAchievements returned HTTP {}.", resp.status));
    }
    serde_json::from_slice(&resp.body)
        .map_err(|e| format!("Invalid RetroAchievements response: {e}"))
}

pub fn load_credentials(data_dir: &Path) -> Option<RaCredentials> {
    let path = data_dir.join("retroachievements.json");
    if let Ok(raw) = fs::read_to_string(&path)
        && let Ok(val) = serde_json::from_str::<Value>(&raw)
    {
        let u = val
            .get("username")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_owned();
        let k = val
            .get("api_key")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_owned();
        if !u.is_empty() && !k.is_empty() {
            return Some(RaCredentials {
                username: u,
                api_key: k,
            });
        }
    }
    retroachievements_from_env().map(|(u, k)| RaCredentials {
        username: u,
        api_key: k,
    })
}

pub fn save_credentials(data_dir: &Path, username: &str, api_key: &str) -> Result<Value, String> {
    let creds = RaCredentials {
        username: username.trim().to_owned(),
        api_key: api_key.trim().to_owned(),
    };
    if creds.username.is_empty() || creds.api_key.is_empty() {
        return Err("RetroAchievements username and web API key are required.".to_owned());
    }
    let profile = api_get(
        "API_GetUserProfile.php",
        &[("u", creds.username.clone())],
        &creds,
    )?;
    if profile
        .get("User")
        .and_then(Value::as_str)
        .is_none_or(str::is_empty)
    {
        return Err("RetroAchievements rejected those credentials.".to_owned());
    }
    let path = data_dir.join("retroachievements.json");
    let body = serde_json::to_string(&json!({
        "username": creds.username,
        "api_key": creds.api_key,
    }))
    .map_err(|e| e.to_string())?;
    atomic_write_text(&path, &body, 0o600).map_err(|e| e.to_string())?;
    Ok(profile)
}

pub fn archive_rom_bytes(path: &Path) -> Result<Vec<u8>, String> {
    let ext = path
        .extension()
        .and_then(|e| e.to_str())
        .map(|s| format!(".{}", s.to_ascii_lowercase()))
        .unwrap_or_default();
    if ext == ".zip" {
        let file = File::open(path).map_err(|e| e.to_string())?;
        let mut archive = ZipArchive::new(file).map_err(|e| e.to_string())?;
        let mut largest_idx: Option<usize> = None;
        let mut largest_size = 0u64;
        for i in 0..archive.len() {
            let Ok(entry) = archive.by_index(i) else {
                continue;
            };
            if entry.is_dir() {
                continue;
            }
            if largest_idx.is_none() || entry.size() > largest_size {
                largest_idx = Some(i);
                largest_size = entry.size();
            }
        }
        let Some(idx) = largest_idx else {
            return Err("Archive contains no ROM files.".to_owned());
        };
        if largest_size > MAX_ROM_BYTES {
            return Err("ROM archive member is too large.".to_owned());
        }
        let entry = archive.by_index(idx).map_err(|e| e.to_string())?;
        let mut buf = Vec::with_capacity(largest_size.min(64 * 1024 * 1024) as usize);
        entry
            .take(MAX_ROM_BYTES + 1)
            .read_to_end(&mut buf)
            .map_err(|e| e.to_string())?;
        if buf.len() as u64 > MAX_ROM_BYTES {
            return Err("ROM archive member is too large.".to_owned());
        }
        return Ok(buf);
    }
    if ext == ".7z" {
        let out = Command::new("7z")
            .args(["e", "-so"])
            .arg(path)
            .output()
            .map_err(|_| "Install py7zr or 7z to hash ROMs inside .7z archives.".to_owned())?;
        if !out.status.success() || out.stdout.is_empty() {
            return Err("Install py7zr or 7z to hash ROMs inside .7z archives.".to_owned());
        }
        if out.stdout.len() as u64 > MAX_ROM_BYTES {
            return Err("ROM archive member is too large.".to_owned());
        }
        return Ok(out.stdout);
    }
    let meta =
        fs::metadata(path).map_err(|_| format!("Could not stat ROM file: {}", path.display()))?;
    if meta.len() > MAX_ROM_BYTES {
        return Err("ROM file is too large for hashing.".to_owned());
    }
    let data = fs::read(path).map_err(|e| e.to_string())?;
    if data.len() as u64 > MAX_ROM_BYTES {
        return Err("ROM file is too large for hashing.".to_owned());
    }
    Ok(data)
}

fn m3u_disc(path: &Path) -> Result<PathBuf, String> {
    let raw = fs::read_to_string(path)
        .map_err(|_| format!("Could not read playlist: {}", path.display()))?;
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    for line in raw.lines() {
        let mut cand = line.trim();
        if cand.is_empty() || cand.starts_with('#') {
            continue;
        }
        if cand.len() >= 2
            && ((cand.starts_with('"') && cand.ends_with('"'))
                || (cand.starts_with('\'') && cand.ends_with('\'')))
        {
            cand = &cand[1..cand.len() - 1];
        }
        let mut disc = PathBuf::from(cand);
        if !disc.is_absolute() {
            disc = parent.join(disc);
        }
        if disc.is_file() {
            return Ok(disc);
        }
    }
    Err("The M3U playlist does not reference an existing disc image.".to_owned())
}

fn run_hash_tool(argv: &[&str]) -> Result<String, String> {
    if which(argv[0]).is_none() {
        return Err(format!(
            "{} is required to hash this game's format.",
            argv[0]
        ));
    }
    let out = Command::new(argv[0])
        .args(&argv[1..])
        .output()
        .map_err(|_| format!("{} could not hash this game.", argv[0]))?;
    if !out.status.success() {
        let err_s = String::from_utf8_lossy(&out.stderr);
        let out_s = String::from_utf8_lossy(&out.stdout);
        let detail = err_s
            .trim()
            .lines()
            .next()
            .or_else(|| out_s.trim().lines().next())
            .unwrap_or("");
        return Err(if detail.is_empty() {
            format!("{} could not hash this game.", argv[0])
        } else {
            detail.to_owned()
        });
    }
    Ok(String::from_utf8_lossy(&out.stdout).into_owned())
}

fn chd_data(path: &Path) -> Result<Vec<u8>, String> {
    let tmp = TempDir::with_prefix("openbox-chd-").map_err(|e| e.to_string())?;
    let cue = tmp.path().join("disc.cue");
    run_hash_tool(&[
        "chdman",
        "extractcd",
        "-i",
        &path.to_string_lossy(),
        "-o",
        &cue.to_string_lossy(),
    ])?;
    let cue_text =
        fs::read_to_string(&cue).map_err(|_| "chdman produced no usable cue sheet.".to_owned())?;
    let mut first: Option<PathBuf> = None;
    for line in cue_text.lines() {
        if let Some(caps) = CUE_FILE_RE.captures(line) {
            first = Some(tmp.path().join(&caps[1]));
            break;
        }
    }
    let mut data_files: Vec<PathBuf> = fs::read_dir(tmp.path())
        .map(|rd| {
            rd.flatten()
                .map(|e| e.path())
                .filter(|p| {
                    p.extension().and_then(|e| e.to_str()).is_some_and(|s| {
                        matches!(s.to_ascii_lowercase().as_str(), "bin" | "iso" | "img")
                    })
                })
                .collect()
        })
        .unwrap_or_default();
    data_files.sort();
    let dir_canon = fs::canonicalize(tmp.path()).unwrap_or_else(|_| tmp.path().to_path_buf());
    let chosen = first
        .filter(|f| {
            f.is_file()
                && fs::canonicalize(f)
                    .ok()
                    .and_then(|c| c.parent().map(Path::to_path_buf))
                    == Some(dir_canon.clone())
        })
        .or_else(|| data_files.into_iter().next())
        .ok_or_else(|| "chdman produced no data track.".to_owned())?;
    let size = fs::metadata(&chosen).map_err(|e| e.to_string())?.len();
    if size > MAX_ROM_BYTES {
        return Err("ROM file is too large for hashing.".to_owned());
    }
    fs::read(&chosen).map_err(|e| e.to_string())
}

fn dolphin_hash(path: &Path) -> Result<String, String> {
    let out = run_hash_tool(&[
        "dolphin-tool",
        "hash",
        "-a",
        "sha1",
        &path.to_string_lossy(),
    ])?;
    let caps = SHA1_HEX_RE
        .captures(&out)
        .ok_or_else(|| "dolphin-tool did not return a usable SHA-1 hash.".to_owned())?;
    Ok(caps[1].to_ascii_lowercase())
}

/// Exact port of `retroachievements.py::_wad_hash` (mirrors rcheevos `rc_hash_wiiware`).
pub fn wad_hash(path: &Path) -> Result<String, String> {
    let file_size = fs::metadata(path)
        .map_err(|_| format!("Could not stat WAD file: {}", path.display()))?
        .len();
    let mut handle = File::open(path).map_err(|e| format!("Could not read WAD file: {e}"))?;
    let mut header = [0u8; 0x40];
    handle
        .read_exact(&mut header)
        .map_err(|_| "The file is not a WiiWare WAD.".to_owned())?;
    if &header[4..8] != b"Is\x00\x00" {
        return Err("The file is not a WiiWare WAD.".to_owned());
    }
    let align64 = |n: u64| (n + 0x3F) & !0x3F;
    let cert_size =
        align64(u32::from_be_bytes([header[8], header[9], header[10], header[11]]) as u64);
    let ticket_size =
        align64(u32::from_be_bytes([header[16], header[17], header[18], header[19]]) as u64);
    let tmd_size =
        align64(u32::from_be_bytes([header[20], header[21], header[22], header[23]]) as u64);
    if tmd_size < 0x1E6 {
        return Err("The WAD title metadata is too short.".to_owned());
    }
    let tmd_start = 0x40 + cert_size + ticket_size;
    if tmd_start + tmd_size > file_size {
        return Err("The WAD title metadata runs past the end of the file.".to_owned());
    }
    handle
        .seek(SeekFrom::Start(tmd_start))
        .map_err(|e| format!("Could not read WAD file: {e}"))?;
    let mut tmd = vec![0u8; tmd_size as usize];
    handle
        .read_exact(&mut tmd)
        .map_err(|_| "The WAD title metadata is truncated.".to_owned())?;
    let mut digest = Md5::new();
    digest.update(&tmd);
    let content_count = u16::from_be_bytes([tmd[0x1DE], tmd[0x1DF]]) as usize;
    if content_count > 4096 {
        return Err("The WAD declares too many contents.".to_owned());
    }
    let mut content_addr = tmd_start + tmd_size;
    let mut buf = vec![0u8; 1024 * 1024];
    for idx in 0..content_count {
        let entry = 0x1E4 + 8 + idx * 0x24;
        if entry + 8 > tmd.len() {
            return Err("The WAD content table is truncated.".to_owned());
        }
        let size_field = &tmd[entry..entry + 8];
        let content_size: u64 = if &size_field[..4] == b"\x00\x00\x00\x00" {
            let raw =
                u32::from_be_bytes([size_field[4], size_field[5], size_field[6], size_field[7]])
                    as u64;
            (raw + 0x0F) & !0x0F
        } else {
            MAX_ROM_BYTES
        };
        let hashed_size = content_size.min(MAX_ROM_BYTES);
        if content_addr + hashed_size > file_size {
            return Err("A WAD content runs past the end of the file.".to_owned());
        }
        handle
            .seek(SeekFrom::Start(content_addr))
            .map_err(|e| format!("Could not read WAD file: {e}"))?;
        let mut remaining = hashed_size as usize;
        while remaining > 0 {
            let want = remaining.min(buf.len());
            let n = handle
                .read(&mut buf[..want])
                .map_err(|e| format!("Could not read WAD file: {e}"))?;
            if n == 0 {
                return Err("A WAD content is truncated.".to_owned());
            }
            digest.update(&buf[..n]);
            remaining -= n;
        }
        content_addr = align64(content_addr + content_size);
    }
    Ok(hex::encode(digest.finalize()))
}

/// Exact port of `retroachievements.py::game_hash`.
pub fn game_hash(game: &Value) -> Result<String, String> {
    let raw_path = game.get("path").and_then(Value::as_str).unwrap_or("");
    let mut path = PathBuf::from(raw_path);
    let platform = game
        .get("platform")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_lowercase();

    if platform == "arcade" {
        if !path.is_file() {
            return Err(
                "The game path must be a local ROM file for automatic achievement matching."
                    .to_owned(),
            );
        }
        let stem = path.file_stem().and_then(|s| s.to_str()).unwrap_or("");
        let mut h = Md5::new();
        h.update(stem.as_bytes());
        return Ok(hex::encode(h.finalize()));
    }
    if system_names(&platform).is_none() {
        return Err(
            "Automatic matching is unavailable for this platform. Enter a RetroAchievements Game ID in Edit metadata."
                .to_owned(),
        );
    }
    if path
        .extension()
        .and_then(|e| e.to_str())
        .is_some_and(|s| s.eq_ignore_ascii_case("m3u"))
    {
        path = m3u_disc(&path)?;
    }
    if !path.is_file() {
        return Err(
            "The game path must be a local ROM file for automatic achievement matching.".to_owned(),
        );
    }
    let suffix = path
        .extension()
        .and_then(|e| e.to_str())
        .map(|s| format!(".{}", s.to_ascii_lowercase()))
        .unwrap_or_default();
    if suffix == ".rvz" {
        if !matches!(
            platform.as_str(),
            "gamecube" | "nintendo gamecube" | "wii" | "nintendo wii"
        ) {
            return Err("RVZ hashing is available for GameCube and Wii games only.".to_owned());
        }
        return dolphin_hash(&path);
    }
    if suffix == ".wad" {
        if !matches!(
            platform.as_str(),
            "wii" | "nintendo wii" | "wiiware" | "nintendo wiiware"
        ) {
            return Err("WAD hashing is available for Wii and WiiWare games only.".to_owned());
        }
        return wad_hash(&path);
    }
    let mut data = if suffix == ".chd" {
        if !matches!(
            platform.as_str(),
            "playstation" | "sony playstation" | "ps1"
        ) {
            return Err("CHD hashing is available for PlayStation games only.".to_owned());
        }
        chd_data(&path)?
    } else {
        archive_rom_bytes(&path)?
    };

    if matches!(platform.as_str(), "nes" | "nintendo entertainment system")
        && data.starts_with(b"NES\x1a")
    {
        data.drain(..16.min(data.len()));
    } else if matches!(
        platform.as_str(),
        "snes" | "super nintendo entertainment system"
    ) && data.len() % 8192 == 512
    {
        data.drain(..512);
    } else if platform == "atari 7800" && data.starts_with(b"\x01ATARI7800") {
        data.drain(..128.min(data.len()));
    } else if platform == "atari lynx" && data.starts_with(b"LYNX\x00") {
        data.drain(..64.min(data.len()));
    } else if matches!(platform.as_str(), "pc engine" | "turbografx-16")
        && data.len() % (128 * 1024) == 512
    {
        data.drain(..512);
    } else if platform == "nintendo 64" && suffix == ".v64" {
        for chunk in data.as_chunks_mut::<2>().0 {
            chunk.swap(0, 1);
        }
    } else if platform == "nintendo 64" && suffix == ".n64" {
        for chunk in data.as_chunks_mut::<4>().0 {
            chunk.reverse();
        }
    } else if !is_raw_md5_platform(&platform) && platform != "arcade" && platform != "nintendo 64" {
        return Err(
            "Automatic matching is unavailable for this platform. Enter a RetroAchievements Game ID in Edit metadata."
                .to_owned(),
        );
    }
    let mut h = Md5::new();
    h.update(&data);
    Ok(hex::encode(h.finalize()))
}

pub fn cached<F>(path: &Path, max_age_secs: u64, getter: F) -> Result<Value, String>
where
    F: FnOnce() -> Result<Value, String>,
{
    if max_age_secs > 0
        && let Ok(meta) = fs::metadata(path)
        && let Ok(mtime) = meta.modified()
        && let Ok(age) = SystemTime::now().duration_since(mtime)
        && age.as_secs() < max_age_secs
        && let Ok(raw) = fs::read_to_string(path)
        && let Ok(val) = serde_json::from_str::<Value>(&raw)
    {
        return Ok(val);
    }
    let data = getter()?;
    if let Some(p) = path.parent() {
        let _ = fs::create_dir_all(p);
    }
    let text = serde_json::to_string(&data).map_err(|e| e.to_string())?;
    atomic_write_text(path, &text, 0o600).map_err(|e| e.to_string())?;
    Ok(data)
}

pub fn match_game(
    game: &Value,
    credentials: &RaCredentials,
    cache_directory: &Path,
) -> Result<(i64, String), String> {
    if let Some(id_str) = game.get("ra_game_id").and_then(|v| match v {
        Value::String(s) => Some(s.clone()),
        Value::Number(n) => Some(n.to_string()),
        _ => None,
    }) && !id_str.is_empty()
        && id_str.bytes().all(|b| b.is_ascii_digit())
        && let Ok(id) = id_str.parse::<i64>()
    {
        let digest = game
            .get("ra_hash")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        return Ok((id, digest));
    }
    let platform = game
        .get("platform")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_lowercase();
    let Some(targets) = system_names(&platform) else {
        return Err(
            "This platform has no RetroAchievements system mapping. Enter a Game ID in Edit metadata."
                .to_owned(),
        );
    };
    let systems = cached(&cache_directory.join("systems.json"), 604800, || {
        api_get(
            "API_GetConsoleIDs.php",
            &[("a", "1".into()), ("g", "1".into())],
            credentials,
        )
    })?;
    let console = systems
        .as_array()
        .and_then(|arr| {
            arr.iter().find(|it| {
                let name = it.get("Name").and_then(Value::as_str).unwrap_or("");
                targets.iter().any(|t| name.eq_ignore_ascii_case(t))
            })
        })
        .ok_or_else(|| "RetroAchievements did not return a matching system.".to_owned())?;
    let digest = game_hash(game)?;
    let console_id = console
        .get("ID")
        .and_then(|v| match v {
            Value::Number(n) => n.as_i64(),
            Value::String(s) => s.trim().parse::<i64>().ok(),
            _ => None,
        })
        .ok_or_else(|| "RetroAchievements returned an unusable system ID.".to_owned())?;
    let games = cached(
        &cache_directory.join(format!("system-{console_id}.json")),
        604800,
        || {
            api_get(
                "API_GetGameList.php",
                &[
                    ("i", console_id.to_string()),
                    ("h", "1".into()),
                    ("f", "1".into()),
                ],
                credentials,
            )
        },
    )?;
    let matched = games
        .as_array()
        .and_then(|arr| {
            arr.iter().find(|it| {
                it.get("Hashes")
                    .and_then(Value::as_array)
                    .is_some_and(|h| h.iter().any(|x| x.as_str() == Some(digest.as_str())))
            })
        })
        .ok_or_else(|| "This ROM hash is not linked to a RetroAchievements game.".to_owned())?;
    let matched_id = matched
        .get("ID")
        .and_then(|v| match v {
            Value::Number(n) => n.as_i64(),
            Value::String(s) => s.trim().parse::<i64>().ok(),
            _ => None,
        })
        .ok_or_else(|| "RetroAchievements returned an unusable game ID.".to_owned())?;
    Ok((matched_id, digest))
}

pub fn game_progress(game_id: i64, credentials: &RaCredentials) -> Result<Value, String> {
    let data = api_get(
        "API_GetGameInfoAndUserProgress.php",
        &[
            ("g", game_id.to_string()),
            ("u", credentials.username.clone()),
            ("a", "1".into()),
        ],
        credentials,
    )?;
    let ach_list: Vec<&Value> = match data.get("Achievements") {
        Some(Value::Object(m)) => m.values().collect(),
        Some(Value::Array(a)) => a.iter().collect(),
        _ => Vec::new(),
    };
    let earned = data
        .get("NumAwardedToUser")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let earned_hc = data
        .get("NumAwardedToUserHardcore")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let total = data
        .get("NumAchievements")
        .and_then(Value::as_i64)
        .unwrap_or(ach_list.len() as i64);
    let beaten = data
        .get("Beaten")
        .filter(|v| !v.is_null() && v.as_str() != Some(""))
        .or_else(|| data.get("NumAwardedToUserBeaten"))
        .and_then(|v| match v {
            Value::Number(n) => n.as_i64(),
            Value::String(s) => s.trim().parse::<i64>().ok(),
            _ => None,
        })
        .unwrap_or(0);
    let achievements: Vec<Value> = ach_list
        .into_iter()
        .map(|item| {
            let badge_raw = item
                .get("BadgeName")
                .map(|v| match v {
                    Value::String(s) => s.clone(),
                    Value::Null => String::new(),
                    other => other.to_string(),
                })
                .unwrap_or_default();
            let badge = BADGE_STRIP_RE.replace_all(&badge_raw, "").into_owned();
            let de = item
                .get("DateEarned")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty());
            let deh = item
                .get("DateEarnedHardcore")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty());
            json!({
                "id": item.get("ID").cloned().unwrap_or(Value::Null),
                "title": item.get("Title").and_then(Value::as_str).unwrap_or(""),
                "description": item.get("Description").and_then(Value::as_str).unwrap_or(""),
                "points": item.get("Points").and_then(Value::as_i64).unwrap_or(0),
                "badge": badge,
                "earned": de.is_some() || deh.is_some(),
                "hardcore": deh.is_some(),
                "date_earned": de.map(|s| Value::String(s.to_owned())).unwrap_or(Value::Null),
                "date_earned_hardcore": deh.map(|s| Value::String(s.to_owned())).unwrap_or(Value::Null),
            })
        })
        .collect();

    Ok(json!({
        "title": data.get("Title").and_then(Value::as_str).unwrap_or(""),
        "earned": earned,
        "earned_hardcore": earned_hc,
        "total": total,
        "completion": data.get("UserCompletion").and_then(Value::as_str).unwrap_or("0%"),
        "beaten": beaten,
        "mastered": earned_hc >= total && total > 0,
        "achievements": achievements,
    }))
}

pub fn enhanced_ra_profile(progress: &Value, credentials: &RaCredentials) -> Value {
    let profile = api_get(
        "API_GetUserProfile.php",
        &[("u", credentials.username.clone())],
        credentials,
    )
    .unwrap_or_else(|_| json!({}));
    let mut out = progress.as_object().cloned().unwrap_or_default();
    let earned = out.get("earned").and_then(Value::as_i64).unwrap_or(0);
    let total = out.get("total").and_then(Value::as_i64).unwrap_or(0);
    let earned_hc = out
        .get("earned_hardcore")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let beaten = profile
        .get("Awarded")
        .and_then(Value::as_i64)
        .unwrap_or_else(|| out.get("beaten").and_then(Value::as_i64).unwrap_or(earned));
    out.insert("beaten".into(), json!(beaten));
    out.insert("mastered".into(), json!(earned_hc));
    out.insert(
        "points_earned".into(),
        json!(
            profile
                .get("TotalPoints")
                .and_then(Value::as_i64)
                .unwrap_or(0)
        ),
    );
    out.insert(
        "points_total".into(),
        json!(
            profile
                .get("TotalTruePoints")
                .and_then(Value::as_i64)
                .unwrap_or(0)
        ),
    );
    out.insert(
        "motivation".into(),
        Value::String(
            profile
                .get("Motivation")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned(),
        ),
    );
    out.insert(
        "rich_presence".into(),
        Value::String(
            profile
                .get("RichPresenceMsg")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned(),
        ),
    );
    out.insert(
        "commitment_label".into(),
        Value::String(format!("{earned}/{total} unlocked")),
    );
    Value::Object(out)
}

pub fn inject_retroachievements(
    credentials: &RaCredentials,
    hardcore: bool,
) -> Result<Value, String> {
    let username = credentials.username.trim();
    let api_key = credentials.api_key.trim();
    if username.is_empty() || api_key.is_empty() {
        return Err("RetroAchievements credentials are required.".to_owned());
    }
    let home = home_dir().unwrap_or_else(|| PathBuf::from("."));
    let mut updated = Vec::new();
    let mut skipped = Vec::new();

    let mut write_kv = |path: PathBuf, mapping: BTreeMap<&str, String>| {
        if let Some(p) = path.parent()
            && let Err(e) = fs::create_dir_all(p)
        {
            skipped.push(format!("{}: {e}", path.display()));
            return;
        }
        let existing = fs::read_to_string(&path).unwrap_or_default();
        let mut out = Vec::new();
        let mut seen: HashSet<&str> = HashSet::new();
        for line in existing.lines() {
            let trimmed = line.trim_start();
            let mut matched = false;
            for (&k, v) in &mapping {
                if let Some(rest) = trimmed.strip_prefix(k)
                    && rest.trim_start().starts_with('=')
                {
                    out.push(format!("{k} = {v}"));
                    seen.insert(k);
                    matched = true;
                    break;
                }
            }
            if !matched {
                out.push(line.to_owned());
            }
        }
        for (&k, v) in &mapping {
            if !seen.contains(k) {
                out.push(format!("{k} = {v}"));
            }
        }
        let body = if out.is_empty() {
            String::new()
        } else {
            format!("{}\n", out.join("\n"))
        };
        match atomic_write_text(&path, &body, 0o600) {
            Ok(_) => updated.push(path.to_string_lossy().into_owned()),
            Err(e) => skipped.push(format!("{}: {e}", path.display())),
        }
    };

    for cfg in [
        home.join(".config/retroarch/retroarch.cfg"),
        home.join(".var/app/org.libretro.RetroArch/config/retroarch/retroarch.cfg"),
    ] {
        let mut map = BTreeMap::new();
        map.insert("cheevos_enable", "true".to_owned());
        map.insert("cheevos_username", format!("\"{username}\""));
        map.insert("cheevos_password", format!("\"{api_key}\""));
        map.insert("cheevos_token", format!("\"{api_key}\""));
        map.insert(
            "cheevos_hardcore_enabled",
            if hardcore { "true" } else { "false" }.to_owned(),
        );
        write_kv(cfg, map);
    }
    for ini in [
        home.join(".config/dolphin-emu/Dolphin.ini"),
        home.join(".var/app/org.DolphinEmu.dolphin-emu/config/dolphin-emu/Dolphin.ini"),
    ] {
        let mut map = BTreeMap::new();
        map.insert("Enabled", "True".to_owned());
        map.insert("Username", username.to_owned());
        map.insert("Password", api_key.to_owned());
        write_kv(ini, map);
    }
    for ini in [
        home.join(".config/PCSX2/inis/PCSX2.ini"),
        home.join(".var/app/net.pcsx2.PCSX2/config/PCSX2/inis/PCSX2.ini"),
    ] {
        let mut map = BTreeMap::new();
        map.insert("Enabled", "true".to_owned());
        map.insert("Username", username.to_owned());
        map.insert("Token", api_key.to_owned());
        write_kv(ini, map);
    }
    Ok(json!({"updated": updated, "skipped": skipped}))
}

pub fn now_unix_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

pub fn write_ra_game_cache(data_dir: &Path, game_id: &str, progress: &Value) {
    let cache_path = data_dir
        .join("cache")
        .join("retroachievements")
        .join(format!("{game_id}.json"));
    let ach: Vec<Value> = progress
        .get("achievements")
        .and_then(Value::as_array)
        .map(|a| a.iter().take(500).cloned().collect())
        .unwrap_or_default();
    let payload = json!({
        "earned": progress.get("earned").and_then(Value::as_i64).unwrap_or(0),
        "earned_hardcore": progress.get("earned_hardcore").and_then(Value::as_i64).unwrap_or(0),
        "total": progress.get("total").and_then(Value::as_i64).unwrap_or(0),
        "progress_pct": progress.get("progress_pct").cloned().unwrap_or_else(|| json!(0)),
        "mastered": progress.get("mastered").cloned().unwrap_or(Value::Bool(false)),
        "beaten": progress.get("beaten").and_then(Value::as_i64).unwrap_or(0),
        "achievements": ach,
    });
    if let Ok(text) = serde_json::to_string(&payload) {
        let _ = atomic_write_text(&cache_path, &text, 0o600);
    }
}

pub fn _unused_map_marker(_: &Map<String, Value>) {}
