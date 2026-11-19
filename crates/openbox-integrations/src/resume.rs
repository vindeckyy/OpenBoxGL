//! Quick Resume & Moments state-capture plumbing (`pkg/parity/parity_resume.py`).

use std::collections::HashSet;
use std::fs;
use std::path::{Component, Path, PathBuf};
use std::process::Command;
use std::sync::LazyLock;
use std::time::UNIX_EPOCH;

use jiff::Timestamp;
use openbox_emulators::defs::{
    Adapter, StateConfig, apply_core_override, detect_adapter_for_platform, detect_adapter_prefix,
    find_adapter, resolved_startup_args,
};
use openbox_emulators::tokens::{build_context, path_parent, substitute_once};
use openbox_io::atomic_write_text;
use regex::Regex;
use serde_json::{Map, Value, json};
use sha1::{Digest, Sha1};
use uuid::Uuid;

pub const RESUME_DIRNAME: &str = "resume_states";
pub const STATE_META: &str = "state.json";
pub const RA_CONFIG_NAME: &str = "openbox-session.cfg";
pub const RESUME_BASENAME: &str = "resume";
pub const RESUME_SLOT: &str = "openbox-resume";
pub const CAPTURE_SLOT: &str = "openbox-session";
pub const ARCHIVE_DIRNAME: &str = "archive";
pub const MOMENTS_DIRNAME: &str = "moments";
pub const ARCHIVE_LIMIT: usize = 20;

const STATE_KINDS: &[&str] = &["retroarch", "adapter-cli", "mame", "none"];
static RESUME_SLOT_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z0-9_-]{1,32}$").unwrap());
static KEY_UNSAFE_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[^A-Za-z0-9._-]+").unwrap());

pub fn validate_resume_slot(value: Option<&str>) -> Result<String, String> {
    let Some(raw) = value else {
        return Ok(String::new());
    };
    if raw.is_empty() {
        return Ok(String::new());
    }
    if !RESUME_SLOT_RE.is_match(raw) {
        return Err("Resume slot must be 1-32 letters, digits, '-' or '_'.".to_owned());
    }
    Ok(raw.to_owned())
}

pub fn resume_slot_for(game: &Value) -> Result<String, String> {
    let s = validate_resume_slot(game.get("resume_slot").and_then(Value::as_str))?;
    Ok(if s.is_empty() {
        CAPTURE_SLOT.to_owned()
    } else {
        s
    })
}

pub fn resume_state_name_for(game: &Value) -> Result<String, String> {
    let s = validate_resume_slot(game.get("resume_slot").and_then(Value::as_str))?;
    Ok(if s.is_empty() {
        RESUME_SLOT.to_owned()
    } else {
        s
    })
}

pub fn resume_root(data_parent: &Path) -> PathBuf {
    data_parent.join(RESUME_DIRNAME)
}

pub fn state_key(game: &Value) -> String {
    let stable = game
        .get("game_id")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if !stable.is_empty() {
        let mut safe = KEY_UNSAFE_RE.replace_all(stable, "_").into_owned();
        if matches!(safe.as_str(), "" | "." | "..") {
            safe = "game".to_owned();
        }
        if safe != stable || safe.len() > 80 {
            let digest = &hex::encode(Sha1::digest(stable.as_bytes()))[..12];
            let prefix_max = 80 - digest.len() - 1;
            let prefix: String = safe.chars().take(prefix_max).collect();
            return format!("{prefix}-{digest}");
        }
        return safe;
    }
    let name = game.get("name").and_then(Value::as_str).unwrap_or("");
    let path = game.get("path").and_then(Value::as_str).unwrap_or("");
    let input = format!("{name}|{path}");
    hex::encode(Sha1::digest(input.as_bytes()))[..16].to_owned()
}

fn has_symlink_component(path: &Path) -> bool {
    let mut cur = path.to_path_buf();
    loop {
        if let Ok(meta) = fs::symlink_metadata(&cur)
            && meta.file_type().is_symlink()
        {
            return true;
        }
        if !cur.pop() {
            return false;
        }
    }
}

