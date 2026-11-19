//! MAME high score discovery and `hi2txt` table parsing. Mirrors `pkg/parity/parity_highscores.py`.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use openbox_platform::{contained_path, expand_user, home_dir, which};
use serde_json::{Value, json};

pub const MAX_ROWS: usize = 100;
pub const MAX_DISCOVERY_FILES: usize = 500;

pub fn mame_highscore_path(home: Option<&Path>) -> PathBuf {
    let base = home
        .map(Path::to_path_buf)
        .or_else(home_dir)
        .unwrap_or_else(|| PathBuf::from("."));
    let flatpak = base.join(".var/app/org.mamedev.MAME/data/mame/hi");
    if flatpak.is_dir() {
        return flatpak;
    }
    base.join(".mame").join("hi")
}

fn file_metadata(path: &Path) -> Value {
    let size = fs::metadata(path).map(|m| m.len()).unwrap_or(0);
    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let label = path
        .file_stem()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    json!({
        "name": name,
        "path": path.to_string_lossy(),
        "size": size,
        "label": label,
    })
}

pub fn discover(home: Option<&Path>) -> Value {
    let root = mame_highscore_path(home);
    let mut entries: Vec<PathBuf> = fs::read_dir(&root)
        .ok()
        .map(|rd| {
            rd.flatten()
                .map(|e| e.path())
                .filter(|p| p.is_file())
                .collect()
        })
        .unwrap_or_default();
    entries.sort_by(|a, b| {
        a.file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .to_ascii_lowercase()
            .cmp(
                &b.file_name()
                    .unwrap_or_default()
                    .to_string_lossy()
                    .to_ascii_lowercase(),
            )
    });
    entries.truncate(MAX_DISCOVERY_FILES);
    let files: Vec<Value> = entries.iter().map(|p| file_metadata(p)).collect();
    json!({
        "root": root.to_string_lossy(),
        "count": files.len(),
        "files": files,
    })
}

fn parse_score_row(
    rank: Option<&Value>,
    score: Option<&Value>,
    name: Option<&Value>,
) -> Option<Value> {
    let r = match rank? {
        Value::Number(n) => n.as_i64()?,
        Value::String(s) => s.trim().parse::<i64>().ok()?,
        _ => return None,
    };
    let s = match score? {
        Value::Number(n) => n.as_i64()?,
        Value::String(st) => st.replace(',', "").trim().parse::<i64>().ok()?,
        _ => return None,
    };
    let n: String = name
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .chars()
        .take(200)
        .collect();
    Some(json!({"rank": r, "score": s, "name": n}))
}

pub fn parse_hi2txt_output(text: &str) -> Result<Vec<Value>, String> {
    if let Ok(Value::Array(arr)) = serde_json::from_str::<Value>(text) {
        let mut rows = Vec::new();
        for item in arr.iter().take(MAX_ROWS) {
            let Some(obj) = item.as_object() else {
                continue;
            };
            if let Some(row) = parse_score_row(
                obj.get("rank").or_else(|| obj.get("Rank")),
                obj.get("score").or_else(|| obj.get("Score")),
                obj.get("name").or_else(|| obj.get("Name")),
            ) {
                rows.push(row);
            }
        }
        if !rows.is_empty() {
            return Ok(rows);
        }
    }
    let mut rows = Vec::new();
    let mut rank_col: Option<usize> = None;
    let mut score_col: Option<usize> = None;
    let mut name_col: Option<usize> = None;
    for line in text.lines() {
        let cells: Vec<&str> = line
            .trim()
            .trim_matches('|')
            .split('|')
            .map(str::trim)
            .collect();
        if cells.len() < 2 {
            continue;
        }
        let mut hdr_rank = None;
        let mut hdr_score = None;
        let mut hdr_name = None;
        for (i, c) in cells.iter().enumerate() {
            match c.to_ascii_lowercase().as_str() {
                "rank" | "place" | "position" => hdr_rank.get_or_insert(i),
                "score" | "points" => hdr_score.get_or_insert(i),
                "name" | "player" | "initials" => hdr_name.get_or_insert(i),
                _ => continue,
            };
        }
        if hdr_rank.is_some() && hdr_score.is_some() {
            rank_col = hdr_rank;
            score_col = hdr_score;
            name_col = hdr_name;
            continue;
        }
        let r_str = rank_col
            .and_then(|i| cells.get(i))
            .copied()
            .unwrap_or(cells[0]);
        let s_str = score_col
            .and_then(|i| cells.get(i))
            .copied()
            .unwrap_or(cells[1]);
        let n_str = match name_col {
            Some(i) if i < cells.len() => cells[i..].join("|"),
            None if cells.len() > 2 => cells[2..].join("|"),
            _ => String::new(),
        };
        if let Some(row) = parse_score_row(
            Some(&Value::String(r_str.to_owned())),
            Some(&Value::String(s_str.to_owned())),
            Some(&Value::String(n_str)),
        ) {
            rows.push(row);
            if rows.len() >= MAX_ROWS {
                break;
            }
        }
    }
    if !rows.is_empty() {
        return Ok(rows);
    }
    if text.trim_start().starts_with(['[', '{']) {
        Err("hi2txt returned no usable score rows.".to_owned())
    } else {
        Err("hi2txt did not return a usable score table.".to_owned())
    }
}

