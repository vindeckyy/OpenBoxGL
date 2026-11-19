//! Per-emulator settings schemas, validation, launch-argument application, and version probes.
//! Mirrors `pkg/parity/parity_emulator_settings.py`.

use std::collections::HashMap;
use std::path::Path;
use std::process::Command;
use std::sync::LazyLock;
use std::time::{Duration, Instant};

use openbox_io::atomic_write_text;
use openbox_platform::resolve_native_executable;
use parking_lot::Mutex;
use serde_json::{Map, Value, json};

const VERSION_CACHE_TTL: Duration = Duration::from_secs(60);

static VERSION_CACHE: LazyLock<Mutex<HashMap<String, (Instant, Value)>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

pub fn settings_schemas() -> Value {
    json!({
        "mame": {
            "label": "MAME",
            "fields": {
                "monitor": {"type": "integer", "minimum": 1, "maximum": 4, "default": 1},
                "borderless": {"type": "boolean", "default": false},
                "screen_effect": {"type": "enum", "values": ["none", "scanlines"], "default": "none"},
                "lightgun": {"type": "boolean", "default": false},
                "trackball": {"type": "boolean", "default": false},
            },
        },
        "retroarch": {
            "label": "RetroArch",
            "fields": {
                "display_mode": {
                    "type": "enum",
                    "values": ["windowed", "fullscreen", "borderless"],
                    "default": "fullscreen",
                },
                "screen_effect": {"type": "enum", "values": ["none", "scanlines"], "default": "none"},
                "run_ahead": {"type": "integer", "minimum": 0, "maximum": 3, "default": 0},
                "rewind": {"type": "boolean", "default": false},
                "upscale": {"type": "integer", "minimum": 1, "maximum": 4, "default": 1},
            },
        },
    })
}

pub fn canonical_emulator_id(emulator_id: &str) -> Result<&'static str, String> {
    match emulator_id.trim().to_ascii_lowercase().as_str() {
        "mame" | "org.mamedev.mame" => Ok("mame"),
        "retroarch" | "org.libretro.retroarch" => Ok("retroarch"),
        "" => Err("Unknown emulator: (empty)".to_owned()),
        _ => Err(format!("Unknown emulator: {emulator_id}")),
    }
}

pub fn app_id_for_canonical(canonical: &str) -> &'static str {
    match canonical {
        "mame" => "org.mamedev.MAME",
        "retroarch" => "org.libretro.RetroArch",
        _ => "",
    }
}

fn coerce_field(field: &Value, val: &Value, name: &str) -> Result<Value, String> {
    let kind = field.get("type").and_then(Value::as_str).unwrap_or("");
    match kind {
        "integer" => {
            if val.is_boolean() {
                return Err(format!("{name} must be an integer."));
            }
            let n = if let Some(i) = val.as_i64() {
                i
            } else if let Some(f) = val.as_f64() {
                if f.fract() != 0.0 {
                    return Err(format!("{name} must be an integer."));
                }
                f as i64
            } else if let Some(s) = val.as_str() {
                s.trim()
                    .parse::<i64>()
                    .map_err(|_| format!("{name} must be an integer."))?
            } else {
                return Err(format!("{name} must be an integer."));
            };
            let min = field
                .get("minimum")
                .and_then(Value::as_i64)
                .unwrap_or(i64::MIN);
            let max = field
                .get("maximum")
                .and_then(Value::as_i64)
                .unwrap_or(i64::MAX);
            if n < min || n > max {
                return Err(format!("{name} must be between {min} and {max}."));
            }
            Ok(Value::from(n))
        }
        "boolean" => {
            if let Some(b) = val.as_bool() {
                return Ok(Value::Bool(b));
            }
            if let Some(i) = val.as_i64()
                && (i == 0 || i == 1)
            {
                return Ok(Value::Bool(i == 1));
            }
            if let Some(s) = val.as_str() {
                match s.trim().to_ascii_lowercase().as_str() {
                    "1" | "true" | "yes" | "on" => return Ok(Value::Bool(true)),
                    "0" | "false" | "no" | "off" => return Ok(Value::Bool(false)),
                    _ => {}
                }
            }
            Err(format!("{name} must be true or false."))
        }
        "enum" => {
            let allowed: Vec<&str> = field
                .get("values")
                .and_then(Value::as_array)
                .map(|a| a.iter().filter_map(Value::as_str).collect())
                .unwrap_or_default();
            if let Some(s) = val.as_str()
                && allowed.contains(&s)
            {
                return Ok(Value::String(s.to_owned()));
            }
            Err(format!("{name} must be one of: {}.", allowed.join(", ")))
        }
        _ => Err(format!("{name} has an unsupported type.")),
    }
}

