//! Artwork Doctor bulk hygiene report and safe fix/undo journal
//! (`pkg/parity/parity_artwork_hygiene.py`).

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fs::{self, File};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use openbox_platform::expand_user;
use regex::Regex;
use serde_json::{Map, Value, json};
use sha1::{Digest as _, Sha1};

use crate::http::py_round;

pub const PROVIDER_ATTRIBUTION: &str = "SteamGridDB";
pub const UNDO_DIRECTORY: &str = "artwork-hygiene";
pub const ARTWORK_FIELDS: &[&str] = &[
    "cover",
    "hero",
    "background",
    "clear_logo",
    "icon",
    "banner",
];

static BATCH_ID_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$").unwrap());

fn utc_now_secs() -> String {
    jiff::Timestamp::now()
        .strftime("%Y-%m-%dT%H:%M:%S+00:00")
        .to_string()
}

fn issue_label(issue: &str) -> &'static str {
    match issue {
        "missing_cover" => "Missing cover",
        "missing_file" => "Artwork file is missing",
        "low_res" => "Low resolution",
        "wrong_aspect" => "Wrong aspect ratio",
        "duplicate" => "Duplicate artwork",
        _ => "",
    }
}

fn aspect_target(kind: &str) -> Option<(f64, f64)> {
    match kind {
        "cover" => Some((3.0 / 4.0, 0.16)),
        "hero" | "background" => Some((16.0 / 9.0, 0.35)),
        "banner" => Some((460.0 / 215.0, 0.35)),
        _ => None,
    }
}

fn png_size(blob: &[u8]) -> Option<(u32, u32)> {
    if blob.len() < 24 || &blob[..8] != b"\x89PNG\r\n\x1a\n" || &blob[12..16] != b"IHDR" {
        return None;
    }
    let w = u32::from_be_bytes([blob[16], blob[17], blob[18], blob[19]]);
    let h = u32::from_be_bytes([blob[20], blob[21], blob[22], blob[23]]);
    Some((w, h))
}

fn gif_size(blob: &[u8]) -> Option<(u32, u32)> {
    if blob.len() < 10 || (&blob[..6] != b"GIF87a" && &blob[..6] != b"GIF89a") {
        return None;
    }
    let w = u16::from_le_bytes([blob[6], blob[7]]) as u32;
    let h = u16::from_le_bytes([blob[8], blob[9]]) as u32;
    Some((w, h))
}

fn bmp_size(blob: &[u8]) -> Option<(u32, u32)> {
    if blob.len() < 26 || &blob[..2] != b"BM" {
        return None;
    }
    let w = i32::from_le_bytes([blob[18], blob[19], blob[20], blob[21]]).unsigned_abs();
    let h = i32::from_le_bytes([blob[22], blob[23], blob[24], blob[25]]).unsigned_abs();
    Some((w, h))
}

fn jpeg_size(blob: &[u8]) -> Option<(u32, u32)> {
    if blob.len() < 4 || blob[0] != 0xFF || blob[1] != 0xD8 {
        return None;
    }
    let mut index = 2usize;
    while index + 9 < blob.len() {
        if blob[index] != 0xFF {
            index += 1;
            continue;
        }
        let marker = blob[index + 1];
        if marker == 0xD8 || marker == 0x01 || (0xD0..=0xD7).contains(&marker) {
            index += 2;
            continue;
        }
        let length = u16::from_be_bytes([blob[index + 2], blob[index + 3]]) as usize;
        if matches!(
            marker,
            0xC0 | 0xC1
                | 0xC2
                | 0xC3
                | 0xC5
                | 0xC6
                | 0xC7
                | 0xC9
                | 0xCA
                | 0xCB
                | 0xCD
                | 0xCE
                | 0xCF
        ) {
            let h = u16::from_be_bytes([blob[index + 5], blob[index + 6]]) as u32;
            let w = u16::from_be_bytes([blob[index + 7], blob[index + 8]]) as u32;
            return Some((w, h));
        }
        index += 2 + length.max(2);
    }
    None
}

