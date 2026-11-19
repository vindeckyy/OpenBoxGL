//! LaunchBox Games Database sync, SQLite indexing, search, batch matching,
//! match preview workflow, and media download (`metadata.py`).

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fs::{self, File};
use std::io::BufReader;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use openbox_core::ApiError;
use openbox_io::{atomic_write_text, extract_game};
use quick_xml::Reader;
use quick_xml::events::Event;
use regex::Regex;
use rusqlite::{Connection, params_from_iter};
use serde_json::{Map, Value, json};
use uuid::Uuid;
use zip::ZipArchive;

use crate::http::{
    download_file_sync, download_streaming_sha256, py_round, sequence_matcher_ratio,
};
use crate::media::{REGION_PRIORITY_DEFAULT, sort_images_by_region};

pub const DATABASE_URL: &str = "https://gamesdb.launchbox-app.com/Metadata.zip";
pub const IMAGE_URL: &str = "https://images.launchbox-app.com/";
pub const MANUAL_SUFFIXES: &[&str] = &[".pdf", ".txt"];

pub const DATABASE_SHA256: &str = "";
pub const METADATA_SHA_FILE: &str = "metadata-db.sha256";
const MAX_SYNC_ATTEMPTS: usize = 3;
const SYNC_BACKOFF_SECS: &[u64] = &[2, 8];

pub const MATCH_PREVIEW_TTL_HOURS: i64 = 24;
pub const MAX_MATCH_DECISION_BATCH: usize = 200;
pub const DEFAULT_MATCH_ITEMS_LIMIT: usize = 50;
pub const MAX_MATCH_ITEMS_LIMIT: usize = 200;
pub const MAX_REJECTED_IDS: usize = 20;

pub const ESRB_VALUES: &[&str] = &[
    "", "E", "E10+", "T", "M", "AO", "RP", "EC", "K-A", "Unrated",
];

static NORM_STRIP_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)\([^)]*(?:USA|Europe|Japan|World|Rev|Disc)[^)]*\)|\[[^\]]+\]").unwrap()
});
static NORM_NON_ALNUM_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[^a-z0-9]+").unwrap());

pub fn media_type_map() -> &'static [(&'static str, &'static [&'static str])] {
    &[
        ("cover", &["Box - Front", "Box - Front - Reconstructed"]),
        ("background", &["Fanart - Background"]),
        ("screenshots", &["Screenshot - Gameplay"]),
        ("box_back", &["Box - Back", "Box - Back - Reconstructed"]),
        ("box_spine", &["Box - Spine"]),
        ("box_3d", &["Box - 3D"]),
        ("clear_logo", &["Clear Logo"]),
        ("fanart", &["Fanart - Box - Front", "Fanart - Box - Back"]),
        ("banner", &["Banner"]),
        ("icon", &["Icon"]),
        ("title_screen", &["Screenshot - Game Title"]),
        ("cart_front", &["Cart - Front"]),
        ("cart_back", &["Cart - Back"]),
        ("disc", &["Disc", "Fanart - Disc"]),
        (
            "advertisement",
            &["Advertisement Flyer - Front", "Advertisement Flyer - Back"],
        ),
        ("manual", &[]),
    ]
}

pub fn platform_alias(platform: &str) -> &str {
    match platform {
        "NES" => "Nintendo Entertainment System",
        "SNES" => "Super Nintendo Entertainment System",
        "Game Boy" => "Nintendo Game Boy",
        "Game Boy Color" => "Nintendo Game Boy Color",
        "Game Boy Advance" => "Nintendo Game Boy Advance",
        "Nintendo 64" => "Nintendo 64",
        "Nintendo DS" => "Nintendo DS",
        "Nintendo 3DS" => "Nintendo 3DS",
        "GameCube" => "Nintendo GameCube",
        "Wii" => "Nintendo Wii",
        "Wii U" => "Nintendo Wii U",
        "Nintendo Switch" => "Nintendo Switch",
        "PlayStation" => "Sony Playstation",
        "PlayStation 2" => "Sony Playstation 2",
        "PlayStation 3" => "Sony Playstation 3",
        "PSP" => "Sony PSP",
        "PlayStation Vita" => "Sony Playstation Vita",
        "Xbox" => "Microsoft Xbox",
        "Xbox 360" => "Microsoft Xbox 360",
        "Genesis" => "Sega Genesis",
        "Sega Saturn" => "Sega Saturn",
        "Arcade" => "Arcade",
        "ScummVM" => "ScummVM",
        "PC" => "Windows",
        "MS-DOS" | "DOS" => "MS-DOS",
        other => other,
    }
}

pub fn normalized(text: &str) -> String {
    let stripped = NORM_STRIP_RE.replace_all(text, "");
    let lowered = stripped.to_lowercase();
    NORM_NON_ALNUM_RE
        .replace_all(&lowered, " ")
        .trim()
        .to_owned()
}

pub fn open_db(database_path: &Path) -> Result<Connection, String> {
    let conn = Connection::open(database_path).map_err(|e| e.to_string())?;
    let _ = conn.busy_timeout(Duration::from_secs(30));
    let _ = conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;");
    Ok(conn)
}