pub fn normalize_values(
    emulator_id: &str,
    values: Option<&Value>,
) -> Result<Map<String, Value>, String> {
    let canonical = canonical_emulator_id(emulator_id)?;
    let schemas = settings_schemas();
    let schema = &schemas[canonical];
    let Some(val) = values else {
        return Ok(Map::new());
    };
    if val.is_null() {
        return Ok(Map::new());
    }
    let Some(obj) = val.as_object() else {
        return Err("Emulator settings values must be an object.".to_owned());
    };
    let fields = schema["fields"].as_object().unwrap();
    let label = schema["label"].as_str().unwrap_or(canonical);
    let mut cleaned = Map::new();
    for (name, v) in obj {
        let Some(field) = fields.get(name) else {
            return Err(format!("Unknown setting for {label}: {name}"));
        };
        cleaned.insert(name.clone(), coerce_field(field, v, name)?);
    }
    Ok(cleaned)
}

fn lookup_entry<'a>(state: &'a Value, canonical: &str) -> Option<&'a Map<String, Value>> {
    let entries = state.get("emulator_settings")?.as_object()?;
    if let Some(e) = entries.get(canonical).and_then(Value::as_object) {
        return Some(e);
    }
    let app_id = app_id_for_canonical(canonical);
    entries.iter().find_map(|(k, v)| {
        let kl = k.to_ascii_lowercase();
        if (kl == canonical || kl == app_id.to_ascii_lowercase()) && v.is_object() {
            v.as_object()
        } else {
            None
        }
    })
}

pub fn is_managed(state: &Value, emulator_id: &str) -> bool {
    let Ok(canonical) = canonical_emulator_id(emulator_id) else {
        return false;
    };
    lookup_entry(state, canonical)
        .and_then(|e| e.get("managed"))
        .and_then(Value::as_bool)
        .unwrap_or(false)
}

pub fn effective_values(state: &Value, emulator_id: &str, platform: &str) -> Map<String, Value> {
    let Ok(canonical) = canonical_emulator_id(emulator_id) else {
        return Map::new();
    };
    let schemas = settings_schemas();
    let fields = schemas[canonical]["fields"].as_object().unwrap();
    let mut values = Map::new();
    for (name, spec) in fields {
        values.insert(name.clone(), spec["default"].clone());
    }
    if let Some(entry) = lookup_entry(state, canonical) {
        if let Some(glob) = entry.get("global").and_then(Value::as_object) {
            for (k, v) in glob {
                if let Some(spec) = fields.get(k)
                    && let Ok(c) = coerce_field(spec, v, k)
                {
                    values.insert(k.clone(), c);
                }
            }
        }
        if !platform.is_empty()
            && let Some(plat) = entry
                .get("platforms")
                .and_then(|p| p.get(platform))
                .and_then(Value::as_object)
        {
            for (k, v) in plat {
                if let Some(spec) = fields.get(k)
                    && let Ok(c) = coerce_field(spec, v, k)
                {
                    values.insert(k.clone(), c);
                }
            }
        }
    }
    values
}

fn insert_before_rom(argv: &[String], flags: &[String]) -> Vec<String> {
    if flags.is_empty() {
        return argv.to_vec();
    }
    let idx = if argv.len() < 2 || argv.last().is_some_and(|s| s.starts_with('-')) {
        argv.len()
    } else {
        argv.len() - 1
    };
    let mut out = Vec::with_capacity(argv.len() + flags.len());
    out.extend_from_slice(&argv[..idx]);
    out.extend_from_slice(flags);
    out.extend_from_slice(&argv[idx..]);
    out
}

