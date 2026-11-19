//! Per-game save discovery, backup, restore, retention, and scan helpers (`saves.py`, `pkg/parity/parity_saves.py`).

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::LazyLock;
use std::time::{Instant, UNIX_EPOCH};

use jiff::Timestamp;
use openbox_io::{atomic_copy_stream, fsync_directory};
use openbox_platform::{
    home_dir, steam_candidate_roots, steam_install_roots, user_symlink_component,
};
use parking_lot::{Mutex, ReentrantMutex};
use regex::Regex;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use zip::write::SimpleFileOptions;
use zip::{CompressionMethod, ZipArchive, ZipWriter};

pub const MAX_SAVE_ARCHIVE_MEMBER_BYTES: u64 = 4 * 1024 * 1024 * 1024;
pub const MAX_SAVE_ARCHIVE_TOTAL_BYTES: u64 = 32 * 1024 * 1024 * 1024;
pub const MAX_SAVE_ARCHIVE_MEMBERS: usize = 50_000;
pub const PREVIEW_BUCKET_LIMIT: usize = 200;

/// Serializes save-archive creation/extraction against cloud-sync and save-vault operations.
pub static SAVE_SYNC_LOCK: LazyLock<ReentrantMutex<()>> = LazyLock::new(|| ReentrantMutex::new(()));

static NON_ALNUM_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[^a-z0-9]+").unwrap());
static LABEL_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[A-Za-z0-9._-]{1,64}$").unwrap());

#[derive(Default)]
struct SaveScanCache {
    at: Option<Instant>,
    signature: String,
    indices: Vec<usize>,
}

static SAVE_CACHE: LazyLock<Mutex<SaveScanCache>> =
    LazyLock::new(|| Mutex::new(SaveScanCache::default()));

#[derive(Debug, Clone)]
pub enum SaveError {
    NotFound(String),
    Invalid(String),
    Io(String),
}

impl std::fmt::Display for SaveError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SaveError::NotFound(s) | SaveError::Invalid(s) | SaveError::Io(s) => f.write_str(s),
        }
    }
}

impl std::error::Error for SaveError {}

pub fn reject_symlink_components(path: &Path) -> Result<(), SaveError> {
    if let Some(comp) = user_symlink_component(path) {
        return Err(SaveError::Invalid(format!(
            "Save path may not contain symlinks: {}",
            comp.display()
        )));
    }
    Ok(())
}

pub fn expand_user_path(raw: &str) -> PathBuf {
    let trimmed = raw.trim();
    if let Some(rest) = trimmed.strip_prefix("~/")
        && let Some(home) = home_dir()
    {
        return home.join(rest);
    }
    if trimmed == "~"
        && let Some(home) = home_dir()
    {
        return home;
    }
    PathBuf::from(trimmed)
}

pub fn resolve_non_strict(path: &Path) -> PathBuf {
    if let Ok(canon) = fs::canonicalize(path) {
        return canon;
    }
    let mut cur = path.to_path_buf();
    let mut tail = Vec::new();
    while !cur.exists() {
        if let Some(name) = cur.file_name() {
            tail.push(name.to_os_string());
        } else {
            break;
        }
        if !cur.pop() {
            break;
        }
    }
    let mut base = fs::canonicalize(&cur).unwrap_or(cur);
    for part in tail.into_iter().rev() {
        base.push(part);
    }
    base
}

fn open_regular_nofollow(path: &Path) -> Result<(File, fs::Metadata), SaveError> {
    if path
        .symlink_metadata()
        .map(|m| m.file_type().is_symlink())
        .unwrap_or(false)
    {
        return Err(SaveError::Invalid(format!(
            "Save path may not be a symlink: {}",
            path.display()
        )));
    }
    let mut opts = OpenOptions::new();
    opts.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.custom_flags(libc_o_nofollow());
    }
    let file = opts.open(path).map_err(|e| {
        SaveError::Invalid(format!(
            "Save file could not be opened safely: {} ({e})",
            path.display()
        ))
    })?;
    let meta = file.metadata().map_err(|e| SaveError::Io(e.to_string()))?;
    if !meta.file_type().is_file() {
        return Err(SaveError::Invalid(format!(
            "Save path is not a regular file: {}",
            path.display()
        )));
    }
    Ok((file, meta))
}

#[cfg(unix)]
pub(crate) fn libc_o_nofollow() -> i32 {
    #[cfg(target_os = "linux")]
    {
        0x20000
    }
    #[cfg(target_os = "macos")]
    {
        0x0100
    }
    #[cfg(not(any(target_os = "linux", target_os = "macos")))]
    {
        0
    }
}

#[cfg(unix)]
pub(crate) fn set_mode(path: &Path, mode: u32) {
    use std::os::unix::fs::PermissionsExt;
    let _ = fs::set_permissions(path, fs::Permissions::from_mode(mode));
}

#[cfg(not(unix))]
pub(crate) fn set_mode(_path: &Path, _mode: u32) {}

#[cfg(unix)]
pub(crate) fn file_mode_bits(meta: &fs::Metadata) -> u32 {
    use std::os::unix::fs::PermissionsExt;
    meta.permissions().mode() & 0o7777
}

#[cfg(not(unix))]
pub(crate) fn file_mode_bits(_meta: &fs::Metadata) -> u32 {
    0o600
}

fn file_mtime_secs(meta: &fs::Metadata) -> i64 {
    meta.modified()
        .ok()
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

pub fn file_sha256(path: &Path) -> Result<String, SaveError> {
    let (mut file, _meta) = open_regular_nofollow(path)?;
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; 1024 * 1024];
    loop {
        let n = file
            .read(&mut buf)
            .map_err(|e| SaveError::Io(e.to_string()))?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hex::encode(hasher.finalize()))
}

fn walk_regular_files_no_symlinks(root: &Path) -> Result<Vec<(PathBuf, String)>, SaveError> {
    let mut out = Vec::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let rd = match fs::read_dir(&dir) {
            Ok(rd) => rd,
            Err(_) => continue,
        };
        let mut entries: Vec<PathBuf> = rd.flatten().map(|e| e.path()).collect();
        entries.sort();
        for p in entries {
            let meta = fs::symlink_metadata(&p).map_err(|e| SaveError::Io(e.to_string()))?;
            if meta.file_type().is_symlink() {
                return Err(SaveError::Invalid(format!(
                    "Save backup source contains a symlink: {}",
                    p.display()
                )));
            }
            if meta.is_dir() {
                stack.push(p);
            } else if meta.is_file() {
                let rel = p
                    .strip_prefix(root)
                    .map_err(|e| SaveError::Invalid(e.to_string()))?
                    .to_string_lossy()
                    .replace('\\', "/");
                out.push((p, rel));
            }
        }
    }
    out.sort_by(|a, b| a.1.cmp(&b.1));
    Ok(out)
}

