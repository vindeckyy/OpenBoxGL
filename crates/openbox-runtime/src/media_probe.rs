//! Media path validation, TTL file probing, and media field definitions.
//! Mirrors `pkg/state/media_probe.py`.

use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use openbox_platform::{expand_user, resolve_non_strict, user_symlink_component};
use parking_lot::Mutex;
use serde_json::Value;

pub const MEDIA_ROOTS_ENV: &str = "OPENBOX_MEDIA_ROOTS";
pub const FILE_PROBE_TTL: Duration = Duration::from_secs(120);
pub const FILE_PROBE_MAX: usize = 250_000;
pub const SANITIZE_MEDIA_PATH_MAX: usize = 250_000;

pub const FIELDS: &[&str] = &[
    "name",
    "platform",
    "genre",
    "year",
    "developer",
    "publisher",
    "series",
    "manual_entry",
    "collection",
    "description",
    "path",
    "launch",
    "launch_profile",
    "cover",
    "background",
    "clear_logo",
    "fanart",
    "banner",
    "icon",
    "box_back",
    "box_spine",
    "box_3d",
    "title_screen",
    "cart_front",
    "cart_back",
    "disc",
    "advertisement",
    "manual",
    "source",
    "steam_app_id",
    "lutris_id",
    "install_dir",
    "heroic_app_id",
    "rom_name",
    "clone_of",
    "set_type",
    "ra_game_id",
    "ra_hash",
    "launchbox_db_id",
    "archive_member",
    "video",
    "music",
    "video_snap",
    "video_theme",
    "video_trailer",
    "video_recording",
    "progress",
    "rating",
    "user_rating",
    "notes",
    "region",
    "play_mode",
    "sort_title",
    "added_at",
    "alternate_names",
    "max_players",
    "wikipedia_url",
    "video_url",
    "hide_in_bigbox",
    "esrb",
    "broken",
    "portable",
    "controller_support",
    "disc_count",
    "gameyfin_id",
    "gameyfin_provider",
    "store_catalog",
    "store_installed",
    "owned",
    "gog_id",
    "ea_id",
    "ubisoft_id",
    "tracking_mode",
    "tracking_delay",
    "tracking_frequency",
    "tracking_process_name",
    "igdb_id",
    "gamescope_preset",
    "screenscraper_id",
    "cover_model",
];

pub const MEDIA_TYPES_ALL: &[&str] = &[
    "cover",
    "background",
    "screenshots",
    "clear_logo",
    "fanart",
    "banner",
    "icon",
    "box_back",
    "box_spine",
    "box_3d",
    "title_screen",
    "cart_front",
    "cart_back",
    "disc",
    "advertisement",
    "manual",
    "poster",
    "square",
    "video",
    "video_snap",
    "video_theme",
    "video_trailer",
    "music",
];

pub const MEDIA_PATH_FIELDS: &[&str] = &[
    "cover",
    "background",
    "clear_logo",
    "fanart",
    "banner",
    "icon",
    "box_back",
    "box_spine",
    "box_3d",
    "title_screen",
    "cart_front",
    "cart_back",
    "disc",
    "advertisement",
    "manual",
    "video",
    "music",
    "video_snap",
    "video_theme",
    "video_trailer",
    "video_recording",
    "poster",
    "square",
];

pub const VIDEO_MEDIA_TYPES: &[&str] = &["video", "video_snap", "video_theme", "video_trailer"];
pub const MUSIC_MEDIA_TYPES: &[&str] = &["music"];

pub struct MediaProbeService {
    data_dir: PathBuf,
    probe_cache_file: Mutex<HashMap<String, (Instant, bool)>>,
    probe_cache_any: Mutex<HashMap<String, (Instant, bool)>>,
    sanitize_cache: Mutex<HashMap<String, String>>,
}

impl MediaProbeService {
    pub fn new(data_dir: impl Into<PathBuf>) -> Self {
        Self {
            data_dir: data_dir.into(),
            probe_cache_file: Mutex::new(HashMap::new()),
            probe_cache_any: Mutex::new(HashMap::new()),
            sanitize_cache: Mutex::new(HashMap::new()),
        }
    }

