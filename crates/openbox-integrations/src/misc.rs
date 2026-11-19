//! External integrations: OBS recording directory/status/attach and MAME high scores
//! export/import (`pkg/parity/parity_integrations.py`).

use std::fs::{self, File};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use jiff::Timestamp;
use openbox_io::{atomic_copy_stream, atomic_write_text};
use openbox_platform::{contained_path, expand_user, home_dir, safe_filename_component, which};
use serde_json::{Map, Value, json};

pub fn mame_highscore_path(home: Option<&Path>) -> PathBuf {
    let base = home
        .map(Path::to_path_buf)
        .unwrap_or_else(|| home_dir().unwrap_or_else(|| PathBuf::from(".")));
    let candidates = [
        base.join(".mame/hi"),
        base.join(".config/mame/hi"),
        base.join(".var/app/org.mamedev.MAME/config/mame/hi"),
    ];
    for p in &candidates {
        if p.exists() {
            return p.clone();
        }
    }
    candidates[0].clone()
}

pub fn read_local_highscores(game: &Value, home: Option<&Path>) -> Vec<Value> {
    let rom = game
        .get("rom_name")
        .and_then(Value::as_str)
        .filter(|s| !s.trim().is_empty())
        .map(|s| s.trim().to_owned())
        .unwrap_or_else(|| {
            Path::new(game.get("path").and_then(Value::as_str).unwrap_or(""))
                .file_stem()
                .and_then(|s| s.to_str())
                .unwrap_or("")
                .trim()
                .to_owned()
        });
    if rom.is_empty() || rom.chars().any(|c| matches!(c, '*' | '?' | '[' | ']')) {
        return Vec::new();
    }
    let root = mame_highscore_path(home);
    let Ok(root_resolved) = fs::canonicalize(&root) else {
        return Vec::new();
    };
    let Ok(rd) = fs::read_dir(&root) else {
        return Vec::new();
    };
    let mut scores = Vec::new();
    for entry in rd.flatten() {
        let p = entry.path();
        let name = entry.file_name().to_string_lossy().into_owned();
        if !name.starts_with(&rom) {
            continue;
        }
        let Ok(meta) = fs::symlink_metadata(&p) else {
            continue;
        };
        if meta.file_type().is_symlink() || !meta.is_file() {
            continue;
        }
        let Ok(resolved) = fs::canonicalize(&p) else {
            continue;
        };
        if !resolved.starts_with(&root_resolved) {
            continue;
        }
        let label = p
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or(&name)
            .to_owned();
        scores.push(json!({
            "file": name,
            "size": meta.len(),
            "label": label,
        }));
    }
    scores
}

pub fn export_highscores(
    game: &Value,
    export_dir: &Path,
    home: Option<&Path>,
) -> Result<Value, String> {
    let root = mame_highscore_path(home);
    fs::create_dir_all(export_dir).map_err(|e| e.to_string())?;
    let mut copied = Vec::new();
    for score in read_local_highscores(game, home) {
        let Some(fname) = score.get("file").and_then(Value::as_str) else {
            continue;
        };
        let src = root.join(fname);
        if !src.is_file() {
            continue;
        }
        let dst = export_dir.join(fname);
        fs::copy(&src, &dst).map_err(|e| e.to_string())?;
        copied.push(dst.to_string_lossy().into_owned());
    }
    let rom = game
        .get("rom_name")
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
    let manifest_path = export_dir.join("highscores.json");
    let manifest = json!({
        "format": 1,
        "exported_at": Timestamp::now().strftime("%Y-%m-%dT%H:%M:%S").to_string(),
        "game": game.get("name").cloned().unwrap_or(Value::Null),
        "rom": rom,
        "files": copied,
    });
    let text = serde_json::to_string_pretty(&manifest).map_err(|e| e.to_string())? + "\n";
    atomic_write_text(&manifest_path, &text, 0o600).map_err(|e| e.to_string())?;
    Ok(json!({
        "files": copied,
        "manifest": manifest_path.to_string_lossy(),
    }))
}

pub fn import_highscores(
    game: &Value,
    import_dir: &Path,
    home: Option<&Path>,
) -> Result<Vec<String>, String> {
    let folder = expand_user(import_dir);
    let manifest = folder.join("highscores.json");
    let files: Vec<String> = if manifest.is_file() {
        let raw = fs::read_to_string(&manifest)
            .map_err(|_| "High score bundle is invalid.".to_owned())?;
        let parsed: Value =
            serde_json::from_str(&raw).map_err(|_| "High score bundle is invalid.".to_owned())?;
        parsed
            .get("files")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .map(str::to_owned)
                    .collect()
            })
            .unwrap_or_default()
    } else {
        fs::read_dir(&folder)
            .ok()
            .into_iter()
            .flatten()
            .flatten()
            .map(|e| e.path())
            .filter(|p| p.is_file())
            .map(|p| p.to_string_lossy().into_owned())
            .collect()
    };

    let raw_rom = game
        .get("rom_name")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .unwrap_or_else(|| {
            Path::new(game.get("path").and_then(Value::as_str).unwrap_or(""))
                .file_stem()
                .and_then(|s| s.to_str())
                .filter(|s| !s.is_empty())
                .unwrap_or("game")
                .to_owned()
        });
    let rom = safe_filename_component(&raw_rom, "rom_name").map_err(|e| e.to_string())?;
    let target = mame_highscore_path(home);
    fs::create_dir_all(&target).map_err(|e| e.to_string())?;
    let folder_resolved = fs::canonicalize(&folder).map_err(|e| e.to_string())?;

    let mut restored = Vec::new();
    for file_str in files {
        let cand = Path::new(&file_str);
        let joined = if cand.is_absolute() {
            cand.to_path_buf()
        } else {
            folder.join(cand)
        };
        let Ok(source) = fs::canonicalize(&joined) else {
            continue;
        };
        if !source.starts_with(&folder_resolved) || !source.is_file() {
            continue;
        }
        let sname = source
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("score.hi");
        let dest_name = if sname.contains(&rom) {
            sname.to_owned()
        } else {
            format!("{rom}-{sname}")
        };
        let dest = contained_path(&target, &[dest_name.as_str()]).map_err(|e| e.to_string())?;
        let mut f = File::open(&source).map_err(|e| e.to_string())?;
        atomic_copy_stream(&mut f, &dest, 0o600, Some(64 * 1024 * 1024))
            .map_err(|e| e.to_string())?;
        restored.push(dest.to_string_lossy().into_owned());
    }
    if restored.is_empty() {
        return Err("No high score files were found in the import bundle.".to_owned());
    }
    Ok(restored)
}