fn manifest_entries(roots: &[PathBuf]) -> Result<Vec<Value>, SaveError> {
    let mut entries = Vec::new();
    for (index, source) in roots.iter().enumerate() {
        let candidates: Vec<(PathBuf, String)> = if source.is_file() {
            let name = source
                .file_name()
                .and_then(|s| s.to_str())
                .unwrap_or("save")
                .to_owned();
            vec![(source.clone(), name)]
        } else {
            reject_symlink_components(source)?;
            walk_regular_files_no_symlinks(source)?
        };
        for (file_path, relative) in candidates {
            let (mut f, meta) = open_regular_nofollow(&file_path)?;
            let mut hasher = Sha256::new();
            let mut buf = vec![0u8; 1024 * 1024];
            loop {
                let n = f.read(&mut buf).map_err(|e| SaveError::Io(e.to_string()))?;
                if n == 0 {
                    break;
                }
                hasher.update(&buf[..n]);
            }
            let archive_name = if source.is_file() {
                let fname = source
                    .file_name()
                    .and_then(|s| s.to_str())
                    .unwrap_or("save");
                format!("roots/{index}/{fname}")
            } else {
                format!("roots/{index}/{relative}")
            };
            entries.push(json!({
                "root": index,
                "relative": relative,
                "archive": archive_name,
                "size": meta.len(),
                "mode": file_mode_bits(&meta),
                "mtime": file_mtime_secs(&meta),
                "sha256": hex::encode(hasher.finalize()),
            }));
        }
    }
    Ok(entries)
}

fn write_archive_file<W: Write + io::Seek>(
    package: &mut ZipWriter<W>,
    source: &Path,
    archive_name: &str,
) -> Result<(), SaveError> {
    let (mut input, _meta) = open_regular_nofollow(source)?;
    let opts = SimpleFileOptions::default().compression_method(CompressionMethod::Deflated);
    package
        .start_file(archive_name, opts)
        .map_err(|e| SaveError::Io(e.to_string()))?;
    let mut buf = vec![0u8; 1024 * 1024];
    loop {
        let n = input
            .read(&mut buf)
            .map_err(|e| SaveError::Io(e.to_string()))?;
        if n == 0 {
            break;
        }
        package
            .write_all(&buf[..n])
            .map_err(|e| SaveError::Io(e.to_string()))?;
    }
    Ok(())
}

fn restrict_archive_permissions(folder: &Path) {
    if let Ok(rd) = fs::read_dir(folder) {
        for entry in rd.flatten() {
            let p = entry.path();
            if p.extension().and_then(|e| e.to_str()) == Some("zip")
                && !p
                    .symlink_metadata()
                    .map(|m| m.file_type().is_symlink())
                    .unwrap_or(true)
            {
                set_mode(&p, 0o600);
            }
        }
    }
}

fn write_private_archive<F>(archive: &Path, populate: F) -> Result<(), SaveError>
where
    F: FnOnce(&mut ZipWriter<&mut File>) -> Result<(), SaveError>,
{
    let parent = archive
        .parent()
        .ok_or_else(|| SaveError::Invalid("Archive has no parent directory.".to_owned()))?;
    fs::create_dir_all(parent).map_err(|e| SaveError::Io(e.to_string()))?;
    if parent
        .symlink_metadata()
        .map(|m| m.file_type().is_symlink())
        .unwrap_or(false)
    {
        return Err(SaveError::Invalid(
            "Save backup directory may not be a symlink.".to_owned(),
        ));
    }
    set_mode(parent, 0o700);
    restrict_archive_permissions(parent);

    let prefix = format!(
        ".{}.",
        archive
            .file_name()
            .and_then(|s| s.to_str())
            .unwrap_or("save")
    );
    let tmp = tempfile::Builder::new()
        .prefix(&prefix)
        .suffix(".tmp")
        .tempfile_in(parent)
        .map_err(|e| SaveError::Io(e.to_string()))?;
    let (mut file, tmp_path) = tmp.keep().map_err(|e| SaveError::Io(e.to_string()))?;
    set_mode(&tmp_path, 0o600);

    let write_res = (|| -> Result<(), SaveError> {
        {
            let mut zip = ZipWriter::new(&mut file);
            populate(&mut zip)?;
            zip.finish().map_err(|e| SaveError::Io(e.to_string()))?;
        }
        file.flush().map_err(|e| SaveError::Io(e.to_string()))?;
        file.sync_all().map_err(|e| SaveError::Io(e.to_string()))?;
        Ok(())
    })();

    drop(file);
    if let Err(err) = write_res {
        let _ = fs::remove_file(&tmp_path);
        return Err(err);
    }
    fs::rename(&tmp_path, archive).map_err(|e| {
        let _ = fs::remove_file(&tmp_path);
        SaveError::Io(e.to_string())
    })?;
    set_mode(archive, 0o600);
    fsync_directory(parent);
    Ok(())
}

pub fn game_backup_dir(game: &Value, root: &Path) -> PathBuf {
    let name = game.get("name").and_then(Value::as_str).unwrap_or("");
    let path = game.get("path").and_then(Value::as_str).unwrap_or("");
    let mut hasher = Sha256::new();
    hasher.update(format!("{name}:{path}").as_bytes());
    let key = &hex::encode(hasher.finalize())[..16];
    root.join(key)
}

pub fn save_roots(game: &Value) -> Result<Vec<PathBuf>, SaveError> {
    let Some(arr) = game.get("save_paths").and_then(Value::as_array) else {
        return Ok(Vec::new());
    };
    let mut roots = Vec::new();
    for item in arr {
        let s = match item {
            Value::String(s) => s.as_str(),
            _ => "",
        };
        if s.trim().is_empty() {
            continue;
        }
        let raw = expand_user_path(s);
        reject_symlink_components(&raw)?;
        roots.push(resolve_non_strict(&raw));
    }
    Ok(roots)
}

fn source_entry(
    path: &Path,
    label: &str,
    shared: bool,
    emulator_id: &str,
    core: &str,
    slot: &str,
) -> Value {
    json!({
        "path": path.to_string_lossy(),
        "label": label,
        "shared": shared,
        "emulator_id": emulator_id,
        "core": core,
        "slot": slot,
    })
}

fn normalize_title_token(s: &str) -> String {
    NON_ALNUM_RE.replace_all(&s.to_lowercase(), "").into_owned()
}

fn find_steam_userdata_dirs(steam_root: &Path, app_id: &str) -> Vec<PathBuf> {
    let userdata = steam_root.join("userdata");
    let Ok(rd) = fs::read_dir(&userdata) else {
        return Vec::new();
    };
    let mut out = Vec::new();
    for entry in rd.flatten() {
        let candidate = entry.path().join(app_id).join("remote");
        if candidate.is_dir() {
            out.push(candidate);
        }
    }
    out
}

