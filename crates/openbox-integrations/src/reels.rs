//! Deterministic highlight-reel manifests and optional ffmpeg rendering (`pkg/parity/parity_reels.py`).

use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::LazyLock;
use std::thread;
use std::time::{Duration, Instant};

use openbox_io::atomic_write_text;
use openbox_platform::{expand_user, which};
use percent_encoding::{AsciiSet, CONTROLS, utf8_percent_encode};
use regex::Regex;
use serde_json::{Map, Value, json};

pub const REEL_FORMAT: &str = "openbox-reel-v1";
pub const DEFAULT_IMAGE_SECONDS: f64 = 3.0;
pub const DEFAULT_FFMPEG_TIMEOUT: f64 = 300.0;
pub const MAX_FFMPEG_TIMEOUT: f64 = 600.0;
pub const MAX_INPUTS: usize = 64;
pub const MAX_MANIFEST_BYTES: usize = 2 * 1024 * 1024;

const IMAGE_EXTENSIONS: &[&str] = &[".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"];
static SAFE_NAME_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[^a-zA-Z0-9._-]+").unwrap());

const HREF_ENCODE_SET: &AsciiSet = &CONTROLS
    .add(b' ')
    .add(b'"')
    .add(b'#')
    .add(b'<')
    .add(b'>')
    .add(b'?')
    .add(b'`')
    .add(b'{')
    .add(b'}')
    .add(b'%');

fn slug(value: &str) -> String {
    let replaced = SAFE_NAME_RE.replace_all(value.trim(), "-");
    let trimmed = replaced.trim_matches(|c| c == '-' || c == '.');
    if trimmed.is_empty() {
        "reel".to_owned()
    } else {
        trimmed.to_owned()
    }
}

fn subject_for(
    game: Option<&Map<String, Value>>,
    year: Option<&str>,
    subject: Option<&str>,
) -> String {
    if let Some(y) = year.filter(|s| !s.is_empty()) {
        return y.to_owned();
    }
    if let Some(s) = subject.filter(|s| !s.is_empty()) {
        return s.to_owned();
    }
    if let Some(g) = game {
        for key in ["game_id", "id", "name", "title"] {
            if let Some(v) = g.get(key)
                && !v.is_null()
                && v.as_str() != Some("")
            {
                return match v {
                    Value::String(s) => s.clone(),
                    other => other.to_string(),
                };
            }
        }
    }
    "reel".to_owned()
}

fn title_for(game: Option<&Map<String, Value>>, year: Option<&str>, title: Option<&str>) -> String {
    if let Some(t) = title.filter(|s| !s.is_empty()) {
        return t.to_owned();
    }
    if let Some(y) = year.filter(|s| !s.is_empty()) {
        return format!("OpenBox {y}");
    }
    if let Some(g) = game {
        for key in ["name", "title"] {
            if let Some(s) = g.get(key).and_then(Value::as_str).filter(|s| !s.is_empty()) {
                return s.to_owned();
            }
        }
    }
    "OpenBox Reel".to_owned()
}

fn raw_path_from_value(val: &Value) -> Option<&str> {
    if let Some(s) = val.as_str() {
        return Some(s);
    }
    let obj = val.as_object()?;
    for key in [
        "path",
        "file",
        "filename",
        "source",
        "clip",
        "clip_path",
        "video",
        "video_path",
        "image",
        "image_path",
        "screenshot",
        "screenshot_path",
        "media",
        "media_path",
    ] {
        if let Some(s) = obj.get(key).and_then(Value::as_str) {
            return Some(s);
        }
    }
    None
}

fn local_path(val: &Value) -> Option<PathBuf> {
    let text = raw_path_from_value(val)?.trim();
    if text.is_empty() || text.contains('\0') {
        return None;
    }
    let lower = text.to_lowercase();
    if lower.starts_with("http://")
        || lower.starts_with("https://")
        || lower.starts_with("rtmp://")
        || lower.starts_with("file://")
    {
        return None;
    }
    let path = expand_user(Path::new(text));
    let meta = fs::symlink_metadata(&path).ok()?;
    if meta.file_type().is_symlink() || !meta.is_file() {
        return None;
    }
    fs::canonicalize(&path).ok()
}