pub fn parse_file(path: &Path, rom_name: &str) -> Value {
    let expanded = expand_user(path);
    let rom = if !rom_name.trim().is_empty() {
        rom_name.trim().to_owned()
    } else {
        expanded
            .file_stem()
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or_default()
    };
    let meta = file_metadata(&expanded);
    let make = |parsed: bool, scores: Vec<Value>, msg: &str| {
        json!({
            "name": meta["name"],
            "path": meta["path"],
            "size": meta["size"],
            "label": meta["label"],
            "rom_name": rom,
            "source": meta["path"],
            "parsed": parsed,
            "scores": scores,
            "message": msg,
        })
    };
    if !expanded.is_file() {
        return make(false, Vec::new(), "High score file not found.");
    }
    let tool = which("hi2txt").or_else(|| which("hi2txt.exe"));
    let Some(bin) = tool else {
        return make(false, Vec::new(), "hi2txt is not installed.");
    };
    let out = match Command::new(bin)
        .args(["--format", "json", &rom, &expanded.to_string_lossy()])
        .output()
    {
        Ok(o) => o,
        Err(e) => return make(false, Vec::new(), &e.to_string()),
    };
    let stdout = String::from_utf8_lossy(&out.stdout);
    let stderr = String::from_utf8_lossy(&out.stderr);
    if !out.status.success() {
        let detail = if !stderr.trim().is_empty() {
            stderr.trim().to_owned()
        } else if !stdout.trim().is_empty() {
            stdout.trim().to_owned()
        } else {
            format!(
                "hi2txt exited with status {}.",
                out.status.code().unwrap_or(-1)
            )
        };
        let snippet: String = detail.chars().take(300).collect();
        return make(false, Vec::new(), &snippet);
    }
    match parse_hi2txt_output(&stdout) {
        Ok(scores) => make(true, scores, ""),
        Err(msg) => make(false, Vec::new(), &msg),
    }
}

pub fn table_for_game(game: &Value, home: Option<&Path>) -> Value {
    let rom = game
        .get("rom_name")
        .and_then(Value::as_str)
        .filter(|s| !s.trim().is_empty())
        .map(str::to_owned)
        .unwrap_or_else(|| {
            let p = game.get("path").and_then(Value::as_str).unwrap_or("");
            Path::new(p)
                .file_stem()
                .map(|s| s.to_string_lossy().into_owned())
                .unwrap_or_default()
        });
    let root = mame_highscore_path(home);
    if rom.is_empty() {
        return json!({
            "source": root.to_string_lossy(),
            "path": "",
            "name": "",
            "size": 0,
            "label": "",
            "rom_name": "",
            "parsed": false,
            "scores": [],
            "message": "No ROM name is available for this game.",
        });
    }
    let found = fs::read_dir(&root).ok().and_then(|rd| {
        rd.flatten().map(|e| e.path()).find(|p| {
            p.extension()
                .and_then(|e| e.to_str())
                .is_some_and(|e| e.eq_ignore_ascii_case("hi"))
                && p.file_stem()
                    .and_then(|s| s.to_str())
                    .is_some_and(|s| s.eq_ignore_ascii_case(&rom))
        })
    });
    let Some(cand) = found else {
        return json!({
            "source": root.to_string_lossy(),
            "path": "",
            "name": "",
            "size": 0,
            "label": "",
            "rom_name": rom,
            "parsed": false,
            "scores": [],
            "message": "No high score file was found for this game.",
        });
    };
    let fname = cand
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    match contained_path(&root, &[fname.as_str()]) {
        Ok(safe) => parse_file(&safe, &rom),
        Err(err) => json!({
            "source": root.to_string_lossy(),
            "path": "",
            "name": fname,
            "size": 0,
            "label": rom,
            "rom_name": rom,
            "parsed": false,
            "scores": [],
            "message": format!("High score file is outside the MAME directory: {err}"),
        }),
    }
}
