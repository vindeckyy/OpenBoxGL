//! Centralized single-pass launch command placeholder replacement.
//! Mirrors `pkg/parity/launch_tokens.py`.

use std::collections::HashSet;
use std::path::Path;
use std::sync::LazyLock;

use openbox_platform::split_command;
use regex::Regex;
use serde_json::{Value, json};

pub const KNOWN_TOKENS: &[&str] = &[
    "{retroarch_core}",
    "{path}",
    "{Path}",
    "{ImagePath}",
    "{name}",
    "{Name}",
    "{dir}",
    "{Dir}",
    "{file}",
    "{File}",
    "{stem}",
    "{FileNameWithoutExtension}",
    "{platform}",
    "{Platform}",
    "{app_id}",
    "{heroic_app_id}",
    "{lutris_id}",
    "{rom_name}",
    "{DataDir}",
    "{EmulatorDir}",
    "{state_path}",
    "{state_dir}",
    "{state_config}",
    "{state_name}",
];

static ANY_TOKEN_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\{[A-Za-z0-9_]+\}").expect("token regex"));

#[derive(Debug, Clone, Default)]
pub struct TokenContext {
    pub path: String,
    pub name: String,
    pub dir: String,
    pub file: String,
    pub stem: String,
    pub platform: String,
    pub app_id: String,
    pub heroic_app_id: String,
    pub lutris_id: String,
    pub rom_name: String,
    pub data_dir: String,
    pub emulator_dir: String,
    pub state_path: String,
    pub state_dir: String,
    pub state_config: String,
    pub state_name: String,
    pub retroarch_core: String,
}

fn path_parts(resolved: &str) -> (String, String, String) {
    if resolved.starts_with('/') {
        let trimmed = resolved.trim_end_matches('/');
        if trimmed.is_empty() {
            return ("/".to_owned(), String::new(), String::new());
        }
        let (parent, name) = match trimmed.rsplit_once('/') {
            Some(("", n)) => ("/".to_owned(), n.to_owned()),
            Some((p, n)) => (p.to_owned(), n.to_owned()),
            None => (String::new(), trimmed.to_owned()),
        };
        let stem = match name.rsplit_once('.') {
            Some((s, _)) if !s.is_empty() => s.to_owned(),
            _ => name.clone(),
        };
        return (parent, name, stem);
    }
    let p = Path::new(resolved);
    let parent = p
        .parent()
        .map(|d| d.to_string_lossy().into_owned())
        .unwrap_or_default();
    let name = p
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let stem = p
        .file_stem()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_default();
    (parent, name, stem)
}

pub fn path_parent(resolved: &str) -> String {
    path_parts(resolved).0
}

pub fn build_context(
    game: &Value,
    path_override: Option<&str>,
    data_dir: &str,
    emulator_dir: &str,
) -> TokenContext {
    let resolved = path_override.map(str::to_owned).unwrap_or_else(|| {
        game.get("path")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned()
    });
    let (dir, file, stem) = path_parts(&resolved);
    TokenContext {
        path: resolved,
        name: game
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned(),
        dir,
        file,
        stem,
        platform: game
            .get("platform")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned(),
        app_id: game
            .get("steam_app_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned(),
        heroic_app_id: game
            .get("heroic_app_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned(),
        lutris_id: game
            .get("lutris_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned(),
        rom_name: game
            .get("rom_name")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned(),
        data_dir: data_dir.to_owned(),
        emulator_dir: emulator_dir.to_owned(),
        ..Default::default()
    }
}

fn lookup_token<'a>(tok: &str, ctx: &'a TokenContext) -> Option<&'a str> {
    Some(match tok {
        "{retroarch_core}" => &ctx.retroarch_core,
        "{path}" | "{Path}" | "{ImagePath}" => &ctx.path,
        "{name}" | "{Name}" => &ctx.name,
        "{dir}" | "{Dir}" => &ctx.dir,
        "{file}" | "{File}" => &ctx.file,
        "{stem}" | "{FileNameWithoutExtension}" => &ctx.stem,
        "{platform}" | "{Platform}" => &ctx.platform,
        "{app_id}" => &ctx.app_id,
        "{heroic_app_id}" => &ctx.heroic_app_id,
        "{lutris_id}" => &ctx.lutris_id,
        "{rom_name}" => &ctx.rom_name,
        "{DataDir}" => &ctx.data_dir,
        "{EmulatorDir}" => &ctx.emulator_dir,
        "{state_path}" => &ctx.state_path,
        "{state_dir}" => &ctx.state_dir,
        "{state_config}" => &ctx.state_config,
        "{state_name}" => &ctx.state_name,
        _ => return None,
    })
}

pub fn substitute_once(template: &str, ctx: &TokenContext) -> String {
    ANY_TOKEN_RE
        .replace_all(template, |caps: &regex::Captures<'_>| {
            let tok = &caps[0];
            lookup_token(tok, ctx).unwrap_or(tok).to_owned()
        })
        .into_owned()
}

pub fn apply_tokens(
    template: &str,
    game: &Value,
    path_override: Option<&str>,
    data_dir: &str,
    emulator_dir: &str,
) -> String {
    let ctx = build_context(game, path_override, data_dir, emulator_dir);
    substitute_once(template, &ctx)
}

pub fn build_launch_args(
    template: &str,
    game: &Value,
    path_override: Option<&str>,
    data_dir: &str,
    emulator_dir: &str,
) -> Vec<String> {
    let parts = split_command(template)
        .unwrap_or_else(|_| template.split_whitespace().map(str::to_owned).collect());
    let ctx = build_context(game, path_override, data_dir, emulator_dir);
    parts
        .into_iter()
        .map(|part| substitute_once(&part, &ctx))
        .collect()
}

pub fn extract_tokens(template: &str) -> HashSet<String> {
    ANY_TOKEN_RE
        .find_iter(template)
        .map(|m| m.as_str().to_owned())
        .collect()
}

pub fn find_invalid_tokens(template: &str) -> Vec<String> {
    let mut bad: Vec<String> = extract_tokens(template)
        .into_iter()
        .filter(|tok| !KNOWN_TOKENS.contains(&tok.as_str()))
        .collect();
    bad.sort();
    bad
}

pub fn validate_tokens(template: &str) -> Option<Value> {
    let invalid = find_invalid_tokens(template);
    if invalid.is_empty() {
        return None;
    }
    let mut known: Vec<&str> = KNOWN_TOKENS.to_vec();
    known.sort_unstable();
    Some(json!({
        "kind": "explain_token",
        "label": format!("Unknown token: {}", invalid.join(", ")),
        "payload": {
            "invalid_tokens": invalid,
            "known_tokens": known,
        }
    }))
}

pub fn validate_startup_args(startup_args: &[String]) -> Vec<String> {
    if startup_args.is_empty() {
        return Vec::new();
    }
    find_invalid_tokens(&startup_args.join(" "))
}