fn shared_and_retro_candidates(
    game: &Value,
    shared: &HashMap<&str, Vec<PathBuf>>,
    shared_emulators: &HashMap<&str, &str>,
    retro_roots: &[PathBuf],
) -> Vec<Value> {
    let platform = game.get("platform").and_then(Value::as_str).unwrap_or("");
    let mut candidates = Vec::new();
    if let Some(paths) = shared.get(platform) {
        for p in paths {
            if !p.exists() {
                continue;
            }
            let mut emu_id = *shared_emulators.get(platform).unwrap_or(&"");
            if p.file_name().and_then(|s| s.to_str()) == Some(".mednafen") {
                emu_id = "mednafen";
            }
            candidates.push(source_entry(
                p,
                &format!("{platform} shared saves"),
                true,
                emu_id,
                "",
                "",
            ));
        }
    }
    let name_src = game
        .get("name")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .unwrap_or_else(|| {
            Path::new(game.get("path").and_then(Value::as_str).unwrap_or(""))
                .file_stem()
                .and_then(|s| s.to_str())
                .unwrap_or("")
                .to_owned()
        });
    let title = normalize_title_token(&name_src);
    if !title.is_empty() {
        for root in retro_roots {
            if !root.is_dir() {
                continue;
            }
            let mut stack = vec![root.clone()];
            while let Some(dir) = stack.pop() {
                let Ok(rd) = fs::read_dir(&dir) else {
                    continue;
                };
                for entry in rd.flatten() {
                    let p = entry.path();
                    if p.is_dir() {
                        stack.push(p);
                    } else if p.is_file() {
                        let stem = p.file_stem().and_then(|s| s.to_str()).unwrap_or("");
                        if normalize_title_token(stem) == title {
                            let core = p
                                .parent()
                                .filter(|par| *par != root.as_path())
                                .and_then(|par| par.file_name())
                                .and_then(|s| s.to_str())
                                .unwrap_or("");
                            candidates.push(source_entry(
                                &p,
                                "RetroArch save",
                                false,
                                "retroarch",
                                core,
                                "",
                            ));
                        }
                    }
                }
            }
        }
    }
    candidates
}

fn unique_candidates(candidates: Vec<Value>) -> Vec<Value> {
    let mut order = Vec::new();
    let mut map: HashMap<String, Value> = HashMap::new();
    for c in candidates {
        let p = c
            .get("path")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        if !map.contains_key(&p) {
            order.push(p.clone());
        }
        map.insert(p, c);
    }
    order.into_iter().filter_map(|k| map.remove(&k)).collect()
}

pub fn discover_save_paths(game: &Value, home_override: Option<&Path>) -> Vec<Value> {
    if home_override.is_none() && cfg!(target_os = "windows") {
        return discover_save_paths_windows(game);
    }
    if home_override.is_none() && cfg!(target_os = "macos") {
        let home = home_dir().unwrap_or_else(|| PathBuf::from("."));
        return discover_save_paths_darwin(game, &home);
    }
    let home = home_override
        .map(PathBuf::from)
        .or_else(home_dir)
        .unwrap_or_else(|| PathBuf::from("."));
    let mut candidates = Vec::new();
    let app_id = game
        .get("steam_app_id")
        .and_then(Value::as_str)
        .unwrap_or("");
    if !app_id.is_empty() && app_id.chars().all(|c| c.is_ascii_digit()) {
        for steam in [
            home.join(".local/share/Steam"),
            home.join(".steam/steam"),
            home.join(".var/app/com.valvesoftware.Steam/.local/share/Steam"),
        ] {
            for p in find_steam_userdata_dirs(&steam, app_id) {
                candidates.push(source_entry(&p, "Steam Cloud", false, "steam", "", ""));
            }
        }
    }
    let mut shared: HashMap<&str, Vec<PathBuf>> = HashMap::new();
    shared.insert(
        "PlayStation 2",
        vec![
            home.join(".config/PCSX2/memcards"),
            home.join(".var/app/net.pcsx2.PCSX2/config/PCSX2/memcards"),
        ],
    );
    shared.insert(
        "PSP",
        vec![
            home.join(".config/ppsspp/PSP/SAVEDATA"),
            home.join(".var/app/org.ppsspp.PPSSPP/config/ppsspp/PSP/SAVEDATA"),
        ],
    );
    shared.insert(
        "PlayStation 3",
        vec![
            home.join(".config/rpcs3/dev_hdd0/home/00000001/savedata"),
            home.join(".var/app/net.rpcs3.RPCS3/config/rpcs3/dev_hdd0/home/00000001/savedata"),
        ],
    );
    shared.insert(
        "GameCube",
        vec![
            home.join(".local/share/dolphin-emu/GC"),
            home.join(".var/app/org.DolphinEmu.dolphin-emu/data/dolphin-emu/GC"),
        ],
    );
    shared.insert(
        "Wii",
        vec![
            home.join(".local/share/dolphin-emu/Wii/title"),
            home.join(".var/app/org.DolphinEmu.dolphin-emu/data/dolphin-emu/Wii/title"),
        ],
    );
    shared.insert(
        "WiiWare",
        vec![
            home.join(".local/share/dolphin-emu/Wii/title"),
            home.join(".var/app/org.DolphinEmu.dolphin-emu/data/dolphin-emu/Wii/title"),
        ],
    );
    shared.insert(
        "Sega Saturn",
        vec![
            home.join(".config/retroarch/saves"),
            home.join(".var/app/org.libretro.RetroArch/config/retroarch/saves"),
            home.join(".mednafen"),
        ],
    );
    shared.insert("Wii U", vec![home.join(".local/share/Cemu/mlc01/usr/save")]);

    let shared_emulators: HashMap<&str, &str> = [
        ("PlayStation 2", "pcsx2"),
        ("PSP", "ppsspp"),
        ("PlayStation 3", "rpcs3"),
        ("GameCube", "dolphin"),
        ("Wii", "dolphin"),
        ("WiiWare", "dolphin"),
        ("Sega Saturn", "retroarch"),
        ("Wii U", "cemu"),
    ]
    .into_iter()
    .collect();

    let retro_roots = [
        home.join(".config/retroarch/saves"),
        home.join(".config/retroarch/states"),
        home.join(".var/app/org.libretro.RetroArch/config/retroarch/saves"),
        home.join(".var/app/org.libretro.RetroArch/config/retroarch/states"),
    ];
    candidates.extend(shared_and_retro_candidates(
        game,
        &shared,
        &shared_emulators,
        &retro_roots,
    ));
    unique_candidates(candidates)
}

fn discover_save_paths_darwin(game: &Value, home: &Path) -> Vec<Value> {
    let mut candidates = Vec::new();
    let app_id = game
        .get("steam_app_id")
        .and_then(Value::as_str)
        .unwrap_or("");
    if !app_id.is_empty() && app_id.chars().all(|c| c.is_ascii_digit()) {
        for steam in steam_candidate_roots(home) {
            if steam.is_dir() {
                for p in find_steam_userdata_dirs(&steam, app_id) {
                    candidates.push(source_entry(&p, "Steam Cloud", false, "steam", "", ""));
                }
            }
        }
    }
    let library = home.join("Library").join("Application Support");
    let retroarch = home.join("Documents").join("RetroArch");
    let mut shared: HashMap<&str, Vec<PathBuf>> = HashMap::new();
    shared.insert("PSP", vec![home.join(".config/ppsspp/PSP/SAVEDATA")]);
    shared.insert(
        "PlayStation 3",
        vec![library.join("rpcs3/dev_hdd0/home/00000001/savedata")],
    );
    shared.insert("GameCube", vec![library.join("Dolphin/GC")]);
    shared.insert("Wii", vec![library.join("Dolphin/Wii/title")]);
    shared.insert("WiiWare", vec![library.join("Dolphin/Wii/title")]);
    shared.insert("Sega Saturn", vec![retroarch.join("saves")]);

    let shared_emulators: HashMap<&str, &str> = [
        ("PSP", "ppsspp"),
        ("PlayStation 3", "rpcs3"),
        ("GameCube", "dolphin"),
        ("Wii", "dolphin"),
        ("WiiWare", "dolphin"),
        ("Sega Saturn", "retroarch"),
    ]
    .into_iter()
    .collect();
    candidates.extend(shared_and_retro_candidates(
        game,
        &shared,
        &shared_emulators,
        &[retroarch.join("saves"), retroarch.join("states")],
    ));
    unique_candidates(candidates)
}

