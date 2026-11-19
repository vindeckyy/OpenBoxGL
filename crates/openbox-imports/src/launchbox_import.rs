//! Pure LaunchBox XML migration parser, planner, and applier
//! (`pkg/parity/parity_launchbox_import.py`).

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fs;
use std::path::Path;
use std::sync::LazyLock;

use openbox_core::ApiError;
use openbox_platform::{expand_user, resolve_non_strict};
use quick_xml::Reader;
use quick_xml::events::Event;
use regex::Regex;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

pub const MAX_XML_BYTES: usize = 64 * 1024 * 1024;

static YEAR_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(\d{4})").expect("valid year regex"));
static WIN_PREFIX_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^(?:[A-Za-z]:/|/|//)").expect("valid win prefix regex"));

const USER_FIELDS: &[&str] = &[
    "game_id",
    "favorite",
    "hidden",
    "broken",
    "portable",
    "installed",
    "progress",
    "play_count",
    "playtime_seconds",
    "last_played",
    "rating",
    "notes",
    "description",
    "tags",
    "playlists",
    "added_at",
    "launch",
    "emulator_id",
    "emulator_adapter_id",
    "emulator_profile_id",
];

const MEDIA_FIELDS: &[&str] = &[
    "cover",
    "background",
    "clear_logo",
    "fanart",
    "banner",
    "icon",
    "manual",
    "music",
    "video",
    "video_snap",
    "video_theme",
    "video_trailer",
    "video_recording",
    "title_screen",
    "cart_front",
    "cart_back",
    "disc",
    "advertisement",
    "box_back",
    "box_spine",
    "box_3d",
    "screenshots",
];

fn field_map(lb_field: &str) -> Option<&'static str> {
    match lb_field {
        "Title" => Some("name"),
        "ApplicationPath" => Some("path"),
        "Platform" => Some("platform"),
        "Genre" => Some("genre"),
        "Developer" => Some("developer"),
        "Publisher" => Some("publisher"),
        "PlayMode" => Some("play_mode"),
        "Region" => Some("region"),
        "Status" => Some("status"),
        "Notes" => Some("description"),
        "Rating" => Some("rating"),
        "ManualPath" => Some("manual"),
        "MusicPath" => Some("music"),
        "VideoPath" => Some("video_snap"),
        "ImagePath" | "BoxFrontImagePath" => Some("cover"),
        "ScreenshotPath" | "ScreenshotsPath" => Some("screenshots"),
        _ => None,
    }
}

fn local_tag(raw: &str) -> String {
    raw.rsplit(['}', ':']).next().unwrap_or(raw).to_owned()
}

fn parse_year(value: &str) -> String {
    for cap in YEAR_RE.captures_iter(value) {
        if let Some(m) = cap.get(1) {
            let start = m.start();
            let end = m.end();
            let prev_digit = start > 0 && value.as_bytes()[start - 1].is_ascii_digit();
            let next_digit = end < value.len() && value.as_bytes()[end].is_ascii_digit();
            if !prev_digit
                && !next_digit
                && let Ok(num) = m.as_str().parse::<u32>()
                && (1000..=9999).contains(&num)
            {
                return num.to_string();
            }
        }
    }
    String::new()
}

fn canonical_json_digest(val: &Value) -> String {
    fn sort_val(v: &Value) -> Value {
        match v {
            Value::Object(m) => {
                let btree: BTreeMap<&String, &Value> = m.iter().collect();
                let mut out = Map::new();
                for (k, v) in btree {
                    out.insert(k.clone(), sort_val(v));
                }
                Value::Object(out)
            }
            Value::Array(arr) => Value::Array(arr.iter().map(sort_val).collect()),
            other => other.clone(),
        }
    }
    let sorted = sort_val(val);
    let bytes = serde_json::to_vec(&sorted).unwrap_or_default();
    hex::encode(Sha256::digest(&bytes))
}