pub fn state_dir_for(game: &Value, data_parent: &Path) -> Result<PathBuf, String> {
    let root = resume_root(data_parent);
    let candidate = root.join(state_key(game));
    if let Ok(meta) = fs::symlink_metadata(&candidate)
        && meta.file_type().is_symlink()
    {
        return Err("Resume state directory must not be a symlink.".to_owned());
    }
    let resolved_root = fs::canonicalize(&root).unwrap_or_else(|_| root.clone());
    let resolved = if candidate.exists() {
        fs::canonicalize(&candidate).unwrap_or_else(|_| candidate.clone())
    } else {
        resolved_root.join(state_key(game))
    };
    if resolved == resolved_root || !resolved.starts_with(&resolved_root) {
        return Err("Resume state directory escapes the OpenBox data root.".to_owned());
    }
    Ok(resolved)
}

pub fn adapter_state(adapter: Option<&Adapter>) -> StateConfig {
    let Some(a) = adapter else {
        return StateConfig {
            kind: "none".to_owned(),
            ..StateConfig::default()
        };
    };
    let kind = if STATE_KINDS.contains(&a.state.kind.as_str()) {
        a.state.kind.clone()
    } else {
        "none".to_owned()
    };
    StateConfig {
        kind,
        template: a.state.template.clone(),
        capture: a.state.capture.clone(),
        glob: a.state.glob.clone(),
    }
}

pub fn adapter_for_launch(
    game: &Value,
    profiles: &Map<String, Value>,
    adapters: &[Adapter],
) -> (Option<Adapter>, &'static str) {
    if game
        .get("launch")
        .and_then(Value::as_str)
        .is_some_and(|s| !s.trim().is_empty())
    {
        return (None, "game_launch");
    }
    let adapter_id = game
        .get("emulator_adapter_id")
        .and_then(Value::as_str)
        .unwrap_or("");
    let emulator_id = game
        .get("emulator_id")
        .and_then(Value::as_str)
        .unwrap_or("");
    if let Some(a) = find_adapter(adapters, adapter_id, emulator_id)
        && !detect_adapter_prefix(a).is_empty()
    {
        return (Some(apply_core_override(a, game)), "game_adapter");
    }
    let platform = game.get("platform").and_then(Value::as_str).unwrap_or("");
    if profiles
        .get(platform)
        .and_then(Value::as_str)
        .is_some_and(|s| !s.trim().is_empty())
    {
        return (None, "platform_profile");
    }
    if let Some(detected) = detect_adapter_for_platform(adapters, platform) {
        return (
            Some(apply_core_override(detected, game)),
            "registry_adapter",
        );
    }
    (None, "direct_exe")
}

fn flatpak_version(app_id: &str) -> Option<String> {
    let out = Command::new("flatpak")
        .args(["info", "--show-version", app_id])
        .output()
        .ok()?;
    let ver = String::from_utf8_lossy(&out.stdout).trim().to_owned();
    if ver.is_empty() { None } else { Some(ver) }
}

fn file_stamp(path: &str) -> String {
    if path.is_empty() {
        return String::new();
    }
    match fs::metadata(path) {
        Ok(meta) => {
            let mtime_ns = meta
                .modified()
                .ok()
                .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
                .map(|d| d.as_nanos())
                .unwrap_or(0);
            format!("{}:{}:{}", path, mtime_ns, meta.len())
        }
        Err(_) => format!("{path}:unknown"),
    }
}

pub fn emulator_fingerprint(adapter: Option<&Adapter>) -> String {
    let Some(a) = adapter else {
        return "uninstalled".to_owned();
    };
    let prefix = detect_adapter_prefix(a);
    if prefix.is_empty() {
        return "uninstalled".to_owned();
    }
    let mut core = a.core_path.clone().unwrap_or_default().trim().to_owned();
    if core.is_empty() {
        let startup = resolved_startup_args(a);
        if let Some(pos) = startup.iter().position(|s| s == "-L")
            && let Some(next) = startup.get(pos + 1)
        {
            let cand = next.replace("{EmulatorDir}", &path_parent(&prefix[0]));
            if Path::new(&cand).is_absolute() {
                core = cand;
            }
        }
    }
    let core_stamp = if core.is_empty() {
        String::new()
    } else {
        format!("|core:{}", file_stamp(&core))
    };
    if prefix[0] == "flatpak" {
        let app_id = prefix.last().map(String::as_str).unwrap_or("");
        let ver = flatpak_version(app_id).unwrap_or_else(|| "unknown".to_owned());
        return format!("flatpak:{app_id}:{ver}{core_stamp}");
    }
    format!("{}{}", file_stamp(&prefix[0]), core_stamp)
}

