//! Pure, bounded ES-DE `gamelist.xml` import parser, planner, and applier
//! (`pkg/parity/parity_esde_import.py`).

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

pub const MAX_XML_BYTES: usize = 32 * 1024 * 1024;

static YEAR_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(\d{4})").expect("valid year regex"));

fn local_tag_lower(raw: &str) -> String {
    raw.rsplit(['}', ':']).next().unwrap_or(raw).to_lowercase()
}

fn parse_year(value: &str) -> String {
    let compact: String = value.chars().filter(|c| c.is_ascii_digit()).collect();
    if compact.len() >= 8
        && let Ok(y) = compact[..4].parse::<u32>()
        && (1000..=9999).contains(&y)
    {
        return y.to_string();
    }
    for cap in YEAR_RE.captures_iter(value) {
        if let Some(m) = cap.get(1)
            && let Ok(y) = m.as_str().parse::<u32>()
            && (1000..=9999).contains(&y)
        {
            return y.to_string();
        }
    }
    String::new()
}

fn parse_bool(value: &str) -> bool {
    matches!(
        value.trim().to_lowercase().as_str(),
        "1" | "true" | "yes" | "on"
    )
}

fn source_path(value: &str, parent: Option<&Path>) -> String {
    let raw = value.trim().replace('\\', "/");
    if raw.is_empty() {
        return String::new();
    }
    if let Some(rest) = raw.strip_prefix("./")
        && let Some(p) = parent
    {
        let joined = p.join(rest);
        return fs::canonicalize(&joined)
            .unwrap_or_else(|_| resolve_non_strict(&joined))
            .to_string_lossy()
            .into_owned();
    }
    expand_user(Path::new(&raw)).to_string_lossy().into_owned()
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
    let bytes = serde_json::to_vec(&sort_val(val)).unwrap_or_default();
    hex::encode(Sha256::digest(&bytes))
}

fn path_key(raw: &str) -> String {
    let text = raw.trim().replace('\\', "/");
    if text.is_empty() {
        return String::new();
    }
    let norm = resolve_non_strict(Path::new(&text))
        .to_string_lossy()
        .replace('\\', "/");
    let windows =
        (norm.len() >= 3 && norm.as_bytes()[0].is_ascii_alphabetic() && &norm[1..3] == ":/")
            || norm.starts_with("//");
    if windows { norm.to_lowercase() } else { norm }
}