pub fn parse_launchbox_xml_bytes(data: &[u8], label: &str) -> Result<Value, ApiError> {
    if data.len() > MAX_XML_BYTES {
        return Err(ApiError::bad_request(format!(
            "LaunchBox XML is too large ({} bytes; limit is {MAX_XML_BYTES} bytes).",
            data.len()
        )));
    }
    let digest = hex::encode(Sha256::digest(data));
    let mut reader = Reader::from_reader(data);
    reader.config_mut().trim_text(false);
    let mut buf = Vec::new();
    let mut root_seen = false;
    let mut depth = 0usize;
    let mut in_game = false;
    let mut game_depth = 0usize;
    let mut cur_field: Option<String> = None;
    let mut cur_text = String::new();
    let mut cur_entry = Map::new();
    let mut cur_screenshots: Vec<String> = Vec::new();
    let mut cur_unsupported: Vec<String> = Vec::new();

    let mut games: Vec<Value> = Vec::new();
    let mut skipped = 0usize;
    let mut emulator_ids: BTreeSet<String> = BTreeSet::new();
    let mut unsupported_fields: BTreeSet<String> = BTreeSet::new();
    let errors: Vec<String> = Vec::new();

    loop {
        match reader.read_event_into(&mut buf) {
            Ok(Event::Start(e)) => {
                let tag = local_tag(e.name().as_ref());
                if !root_seen {
                    root_seen = true;
                    if tag.to_lowercase() != "launchbox" {
                        return Err(ApiError::bad_request(format!(
                            "Could not parse LaunchBox XML {label}: root element must be LaunchBox."
                        )));
                    }
                } else if !in_game && tag.to_lowercase() == "game" {
                    in_game = true;
                    game_depth = depth;
                    cur_entry.clear();
                    cur_screenshots.clear();
                    cur_unsupported.clear();
                } else if in_game && depth == game_depth + 1 {
                    cur_field = Some(tag);
                    cur_text.clear();
                }
                depth += 1;
            }
            Ok(Event::Empty(e)) => {
                let tag = local_tag(e.name().as_ref());
                if !root_seen {
                    root_seen = true;
                    if tag.to_lowercase() != "launchbox" {
                        return Err(ApiError::bad_request(format!(
                            "Could not parse LaunchBox XML {label}: root element must be LaunchBox."
                        )));
                    }
                }
            }
            Ok(Event::Text(t)) => {
                if cur_field.is_some() {
                    let s = quick_xml::escape::unescape(t.as_ref())
                        .unwrap_or_else(|_| std::borrow::Cow::Borrowed(t.as_ref()));
                    cur_text.push_str(&s);
                }
            }
            Ok(Event::CData(c)) => {
                if cur_field.is_some() {
                    cur_text.push_str(c.as_ref());
                }
            }
            Ok(Event::End(e)) => {
                depth = depth.saturating_sub(1);
                let tag = local_tag(e.name().as_ref());
                if in_game && depth == game_depth + 1 {
                    if let Some(lb_field) = cur_field.take() {
                        let val = cur_text.trim();
                        if !val.is_empty() {
                            match lb_field.as_str() {
                                "ID" => {
                                    cur_entry.insert(
                                        "launchbox_source_id".into(),
                                        Value::String(val.to_owned()),
                                    );
                                }
                                "DatabaseID" => {
                                    if val.chars().all(|c| c.is_ascii_digit()) {
                                        cur_entry.insert(
                                            "launchbox_db_id".into(),
                                            Value::String(val.to_owned()),
                                        );
                                    }
                                }
                                "ReleaseDate" | "Year" => {
                                    let y = parse_year(val);
                                    if !y.is_empty() {
                                        cur_entry.insert("year".into(), Value::String(y));
                                    }
                                }
                                "EmulatorId" => {
                                    cur_entry.insert(
                                        "launchbox_emulator_id".into(),
                                        Value::String(val.to_owned()),
                                    );
                                }
                                other => match field_map(other) {
                                    None => cur_unsupported.push(other.to_owned()),
                                    Some("screenshots") => cur_screenshots.push(val.to_owned()),
                                    Some("rating") => {
                                        if let Ok(r) = val.parse::<f64>() {
                                            cur_entry.insert("rating".into(), json!(r));
                                        }
                                    }
                                    Some(ob) => {
                                        cur_entry
                                            .insert(ob.to_owned(), Value::String(val.to_owned()));
                                    }
                                },
                            }
                        }
                    }
                } else if in_game && depth == game_depth && tag.to_lowercase() == "game" {
                    in_game = false;
                    if !cur_screenshots.is_empty() {
                        cur_entry.insert(
                            "screenshots".into(),
                            Value::Array(cur_screenshots.drain(..).map(Value::String).collect()),
                        );
                    }
                    let has_name = cur_entry
                        .get("name")
                        .and_then(Value::as_str)
                        .is_some_and(|s| !s.is_empty());
                    let has_id = cur_entry
                        .get("launchbox_source_id")
                        .and_then(Value::as_str)
                        .is_some_and(|s| !s.is_empty());
                    if !has_name || !has_id {
                        skipped += 1;
                    } else {
                        if let Some(eid) = cur_entry
                            .get("launchbox_emulator_id")
                            .and_then(Value::as_str)
                            && !eid.is_empty()
                        {
                            emulator_ids.insert(eid.to_owned());
                        }
                        unsupported_fields.extend(cur_unsupported.drain(..));
                        games.push(Value::Object(std::mem::take(&mut cur_entry)));
                    }
                }
            }
            Ok(Event::Eof) => break,
            Err(err) => {
                return Err(ApiError::bad_request(format!(
                    "Could not parse LaunchBox XML {label}: {err}"
                )));
            }
            _ => {}
        }
        buf.clear();
    }
    if !root_seen {
        return Err(ApiError::bad_request(format!(
            "Could not parse LaunchBox XML {label}: empty document."
        )));
    }
    Ok(json!({
        "games": games,
        "skipped": skipped,
        "emulator_ids": emulator_ids.into_iter().collect::<Vec<_>>(),
        "errors": errors,
        "unsupported_fields": unsupported_fields.into_iter().collect::<Vec<_>>(),
        "source_digest": digest,
        "source_bytes": data.len(),
        "source_label": label,
    }))
}