    pub fn clear_caches(&self) {
        self.probe_cache_file.lock().clear();
        self.probe_cache_any.lock().clear();
        self.sanitize_cache.lock().clear();
    }

    pub fn probe_path(&self, path: &str, file_only: bool) -> bool {
        if path.is_empty() {
            return false;
        }
        let now = Instant::now();
        let cache_mutex = if file_only {
            &self.probe_cache_file
        } else {
            &self.probe_cache_any
        };
        {
            let mut cache = cache_mutex.lock();
            if let Some(&(ts, result)) = cache.get(path) {
                if now.duration_since(ts) < FILE_PROBE_TTL {
                    return result;
                }
                cache.remove(path);
            }
        }
        let result = match fs::metadata(path) {
            Ok(meta) => !file_only || meta.is_file(),
            Err(_) => false,
        };
        let mut cache = cache_mutex.lock();
        if cache.len() >= FILE_PROBE_MAX {
            cache.clear();
        }
        cache.insert(path.to_owned(), (now, result));
        result
    }

    pub fn probe_paths_batch(&self, paths: &[String], file_only: bool) -> HashMap<String, bool> {
        let now = Instant::now();
        let mut unique = Vec::new();
        let mut seen = HashSet::new();
        for p in paths {
            if !p.is_empty() && seen.insert(p.as_str()) {
                unique.push(p.as_str());
            }
        }
        let mut results = HashMap::with_capacity(unique.len());
        let mut misses = Vec::new();
        let cache_mutex = if file_only {
            &self.probe_cache_file
        } else {
            &self.probe_cache_any
        };
        {
            let cache = cache_mutex.lock();
            for &value in &unique {
                if let Some(&(ts, res)) = cache.get(value)
                    && now.duration_since(ts) < FILE_PROBE_TTL
                {
                    results.insert(value.to_owned(), res);
                    continue;
                }
                misses.push(value.to_owned());
            }
        }
        for value in misses {
            let result = match fs::metadata(&value) {
                Ok(meta) => !file_only || meta.is_file(),
                Err(_) => false,
            };
            results.insert(value.clone(), result);
            let mut cache = cache_mutex.lock();
            if cache.len() >= FILE_PROBE_MAX {
                cache.clear();
            }
            cache.insert(value, (now, result));
        }
        results
    }

    pub fn media_roots(&self) -> Result<Vec<PathBuf>, String> {
        let mut values = vec![self.data_dir.join("media")];
        let configured = std::env::var(MEDIA_ROOTS_ENV).unwrap_or_default();
        if configured.len() > 16 * 4096 {
            return Err(format!("{MEDIA_ROOTS_ENV} is too long."));
        }
        let sep = if cfg!(windows) { ';' } else { ':' };
        for item in configured.split(sep) {
            let trimmed = item.trim();
            if trimmed.is_empty() {
                continue;
            }
            let cand = expand_user(Path::new(trimmed));
            if !cand.is_absolute() {
                return Err(format!("{MEDIA_ROOTS_ENV} entries must be absolute paths."));
            }
            values.push(cand);
        }
        if values.len() > 32 {
            return Err(format!("{MEDIA_ROOTS_ENV} contains too many roots."));
        }
        let mut roots = Vec::new();
        for val in values {
            reject_media_symlink_components(&val)?;
            let resolved = resolve_non_strict(&val);
            if resolved == Path::new("/") {
                return Err("Filesystem root is not an approved media root.".to_owned());
            }
            if !roots.contains(&resolved) {
                roots.push(resolved);
            }
        }
        Ok(roots)
    }

