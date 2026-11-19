//! Memory roots auto-import: Steam, RetroArch, Dolphin, and custom screenshot folders
//! (`pkg/parity/parity_memories.py`).

use std::collections::{HashMap, HashSet};
use std::fs::{self, File};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::UNIX_EPOCH;

use openbox_platform::{expand_user, home_dir};
use regex::Regex;
use serde_json::{Map, Value, json};
use sha2::{Digest as _, Sha256};

pub const IMPORT_JOB_NAME: &str = "memories-import";
pub const MEMORIES_FIELD: &str = "memories";
pub const UNASSIGNED_FIELD: &str = "memories_unassigned";
pub const MEDIA_SUBDIR: &str = "memories";
pub const UNASSIGNED_DIR: &str = "_unassigned";

pub const MAX_MEMORIES_PER_GAME: usize = 200;
pub const MAX_IMPORT_FILES: usize = 2000;
pub const MAX_IMPORT_BYTES: u64 = 2 * 1024 * 1024 * 1024;
pub const MAX_FILE_BYTES: u64 = 64 * 1024 * 1024;
pub const MAX_UNASSIGNED: usize = 500;
pub const MAX_IMPORT_ROOTS: usize = 32;
pub const MAX_ERRORS: usize = 20;

static SAFE_COMP_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[^a-zA-Z0-9._-]+").unwrap());
static STEAM_STEM_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^(\d{14})_\d+$").unwrap());
static RETROARCH_STAMP_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"-(\d{6})-(\d{6})$").unwrap());
static DOLPHIN_STAMP_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"-\d+$").unwrap());
static TAG_NOISE_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"[\(\[][^\)\]]*[\)\]]").unwrap());
static NON_ALNUM_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[^a-z0-9]+").unwrap());

#[derive(Debug, Clone)]
pub struct MemoryCandidate {
    pub path: PathBuf,
    pub source: String,
    pub appid: String,
    pub title_id: String,
    pub names: Vec<String>,
}

pub fn memories_import_enabled(settings: &Value) -> bool {
    settings
        .get("memories_import_enabled")
        .and_then(Value::as_bool)
        .unwrap_or(false)
}

pub fn configured_roots(settings: &Value) -> Vec<String> {
    settings
        .get("memories_import_roots")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .take(MAX_IMPORT_ROOTS)
                .map(ToOwned::to_owned)
                .collect()
        })
        .unwrap_or_default()
}

pub fn normalize_memory_key(value: &str) -> String {
    let low = value.to_lowercase();
    let t1 = TAG_NOISE_RE.replace_all(&low, " ");
    let t2 = RETROARCH_STAMP_RE.replace(&t1, "");
    let t3 = DOLPHIN_STAMP_RE.replace(&t2, "");
    NON_ALNUM_RE.replace_all(&t3, "").into_owned()
}

fn safe_component(val: &str) -> String {
    let cleaned = SAFE_COMP_RE.replace_all(val, "_");
    if cleaned.is_empty() {
        "unknown".to_owned()
    } else {
        cleaned.into_owned()
    }
}

fn is_memory_image(path: &Path) -> bool {
    let ext = path
        .extension()
        .and_then(|e| e.to_str())
        .map(|s| format!(".{}", s.to_ascii_lowercase()))
        .unwrap_or_default();
    matches!(
        ext.as_str(),
        ".png" | ".jpg" | ".jpeg" | ".webp" | ".bmp" | ".gif"
    )
}

fn retroarch_cfg_screenshot_dir(home: &Path) -> Option<PathBuf> {
    let cfg = home.join(".config").join("retroarch").join("retroarch.cfg");
    let raw = fs::read_to_string(cfg).ok()?;
    for line in raw.lines() {
        let stripped = line.trim();
        if stripped.starts_with("screenshot_directory")
            && let Some((_, rhs)) = stripped.split_once('=')
        {
            let val = rhs.trim().trim_matches('"');
            if !val.is_empty() && val != "default" {
                return Some(expand_user(Path::new(val)));
            }
            return None;
        }
    }
    None
}