pub fn safe_state_file(state_dir: &Path, relative: &str) -> Option<PathBuf> {
    let trimmed = relative.trim();
    if trimmed.is_empty() {
        return None;
    }
    let rel = Path::new(trimmed);
    if rel.is_absolute() {
        return None;
    }
    let mut current = state_dir.to_path_buf();
    for comp in rel.components() {
        match comp {
            Component::Normal(part) => {
                current.push(part);
                if fs::symlink_metadata(&current)
                    .ok()
                    .is_some_and(|m| m.file_type().is_symlink())
                {
                    return None;
                }
            }
            _ => return None,
        }
    }
    let resolved_dir = fs::canonicalize(state_dir).ok()?;
    let resolved = fs::canonicalize(&current).ok()?;
    if !resolved.starts_with(&resolved_dir) || !resolved.is_file() {
        return None;
    }
    Some(resolved)
}

pub fn load_meta(game: &Value, data_parent: &Path) -> Option<Map<String, Value>> {
    let sdir = state_dir_for(game, data_parent).ok()?;
    let path = safe_state_file(&sdir, STATE_META)?;
    let raw = fs::read_to_string(path).ok()?;
    match serde_json::from_str::<Value>(&raw).ok()? {
        Value::Object(map) => Some(map),
        _ => None,
    }
}

pub fn is_stale(meta: Option<&Map<String, Value>>, adapter: &Adapter) -> bool {
    let Some(m) = meta else {
        return false;
    };
    if m.get("adapter_id").and_then(Value::as_str).unwrap_or("") != adapter.adapter_id {
        return true;
    }
    let stored = m
        .get("emulator_version")
        .and_then(Value::as_str)
        .unwrap_or("");
    stored != emulator_fingerprint(Some(adapter))
}

fn write_ra_session_config(state_dir: &Path) -> Result<PathBuf, String> {
    fs::create_dir_all(state_dir).map_err(|e| e.to_string())?;
    let target = state_dir.join(RA_CONFIG_NAME);
    if has_symlink_component(&target) {
        return Err("Refusing to write a resume config through a symlink.".to_owned());
    }
    let text = format!(
        "# Auto-generated by OpenBox Quick Resume. Do not edit; rewritten per launch.\n\
         savestate_auto_save = \"true\"\n\
         savestate_auto_load = \"false\"\n\
         savestate_directory = \"{}\"\n\
         savestate_thumbnail_enable = \"true\"\n\
         sort_savestates_enable = \"false\"\n\
         state_slot = \"0\"\n\
         network_cmd_enable = \"true\"\n",
        state_dir.to_string_lossy()
    );
    atomic_write_text(&target, &text, 0o600).map_err(|e| e.to_string())?;
    Ok(target)
}

fn render_state_args(
    game: &Value,
    raw_args: &[String],
    state_dir: &Path,
    state_path: &str,
    state_name: &str,
    state_config: &str,
) -> Vec<String> {
    let mut ctx = build_context(game, None, "", "");
    ctx.state_path = state_path.to_owned();
    ctx.state_dir = state_dir.to_string_lossy().into_owned();
    ctx.state_name = state_name.to_owned();
    ctx.state_config = state_config.to_owned();
    raw_args.iter().map(|r| substitute_once(r, &ctx)).collect()
}