pub fn parse_launchbox_xml(path: &Path) -> Result<Value, ApiError> {
    let expanded = expand_user(path);
    let meta = fs::metadata(&expanded).map_err(|e| {
        ApiError::bad_request(format!(
            "Could not read LaunchBox XML {}: {e}",
            expanded.display()
        ))
    })?;
    if meta.len() as usize > MAX_XML_BYTES {
        return Err(ApiError::bad_request(format!(
            "LaunchBox XML is too large ({} bytes; limit is {MAX_XML_BYTES} bytes).",
            meta.len()
        )));
    }
    let data = fs::read(&expanded).map_err(|e| {
        ApiError::bad_request(format!(
            "Could not read LaunchBox XML {}: {e}",
            expanded.display()
        ))
    })?;
    parse_launchbox_xml_bytes(&data, &expanded.to_string_lossy())
}

fn path_key(raw: &str) -> String {
    let text = raw.trim().replace('\\', "/");
    if text.is_empty() {
        return String::new();
    }
    let windows =
        (text.len() >= 3 && text.as_bytes()[0].is_ascii_alphabetic() && &text[1..3] == ":/")
            || text.starts_with("//");
    let norm = resolve_non_strict(Path::new(&text))
        .to_string_lossy()
        .replace('\\', "/");
    if windows { norm.to_lowercase() } else { norm }
}