/// Build the schema-identical LaunchBox metadata SQLite DB by streaming XML entries out of `metadata_zip`.
pub fn build_database(metadata_zip: &Path, destination: &Path) -> Result<(), String> {
    let temporary = destination.with_extension("tmp");
    let _ = fs::remove_file(&temporary);
    if let Some(parent) = destination.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let mut conn = Connection::open(&temporary).map_err(|e| e.to_string())?;
    conn.execute_batch(
        r#"
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        CREATE TABLE games (
            database_id INTEGER PRIMARY KEY, name TEXT, normalized TEXT, platform TEXT,
            release_date TEXT, developer TEXT, publisher TEXT, genre TEXT, overview TEXT,
            series TEXT, esrb TEXT, max_players TEXT, cooperative TEXT
        );
        CREATE TABLE images (database_id INTEGER, filename TEXT, type TEXT, region TEXT);
        CREATE INDEX game_lookup ON games(normalized, platform);
        CREATE INDEX image_lookup ON images(database_id, type);
        "#,
    )
    .map_err(|e| e.to_string())?;

    let file = File::open(metadata_zip).map_err(|e| e.to_string())?;
    let mut archive = ZipArchive::new(file).map_err(|e| e.to_string())?;
    let member_names: Vec<String> = archive
        .file_names()
        .filter_map(|r| r.ok())
        .map(|s| s.into_owned())
        .filter(|n| n.to_ascii_lowercase().ends_with(".xml"))
        .collect();

    {
        let tx = conn.transaction().map_err(|e| e.to_string())?;
        let mut ins_game = tx
            .prepare(
                "INSERT OR REPLACE INTO games VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13)",
            )
            .map_err(|e| e.to_string())?;
        let mut ins_img = tx
            .prepare("INSERT INTO images VALUES (?1,?2,?3,?4)")
            .map_err(|e| e.to_string())?;

        for member in &member_names {
            let entry = archive.by_name(member).map_err(|e| e.to_string())?;
            let mut reader = Reader::from_reader(BufReader::new(entry));
            reader.config_mut().trim_text(false);
            let mut buf: Vec<u8> = Vec::new();
            let mut current_record: Option<&'static str> = None;
            let mut current_field: Option<String> = None;
            let mut fields: HashMap<String, String> = HashMap::new();

            loop {
                buf.clear();
                match reader.read_event_into(&mut buf) {
                    Ok(Event::Start(e)) => {
                        let raw_tag = e.name();
                        let tag_str = raw_tag.as_ref();
                        let tag = tag_str.rsplit(':').next().unwrap_or(tag_str);
                        if current_record.is_none() {
                            if tag == "Game" {
                                current_record = Some("Game");
                                fields.clear();
                            } else if tag == "GameImage" {
                                current_record = Some("GameImage");
                                fields.clear();
                            }
                        } else if current_field.is_none() {
                            current_field = Some(tag.to_owned());
                        }
                    }
                    Ok(Event::Text(t)) => {
                        if let Some(ref f) = current_field {
                            let raw_s = t.as_ref();
                            let decoded = quick_xml::escape::unescape(raw_s)
                                .map(|c| c.into_owned())
                                .unwrap_or_else(|_| raw_s.to_owned());
                            fields.entry(f.clone()).or_default().push_str(&decoded);
                        }
                    }
                    Ok(Event::CData(c)) => {
                        if let Some(ref f) = current_field {
                            fields.entry(f.clone()).or_default().push_str(c.as_ref());
                        }
                    }
                    Ok(Event::End(e)) => {
                        let raw_tag = e.name();
                        let tag_str = raw_tag.as_ref();
                        let tag = tag_str.rsplit(':').next().unwrap_or(tag_str);
                        if let Some(ref f) = current_field
                            && f == tag
                        {
                            current_field = None;
                        } else if let Some(rec) = current_record
                            && rec == tag
                        {
                            if let Some(db_id) = fields
                                .get("DatabaseID")
                                .and_then(|s| s.trim().parse::<i64>().ok())
                            {
                                if rec == "Game" {
                                    let name = fields.get("Name").map(|s| s.trim()).unwrap_or("");
                                    if !name.is_empty() {
                                        let g =
                                            |k: &str| fields.get(k).map(|s| s.trim()).unwrap_or("");
                                        let genre = {
                                            let gs = g("Genres");
                                            if !gs.is_empty() { gs } else { g("Genre") }
                                        };
                                        let norm = normalized(name);
                                        let _ = ins_game.execute(rusqlite::params![
                                            db_id,
                                            name,
                                            norm,
                                            g("Platform"),
                                            g("ReleaseDate"),
                                            g("Developer"),
                                            g("Publisher"),
                                            genre,
                                            g("Overview"),
                                            g("Series"),
                                            g("ESRB"),
                                            g("MaxPlayers"),
                                            g("Cooperative"),
                                        ]);
                                    }
                                } else if rec == "GameImage" {
                                    let fname_raw =
                                        fields.get("FileName").map(|s| s.trim()).unwrap_or("");
                                    if !fname_raw.is_empty() {
                                        let fname = Path::new(fname_raw)
                                            .file_name()
                                            .map(|n| n.to_string_lossy().into_owned())
                                            .unwrap_or_else(|| fname_raw.to_owned());
                                        let img_type =
                                            fields.get("Type").map(|s| s.trim()).unwrap_or("");
                                        let region =
                                            fields.get("Region").map(|s| s.trim()).unwrap_or("");
                                        let _ = ins_img.execute(rusqlite::params![
                                            db_id, fname, img_type, region
                                        ]);
                                    }
                                }
                            }
                            fields.clear();
                            current_record = None;
                            current_field = None;
                        }
                    }
                    Ok(Event::Eof) => break,
                    Err(e) => return Err(format!("XML parse error in {member}: {e}")),
                    _ => {}
                }
            }
        }
        drop(ins_game);
        drop(ins_img);
        tx.commit().map_err(|e| e.to_string())?;
    }
    drop(conn);
    fs::rename(&temporary, destination).map_err(|e| e.to_string())?;
    Ok(())
}

fn metadata_sha_path(destination: &Path) -> PathBuf {
    if let Ok(override_dir) = std::env::var("OPENBOX_DATA_DIR")
        && !override_dir.trim().is_empty()
    {
        return PathBuf::from(override_dir)
            .join("cache")
            .join(METADATA_SHA_FILE);
    }
    if destination
        .parent()
        .and_then(|p| p.file_name())
        .and_then(|n| n.to_str())
        == Some("metadata")
        && let Some(grand) = destination.parent().and_then(|p| p.parent())
    {
        return grand.join("cache").join(METADATA_SHA_FILE);
    }
    destination
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .join(METADATA_SHA_FILE)
}

fn expected_metadata_sha(checksum_file: &Path) -> String {
    let pinned = DATABASE_SHA256.trim().to_ascii_lowercase();
    if !pinned.is_empty() {
        return pinned;
    }
    fs::read_to_string(checksum_file)
        .map(|s| s.trim().to_ascii_lowercase())
        .unwrap_or_default()
}

pub fn sync_database<F>(destination: &Path, mut progress: F) -> Result<(), String>
where
    F: FnMut(&str, u64, u64) + Send,
{
    if let Some(parent) = destination.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let checksum_file = metadata_sha_path(destination);
    let expected = expected_metadata_sha(&checksum_file);
    let mut last_checksum = String::new();

    for attempt in 0..MAX_SYNC_ATTEMPTS {
        let archive = destination
            .parent()
            .unwrap_or_else(|| Path::new("."))
            .join(format!(".metadata-sync-{}.zip", Uuid::new_v4().simple()));
        let res = download_streaming_sha256(
            DATABASE_URL,
            &archive,
            2 * 1024 * 1024 * 1024,
            120,
            |cur, tot| progress("download", cur, tot),
        );
        let (_, digest, size) = match res {
            Ok(tuple) => tuple,
            Err(err) => {
                let _ = fs::remove_file(&archive);
                return Err(format!(
                    "Could not reach the metadata database ({err}). Check the connection and try again."
                ));
            }
        };
        progress("verify", size, size);
        if !expected.is_empty() && digest != expected {
            let _ = fs::remove_file(&archive);
            last_checksum = digest;
            if attempt + 1 < MAX_SYNC_ATTEMPTS {
                let delay = SYNC_BACKOFF_SECS[attempt.min(SYNC_BACKOFF_SECS.len() - 1)];
                std::thread::sleep(Duration::from_secs(delay));
                continue;
            }
            return Err("Metadata database checksum mismatch".to_owned());
        }
        if expected.is_empty() {
            let _ = atomic_write_text(&checksum_file, &format!("{digest}\n"), 0o600);
        }
        progress("build", 0, size);
        let build_res = build_database(&archive, destination);
        let _ = fs::remove_file(&archive);
        return build_res;
    }
    Err(format!(
        "Metadata database checksum mismatch ({})",
        if last_checksum.is_empty() {
            "unknown"
        } else {
            &last_checksum
        }
    ))
}