fn webp_size(blob: &[u8]) -> Option<(u32, u32)> {
    if blob.len() < 30 || &blob[..4] != b"RIFF" || &blob[8..12] != b"WEBP" {
        return None;
    }
    let chunk = &blob[12..16];
    if chunk == b"VP8X" {
        let w = (u32::from_le_bytes([blob[24], blob[25], blob[26], 0])) + 1;
        let h = (u32::from_le_bytes([blob[27], blob[28], blob[29], 0])) + 1;
        return Some((w, h));
    }
    if chunk == b"VP8 "
        && let Some(pos) = blob[20..].windows(3).position(|w| w == [0x9d, 0x01, 0x2a])
    {
        let start = 20 + pos;
        if start + 7 <= blob.len() {
            let w = (u16::from_le_bytes([blob[start + 3], blob[start + 4]]) & 0x3FFF) as u32;
            let h = (u16::from_le_bytes([blob[start + 5], blob[start + 6]]) & 0x3FFF) as u32;
            return Some((w, h));
        }
    }
    if chunk == b"VP8L" && blob.len() >= 25 {
        let bits = u32::from_le_bytes([blob[21], blob[22], blob[23], blob[24]]);
        let w = (bits & 0x3FFF) + 1;
        let h = ((bits >> 14) & 0x3FFF) + 1;
        return Some((w, h));
    }
    None
}

pub fn image_dimensions(path: &Path) -> Option<(u32, u32)> {
    let mut file = File::open(path).ok()?;
    let mut buf = vec![0u8; 65536];
    let n = file.read(&mut buf).ok()?;
    buf.truncate(n);
    if buf.len() >= 2 && buf[0] == 0xFF && buf[1] == 0xD8 {
        if let Some(sz) = jpeg_size(&buf) {
            return Some(sz);
        }
        // Fallback: read entire JPEG (bounded to 16 MB) if SOF is past 64 KB EXIF block
        let mut full = buf;
        let _ = file.take(16 * 1024 * 1024).read_to_end(&mut full);
        return jpeg_size(&full);
    }
    png_size(&buf)
        .or_else(|| gif_size(&buf))
        .or_else(|| bmp_size(&buf))
        .or_else(|| webp_size(&buf))
}

