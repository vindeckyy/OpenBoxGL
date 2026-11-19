//! `openbox://` URI parsing and local launcher helpers (`pkg/parity/parity_deeplinks.py`).

use std::fs;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use openbox_metadata::http::{http_get_raw, http_post_raw, quote_path};
use openbox_platform::{open_url, which};
use serde_json::{Map, Value, json};

pub const SCHEME: &str = "openbox";

pub fn parse_uri(uri: &str) -> Map<String, Value> {
    let mut text = uri.trim().to_owned();
    let prefix_slashes = format!("{SCHEME}://");
    let prefix_colon = format!("{SCHEME}:");
    if let Some(rest) = text.strip_prefix(&prefix_slashes) {
        if let Some((authority, path)) = rest.split_once('/') {
            let bare = authority.to_lowercase();
            if !matches!(bare.as_str(), "" | "localhost" | "openbox")
                && (authority.contains('.') || authority.contains(':'))
            {
                let mut m = Map::new();
                m.insert("action".into(), json!("unknown"));
                return m;
            }
            text = if !matches!(bare.as_str(), "" | "localhost" | "openbox") {
                rest.to_owned()
            } else {
                path.to_owned()
            };
        } else {
            let authority = rest;
            if !authority.is_empty() && (authority.contains('.') || authority.contains(':')) {
                let mut m = Map::new();
                m.insert("action".into(), json!("unknown"));
                return m;
            }
            let bare = authority.to_lowercase();
            text = if matches!(bare.as_str(), "" | "localhost" | "openbox") {
                String::new()
            } else {
                authority.to_owned()
            };
        }
    } else if let Some(rest) = text.strip_prefix(&prefix_colon) {
        text = rest.to_owned();
    }
    let trimmed = text.trim_start_matches('/');
    if trimmed.is_empty() {
        let mut m = Map::new();
        m.insert("action".into(), json!("start"));
        return m;
    }
    let (action_raw, remainder) = match trimmed.split_once('/') {
        Some((a, r)) => (a, r),
        None => (trimmed, ""),
    };
    let action = action_raw.to_lowercase();
    let mut payload = Map::new();
    payload.insert("action".into(), json!(action));
    match action.as_str() {
        "showgame" | "game" => {
            payload.insert("id".into(), json!(remainder.trim()));
        }
        "search" => {
            let decoded = percent_encoding::percent_decode_str(remainder)
                .decode_utf8_lossy()
                .into_owned();
            payload.insert("query".into(), json!(decoded));
        }
        "launch" => {
            let decoded = percent_encoding::percent_decode_str(remainder)
                .decode_utf8_lossy()
                .trim()
                .to_owned();
            payload.insert("id".into(), json!(decoded));
        }
        "resume" | "moment" | "clip" => {
            payload.insert("id".into(), json!(remainder.trim()));
        }
        "bigbox" | "fullscreen" => {
            payload.insert("mode".into(), json!("bigbox"));
        }
        "settings" => {
            let panel = remainder.trim();
            payload.insert(
                "panel".into(),
                json!(if panel.is_empty() { "general" } else { panel }),
            );
        }
        _ => {}
    }
    payload
}

pub fn build_launch_url(base_url: &str, action: &str, id: &str, query: &str) -> String {
    let base = base_url.trim_end_matches('/');
    match action {
        "start" => base.to_owned(),
        "showgame" | "launch" | "game" => {
            format!("{base}/?deeplink=showgame&id={}", quote_path(id))
        }
        "search" => format!("{base}/?deeplink=search&q={}", quote_path(query)),
        "bigbox" | "fullscreen" => format!("{base}/?deeplink=bigbox"),
        "settings" => format!("{base}/?deeplink=settings"),
        "resume" | "moment" | "clip" => {
            format!("{base}/?deeplink={action}&id={}", quote_path(id))
        }
        _ => base.to_owned(),
    }
}

pub fn read_port_file(data_dir: &Path) -> u16 {
    fs::read_to_string(data_dir.join("server.port"))
        .ok()
        .and_then(|s| s.trim().parse::<u16>().ok())
        .unwrap_or(0)
}

pub fn read_token_file(data_dir: &Path) -> String {
    fs::read_to_string(data_dir.join("server.token"))
        .ok()
        .map(|s| s.trim().to_owned())
        .unwrap_or_default()
}

fn server_responds(host: &str, port: u16, token: &str) -> bool {
    if port == 0 || token.is_empty() {
        return false;
    }
    let url = format!("http://{host}:{port}/api/v2/version");
    let headers = [("X-OpenBox-Token", token.to_owned())];
    http_get_raw(&url, &headers, 64 * 1024, 2)
        .map(|r| r.status == 200)
        .unwrap_or(false)
}