fn game_row_to_value(row: &rusqlite::Row<'_>) -> rusqlite::Result<Value> {
    Ok(json!({
        "database_id": row.get::<_, i64>("database_id")?,
        "name": row.get::<_, String>("name").unwrap_or_default(),
        "normalized": row.get::<_, String>("normalized").unwrap_or_default(),
        "platform": row.get::<_, String>("platform").unwrap_or_default(),
        "release_date": row.get::<_, String>("release_date").unwrap_or_default(),
        "developer": row.get::<_, String>("developer").unwrap_or_default(),
        "publisher": row.get::<_, String>("publisher").unwrap_or_default(),
        "genre": row.get::<_, String>("genre").unwrap_or_default(),
        "overview": row.get::<_, String>("overview").unwrap_or_default(),
        "series": row.get::<_, String>("series").unwrap_or_default(),
        "esrb": row.get::<_, String>("esrb").unwrap_or_default(),
        "max_players": row.get::<_, String>("max_players").unwrap_or_default(),
        "cooperative": row.get::<_, String>("cooperative").unwrap_or_default(),
    }))
}

pub fn search_games(
    database_path: &Path,
    title: &str,
    platform: &str,
    limit: usize,
) -> Result<Vec<Value>, String> {
    let conn = open_db(database_path)?;
    let query = normalized(title);
    let like_q = format!("%{query}%");
    let lbdb_platform = platform_alias(platform);
    let mut stmt = conn
        .prepare(
            "SELECT database_id, name, normalized, platform, release_date, developer, publisher, \
             genre, overview, series, esrb, max_players, cooperative FROM games \
             WHERE normalized = ?1 OR normalized LIKE ?2 \
             ORDER BY (normalized = ?3) DESC, (lower(platform) = lower(?4)) DESC, length(name) \
             LIMIT ?5",
        )
        .map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map(
            rusqlite::params![query, like_q, query, lbdb_platform, limit as i64],
            game_row_to_value,
        )
        .map_err(|e| e.to_string())?;
    let mut out = Vec::new();
    for row in rows.flatten() {
        out.push(row);
    }
    Ok(out)
}

pub fn batch_match(
    database_path: &Path,
    titles: &[(String, String)],
) -> Result<HashMap<(String, String), Value>, String> {
    if titles.is_empty() {
        return Ok(HashMap::new());
    }
    let conn = open_db(database_path)?;
    let pairs: Vec<(&str, &str, String)> = titles
        .iter()
        .map(|(t, p)| (t.as_str(), p.as_str(), normalized(t)))
        .collect();
    let unique_queries: Vec<String> = pairs
        .iter()
        .map(|(_, _, q)| q.clone())
        .filter(|q| !q.is_empty())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    if unique_queries.is_empty() {
        return Ok(HashMap::new());
    }

    let mut rows_by_norm: HashMap<String, Vec<Value>> = HashMap::new();
    for chunk in unique_queries.chunks(500) {
        let placeholders = vec!["?"; chunk.len()].join(",");
        let sql = format!(
            "SELECT database_id, name, platform, normalized FROM games \
             WHERE normalized IN ({placeholders}) ORDER BY length(name)"
        );
        let mut stmt = conn.prepare(&sql).map_err(|e| e.to_string())?;
        let mapped = stmt
            .query_map(params_from_iter(chunk.iter()), |row| {
                Ok(json!({
                    "database_id": row.get::<_, i64>(0)?,
                    "name": row.get::<_, String>(1).unwrap_or_default(),
                    "platform": row.get::<_, String>(2).unwrap_or_default(),
                    "normalized": row.get::<_, String>(3).unwrap_or_default(),
                }))
            })
            .map_err(|e| e.to_string())?;
        for r in mapped.flatten() {
            if let Some(n) = r.get("normalized").and_then(Value::as_str) {
                rows_by_norm.entry(n.to_owned()).or_default().push(r);
            }
        }
    }

    let mut matched = HashMap::new();
    for (title, platform, query) in pairs {
        let Some(candidates) = rows_by_norm.get(&query) else {
            continue;
        };
        let lbdb_plat = platform_alias(platform).to_lowercase();
        let best = candidates.iter().max_by(|a, b| {
            let a_plat = a
                .get("platform")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_lowercase()
                == lbdb_plat;
            let b_plat = b
                .get("platform")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_lowercase()
                == lbdb_plat;
            let a_len = -(a.get("name").and_then(Value::as_str).unwrap_or("").len() as isize);
            let b_len = -(b.get("name").and_then(Value::as_str).unwrap_or("").len() as isize);
            (a_plat, a_len).cmp(&(b_plat, b_len))
        });
        if let Some(b) = best {
            matched.insert((title.to_owned(), platform.to_owned()), b.clone());
        }
    }
    Ok(matched)
}

pub fn token_overlap(left: &str, right: &str) -> f64 {
    let l: HashSet<String> = normalized(left)
        .split_whitespace()
        .map(ToOwned::to_owned)
        .collect();
    let r: HashSet<String> = normalized(right)
        .split_whitespace()
        .map(ToOwned::to_owned)
        .collect();
    if l.is_empty() || r.is_empty() {
        return 0.0;
    }
    let inter = l.intersection(&r).count();
    let union = l.union(&r).count();
    inter as f64 / union as f64
}

pub fn title_similarity(left: &str, right: &str) -> f64 {
    let ln = normalized(left);
    let rn = normalized(right);
    if ln.is_empty() || rn.is_empty() {
        return 0.0;
    }
    if ln == rn {
        return 1.0;
    }
    sequence_matcher_ratio(&ln, &rn)
}

pub fn platform_exact_match(game_platform: &str, db_platform: &str) -> bool {
    platform_alias(game_platform).eq_ignore_ascii_case(db_platform)
}

pub fn year_from_record(record: &Value) -> Option<String> {
    let value = record
        .get("release_date")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if value.is_empty() {
        None
    } else if value.len() >= 4 {
        Some(value[..4].to_owned())
    } else {
        Some(value.to_owned())
    }
}