fn steam_remote_dirs(home: &Path) -> Vec<PathBuf> {
    let mut out = Vec::new();
    for root in [
        home.join(".steam").join("steam"),
        home.join(".local").join("share").join("Steam"),
    ] {
        let userdata = root.join("userdata");
        let Ok(rd) = fs::read_dir(&userdata) else {
            continue;
        };
        for entry in rd.flatten() {
            let p = entry.path();
            if p.is_dir() {
                let remote = p.join("760").join("remote");
                if remote.is_dir() {
                    out.push(remote);
                }
            }
        }
    }
    out
}

pub fn discover_memory_roots(
    home_override: Option<&Path>,
    extra_roots: &[String],
) -> (Vec<(String, PathBuf)>, Vec<Value>) {
    let home = home_override
        .map(expand_user)
        .unwrap_or_else(|| home_dir().unwrap_or_else(|| PathBuf::from(".")));
    let mut candidates: Vec<(String, PathBuf)> = Vec::new();
    for remote in steam_remote_dirs(&home) {
        candidates.push(("steam".to_owned(), remote));
    }
    if let Some(cfg_dir) = retroarch_cfg_screenshot_dir(&home) {
        candidates.push(("retroarch".to_owned(), cfg_dir));
    } else {
        candidates.push((
            "retroarch".to_owned(),
            home.join(".config").join("retroarch").join("screenshots"),
        ));
        candidates.push((
            "retroarch".to_owned(),
            home.join("RetroArch").join("screenshots"),
        ));
    }
    candidates.push((
        "dolphin".to_owned(),
        home.join(".local")
            .join("share")
            .join("dolphin-emu")
            .join("ScreenShots"),
    ));
    candidates.push((
        "dolphin".to_owned(),
        home.join("Dolphin Emulator").join("ScreenShots"),
    ));
    for extra in extra_roots {
        candidates.push(("custom".to_owned(), expand_user(Path::new(extra))));
    }

    let mut roots = Vec::new();
    let mut skipped = Vec::new();
    let mut seen = HashSet::new();
    for (kind, path) in candidates {
        let key = fs::canonicalize(&path)
            .unwrap_or_else(|_| path.clone())
            .to_string_lossy()
            .into_owned();
        if !seen.insert(key) {
            continue;
        }
        if !path.exists() {
            if kind == "custom" {
                skipped.push(json!({"path": path.to_string_lossy(), "reason": "missing"}));
            }
            continue;
        }
        if !path.is_dir() || fs::read_dir(&path).is_err() {
            skipped.push(json!({"path": path.to_string_lossy(), "reason": "unreadable"}));
            continue;
        }
        roots.push((kind, path));
    }
    (roots, skipped)
}

fn walk_image_files(root: &Path, out: &mut Vec<PathBuf>, cancel: Option<&AtomicBool>) {
    let Ok(rd) = fs::read_dir(root) else {
        return;
    };
    for entry in rd.flatten() {
        if cancel.is_some_and(|c| c.load(Ordering::Acquire)) {
            return;
        }
        let p = entry.path();
        let Ok(meta) = fs::symlink_metadata(&p) else {
            continue;
        };
        if meta.file_type().is_symlink() {
            continue;
        }
        if meta.is_dir() {
            walk_image_files(&p, out, cancel);
        } else if meta.is_file() && is_memory_image(&p) {
            out.push(p);
        }
    }
}

