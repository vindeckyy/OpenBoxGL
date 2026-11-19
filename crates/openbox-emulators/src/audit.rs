//! Launch Audit (F1): run Launch Doctor across the whole library grouped by root cause.
//! Mirrors `pkg/parity/parity_launch_audit.py`.

use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};

use openbox_io::atomic_write_text;
use serde_json::{Map, Value, json};

use crate::defs::Adapter;
use crate::doctor::{derive_status, run_preflight_checks};

pub const AUDIT_FILE_NAME: &str = "launch_audit.json";

fn audit_path(data_dir: &Path) -> PathBuf {
    data_dir.join("cache").join(AUDIT_FILE_NAME)
}

fn subject_of(check: &Value) -> String {
    let payload = check
        .get("fix_action")
        .and_then(|f| f.get("payload"))
        .and_then(Value::as_object);
    let Some(p) = payload else {
        return String::new();
    };
    for key in ["app_id", "core", "adapter_id", "extension"] {
        if let Some(s) = p.get(key).and_then(Value::as_str).filter(|s| !s.is_empty()) {
            return s.to_owned();
        }
    }
    String::new()
}

fn cause_payload(fix_action: Option<&Value>) -> Map<String, Value> {
    let Some(fix) = fix_action.and_then(Value::as_object) else {
        return Map::new();
    };
    let mut payload = fix
        .get("payload")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let keep_path = fix.get("kind").and_then(Value::as_str) == Some("flatpak_grant");
    for key in ["path", "template", "name"] {
        if key == "path" && keep_path {
            continue;
        }
        payload.remove(key);
    }
    payload
}

fn group_key(check: &Value) -> String {
    let code = check.get("code").and_then(Value::as_str).unwrap_or("");
    let kind = check
        .get("fix_action")
        .and_then(|f| f.get("kind"))
        .and_then(Value::as_str)
        .unwrap_or("");
    let mut key = format!("{code}|{kind}|{}", subject_of(check));
    if kind == "flatpak_grant"
        && let Some(path) = check
            .get("fix_action")
            .and_then(|f| f.get("payload"))
            .and_then(|p| p.get("path"))
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
    {
        key.push('|');
        key.push_str(path);
    }
    key
}

fn severity_rank(sev: &str) -> u8 {
    match sev {
        "error" => 0,
        "warning" => 1,
        "info" => 2,
        _ => 3,
    }
}

pub fn audit_library(
    games: &[Value],
    profiles: &Map<String, Value>,
    adapters: &[Adapter],
    data_dir: &Path,
    deep: bool,
) -> Value {
    let mut groups: HashMap<String, Value> = HashMap::new();
    let mut ready = 0usize;
    let mut warning = 0usize;
    let mut blocked = 0usize;

    for (idx, game) in games.iter().enumerate() {
        if !game.is_object() {
            continue;
        }
        let member = json!({
            "game_id": game.get("game_id").and_then(Value::as_str).unwrap_or(""),
            "index": idx,
            "name": game.get("name").and_then(Value::as_str).unwrap_or(""),
        });
        let (checks, _) = run_preflight_checks(game, profiles, adapters, data_dir, deep);
        match derive_status(&checks) {
            "blocked" => blocked += 1,
            "warning" => warning += 1,
            _ => ready += 1,
        }
        let mut seen = HashSet::new();
        for check in &checks {
            let key = group_key(check);
            if !seen.insert(key.clone()) {
                continue;
            }
            let cause = cause_payload(check.get("fix_action"));
            let entry = groups.entry(key.clone()).or_insert_with(|| {
                let fix = check.get("fix_action").and_then(Value::as_object).map(|f| {
                    let mut m = f.clone();
                    m.insert("payload".into(), Value::Object(cause.clone()));
                    Value::Object(m)
                });
                json!({
                    "key": key,
                    "code": check.get("code").and_then(Value::as_str).unwrap_or(""),
                    "subject": subject_of(check),
                    "severity": check.get("severity").and_then(Value::as_str).unwrap_or("info"),
                    "message": check.get("message").and_then(Value::as_str).unwrap_or(""),
                    "fix_action": fix,
                    "games": [],
                })
            });
            if let Some(arr) = entry.get_mut("games").and_then(Value::as_array_mut) {
                arr.push(member.clone());
            }
        }
    }

    let mut ordered: Vec<Value> = groups.into_values().collect();
    for g in &mut ordered {
        let cnt = g
            .get("games")
            .and_then(Value::as_array)
            .map(|a| a.len())
            .unwrap_or(0);
        g["count"] = Value::from(cnt);
    }
    ordered.sort_by(|a, b| {
        let sa = severity_rank(a["severity"].as_str().unwrap_or(""));
        let sb = severity_rank(b["severity"].as_str().unwrap_or(""));
        let ca = a["count"].as_u64().unwrap_or(0);
        let cb = b["count"].as_u64().unwrap_or(0);
        sa.cmp(&sb)
            .then_with(|| cb.cmp(&ca))
            .then_with(|| a["key"].as_str().cmp(&b["key"].as_str()))
    });

    json!({
        "computed_at": jiff::Timestamp::now().to_string(),
        "game_count": games.len(),
        "deep": deep,
        "totals": {"ready": ready, "warning": warning, "blocked": blocked},
        "groups": ordered,
        "failed": {"count": 0, "sample": []},
    })
}