pub fn obs_recording_directory(home: Option<&Path>) -> PathBuf {
    let base = home
        .map(Path::to_path_buf)
        .unwrap_or_else(|| home_dir().unwrap_or_else(|| PathBuf::from(".")));
    let profile_roots = [
        base.join(".config/obs-studio/basic/profiles"),
        base.join(".var/app/com.obsproject.Studio/config/obs-studio/basic/profiles"),
    ];
    for profiles in profile_roots {
        let Ok(rd) = fs::read_dir(&profiles) else {
            continue;
        };
        let mut dirs: Vec<PathBuf> = rd
            .flatten()
            .map(|e| e.path())
            .filter(|p| p.is_dir())
            .collect();
        dirs.sort();
        dirs.reverse();
        for profile in dirs {
            let ini = profile.join("basic.ini");
            let Ok(text) = fs::read_to_string(&ini) else {
                continue;
            };
            let mut section = String::new();
            for line in text.lines() {
                let trimmed = line.trim();
                if trimmed.starts_with('[') && trimmed.ends_with(']') {
                    section = trimmed[1..trimmed.len() - 1].trim().to_owned();
                    continue;
                }
                if section != "SimpleOutput" && section != "AdvOut" {
                    continue;
                }
                if let Some((k, v)) = trimmed.split_once('=') {
                    let key = k.trim();
                    let val = v.trim();
                    if (key == "FilePath" || key == "RecFilePath") && !val.is_empty() {
                        return expand_user(Path::new(val));
                    }
                }
            }
        }
    }
    base.join("Videos")
}

fn find_latest_recording_in(dir: &Path, since_secs: Option<f64>) -> Option<String> {
    const EXTS: &[&str] = &["mp4", "mkv", "mov", "flv", "webm"];
    fn walk(dir: &Path, since_secs: Option<f64>, best: &mut Option<(f64, PathBuf)>) {
        let Ok(rd) = fs::read_dir(dir) else {
            return;
        };
        for entry in rd.flatten() {
            let p = entry.path();
            if p.is_dir() {
                walk(&p, since_secs, best);
            } else if p.is_file() {
                let ext = p
                    .extension()
                    .and_then(|e| e.to_str())
                    .map(str::to_lowercase)
                    .unwrap_or_default();
                if !EXTS.contains(&ext.as_str()) {
                    continue;
                }
                let Ok(mtime) = fs::metadata(&p)
                    .and_then(|m| m.modified())
                    .and_then(|t| t.duration_since(UNIX_EPOCH).map_err(std::io::Error::other))
                else {
                    continue;
                };
                let secs = mtime.as_secs_f64();
                if let Some(cutoff) = since_secs
                    && secs < cutoff - 1.0
                {
                    continue;
                }
                if best.as_ref().is_none_or(|(b, _)| secs > *b) {
                    *best = Some((secs, p));
                }
            }
        }
    }
    let root = expand_user(dir);
    if !root.is_dir() {
        return None;
    }
    let mut best = None;
    walk(&root, since_secs, &mut best);
    best.map(|(_, p)| p.to_string_lossy().into_owned())
}

fn pgrep_running(name: &str) -> bool {
    Command::new("pgrep")
        .args(["-x", name])
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

pub fn obs_recording_status(home: Option<&Path>) -> Value {
    let dir = obs_recording_directory(home);
    let latest = find_latest_recording_in(&dir, None);
    let running = which("obs").is_some() && pgrep_running("obs");
    let cutoff = SystemTime::now()
        .checked_sub(Duration::from_secs(120))
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map(|d| d.as_secs_f64());
    let recording = running && find_latest_recording_in(&dir, cutoff).is_some();
    json!({
        "running": running,
        "recording": recording,
        "directory": dir.to_string_lossy(),
        "latest_recording": latest,
    })
}

pub fn attach_recording(game: &mut Map<String, Value>, video_path: &str) -> Result<String, String> {
    let path = expand_user(Path::new(video_path));
    if !path.is_file() {
        return Err("Recording file not found.".to_owned());
    }
    let s = path.to_string_lossy().into_owned();
    game.insert("video_recording".into(), Value::String(s.clone()));
    if game
        .get("video")
        .and_then(Value::as_str)
        .is_none_or(str::is_empty)
    {
        game.insert("video".into(), Value::String(s.clone()));
    }
    Ok(s)
}