fn discover_save_paths_windows(game: &Value) -> Vec<Value> {
    let home = home_dir().unwrap_or_else(|| PathBuf::from("."));
    let userprofile = std::env::var("USERPROFILE")
        .ok()
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| home.clone());
    let documents = userprofile.join("Documents");
    let roaming = std::env::var("APPDATA")
        .ok()
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join("AppData/Roaming"));
    let local = std::env::var("LOCALAPPDATA")
        .ok()
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join("AppData/Local"));

    let mut candidates = Vec::new();
    let app_id = game
        .get("steam_app_id")
        .and_then(Value::as_str)
        .unwrap_or("");
    if !app_id.is_empty() && app_id.chars().all(|c| c.is_ascii_digit()) {
        for steam in steam_install_roots() {
            for p in find_steam_userdata_dirs(&steam, app_id) {
                candidates.push(source_entry(&p, "Steam Cloud", false, "steam", "", ""));
            }
        }
    }
    let mut shared: HashMap<&str, Vec<PathBuf>> = HashMap::new();
    shared.insert(
        "PlayStation 2",
        vec![
            documents.join("PCSX2/memcards"),
            roaming.join("PCSX2/memcards"),
        ],
    );
    shared.insert("PSP", vec![documents.join("PPSSPP/PSP/SAVEDATA")]);
    shared.insert(
        "PlayStation 3",
        vec![roaming.join("rpcs3/dev_hdd0/home/00000001/savedata")],
    );
    shared.insert("GameCube", vec![documents.join("Dolphin Emulator/GC")]);
    shared.insert("Wii", vec![documents.join("Dolphin Emulator/Wii/title")]);
    shared.insert(
        "WiiWare",
        vec![documents.join("Dolphin Emulator/Wii/title")],
    );
    shared.insert(
        "Sega Saturn",
        vec![
            roaming.join("retroarch/saves"),
            local.join("retroarch/saves"),
        ],
    );
    shared.insert("Wii U", vec![roaming.join("Cemu/mlc01/usr/save")]);

    let shared_emulators: HashMap<&str, &str> = [
        ("PlayStation 2", "pcsx2"),
        ("PSP", "ppsspp"),
        ("PlayStation 3", "rpcs3"),
        ("GameCube", "dolphin"),
        ("Wii", "dolphin"),
        ("WiiWare", "dolphin"),
        ("Sega Saturn", "retroarch"),
        ("Wii U", "cemu"),
    ]
    .into_iter()
    .collect();
    let retro_roots = [
        roaming.join("retroarch/saves"),
        roaming.join("retroarch/states"),
        local.join("retroarch/saves"),
        local.join("retroarch/states"),
    ];
    candidates.extend(shared_and_retro_candidates(
        game,
        &shared,
        &shared_emulators,
        &retro_roots,
    ));
    unique_candidates(candidates)
}

pub fn extra_save_candidates(game: &Value, home_override: Option<&Path>) -> Vec<Value> {
    let home = home_override
        .map(PathBuf::from)
        .or_else(home_dir)
        .unwrap_or_else(|| PathBuf::from("."));
    let platform = game.get("platform").and_then(Value::as_str).unwrap_or("");
    let mut candidates = Vec::new();
    if matches!(platform, "WiiWare" | "Wii") {
        for root in [
            home.join(".local/share/dolphin-emu/Wii/title"),
            home.join(".var/app/org.DolphinEmu.dolphin-emu/data/dolphin-emu/Wii/title"),
        ] {
            if root.exists() {
                candidates.push(source_entry(
                    &root,
                    "Dolphin Wii/WiiWare",
                    true,
                    "dolphin",
                    "",
                    "",
                ));
            }
        }
    }
    if matches!(platform, "Sega Saturn" | "Saturn") {
        for root in [
            home.join(".config/retroarch/saves"),
            home.join(".var/app/org.libretro.RetroArch/config/retroarch/saves"),
            home.join(".mednafen"),
        ] {
            if root.exists() {
                let emu = if root.file_name().and_then(|s| s.to_str()) == Some(".mednafen") {
                    "mednafen"
                } else {
                    "retroarch"
                };
                candidates.push(source_entry(
                    &root,
                    "Saturn / Mednafen saves",
                    true,
                    emu,
                    "",
                    "",
                ));
            }
        }
    }
    candidates
}

pub fn save_source_for(game: &Value, path: &str) -> Map<String, Value> {
    let mut empty = Map::new();
    empty.insert("emulator_id".into(), Value::String(String::new()));
    empty.insert("core".into(), Value::String(String::new()));
    empty.insert("slot".into(), Value::String(String::new()));
    let Some(sources) = game.get("save_sources").and_then(Value::as_object) else {
        return empty;
    };
    let normalize = |entry: &Value| -> Map<String, Value> {
        let mut out = Map::new();
        for k in ["emulator_id", "core", "slot"] {
            let v = entry
                .get(k)
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            out.insert(k.into(), Value::String(v));
        }
        out
    };
    if let Some(found) = sources.get(path) {
        return normalize(found);
    }
    let target = resolve_non_strict(&expand_user_path(path));
    for (k, entry) in sources {
        if k == path {
            continue;
        }
        if resolve_non_strict(&expand_user_path(k)) == target {
            return normalize(entry);
        }
    }
    empty
}