#[allow(clippy::too_many_arguments)]
pub fn inject_resume_args(
    game: &Value,
    args: &[String],
    profiles: &Map<String, Value>,
    settings: &Map<String, Value>,
    adapters: &[Adapter],
    resume: Option<&str>,
    data_parent: &Path,
    allow_stale: bool,
) -> Result<Vec<String>, String> {
    let mut out = args.to_vec();
    let enabled = settings
        .get("quick_resume_enabled")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    if !enabled {
        if resume.is_some() {
            return Err("Quick Resume is disabled in Settings.".to_owned());
        }
        return Ok(out);
    }
    let (adapter_opt, _) = adapter_for_launch(game, profiles, adapters);
    let state = adapter_state(adapter_opt.as_ref());
    let Some(adapter) = adapter_opt else {
        if resume.is_some() {
            return Err("No state-capable adapter is configured for this game.".to_owned());
        }
        return Ok(out);
    };
    if state.kind == "none" {
        if resume.is_some() {
            return Err("No state-capable adapter is configured for this game.".to_owned());
        }
        return Ok(out);
    }
    let sdir = state_dir_for(game, data_parent)?;
    if let Some(req) = resume {
        let meta = load_meta(game, data_parent)
            .ok_or_else(|| "No captured state exists for this game.".to_owned())?;
        if is_stale(Some(&meta), &adapter) && !allow_stale {
            return Err(
                "The captured state was written by a different adapter or emulator build."
                    .to_owned(),
            );
        }
        let rel = if !req.is_empty() && req != "latest" {
            req.to_owned()
        } else {
            meta.get("file")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned()
        };
        let target = safe_state_file(&sdir, &rel)
            .ok_or_else(|| "The captured state file is missing.".to_owned())?;
        let needs_cfg = state.template.iter().any(|s| s.contains("{state_config}"));
        let cfg = if needs_cfg {
            write_ra_session_config(&sdir)?
                .to_string_lossy()
                .into_owned()
        } else {
            String::new()
        };
        let state_name = resume_state_name_for(game).unwrap_or_else(|_| RESUME_SLOT.to_owned());
        out.extend(render_state_args(
            game,
            &state.template,
            &sdir,
            &target.to_string_lossy(),
            &state_name,
            &cfg,
        ));
        return Ok(out);
    }

    let needs_cfg = state.capture.iter().any(|s| s.contains("{state_config}"));
    let cfg = if needs_cfg {
        match write_ra_session_config(&sdir) {
            Ok(p) => p.to_string_lossy().into_owned(),
            Err(_) => return Ok(out),
        }
    } else {
        String::new()
    };
    let slot = resume_slot_for(game).unwrap_or_else(|_| CAPTURE_SLOT.to_owned());
    out.extend(render_state_args(
        game,
        &state.capture,
        &sdir,
        "",
        &slot,
        &cfg,
    ));
    Ok(out)
}

fn matches_simple_glob(pattern: &str, name: &str) -> bool {
    if pattern == "*" {
        return true;
    }
    if let Some(suffix) = pattern.strip_prefix('*')
        && !suffix.contains('*')
    {
        return name.ends_with(suffix);
    }
    if let Some(prefix) = pattern.strip_suffix('*')
        && !prefix.contains('*')
    {
        return name.starts_with(prefix);
    }
    name == pattern
}

fn collect_state_files(state_dir: &Path, state: &StateConfig) -> Vec<PathBuf> {
    if !state_dir.is_dir() {
        return Vec::new();
    }
    let patterns: Vec<&str> = if state.glob.is_empty() {
        vec!["*"]
    } else {
        state.glob.iter().map(String::as_str).collect()
    };
    let mut seen = HashSet::new();
    let mut files = Vec::new();
    let Ok(rd) = fs::read_dir(state_dir) else {
        return files;
    };
    for entry in rd.flatten() {
        let p = entry.path();
        let Ok(meta) = fs::symlink_metadata(&p) else {
            continue;
        };
        if meta.file_type().is_symlink() || !meta.is_file() {
            continue;
        }
        let name = entry.file_name().to_string_lossy().into_owned();
        if name == STATE_META || name == RA_CONFIG_NAME || name.starts_with("thumb.") {
            continue;
        }
        if patterns.iter().any(|pat| matches_simple_glob(pat, &name)) && seen.insert(p.clone()) {
            files.push(p);
        }
    }
    files
}

