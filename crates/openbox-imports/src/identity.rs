//! Canonical identity normalization for imported games (`pkg/parity/parity_identity.py`).

use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::sync::LazyLock;

use openbox_platform::{expand_user, path_identity_fold, resolve_non_strict};
use regex::Regex;
use serde_json::{Value, json};

pub const PC_STORE_SOURCES: &[&str] = &[
    "steam", "epic", "gog", "amazon", "ea", "ubisoft", "xbox", "lutris", "gameyfin", "faugus",
];
pub const PC_PLATFORMS: &[&str] = &["", "pc", "windows", "linux", "mac", "macos"];

static ROM_EXT_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)\.(zip|7z|rar|tar|gz|iso|bin|cue|chd|nes|sfc|smc|gba|nds|n64|z64|gcm|wbfs|pbp|pkg|xiso|xbe|mdf|mds|img|ccd|sub)$")
        .expect("valid rom ext regex")
});
static TAG_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\(.*?\)|\[.*?\]").expect("valid tag regex"));
static DISC_MARKER_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)\b(?:disc|disk|cd|dvd|side)\s*[0-9a-d]+").expect("valid disc marker regex")
});
static SPACES_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\s+").expect("valid spaces regex"));
static SPACE_DOT_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\s+\.").expect("valid space dot regex"));
static NON_ALNUM_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"[^a-z0-9]+").expect("valid non alnum regex"));

pub fn normalize_path_identity(path: &str) -> String {
    if path.is_empty() {
        return String::new();
    }
    let expanded = expand_user(Path::new(path));
    let resolved =
        std::fs::canonicalize(&expanded).unwrap_or_else(|_| resolve_non_strict(&expanded));
    let folded = path_identity_fold(&resolved.to_string_lossy());
    let sep = std::path::MAIN_SEPARATOR;
    folded.trim_end_matches(sep).to_owned()
}

pub fn normalize_rom_name(filename: &str) -> String {
    if filename.is_empty() {
        return String::new();
    }
    let stripped_ext = ROM_EXT_RE.replace(filename, "");
    let stripped_tags = TAG_RE.replace_all(&stripped_ext, |caps: &regex::Captures<'_>| {
        let tag = caps.get(0).map(|m| m.as_str()).unwrap_or("");
        if DISC_MARKER_RE.is_match(tag) {
            tag.to_owned()
        } else {
            String::new()
        }
    });
    let lower = stripped_tags.to_lowercase();
    let trimmed = lower.trim();
    let collapsed = SPACES_RE.replace_all(trimmed, " ");
    SPACE_DOT_RE.replace_all(&collapsed, ".").into_owned()
}

pub fn normalize_title_identity(name: &str) -> String {
    if name.is_empty() {
        return String::new();
    }
    let lower = name
        .to_lowercase()
        .replace('&', " and ")
        .replace(['™', '®', '©'], "");
    let cleaned = NON_ALNUM_RE.replace_all(&lower, " ");
    SPACES_RE.replace_all(&cleaned, " ").trim().to_owned()
}

fn str_prop(game: &Value, key: &str) -> String {
    match game.get(key) {
        Some(Value::String(s)) => s.trim().to_owned(),
        Some(Value::Number(n)) => n.to_string(),
        _ => String::new(),
    }
}

pub fn source_identities(game: &Value) -> Vec<String> {
    if !game.is_object() {
        return Vec::new();
    }
    let mut identities = Vec::new();
    fn push_unique(identities: &mut Vec<String>, val: String) {
        if !val.is_empty() && !identities.contains(&val) {
            identities.push(val);
        }
    }
    if let Some(arr) = game.get("source_identities").and_then(Value::as_array) {
        for item in arr {
            if let Some(s) = item.as_str() {
                push_unique(&mut identities, s.trim().to_owned());
            }
        }
    }
    let si = str_prop(game, "source_identity");
    if !si.is_empty() {
        push_unique(&mut identities, si);
    }
    let steam = str_prop(game, "steam_app_id");
    if !steam.is_empty() {
        push_unique(&mut identities, format!("steam:{steam}"));
    }
    let heroic = str_prop(game, "heroic_app_id");
    if !heroic.is_empty() {
        let src = {
            let hs = str_prop(game, "heroic_source");
            if !hs.is_empty() {
                hs
            } else {
                str_prop(game, "source")
            }
        };
        if !src.is_empty() {
            push_unique(&mut identities, format!("heroic:{src}:{heroic}"));
        }
    }
    for (field, prefix) in [
        ("lutris_id", "lutris"),
        ("gameyfin_id", "gameyfin"),
        ("faugus_id", "faugus"),
        ("gog_id", "gog"),
        ("ea_id", "ea"),
        ("ubisoft_id", "ubisoft"),
    ] {
        let val = str_prop(game, field);
        if !val.is_empty() {
            push_unique(&mut identities, format!("{prefix}:{val}"));
        }
    }
    let rom = str_prop(game, "rom_name");
    if !rom.is_empty() {
        let src = {
            let s = str_prop(game, "source");
            if s.is_empty() { "arcade".to_owned() } else { s }
        };
        push_unique(
            &mut identities,
            format!("{src}:{}", normalize_rom_name(&rom)),
        );
    }
    if identities.is_empty() {
        let path = str_prop(game, "path");
        if !path.is_empty() {
            let norm = normalize_path_identity(&path);
            if !norm.is_empty() {
                push_unique(&mut identities, format!("path:{norm}"));
            }
        }
    }
    identities
}