pub fn backup_saves(
    game: &Value,
    root: &Path,
    label: &str,
    allow_empty: bool,
    emulator_id: Option<&str>,
    core: Option<&str>,
    slot: Option<&str>,
) -> Result<Option<PathBuf>, SaveError> {
    let _guard = SAVE_SYNC_LOCK.lock();
    let raw_root = expand_user_path(&root.to_string_lossy());
    reject_symlink_components(&raw_root)?;
    let resolved_root = resolve_non_strict(&raw_root);
    let roots: Vec<PathBuf> = save_roots(game)?
        .into_iter()
        .filter(|p| p.exists())
        .collect();
    if roots.is_empty() {
        if allow_empty {
            return Ok(None);
        }
        return Err(SaveError::NotFound(
            "No configured save paths currently exist.".to_owned(),
        ));
    }
    for p in &roots {
        if p.symlink_metadata()
            .map(|m| m.file_type().is_symlink())
            .unwrap_or(false)
        {
            return Err(SaveError::Invalid(
                "Save backup paths may not be symlinks.".to_owned(),
            ));
        }
    }
    let directory = game_backup_dir(game, &resolved_root);
    reject_symlink_components(&directory)?;
    if let Some(parent) = directory.parent() {
        fs::create_dir_all(parent).map_err(|e| SaveError::Io(e.to_string()))?;
        set_mode(parent, 0o700);
    }
    reject_symlink_components(&directory)?;
    fs::create_dir_all(&directory).map_err(|e| SaveError::Io(e.to_string()))?;
    set_mode(&directory, 0o700);
    restrict_archive_permissions(&directory);

    let clean_label = if label.trim().is_empty() {
        "manual"
    } else {
        label.trim()
    };
    if !LABEL_RE.is_match(clean_label) {
        return Err(SaveError::Invalid(
            "Save backup label is invalid.".to_owned(),
        ));
    }
    let now = Timestamp::now();
    let dt = jiff::tz::TimeZone::UTC.to_datetime(now);
    let micros = now.subsec_nanosecond() / 1000;
    let stamp = format!(
        "{:04}{:02}{:02}-{:02}{:02}{:02}-{:06}",
        dt.year(),
        dt.month(),
        dt.day(),
        dt.hour(),
        dt.minute(),
        dt.second(),
        micros
    );
    let archive = directory.join(format!("{stamp}-{clean_label}.zip"));

    let first_save_path = game
        .get("save_paths")
        .and_then(Value::as_array)
        .and_then(|arr| {
            arr.iter()
                .filter_map(Value::as_str)
                .find(|s| !s.trim().is_empty())
        })
        .unwrap_or("");
    let recorded = if !first_save_path.is_empty() {
        save_source_for(game, first_save_path)
    } else {
        Map::new()
    };
    let emu_val = emulator_id.map(str::to_owned).unwrap_or_else(|| {
        game.get("emulator_adapter_id")
            .or_else(|| game.get("emulator_id"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned()
    });
    let core_val = core.map(str::to_owned).unwrap_or_else(|| {
        recorded
            .get("core")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned()
    });
    let slot_val = slot.map(str::to_owned).unwrap_or_else(|| {
        recorded
            .get("slot")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned()
    });

    let root_objs: Vec<Value> = roots
        .iter()
        .map(|p| json!({"path": p.to_string_lossy(), "file": p.is_file()}))
        .collect();
    let entries = manifest_entries(&roots)?;
    let manifest = json!({
        "game": game.get("name").and_then(Value::as_str).unwrap_or(""),
        "label": clean_label,
        "emulator_id": emu_val.chars().take(64).collect::<String>(),
        "core": core_val.chars().take(64).collect::<String>(),
        "slot": slot_val.chars().take(64).collect::<String>(),
        "roots": root_objs,
        "entries": entries,
    });
    let manifest_text =
        serde_json::to_string(&manifest).map_err(|e| SaveError::Io(e.to_string()))?;

    write_private_archive(&archive, |package| {
        let opts = SimpleFileOptions::default().compression_method(CompressionMethod::Deflated);
        package
            .start_file("manifest.json", opts)
            .map_err(|e| SaveError::Io(e.to_string()))?;
        package
            .write_all(manifest_text.as_bytes())
            .map_err(|e| SaveError::Io(e.to_string()))?;
        for (index, source) in roots.iter().enumerate() {
            if source.is_file() {
                let fname = source
                    .file_name()
                    .and_then(|s| s.to_str())
                    .unwrap_or("save");
                write_archive_file(package, source, &format!("roots/{index}/{fname}"))?;
            } else {
                reject_symlink_components(source)?;
                for (file_path, rel) in walk_regular_files_no_symlinks(source)? {
                    write_archive_file(package, &file_path, &format!("roots/{index}/{rel}"))?;
                }
            }
        }
        Ok(())
    })?;
    Ok(Some(archive))
}

pub fn list_backups(game: &Value, root: &Path) -> Result<Vec<PathBuf>, SaveError> {
    let raw_root = expand_user_path(&root.to_string_lossy());
    reject_symlink_components(&raw_root)?;
    let directory = game_backup_dir(game, &resolve_non_strict(&raw_root));
    reject_symlink_components(&directory)?;
    if !directory.is_dir()
        || directory
            .symlink_metadata()
            .map(|m| m.file_type().is_symlink())
            .unwrap_or(false)
    {
        return Ok(Vec::new());
    }
    let mut out: Vec<PathBuf> = fs::read_dir(&directory)
        .map_err(|e| SaveError::Io(e.to_string()))?
        .flatten()
        .map(|e| e.path())
        .filter(|p| {
            p.extension().and_then(|e| e.to_str()) == Some("zip")
                && p.symlink_metadata()
                    .map(|m| m.is_file() && !m.file_type().is_symlink())
                    .unwrap_or(false)
        })
        .collect();
    out.sort();
    out.reverse();
    Ok(out)
}

fn label_from_archive_name(name: &str) -> String {
    let fname = Path::new(name)
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("");
    let stem = fname.strip_suffix(".zip").unwrap_or(fname);
    let parts: Vec<&str> = stem.splitn(4, '-').collect();
    if parts.len() == 4 && !parts[3].is_empty() {
        parts[3].to_owned()
    } else {
        "manual".to_owned()
    }
}

fn locate_archive(game: &Value, root: &Path, backup_name: &str) -> Result<PathBuf, SaveError> {
    let raw_root = expand_user_path(&root.to_string_lossy());
    reject_symlink_components(&raw_root)?;
    let directory = game_backup_dir(game, &resolve_non_strict(&raw_root));
    reject_symlink_components(&directory)?;
    let fname = Path::new(backup_name)
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("");
    if fname.is_empty() {
        return Err(SaveError::NotFound("Save backup not found.".to_owned()));
    }
    let archive = directory.join(fname);
    reject_symlink_components(&archive)?;
    if archive.parent() != Some(directory.as_path()) || !archive.is_file() {
        return Err(SaveError::NotFound("Save backup not found.".to_owned()));
    }
    Ok(archive)
}

#[derive(Clone)]
struct ArchiveMemberInfo {
    filename: String,
    file_size: u64,
    is_dir: bool,
}

fn validate_save_archive_entries(
    package: &mut ZipArchive<File>,
) -> Result<Vec<ArchiveMemberInfo>, SaveError> {
    let len = package.len();
    if len > MAX_SAVE_ARCHIVE_MEMBERS {
        return Err(SaveError::Invalid(
            "Save backup contains too many files.".to_owned(),
        ));
    }
    let mut infos = Vec::with_capacity(len);
    let mut total_bytes: u64 = 0;
    let mut seen: HashSet<String> = HashSet::new();
    for idx in 0..len {
        let entry = package
            .by_index(idx)
            .map_err(|e| SaveError::Invalid(format!("Invalid zip archive: {e}")))?;
        let name = entry
            .name()
            .map_err(|e| SaveError::Invalid(format!("Invalid zip archive: {e}")))?
            .replace('\\', "/");
        let p = Path::new(&name);
        let has_parent = p.components().any(|c| matches!(c, Component::ParentDir));
        if name.is_empty()
            || name.contains('\0')
            || p.is_absolute()
            || has_parent
            || !seen.insert(name.clone())
        {
            return Err(SaveError::Invalid(
                "Save backup contains an unsafe path.".to_owned(),
            ));
        }
        let sz = entry.size();
        if sz > MAX_SAVE_ARCHIVE_MEMBER_BYTES {
            return Err(SaveError::Invalid(
                "Save backup member is too large.".to_owned(),
            ));
        }
        total_bytes = total_bytes.saturating_add(sz);
        if total_bytes > MAX_SAVE_ARCHIVE_TOTAL_BYTES {
            return Err(SaveError::Invalid(
                "Save backup expands beyond the allowed size.".to_owned(),
            ));
        }
        infos.push(ArchiveMemberInfo {
            filename: name,
            file_size: sz,
            is_dir: entry.is_dir(),
        });
    }
    Ok(infos)
}

fn load_save_manifest(package: &mut ZipArchive<File>) -> Result<Value, SaveError> {
    let mut entry = package
        .by_name("manifest.json")
        .map_err(|_| SaveError::Invalid("Save backup manifest is invalid.".to_owned()))?;
    let mut buf = Vec::new();
    entry
        .by_ref()
        .take(1024 * 1024 + 1)
        .read_to_end(&mut buf)
        .map_err(|_| SaveError::Invalid("Save backup manifest is invalid.".to_owned()))?;
    if buf.len() > 1024 * 1024 {
        return Err(SaveError::Invalid(
            "Save backup manifest is too large.".to_owned(),
        ));
    }
    serde_json::from_slice(&buf)
        .map_err(|_| SaveError::Invalid("Save backup manifest is invalid.".to_owned()))
}

#[derive(Clone)]
struct MatchedRoot {
    path: PathBuf,
    is_file: bool,
}

fn match_save_roots(
    manifest: &Value,
    configured: &[PathBuf],
) -> Result<Vec<MatchedRoot>, SaveError> {
    let Some(saved_roots) = manifest.get("roots").and_then(Value::as_array) else {
        return Err(SaveError::Invalid(
            "Save backup roots do not match this game.".to_owned(),
        ));
    };
    if saved_roots.is_empty() || saved_roots.len() > configured.len() {
        return Err(SaveError::Invalid(
            "Save backup roots do not match this game.".to_owned(),
        ));
    }
    let mut roots = Vec::new();
    let mut seen: HashSet<PathBuf> = HashSet::new();
    for item in saved_roots {
        let Some(obj) = item.as_object() else {
            return Err(SaveError::Invalid(
                "Save backup roots do not match this game.".to_owned(),
            ));
        };
        let saved_str = obj.get("path").and_then(Value::as_str).unwrap_or("");
        let saved_path = expand_user_path(saved_str);
        let matches = configured
            .iter()
            .any(|c| *c == saved_path || *c == resolve_non_strict(&saved_path));
        if !matches || !seen.insert(saved_path.clone()) {
            return Err(SaveError::Invalid(
                "Save backup roots do not match this game.".to_owned(),
            ));
        }
        let expected_file = obj.get("file").and_then(Value::as_bool).unwrap_or(false);
        if saved_path.exists() && saved_path.is_file() != expected_file {
            return Err(SaveError::Invalid(
                "Save backup root type does not match this game.".to_owned(),
            ));
        }
        reject_symlink_components(&saved_path)?;
        roots.push(MatchedRoot {
            path: saved_path,
            is_file: expected_file,
        });
    }
    Ok(roots)
}

fn has_symlink_between(base: &Path, candidate: &Path) -> bool {
    let mut cur = candidate;
    loop {
        if cur == base {
            return false;
        }
        if cur
            .symlink_metadata()
            .map(|m| m.file_type().is_symlink())
            .unwrap_or(false)
        {
            return true;
        }
        match cur.parent() {
            Some(p) if p != cur => cur = p,
            _ => return false,
        }
    }
}

fn compute_save_destinations(
    infos: &[ArchiveMemberInfo],
    roots: &[MatchedRoot],
) -> Result<Vec<(ArchiveMemberInfo, PathBuf, PathBuf)>, SaveError> {
    let mut destinations = Vec::new();
    for info in infos {
        if info.is_dir || !info.filename.starts_with("roots/") {
            continue;
        }
        let parts: Vec<&str> = info.filename.split('/').collect();
        if parts.len() < 3 {
            continue;
        }
        let index: usize = parts[1]
            .parse()
            .map_err(|_| SaveError::Invalid("Invalid save backup manifest.".to_owned()))?;
        if index >= roots.len() {
            return Err(SaveError::Invalid(
                "Invalid save backup manifest.".to_owned(),
            ));
        }
        let root_info = &roots[index];
        let mut rel = PathBuf::new();
        for seg in &parts[2..] {
            rel.push(seg);
        }
        if root_info.is_file {
            let expected = root_info
                .path
                .file_name()
                .map(PathBuf::from)
                .unwrap_or_default();
            if rel != expected {
                return Err(SaveError::Invalid(
                    "Save backup contains an unexpected file.".to_owned(),
                ));
            }
        }
        let base_raw = if root_info.is_file {
            root_info
                .path
                .parent()
                .unwrap_or_else(|| Path::new("."))
                .to_path_buf()
        } else {
            root_info.path.clone()
        };
        reject_symlink_components(&root_info.path)?;
        let base = resolve_non_strict(&base_raw);
        reject_symlink_components(&base)?;
        let raw_dest = base.join(&rel);
        let destination = resolve_non_strict(&raw_dest);
        if destination != base && !destination.starts_with(&base) {
            return Err(SaveError::Invalid(
                "Save backup contains an unsafe path.".to_owned(),
            ));
        }
        if has_symlink_between(&base, &raw_dest) {
            return Err(SaveError::Invalid(
                "Save restore destination contains a symlink.".to_owned(),
            ));
        }
        destinations.push((info.clone(), destination, base));
    }
    Ok(destinations)
}

pub fn backup_metadata(game: &Value, root: &Path, backup_name: &str) -> Result<Value, SaveError> {
    let archive = locate_archive(game, root, backup_name)?;
    let meta = fs::metadata(&archive).map_err(|e| SaveError::Io(e.to_string()))?;
    let name = archive
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("")
        .to_owned();
    let mut out = Map::new();
    out.insert("name".into(), Value::String(name.clone()));
    out.insert("size".into(), json!(meta.len()));
    out.insert("mtime".into(), json!(file_mtime_secs(&meta)));
    out.insert(
        "label".into(),
        Value::String(label_from_archive_name(&name)),
    );
    out.insert("emulator_id".into(), Value::String(String::new()));
    out.insert("core".into(), Value::String(String::new()));
    out.insert("slot".into(), Value::String(String::new()));

    if let Ok(file) = File::open(&archive)
        && let Ok(mut pkg) = ZipArchive::new(file)
        && let Ok(manifest) = load_save_manifest(&mut pkg)
        && let Some(obj) = manifest.as_object()
    {
        if let Some(lbl) = obj.get("label").and_then(Value::as_str).map(str::trim)
            && !lbl.is_empty()
        {
            out.insert("label".into(), Value::String(lbl.to_owned()));
        }
        for k in ["emulator_id", "core", "slot"] {
            if let Some(v) = obj.get(k).and_then(Value::as_str) {
                out.insert(k.into(), Value::String(v.to_owned()));
            }
        }
    }
    Ok(Value::Object(out))
}

pub fn list_backup_metadata(game: &Value, root: &Path) -> Result<Vec<Value>, SaveError> {
    let mut out = Vec::new();
    for arch in list_backups(game, root)? {
        if let Some(name) = arch.file_name().and_then(|s| s.to_str()) {
            out.push(backup_metadata(game, root, name)?);
        }
    }
    Ok(out)
}

fn manifest_entry_destination(entry: &Value, roots: &[MatchedRoot]) -> String {
    let Some(idx) = entry
        .get("root")
        .and_then(Value::as_u64)
        .map(|v| v as usize)
    else {
        return String::new();
    };
    let Some(root_info) = roots.get(idx) else {
        return String::new();
    };
    let rel = entry.get("relative").and_then(Value::as_str).unwrap_or("");
    if rel.is_empty() {
        return String::new();
    }
    let base = if root_info.is_file {
        root_info.path.parent().unwrap_or_else(|| Path::new("."))
    } else {
        root_info.path.as_path()
    };
    base.join(rel).to_string_lossy().into_owned()
}

pub fn preview_restore(
    game: &Value,
    root: &Path,
    backup_name: &str,
    selected: Option<&[String]>,
) -> Result<Value, SaveError> {
    let _guard = SAVE_SYNC_LOCK.lock();
    let archive = locate_archive(game, root, backup_name)?;
    let file = File::open(&archive).map_err(|e| SaveError::Io(e.to_string()))?;
    let mut pkg = ZipArchive::new(file)
        .map_err(|_| SaveError::Invalid("Save backup archive is invalid.".to_owned()))?;
    let infos = validate_save_archive_entries(&mut pkg)?;
    let manifest = load_save_manifest(&mut pkg)?;
    let configured = save_roots(game)?;
    let roots = match_save_roots(&manifest, &configured)?;
    let mut destinations = compute_save_destinations(&infos, &roots)?;
    let archive_names: HashSet<String> = destinations
        .iter()
        .map(|(info, _, _)| info.filename.clone())
        .collect();
    let manifest_entries = manifest
        .get("entries")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();

    let wanted: Option<HashSet<String>> = if let Some(sel) = selected {
        if sel.is_empty() || sel.len() > MAX_SAVE_ARCHIVE_MEMBERS {
            return Err(SaveError::Invalid(
                "Select at least one save file to restore.".to_owned(),
            ));
        }
        let set: HashSet<String> = sel.iter().cloned().collect();
        if !set.is_subset(&archive_names) {
            return Err(SaveError::Invalid(
                "Save selection does not match the backup.".to_owned(),
            ));
        }
        destinations.retain(|(info, _, _)| set.contains(&info.filename));
        Some(set)
    } else {
        None
    };

    let mut entries_by_archive: HashMap<String, &Value> = HashMap::new();
    for entry in &manifest_entries {
        if let Some(arch) = entry.get("archive").and_then(Value::as_str) {
            entries_by_archive.entry(arch.to_owned()).or_insert(entry);
        }
    }

    let mut add = Vec::new();
    let mut overwrite = Vec::new();
    let mut unchanged = 0usize;
    for (info, destination, _base) in &destinations {
        let backup_sha = entries_by_archive
            .get(&info.filename)
            .and_then(|e| e.get("sha256"))
            .and_then(Value::as_str)
            .unwrap_or("");
        if !destination.exists() || !destination.is_file() {
            add.push(json!({
                "archive": info.filename,
                "path": destination.to_string_lossy(),
                "size": info.file_size,
                "sha256": backup_sha,
            }));
            continue;
        }
        let disk_sha = file_sha256(destination)?;
        if !backup_sha.is_empty() && disk_sha == backup_sha {
            unchanged += 1;
            continue;
        }
        overwrite.push(json!({
            "archive": info.filename,
            "path": destination.to_string_lossy(),
            "backup_sha": backup_sha,
            "disk_sha": disk_sha,
            "size": info.file_size,
        }));
    }

    let mut missing = Vec::new();
    for entry in &manifest_entries {
        let Some(member) = entry.get("archive").and_then(Value::as_str) else {
            continue;
        };
        if archive_names.contains(member) {
            continue;
        }
        if let Some(w) = &wanted
            && !w.contains(member)
        {
            continue;
        }
        missing.push(json!({
            "archive": member,
            "path": manifest_entry_destination(entry, &roots),
        }));
    }

    let counts = json!({
        "add": add.len(),
        "overwrite": overwrite.len(),
        "remove": 0,
        "missing": missing.len(),
        "unchanged": unchanged,
    });
    Ok(json!({
        "archive": archive.file_name().and_then(|s| s.to_str()).unwrap_or(""),
        "game": manifest.get("game").and_then(Value::as_str).unwrap_or(""),
        "add": &add[..add.len().min(PREVIEW_BUCKET_LIMIT)],
        "overwrite": &overwrite[..overwrite.len().min(PREVIEW_BUCKET_LIMIT)],
        "remove": [],
        "missing": &missing[..missing.len().min(PREVIEW_BUCKET_LIMIT)],
        "conflict": !overwrite.is_empty(),
        "unchanged": unchanged,
        "counts": counts,
        "truncated": {
            "add": add.len() > PREVIEW_BUCKET_LIMIT,
            "overwrite": overwrite.len() > PREVIEW_BUCKET_LIMIT,
            "remove": false,
            "missing": missing.len() > PREVIEW_BUCKET_LIMIT,
        },
    }))
}

pub fn inspect_save_backup(
    game: &Value,
    root: &Path,
    backup_name: &str,
) -> Result<Value, SaveError> {
    let archive = locate_archive(game, root, backup_name)?;
    let file = File::open(&archive).map_err(|e| SaveError::Io(e.to_string()))?;
    let mut pkg = ZipArchive::new(file)
        .map_err(|_| SaveError::Invalid("Save backup archive is invalid.".to_owned()))?;
    let infos = validate_save_archive_entries(&mut pkg)?;
    let manifest = load_save_manifest(&mut pkg)?;
    let configured = save_roots(game)?;
    let roots = match_save_roots(&manifest, &configured)?;
    let destinations = compute_save_destinations(&infos, &roots)?;
    let files: Vec<Value> = destinations
        .iter()
        .map(|(info, dest, _)| {
            json!({
                "archive": info.filename,
                "path": dest.to_string_lossy(),
                "size": info.file_size,
            })
        })
        .collect();
    Ok(json!({
        "archive": archive.file_name().and_then(|s| s.to_str()).unwrap_or(""),
        "game": manifest.get("game").and_then(Value::as_str).unwrap_or(""),
        "files": files,
        "manifest": manifest,
    }))
}

pub fn restore_saves(
    game: &Value,
    root: &Path,
    backup_name: &str,
    selected: Option<&[String]>,
    dry_run: bool,
) -> Result<Value, SaveError> {
    let _guard = SAVE_SYNC_LOCK.lock();
    let archive = locate_archive(game, root, backup_name)?;
    let file = File::open(&archive).map_err(|e| SaveError::Io(e.to_string()))?;
    let mut pkg = ZipArchive::new(file)
        .map_err(|_| SaveError::Invalid("Save backup archive is invalid.".to_owned()))?;
    let infos = validate_save_archive_entries(&mut pkg)?;
    let manifest = load_save_manifest(&mut pkg)?;
    let configured = save_roots(game)?;
    let roots = match_save_roots(&manifest, &configured)?;
    let mut destinations = compute_save_destinations(&infos, &roots)?;
    if let Some(sel) = selected {
        if sel.is_empty() || sel.len() > MAX_SAVE_ARCHIVE_MEMBERS {
            return Err(SaveError::Invalid(
                "Select at least one save file to restore.".to_owned(),
            ));
        }
        let available: HashSet<&str> = destinations
            .iter()
            .map(|(i, _, _)| i.filename.as_str())
            .collect();
        let wanted: HashSet<&str> = sel.iter().map(String::as_str).collect();
        if !wanted.is_subset(&available) {
            return Err(SaveError::Invalid(
                "Save selection does not match the backup.".to_owned(),
            ));
        }
        destinations.retain(|(i, _, _)| wanted.contains(i.filename.as_str()));
    }
    let arch_name = archive
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("")
        .to_owned();
    if dry_run {
        let files: Vec<Value> = destinations
            .iter()
            .map(
                |(info, dest, _)| json!({"archive": info.filename, "path": dest.to_string_lossy()}),
            )
            .collect();
        return Ok(json!({
            "archive": arch_name,
            "dry_run": true,
            "files": files,
        }));
    }

    let first_path = game
        .get("save_paths")
        .and_then(Value::as_array)
        .and_then(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .find(|s| !s.trim().is_empty())
        })
        .unwrap_or("");
    let src_meta = if !first_path.is_empty() {
        save_source_for(game, first_path)
    } else {
        Map::new()
    };
    let emu_opt = src_meta
        .get("emulator_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty());
    let core_opt = src_meta
        .get("core")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty());
    let slot_opt = src_meta
        .get("slot")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty());
    backup_saves(
        game,
        root,
        "before-restore",
        true,
        emu_opt,
        core_opt,
        slot_opt,
    )?;

    for (info, destination, base) in &destinations {
        if let Some(parent) = destination.parent() {
            fs::create_dir_all(parent).map_err(|e| SaveError::Io(e.to_string()))?;
        }
        let resolved = resolve_non_strict(destination);
        if resolved != *base && !resolved.starts_with(base) {
            return Err(SaveError::Invalid(
                "Save backup contains an unsafe path.".to_owned(),
            ));
        }
        if has_symlink_between(base, &resolved) {
            return Err(SaveError::Invalid(
                "Save restore destination contains a symlink.".to_owned(),
            ));
        }
        let entry = pkg
            .by_name(&info.filename)
            .map_err(|e| SaveError::Invalid(e.to_string()))?;
        atomic_copy_stream(
            entry,
            destination,
            0o600,
            Some(MAX_SAVE_ARCHIVE_MEMBER_BYTES as usize),
        )
        .map_err(|e| SaveError::Io(e.to_string()))?;
    }
    Ok(json!({
        "archive": arch_name,
        "path": archive.to_string_lossy(),
    }))
}

