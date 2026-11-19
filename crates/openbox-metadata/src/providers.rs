//! Unified metadata provider registry, title/platform ranking, and safe candidate media
//! downloader (`pkg/parity/parity_metadata_providers.py`).

use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::sync::LazyLock;

use openbox_platform::contained_path;
use openbox_runtime::media_probe::MEDIA_PATH_FIELDS;
use regex::Regex;
use serde_json::{Map, Value, json};

use crate::http::{download_file_sync, sequence_matcher_ratio};
use crate::igdb;
use crate::lbdb::{
    IMAGE_URL, load_media_images, media_type_map, platform_alias, search_games as lbdb_search,
    year_from_record,
};
use crate::screenscraper::{self, clean_media_url as ss_clean_media_url};
use crate::steamgrid;

pub const MAX_CANDIDATES: usize = 20;
pub const MAX_MEDIA_URLS_PER_KIND: usize = 10;
pub const MAX_URL_LENGTH: usize = 2000;

pub const BUILTIN_PROVIDER_ORDER: &[&str] = &["launchbox", "screenscraper", "igdb", "steamgriddb"];

pub const APPLY_FIELDS: &[&str] = &[
    "name",
    "platform",
    "genre",
    "year",
    "developer",
    "publisher",
    "series",
    "description",
    "esrb",
    "max_players",
    "rating",
    "region",
    "sort_title",
];

static ARTICLE_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^(?:the|a|an)\s+").unwrap());
static LEADING_ARTICLE_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)^(the|a|an)\s+(.+)$").unwrap());
static TRAILING_ARTICLE_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i),\s*(the|a|an)\s*$").unwrap());
static PUNCT_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[^a-z0-9]+").unwrap());
static KIND_SLUG_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[^a-z0-9_-]+").unwrap());

pub fn normalize_title(value: &str) -> String {
    let trimmed = TRAILING_ARTICLE_RE.replace(value.trim(), "");
    let lowered = trimmed.to_lowercase().replace('&', " and ");
    let words: String = PUNCT_RE
        .replace_all(&lowered, " ")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    ARTICLE_RE.replace(&words, "").trim().to_owned()
}

fn title_case_article(s: &str) -> String {
    let mut chars = s.chars();
    match chars.next() {
        None => String::new(),
        Some(first) => first.to_uppercase().collect::<String>() + &chars.as_str().to_lowercase(),
    }
}

pub fn title_query_variants(value: &str) -> Vec<String> {
    let text = value.trim();
    let mut variants: Vec<String> = Vec::new();
    let mut add = |cand: String| {
        let t = cand.trim().to_owned();
        if !t.is_empty() && !variants.iter().any(|v| v.eq_ignore_ascii_case(&t)) {
            variants.push(t);
        }
    };
    add(text.to_owned());
    if let Some(caps) = LEADING_ARTICLE_RE.captures(text) {
        let art = title_case_article(&caps[1]);
        let rest = caps[2].trim();
        add(rest.to_owned());
        add(format!("{rest}, {art}"));
    }
    if let Some(m) = TRAILING_ARTICLE_RE.find(text)
        && let Some(caps) = TRAILING_ARTICLE_RE.captures(text)
    {
        let base = text[..m.start()].trim();
        let art = title_case_article(&caps[1]);
        add(base.to_owned());
        add(format!("{art} {base}"));
    }
    variants
}