fn mapping_entries(options: &Value) -> Vec<(String, String)> {
    let Some(obj) = options.as_object() else {
        return Vec::new();
    };
    let raw = [
        "path_mappings",
        "path_mapping",
        "path_map",
        "path_root_mappings",
        "root_map",
        "path_root_map",
        "windows_root_map",
    ]
    .iter()
    .find_map(|k| obj.get(*k));
    let mut entries = Vec::new();
    match raw {
        Some(Value::Object(m)) => {
            for (src, dst) in m {
                if let Some(d) = dst.as_str()
                    && !src.trim().is_empty()
                    && !d.trim().is_empty()
                {
                    entries.push((src.trim().to_owned(), d.trim().to_owned()));
                }
            }
        }
        Some(Value::Array(arr)) => {
            for item in arr {
                let Some(m) = item.as_object() else {
                    continue;
                };
                let src = ["source", "source_root", "windows_root", "from"]
                    .iter()
                    .find_map(|k| m.get(*k).and_then(Value::as_str))
                    .unwrap_or("")
                    .trim();
                let dst = ["destination", "destination_root", "local_root", "to"]
                    .iter()
                    .find_map(|k| m.get(*k).and_then(Value::as_str))
                    .unwrap_or("")
                    .trim();
                if !src.is_empty() && !dst.is_empty() {
                    entries.push((src.to_owned(), dst.to_owned()));
                }
            }
        }
        _ => {}
    }
    if let (Some(s), Some(d)) = (
        obj.get("source_root").and_then(Value::as_str),
        obj.get("destination_root").and_then(Value::as_str),
    ) {
        entries.push((s.to_owned(), d.to_owned()));
    }
    if let Some(rel) = obj.get("relative_root").and_then(Value::as_str)
        && !rel.is_empty()
    {
        entries.push((".".to_owned(), rel.to_owned()));
    }
    entries
}

fn map_path(raw: &str, options: &Value) -> String {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return String::new();
    }
    let candidate = trimmed.replace('\\', "/");
    let cand_key = path_key(&candidate);
    let mut entries = mapping_entries(options);
    entries.sort_by_key(|(s, _)| std::cmp::Reverse(s.len()));
    for (source, destination) in entries {
        let src_norm = {
            let s = source.replace('\\', "/");
            let t = s.trim_end_matches('/');
            if t.is_empty() { "." } else { t }.to_owned()
        };
        let src_key = path_key(&src_norm);
        let suffix = if src_norm == "." && !WIN_PREFIX_RE.is_match(&candidate) {
            candidate.trim_start_matches(['.', '/']).to_owned()
        } else if cand_key == src_key {
            String::new()
        } else if cand_key.starts_with(&format!("{}/", src_key.trim_end_matches('/'))) {
            let prefix = src_norm.trim_end_matches('/');
            if candidate.len() >= prefix.len()
                && candidate[..prefix.len()].to_lowercase() == prefix.to_lowercase()
            {
                candidate[prefix.len()..].trim_start_matches('/').to_owned()
            } else {
                cand_key[src_key.trim_end_matches('/').len()..]
                    .trim_start_matches('/')
                    .to_owned()
            }
        } else {
            continue;
        };
        let dest_norm = expand_user(Path::new(destination.trim()))
            .to_string_lossy()
            .replace('\\', "/")
            .trim_end_matches('/')
            .to_owned();
        return if suffix.is_empty() {
            dest_norm
        } else {
            format!("{dest_norm}/{suffix}")
        };
    }
    candidate
}