pub fn scan_candidates(
    roots: &[(String, PathBuf)],
    cancel: Option<&AtomicBool>,
    errors: &mut Vec<Value>,
) -> Vec<MemoryCandidate> {
    let mut out = Vec::new();
    for (kind, root) in roots {
        if cancel.is_some_and(|c| c.load(Ordering::Acquire)) {
            break;
        }
        if kind == "steam" {
            let Ok(rd) = fs::read_dir(root) else {
                errors.push(json!({"path": root.to_string_lossy(), "reason": "scan_failed"}));
                continue;
            };
            let mut appid_dirs: Vec<PathBuf> = rd
                .flatten()
                .map(|e| e.path())
                .filter(|p| {
                    fs::symlink_metadata(p)
                        .map(|m| m.is_dir() && !m.file_type().is_symlink())
                        .unwrap_or(false)
                        && p.file_name()
                            .and_then(|n| n.to_str())
                            .is_some_and(|s| s.bytes().all(|b| b.is_ascii_digit()))
                })
                .collect();
            appid_dirs.sort();
            for adir in appid_dirs {
                let shots = adir.join("screenshots");
                if !shots.is_dir() {
                    continue;
                }
                let appid = adir
                    .file_name()
                    .and_then(|n| n.to_str())
                    .unwrap_or("")
                    .to_owned();
                let mut imgs = Vec::new();
                walk_image_files(&shots, &mut imgs, cancel);
                for p in imgs {
                    if p.parent()
                        .and_then(|pr| pr.file_name())
                        .and_then(|n| n.to_str())
                        == Some("thumbnails")
                    {
                        continue;
                    }
                    out.push(MemoryCandidate {
                        path: p,
                        source: "steam".to_owned(),
                        appid: appid.clone(),
                        title_id: String::new(),
                        names: Vec::new(),
                    });
                }
            }
        } else {
            let mut imgs = Vec::new();
            walk_image_files(root, &mut imgs, cancel);
            for p in imgs {
                let stem = p
                    .file_stem()
                    .and_then(|s| s.to_str())
                    .unwrap_or("")
                    .to_owned();
                let title_id = if kind == "dolphin" {
                    p.parent()
                        .and_then(|pr| pr.file_name())
                        .and_then(|n| n.to_str())
                        .unwrap_or("")
                        .to_owned()
                } else {
                    String::new()
                };
                out.push(MemoryCandidate {
                    path: p,
                    source: kind.clone(),
                    appid: String::new(),
                    title_id,
                    names: vec![stem],
                });
            }
        }
    }
    out
}

fn game_name_keys(game: &Value) -> HashSet<String> {
    let mut keys = HashSet::new();
    for field in ["name", "sort_title", "rom_name"] {
        if let Some(s) = game.get(field).and_then(Value::as_str) {
            let k = normalize_memory_key(s);
            if !k.is_empty() {
                keys.insert(k);
            }
        }
    }
    if let Some(alts) = game.get("alternate_names").and_then(Value::as_array) {
        for a in alts {
            if let Some(s) = a.as_str() {
                let k = normalize_memory_key(s);
                if !k.is_empty() {
                    keys.insert(k);
                }
            }
        }
    }
    if let Some(p) = game.get("path").and_then(Value::as_str)
        && let Some(stem) = Path::new(p).file_stem().and_then(|s| s.to_str())
    {
        let k = normalize_memory_key(stem);
        if !k.is_empty() {
            keys.insert(k);
        }
    }
    keys
}

pub fn match_memory_game(games: &[Value], candidate: &MemoryCandidate) -> (Option<String>, usize) {
    let mut matched: HashSet<String> = HashSet::new();
    let appid = candidate.appid.trim();
    if !appid.is_empty() {
        for g in games {
            let gid = g.get("game_id").and_then(Value::as_str).unwrap_or("");
            let g_app = g
                .get("steam_app_id")
                .map(|v| match v {
                    Value::String(s) => s.trim().to_owned(),
                    Value::Number(n) => n.to_string(),
                    _ => String::new(),
                })
                .unwrap_or_default();
            if g_app == appid && !gid.is_empty() {
                matched.insert(gid.to_owned());
            }
        }
    }
    let tid = normalize_memory_key(&candidate.title_id);
    if !tid.is_empty() {
        for g in games {
            let gid = g.get("game_id").and_then(Value::as_str).unwrap_or("");
            if gid.is_empty() {
                continue;
            }
            let rom_k =
                normalize_memory_key(g.get("rom_name").and_then(Value::as_str).unwrap_or(""));
            let stem_k = g
                .get("path")
                .and_then(Value::as_str)
                .and_then(|p| Path::new(p).file_stem())
                .and_then(|s| s.to_str())
                .map(normalize_memory_key)
                .unwrap_or_default();
            if rom_k == tid || stem_k == tid {
                matched.insert(gid.to_owned());
            }
        }
    }
    let name_keys: HashSet<String> = candidate
        .names
        .iter()
        .map(|n| normalize_memory_key(n))
        .filter(|k| !k.is_empty())
        .collect();
    if !name_keys.is_empty() {
        for g in games {
            let gid = g.get("game_id").and_then(Value::as_str).unwrap_or("");
            if !gid.is_empty() && !game_name_keys(g).is_disjoint(&name_keys) {
                matched.insert(gid.to_owned());
            }
        }
    }
    if matched.len() == 1 {
        (matched.into_iter().next(), 1)
    } else {
        (None, matched.len())
    }
}