pub fn normalize_platform(value: &str) -> String {
    let lowered = value.to_lowercase();
    PUNCT_RE
        .replace_all(&lowered, " ")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

pub fn platform_equivalent(left: &str, right: &str) -> bool {
    if left.is_empty() || right.is_empty() {
        return false;
    }
    if normalize_platform(left) == normalize_platform(right) {
        return true;
    }
    normalize_platform(platform_alias(left)) == normalize_platform(right)
}

pub fn title_similarity(left: &str, right: &str) -> f64 {
    let a = normalize_title(left);
    let b = normalize_title(right);
    if a.is_empty() || b.is_empty() {
        return 0.0;
    }
    if a == b {
        return 1.0;
    }
    sequence_matcher_ratio(&a, &b)
}

pub fn match_rank(title: &str, candidate_name: &str) -> u8 {
    let q = normalize_title(title);
    let c = normalize_title(candidate_name);
    if q.is_empty() || c.is_empty() {
        return 3;
    }
    if q == c {
        return 0;
    }
    if c.starts_with(&q) || q.starts_with(&c) {
        return 1;
    }
    if c.contains(&q) || q.contains(&c) {
        return 2;
    }
    3
}

pub fn rank_candidates(
    mut candidates: Vec<Value>,
    title: &str,
    platform: &str,
    limit: usize,
) -> Vec<Value> {
    candidates.sort_by(|a, b| {
        let an = a.get("name").and_then(Value::as_str).unwrap_or("");
        let bn = b.get("name").and_then(Value::as_str).unwrap_or("");
        let ap = a.get("platform").and_then(Value::as_str).unwrap_or("");
        let bp = b.get("platform").and_then(Value::as_str).unwrap_or("");
        let ra = match_rank(title, an);
        let rb = match_rank(title, bn);
        let pa = if !platform.is_empty() && platform_equivalent(platform, ap) {
            0u8
        } else {
            1
        };
        let pb = if !platform.is_empty() && platform_equivalent(platform, bp) {
            0u8
        } else {
            1
        };
        let sa = title_similarity(title, an);
        let sb = title_similarity(title, bn);
        ra.cmp(&rb)
            .then_with(|| pa.cmp(&pb))
            .then_with(|| sb.total_cmp(&sa))
            .then_with(|| an.len().cmp(&bn.len()))
    });
    candidates.truncate(limit);
    candidates
}

pub fn clean_media_url(url: &str) -> String {
    if url.len() > MAX_URL_LENGTH {
        return String::new();
    }
    ss_clean_media_url(url)
}

fn build_candidate(
    source: &str,
    id: &str,
    name: &str,
    platform: &str,
    year: &str,
    fields: &Map<String, Value>,
    media: &Map<String, Value>,
) -> Value {
    let mut clean_fields = Map::new();
    for (k, v) in fields {
        if APPLY_FIELDS.contains(&k.as_str()) {
            let empty = match v {
                Value::Null => true,
                Value::String(s) => s.is_empty(),
                Value::Array(a) => a.is_empty(),
                _ => false,
            };
            if !empty {
                clean_fields.insert(k.clone(), v.clone());
            }
        }
    }
    let mut clean_media = Map::new();
    for (kind, urls) in media {
        let arr: Vec<String> = match urls {
            Value::Array(a) => a
                .iter()
                .filter_map(Value::as_str)
                .map(ToOwned::to_owned)
                .collect(),
            Value::String(s) => vec![s.clone()],
            _ => Vec::new(),
        };
        let mut kept: Vec<Value> = Vec::new();
        for u in arr {
            let clean = clean_media_url(&u);
            if !clean.is_empty() && !kept.iter().any(|x| x.as_str() == Some(clean.as_str())) {
                kept.push(Value::String(clean));
            }
            if kept.len() >= MAX_MEDIA_URLS_PER_KIND {
                break;
            }
        }
        if !kept.is_empty() {
            clean_media.insert(kind.clone(), Value::Array(kept));
        }
    }
    json!({
        "id": id,
        "source": source,
        "name": name,
        "platform": platform,
        "year": year,
        "fields": clean_fields,
        "media": clean_media,
    })
}

pub fn provider_priority(state: &Value) -> Vec<String> {
    let settings = state.get("settings").and_then(Value::as_object);
    let mut priority: Vec<String> = Vec::new();
    if let Some(configured) = settings
        .and_then(|s| s.get("metadata_provider_priority"))
        .and_then(Value::as_array)
    {
        for item in configured.iter().take(20) {
            let key = item.as_str().unwrap_or("").trim();
            if key.is_empty() || priority.iter().any(|p| p == key) {
                continue;
            }
            if BUILTIN_PROVIDER_ORDER.contains(&key) || key.starts_with("plugin:") {
                priority.push(key.to_owned());
            }
        }
    }
    for key in BUILTIN_PROVIDER_ORDER {
        if !priority.iter().any(|p| p == *key) {
            priority.push((*key).to_owned());
        }
    }
    if settings
        .and_then(|s| s.get("plugin_metadata_priority"))
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        let (plugins, rest): (Vec<_>, Vec<_>) =
            priority.into_iter().partition(|k| k.starts_with("plugin:"));
        priority = plugins.into_iter().chain(rest).collect();
    }
    priority
}