pub fn apply_esrb_from_record(game: &mut Map<String, Value>, record: &Value) {
    let esrb = record
        .get("esrb")
        .or_else(|| record.get("ESRB"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if !esrb.is_empty() && ESRB_VALUES.contains(&esrb) {
        game.insert("esrb".into(), Value::String(esrb.to_owned()));
    }
}

fn match_score(title: &str, platform: &str, candidate: &Value) -> Value {
    let mut reasons = Vec::new();
    let cand_name = candidate.get("name").and_then(Value::as_str).unwrap_or("");
    let cand_plat = candidate
        .get("platform")
        .and_then(Value::as_str)
        .unwrap_or("");
    let title_sim = title_similarity(title, cand_name);
    let overlap = token_overlap(title, cand_name);
    let exact_platform = platform_exact_match(platform, cand_plat);
    if title_sim == 1.0 {
        reasons.push("exact normalized title");
    } else if title_sim >= 0.9 {
        reasons.push("high title similarity");
    }
    if overlap >= 0.8 {
        reasons.push("strong token overlap");
    }
    if exact_platform {
        reasons.push("exact platform");
    } else {
        reasons.push("platform mismatch");
    }
    json!({
        "title_similarity": py_round(title_sim, 4),
        "token_overlap": py_round(overlap, 4),
        "platform_exact": exact_platform,
        "reasons": reasons,
    })
}

fn media_categories_for_game(game: &Value) -> Vec<String> {
    let mut out = Vec::new();
    for (field, _) in media_type_map() {
        if *field == "screenshots" {
            if game
                .get("screenshots")
                .and_then(Value::as_array)
                .is_some_and(|a| !a.is_empty())
            {
                out.push((*field).to_owned());
            }
        } else if game
            .get(*field)
            .and_then(Value::as_str)
            .is_some_and(|s| !s.is_empty())
        {
            out.push((*field).to_owned());
        }
    }
    out
}

fn media_categories_for_record(conn: &Connection, database_id: i64) -> Vec<String> {
    let mut reverse: HashMap<&str, &str> = HashMap::new();
    for (field, types) in media_type_map() {
        for t in *types {
            reverse.entry(t).or_insert(*field);
        }
    }
    let mut cats = BTreeSet::new();
    if let Ok(mut stmt) = conn.prepare("SELECT DISTINCT type FROM images WHERE database_id = ?1")
        && let Ok(rows) = stmt.query_map([database_id], |r| r.get::<_, String>(0))
    {
        for t in rows.flatten() {
            if let Some(&mapped) = reverse.get(t.as_str()) {
                cats.insert(mapped.to_owned());
            }
        }
    }
    cats.into_iter().collect()
}

fn current_snapshot(game: &Value) -> Value {
    json!({
        "title": game.get("name").and_then(Value::as_str).unwrap_or(""),
        "platform": game.get("platform").cloned().unwrap_or(Value::Null),
        "year": game.get("year").cloned().unwrap_or(Value::Null),
        "developer": game.get("developer").cloned().unwrap_or(Value::Null),
        "publisher": game.get("publisher").cloned().unwrap_or(Value::Null),
        "genre": game.get("genre").cloned().unwrap_or(Value::Null),
        "esrb": game.get("esrb").cloned().unwrap_or(Value::Null),
        "description": game.get("description").cloned().unwrap_or(Value::Null),
        "media_categories": media_categories_for_game(game),
    })
}

pub fn load_metadata_record(database_path: &Path, database_id: i64) -> Result<Value, String> {
    let conn = open_db(database_path)?;
    conn.query_row(
        "SELECT database_id, name, normalized, platform, release_date, developer, publisher, \
         genre, overview, series, esrb, max_players, cooperative FROM games WHERE database_id = ?1",
        [database_id],
        game_row_to_value,
    )
    .map_err(|_| "Metadata game not found.".to_owned())
}

fn proposed_snapshot(database_path: &Path, database_id: i64) -> Result<Value, String> {
    let conn = open_db(database_path)?;
    let record = load_metadata_record(database_path, database_id)?;
    Ok(json!({
        "database_id": database_id.to_string(),
        "title": record.get("name").and_then(Value::as_str).unwrap_or(""),
        "platform": record.get("platform").cloned().unwrap_or(Value::Null),
        "year": year_from_record(&record),
        "developer": record.get("developer").cloned().unwrap_or(Value::Null),
        "publisher": record.get("publisher").cloned().unwrap_or(Value::Null),
        "genre": record.get("genre").cloned().unwrap_or(Value::Null),
        "esrb": record.get("esrb").cloned().unwrap_or(Value::Null),
        "description": record.get("overview").cloned().unwrap_or(Value::Null),
        "media_categories": media_categories_for_record(&conn, database_id),
    }))
}

fn alternative_entry(candidate: &Value, title: &str, platform: &str) -> Value {
    json!({
        "database_id": candidate.get("database_id").map(|v| match v {
            Value::String(s) => s.clone(),
            other => other.to_string(),
        }).unwrap_or_default(),
        "title": candidate.get("name").and_then(Value::as_str).unwrap_or(""),
        "platform": candidate.get("platform").cloned().unwrap_or(Value::Null),
        "score": match_score(title, platform, candidate),
    })
}

/// Returns `(match_class, Option<review_item>, Option<auto_database_id>)`.
pub fn classify_game_match(
    database_path: &Path,
    game: &Value,
    rejected_ids: &[String],
) -> Result<(String, Option<Value>, Option<String>), String> {
    let title = game
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    let platform = game.get("platform").and_then(Value::as_str).unwrap_or("");
    let game_id = game.get("game_id").and_then(Value::as_str).unwrap_or("");
    let rejected: HashSet<&str> = rejected_ids.iter().map(String::as_str).collect();
    if title.is_empty() {
        return Ok((
            "unmatched".to_owned(),
            Some(json!({
                "game_id": game_id,
                "class": "unmatched",
                "score": {
                    "title_similarity": 0.0,
                    "token_overlap": 0.0,
                    "platform_exact": false,
                    "reasons": ["empty title"],
                },
                "current": current_snapshot(game),
                "proposed": Value::Null,
                "alternatives": [],
            })),
            None,
        ));
    }

    let query = normalized(title);
    let conn = open_db(database_path)?;
    let mut stmt = conn
        .prepare("SELECT database_id, name, platform, normalized FROM games WHERE normalized = ?1")
        .map_err(|e| e.to_string())?;
    let exact_rows: Vec<Value> = stmt
        .query_map([&query], |row| {
            Ok(json!({
                "database_id": row.get::<_, i64>(0)?,
                "name": row.get::<_, String>(1).unwrap_or_default(),
                "platform": row.get::<_, String>(2).unwrap_or_default(),
                "normalized": row.get::<_, String>(3).unwrap_or_default(),
            }))
        })
        .map_err(|e| e.to_string())?
        .flatten()
        .collect();

    let available_exact: Vec<Value> = exact_rows
        .into_iter()
        .filter(|row| {
            platform_exact_match(
                platform,
                row.get("platform").and_then(Value::as_str).unwrap_or(""),
            )
        })
        .filter(|row| {
            let id_s = row["database_id"].to_string();
            !rejected.contains(id_s.as_str())
        })
        .collect();

    if available_exact.len() == 1 {
        let auto_id = available_exact[0]["database_id"].to_string();
        return Ok(("auto".to_owned(), None, Some(auto_id)));
    }
    if available_exact.len() > 1 {
        let proposed = &available_exact[0];
        let pid = proposed["database_id"].as_i64().unwrap_or(0);
        let alternatives: Vec<Value> = available_exact
            .iter()
            .skip(1)
            .take(5)
            .map(|r| alternative_entry(r, title, platform))
            .collect();
        return Ok((
            "exact_review".to_owned(),
            Some(json!({
                "game_id": game_id,
                "class": "exact_review",
                "score": match_score(title, platform, proposed),
                "current": current_snapshot(game),
                "proposed": proposed_snapshot(database_path, pid)?,
                "alternatives": alternatives,
            })),
            None,
        ));
    }

    let candidates = search_games(database_path, title, platform, 12)?;
    let mut ranked: Vec<(f64, f64, Value, Value)> = Vec::new();
    for cand in candidates {
        let id_s = cand["database_id"].to_string();
        if rejected.contains(id_s.as_str()) {
            continue;
        }
        let sc = match_score(title, platform, &cand);
        let sim = sc["title_similarity"].as_f64().unwrap_or(0.0);
        let ov = sc["token_overlap"].as_f64().unwrap_or(0.0);
        ranked.push((sim, ov, cand, sc));
    }
    ranked.sort_by(|a, b| b.0.total_cmp(&a.0).then_with(|| b.1.total_cmp(&a.1)));
    if ranked.is_empty() {
        return Ok((
            "unmatched".to_owned(),
            Some(json!({
                "game_id": game_id,
                "class": "unmatched",
                "score": {
                    "title_similarity": 0.0,
                    "token_overlap": 0.0,
                    "platform_exact": false,
                    "reasons": ["no candidates"],
                },
                "current": current_snapshot(game),
                "proposed": Value::Null,
                "alternatives": [],
            })),
            None,
        ));
    }

    let best = &ranked[0].2;
    let best_score = &ranked[0].3;
    let alternatives: Vec<Value> = ranked
        .iter()
        .skip(1)
        .take(5)
        .map(|r| alternative_entry(&r.2, title, platform))
        .collect();
    let sim = best_score["title_similarity"].as_f64().unwrap_or(0.0);
    let ov = best_score["token_overlap"].as_f64().unwrap_or(0.0);
    let exact_plat = best_score["platform_exact"].as_bool().unwrap_or(false);
    let match_class = if sim >= 0.90 && ov >= 0.80 && exact_plat {
        "likely"
    } else if sim >= 0.75 && ov >= 0.75 {
        "possible"
    } else {
        "unmatched"
    };
    let proposed = if match_class != "unmatched" {
        let pid = best["database_id"].as_i64().unwrap_or(0);
        proposed_snapshot(database_path, pid)?
    } else {
        Value::Null
    };
    Ok((
        match_class.to_owned(),
        Some(json!({
            "game_id": game_id,
            "class": match_class,
            "score": best_score,
            "current": current_snapshot(game),
            "proposed": proposed,
            "alternatives": if match_class != "unmatched" { alternatives } else { Vec::new() },
        })),
        None,
    ))
}

pub fn match_previews_dir(data_dir: &Path) -> PathBuf {
    data_dir.join("match_previews")
}

fn match_preview_path(data_dir: &Path, preview_id: &str) -> PathBuf {
    match_previews_dir(data_dir).join(format!("{preview_id}.json"))
}

fn match_expires_at(hours: i64) -> String {
    let ts = jiff::Timestamp::now()
        .checked_add(jiff::SignedDuration::from_hours(hours))
        .unwrap_or_else(|_| jiff::Timestamp::now());
    ts.to_string()
}

fn match_is_expired(expires_at: &str) -> bool {
    if expires_at.is_empty() {
        return false;
    }
    let Ok(parsed) = expires_at.parse::<jiff::Timestamp>() else {
        return false;
    };
    parsed < jiff::Timestamp::now()
}

pub fn save_match_preview(data_dir: &Path, preview: &Value) -> Result<(), ApiError> {
    let pid = preview
        .get("preview_id")
        .and_then(Value::as_str)
        .unwrap_or("");
    let path = match_preview_path(data_dir, pid);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| ApiError::internal(e.to_string()))?;
    }
    let text =
        serde_json::to_string_pretty(preview).map_err(|e| ApiError::internal(e.to_string()))?;
    atomic_write_text(&path, &text, 0o600).map_err(|e| ApiError::internal(e.to_string()))?;
    Ok(())
}