fn sha256_file(path: &Path) -> Result<String, std::io::Error> {
    let mut f = File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; 1024 * 1024];
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hex::encode(hasher.finalize()))
}

type SizeBucketEntry = (Option<PathBuf>, Option<String>);

struct DedupeIndex {
    digests: HashSet<String>,
    sizes: HashMap<u64, Vec<SizeBucketEntry>>,
}

impl DedupeIndex {
    fn from_state(state: &Value) -> Self {
        let mut idx = Self {
            digests: HashSet::new(),
            sizes: HashMap::new(),
        };
        if let Some(games) = state.get("games").and_then(Value::as_array) {
            for g in games {
                if let Some(mems) = g.get(MEMORIES_FIELD).and_then(Value::as_array) {
                    for e in mems {
                        idx.record_entry(e);
                    }
                }
                if let Some(shots) = g.get("screenshots").and_then(Value::as_array) {
                    for s in shots {
                        if let Some(ps) = s.as_str() {
                            let p = PathBuf::from(ps);
                            if let Ok(m) = fs::metadata(&p) {
                                idx.sizes.entry(m.len()).or_default().push((Some(p), None));
                            }
                        }
                    }
                }
            }
        }
        if let Some(un) = state.get(UNASSIGNED_FIELD).and_then(Value::as_array) {
            for e in un {
                idx.record_entry(e);
            }
        }
        idx
    }

    fn record_entry(&mut self, entry: &Value) {
        let Some(m) = entry.as_object() else {
            return;
        };
        let digest = m.get("sha256").and_then(Value::as_str).unwrap_or("");
        let path_s = m.get("path").and_then(Value::as_str).unwrap_or("");
        let mut size = m.get("bytes").and_then(Value::as_u64).unwrap_or(0);
        if size == 0 && !path_s.is_empty() {
            size = fs::metadata(path_s).map(|md| md.len()).unwrap_or(0);
        }
        if !digest.is_empty() {
            self.digests.insert(digest.to_owned());
        }
        if size > 0 {
            self.sizes.entry(size).or_default().push((
                if path_s.is_empty() {
                    None
                } else {
                    Some(PathBuf::from(path_s))
                },
                if digest.is_empty() {
                    None
                } else {
                    Some(digest.to_owned())
                },
            ));
        }
    }

    fn is_duplicate(&mut self, size: u64, digest: &str) -> bool {
        if self.digests.contains(digest) {
            return true;
        }
        if let Some(list) = self.sizes.get_mut(&size) {
            for (path_opt, dig_opt) in list.iter_mut() {
                if dig_opt.is_none()
                    && let Some(p) = path_opt
                {
                    *dig_opt = Some(sha256_file(p).unwrap_or_default());
                }
                if dig_opt.as_deref() == Some(digest) {
                    return true;
                }
            }
        }
        false
    }

    fn record(&mut self, size: u64, digest: &str, path: PathBuf) {
        self.digests.insert(digest.to_owned());
        self.sizes
            .entry(size)
            .or_default()
            .push((Some(path), Some(digest.to_owned())));
    }
}

fn parse_taken_at(candidate: &MemoryCandidate, mtime_secs: i64) -> String {
    let stem = candidate
        .path
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("");
    if let Some(caps) = STEAM_STEM_RE.captures(stem) {
        let s = &caps[1];
        if s.len() == 14 {
            return format!(
                "{}-{}-{}T{}:{}:{}",
                &s[0..4],
                &s[4..6],
                &s[6..8],
                &s[8..10],
                &s[10..12],
                &s[12..14]
            );
        }
    }
    if let Some(caps) = RETROARCH_STAMP_RE.captures(stem) {
        let d = &caps[1];
        let t = &caps[2];
        if d.len() == 6 && t.len() == 6 {
            return format!(
                "20{}-{}-{}T{}:{}:{}",
                &d[0..2],
                &d[2..4],
                &d[4..6],
                &t[0..2],
                &t[2..4],
                &t[4..6]
            );
        }
    }
    jiff::Timestamp::from_second(mtime_secs)
        .unwrap_or_else(|_| jiff::Timestamp::now())
        .to_string()
}