fn search_launchbox(database_path: &Path, game: &Value, limit: usize) -> Vec<Value> {
    if !database_path.is_file() {
        return Vec::new();
    }
    let name = game.get("name").and_then(Value::as_str).unwrap_or("");
    let platform = game.get("platform").and_then(Value::as_str).unwrap_or("");
    let cap = (limit * 2).clamp(1, MAX_CANDIDATES);
    let mut rows = Vec::new();
    let mut seen: HashSet<i64> = HashSet::new();
    for query in title_query_variants(name) {
        let Ok(hits) = lbdb_search(database_path, &query, platform, cap) else {
            continue;
        };
        for row in hits {
            let id = row.get("database_id").and_then(Value::as_i64).unwrap_or(0);
            if !seen.insert(id) {
                continue;
            }
            rows.push(row);
            if rows.len() >= cap {
                break;
            }
        }
        if rows.len() >= cap {
            break;
        }
    }
    let mut reverse: HashMap<&str, &str> = HashMap::new();
    for (kind, types) in media_type_map() {
        for t in *types {
            reverse.entry(*t).or_insert(*kind);
        }
    }
    reverse.entry("Box - Front").or_insert("poster");
    reverse.entry("Icon").or_insert("square");

    let mut out = Vec::new();
    for row in rows {
        let db_id = row.get("database_id").and_then(Value::as_i64).unwrap_or(0);
        let mut media = Map::new();
        if let Ok(images) = load_media_images(database_path, db_id, None) {
            for img in images {
                let itype = img.get("type").and_then(Value::as_str).unwrap_or("");
                if let Some(&kind) = reverse.get(itype) {
                    let fname = img.get("filename").and_then(Value::as_str).unwrap_or("");
                    if !fname.is_empty() {
                        let url = format!("{IMAGE_URL}{fname}");
                        media
                            .entry(kind.to_owned())
                            .or_insert_with(|| Value::Array(Vec::new()))
                            .as_array_mut()
                            .unwrap()
                            .push(Value::String(url));
                    }
                }
            }
        }
        let year = year_from_record(&row).unwrap_or_default();
        let mut fields = Map::new();
        for (dst, src) in [
            ("name", "name"),
            ("platform", "platform"),
            ("developer", "developer"),
            ("publisher", "publisher"),
            ("genre", "genre"),
            ("description", "overview"),
            ("series", "series"),
            ("esrb", "esrb"),
            ("max_players", "max_players"),
        ] {
            if let Some(v) = row
                .get(src)
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
            {
                fields.insert(dst.to_owned(), Value::String(v.to_owned()));
            }
        }
        if !year.is_empty() {
            fields.insert("year".into(), Value::String(year.clone()));
        }
        out.push(build_candidate(
            "launchbox",
            &db_id.to_string(),
            row.get("name").and_then(Value::as_str).unwrap_or(""),
            row.get("platform").and_then(Value::as_str).unwrap_or(""),
            &year,
            &fields,
            &media,
        ));
    }
    out
}