pub fn load_match_preview(
    data_dir: &Path,
    preview_id: &str,
    allow_expired: bool,
) -> Result<Value, ApiError> {
    let pid = preview_id.trim();
    if pid.is_empty() || pid.contains('/') || pid.contains('\\') || pid.contains("..") {
        return Err(ApiError::preview_not_found("preview_id is required."));
    }
    let path = match_preview_path(data_dir, pid);
    if !path.is_file() {
        return Err(ApiError::preview_not_found("Match preview not found."));
    }
    let raw = fs::read_to_string(&path)
        .map_err(|_| ApiError::preview_not_found("Match preview not found."))?;
    let preview: Value = serde_json::from_str(&raw)
        .map_err(|_| ApiError::preview_not_found("Match preview not found."))?;
    if !allow_expired
        && match_is_expired(
            preview
                .get("expires_at")
                .and_then(Value::as_str)
                .unwrap_or(""),
        )
    {
        return Err(ApiError::preview_expired("Match preview has expired."));
    }
    Ok(preview)
}

pub fn create_match_preview_record(
    data_dir: &Path,
    game_ids: Option<Vec<String>>,
    import_batch_id: Option<&str>,
) -> Result<Value, ApiError> {
    let has_games = game_ids.as_ref().is_some_and(|v| !v.is_empty());
    let batch = import_batch_id.unwrap_or("").trim();
    let has_batch = !batch.is_empty();
    if has_games == has_batch {
        return Err(ApiError::bad_request(
            "Provide either game_ids or import_batch_id.",
        ));
    }
    let preview_id = Uuid::new_v4().simple().to_string();
    let preview = json!({
        "preview_id": preview_id,
        "revision": 1,
        "expires_at": match_expires_at(MATCH_PREVIEW_TTL_HOURS),
        "state": "queued",
        "job_id": Value::Null,
        "game_ids": game_ids.unwrap_or_default(),
        "import_batch_id": batch,
        "counts": {
            "auto_applied": 0,
            "exact_review": 0,
            "likely": 0,
            "possible": 0,
            "unmatched": 0,
        },
        "items": {},
        "decisions": {},
        "auto_applied": [],
    });
    save_match_preview(data_dir, &preview)?;
    Ok(preview)
}

pub fn match_preview_document(preview: &Value) -> Value {
    json!({
        "preview_id": preview["preview_id"],
        "revision": preview["revision"],
        "state": preview.get("state").cloned().unwrap_or_else(|| json!("ready")),
        "job_id": preview.get("job_id").cloned().unwrap_or(Value::Null),
        "counts": preview.get("counts").cloned().unwrap_or_else(|| json!({})),
    })
}

fn encode_match_cursor(preview_id: &str, revision: i64, offset: usize) -> String {
    let raw = json!({
        "preview_id": preview_id,
        "revision": revision,
        "offset": offset,
    })
    .to_string();
    URL_SAFE_NO_PAD.encode(raw.as_bytes())
}

