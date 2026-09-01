"""SQLite read model for OpenBox (1.7.2).

Optional alternative read path for large libraries. Enabled via
OPENBOX_ENABLE_SQLITE_READ=1 environment variable. When disabled, all
methods are no-ops and the JSON read path is used exclusively.

The SQLite database is rebuilt from the canonical JSON state on demand.
It provides:
  - Full-text search via FTS5 (with LIKE fallback when FTS5 is unavailable).
  - Filtered queries with indexed lookups on platform, genre, favorite, etc.
  - Facet computation via GROUP BY.

The database file lives alongside library.json in the data directory.
It is a read-only projection — all writes go through the canonical JSON
state store, then invalidate() is called to trigger a rebuild on next read.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("openbox.sqlite_readmodel")

_ENABLED = os.environ.get("OPENBOX_ENABLE_SQLITE_READ", "").strip() in ("1", "true", "yes")
_FTS5_AVAILABLE: bool | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    game_id TEXT PRIMARY KEY,
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
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_games_platform ON games(platform);
CREATE INDEX IF NOT EXISTS idx_games_genre ON games(genre);
CREATE INDEX IF NOT EXISTS idx_games_favorite ON games(favorite);
CREATE INDEX IF NOT EXISTS idx_games_hidden ON games(hidden);
CREATE INDEX IF NOT EXISTS idx_games_title ON games(title);
"""

_FTS_SCHEMA = """
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
"""

_FTS_TRIGGERS = """
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
"""


def _check_fts5() -> bool:
    """Check if FTS5 is available in the current SQLite build."""
    global _FTS5_AVAILABLE
    if _FTS5_AVAILABLE is not None:
        return _FTS5_AVAILABLE
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE fts5_test USING fts5(x)")
        conn.close()
        _FTS5_AVAILABLE = True
    except sqlite3.OperationalError:
        _FTS5_AVAILABLE = False
        LOGGER.info("SQLite FTS5 not available; falling back to LIKE search")
    return _FTS5_AVAILABLE