fn file_digest(path: &Path) -> Option<String> {
    let mut file = File::open(path).ok()?;
    let mut hasher = Sha1::new();
    let mut buf = [0u8; 65536];
    loop {
        let n = file.read(&mut buf).ok()?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Some(hex::encode(hasher.finalize()))
}

pub fn resolve_artwork_path(value: &str, media_root: Option<&Path>) -> Option<PathBuf> {
    let text = value.trim();
    if text.is_empty() || text.starts_with("http://") || text.starts_with("https://") {
        return None;
    }
    let cand = expand_user(Path::new(text));
    if !cand.is_absolute()
        && let Some(root) = media_root
    {
        return Some(root.join(cand));
    }
    Some(cand)
}

pub fn build_report(
    games: &[Value],
    media_root: Option<&Path>,
    low_res_min: u32,
    include_duplicates: bool,
) -> Value {
    let mut issues: Vec<Value> = Vec::new();
    let mut covers: Vec<(String, String, String, String)> = Vec::new();
    let mut scanned = 0usize;

    for game in games {
        let Some(g) = game.as_object() else {
            continue;
        };
        scanned += 1;
        let game_id = g.get("game_id").and_then(Value::as_str).unwrap_or("");
        let name = g.get("name").and_then(Value::as_str).unwrap_or("");
        let cover_val = g.get("cover").and_then(Value::as_str).unwrap_or("").trim();
        if cover_val.is_empty() {
            issues.push(json!({
                "issue": "missing_cover",
                "label": issue_label("missing_cover"),
                "game_id": game_id,
                "name": name,
                "field": "cover",
                "provider": g.get("artwork_provider").and_then(Value::as_str).unwrap_or(""),
            }));
        }
        for field in ARTWORK_FIELDS {
            let val = g.get(*field).and_then(Value::as_str).unwrap_or("").trim();
            if val.is_empty() {
                continue;
            }
            let Some(path) = resolve_artwork_path(val, media_root) else {
                continue;
            };
            if !path.is_file() {
                issues.push(json!({
                    "issue": "missing_file",
                    "label": issue_label("missing_file"),
                    "game_id": game_id,
                    "name": name,
                    "field": field,
                    "path": val,
                }));
                continue;
            }
            let Some((w, h)) = image_dimensions(&path) else {
                continue;
            };
            if w.min(h) < low_res_min {
                issues.push(json!({
                    "issue": "low_res",
                    "label": issue_label("low_res"),
                    "game_id": game_id,
                    "name": name,
                    "field": field,
                    "width": w,
                    "height": h,
                }));
            }
            if let Some((expected, tol)) = aspect_target(field)
                && w > 0
                && h > 0
            {
                let actual = w as f64 / h as f64;
                if (actual - expected).abs() > tol {
                    issues.push(json!({
                        "issue": "wrong_aspect",
                        "label": issue_label("wrong_aspect"),
                        "game_id": game_id,
                        "name": name,
                        "field": field,
                        "aspect": py_round(actual, 3),
                        "expected_aspect": py_round(expected, 3),
                    }));
                }
            }
            if *field == "cover"
                && include_duplicates
                && let Some(digest) = file_digest(&path)
            {
                covers.push((
                    digest,
                    game_id.to_owned(),
                    name.to_owned(),
                    path.to_string_lossy().into_owned(),
                ));
            }
        }
    }

    let mut duplicates: Vec<Value> = Vec::new();
    if include_duplicates {
        let mut grouped: BTreeMap<String, Vec<Value>> = BTreeMap::new();
        for (digest, gid, name, path) in covers {
            grouped.entry(digest).or_default().push(json!({
                "game_id": gid,
                "name": name,
                "path": path,
            }));
        }
        for (digest, members) in grouped {
            if members.len() > 1 {
                for m in &members {
                    issues.push(json!({
                        "issue": "duplicate",
                        "label": issue_label("duplicate"),
                        "game_id": m["game_id"],
                        "name": m["name"],
                        "field": "cover",
                        "path": m["path"],
                        "digest": digest,
                    }));
                }
                duplicates.push(json!({
                    "digest": digest,
                    "count": members.len(),
                    "games": members,
                }));
            }
        }
    }

    let mut counts: Map<String, Value> = Map::new();
    for k in [
        "missing_cover",
        "missing_file",
        "low_res",
        "wrong_aspect",
        "duplicate",
    ] {
        counts.insert(k.to_owned(), json!(0));
    }
    let mut provider_counts: HashMap<String, usize> = HashMap::new();
    for iss in &issues {
        if let Some(k) = iss.get("issue").and_then(Value::as_str) {
            let cur = counts.get(k).and_then(Value::as_u64).unwrap_or(0);
            counts.insert(k.to_owned(), json!(cur + 1));
        }
        if let Some(p) = iss
            .get("provider")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
        {
            *provider_counts.entry(p.to_owned()).or_insert(0) += 1;
        }
    }
    json!({
        "generated_at": utc_now_secs(),
        "scanned": scanned,
        "issues": issues,
        "counts": counts,
        "duplicates": duplicates,
        "provider_attribution": PROVIDER_ATTRIBUTION,
        "providers": provider_counts,
        "thresholds": {"low_res_min": low_res_min},
    })
}

pub fn select_fixable(
    report: &Value,
    issues: Option<&[String]>,
    fields: Option<&[String]>,
    game_ids: Option<&[String]>,
) -> Vec<Value> {
    let wanted_issues: HashSet<&str> = match issues {
        Some(list) if !list.is_empty() => list.iter().map(String::as_str).collect(),
        _ => ["missing_cover", "low_res", "wrong_aspect", "duplicate"]
            .into_iter()
            .collect(),
    };
    let wanted_fields: HashSet<&str> = match fields {
        Some(list) if !list.is_empty() => list.iter().map(String::as_str).collect(),
        _ => ["cover"].into_iter().collect(),
    };
    let wanted_games: Option<HashSet<&str>> =
        game_ids.map(|g| g.iter().map(String::as_str).collect());

    let mut selected: BTreeMap<(String, String), (String, BTreeSet<String>)> = BTreeMap::new();
    if let Some(arr) = report.get("issues").and_then(Value::as_array) {
        for iss in arr {
            let kind = iss.get("issue").and_then(Value::as_str).unwrap_or("");
            if !wanted_issues.contains(kind) || kind == "missing_file" {
                continue;
            }
            let gid = iss.get("game_id").and_then(Value::as_str).unwrap_or("");
            let field = iss.get("field").and_then(Value::as_str).unwrap_or("cover");
            if gid.is_empty() || !wanted_fields.contains(field) {
                continue;
            }
            if let Some(ref wg) = wanted_games
                && !wg.contains(gid)
            {
                continue;
            }
            let name = iss.get("name").and_then(Value::as_str).unwrap_or("");
            let entry = selected
                .entry((gid.to_owned(), field.to_owned()))
                .or_insert_with(|| (name.to_owned(), BTreeSet::new()));
            entry.1.insert(kind.to_owned());
        }
    }
    selected
        .into_iter()
        .map(|((gid, field), (name, iss_set))| {
            json!({
                "game_id": gid,
                "name": name,
                "field": field,
                "issues": iss_set.into_iter().collect::<Vec<_>>(),
            })
        })
        .collect()
}

pub fn undo_root(cache_dir: &Path) -> PathBuf {
    cache_dir.join(UNDO_DIRECTORY)
}

fn validate_batch_id(batch_id: &str) -> Result<&str, String> {
    if !BATCH_ID_RE.is_match(batch_id) {
        return Err("The artwork undo batch id is invalid.".to_owned());
    }
    Ok(batch_id)
}

pub fn snapshot_for_replacement(
    cache_dir: &Path,
    batch_id: &str,
    game_id: &str,
    field: &str,
    current_path: &str,
    backup: bool,
) -> Result<Value, String> {
    let bid = validate_batch_id(batch_id)?;
    let root = undo_root(cache_dir).join(bid);
    fs::create_dir_all(&root).map_err(|e| e.to_string())?;
    let current = if current_path.is_empty() {
        None
    } else {
        Some(Path::new(current_path))
    };
    let mut created = !current.is_some_and(Path::is_file);
    let mut backup_path = String::new();
    if !created
        && backup
        && let Some(cur) = current
    {
        let ext = cur
            .extension()
            .and_then(|e| e.to_str())
            .map(|s| format!(".{s}"))
            .unwrap_or_else(|| ".bin".to_owned());
        let gid_prefix: String = game_id.chars().take(40).collect();
        let target = root.join(format!("{gid_prefix}-{field}{ext}"));
        if fs::copy(cur, &target).is_ok() {
            backup_path = target.to_string_lossy().into_owned();
        } else {
            created = true;
        }
    }
    Ok(json!({
        "game_id": game_id,
        "field": field,
        "previous": current_path,
        "created": created,
        "backup": backup_path,
    }))
}

pub fn write_undo_manifest(
    cache_dir: &Path,
    batch_id: &str,
    records: &[Value],
    provider: &str,
) -> Result<String, String> {
    let bid = validate_batch_id(batch_id)?;
    let root = undo_root(cache_dir);
    fs::create_dir_all(&root).map_err(|e| e.to_string())?;
    let manifest = json!({
        "batch_id": bid,
        "created_at": utc_now_secs(),
        "provider": provider,
        "records": records,
    });
    let path = root.join(format!("{bid}.json"));
    let body = serde_json::to_string_pretty(&manifest).map_err(|e| e.to_string())? + "\n";
    fs::write(&path, body).map_err(|e| e.to_string())?;
    Ok(path.to_string_lossy().into_owned())
}

pub fn load_undo_manifest(cache_dir: &Path, batch_id: &str) -> Result<Value, String> {
    let bid = validate_batch_id(batch_id)?;
    let path = undo_root(cache_dir).join(format!("{bid}.json"));
    let raw = fs::read_to_string(&path)
        .map_err(|_| "No artwork undo journal was found for this batch.".to_owned())?;
    let payload: Value = serde_json::from_str(&raw)
        .map_err(|_| "No artwork undo journal was found for this batch.".to_owned())?;
    if !payload.is_object() || !payload.get("records").is_some_and(Value::is_array) {
        return Err("The artwork undo journal is invalid.".to_owned());
    }
    if payload.get("batch_id").and_then(Value::as_str) != Some(bid) {
        return Err("The artwork undo journal does not match the requested batch.".to_owned());
    }
    Ok(payload)
}

pub fn undo_batch(cache_dir: &Path, batch_id: &str) -> Result<Vec<Value>, String> {
    let manifest = load_undo_manifest(cache_dir, batch_id)?;
    let records = manifest["records"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let mut restored = Vec::new();
    for rec in records {
        if !rec.is_object() {
            continue;
        }
        let previous = rec.get("previous").and_then(Value::as_str).unwrap_or("");
        let backup = rec.get("backup").and_then(Value::as_str).unwrap_or("");
        let action = if rec.get("created").and_then(Value::as_bool).unwrap_or(false) {
            "removed"
        } else if !backup.is_empty() && Path::new(backup).is_file() && !previous.is_empty() {
            let dest = Path::new(previous);
            if let Some(p) = dest.parent() {
                let _ = fs::create_dir_all(p);
            }
            if fs::copy(backup, dest).is_ok() {
                "restored"
            } else {
                "failed"
            }
        } else {
            "skipped"
        };
        restored.push(json!({
            "game_id": rec.get("game_id").cloned().unwrap_or(Value::Null),
            "field": rec.get("field").cloned().unwrap_or(Value::Null),
            "action": action,
        }));
    }
    Ok(restored)
}

pub fn list_undo_batches(cache_dir: &Path, limit: usize) -> Vec<Value> {
    let root = undo_root(cache_dir);
    let Ok(rd) = fs::read_dir(&root) else {
        return Vec::new();
    };
    let mut files: Vec<(std::time::SystemTime, PathBuf)> = rd
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().and_then(|e| e.to_str()) == Some("json"))
        .filter_map(|p| {
            let mtime = fs::metadata(&p).ok()?.modified().ok()?;
            Some((mtime, p))
        })
        .collect();
    files.sort_by_key(|a| std::cmp::Reverse(a.0));
    let mut out = Vec::new();
    for (_, path) in files {
        let Ok(raw) = fs::read_to_string(&path) else {
            continue;
        };
        let Ok(payload) = serde_json::from_str::<Value>(&raw) else {
            continue;
        };
        let stem = path
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_owned();
        out.push(json!({
            "batch_id": payload.get("batch_id").and_then(Value::as_str).unwrap_or(&stem),
            "created_at": payload.get("created_at").and_then(Value::as_str).unwrap_or(""),
            "provider": payload.get("provider").and_then(Value::as_str).unwrap_or(""),
            "count": payload.get("records").and_then(Value::as_array).map(Vec::len).unwrap_or(0),
        }));
        if out.len() >= limit {
            break;
        }
    }
    out
}