fn mtime_ns(path: &Path) -> u128 {
    fs::metadata(path)
        .ok()
        .and_then(|m| m.modified().ok())
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map(|d| d.as_nanos())
        .unwrap_or(0)
}

fn state_suffix(path: &Path) -> String {
    let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
    if name.ends_with(".state.auto") || name.ends_with(".state") {
        return ".state".to_owned();
    }
    path.extension()
        .and_then(|e| e.to_str())
        .map(|e| format!(".{e}"))
        .unwrap_or_else(|| ".state".to_owned())
}

pub fn collect_resume_state(
    game: &Value,
    settings: &Map<String, Value>,
    profiles: &Map<String, Value>,
    adapters: &[Adapter],
    data_parent: &Path,
    launch_id: &str,
    started_at_secs: Option<f64>,
) -> Option<Value> {
    if !settings
        .get("quick_resume_enabled")
        .and_then(Value::as_bool)
        .unwrap_or(true)
    {
        return None;
    }
    let (adapter_opt, _) = adapter_for_launch(game, profiles, adapters);
    let adapter = adapter_opt?;
    let state = adapter_state(Some(&adapter));
    if state.kind == "none" {
        return None;
    }
    let sdir = state_dir_for(game, data_parent).ok()?;
    let mut candidates = collect_state_files(&sdir, &state);
    if let Some(started) = started_at_secs {
        let started_ns = (started.max(0.0) * 1e9) as u128;
        candidates.retain(|p| mtime_ns(p) > started_ns);
    }
    let picked = candidates.into_iter().max_by_key(|p| mtime_ns(p))?;

    let tpl = state.template.join(" ");
    let canonical_path = if tpl.contains("{state_name}") {
        let sname = resume_state_name_for(game).unwrap_or_else(|_| RESUME_SLOT.to_owned());
        picked.with_file_name(format!("{sname}{}", state_suffix(&picked)))
    } else if tpl.contains("{state_path}") {
        picked.with_file_name(format!("{RESUME_BASENAME}{}", state_suffix(&picked)))
    } else {
        picked.clone()
    };

    let final_path = if picked != canonical_path {
        if canonical_path.exists() {
            let arch = sdir.join(ARCHIVE_DIRNAME);
            let _ = fs::create_dir_all(&arch);
            let cname = canonical_path
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or("state");
            let dest = arch.join(format!("{}-{cname}", mtime_ns(&canonical_path)));
            let _ = fs::rename(&canonical_path, &dest);
        }
        if fs::rename(&picked, &canonical_path).is_ok() {
            canonical_path
        } else {
            picked
        }
    } else {
        picked
    };

    let rel = final_path
        .strip_prefix(&sdir)
        .ok()?
        .to_string_lossy()
        .replace('\\', "/");
    let size = fs::metadata(&final_path).map(|m| m.len()).unwrap_or(0);
    let captured_at = Timestamp::now().strftime("%Y-%m-%dT%H:%M:%S").to_string();
    let capture_id = format!("capture-{}", &Uuid::new_v4().simple().to_string()[..24]);

    let meta = json!({
        "adapter_id": adapter.adapter_id,
        "emulator_version": emulator_fingerprint(Some(&adapter)),
        "kind": state.kind,
        "capture_id": capture_id,
        "file": rel,
        "captured_at": captured_at,
        "size": size,
        "thumbnail": "",
        "launch_id": launch_id,
    });
    let _ = fs::create_dir_all(&sdir);
    let meta_text = serde_json::to_string_pretty(&meta).ok()?;
    atomic_write_text(&sdir.join(STATE_META), &meta_text, 0o600).ok()?;

    // Enforce retention
    let retention = settings
        .get("state_retention")
        .and_then(Value::as_u64)
        .unwrap_or(1)
        .clamp(1, ARCHIVE_LIMIT as u64) as usize;
    let arch = sdir.join(ARCHIVE_DIRNAME);
    if let Ok(rd) = fs::read_dir(&arch) {
        let mut archived: Vec<PathBuf> = rd
            .flatten()
            .map(|e| e.path())
            .filter(|p| p.is_file())
            .collect();
        archived.sort_by_key(|p| std::cmp::Reverse(mtime_ns(p)));
        for stale in archived.into_iter().skip(retention.saturating_sub(1)) {
            let _ = fs::remove_file(stale);
        }
    }
    Some(meta)
}