pub fn search_all(
    state: &Value,
    database_path: &Path,
    game: &Value,
    providers: Option<&[String]>,
    limit: usize,
) -> Vec<Value> {
    let cap = limit.clamp(1, MAX_CANDIDATES);
    let requested: Vec<String> = match providers {
        Some(p) => p.to_vec(),
        None => provider_priority(state),
    };
    let name = game.get("name").and_then(Value::as_str).unwrap_or("");
    let platform = game.get("platform").and_then(Value::as_str).unwrap_or("");
    let mut collected = Vec::new();

    for key in &requested {
        match key.as_str() {
            "launchbox" => {
                collected.extend(search_launchbox(database_path, game, cap));
            }
            "screenscraper" => {
                if screenscraper::is_configured() {
                    let sid = screenscraper::system_id_for_platform(platform);
                    if let Ok(rows) = screenscraper::search_games(name, sid, cap) {
                        for row in rows {
                            let id = row
                                .get("id")
                                .map(|v| match v {
                                    Value::String(s) => s.clone(),
                                    Value::Null => String::new(),
                                    other => other.to_string(),
                                })
                                .unwrap_or_default();
                            let rname = row.get("name").and_then(Value::as_str).unwrap_or("");
                            let rplat = row
                                .get("system_name")
                                .and_then(Value::as_str)
                                .filter(|s| !s.is_empty())
                                .unwrap_or(platform);
                            let ryear = row.get("year").and_then(Value::as_str).unwrap_or("");
                            let mut fields = Map::new();
                            fields.insert("name".into(), Value::String(rname.to_owned()));
                            fields.insert("platform".into(), Value::String(rplat.to_owned()));
                            fields.insert("year".into(), Value::String(ryear.to_owned()));
                            collected.push(build_candidate(
                                "screenscraper",
                                &id,
                                rname,
                                rplat,
                                ryear,
                                &fields,
                                &Map::new(),
                            ));
                        }
                    }
                }
            }
            "igdb" => {
                if igdb::is_configured()
                    && let Ok(rows) = igdb::search_games(name, platform, cap)
                {
                    for row in rows {
                        let id = row
                            .get("id")
                            .map(|v| match v {
                                Value::String(s) => s.clone(),
                                Value::Null => String::new(),
                                other => other.to_string(),
                            })
                            .unwrap_or_default();
                        let rname = row.get("name").and_then(Value::as_str).unwrap_or("");
                        let ryear = row.get("year").and_then(Value::as_str).unwrap_or("");
                        let mut fields = Map::new();
                        fields.insert("name".into(), Value::String(rname.to_owned()));
                        fields.insert(
                            "description".into(),
                            row.get("summary").cloned().unwrap_or(Value::Null),
                        );
                        fields.insert("year".into(), Value::String(ryear.to_owned()));
                        fields.insert(
                            "genre".into(),
                            row.get("genres").cloned().unwrap_or(Value::Null),
                        );
                        collected.push(build_candidate(
                            "igdb",
                            &id,
                            rname,
                            platform,
                            ryear,
                            &fields,
                            &Map::new(),
                        ));
                    }
                }
            }
            "steamgriddb" => {
                if steamgrid::is_configured()
                    && let Ok(rows) = steamgrid::search_games(name, cap, None)
                {
                    for row in rows {
                        let id = row
                            .get("id")
                            .map(|v| match v {
                                Value::String(s) => s.clone(),
                                Value::Null => String::new(),
                                other => other.to_string(),
                            })
                            .unwrap_or_default();
                        let rname = row.get("name").and_then(Value::as_str).unwrap_or("");
                        let ryear = row.get("year").and_then(Value::as_str).unwrap_or("");
                        let mut fields = Map::new();
                        fields.insert("name".into(), Value::String(rname.to_owned()));
                        fields.insert("year".into(), Value::String(ryear.to_owned()));
                        collected.push(build_candidate(
                            "steamgriddb",
                            &id,
                            rname,
                            platform,
                            ryear,
                            &fields,
                            &Map::new(),
                        ));
                    }
                }
            }
            _ => {}
        }
    }
    rank_candidates(collected, name, platform, cap)
}