pub fn apply_to_command(
    emulator_id: &str,
    platform: &str,
    argv: &[String],
    state: &Value,
    data_dir: &Path,
) -> (Vec<String>, Vec<String>) {
    let Ok(canonical) = canonical_emulator_id(emulator_id) else {
        return (argv.to_vec(), Vec::new());
    };
    let vals = effective_values(state, canonical, platform);
    let mut warnings = Vec::new();
    match canonical {
        "mame" => {
            let mut flags = Vec::new();
            let monitor = vals.get("monitor").and_then(Value::as_i64).unwrap_or(1);
            if monitor > 1 {
                flags.push("-screen".to_owned());
                flags.push(format!("\\\\.\\DISPLAY{monitor}"));
            }
            if vals
                .get("borderless")
                .and_then(Value::as_bool)
                .unwrap_or(false)
            {
                flags.push("-window".to_owned());
                flags.push("-nomaximize".to_owned());
            }
            if vals.get("screen_effect").and_then(Value::as_str) == Some("scanlines") {
                flags.push("-effect".to_owned());
                flags.push("scanlines".to_owned());
            }
            if vals
                .get("lightgun")
                .and_then(Value::as_bool)
                .unwrap_or(false)
            {
                flags.push("-lightgun".to_owned());
            }
            if vals
                .get("trackball")
                .and_then(Value::as_bool)
                .unwrap_or(false)
            {
                flags.push("-mouse".to_owned());
            }
            (insert_before_rom(argv, &flags), warnings)
        }
        "retroarch" => {
            let mut lines = Vec::new();
            let mode = vals
                .get("display_mode")
                .and_then(Value::as_str)
                .unwrap_or("fullscreen");
            lines.push(format!(
                "video_fullscreen = \"{}\"",
                if mode == "windowed" { "false" } else { "true" }
            ));
            lines.push(format!(
                "video_windowed_fullscreen = \"{}\"",
                if mode == "borderless" {
                    "true"
                } else {
                    "false"
                }
            ));
            let run_ahead = vals.get("run_ahead").and_then(Value::as_i64).unwrap_or(0);
            lines.push(format!(
                "run_ahead_enabled = \"{}\"",
                if run_ahead > 0 { "true" } else { "false" }
            ));
            if run_ahead > 0 {
                lines.push(format!("run_ahead_frames = \"{run_ahead}\""));
            }
            let rewind = vals.get("rewind").and_then(Value::as_bool).unwrap_or(false);
            lines.push(format!(
                "rewind_enable = \"{}\"",
                if rewind { "true" } else { "false" }
            ));
            let cfg_dir = data_dir.join("emulator-configs");
            let safe_plat: String = platform
                .chars()
                .map(|c| if c.is_ascii_alphanumeric() { c } else { '_' })
                .collect();
            let cfg_path = cfg_dir.join(format!(
                "retroarch-{}.cfg",
                if safe_plat.is_empty() {
                    "global"
                } else {
                    &safe_plat
                }
            ));
            match atomic_write_text(&cfg_path, &(lines.join("\n") + "\n"), 0o600) {
                Ok(_) => (
                    insert_before_rom(
                        argv,
                        &[
                            "--appendconfig".to_owned(),
                            cfg_path.to_string_lossy().into_owned(),
                        ],
                    ),
                    warnings,
                ),
                Err(err) => {
                    warnings.push(format!("Could not write RetroArch config: {err}"));
                    (argv.to_vec(), warnings)
                }
            }
        }
        _ => (argv.to_vec(), warnings),
    }
}

pub fn version_probe(app_id: &str, native_exe: &str) -> Value {
    let key = format!("{app_id}:{native_exe}");
    {
        let cache = VERSION_CACHE.lock();
        if let Some((ts, val)) = cache.get(&key)
            && ts.elapsed() < VERSION_CACHE_TTL
        {
            return val.clone();
        }
    }
    let mut version = String::new();
    if !native_exe.is_empty()
        && let Some(resolved) = resolve_native_executable(native_exe)
        && let Ok(out) = Command::new(resolved).arg("--version").output()
    {
        let stdout = String::from_utf8_lossy(&out.stdout);
        if let Some(first) = stdout.lines().next() {
            version = first.trim().chars().take(80).collect();
        }
    }
    let val = json!({
        "version": version,
        "latest_version": "",
        "update_available": false,
    });
    VERSION_CACHE
        .lock()
        .insert(key, (Instant::now(), val.clone()));
    val
}