    pub fn approved_media_path(&self, path: &str, must_exist: bool) -> Result<PathBuf, String> {
        let trimmed = path.trim();
        if trimmed.is_empty() {
            return Err(
                "Media paths must be absolute paths under an approved media root.".to_owned(),
            );
        }
        let raw = expand_user(Path::new(trimmed));
        if !raw.is_absolute() {
            return Err(
                "Media paths must be absolute paths under an approved media root.".to_owned(),
            );
        }
        reject_media_symlink_components(&raw)?;
        let candidate = resolve_non_strict(&raw);
        let roots = self.media_roots()?;
        if !roots
            .iter()
            .any(|r| candidate != *r && candidate.starts_with(r))
        {
            return Err(format!(
                "Path is outside an approved OpenBox media directory: {}",
                candidate.display()
            ));
        }
        if must_exist && !candidate.is_file() {
            return Err(format!("File not found: {}", candidate.display()));
        }
        Ok(candidate)
    }

    pub fn sanitize_media_path(&self, path: &str) -> String {
        let value = path.trim();
        if value.is_empty() {
            return String::new();
        }
        if let Some(hit) = self.sanitize_cache.lock().get(value).cloned() {
            return hit;
        }
        let result = self
            .approved_media_path(value, false)
            .map(|p| p.to_string_lossy().into_owned())
            .unwrap_or_default();
        let mut cache = self.sanitize_cache.lock();
        if cache.len() >= SANITIZE_MEDIA_PATH_MAX {
            cache.clear();
        }
        cache.insert(value.to_owned(), result.clone());
        result
    }

    pub fn sanitize_document_records(&self, records: &Value) -> Vec<Value> {
        let Some(arr) = records.as_array() else {
            return Vec::new();
        };
        let mut clean = Vec::new();
        for item in arr.iter().take(100) {
            let Some(obj) = item.as_object() else {
                continue;
            };
            let raw_path = obj
                .get("path")
                .and_then(Value::as_str)
                .map(str::trim)
                .unwrap_or("");
            if raw_path.is_empty() {
                continue;
            }
            let Ok(safe) = self.approved_media_path(raw_path, false) else {
                continue;
            };
            let mut rec = obj.clone();
            rec.insert(
                "path".into(),
                Value::String(safe.to_string_lossy().into_owned()),
            );
            clean.push(Value::Object(rec));
        }
        clean
    }

    pub fn approved_backup_file(&self, path: &str) -> Result<PathBuf, String> {
        let trimmed = path.trim();
        let cand = {
            let p = expand_user(Path::new(trimmed));
            if p.is_absolute() {
                p
            } else {
                self.data_dir.join("backups").join(p)
            }
        };
        if cand
            .extension()
            .and_then(|e| e.to_str())
            .map(str::to_ascii_lowercase)
            .as_deref()
            != Some("zip")
        {
            return Err("Backups must be ZIP archives.".to_owned());
        }
        if fs::symlink_metadata(&cand)
            .map(|m| m.file_type().is_symlink())
            .unwrap_or(false)
        {
            return Err("Backup path may not be a symlink.".to_owned());
        }
        if let Some(sym) = user_symlink_component(&cand) {
            return Err(format!(
                "Backup path may not contain symlinks: {}",
                sym.display()
            ));
        }
        let resolved = resolve_non_strict(&cand);
        let roots = [
            resolve_non_strict(&self.data_dir),
            resolve_non_strict(&self.data_dir.join("backups")),
        ];
        if !roots
            .iter()
            .any(|r| resolved != *r && resolved.starts_with(r))
        {
            return Err(
                "Backup archive is outside an approved OpenBox backup directory.".to_owned(),
            );
        }
        if !resolved.is_file() {
            return Err(format!("File not found: {}", resolved.display()));
        }
        Ok(resolved)
    }

    pub fn game_media_paths(&self, game: &Value) -> HashMap<String, String> {
        let mut out = HashMap::new();
        let Some(obj) = game.as_object() else {
            return out;
        };
        for field in MEDIA_PATH_FIELDS {
            if let Some(val) = obj.get(*field).and_then(Value::as_str) {
                let safe = self.sanitize_media_path(val);
                if !safe.is_empty() {
                    out.insert((*field).to_owned(), safe);
                }
            }
        }
        out
    }
}

fn reject_media_symlink_components(path: &Path) -> Result<(), String> {
    if let Some(comp) = user_symlink_component(path) {
        return Err(format!(
            "Media paths may not contain symlinks: {}",
            comp.display()
        ));
    }
    Ok(())
}