fn meta_str(val: &Value, keys: &[&str]) -> Option<String> {
    let obj = val.as_object()?;
    for key in keys {
        if let Some(v) = obj.get(*key)
            && !v.is_null()
            && v.as_str() != Some("")
        {
            return Some(match v {
                Value::String(s) => s.clone(),
                other => other.to_string(),
            });
        }
    }
    None
}

fn duration_val(val: &Value, default: f64) -> f64 {
    let candidate = val.as_object().and_then(|obj| {
        ["duration", "duration_seconds", "seconds"]
            .iter()
            .find_map(|k| obj.get(*k).and_then(Value::as_f64))
    });
    let d = candidate
        .filter(|x| x.is_finite() && *x > 0.0)
        .unwrap_or(default)
        .min(60.0);
    (d * 1000.0).round() / 1000.0
}

fn build_entry(
    val: &Value,
    kind: &str,
    default_duration: Option<f64>,
) -> Option<Map<String, Value>> {
    let path = local_path(val)?;
    let mut item = Map::new();
    item.insert("kind".into(), Value::String(kind.to_owned()));
    item.insert(
        "path".into(),
        Value::String(path.to_string_lossy().into_owned()),
    );
    if let Some(created) = meta_str(
        val,
        &["created_at", "captured_at", "taken_at", "timestamp", "date"],
    ) {
        item.insert("created_at".into(), Value::String(created));
    }
    if let Some(label) = meta_str(val, &["title", "name", "label"]) {
        item.insert("title".into(), Value::String(label));
    }
    if let Some(note) = meta_str(val, &["note", "caption", "description"]) {
        item.insert("note".into(), Value::String(note));
    }
    if let Some(def_dur) = default_duration {
        item.insert("duration".into(), json!(duration_val(val, def_dur)));
    }
    Some(item)
}

fn kind_rank(kind: &str) -> u8 {
    match kind {
        "cover" => 0,
        "clip" => 1,
        "moment" => 2,
        _ => 3,
    }
}

pub fn build_reel_manifest(
    game: Option<&Map<String, Value>>,
    year: Option<&str>,
    title: Option<&str>,
) -> Value {
    let mut entries = Vec::new();
    if let Some(g) = game {
        if let Some(cover) = g.get("cover").or_else(|| g.get("cover_path"))
            && let Some(e) = build_entry(cover, "cover", Some(DEFAULT_IMAGE_SECONDS))
        {
            entries.push(e);
        }
        if let Some(clips) = g.get("clips").and_then(Value::as_array) {
            for clip in clips {
                if let Some(e) = build_entry(clip, "clip", None) {
                    entries.push(e);
                }
            }
        }
        if let Some(moments) = g.get("moments").and_then(Value::as_array) {
            for m in moments {
                if let Some(e) = build_entry(m, "moment", Some(DEFAULT_IMAGE_SECONDS)) {
                    entries.push(e);
                }
            }
        }
    }
    entries.sort_by(|a, b| {
        let ka = (
            kind_rank(a.get("kind").and_then(Value::as_str).unwrap_or("")),
            a.get("created_at").and_then(Value::as_str).unwrap_or(""),
            a.get("path")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_lowercase(),
            a.get("path").and_then(Value::as_str).unwrap_or(""),
        );
        let kb = (
            kind_rank(b.get("kind").and_then(Value::as_str).unwrap_or("")),
            b.get("created_at").and_then(Value::as_str).unwrap_or(""),
            b.get("path")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_lowercase(),
            b.get("path").and_then(Value::as_str).unwrap_or(""),
        );
        ka.cmp(&kb)
    });

    let mut seen: HashSet<(String, String)> = HashSet::new();
    let mut unique = Vec::new();
    for item in entries {
        let key = (
            item.get("kind")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned(),
            item.get("path")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned(),
        );
        if seen.insert(key) {
            unique.push(Value::Object(item));
            if unique.len() >= MAX_INPUTS {
                break;
            }
        }
    }

    let mut manifest = json!({
        "format": REEL_FORMAT,
        "subject": subject_for(game, year, None),
        "title": title_for(game, year, title),
        "inputs": unique,
    });
    if let Some(y) = year.filter(|s| !s.is_empty()) {
        manifest
            .as_object_mut()
            .unwrap()
            .insert("year".into(), Value::String(y.to_owned()));
    }
    manifest
}

pub fn reel_path(output_root: &Path, subject: &str, ext: &str) -> PathBuf {
    let suffix = if ext.starts_with('.') {
        ext.to_owned()
    } else {
        format!(".{ext}")
    };
    expand_user(output_root)
        .join("reels")
        .join(format!("{}{}", slug(subject), suffix))
}