pub fn collect_imports(
    state: &Value,
    data_root: &Path,
    extra_roots: &[String],
    cancel: Option<&AtomicBool>,
) -> Value {
    let (roots, mut skipped_roots) = discover_memory_roots(None, extra_roots);
    let mut dedupe = DedupeIndex::from_state(state);
    let games = state
        .get("games")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let media_root = data_root.join("media").join(MEDIA_SUBDIR);
    let pending_unassigned = state
        .get(UNASSIGNED_FIELD)
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0);
    let existing_counts: HashMap<String, usize> = games
        .iter()
        .filter_map(|g| {
            let gid = g.get("game_id")?.as_str()?.to_owned();
            let cnt = g
                .get(MEMORIES_FIELD)
                .and_then(Value::as_array)
                .map(Vec::len)
                .unwrap_or(0);
            Some((gid, cnt))
        })
        .collect();

    let candidates = scan_candidates(&roots, cancel, &mut skipped_roots);
    let mut attach: Map<String, Value> = Map::new();
    let mut unassigned: Vec<Value> = Vec::new();
    let mut scanned = 0usize;
    let mut added = 0usize;
    let mut unassigned_cnt = 0usize;
    let mut skipped = 0usize;
    let mut failed = 0usize;
    let mut errors: Vec<Value> = Vec::new();
    let mut cancelled = false;
    let mut bytes_used = 0u64;

    for cand in candidates {
        if cancel.is_some_and(|c| c.load(Ordering::Acquire)) {
            cancelled = true;
            break;
        }
        scanned += 1;
        if added + unassigned_cnt >= MAX_IMPORT_FILES {
            skipped += 1;
            continue;
        }
        let meta = match fs::metadata(&cand.path) {
            Ok(m) => m,
            Err(e) => {
                failed += 1;
                if errors.len() < MAX_ERRORS {
                    errors.push(Value::String(format!("{}: {e}", cand.path.display())));
                }
                continue;
            }
        };
        let size = meta.len();
        if size > MAX_FILE_BYTES || bytes_used + size > MAX_IMPORT_BYTES {
            skipped += 1;
            continue;
        }
        let digest = match sha256_file(&cand.path) {
            Ok(d) => d,
            Err(e) => {
                failed += 1;
                if errors.len() < MAX_ERRORS {
                    errors.push(Value::String(format!("{}: {e}", cand.path.display())));
                }
                continue;
            }
        };
        if dedupe.is_duplicate(size, &digest) {
            skipped += 1;
            continue;
        }
        let (gid_opt, match_count) = match_memory_game(games, &cand);
        if gid_opt.is_none() && match_count == 0 {
            skipped += 1;
            continue;
        }
        if let Some(ref gid) = gid_opt {
            let held = existing_counts.get(gid).copied().unwrap_or(0)
                + attach
                    .get(gid)
                    .and_then(Value::as_array)
                    .map(Vec::len)
                    .unwrap_or(0);
            if held >= MAX_MEMORIES_PER_GAME {
                skipped += 1;
                continue;
            }
        }
        let dest_dir = match &gid_opt {
            Some(gid) => media_root.join(safe_component(gid)),
            None => media_root.join(UNASSIGNED_DIR),
        };
        let ext = cand
            .path
            .extension()
            .and_then(|e| e.to_str())
            .map(|s| format!(".{}", s.to_ascii_lowercase()))
            .unwrap_or_default();
        let dest = dest_dir.join(format!("{digest}{ext}"));
        let copy_ok = fs::create_dir_all(&dest_dir).and_then(|()| {
            if dest.is_file()
                && fs::metadata(&dest)
                    .map(|m| m.len() == size)
                    .unwrap_or(false)
            {
                Ok(size)
            } else {
                fs::copy(&cand.path, &dest)
            }
        });
        if let Err(e) = copy_ok {
            failed += 1;
            if errors.len() < MAX_ERRORS {
                errors.push(Value::String(format!("{}: {e}", cand.path.display())));
            }
            continue;
        }
        let mtime = meta
            .modified()
            .ok()
            .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
            .map(|d| d.as_secs() as i64)
            .unwrap_or(0);
        let mut entry = json!({
            "path": dest.to_string_lossy(),
            "sha256": digest,
            "bytes": size,
            "source": cand.source,
            "taken_at": parse_taken_at(&cand, mtime),
            "imported_at": jiff::Timestamp::now().to_string(),
        });
        dedupe.record(size, &digest, dest);
        bytes_used += size;

        if let Some(gid) = gid_opt {
            attach
                .entry(gid)
                .or_insert_with(|| Value::Array(Vec::new()))
                .as_array_mut()
                .unwrap()
                .push(entry);
            added += 1;
        } else {
            if pending_unassigned + unassigned.len() >= MAX_UNASSIGNED {
                skipped += 1;
                continue;
            }
            let hint = if !cand.title_id.is_empty() {
                cand.title_id.clone()
            } else {
                cand.names.first().cloned().unwrap_or_default()
            };
            entry["hint"] = Value::String(hint);
            unassigned.push(entry);
            unassigned_cnt += 1;
        }
    }

    json!({
        "attach": attach,
        "unassigned": unassigned,
        "max_per_game": MAX_MEMORIES_PER_GAME,
        "counts": {
            "enabled": true,
            "roots": roots.len(),
            "scanned": scanned,
            "added": added,
            "unassigned": unassigned_cnt,
            "skipped": skipped,
            "failed": failed,
            "errors": errors,
            "skipped_roots": skipped_roots,
            "cancelled": cancelled,
        }
    })
}