pub fn apply_candidate(
    game: &mut Map<String, Value>,
    candidate: &Value,
    overwrite: bool,
    local_media: Option<&Map<String, Value>>,
) -> Result<(), String> {
    let cand = candidate
        .as_object()
        .ok_or_else(|| "Invalid metadata candidate.".to_owned())?;
    let source = cand
        .get("source")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if BUILTIN_PROVIDER_ORDER.contains(&source) || source.starts_with("plugin:") {
        game.insert("metadata_provider".into(), Value::String(source.to_owned()));
    }
    let fields_map = cand
        .get("fields")
        .and_then(Value::as_object)
        .unwrap_or(cand);
    for key in APPLY_FIELDS {
        let Some(val) = fields_map.get(*key) else {
            continue;
        };
        let empty = match val {
            Value::Null => true,
            Value::String(s) => s.is_empty(),
            Value::Array(a) => a.is_empty(),
            _ => false,
        };
        if empty {
            continue;
        }
        let existing_empty = game
            .get(*key)
            .map(|v| match v {
                Value::Null => true,
                Value::String(s) => s.is_empty(),
                Value::Array(a) => a.is_empty(),
                _ => false,
            })
            .unwrap_or(true);
        if overwrite || existing_empty {
            game.insert((*key).to_owned(), val.clone());
        }
    }
    if let Some(lm) = local_media {
        for (kind, path_val) in lm {
            if kind == "video_recording" {
                continue;
            }
            let values: Vec<String> = match path_val {
                Value::Array(a) => a
                    .iter()
                    .filter_map(Value::as_str)
                    .map(|s| s.trim().to_owned())
                    .filter(|s| !s.is_empty())
                    .collect(),
                Value::String(s) if !s.trim().is_empty() => vec![s.trim().to_owned()],
                _ => Vec::new(),
            };
            if values.is_empty() {
                continue;
            }
            if kind == "screenshots" {
                let entry = game
                    .entry("screenshots".to_owned())
                    .or_insert_with(|| Value::Array(Vec::new()));
                if !entry.is_array() {
                    *entry = Value::Array(Vec::new());
                }
                if let Some(arr) = entry.as_array_mut() {
                    for v in values {
                        if !arr.iter().any(|x| x.as_str() == Some(v.as_str())) {
                            arr.push(Value::String(v));
                        }
                    }
                }
                continue;
            }
            if !MEDIA_PATH_FIELDS.contains(&kind.as_str()) {
                continue;
            }
            if overwrite
                || game
                    .get(kind)
                    .and_then(Value::as_str)
                    .is_none_or(str::is_empty)
            {
                game.insert(kind.clone(), Value::String(values[0].clone()));
            }
        }
    }
    Ok(())
}

fn download_limits(kind: &str) -> (usize, &'static [&'static str]) {
    match kind {
        "video" | "video_snap" | "video_theme" | "video_trailer" | "video_recording" => {
            (512 * 1024 * 1024, &["video/"])
        }
        "music" => (32 * 1024 * 1024, &["audio/", "application/ogg"]),
        "manual" => (64 * 1024 * 1024, &["application/pdf"]),
        _ => (64 * 1024 * 1024, &["image/"]),
    }
}

pub fn download_candidate_media(
    url: &str,
    kind: &str,
    destination_root: &Path,
    filename: Option<&str>,
) -> Result<String, String> {
    let safe = clean_media_url(url);
    if safe.is_empty() {
        return Err("Only public HTTPS media URLs are supported.".to_owned());
    }
    let default_name = {
        let lower = kind.to_lowercase();
        let slug = KIND_SLUG_RE.replace_all(&lower, "-");
        let trimmed = slug.trim_matches('-');
        format!("{}.bin", if trimmed.is_empty() { "media" } else { trimmed })
    };
    let bare = Path::new(filename.unwrap_or(&default_name))
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or(default_name);
    let dest = contained_path(destination_root, &[&bare]).map_err(|e| e.to_string())?;
    let (max_bytes, expected) = download_limits(kind);
    download_file_sync(&safe, &dest, expected, max_bytes, 30, &[])?;
    Ok(dest.to_string_lossy().into_owned())
}