fn normalize_game(parsed: &Value, options: &Value) -> Value {
    let mut game = parsed.as_object().cloned().unwrap_or_default();
    for field in std::iter::once(&"path").chain(MEDIA_FIELDS.iter()) {
        if *field == "screenshots" {
            continue;
        }
        if let Some(s) = game
            .get(*field)
            .and_then(Value::as_str)
            .map(ToOwned::to_owned)
        {
            game.insert((*field).to_owned(), Value::String(map_path(&s, options)));
        }
    }
    if let Some(shots) = game.get("screenshots").and_then(Value::as_array) {
        let mapped: Vec<Value> = shots
            .iter()
            .filter_map(Value::as_str)
            .filter(|s| !s.trim().is_empty())
            .map(|s| Value::String(map_path(s, options)))
            .collect();
        game.insert("screenshots".into(), Value::Array(mapped));
    }
    let source_id = game
        .get("launchbox_source_id")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_owned();
    game.insert(
        "launchbox_source_id".into(),
        Value::String(source_id.clone()),
    );
    game.insert(
        "source_identity".into(),
        Value::String(format!("launchbox:{source_id}")),
    );
    game.insert(
        "source_identities".into(),
        json!([format!("launchbox:{source_id}")]),
    );
    if let Some(eid) = game
        .get("launchbox_emulator_id")
        .and_then(Value::as_str)
        .map(ToOwned::to_owned)
        && let Some(opts) = options.as_object()
    {
        let raw = [
            "emulator_profile_map",
            "emulator_profiles",
            "emulator_mappings",
            "emulator_mapping",
            "emulator_map",
            "emulators",
        ]
        .iter()
        .find_map(|k| opts.get(*k).and_then(Value::as_object));
        if let Some(map) = raw
            && let Some(val) = map.get(&eid)
        {
            match val {
                Value::String(s) if !s.trim().is_empty() => {
                    game.insert(
                        "emulator_adapter_id".into(),
                        Value::String(s.trim().to_owned()),
                    );
                }
                Value::Object(m) => {
                    for k in [
                        "emulator_id",
                        "emulator_adapter_id",
                        "emulator_profile_id",
                        "profile_id",
                    ] {
                        if let Some(s) = m
                            .get(k)
                            .and_then(Value::as_str)
                            .filter(|s| !s.trim().is_empty())
                        {
                            let target_k = if k == "profile_id" {
                                "emulator_profile_id"
                            } else {
                                k
                            };
                            game.insert(target_k.to_owned(), Value::String(s.trim().to_owned()));
                        }
                    }
                }
                _ => {}
            }
        }
    }
    Value::Object(game)
}

fn overwrite_fields(options: &Value) -> HashSet<String> {
    let Some(opts) = options.as_object() else {
        return HashSet::new();
    };
    let val = opts
        .get("overwrite_fields")
        .or_else(|| opts.get("overwrite_user_fields"))
        .or_else(|| opts.get("replace_fields"))
        .or_else(|| opts.get("overwrite"));
    match val {
        Some(Value::Bool(true)) => [
            "name",
            "path",
            "platform",
            "genre",
            "developer",
            "publisher",
            "play_mode",
            "region",
            "status",
            "description",
            "rating",
            "manual",
            "music",
            "video_snap",
            "cover",
            "screenshots",
            "year",
        ]
        .iter()
        .map(|s| (*s).to_owned())
        .collect(),
        Some(Value::String(s)) => [s.clone()].into_iter().collect(),
        Some(Value::Array(arr)) => arr
            .iter()
            .filter_map(Value::as_str)
            .map(ToOwned::to_owned)
            .collect(),
        _ => HashSet::new(),
    }
}

fn is_empty_val(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => true,
        Some(Value::String(s)) => s.is_empty(),
        Some(Value::Array(a)) => a.is_empty(),
        Some(Value::Object(m)) => m.is_empty(),
        _ => false,
    }
}