pub fn load_audit(data_dir: &Path) -> Option<Value> {
    let text = fs::read_to_string(audit_path(data_dir)).ok()?;
    serde_json::from_str::<Value>(&text)
        .ok()
        .filter(Value::is_object)
}

pub fn store_audit(data_dir: &Path, report: &Value) -> Result<(), String> {
    let path = audit_path(data_dir);
    let text = serde_json::to_string_pretty(report).map_err(|e| e.to_string())?;
    atomic_write_text(&path, &text, 0o600)
        .map(|_| ())
        .map_err(|e| e.to_string())
}

pub fn public_summary(report: Option<&Value>, game_count: usize) -> Value {
    let Some(rep) = report else {
        return json!({
            "scanned": false,
            "stale": false,
            "computed_at": null,
            "game_count": game_count,
            "deep": false,
            "totals": {"ready": 0, "warning": 0, "blocked": 0},
            "groups": [],
        });
    };
    let rep_count = rep.get("game_count").and_then(Value::as_u64).unwrap_or(0) as usize;
    let groups: Vec<Value> = rep
        .get("groups")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .map(|g| {
                    let mut m = g.as_object().cloned().unwrap_or_default();
                    m.remove("games");
                    Value::Object(m)
                })
                .collect()
        })
        .unwrap_or_default();
    json!({
        "scanned": true,
        "stale": rep_count != game_count,
        "computed_at": rep.get("computed_at").cloned().unwrap_or(Value::Null),
        "game_count": rep_count,
        "deep": rep.get("deep").and_then(Value::as_bool).unwrap_or(false),
        "totals": rep.get("totals").cloned().unwrap_or_else(|| json!({"ready": 0, "warning": 0, "blocked": 0})),
        "groups": groups,
    })
}

pub fn group_members(
    report: Option<&Value>,
    group_key: &str,
    offset: usize,
    limit: usize,
) -> Value {
    let Some(games) = report
        .and_then(|r| r.get("groups"))
        .and_then(Value::as_array)
        .and_then(|arr| {
            arr.iter()
                .find(|g| g.get("key").and_then(Value::as_str) == Some(group_key))
        })
        .and_then(|g| g.get("games"))
        .and_then(Value::as_array)
    else {
        return json!({"group": group_key, "total": 0, "offset": offset, "limit": limit, "games": []});
    };
    let total = games.len();
    let start = offset.min(total);
    let end = (start + limit).min(total);
    json!({
        "group": group_key,
        "total": total,
        "offset": offset,
        "limit": limit,
        "games": games[start..end],
    })
}

pub fn readiness_statuses(report: Option<&Value>, game_count: usize) -> Map<String, Value> {
    let mut statuses = Map::new();
    let Some(rep) = report else {
        return statuses;
    };
    if rep.get("game_count").and_then(Value::as_u64).unwrap_or(0) as usize != game_count {
        return statuses;
    }
    let Some(groups) = rep.get("groups").and_then(Value::as_array) else {
        return statuses;
    };
    for g in groups {
        let sev = g.get("severity").and_then(Value::as_str).unwrap_or("");
        let st = match sev {
            "error" => "blocked",
            "warning" => "warning",
            _ => continue,
        };
        if let Some(members) = g.get("games").and_then(Value::as_array) {
            for m in members {
                if let Some(gid) = m
                    .get("game_id")
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
                    && statuses.get(gid).and_then(Value::as_str) != Some("blocked")
                {
                    statuses.insert(gid.to_owned(), Value::String(st.to_owned()));
                }
            }
        }
    }
    statuses
}