pub fn enforce_backup_limit(game: &Value, root: &Path, max_backups: usize) -> usize {
    if max_backups == 0 {
        return 0;
    }
    let Ok(backups) = list_backups(game, root) else {
        return 0;
    };
    let mut removed = 0;
    for arch in backups.into_iter().skip(max_backups) {
        if fs::remove_file(&arch).is_ok() {
            removed += 1;
        }
    }
    removed
}

pub fn scan_all_saves(games: &mut [Value], home: Option<&Path>) -> BTreeMap<usize, Vec<String>> {
    let mut found = BTreeMap::new();
    for (idx, game) in games.iter_mut().enumerate() {
        let mut paths = Vec::new();
        let mut sources: HashMap<String, Value> = HashMap::new();
        let mut combined = discover_save_paths(game, home);
        combined.extend(extra_save_candidates(game, home));
        for item in combined {
            let p = item.get("path").and_then(Value::as_str).unwrap_or("");
            if p.is_empty() {
                continue;
            }
            if !paths.iter().any(|x| x == p) {
                paths.push(p.to_owned());
            }
            sources.insert(
                p.to_owned(),
                json!({
                    "emulator_id": item.get("emulator_id").and_then(Value::as_str).unwrap_or(""),
                    "core": item.get("core").and_then(Value::as_str).unwrap_or(""),
                    "slot": item.get("slot").and_then(Value::as_str).unwrap_or(""),
                }),
            );
        }
        let existing: Vec<String> = paths
            .into_iter()
            .filter(|p| Path::new(p).exists())
            .collect();
        if !existing.is_empty() {
            let mut src_map = Map::new();
            for p in &existing {
                if let Some(meta) = sources.get(p) {
                    src_map.insert(p.clone(), meta.clone());
                }
            }
            if let Some(obj) = game.as_object_mut() {
                obj.insert("save_sources".into(), Value::Object(src_map));
            }
            found.insert(idx, existing);
        }
    }
    found
}