pub fn apply_import_plan(state: &mut Value, plan: &Value) -> Value {
    let mut attached = 0usize;
    let mut unassigned_added = 0usize;
    let mut failed = 0usize;
    let max_per_game = plan
        .get("max_per_game")
        .and_then(Value::as_u64)
        .map(|n| n as usize)
        .filter(|&n| n > 0)
        .unwrap_or(MAX_MEMORIES_PER_GAME);

    if let Some(attach) = plan.get("attach").and_then(Value::as_object)
        && let Some(games) = state.get_mut("games").and_then(Value::as_array_mut)
    {
        for (gid, entries_val) in attach {
            let Some(entries) = entries_val.as_array() else {
                continue;
            };
            let target = games.iter_mut().find(|g| {
                g.get("game_id").and_then(Value::as_str) == Some(gid.as_str())
                    || g.get("legacy_game_ids")
                        .and_then(Value::as_array)
                        .is_some_and(|a| a.iter().any(|x| x.as_str() == Some(gid.as_str())))
            });
            let Some(game) = target else {
                failed += entries.len();
                continue;
            };
            let Some(gm) = game.as_object_mut() else {
                continue;
            };
            let list = gm
                .entry(MEMORIES_FIELD.to_owned())
                .or_insert_with(|| Value::Array(Vec::new()));
            if !list.is_array() {
                *list = Value::Array(Vec::new());
            }
            let arr = list.as_array_mut().unwrap();
            let mut known: HashSet<String> = arr
                .iter()
                .filter_map(|i| {
                    i.get("sha256")
                        .and_then(Value::as_str)
                        .map(ToOwned::to_owned)
                })
                .collect();
            for entry in entries {
                if arr.len() >= max_per_game {
                    break;
                }
                let sha = entry.get("sha256").and_then(Value::as_str).unwrap_or("");
                if known.contains(sha) {
                    continue;
                }
                arr.push(entry.clone());
                known.insert(sha.to_owned());
                attached += 1;
            }
        }
    }

    if let Some(un_entries) = plan.get("unassigned").and_then(Value::as_array)
        && let Some(st) = state.as_object_mut()
    {
        let bucket = st
            .entry(UNASSIGNED_FIELD.to_owned())
            .or_insert_with(|| Value::Array(Vec::new()));
        if !bucket.is_array() {
            *bucket = Value::Array(Vec::new());
        }
        let arr = bucket.as_array_mut().unwrap();
        let mut known: HashSet<String> = arr
            .iter()
            .filter_map(|i| {
                i.get("sha256")
                    .and_then(Value::as_str)
                    .map(ToOwned::to_owned)
            })
            .collect();
        for entry in un_entries {
            if arr.len() >= MAX_UNASSIGNED {
                break;
            }
            let sha = entry.get("sha256").and_then(Value::as_str).unwrap_or("");
            if known.contains(sha) {
                continue;
            }
            arr.push(entry.clone());
            known.insert(sha.to_owned());
            unassigned_added += 1;
        }
        if arr.is_empty() {
            st.remove(UNASSIGNED_FIELD);
        }
    }

    json!({
        "attached": attached,
        "unassigned": unassigned_added,
        "failed": failed,
    })
}