pub fn build_import_plan(parsed: &Value, state: &Value, options: &Value) -> Value {
    let existing_games = state
        .get("games")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let settings = state.get("settings").cloned().unwrap_or_else(|| json!({}));
    let source_games = parsed
        .get("games")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let source_digest = parsed
        .get("source_digest")
        .and_then(Value::as_str)
        .map(ToOwned::to_owned)
        .unwrap_or_else(|| canonical_json_digest(&Value::Array(source_games.clone())));
    let base_digest = canonical_json_digest(&Value::Array(existing_games.clone()));
    let options_digest = canonical_json_digest(options);

    let mut source_by_id: HashMap<String, usize> = HashMap::new();
    let mut source_by_ident: HashMap<String, usize> = HashMap::new();
    let mut path_by_key: HashMap<String, usize> = HashMap::new();
    for (idx, g) in existing_games.iter().enumerate() {
        let sid = g
            .get("launchbox_source_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_owned();
        if !sid.is_empty() {
            source_by_id.entry(sid.clone()).or_insert(idx);
            source_by_ident
                .entry(format!("launchbox:{sid}"))
                .or_insert(idx);
        } else {
            let ident = g
                .get("source_identity")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim();
            if ident.starts_with("launchbox:") {
                source_by_ident.entry(ident.to_owned()).or_insert(idx);
            }
            if let Some(p) = g.get("path").and_then(Value::as_str) {
                let pk = path_key(p);
                if !pk.is_empty() {
                    path_by_key.entry(pk).or_insert(idx);
                }
            }
        }
    }

    let exclusions = options
        .get("import_exclusions")
        .or_else(|| options.get("exclusions"))
        .or_else(|| settings.get("import_exclusions"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let overwrite = overwrite_fields(options);

    let mut operations = Vec::new();
    let mut normalized_rows = Vec::new();
    let mut seen_sources = HashSet::new();
    let mut excluded_c = 0usize;
    let mut dup_c = 0usize;
    let mut added_c = 0usize;
    let mut merged_c = 0usize;
    let mut skipped_c = 0usize;

    for row in &source_games {
        let imported = normalize_game(row, options);
        normalized_rows.push(imported.clone());
        let source_id = imported
            .get("launchbox_source_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        let is_excluded = exclusions.iter().any(|item| match item {
            Value::String(s) => s.trim() == source_id,
            Value::Array(a) if a.len() >= 2 => {
                let s = a[0].as_str().unwrap_or("").trim().to_lowercase();
                matches!(
                    s.as_str(),
                    "launchbox" | "launchbox_xml" | "launchbox_source"
                ) && a[1].as_str().unwrap_or("").trim() == source_id
            }
            Value::Object(m) => {
                let s = m
                    .get("source")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .trim()
                    .to_lowercase();
                let eid = m
                    .get("external_id")
                    .or_else(|| m.get("launchbox_source_id"))
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .trim();
                matches!(
                    s.as_str(),
                    "launchbox" | "launchbox_xml" | "launchbox_source"
                ) && eid == source_id
            }
            _ => false,
        });
        if is_excluded {
            excluded_c += 1;
            operations.push(json!({
                "action": "exclude",
                "reason": "import_exclusion",
                "source_id": source_id,
                "game": imported,
            }));
            continue;
        }
        if !seen_sources.insert(source_id.clone()) {
            dup_c += 1;
            skipped_c += 1;
            operations.push(json!({
                "action": "skip",
                "reason": "duplicate_source",
                "source_id": source_id,
                "game": imported,
            }));
            continue;
        }
        let target_idx = source_by_id
            .get(&source_id)
            .or_else(|| source_by_ident.get(&format!("launchbox:{source_id}")))
            .or_else(|| {
                imported
                    .get("path")
                    .and_then(Value::as_str)
                    .and_then(|p| path_by_key.get(&path_key(p)))
            })
            .copied();

        let Some(tidx) = target_idx else {
            added_c += 1;
            let mut affected: Vec<String> = imported
                .as_object()
                .map(|m| m.keys().cloned().collect())
                .unwrap_or_default();
            affected.sort();
            operations.push(json!({
                "action": "add",
                "source_id": source_id,
                "game": imported,
                "changes": imported,
                "affected_fields": affected,
            }));
            continue;
        };

        let target = &existing_games[tidx];
        let mut changes = Map::new();
        let mut review: BTreeSet<String> = BTreeSet::new();
        if let Some(imp_map) = imported.as_object() {
            for (field, val) in imp_map {
                if field == "launchbox_emulator_id" || is_empty_val(Some(val)) {
                    continue;
                }
                let old = target.get(field);
                if old == Some(val) {
                    continue;
                }
                let protected = USER_FIELDS.contains(&field.as_str())
                    || field == "path"
                    || MEDIA_FIELDS.contains(&field.as_str());
                if protected && !is_empty_val(old) && !overwrite.contains(field) {
                    review.insert(field.clone());
                    continue;
                }
                changes.insert(field.clone(), val.clone());
            }
            for f in [
                "launchbox_source_id",
                "source_identity",
                "source_identities",
            ] {
                if let Some(v) = imp_map.get(f)
                    && !is_empty_val(Some(v))
                    && target.get(f) != Some(v)
                {
                    changes.insert(f.to_owned(), v.clone());
                }
            }
        }
        merged_c += 1;
        let target_id = target
            .get("game_id")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .map(ToOwned::to_owned)
            .or_else(|| {
                target
                    .get("id")
                    .map(|v| v.to_string().trim_matches('"').to_owned())
            })
            .unwrap_or_default();
        let mut affected: Vec<String> = changes.keys().cloned().collect();
        affected.sort();
        operations.push(json!({
            "action": "merge",
            "source_id": source_id,
            "target_game_id": target_id,
            "target_index": tidx,
            "game": imported,
            "changes": Value::Object(changes),
            "review_fields": review.into_iter().collect::<Vec<_>>(),
            "affected_fields": affected,
        }));
    }

    let total_in_xml = source_games.len();
    let skipped_malformed = parsed.get("skipped").and_then(Value::as_u64).unwrap_or(0) as usize;
    let found = total_in_xml.saturating_sub(excluded_c);
    let counts = json!({
        "total_in_xml": total_in_xml,
        "skipped_malformed": skipped_malformed,
        "excluded": excluded_c,
        "duplicates": dup_c,
        "added": added_c,
        "merged": merged_c,
        "skipped": skipped_c,
        "found": found,
        "would_import": added_c,
        "would_change": merged_c,
    });
    let preview_token = canonical_json_digest(&json!({
        "source_digest": source_digest,
        "options_digest": options_digest,
        "base_digest": base_digest,
    }));
    let preview_games: Vec<Value> = operations
        .iter()
        .filter(|op| {
            matches!(
                op.get("action").and_then(Value::as_str),
                Some("add" | "merge")
            )
        })
        .filter_map(|op| op.get("game").cloned())
        .take(50)
        .collect();
    json!({
        "schema_version": 1,
        "source": {
            "digest": source_digest,
            "bytes": parsed.get("source_bytes").and_then(Value::as_u64).unwrap_or(0),
            "label": parsed.get("source_label").and_then(Value::as_str).unwrap_or("<parsed>"),
        },
        "source_digest": source_digest,
        "options": options,
        "options_digest": options_digest,
        "base_digest": base_digest,
        "preview_token": preview_token,
        "counts": counts,
        "operations": operations,
        "emulator_ids": parsed.get("emulator_ids").cloned().unwrap_or_else(|| json!([])),
        "errors": parsed.get("errors").cloned().unwrap_or_else(|| json!([])),
        "unsupported_fields": parsed.get("unsupported_fields").cloned().unwrap_or_else(|| json!([])),
        "preview_games": preview_games,
        "normalized_games": normalized_rows,
    })
}

pub fn preview_import(xml_path: &Path, state: &Value, options: &Value) -> Result<Value, ApiError> {
    let parsed = parse_launchbox_xml(xml_path)?;
    let plan = build_import_plan(&parsed, state, options);
    let mut report = plan
        .get("counts")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    for k in [
        "emulator_ids",
        "errors",
        "unsupported_fields",
        "preview_games",
        "preview_token",
        "source_digest",
        "base_digest",
        "options_digest",
    ] {
        if let Some(v) = plan.get(k) {
            report.insert(k.to_owned(), v.clone());
        }
    }
    report.insert("plan".into(), plan);
    Ok(Value::Object(report))
}

pub fn apply_import_plan(
    plan: &Value,
    state: &mut Value,
    preview_token: Option<&str>,
    source_digest: Option<&str>,
    source: Option<&Value>,
    options: &Value,
) -> Result<Value, ApiError> {
    let expected_token = plan
        .get("preview_token")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| ApiError::bad_request("A valid LaunchBox import plan is required."))?
        .to_owned();
    let effective_plan = if let Some(src) = source {
        let canonical = build_import_plan(src, state, options);
        if canonical.get("preview_token").and_then(Value::as_str) != Some(expected_token.as_str()) {
            return Err(ApiError::bad_request(
                "LaunchBox preview decisions changed; review them again.",
            ));
        }
        canonical
    } else {
        plan.clone()
    };
    if let Some(pt) = preview_token
        && pt != expected_token
    {
        return Err(ApiError::bad_request(
            "LaunchBox import preview token is stale.",
        ));
    }
    if let Some(sd) = source_digest
        && Some(sd) != effective_plan.get("source_digest").and_then(Value::as_str)
    {
        return Err(ApiError::bad_request(
            "LaunchBox XML changed since the preview; review it again.",
        ));
    }
    let current_games = state
        .get("games")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let cur_digest = canonical_json_digest(&Value::Array(current_games.clone()));
    if Some(cur_digest.as_str()) != effective_plan.get("base_digest").and_then(Value::as_str) {
        return Err(ApiError::bad_request(
            "OpenBox library changed since the LaunchBox preview; review it again.",
        ));
    }
    let mut result_games = current_games;
    let mut added = 0usize;
    let mut merged = 0usize;
    let mut skipped = 0usize;
    let mut excluded = 0usize;
    for op in effective_plan
        .get("operations")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        match op.get("action").and_then(Value::as_str) {
            Some("add") => {
                if let Some(g) = op.get("game") {
                    result_games.push(g.clone());
                    added += 1;
                }
            }
            Some("merge") => {
                let target_id = op
                    .get("target_game_id")
                    .and_then(Value::as_str)
                    .unwrap_or("");
                let source_id = op.get("source_id").and_then(Value::as_str).unwrap_or("");
                let pos = result_games
                    .iter()
                    .position(|g| {
                        (!target_id.is_empty()
                            && g.get("game_id").and_then(Value::as_str) == Some(target_id))
                            || (!source_id.is_empty()
                                && g.get("launchbox_source_id").and_then(Value::as_str)
                                    == Some(source_id))
                    })
                    .or_else(|| {
                        if target_id.is_empty() && source_id.is_empty() {
                            op.get("target_index")
                                .and_then(Value::as_u64)
                                .map(|i| i as usize)
                                .filter(|&i| i < result_games.len())
                        } else {
                            None
                        }
                    });
                let Some(idx) = pos else {
                    return Err(ApiError::bad_request(
                        "A LaunchBox merge target changed since the preview.",
                    ));
                };
                if let (Some(target), Some(ch)) = (
                    result_games[idx].as_object_mut(),
                    op.get("changes").and_then(Value::as_object),
                ) {
                    for (k, v) in ch {
                        target.insert(k.clone(), v.clone());
                    }
                }
                merged += 1;
            }
            Some("exclude") => excluded += 1,
            Some("skip") => skipped += 1,
            _ => {}
        }
    }
    let found = effective_plan
        .get("counts")
        .and_then(|c| c.get("found"))
        .and_then(Value::as_u64)
        .unwrap_or(0);
    let counts = json!({
        "added": added,
        "merged": merged,
        "skipped": skipped,
        "excluded": excluded,
        "found": found,
    });
    if let Some(obj) = state.as_object_mut() {
        obj.insert("games".into(), Value::Array(result_games));
        obj.insert("import_counts".into(), counts.clone());
    }
    Ok(counts)
}