fn is_image_entry(entry: &Map<String, Value>) -> bool {
    let kind = entry.get("kind").and_then(Value::as_str).unwrap_or("");
    if matches!(kind, "cover" | "moment" | "image") {
        return true;
    }
    let path = entry.get("path").and_then(Value::as_str).unwrap_or("");
    Path::new(path)
        .extension()
        .and_then(|e| e.to_str())
        .is_some_and(|ext| IMAGE_EXTENSIONS.contains(&format!(".{}", ext.to_lowercase()).as_str()))
}

fn html_escape(input: &str) -> String {
    input
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#x27;")
}

pub fn render_html_reel(manifest: &Value, output_path: &Path) -> Result<String, String> {
    let dest = expand_user(output_path);
    if let Some(parent) = dest.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let title = html_escape(
        manifest
            .get("title")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .unwrap_or("OpenBox Reel"),
    );
    let mut cards = Vec::new();
    if let Some(inputs) = manifest.get("inputs").and_then(Value::as_array) {
        for entry in inputs {
            let Some(obj) = entry.as_object() else {
                continue;
            };
            let Some(path) = obj
                .get("path")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
            else {
                continue;
            };
            let encoded = utf8_percent_encode(path, HREF_ENCODE_SET).to_string();
            let href = html_escape(&encoded);
            let label = html_escape(
                obj.get("title")
                    .or_else(|| obj.get("kind"))
                    .and_then(Value::as_str)
                    .unwrap_or("Media"),
            );
            let note = html_escape(obj.get("note").and_then(Value::as_str).unwrap_or(""));
            let content = if is_image_entry(obj) {
                format!(r#"<img loading="lazy" src="{href}" alt="{label}">"#)
            } else {
                format!(r#"<video controls preload="metadata" src="{href}"></video>"#)
            };
            let mut caption = format!("<h2>{label}</h2>");
            if !note.is_empty() {
                caption.push_str(&format!("<p>{note}</p>"));
            }
            cards.push(format!("<article>{content}{caption}</article>"));
        }
    }
    if cards.is_empty() {
        cards.push("<p class=empty>No local media was available for this reel.</p>".to_owned());
    }
    let doc = format!(
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">\
         <meta name=viewport content=\"width=device-width, initial-scale=1\"><title>{title}</title>\
         <style>body{{font:16px system-ui,sans-serif;max-width:72rem;margin:2rem auto;padding:0 1rem;\
         background:#111;color:#eee}}main{{display:grid;gap:1rem}}article{{background:#222;padding:1rem;\
         border-radius:.5rem}}img,video{{display:block;max-width:100%;max-height:70vh;margin:auto}}\
         h1,h2{{margin:.5rem 0}}p{{color:#bbb}}.empty{{text-align:center}}</style></head>\
         <body><header><h1>{title}</h1></header><main>{}</main></body></html>\n",
        cards.join("")
    );
    atomic_write_text(&dest, &doc, 0o644).map_err(|e| e.to_string())?;
    Ok(dest.to_string_lossy().into_owned())
}

fn build_ffmpeg_args(manifest: &Value, output: &Path) -> Option<Vec<String>> {
    let inputs = manifest.get("inputs").and_then(Value::as_array)?;
    let valid: Vec<&Map<String, Value>> = inputs
        .iter()
        .filter_map(Value::as_object)
        .filter(|o| {
            o.get("path")
                .and_then(Value::as_str)
                .is_some_and(|s| !s.is_empty())
        })
        .collect();
    if valid.is_empty() {
        return None;
    }
    let mut args = vec![
        "-y".to_owned(),
        "-hide_banner".to_owned(),
        "-loglevel".to_owned(),
        "error".to_owned(),
    ];
    let mut filters = Vec::new();
    let mut labels = Vec::new();
    for (idx, entry) in valid.iter().enumerate() {
        let p = entry["path"].as_str().unwrap_or("");
        if is_image_entry(entry) {
            let dur = entry
                .get("duration")
                .and_then(Value::as_f64)
                .filter(|x| x.is_finite() && *x > 0.0)
                .unwrap_or(DEFAULT_IMAGE_SECONDS);
            args.extend([
                "-loop".into(),
                "1".into(),
                "-t".into(),
                dur.to_string(),
                "-i".into(),
                p.to_owned(),
            ]);
        } else {
            args.extend(["-i".into(), p.to_owned()]);
        }
        let label = format!("v{idx}");
        labels.push(format!("[{label}]"));
        filters.push(format!(
            "[{idx}:v]scale=1280:720:force_original_aspect_ratio=decrease,\
             pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[{label}]"
        ));
    }
    filters.push(format!(
        "{}concat=n={}:v=1:a=0[outv]",
        labels.join(""),
        labels.len()
    ));
    args.extend([
        "-filter_complex".into(),
        filters.join(";"),
        "-map".into(),
        "[outv]".into(),
        "-an".into(),
        "-c:v".into(),
        "libx264".into(),
        "-pix_fmt".into(),
        "yuv420p".into(),
        "-movflags".into(),
        "+faststart".into(),
        output.to_string_lossy().into_owned(),
    ]);
    Some(args)
}

pub fn render_reel(
    manifest: &Value,
    output_root: &Path,
    timeout_secs: f64,
) -> Result<Value, String> {
    let subject = manifest
        .get("subject")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .unwrap_or("reel");
    let output = reel_path(output_root, subject, ".mp4");
    if let Some(parent) = output.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let manifest_file = output.with_extension("manifest.json");
    let manifest_text = serde_json::to_string_pretty(manifest).map_err(|e| e.to_string())? + "\n";
    if manifest_text.len() > MAX_MANIFEST_BYTES {
        return Err("Reel manifest is too large.".to_owned());
    }
    atomic_write_text(&manifest_file, &manifest_text, 0o644).map_err(|e| e.to_string())?;

    let inputs_count = manifest
        .get("inputs")
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0);
    let ffmpeg_bin = which("ffmpeg");
    let mut render_error = String::new();

    if let Some(ref bin) = ffmpeg_bin
        && let Some(parent) = output.parent()
    {
        let tmp_name = format!(".{}.tmp.mp4", slug(subject));
        let tmp_output = parent.join(tmp_name);
        if let Some(args) = build_ffmpeg_args(manifest, &tmp_output) {
            let cap = timeout_secs.clamp(1.0, MAX_FFMPEG_TIMEOUT);
            match Command::new(bin)
                .args(&args)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
            {
                Ok(mut child) => {
                    let deadline = Instant::now() + Duration::from_secs_f64(cap);
                    let mut ok = false;
                    loop {
                        match child.try_wait() {
                            Ok(Some(st)) => {
                                ok = st.success();
                                if !ok {
                                    render_error = "CalledProcessError".to_owned();
                                }
                                break;
                            }
                            Ok(None) => {
                                if Instant::now() >= deadline {
                                    let _ = child.kill();
                                    let _ = child.wait();
                                    render_error = "TimeoutExpired".to_owned();
                                    break;
                                }
                                thread::sleep(Duration::from_millis(50));
                            }
                            Err(_) => {
                                render_error = "OSError".to_owned();
                                break;
                            }
                        }
                    }
                    if ok && fs::rename(&tmp_output, &output).is_ok() {
                        return Ok(json!({
                            "path": output.to_string_lossy(),
                            "output": output.to_string_lossy(),
                            "format": "mp4",
                            "fallback": false,
                            "ffmpeg": true,
                            "manifest": manifest_file.to_string_lossy(),
                            "inputs": inputs_count,
                        }));
                    }
                }
                Err(_) => {
                    render_error = "OSError".to_owned();
                }
            }
        }
        let _ = fs::remove_file(&tmp_output);
    }

    let html_output = output.with_extension("html");
    render_html_reel(manifest, &html_output)?;
    let mut res = json!({
        "path": html_output.to_string_lossy(),
        "output": html_output.to_string_lossy(),
        "format": "html",
        "fallback": true,
        "ffmpeg": ffmpeg_bin.is_some(),
        "manifest": manifest_file.to_string_lossy(),
        "inputs": inputs_count,
    });
    if !render_error.is_empty() {
        res.as_object_mut()
            .unwrap()
            .insert("error".into(), Value::String(render_error));
    }
    Ok(res)
}

pub fn create_reel(
    game: Option<&Map<String, Value>>,
    output_root: &Path,
    year: Option<&str>,
) -> Result<Value, String> {
    let manifest = build_reel_manifest(game, year, None);
    render_reel(&manifest, output_root, DEFAULT_FFMPEG_TIMEOUT)
}