pub fn games_with_saves(games: &mut [Value], home: Option<&Path>) -> Vec<usize> {
    let home_key = home
        .map(|p| p.to_string_lossy().into_owned())
        .or_else(|| home_dir().map(|p| p.to_string_lossy().into_owned()))
        .unwrap_or_default();
    let mut sig = String::new();
    for (idx, g) in games.iter().enumerate() {
        let gid = g.get("game_id").and_then(Value::as_str).unwrap_or("");
        let path = g.get("path").and_then(Value::as_str).unwrap_or("");
        sig.push_str(&format!("{idx}:{gid}:{path}:"));
        if let Some(arr) = g.get("save_paths").and_then(Value::as_array) {
            for s in arr.iter().filter_map(Value::as_str) {
                if !s.trim().is_empty() {
                    sig.push_str(s);
                    sig.push(',');
                }
            }
        }
        sig.push(';');
    }
    sig.push_str(&home_key);

    {
        let cache = SAVE_CACHE.lock();
        if let Some(at) = cache.at
            && cache.signature == sig
            && at.elapsed().as_secs() < 2
        {
            return cache.indices.clone();
        }
    }
    let scanned = scan_all_saves(games, home);
    let mut indices: HashSet<usize> = scanned.keys().copied().collect();
    for (idx, g) in games.iter().enumerate() {
        if let Some(arr) = g.get("save_paths").and_then(Value::as_array)
            && arr
                .iter()
                .filter_map(Value::as_str)
                .any(|p| !p.trim().is_empty() && expand_user_path(p).exists())
        {
            indices.insert(idx);
        }
    }
    let mut result: Vec<usize> = indices.into_iter().collect();
    result.sort();
    let mut cache = SAVE_CACHE.lock();
    cache.at = Some(Instant::now());
    cache.signature = sig;
    cache.indices = result.clone();
    result
}
