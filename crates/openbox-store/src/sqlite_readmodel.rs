//! Optional SQLite-backed read model via bundled `rusqlite` (FTS5 guaranteed).
//! Mirrors `pkg/state/sqlite_readmodel.py` (ADR 0014, ADR 0032, ADR 0101).

use std::env;
use std::fs;
use std::path::{Path, PathBuf};

use parking_lot::Mutex;
use rusqlite::{Connection, params};
use serde_json::{Value, json};

use crate::json_store::FileSignature;

pub const SQLITE_AUTO_THRESHOLD: usize = 5000;
pub const REBUILD_DEBOUNCE_MS: u64 = 750;
pub const REBUILD_MAX_WAIT_MS: u64 = 5000;

const SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS games (
    game_id TEXT PRIMARY KEY,
    name TEXT,
    title TEXT,
    platform TEXT,
    genre TEXT,
    developer TEXT,
    publisher TEXT,
    series TEXT,
    region TEXT,
    year INTEGER,
    favorite INTEGER DEFAULT 0,
    hidden INTEGER DEFAULT 0,
    installed INTEGER DEFAULT 0,
    broken INTEGER DEFAULT 0,
    portable INTEGER DEFAULT 0,
    play_count INTEGER DEFAULT 0,
    playtime_seconds INTEGER DEFAULT 0,
    last_played TEXT,
    date_added TEXT,
    rating REAL,
    progress TEXT,
    esrb TEXT,
    controller_support TEXT,
    sort_title TEXT,
    description TEXT,
    raw_json TEXT,
    library_order INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_games_platform ON games(platform);
CREATE INDEX IF NOT EXISTS idx_games_genre ON games(genre);
CREATE INDEX IF NOT EXISTS idx_games_favorite ON games(favorite);
CREATE INDEX IF NOT EXISTS idx_games_hidden ON games(hidden);
CREATE INDEX IF NOT EXISTS idx_games_title ON games(title);
"#;

const FTS_SCHEMA: &str = r#"
CREATE VIRTUAL TABLE IF NOT EXISTS games_fts USING fts5(
    game_id UNINDEXED,
    title,
    platform,
    genre,
    developer,
    description,
    content='games',
    content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS games_ai AFTER INSERT ON games BEGIN
    INSERT INTO games_fts(rowid, game_id, title, platform, genre, developer, description)
    VALUES (new.rowid, new.game_id, new.title, new.platform, new.genre, new.developer, new.description);
END;
CREATE TRIGGER IF NOT EXISTS games_ad AFTER DELETE ON games BEGIN
    INSERT INTO games_fts(games_fts, rowid, game_id, title, platform, genre, developer, description)
    VALUES ('delete', old.rowid, old.game_id, old.title, old.platform, old.genre, old.developer, old.description);
END;
CREATE TRIGGER IF NOT EXISTS games_au AFTER UPDATE ON games BEGIN
    INSERT INTO games_fts(games_fts, rowid, game_id, title, platform, genre, developer, description)
    VALUES ('delete', old.rowid, old.game_id, old.title, old.platform, old.genre, old.developer, old.description);
    INSERT INTO games_fts(rowid, game_id, title, platform, genre, developer, description)
    VALUES (new.rowid, new.game_id, new.title, new.platform, new.genre, new.developer, new.description);
END;
"#;

struct Inner {
    conn: Option<Connection>,
    signature: Option<FileSignature>,
    generation: u64,
    enabled: bool,
    disabled_explicitly: bool,
    trusted: bool,
}

pub struct SqliteReadModel {
    db_path: PathBuf,
    inner: Mutex<Inner>,
}

impl SqliteReadModel {
    pub fn new(db_path: impl Into<PathBuf>) -> Self {
        let raw = env::var("OPENBOX_ENABLE_SQLITE_READ")
            .unwrap_or_default()
            .trim()
            .to_lowercase();
        let enabled = matches!(raw.as_str(), "1" | "true" | "yes");
        let disabled_explicitly = matches!(raw.as_str(), "0" | "false" | "no" | "off");
        Self {
            db_path: db_path.into(),
            inner: Mutex::new(Inner {
                conn: None,
                signature: None,
                generation: 0,
                enabled,
                disabled_explicitly,
                trusted: true,
            }),
        }
    }

    pub fn enabled(&self) -> bool {
        self.inner.lock().enabled
    }

    pub fn set_enabled(&self, enabled: bool) {
        self.inner.lock().enabled = enabled;
    }

    pub fn should_auto_enable(&self, game_count: usize) -> bool {
        let mut guard = self.inner.lock();
        if guard.enabled || guard.disabled_explicitly {
            return guard.enabled;
        }
        if game_count >= SQLITE_AUTO_THRESHOLD {
            guard.enabled = true;
        }
        guard.enabled
    }

    pub fn is_fresh(&self, sig: Option<FileSignature>) -> bool {
        let guard = self.inner.lock();
        guard.enabled && guard.trusted && sig.is_some() && guard.signature == sig
    }

    pub fn invalidate(&self) {
        self.inner.lock().signature = None;
    }

    fn discard_files(path: &Path) {
        for suffix in ["", "-wal", "-shm"] {
            let mut s = path.as_os_str().to_os_string();
            s.push(suffix);
            let _ = fs::remove_file(PathBuf::from(s));
        }
    }

    fn open_db(path: &Path) -> rusqlite::Result<Connection> {
        if let Some(parent) = path.parent() {
            let _ = fs::create_dir_all(parent);
        }
        let conn = Connection::open(path)?;
        conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")?;
        conn.execute_batch(SCHEMA)?;
        let _ = conn.execute_batch(FTS_SCHEMA);
        Ok(conn)
    }

    fn ensure_conn<'a>(
        inner: &'a mut Inner,
        db_path: &Path,
    ) -> rusqlite::Result<&'a mut Connection> {
        if inner.conn.is_none() {
            let conn = match Self::open_db(db_path) {
                Ok(c) => c,
                Err(_) => {
                    Self::discard_files(db_path);
                    Self::open_db(db_path)?
                }
            };
            inner.conn = Some(conn);
        }
        Ok(inner.conn.as_mut().expect("opened"))
    }

    pub fn rebuild(&self, state: &Value, sig: Option<FileSignature>) -> rusqlite::Result<()> {
        let mut guard = self.inner.lock();
        if !guard.enabled {
            return Ok(());
        }
        guard.generation += 1;
        let conn = Self::ensure_conn(&mut guard, &self.db_path)?;
        let tx = conn.transaction()?;
        tx.execute("DELETE FROM games", [])?;
        if let Some(games) = state.get("games").and_then(Value::as_array) {
            let mut stmt = tx.prepare_cached(
                "INSERT OR REPLACE INTO games (
                    game_id, name, title, platform, genre, developer, publisher,
                    series, region, year, favorite, hidden, installed, broken,
                    portable, play_count, playtime_seconds, last_played,
                    date_added, rating, progress, esrb, controller_support,
                    sort_title, description, raw_json, library_order
                ) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17,?18,?19,?20,?21,?22,?23,?24,?25,?26,?27)",
            )?;
            let mut order: i64 = 0;
            for game in games {
                let Some(obj) = game.as_object() else {
                    continue;
                };
                let gid = obj
                    .get("game_id")
                    .and_then(Value::as_str)
                    .map(str::trim)
                    .unwrap_or("");
                if gid.is_empty() {
                    continue;
                }
                let s = |k: &str| -> String {
                    obj.get(k).and_then(Value::as_str).unwrap_or("").to_owned()
                };
                let b = |k: &str| -> i64 {
                    i64::from(obj.get(k).and_then(Value::as_bool).unwrap_or(false))
                };
                let name = s("name");
                let year = obj
                    .get("year")
                    .and_then(|v| {
                        v.as_i64()
                            .or_else(|| v.as_str().and_then(|t| t.trim().parse().ok()))
                    })
                    .filter(|&y| y != 0);
                let rating = obj
                    .get("rating")
                    .and_then(Value::as_f64)
                    .filter(|&r| r != 0.0);
                let raw_json = serde_json::to_string(game).unwrap_or_else(|_| "{}".to_owned());
                stmt.execute(params![
                    gid,
                    name,
                    name,
                    s("platform"),
                    s("genre"),
                    s("developer"),
                    s("publisher"),
                    s("series"),
                    s("region"),
                    year,
                    b("favorite"),
                    b("hidden"),
                    b("installed"),
                    b("broken"),
                    b("portable"),
                    obj.get("play_count").and_then(Value::as_i64).unwrap_or(0),
                    obj.get("playtime_seconds")
                        .and_then(Value::as_i64)
                        .unwrap_or(0),
                    s("last_played"),
                    s("date_added"),
                    rating,
                    s("progress"),
                    s("esrb"),
                    s("controller_support"),
                    s("sort_title"),
                    s("description"),
                    raw_json,
                    order,
                ])?;
                order += 1;
            }
        }
        tx.commit()?;
        guard.signature = sig;
        guard.trusted = true;
        Ok(())
    }

    pub fn search(&self, query: &str, limit: usize) -> Vec<Value> {
        let needle = query.to_lowercase();
        if needle.is_empty() {
            return Vec::new();
        }
        let capped = limit.clamp(1, 200);
        let mut guard = self.inner.lock();
        if !guard.enabled {
            return Vec::new();
        }
        let Ok(conn) = Self::ensure_conn(&mut guard, &self.db_path) else {
            return Vec::new();
        };
        let Ok(mut stmt) =
            conn.prepare_cached("SELECT raw_json FROM games ORDER BY library_order, rowid")
        else {
            return Vec::new();
        };
        let Ok(rows) = stmt.query_map([], |r| r.get::<_, String>(0)) else {
            return Vec::new();
        };
        let mut out = Vec::new();
        for raw in rows.flatten() {
            if let Ok(game) = serde_json::from_str::<Value>(&raw) {
                let name = game
                    .get("name")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_lowercase();
                if name.contains(&needle) {
                    out.push(game);
                    if out.len() >= capped {
                        break;
                    }
                }
            }
        }
        out
    }

    pub fn count(&self) -> usize {
        let mut guard = self.inner.lock();
        if !guard.enabled {
            return 0;
        }
        let Ok(conn) = Self::ensure_conn(&mut guard, &self.db_path) else {
            return 0;
        };
        conn.query_row("SELECT COUNT(*) FROM games", [], |r| r.get::<_, i64>(0))
            .map(|n| n.max(0) as usize)
            .unwrap_or(0)
    }

    pub fn status(&self) -> Value {
        let games = self.count();
        let guard = self.inner.lock();
        json!({
            "enabled": guard.enabled,
            "trusted": guard.trusted,
            "fresh": guard.signature.is_some(),
            "pending": false,
            "rebuilding": false,
            "games": games,
        })
    }
}