fn decode_match_cursor(cursor: &str, preview_id: &str, revision: i64) -> Result<usize, ApiError> {
    if cursor.is_empty() {
        return Ok(0);
    }
    let bytes = URL_SAFE_NO_PAD
        .decode(cursor.trim_end_matches('='))
        .map_err(|_| ApiError::preview_stale("Cursor is stale."))?;
    let payload: Value =
        serde_json::from_slice(&bytes).map_err(|_| ApiError::preview_stale("Cursor is stale."))?;
    if payload.get("preview_id").and_then(Value::as_str) != Some(preview_id)
        || payload.get("revision").and_then(Value::as_i64).unwrap_or(0) != revision
    {
        return Err(ApiError::preview_stale("Cursor is stale."));
    }
    Ok(payload.get("offset").and_then(Value::as_u64).unwrap_or(0) as usize)
}

pub fn list_match_preview_items(
    data_dir: &Path,
    preview_id: &str,
    cursor: Option<&str>,
    limit: usize,
    match_class: Option<&str>,
) -> Result<Value, ApiError> {
    let preview = load_match_preview(data_dir, preview_id, false)?;
    let revision = preview.get("revision").and_then(Value::as_i64).unwrap_or(1);
    let offset = decode_match_cursor(cursor.unwrap_or(""), preview_id, revision)?;
    let mut items: Vec<Value> = preview
        .get("items")
        .and_then(Value::as_object)
        .map(|m| m.values().cloned().collect())
        .unwrap_or_default();
    if let Some(mc) = match_class.filter(|s| !s.is_empty()) {
        items.retain(|it| it.get("class").and_then(Value::as_str) == Some(mc));
    }
    let total = items.len();
    let start = offset.min(total);
    let end = (start + limit).min(total);
    let page = items[start..end].to_vec();
    let next_offset = start + page.len();
    let next_cursor = if next_offset < total {
        Value::String(encode_match_cursor(preview_id, revision, next_offset))
    } else {
        Value::Null
    };
    Ok(json!({
        "preview_id": preview_id,
        "revision": revision,
        "cursor": cursor.map(|s| Value::String(s.to_owned())).unwrap_or(Value::Null),
        "next_cursor": next_cursor,
        "items": page,
    }))
}

pub fn selected_games_for_preview(state: &Value, preview: &Value) -> Vec<Value> {
    let games = state
        .get("games")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    if let Some(ids) = preview
        .get("game_ids")
        .and_then(Value::as_array)
        .filter(|a| !a.is_empty())
    {
        let wanted: HashSet<String> = ids
            .iter()
            .filter_map(|v| v.as_str().map(ToOwned::to_owned))
            .collect();
        return games
            .iter()
            .filter(|g| {
                g.get("game_id")
                    .and_then(Value::as_str)
                    .is_some_and(|id| wanted.contains(id))
            })
            .cloned()
            .collect();
    }
    let batch = preview
        .get("import_batch_id")
        .and_then(Value::as_str)
        .unwrap_or("");
    games
        .iter()
        .filter(|g| g.get("import_batch_id").and_then(Value::as_str) == Some(batch))
        .cloned()
        .collect()
}

pub fn run_match_preview_job<F>(
    data_dir: &Path,
    database_path: &Path,
    preview_id: &str,
    state: &Value,
    cancel: Option<&AtomicBool>,
    mut set_auto_id: F,
) -> Result<Value, ApiError>
where
    F: FnMut(&str, &str),
{
    let mut preview = load_match_preview(data_dir, preview_id, true)?;
    let games = selected_games_for_preview(state, &preview);
    let mut counts = BTreeMap::from([
        ("auto_applied".to_owned(), 0usize),
        ("exact_review".to_owned(), 0usize),
        ("likely".to_owned(), 0usize),
        ("possible".to_owned(), 0usize),
        ("unmatched".to_owned(), 0usize),
    ]);
    let mut items = preview
        .get("items")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let mut auto_applied: Vec<Value> = preview
        .get("auto_applied")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();

    for game in &games {
        if cancel.is_some_and(|c| c.load(Ordering::Acquire)) {
            break;
        }
        let rejected: Vec<String> = game
            .get("rejected_launchbox_ids")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(|v| v.as_str().map(ToOwned::to_owned))
                    .collect()
            })
            .unwrap_or_default();
        let (match_class, item, auto_id) =
            classify_game_match(database_path, game, &rejected).map_err(ApiError::internal)?;
        let gid = game.get("game_id").and_then(Value::as_str).unwrap_or("");
        if match_class == "auto"
            && let Some(aid) = auto_id
        {
            set_auto_id(gid, &aid);
            *counts.entry("auto_applied".to_owned()).or_insert(0) += 1;
            auto_applied.push(Value::String(gid.to_owned()));
        } else if let Some(it) = item {
            let cls = it
                .get("class")
                .and_then(Value::as_str)
                .unwrap_or("unmatched")
                .to_owned();
            items.insert(gid.to_owned(), it);
            *counts.entry(cls).or_insert(0) += 1;
        }
    }

    preview["counts"] = json!(counts);
    preview["items"] = Value::Object(items);
    preview["auto_applied"] = Value::Array(auto_applied);
    preview["state"] = Value::String("ready".to_owned());
    save_match_preview(data_dir, &preview)?;
    Ok(preview)
}

pub fn apply_match_decisions(
    data_dir: &Path,
    preview_id: &str,
    decision_items: &[Value],
) -> Result<(Value, Vec<(String, String)>), ApiError> {
    let mut preview = load_match_preview(data_dir, preview_id, false)?;
    if decision_items.is_empty() {
        return Err(ApiError::bad_request("items must be a non-empty array."));
    }
    if decision_items.len() > MAX_MATCH_DECISION_BATCH {
        return Err(ApiError::bad_request(format!(
            "items length must be <= {MAX_MATCH_DECISION_BATCH}."
        )));
    }
    let mut accepted = 0usize;
    let mut chosen = 0usize;
    let mut skipped = 0usize;
    let mut never = 0usize;
    let mut decisions = preview
        .get("decisions")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let items_map = preview
        .get("items")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let mut never_rejections = Vec::new();

    for entry in decision_items {
        let gid = entry.get("game_id").and_then(Value::as_str).unwrap_or("");
        let action = entry.get("action").and_then(Value::as_str).unwrap_or("");
        let mut db_id = entry.get("database_id").and_then(|v| match v {
            Value::Null => None,
            Value::String(s) if s.is_empty() => None,
            Value::String(s) => Some(s.clone()),
            other => Some(other.to_string()),
        });
        if !matches!(action, "accept" | "choose" | "skip" | "never") {
            return Err(ApiError::bad_request("Invalid decision action."));
        }
        if action == "choose" && db_id.is_none() {
            return Err(ApiError::bad_request("database_id is required for choose."));
        }
        if matches!(action, "skip" | "never") && db_id.is_some() {
            return Err(ApiError::bad_request(
                "database_id must be null for skip/never.",
            ));
        }
        let Some(item) = items_map.get(gid) else {
            return Err(ApiError::bad_request(format!("Unknown game_id {gid}.")));
        };
        if action == "accept" {
            let cls = item.get("class").and_then(Value::as_str).unwrap_or("");
            if !matches!(cls, "exact_review" | "likely") {
                return Err(ApiError::bad_request(
                    "accept is only allowed for exact_review or likely.",
                ));
            }
            db_id = item
                .get("proposed")
                .and_then(|p| p.get("database_id"))
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
        }
        if action == "never" {
            let proposed_id = item
                .get("proposed")
                .and_then(|p| p.get("database_id"))
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            decisions.insert(
                gid.to_owned(),
                json!({"action": action, "database_id": proposed_id}),
            );
            if let Some(pid) = proposed_id {
                never_rejections.push((gid.to_owned(), pid));
            }
            never += 1;
            continue;
        }
        decisions.insert(
            gid.to_owned(),
            json!({"action": action, "database_id": db_id}),
        );
        match action {
            "accept" => accepted += 1,
            "choose" => chosen += 1,
            _ => skipped += 1,
        }
    }
    let next_rev = preview.get("revision").and_then(Value::as_i64).unwrap_or(1) + 1;
    preview["decisions"] = Value::Object(decisions);
    preview["revision"] = json!(next_rev);
    save_match_preview(data_dir, &preview)?;
    Ok((
        json!({
            "accepted": accepted,
            "chosen": chosen,
            "skipped": skipped,
            "never": never,
        }),
        never_rejections,
    ))
}