pub fn parse_gamelist_bytes(
    data: &[u8],
    label: &str,
    parent: Option<&Path>,
) -> Result<Value, ApiError> {
    if data.len() > MAX_XML_BYTES {
        return Err(ApiError::bad_request(format!(
            "ES-DE XML is too large ({} bytes; limit is {MAX_XML_BYTES} bytes).",
            data.len()
        ))
        .with_code("ESDE_INVALID_SOURCE"));
    }
    let digest = hex::encode(Sha256::digest(data));
    let mut reader = Reader::from_reader(data);
    reader.config_mut().trim_text(false);
    let mut buf = Vec::new();
    let mut root_seen = false;
    let mut depth = 0usize;
    let mut in_game = false;
    let mut row_num = 0usize;
    let mut cur_field: Option<String> = None;
    let mut cur_text = String::new();
    let mut fields: HashMap<String, String> = HashMap::new();

    let mut games = Vec::new();
    let mut errors = Vec::new();
    let mut skipped = 0usize;

    loop {
        match reader.read_event_into(&mut buf) {
            Ok(Event::Start(e)) => {
                let tag = local_tag_lower(e.name().as_ref());
                if !root_seen {
                    root_seen = true;
                    if !matches!(tag.as_str(), "gamelist" | "game_list") {
                        return Err(ApiError::bad_request(format!(
                            "Could not parse ES-DE XML {label}: root element must be gameList."
                        ))
                        .with_code("ESDE_INVALID_SOURCE"));
                    }
                } else if depth == 1 && tag == "game" {
                    in_game = true;
                    row_num += 1;
                    fields.clear();
                } else if in_game && depth == 2 {
                    cur_field = Some(tag);
                    cur_text.clear();
                }
                depth += 1;
            }
            Ok(Event::Empty(e)) => {
                let tag = local_tag_lower(e.name().as_ref());
                if !root_seen {
                    root_seen = true;
                    if !matches!(tag.as_str(), "gamelist" | "game_list") {
                        return Err(ApiError::bad_request(format!(
                            "Could not parse ES-DE XML {label}: root element must be gameList."
                        ))
                        .with_code("ESDE_INVALID_SOURCE"));
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
                let tag = local_tag_lower(e.name().as_ref());
                if in_game && depth == 2 {
                    if let Some(f) = cur_field.take() {
                        let val = cur_text.trim().to_owned();
                        if !val.is_empty() {
                            fields.entry(f).or_insert(val);
                        }
                    }
                } else if in_game && depth == 1 && tag == "game" {
                    in_game = false;
                    let name = fields.get("name").cloned().unwrap_or_default();
                    let path =
                        source_path(fields.get("path").map(String::as_str).unwrap_or(""), parent);
                    if name.is_empty() || path.is_empty() {
                        skipped += 1;
                        errors.push(format!("Game row {row_num}: name and path are required"));
                        continue;
                    }
                    let mut entry = Map::new();
                    entry.insert("name".into(), Value::String(name));
                    entry.insert("path".into(), Value::String(path.clone()));
                    entry.insert("source".into(), Value::String("ES-DE".to_owned()));
                    let put = |m: &mut Map<String, Value>, k: &str, v: String| {
                        if !v.is_empty() {
                            m.insert(k.to_owned(), Value::String(v));
                        }
                    };
                    put(
                        &mut entry,
                        "platform",
                        fields.get("system").cloned().unwrap_or_default(),
                    );
                    put(
                        &mut entry,
                        "genre",
                        fields.get("genre").cloned().unwrap_or_default(),
                    );
                    put(
                        &mut entry,
                        "developer",
                        fields.get("developer").cloned().unwrap_or_default(),
                    );
                    put(
                        &mut entry,
                        "publisher",
                        fields.get("publisher").cloned().unwrap_or_default(),
                    );
                    let desc = fields
                        .get("desc")
                        .or_else(|| fields.get("description"))
                        .cloned()
                        .unwrap_or_default();
                    put(&mut entry, "description", desc);
                    put(
                        &mut entry,
                        "year",
                        parse_year(fields.get("releasedate").map(String::as_str).unwrap_or("")),
                    );
                    put(
                        &mut entry,
                        "region",
                        fields.get("region").cloned().unwrap_or_default(),
                    );
                    put(
                        &mut entry,
                        "max_players",
                        fields.get("players").cloned().unwrap_or_default(),
                    );
                    if parse_bool(fields.get("favorite").map(String::as_str).unwrap_or("")) {
                        entry.insert("favorite".into(), Value::Bool(true));
                    }
                    if parse_bool(fields.get("hidden").map(String::as_str).unwrap_or("")) {
                        entry.insert("hidden".into(), Value::Bool(true));
                    }
                    let source_id = fields.get("id").cloned().unwrap_or_default();
                    put(&mut entry, "esde_source_id", source_id.clone());
                    for (xml_k, ob_k) in [
                        ("image", "cover"),
                        ("thumbnail", "cover"),
                        ("marquee", "clear_logo"),
                        ("video", "video_snap"),
                        ("manual", "manual"),
                        ("manualpath", "manual"),
                        ("music", "music"),
                    ] {
                        if let Some(raw) = fields.get(xml_k) {
                            let resolved = source_path(raw, parent);
                            if !resolved.is_empty()
                                && !(ob_k == "cover" && entry.contains_key("cover"))
                            {
                                entry.insert(ob_k.to_owned(), Value::String(resolved));
                            }
                        }
                    }
                    if let Some(r_str) = fields.get("rating")
                        && let Ok(r) = r_str.parse::<f64>()
                        && r != 0.0
                    {
                        entry.insert("rating".into(), json!(if r <= 1.0 { r * 5.0 } else { r }));
                    }
                    let ident = if !source_id.is_empty() {
                        format!("esde:{source_id}")
                    } else {
                        format!("esde:path:{path}")
                    };
                    entry.insert("source_identity".into(), Value::String(ident.clone()));
                    entry.insert("source_identities".into(), json!([ident]));
                    games.push(Value::Object(entry));
                }
            }
            Ok(Event::Eof) => break,
            Err(err) => {
                return Err(ApiError::bad_request(format!(
                    "Could not parse ES-DE XML {label}: {err}"
                ))
                .with_code("ESDE_INVALID_SOURCE"));
            }
            _ => {}
        }
        buf.clear();
    }
    if !root_seen {
        return Err(ApiError::bad_request(format!(
            "Could not parse ES-DE XML {label}: empty document."
        ))
        .with_code("ESDE_INVALID_SOURCE"));
    }
    Ok(json!({
        "games": games,
        "skipped": skipped,
        "errors": errors,
        "source_digest": digest,
        "source_bytes": data.len(),
        "source_label": label,
    }))
}

pub fn parse_gamelist(path: &Path) -> Result<Value, ApiError> {
    let expanded = expand_user(path);
    let meta = fs::metadata(&expanded).map_err(|e| {
        ApiError::bad_request(format!(
            "Could not read ES-DE XML {}: {e}",
            expanded.display()
        ))
        .with_code("ESDE_INVALID_SOURCE")
    })?;
    if meta.len() as usize > MAX_XML_BYTES {
        return Err(ApiError::bad_request(format!(
            "ES-DE XML is too large ({} bytes; limit is {MAX_XML_BYTES} bytes).",
            meta.len()
        ))
        .with_code("ESDE_INVALID_SOURCE"));
    }
    let data = fs::read(&expanded).map_err(|e| {
        ApiError::bad_request(format!(
            "Could not read ES-DE XML {}: {e}",
            expanded.display()
        ))
        .with_code("ESDE_INVALID_SOURCE")
    })?;
    parse_gamelist_bytes(&data, &expanded.to_string_lossy(), expanded.parent())
}

fn map_path(value: &str, options: &Value) -> String {
    let candidate = value.trim();
    let cand_key = path_key(candidate);
    let mut mappings: Vec<(String, String)> = Vec::new();
    match options.get("path_mappings") {
        Some(Value::Object(m)) => {
            for (k, v) in m {
                if let Some(d) = v.as_str() {
                    mappings.push((k.clone(), d.to_owned()));
                }
            }
        }
        Some(Value::Array(arr)) => {
            for item in arr {
                if let (Some(s), Some(d)) = (
                    item.get("source").and_then(Value::as_str),
                    item.get("destination").and_then(Value::as_str),
                ) {
                    mappings.push((s.to_owned(), d.to_owned()));
                }
            }
        }
        _ => {}
    }
    mappings.sort_by_key(|(s, _)| std::cmp::Reverse(s.len()));
    for (src, dst) in mappings {
        let src_norm = src.trim().replace('\\', "/");
        let src_trimmed = src_norm.trim_end_matches('/');
        let dst_norm = expand_user(Path::new(dst.trim()))
            .to_string_lossy()
            .replace('\\', "/")
            .trim_end_matches('/')
            .to_owned();
        if src_trimmed.is_empty() || dst_norm.is_empty() {
            continue;
        }
        let src_key = path_key(src_trimmed);
        if cand_key == src_key {
            return dst_norm;
        }
        if cand_key.starts_with(&format!("{src_key}/")) {
            let suffix = cand_key[src_key.len()..].trim_start_matches('/');
            return format!("{dst_norm}/{suffix}");
        }
    }
    candidate.to_owned()
}

fn overwrite_set(options: &Value) -> HashSet<String> {
    match options.get("overwrite_fields") {
        Some(Value::Bool(true)) => [
            "name",
            "path",
            "platform",
            "genre",
            "year",
            "developer",
            "publisher",
            "description",
            "cover",
            "clear_logo",
            "video_snap",
            "manual",
            "music",
            "rating",
            "favorite",
            "hidden",
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
        None | Some(Value::Null) | Some(Value::Bool(false)) => true,
        Some(Value::String(s)) => s.is_empty(),
        Some(Value::Array(a)) => a.is_empty(),
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

    let mut by_ident: HashMap<String, usize> = HashMap::new();
    let mut by_path: HashMap<String, usize> = HashMap::new();
    for (idx, g) in existing_games.iter().enumerate() {
        if let Some(id) = g.get("source_identity").and_then(Value::as_str)
            && id.starts_with("esde:")
        {
            by_ident.insert(id.to_owned(), idx);
        }
        if let Some(p) = g.get("path").and_then(Value::as_str) {
            let pk = path_key(p);
            if !pk.is_empty() {
                by_path.insert(pk, idx);
            }
        }
    }

    let overwrite = overwrite_set(options);
    let mut normalized = Vec::new();
    let mut operations = Vec::new();
    let mut added = 0usize;
    let mut merged = 0usize;

    for row in &source_games {
        let mut game = row.as_object().cloned().unwrap_or_default();
        for f in [
            "path",
            "cover",
            "clear_logo",
            "video_snap",
            "manual",
            "music",
        ] {
            if let Some(s) = game.get(f).and_then(Value::as_str).map(ToOwned::to_owned) {
                game.insert(f.to_owned(), Value::String(map_path(&s, options)));
            }
        }
        game.insert("source".into(), Value::String("ES-DE".to_owned()));
        let esde_id = game
            .get("esde_source_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_owned();
        let path_val = game
            .get("path")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        let ident = if !esde_id.is_empty() {
            format!("esde:{esde_id}")
        } else {
            format!("esde:path:{path_val}")
        };
        game.insert("source_identity".into(), Value::String(ident.clone()));
        game.insert("source_identities".into(), json!([ident]));
        let imported = Value::Object(game.clone());
        normalized.push(imported.clone());

        let tidx = by_ident
            .get(&ident)
            .or_else(|| by_path.get(&path_key(&path_val)))
            .copied();
        let Some(tidx) = tidx else {
            added += 1;
            operations.push(json!({
                "action": "add",
                "game": imported,
                "changes": imported,
            }));
            continue;
        };
        let target = &existing_games[tidx];
        let mut changes = Map::new();
        let mut review: BTreeSet<String> = BTreeSet::new();
        for (field, value) in &game {
            if matches!(
                field.as_str(),
                "source" | "source_identity" | "source_identities" | "esde_source_id"
            ) || is_empty_val(Some(value))
            {
                continue;
            }
            if target.get(field) == Some(value) {
                continue;
            }
            if !is_empty_val(target.get(field)) && !overwrite.contains(field) {
                review.insert(field.clone());
                continue;
            }
            changes.insert(field.clone(), value.clone());
        }
        merged += 1;
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
        operations.push(json!({
            "action": "merge",
            "target_game_id": target_id,
            "game": imported,
            "changes": Value::Object(changes),
            "review_fields": review.into_iter().collect::<Vec<_>>(),
        }));
    }

    let base_digest = canonical_json_digest(&Value::Array(existing_games));
    let options_digest = canonical_json_digest(options);
    let source_games_digest = canonical_json_digest(&Value::Array(source_games.clone()));
    let token = canonical_json_digest(&json!({
        "source_digest": source_digest,
        "source_games_digest": source_games_digest,
        "base_digest": base_digest,
        "options_digest": options_digest,
    }));
    let total = source_games.len();
    json!({
        "schema_version": 1,
        "source": {
            "digest": source_digest,
            "bytes": parsed.get("source_bytes").and_then(Value::as_u64).unwrap_or(0),
            "label": parsed.get("source_label").and_then(Value::as_str).unwrap_or("<parsed>"),
        },
        "source_digest": source_digest,
        "source_games_digest": source_games_digest,
        "base_digest": base_digest,
        "options": options,
        "options_digest": options_digest,
        "preview_token": token,
        "counts": {
            "total_in_xml": total,
            "skipped_malformed": parsed.get("skipped").and_then(Value::as_u64).unwrap_or(0),
            "added": added,
            "merged": merged,
            "found": total,
        },
        "operations": operations,
        "errors": parsed.get("errors").cloned().unwrap_or_else(|| json!([])),
        "normalized_games": normalized,
        "settings": settings,
    })
}

pub fn apply_import_plan(
    plan: &Value,
    state: &mut Value,
    preview_token: Option<&str>,
    source_digest: Option<&str>,
    source: Option<&Value>,
    options: &Value,
) -> Result<Value, ApiError> {
    let expected = plan
        .get("preview_token")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| {
            ApiError::bad_request("A valid ES-DE import plan is required.")
                .with_code("ESDE_INVALID_PLAN")
        })?
        .to_owned();
    let canonical = if let Some(src) = source {
        build_import_plan(src, state, options)
    } else {
        plan.clone()
    };
    if canonical.get("preview_token").and_then(Value::as_str) != Some(expected.as_str()) {
        return Err(
            ApiError::bad_request("ES-DE preview decisions changed; review them again.")
                .with_code("ESDE_STALE_PLAN"),
        );
    }
    if let Some(pt) = preview_token
        && pt != expected
    {
        return Err(
            ApiError::bad_request("ES-DE preview token is stale.").with_code("ESDE_STALE_PLAN")
        );
    }
    if let Some(sd) = source_digest
        && Some(sd) != canonical.get("source_digest").and_then(Value::as_str)
    {
        return Err(
            ApiError::bad_request("ES-DE gamelist changed since the preview.")
                .with_code("ESDE_STALE_PLAN"),
        );
    }
    let current_games = state
        .get("games")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let cur_digest = canonical_json_digest(&Value::Array(current_games.clone()));
    if Some(cur_digest.as_str()) != canonical.get("base_digest").and_then(Value::as_str) {
        return Err(
            ApiError::bad_request("OpenBox library changed since the ES-DE preview.")
                .with_code("ESDE_STALE_PLAN"),
        );
    }
    let mut result_games = current_games;
    let mut added = 0usize;
    let mut merged = 0usize;
    for op in canonical
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
                let pos = result_games.iter().position(|g| {
                    g.get("game_id").and_then(Value::as_str) == Some(target_id)
                        || g.get("id")
                            .map(|v| v.to_string().trim_matches('"').to_owned())
                            .as_deref()
                            == Some(target_id)
                });
                let Some(idx) = pos else {
                    return Err(ApiError::bad_request(
                        "An ES-DE merge target changed since the preview.",
                    )
                    .with_code("ESDE_STALE_PLAN"));
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
            _ => {}
        }
    }
    let found = canonical
        .get("counts")
        .and_then(|c| c.get("found"))
        .and_then(Value::as_u64)
        .unwrap_or(0);
    let counts = json!({"added": added, "merged": merged, "found": found});
    if let Some(obj) = state.as_object_mut() {
        obj.insert("games".into(), Value::Array(result_games));
        obj.insert("import_counts".into(), counts.clone());
    }
    Ok(counts)
}