pub fn source_family(game: &Value) -> String {
    if !game.is_object() {
        return String::new();
    }
    if !str_prop(game, "steam_app_id").is_empty() {
        return "steam".to_owned();
    }
    if !str_prop(game, "heroic_app_id").is_empty() {
        let src = {
            let hs = str_prop(game, "heroic_source");
            if !hs.is_empty() {
                hs
            } else {
                str_prop(game, "source")
            }
        }
        .to_lowercase();
        return if src.is_empty() {
            "heroic".to_owned()
        } else {
            format!("heroic:{src}")
        };
    }
    for (field, label) in [
        ("lutris_id", "lutris"),
        ("gameyfin_id", "gameyfin"),
        ("faugus_id", "faugus"),
        ("gog_id", "gog"),
        ("ea_id", "ea"),
        ("ubisoft_id", "ubisoft"),
    ] {
        if !str_prop(game, field).is_empty() {
            return label.to_owned();
        }
    }
    let src = str_prop(game, "source");
    if !src.is_empty() {
        src.to_lowercase()
    } else {
        str_prop(game, "collection").to_lowercase()
    }
}

pub fn normalize_identity(game: &Value) -> Option<String> {
    source_identities(game).into_iter().next()
}

pub fn cross_source_identity(game: &Value) -> Option<String> {
    if !game.is_object() {
        return None;
    }
    let source = {
        let s = str_prop(game, "source");
        if !s.is_empty() {
            s
        } else {
            str_prop(game, "collection")
        }
    }
    .to_lowercase();
    let platform = str_prop(game, "platform").to_lowercase();
    let has_store_id = [
        "steam_app_id",
        "heroic_app_id",
        "lutris_id",
        "gameyfin_id",
        "faugus_id",
        "gog_id",
        "ea_id",
        "ubisoft_id",
    ]
    .iter()
    .any(|f| !str_prop(game, f).is_empty());
    if (!PC_STORE_SOURCES.contains(&source.as_str()) && !has_store_id)
        || !PC_PLATFORMS.contains(&platform.as_str())
    {
        return None;
    }
    let title = normalize_title_identity(&str_prop(game, "name"));
    if title.is_empty() {
        None
    } else {
        Some(format!("pc-title:{title}"))
    }
}

pub fn detect_duplicate_identities(games: &[Value], include_cross_source: bool) -> Vec<Value> {
    let mut identities: HashMap<String, Vec<Value>> = HashMap::new();
    let mut cross: HashMap<String, (Vec<Value>, HashSet<String>)> = HashMap::new();
    let mut ident_order = Vec::new();
    let mut cross_order = Vec::new();
    for game in games {
        let gid = game
            .get("game_id")
            .filter(|v| !v.is_null() && v.as_str() != Some(""))
            .or_else(|| game.get("id"))
            .cloned();
        let Some(gid) = gid else {
            continue;
        };
        for ident in source_identities(game) {
            if !identities.contains_key(&ident) {
                ident_order.push(ident.clone());
            }
            identities.entry(ident).or_default().push(gid.clone());
        }
        if include_cross_source && let Some(ci) = cross_source_identity(game) {
            let family = source_family(game);
            if !family.is_empty() {
                if !cross.contains_key(&ci) {
                    cross_order.push(ci.clone());
                }
                let entry = cross.entry(ci).or_default();
                entry.0.push(gid);
                entry.1.insert(family);
            }
        }
    }
    let mut out = Vec::new();
    for ident in ident_order {
        if let Some(gids) = identities.remove(&ident)
            && gids.len() > 1
        {
            out.push(json!({"identity": ident, "games": gids}));
        }
    }
    for ident in cross_order {
        if let Some((gids, families)) = cross.remove(&ident)
            && gids.len() > 1
            && families.len() > 1
        {
            out.push(json!({"identity": ident, "games": gids}));
        }
    }
    out
}

pub fn backfill_source_identity(games: &mut [Value]) -> usize {
    let mut count = 0usize;
    for game in games.iter_mut() {
        if game
            .as_object()
            .is_some_and(|o| !o.contains_key("source_identity"))
            && let Some(ident) = normalize_identity(game)
            && let Some(obj) = game.as_object_mut()
        {
            obj.insert("source_identity".into(), Value::String(ident));
            count += 1;
        }
    }
    count
}

pub fn game_identity_key(game: &Value) -> Vec<String> {
    let sp = |k: &str| str_prop(game, k);
    if !sp("steam_app_id").is_empty() {
        return vec!["steam".into(), sp("steam_app_id")];
    }
    if !sp("heroic_app_id").is_empty() {
        return vec!["heroic".into(), sp("source"), sp("heroic_app_id")];
    }
    if !sp("lutris_id").is_empty() {
        return vec!["lutris".into(), sp("lutris_id")];
    }
    if !sp("gog_id").is_empty() {
        return vec!["gog".into(), sp("gog_id")];
    }
    if !sp("ea_id").is_empty() {
        return vec!["ea".into(), sp("ea_id")];
    }
    if !sp("ubisoft_id").is_empty() {
        return vec!["ubisoft".into(), sp("ubisoft_id")];
    }
    if !sp("rom_name").is_empty() {
        return vec!["arcade".into(), sp("source"), sp("rom_name")];
    }
    let p = sp("path");
    let exp = if p.is_empty() {
        String::new()
    } else {
        expand_user(Path::new(&p)).to_string_lossy().into_owned()
    };
    vec![
        "path".into(),
        Path::new(&exp).to_string_lossy().into_owned(),
    ]
}