pub fn resume_status(
    game: &Value,
    settings: &Map<String, Value>,
    profiles: &Map<String, Value>,
    adapters: &[Adapter],
    data_parent: &Path,
) -> Value {
    let (adapter_opt, precedence) = adapter_for_launch(game, profiles, adapters);
    let state = adapter_state(adapter_opt.as_ref());
    let enabled = settings
        .get("quick_resume_enabled")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    let adapter_id = adapter_opt
        .as_ref()
        .map(|a| a.adapter_id.clone())
        .unwrap_or_default();
    let mut status = json!({
        "game_id": game.get("game_id").and_then(Value::as_str).unwrap_or(""),
        "enabled": enabled,
        "capable": false,
        "kind": state.kind,
        "capture": !state.capture.is_empty() || state.kind == "retroarch",
        "adapter_id": adapter_id,
        "precedence": precedence,
        "available": false,
        "stale": false,
        "adapter_changed": false,
        "state": Value::Null,
        "reason": "ok",
    });

    let path = game
        .get("path")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    let shelf = game.get("shelf").and_then(Value::as_bool).unwrap_or(false);
    let manual = game
        .get("manual_entry")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if shelf || manual || path.is_empty() || !Path::new(path).exists() {
        status["reason"] = json!("missing-path");
        return status;
    }
    let Some(adapter) = adapter_opt else {
        status["reason"] = json!("unsupported-adapter");
        return status;
    };
    if state.kind == "none" {
        status["reason"] = json!("unsupported-adapter");
        return status;
    }
    status["capable"] = json!(true);
    let Some(meta) = load_meta(game, data_parent) else {
        return status;
    };
    let Ok(sdir) = state_dir_for(game, data_parent) else {
        return status;
    };
    let file_rel = meta
        .get("file")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    let Some(target) = safe_state_file(&sdir, &file_rel) else {
        return status;
    };
    let meta_adapter = meta.get("adapter_id").and_then(Value::as_str).unwrap_or("");
    let size = meta
        .get("size")
        .and_then(Value::as_u64)
        .unwrap_or_else(|| fs::metadata(&target).map(|m| m.len()).unwrap_or(0));
    status["available"] = json!(true);
    status["adapter_changed"] = json!(meta_adapter != adapter.adapter_id);
    status["stale"] = json!(is_stale(Some(&meta), &adapter));
    status["state"] = json!({
        "file": file_rel,
        "capture_id": meta.get("capture_id").and_then(Value::as_str).unwrap_or(""),
        "captured_at": meta.get("captured_at").and_then(Value::as_str).unwrap_or(""),
        "emulator_version": meta.get("emulator_version").and_then(Value::as_str).unwrap_or(""),
        "adapter_id": meta_adapter,
        "thumbnail": meta.get("thumbnail").and_then(Value::as_str).unwrap_or(""),
        "size": size,
        "launch_id": meta.get("launch_id").and_then(Value::as_str).unwrap_or(""),
    });
    status
}

pub fn discard_resume_state(game: &Value, data_parent: &Path) -> Result<bool, String> {
    let sdir = state_dir_for(game, data_parent)?;
    let mut removed = false;
    if let Some(meta) = load_meta(game, data_parent)
        && let Some(rel) = meta.get("file").and_then(Value::as_str)
        && let Some(candidate) = safe_state_file(&sdir, rel)
        && fs::remove_file(candidate).is_ok()
    {
        removed = true;
    }
    let meta_path = sdir.join(STATE_META);
    if meta_path.exists() && fs::remove_file(meta_path).is_ok() {
        removed = true;
    }
    Ok(removed)
}