pub fn apply_preview_fields(
    game: &mut Map<String, Value>,
    record: &Value,
    database_id: &str,
    field_allow_list: Option<&[String]>,
    replace_existing: bool,
) -> Result<(), ApiError> {
    let default_fields: Vec<String> = [
        "title",
        "platform",
        "year",
        "developer",
        "publisher",
        "genre",
        "esrb",
        "description",
    ]
    .iter()
    .map(|s| (*s).to_owned())
    .collect();
    let allow = field_allow_list.unwrap_or(&default_fields);
    for field in allow {
        let (target, source) = match field.as_str() {
            "title" => ("name", "name"),
            "platform" => ("platform", "platform"),
            "year" => ("year", "release_date"),
            "developer" => ("developer", "developer"),
            "publisher" => ("publisher", "publisher"),
            "genre" => ("genre", "genre"),
            "esrb" => ("esrb", "esrb"),
            "description" => ("description", "overview"),
            _ => continue,
        };
        let value = if field == "year" {
            year_from_record(record)
        } else {
            record
                .get(source)
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .map(ToOwned::to_owned)
        };
        if let Some(val) = value {
            let existing_empty = game
                .get(target)
                .map(|v| v.is_null() || v.as_str() == Some(""))
                .unwrap_or(true);
            if replace_existing || existing_empty {
                game.insert(target.to_owned(), Value::String(val));
            }
        }
    }
    let id_empty = game
        .get("launchbox_db_id")
        .and_then(Value::as_str)
        .is_none_or(str::is_empty);
    if replace_existing || id_empty {
        game.insert(
            "launchbox_db_id".into(),
            Value::String(database_id.to_owned()),
        );
    }
    Ok(())
}

pub fn load_media_images(
    database_path: &Path,
    database_id: i64,
    region_priority: Option<&[String]>,
) -> Result<Vec<Value>, String> {
    let conn = open_db(database_path)?;
    let mut stmt = conn
        .prepare("SELECT database_id, filename, type, region FROM images WHERE database_id = ?1")
        .map_err(|e| e.to_string())?;
    let rows: Vec<Value> = stmt
        .query_map([database_id], |r| {
            Ok(json!({
                "database_id": r.get::<_, i64>(0)?,
                "filename": r.get::<_, String>(1).unwrap_or_default(),
                "type": r.get::<_, String>(2).unwrap_or_default(),
                "region": r.get::<_, String>(3).unwrap_or_default(),
            }))
        })
        .map_err(|e| e.to_string())?
        .flatten()
        .collect();
    let default_pri: Vec<String> = REGION_PRIORITY_DEFAULT
        .iter()
        .map(|s| (*s).to_owned())
        .collect();
    let pri = region_priority.unwrap_or(&default_pri);
    Ok(sort_images_by_region(&rows, pri))
}

pub fn download_lbdb_image(filename: &str, destination: &Path) -> Result<String, String> {
    let safe_name = Path::new(filename)
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| filename.to_owned());
    let url = format!("{IMAGE_URL}{safe_name}");
    download_file_sync(&url, destination, &["image/"], 32 * 1024 * 1024, 30, &[])
        .map(|p| p.to_string_lossy().into_owned())
}

pub fn find_archive_manual(game: &Map<String, Value>, media_root: &Path) -> Option<String> {
    let source = game.get("path").and_then(Value::as_str).unwrap_or("");
    if source.is_empty() {
        return None;
    }
    let archive = Path::new(source);
    let ext = archive
        .extension()
        .and_then(|e| e.to_str())
        .map(|s| format!(".{}", s.to_ascii_lowercase()))
        .unwrap_or_default();
    if !archive.is_file() || !matches!(ext.as_str(), ".zip" | ".7z" | ".rar") {
        return None;
    }
    let cache_root = media_root
        .parent()
        .and_then(Path::parent)
        .unwrap_or(media_root)
        .join("cache")
        .join("archives");
    let extracted = extract_game(archive, &cache_root, "").ok()?;
    let extraction_dir = extracted.parent()?;
    let mut candidates: Vec<PathBuf> = Vec::new();
    collect_manual_files(extraction_dir, &mut candidates);
    if candidates.is_empty() {
        return None;
    }
    candidates.truncate(8);
    candidates.sort_by(|a, b| {
        let rank = |p: &Path| {
            let name = p
                .file_name()
                .map(|n| n.to_string_lossy().to_lowercase())
                .unwrap_or_default();
            let tier = if name == "manual.pdf" {
                0usize
            } else if name == "manual.txt" {
                1
            } else {
                2
            };
            let mtime = fs::metadata(p)
                .and_then(|m| m.modified())
                .ok()
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_secs())
                .unwrap_or(0);
            (
                tier,
                if tier == 2 { name.len() } else { 0 },
                std::cmp::Reverse(mtime),
            )
        };
        rank(a).cmp(&rank(b))
    });
    let chosen = &candidates[0];
    let suffix = chosen
        .extension()
        .and_then(|e| e.to_str())
        .map(|s| format!(".{}", s.to_ascii_lowercase()))
        .unwrap_or_else(|| ".pdf".to_owned());
    let dest = media_root.join(format!("manual{suffix}"));
    if let Some(p) = dest.parent() {
        let _ = fs::create_dir_all(p);
    }
    fs::copy(chosen, &dest).ok()?;
    Some(dest.to_string_lossy().into_owned())
}

fn collect_manual_files(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let p = entry.path();
        if fs::symlink_metadata(&p)
            .map(|m| m.file_type().is_symlink())
            .unwrap_or(true)
        {
            continue;
        }
        if p.is_dir() {
            collect_manual_files(&p, out);
        } else if p.is_file() {
            let name = p.file_name().and_then(|n| n.to_str()).unwrap_or("");
            if name == ".complete" {
                continue;
            }
            let ext = p
                .extension()
                .and_then(|e| e.to_str())
                .map(|s| format!(".{}", s.to_ascii_lowercase()))
                .unwrap_or_default();
            if MANUAL_SUFFIXES.contains(&ext.as_str()) {
                out.push(p);
            }
        }
    }
}