fn start_server_and_wait(data_dir: &Path, host: &str) -> (u16, String) {
    let cur_port = read_port_file(data_dir);
    let cur_tok = read_token_file(data_dir);
    if server_responds(host, cur_port, &cur_tok) {
        return (cur_port, cur_tok);
    }
    for name in ["server.port", "server.token"] {
        let p = data_dir.join(name);
        if let Ok(m) = fs::symlink_metadata(&p)
            && m.is_file()
        {
            let _ = fs::remove_file(&p);
        }
    }
    let Ok(exe) = std::env::current_exe() else {
        return (0, String::new());
    };
    let spawn_ok = Command::new(exe)
        .arg("--no-browser")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .is_ok();
    if !spawn_ok {
        return (0, String::new());
    }
    let deadline = Instant::now() + Duration::from_secs(15);
    while Instant::now() < deadline {
        let p = read_port_file(data_dir);
        let t = read_token_file(data_dir);
        if server_responds(host, p, &t) {
            return (p, t);
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    (0, String::new())
}

pub fn dispatch_uri(uri: &str, data_dir: &Path, open_browser: bool) -> Option<i32> {
    let host = "127.0.0.1";
    let mut port = read_port_file(data_dir);
    let mut token = read_token_file(data_dir);
    let parsed = parse_uri(uri);
    let action = parsed
        .get("action")
        .and_then(Value::as_str)
        .unwrap_or("start");
    if action == "start" {
        if token.is_empty() || port == 0 {
            return None;
        }
        if open_browser {
            let _ = open_url(&format!("http://{host}:{port}/?token={token}"));
        }
        return Some(0);
    }
    let needs_token = matches!(action, "launch" | "resume");
    if port == 0 || (needs_token && token.is_empty()) {
        let (p, t) = start_server_and_wait(data_dir, host);
        port = p;
        token = t;
        if port == 0 {
            eprintln!("OpenBox could not start its local server.");
            return Some(1);
        }
    }
    let id = parsed
        .get("id")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    let query = parsed.get("query").and_then(Value::as_str).unwrap_or("");
    let base = format!("http://{host}:{port}");
    match action {
        "launch" | "resume" => {
            if id.is_empty() {
                eprintln!("Game id is required.");
                return Some(1);
            }
            let body = if id.chars().all(|c| c.is_ascii_digit()) {
                json!({"id": id.parse::<i64>().unwrap_or(0)})
            } else {
                json!({"game_id": id})
            };
            let route = if action == "launch" {
                "/api/v2/launch"
            } else {
                "/api/v2/resume"
            };
            let headers = [
                ("X-OpenBox-Token", token),
                ("Content-Type", "application/json".to_owned()),
            ];
            let payload = serde_json::to_vec(&body).unwrap_or_default();
            match http_post_raw(
                &format!("{base}{route}"),
                payload,
                &headers,
                4 * 1024 * 1024,
                30,
            ) {
                Ok(r) if r.status < 400 => Some(0),
                Ok(r) => {
                    eprintln!(
                        "Request failed ({}): {}",
                        r.status,
                        String::from_utf8_lossy(&r.body)
                    );
                    Some(1)
                }
                Err(e) => {
                    eprintln!("{e}");
                    Some(1)
                }
            }
        }
        "showgame" | "game" | "moment" | "clip" | "search" | "bigbox" | "fullscreen"
        | "settings" => {
            let url = build_launch_url(&base, action, id, query);
            if open_browser {
                let _ = open_url(&url);
            } else {
                println!("{url}");
            }
            Some(0)
        }
        other => {
            eprintln!("Unknown deeplink action: {other}");
            Some(1)
        }
    }
}

pub fn run_keyboard_launcher(app_root: &Path) -> i32 {
    let picker = ["rofi", "wofi", "dmenu"]
        .into_iter()
        .find(|cand| which(cand).is_some());
    let Some(picker) = picker else {
        eprintln!("Install rofi, wofi, or dmenu for the keyboard launcher.");
        return 1;
    };
    for cand in [
        app_root.join("scripts/openbox-launcher.sh"),
        app_root.join("openbox-launcher.sh"),
    ] {
        if cand.is_file() {
            return Command::new(cand)
                .arg(picker)
                .status()
                .map(|s| s.code().unwrap_or(1))
                .unwrap_or(1);
        }
    }
    let menu = "#bigbox\tOpen Big Box\n#settings\tOpen Settings\n/search\tSearch library\n/refresh\tRefresh library imports";
    let mut cmd = match picker {
        "rofi" => {
            let mut c = Command::new("rofi");
            c.args([
                "-dmenu",
                "-i",
                "-p",
                "OpenBox",
                "-kb-custom-1",
                "Control-Return",
            ]);
            c
        }
        "wofi" => {
            let mut c = Command::new("wofi");
            c.args(["--dmenu", "--prompt", "OpenBox"]);
            c
        }
        _ => {
            let mut c = Command::new("dmenu");
            c.args(["-i", "-p", "OpenBox"]);
            c
        }
    };
    let Ok(mut child) = cmd
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
    else {
        return 0;
    };
    if let Some(mut stdin) = child.stdin.take() {
        use std::io::Write;
        let _ = stdin.write_all(menu.as_bytes());
    }
    let Ok(out) = child.wait_with_output() else {
        return 0;
    };
    if !out.status.success() {
        return 0;
    }
    let selection = String::from_utf8_lossy(&out.stdout).trim().to_owned();
    if selection.starts_with("#bigbox") {
        println!("openbox://bigbox");
    } else if selection.starts_with("#settings") {
        println!("openbox://settings");
    } else if selection.starts_with("/refresh") {
        println!("openbox://start");
    } else if !selection.is_empty() {
        println!("{selection}");
    }
    0
}

pub fn launcher_menu_items(games: &[Value]) -> Vec<Value> {
    let mut items = vec![
        json!({"id": "bigbox", "label": "Open Big Box"}),
        json!({"id": "settings", "label": "Open Settings"}),
        json!({"id": "search", "label": "Search library"}),
    ];
    for game in games.iter().take(40) {
        if let Some(name) = game
            .get("name")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
        {
            let id = game.get("id").and_then(Value::as_i64).unwrap_or(0);
            items.push(json!({"id": format!("launch:{id}"), "label": name}));
        }
    }
    items
}