class SqliteReadModel:
    """Optional SQLite-backed read model for the game library."""

    def __init__(self, db_path: Path | str):
        self._db_path = Path(db_path)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._signature: tuple[int, int, int] | None = None
        self._enabled = _ENABLED

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        if _check_fts5():
            conn.executescript(_FTS_SCHEMA)
            conn.executescript(_FTS_TRIGGERS)
        conn.commit()
        self._conn = conn
        return conn

    def rebuild(self, state: dict[str, Any]) -> None:
        """Rebuild the SQLite database from a canonical state dict."""
        if not self._enabled:
            return
        with self._lock:
            conn = self._connect()
            conn.execute("DELETE FROM games")
            games = state.get("games", [])
            if not isinstance(games, list):
                games = []
            rows = []
            for game in games:
                if not isinstance(game, dict):
                    continue
                gid = str(game.get("game_id") or "").strip()
                if not gid:
                    continue
                rows.append((
                    gid,
                    str(game.get("title") or ""),
                    str(game.get("platform") or ""),
                    str(game.get("genre") or ""),
                    str(game.get("developer") or ""),
                    str(game.get("publisher") or ""),
                    str(game.get("series") or ""),
                    str(game.get("region") or ""),
                    int(game.get("year") or 0) if game.get("year") else None,
                    1 if game.get("favorite") else 0,
                    1 if game.get("hidden") else 0,
                    1 if game.get("installed") else 0,
                    1 if game.get("broken") else 0,
                    1 if game.get("portable") else 0,
                    int(game.get("play_count") or 0),
                    int(game.get("playtime_seconds") or 0),
                    str(game.get("last_played") or ""),
                    str(game.get("date_added") or ""),
                    float(game.get("rating") or 0) if game.get("rating") else None,
                    str(game.get("progress") or ""),
                    str(game.get("esrb") or ""),
                    str(game.get("controller_support") or ""),
                    str(game.get("sort_title") or ""),
                    str(game.get("description") or ""),
                    json.dumps(game, ensure_ascii=False),
                ))
            conn.executemany(
                """INSERT OR REPLACE INTO games (
                    game_id, title, platform, genre, developer, publisher,
                    series, region, year, favorite, hidden, installed, broken,
                    portable, play_count, playtime_seconds, last_played,
                    date_added, rating, progress, esrb, controller_support,
                    sort_title, description, raw_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            conn.commit()

    def invalidate(self) -> None:
        """Mark the read model as stale; next query triggers a rebuild."""
        with self._lock:
            self._signature = None

    def ensure_fresh(self, state: dict[str, Any], signature: tuple[int, int, int]) -> None:
        """Rebuild if the signature has changed since the last rebuild."""
        if not self._enabled:
            return
        with self._lock:
            if self._signature == signature:
                return
        # Rebuild outside the check lock to avoid deadlock with rebuild's own lock
        self.rebuild(state)
        with self._lock:
            self._signature = signature

    def query(
        self,
        platform: str | None = None,
        genre: str | None = None,
        favorite: bool | None = None,
        hidden: bool | None = None,
        installed: bool | None = None,
        limit: int = 10000,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Query games with optional filters. Returns list of game dicts."""
        if not self._enabled:
            return []
        conn = self._connect()
        clauses = []
        params: list[Any] = []
        if platform:
            clauses.append("platform = ?")
            params.append(platform)
        if genre:
            clauses.append("genre = ?")
            params.append(genre)
        if favorite is not None:
            clauses.append("favorite = ?")
            params.append(1 if favorite else 0)
        if hidden is not None:
            clauses.append("hidden = ?")
            params.append(1 if hidden else 0)
        if installed is not None:
            clauses.append("installed = ?")
            params.append(1 if installed else 0)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = f"SELECT raw_json FROM games{where} ORDER BY title LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = conn.execute(sql, params).fetchall()
        return [json.loads(r[0]) for r in rows]

    def search(self, query: str, limit: int = 50) -> list[dict[str, Any]]:
        """Full-text search for games. Uses FTS5 if available, LIKE otherwise."""
        if not self._enabled:
            return []
        conn = self._connect()
        if _check_fts5():
            sql = """SELECT g.raw_json FROM games_fts f
                     JOIN games g ON g.rowid = f.rowid
                     WHERE games_fts MATCH ? ORDER BY rank LIMIT ?"""
            try:
                rows = conn.execute(sql, [query + "*", limit]).fetchall()
                return [json.loads(r[0]) for r in rows]
            except sqlite3.OperationalError:
                pass  # fall through to LIKE
        # LIKE fallback
        pattern = f"%{query}%"
        sql = "SELECT raw_json FROM games WHERE title LIKE ? OR platform LIKE ? OR genre LIKE ? LIMIT ?"
        rows = conn.execute(sql, [pattern, pattern, pattern, limit]).fetchall()
        return [json.loads(r[0]) for r in rows]

    def facets(self, field: str, limit: int = 40) -> list[tuple[str, int]]:
        """Compute facets (value, count) for a given field via GROUP BY."""
        if not self._enabled:
            return []
        conn = self._connect()
        allowed = {"platform", "genre", "developer", "publisher", "series", "region", "progress", "esrb"}
        if field not in allowed:
            return []
        sql = f"SELECT {field}, COUNT(*) as cnt FROM games WHERE {field} != '' GROUP BY {field} ORDER BY cnt DESC LIMIT ?"
        rows = conn.execute(sql, [limit]).fetchall()
        return [(r[0], r[1]) for r in rows]

    def count(self) -> int:
        """Return the total number of games in the read model."""
        if not self._enabled:
            return 0
        conn = self._connect()
        return conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def query_parity_check(self, json_games: list[dict[str, Any]]) -> bool:
        """Verify that the SQLite read model returns the same games as the JSON path."""
        if not self._enabled:
            return True
        sqlite_games = self.query(limit=100000)
        json_ids = {str(g.get("game_id") or "") for g in json_games if isinstance(g, dict)}
        sqlite_ids = {str(g.get("game_id") or "") for g in sqlite_games if isinstance(g, dict)}
        return json_ids == sqlite_ids