pub fn apply_game_metadata(
    game: &mut Map<String, Value>,
    database_path: &Path,
    database_id: i64,
    media_types: &[String],
    media_root: &Path,
    overwrite: bool,
    region_priority: Option<&[String]>,
) -> Result<(), String> {
    let record = load_metadata_record(database_path, database_id)?;
    let fields = [
        ("name", "name"),
        ("platform", "platform"),
        ("year", "release_date"),
        ("developer", "developer"),
        ("publisher", "publisher"),
        ("genre", "genre"),
        ("description", "overview"),
        ("series", "series"),
    ];
    for (target, source) in fields {
        let value = if target == "year" {
            year_from_record(&record)
        } else {
            record
                .get(source)
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .map(ToOwned::to_owned)
        };
        if let Some(v) = value {
            let missing = game
                .get(target)
                .map(|x| x.is_null() || x.as_str() == Some(""))
                .unwrap_or(true);
            if overwrite || missing {
                game.insert(target.to_owned(), Value::String(v));
            }
        }
    }
    if overwrite
        || game
            .get("esrb")
            .and_then(Value::as_str)
            .is_none_or(str::is_empty)
    {
        apply_esrb_from_record(game, &record);
    }
    if let Some(mp) = record
        .get("max_players")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        && (overwrite
            || game
                .get("max_players")
                .and_then(Value::as_str)
                .is_none_or(str::is_empty))
    {
        game.insert("max_players".into(), Value::String(mp.to_owned()));
    }
    game.insert(
        "launchbox_db_id".into(),
        Value::String(database_id.to_string()),
    );

    let images = load_media_images(database_path, database_id, region_priority)?;
    let mut by_type: HashMap<String, Vec<Value>> = HashMap::new();
    for img in images {
        let t = img
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        by_type.entry(t).or_default().push(img);
    }
    let root = media_root.join(database_id.to_string());
    let mut tasks: Vec<(String, PathBuf, String, Option<usize>)> = Vec::new();
    let mut media_notes: Vec<Value> = game
        .get("_media_notes")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();

    for mtype in media_types {
        if mtype == "screenshots" {
            let empty = game
                .get("screenshots")
                .and_then(Value::as_array)
                .is_none_or(Vec::is_empty);
            if (overwrite || empty)
                && let Some(cands) = by_type.get("Screenshot - Gameplay")
            {
                for (i, item) in cands.iter().take(12).enumerate() {
                    let fn_s = item.get("filename").and_then(Value::as_str).unwrap_or("");
                    let ext = Path::new(fn_s)
                        .extension()
                        .and_then(|e| e.to_str())
                        .map(|s| format!(".{s}"))
                        .unwrap_or_else(|| ".jpg".to_owned());
                    tasks.push((
                        "screenshots".to_owned(),
                        root.join(format!("screenshot-{}{ext}", i + 1)),
                        fn_s.to_owned(),
                        Some(i + 1),
                    ));
                }
            }
        } else if mtype == "manual" {
            let empty = game
                .get("manual")
                .and_then(Value::as_str)
                .is_none_or(str::is_empty);
            if overwrite || empty {
                if let Some(cand) = find_archive_manual(game, &root) {
                    game.insert("manual".into(), Value::String(cand));
                } else {
                    media_notes.push(Value::String(
                        "manual: no manual in this archive".to_owned(),
                    ));
                }
            }
        } else {
            let empty = game
                .get(mtype)
                .and_then(Value::as_str)
                .is_none_or(str::is_empty);
            if (overwrite || empty)
                && let Some((_, lbdb_types)) =
                    media_type_map().iter().find(|(k, _)| *k == mtype.as_str())
            {
                for lbdb_type in *lbdb_types {
                    if let Some(cands) = by_type.get(*lbdb_type)
                        && let Some(first) = cands.first()
                    {
                        let fn_s = first.get("filename").and_then(Value::as_str).unwrap_or("");
                        let ext = Path::new(fn_s)
                            .extension()
                            .and_then(|e| e.to_str())
                            .map(|s| format!(".{s}"))
                            .unwrap_or_else(|| ".jpg".to_owned());
                        tasks.push((
                            mtype.clone(),
                            root.join(format!("{mtype}{ext}")),
                            fn_s.to_owned(),
                            None,
                        ));
                        break;
                    }
                }
            }
        }
    }
    if !media_notes.is_empty() {
        game.insert("_media_notes".into(), Value::Array(media_notes));
    }

    if tasks.is_empty() {
        return Ok(());
    }

    let mut failures = Vec::new();
    let mut screenshots = Vec::new();
    for (mtype, dest, fn_s, idx) in tasks {
        match download_lbdb_image(&fn_s, &dest) {
            Ok(path) => {
                if mtype == "screenshots" {
                    screenshots.push((idx.unwrap_or(0), path));
                } else {
                    game.insert(mtype, Value::String(path));
                }
            }
            Err(err) => failures.push(format!("{mtype}: {err}")),
        }
    }
    if !failures.is_empty() {
        let details = failures
            .iter()
            .take(3)
            .cloned()
            .collect::<Vec<_>>()
            .join(", ");
        return Err(format!("failed to download selected media: {details}"));
    }
    if !screenshots.is_empty() {
        screenshots.sort_by_key(|(i, _)| *i);
        game.insert(
            "screenshots".into(),
            Value::Array(
                screenshots
                    .into_iter()
                    .map(|(_, p)| Value::String(p))
                    .collect(),
            ),
        );
    }
    Ok(())
}

pub fn lbdb_media_candidates(database_path: &Path, database_id: i64) -> Result<Vec<Value>, String> {
    let mut kind_for_type: HashMap<&str, &str> = HashMap::new();
    for (kind, types) in media_type_map() {
        for t in *types {
            kind_for_type.entry(*t).or_insert(*kind);
        }
    }
    let conn = open_db(database_path)?;
    let mut stmt = conn
        .prepare(
            "SELECT type, filename, region FROM images WHERE database_id = ?1 ORDER BY type, region",
        )
        .map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map([database_id], |r| {
            Ok((
                r.get::<_, String>(0).unwrap_or_default(),
                r.get::<_, String>(1).unwrap_or_default(),
                r.get::<_, String>(2).unwrap_or_default(),
            ))
        })
        .map_err(|e| e.to_string())?;
    let mut out = Vec::new();
    for (img_type, filename, region) in rows.flatten() {
        if !filename.is_empty()
            && let Some(&kind) = kind_for_type.get(img_type.as_str())
        {
            out.push(json!({
                "kind": kind,
                "type": img_type,
                "url": format!("{IMAGE_URL}{filename}"),
                "region": region,
            }));
        }
    }
    Ok(out)
}
