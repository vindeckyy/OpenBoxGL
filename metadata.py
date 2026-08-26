"""LaunchBox Games Database sync, search, and media download. Independent open-source implementation not affiliated with LaunchBox or Unbroken Software, LLC."""

import base64
import concurrent.futures
import json
import re
import shutil
import sqlite3
import tempfile
import threading
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.request import urlopen
from xml.etree import ElementTree

from api_errors import BadRequest, PreviewExpired, PreviewNotFound, PreviewStale
from backend_io import atomic_write_text, download_file


DATABASE_URL = "https://gamesdb.launchbox-app.com/Metadata.zip"
IMAGE_URL = "https://images.launchbox-app.com/"
MANUAL_SUFFIXES = (".pdf", ".txt")

_LOCAL = threading.local()


def get_db_connection(database_path):
    """Retrieve or create a thread-local cached SQLite connection for the LBDB."""
    path = Path(database_path).resolve()
    try:
        st = path.stat()
        key = (path, st.st_ino, st.st_mtime_ns)
    except OSError:
        key = (path, 0, 0)
    conns = getattr(_LOCAL, "connections", None)
    if conns is None:
        conns = {}
        _LOCAL.connections = conns

    # Close stale connections for the same path if inode/mtime changed
    for existing_key in list(conns.keys()):
        if existing_key[0] == path and existing_key != key:
            try:
                conns[existing_key].close()
            except Exception:
                pass
            conns.pop(existing_key, None)

    conn = conns.get(key)
    if conn is None:
        conn = sqlite3.connect(str(path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
        except sqlite3.Error:
            pass
        conns[key] = conn
    return conn


# LBDB image type strings mapped to OpenBox media fields, in preference order.
MEDIA_TYPE_MAP = {
    "cover": ("Box - Front", "Box - Front - Reconstructed"),
    "background": ("Fanart - Background",),
    "screenshots": ("Screenshot - Gameplay",),
    "box_back": ("Box - Back", "Box - Back - Reconstructed"),
    "box_spine": ("Box - Spine",),
    "box_3d": ("Box - 3D",),
    "clear_logo": ("Clear Logo",),
    "fanart": ("Fanart - Box - Front", "Fanart - Box - Back"),
    "banner": ("Banner",),
    "icon": ("Icon",),
    "title_screen": ("Screenshot - Game Title",),
    "cart_front": ("Cart - Front",),
    "cart_back": ("Cart - Back",),
    "disc": ("Disc", "Fanart - Disc"),
    "advertisement": ("Advertisement Flyer - Front", "Advertisement Flyer - Back"),
    # No LBDB type ships manual scans; find_archive_manual handles this field.
    "manual": (),
}


# App platform names mapped to LBDB spellings for search ranking; unlisted names pass through.
PLATFORM_ALIASES = {
    "NES": "Nintendo Entertainment System",
    "SNES": "Super Nintendo Entertainment System",
    "Game Boy": "Nintendo Game Boy",
    "Game Boy Color": "Nintendo Game Boy Color",
    "Game Boy Advance": "Nintendo Game Boy Advance",
    "Nintendo 64": "Nintendo 64",
    "Nintendo DS": "Nintendo DS",
    "Nintendo 3DS": "Nintendo 3DS",
    "GameCube": "Nintendo GameCube",
    "Wii": "Nintendo Wii",
    "Wii U": "Nintendo Wii U",
    "Nintendo Switch": "Nintendo Switch",
    "PlayStation": "Sony Playstation",
    "PlayStation 2": "Sony Playstation 2",
    "PlayStation 3": "Sony Playstation 3",
    "PSP": "Sony PSP",
    "PlayStation Vita": "Sony Playstation Vita",
    "Xbox": "Microsoft Xbox",
    "Xbox 360": "Microsoft Xbox 360",
    "Genesis": "Sega Genesis",
    "Sega Saturn": "Sega Saturn",
    "Arcade": "Arcade",
    "ScummVM": "ScummVM",
    "PC": "Windows",
    "MS-DOS": "MS-DOS",
    "DOS": "MS-DOS",
}


def normalized(text):
    text = re.sub(r"\([^)]*(?:USA|Europe|Japan|World|Rev|Disc)[^)]*\)|\[[^]]+]", "", str(text), flags=re.I)
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def element_values(element):
    return {
        child.tag.rsplit("}", 1)[-1]: (child.text or "").strip()
        for child in element
    }


def build_database(metadata_zip, destination):
    destination = Path(destination)
    temporary = destination.with_suffix(".tmp")
    temporary.unlink(missing_ok=True)
    database = sqlite3.connect(temporary)
    database.executescript("""
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
    """)
    with zipfile.ZipFile(metadata_zip) as package:
        for member in package.namelist():
            if not member.casefold().endswith(".xml"):
                continue
            with package.open(member) as source:
                for _, element in ElementTree.iterparse(source, events=("end",)):
                    tag = element.tag.rsplit("}", 1)[-1]
                    if tag not in {"Game", "GameImage"}:
                        continue
                    values = element_values(element)
                    try:
                        database_id = int(values.get("DatabaseID", ""))
                    except ValueError:
                        element.clear()
                        continue
                    if tag == "Game" and values.get("Name"):
                        database.execute(
                            "INSERT OR REPLACE INTO games VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                database_id, values["Name"], normalized(values["Name"]), values.get("Platform", ""),
                                values.get("ReleaseDate", ""), values.get("Developer", ""), values.get("Publisher", ""),
                                values.get("Genres") or values.get("Genre", ""), values.get("Overview", ""),
                                values.get("Series", ""), values.get("ESRB", ""), values.get("MaxPlayers", ""),
                                values.get("Cooperative", ""),
                            ),
                        )
                    elif tag == "GameImage" and values.get("FileName"):
                        database.execute(
                            "INSERT INTO images VALUES (?,?,?,?)",
                            (database_id, Path(values["FileName"]).name, values.get("Type", ""), values.get("Region", "")),
                        )
                    element.clear()
    database.commit()
    database.close()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary.replace(destination)


def sync_database(destination, opener=urlopen):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".zip", delete=False) as temporary:
        archive = Path(temporary.name)
    try:
        download_file(
            DATABASE_URL,
            archive,
            max_bytes=2 * 1024 * 1024 * 1024,
            timeout=120,
            opener=opener,
        )
        build_database(archive, destination)
    finally:
        archive.unlink(missing_ok=True)


def search_games(database_path, title, platform="", limit=20):
    database = get_db_connection(database_path)
    query = normalized(title)
    lbdb_platform = PLATFORM_ALIASES.get(platform, platform)
    rows = database.execute(
        """SELECT * FROM games
           WHERE normalized = ? OR normalized LIKE ?
           ORDER BY (normalized = ?) DESC, (lower(platform) = lower(?)) DESC, length(name)
           LIMIT ?""",
        (query, f"%{query}%", query, lbdb_platform, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def batch_match(database_path, titles):
    """Match many (title, platform) pairs against LBDB in one pass.

    Only exact normalized-title hits qualify; auto-match never guesses fuzzy or partial titles.
    Uses chunked parameterized IN queries and in-memory indexed ranking for high throughput.
    """
    if not titles:
        return {}
    database = get_db_connection(database_path)
    pairs = [(title, platform, normalized(title)) for title, platform in titles]
    queries = sorted({query for _, _, query in pairs if query})
    if not queries:
        return {}

    rows = {}
    CHUNK_SIZE = 500
    for i in range(0, len(queries), CHUNK_SIZE):
        chunk = queries[i : i + CHUNK_SIZE]
        placeholders = ",".join("?" for _ in chunk)
        for row in database.execute(
            f"""SELECT database_id, name, platform, normalized FROM games
               WHERE normalized IN ({placeholders})
               ORDER BY length(name)""",
            chunk,
        ).fetchall():
            rows.setdefault(row["normalized"], []).append(dict(row))

    matched = {}
    for title, platform, query in pairs:
        candidates = rows.get(query)
        if not candidates:
            continue
        lbdb_platform = PLATFORM_ALIASES.get(platform, platform)
        best = max(
            candidates,
            key=lambda item: (
                str(item["platform"]).casefold() == str(lbdb_platform).casefold(),
                -len(str(item["name"])),
            ),
        )
        matched[(title, platform)] = best
    return matched


def best_match(database_path, title, platform=""):
    """Return the single most confident LBDB match for a title, or None."""
    return batch_match(database_path, [(title, platform)]).get((title, platform))


def match_pair_key(title, platform):
    return (str(title or "").strip(), str(platform or ""))


def token_overlap(left, right):
    left_tokens = set(normalized(left).split()) - {""}
    right_tokens = set(normalized(right).split()) - {""}
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def title_similarity(left, right):
    left_norm = normalized(left)
    right_norm = normalized(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    return SequenceMatcher(None, left_norm, right_norm).ratio()


def platform_exact_match(game_platform, db_platform):
    left = PLATFORM_ALIASES.get(str(game_platform or ""), str(game_platform or ""))
    return str(left).casefold() == str(db_platform or "").casefold()


def _match_score(title, platform, candidate):
    reasons = []
    title_sim = title_similarity(title, candidate.get("name", ""))
    overlap = token_overlap(title, candidate.get("name", ""))
    exact_platform = platform_exact_match(platform, candidate.get("platform", ""))
    if title_sim == 1.0:
        reasons.append("exact normalized title")
    elif title_sim >= 0.9:
        reasons.append("high title similarity")
    if overlap >= 0.8:
        reasons.append("strong token overlap")
    if exact_platform:
        reasons.append("exact platform")
    else:
        reasons.append("platform mismatch")
    return {
        "title_similarity": round(title_sim, 4),
        "token_overlap": round(overlap, 4),
        "platform_exact": exact_platform,
        "reasons": reasons,
    }


def _year_from_record(record):
    value = str(record.get("release_date") or "").strip()
    if not value:
        return None
    return value[:4] if len(value) >= 4 else value


def _media_categories_for_game(game):
    categories = []
    for media_type in MEDIA_TYPE_MAP:
        if media_type == "screenshots":
            if game.get("screenshots"):
                categories.append(media_type)
        elif game.get(media_type):
            categories.append(media_type)
    return categories


def _media_categories_for_record(database, database_id):
    rows = database.execute(
        "SELECT DISTINCT type FROM images WHERE database_id = ?",
        (int(database_id),),
    ).fetchall()
    categories = set()
    reverse = {}
    for field, types in MEDIA_TYPE_MAP.items():
        for lbdb_type in types:
            reverse.setdefault(lbdb_type, field)
    for row in rows:
        mapped = reverse.get(row["type"])
        if mapped:
            categories.add(mapped)
    return sorted(categories)


def _current_snapshot(game):
    return {
        "title": str(game.get("name") or ""),
        "platform": game.get("platform"),
        "year": game.get("year"),
        "developer": game.get("developer"),
        "publisher": game.get("publisher"),
        "genre": game.get("genre"),
        "esrb": game.get("esrb"),
        "description": game.get("description"),
        "media_categories": _media_categories_for_game(game),
    }


def _proposed_snapshot(database_path, database_id):
    _, record = _load_metadata_record(database_path, database_id)
    database = get_db_connection(database_path)
    return {
        "database_id": str(database_id),
        "title": str(record.get("name") or ""),
        "platform": record.get("platform"),
        "year": _year_from_record(record),
        "developer": record.get("developer"),
        "publisher": record.get("publisher"),
        "genre": record.get("genre"),
        "esrb": record.get("esrb"),
        "description": record.get("overview"),
        "media_categories": _media_categories_for_record(database, database_id),
    }


def _alternative_entry(candidate, title, platform):
    return {
        "database_id": str(candidate["database_id"]),
        "title": str(candidate.get("name") or ""),
        "platform": candidate.get("platform"),
        "score": _match_score(title, platform, candidate),
    }


def classify_game_match(database_path, game, *, rejected_ids=None):
    title = str(game.get("name") or "").strip()
    platform = str(game.get("platform") or "")
    rejected = {str(item) for item in (rejected_ids or [])}
    if not title:
        return "unmatched", {
            "game_id": str(game.get("game_id")),
            "class": "unmatched",
            "score": {
                "title_similarity": 0.0,
                "token_overlap": 0.0,
                "platform_exact": False,
                "reasons": ["empty title"],
            },
            "current": _current_snapshot(game),
            "proposed": None,
            "alternatives": [],
        }, None

    query = normalized(title)
    database = get_db_connection(database_path)
    exact_rows = [
        dict(row)
        for row in database.execute(
            "SELECT database_id, name, platform, normalized FROM games WHERE normalized = ?",
            (query,),
        ).fetchall()
    ]
    exact_platform_rows = [row for row in exact_rows if platform_exact_match(platform, row.get("platform"))]
    available_exact = [row for row in exact_platform_rows if str(row["database_id"]) not in rejected]

    if len(available_exact) == 1:
        return "auto", None, str(available_exact[0]["database_id"])
    if len(available_exact) > 1:
        proposed = available_exact[0]
        alternatives = [_alternative_entry(row, title, platform) for row in available_exact[1:6]]
        return "exact_review", {
            "game_id": str(game.get("game_id")),
            "class": "exact_review",
            "score": _match_score(title, platform, proposed),
            "current": _current_snapshot(game),
            "proposed": _proposed_snapshot(database_path, proposed["database_id"]),
            "alternatives": alternatives,
        }, None

    candidates = search_games(database_path, title, platform, limit=12)
    ranked = []
    for candidate in candidates:
        if str(candidate["database_id"]) in rejected:
            continue
        score = _match_score(title, platform, candidate)
        ranked.append((score["title_similarity"], score["token_overlap"], candidate, score))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if not ranked:
        return "unmatched", {
            "game_id": str(game.get("game_id")),
            "class": "unmatched",
            "score": {
                "title_similarity": 0.0,
                "token_overlap": 0.0,
                "platform_exact": False,
                "reasons": ["no candidates"],
            },
            "current": _current_snapshot(game),
            "proposed": None,
            "alternatives": [],
        }, None

    best = ranked[0][2]
    best_score = ranked[0][3]
    alternatives = [_alternative_entry(row, title, platform) for row in ranked[1:6]]
    if best_score["title_similarity"] >= 0.90 and best_score["token_overlap"] >= 0.80 and best_score["platform_exact"]:
        match_class = "likely"
    elif best_score["title_similarity"] >= 0.75 and best_score["token_overlap"] >= 0.75:
        match_class = "possible"
    else:
        match_class = "unmatched"
    item = {
        "game_id": str(game.get("game_id")),
        "class": match_class,
        "score": best_score,
        "current": _current_snapshot(game),
        "proposed": _proposed_snapshot(database_path, best["database_id"]) if match_class != "unmatched" else None,
        "alternatives": alternatives if match_class != "unmatched" else [],
    }
    return match_class, item, None


MATCH_PREVIEW_TTL_HOURS = 24
MAX_MATCH_DECISION_BATCH = 200
DEFAULT_MATCH_ITEMS_LIMIT = 50
MAX_MATCH_ITEMS_LIMIT = 200
MAX_REJECTED_IDS = 20


def _match_data_path(data_dir=None):
    if data_dir is not None:
        return data_dir / "library.json" if data_dir.is_dir() else data_dir
    from openbox import DATA

    return DATA


def match_previews_dir(data_dir=None):
    return _match_data_path(data_dir).parent / "match_previews"


def _match_preview_path(preview_id, data_dir=None):
    return match_previews_dir(data_dir) / f"{preview_id}.json"


def _match_now_iso():
    return datetime.now(timezone.utc).isoformat()


def _match_expires_at(hours=MATCH_PREVIEW_TTL_HOURS):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _match_is_expired(expires_at):
    if not expires_at:
        return False
    try:
        return datetime.fromisoformat(expires_at) < datetime.now(timezone.utc)
    except ValueError:
        return False


def save_match_preview(preview, *, data_dir=None):
    preview_id = str(preview["preview_id"])
    path = _match_preview_path(preview_id, data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(preview, indent=2, sort_keys=True))


def load_match_preview(preview_id, *, data_dir=None, allow_expired=False):
    preview_id = str(preview_id or "").strip()
    if not preview_id:
        raise PreviewNotFound("preview_id is required.")
    path = _match_preview_path(preview_id, data_dir)
    if not path.is_file():
        raise PreviewNotFound("Match preview not found.")
    try:
        preview = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise PreviewNotFound("Match preview not found.") from error
    if not allow_expired and _match_is_expired(str(preview.get("expires_at") or "")):
        raise PreviewExpired("Match preview has expired.")
    return preview


def create_match_preview_record(*, game_ids=None, import_batch_id=None, data_dir=None):
    has_games = bool(game_ids)
    has_batch = bool(str(import_batch_id or "").strip())
    if has_games == has_batch:
        raise BadRequest("Provide either game_ids or import_batch_id.")
    preview_id = uuid.uuid4().hex
    preview = {
        "preview_id": preview_id,
        "revision": 1,
        "expires_at": _match_expires_at(),
        "state": "queued",
        "job_id": None,
        "game_ids": list(game_ids or []),
        "import_batch_id": str(import_batch_id or ""),
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
    }
    save_match_preview(preview, data_dir=data_dir)
    return preview


def match_preview_document(preview):
    return {
        "preview_id": preview["preview_id"],
        "revision": preview["revision"],
        "state": preview.get("state", "ready"),
        "job_id": preview.get("job_id"),
        "counts": dict(preview.get("counts") or {}),
    }


def _encode_match_cursor(preview_id, revision, offset):
    raw = json.dumps({"preview_id": preview_id, "revision": revision, "offset": offset}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_match_cursor(cursor, *, preview_id, revision):
    if not cursor:
        return 0
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding).decode())
    except (ValueError, json.JSONDecodeError) as error:
        raise PreviewStale("Cursor is stale.") from error
    if payload.get("preview_id") != preview_id or int(payload.get("revision") or 0) != revision:
        raise PreviewStale("Cursor is stale.")
    return int(payload.get("offset") or 0)


def list_match_preview_items(preview_id, *, cursor=None, limit=DEFAULT_MATCH_ITEMS_LIMIT, match_class=None, data_dir=None):
    preview = load_match_preview(preview_id, data_dir=data_dir)
    revision = int(preview.get("revision") or 1)
    offset = _decode_match_cursor(cursor or "", preview_id=preview_id, revision=revision)
    items = list((preview.get("items") or {}).values())
    if match_class:
        items = [item for item in items if item.get("class") == match_class]
    page = items[offset : offset + limit]
    next_offset = offset + len(page)
    next_cursor = _encode_match_cursor(preview_id, revision, next_offset) if next_offset < len(items) else None
    return {
        "preview_id": preview_id,
        "revision": revision,
        "cursor": cursor,
        "next_cursor": next_cursor,
        "items": page,
    }


def _selected_games(state, preview):
    games = state.get("games") or []
    if preview.get("game_ids"):
        wanted = {str(game_id) for game_id in preview["game_ids"]}
        return [game for game in games if str(game.get("game_id")) in wanted]
    batch_id = str(preview.get("import_batch_id") or "")
    return [game for game in games if str(game.get("import_batch_id") or "") == batch_id]


def run_match_preview_job(preview_id, *, database_path, transact_state, cancel_event=None, data_dir=None, checkpoint=None):
    preview = load_match_preview(preview_id, data_dir=data_dir, allow_expired=True)
    from openbox import load_state

    state = load_state()
    games = _selected_games(state, preview)
    start_index = 0
    if checkpoint and checkpoint.get("last_game_id"):
        for index, game in enumerate(games):
            if str(game.get("game_id")) == str(checkpoint["last_game_id"]):
                start_index = index + 1
                break

    counts = {key: 0 for key in preview["counts"]}
    items = dict(preview.get("items") or {})
    auto_applied = list(preview.get("auto_applied") or [])

    for game in games[start_index:]:
        if cancel_event is not None and cancel_event.is_set():
            break
        rejected_ids = list(game.get("rejected_launchbox_ids") or [])
        match_class, item, auto_id = classify_game_match(database_path, game, rejected_ids=rejected_ids)
        game_id = str(game.get("game_id"))
        if match_class == "auto" and auto_id:
            def mutate(state, game_id=game_id, auto_id=auto_id):
                target = next(g for g in state["games"] if str(g.get("game_id")) == game_id)
                if not target.get("launchbox_db_id"):
                    target["launchbox_db_id"] = auto_id
            transact_state(mutate)
            counts["auto_applied"] += 1
            auto_applied.append(game_id)
        elif item:
            items[game_id] = item
            counts[item["class"]] += 1
        if cancel_event is not None:
            checkpoint = {"last_game_id": game_id}

    preview["counts"] = counts
    preview["items"] = items
    preview["auto_applied"] = auto_applied
    preview["state"] = "ready"
    save_match_preview(preview, data_dir=data_dir)
    return preview


def apply_match_decisions(preview_id, decision_items, *, data_dir=None):
    preview = load_match_preview(preview_id, data_dir=data_dir)
    if not isinstance(decision_items, list) or not decision_items:
        raise BadRequest("items must be a non-empty array.")
    if len(decision_items) > MAX_MATCH_DECISION_BATCH:
        raise BadRequest(f"items length must be <= {MAX_MATCH_DECISION_BATCH}.")
    accepted = chosen = skipped = never = 0
    decisions = dict(preview.get("decisions") or {})
    never_rejections = []
    for entry in decision_items:
        game_id = str(entry.get("game_id") or "")
        action = str(entry.get("action") or "")
        database_id = entry.get("database_id")
        if action not in {"accept", "choose", "skip", "never"}:
            raise BadRequest("Invalid decision action.")
        if action == "choose" and not database_id:
            raise BadRequest("database_id is required for choose.")
        if action in {"skip", "never"} and database_id not in (None, ""):
            raise BadRequest("database_id must be null for skip/never.")
        item = (preview.get("items") or {}).get(game_id)
        if not item:
            raise BadRequest(f"Unknown game_id {game_id}.")
        if action == "accept":
            if item.get("class") not in {"exact_review", "likely"}:
                raise BadRequest("accept is only allowed for exact_review or likely.")
            database_id = (item.get("proposed") or {}).get("database_id")
        if action == "never":
            proposed_id = (item.get("proposed") or {}).get("database_id")
            decisions[game_id] = {"action": action, "database_id": proposed_id}
            if proposed_id:
                never_rejections.append((game_id, str(proposed_id)))
            never += 1
            continue
        decisions[game_id] = {"action": action, "database_id": str(database_id) if database_id else None}
        if action == "accept":
            accepted += 1
        elif action == "choose":
            chosen += 1
        else:
            skipped += 1
    preview["decisions"] = decisions
    preview["revision"] = int(preview.get("revision") or 1) + 1
    save_match_preview(preview, data_dir=data_dir)
    return {
        "accepted": accepted,
        "chosen": chosen,
        "skipped": skipped,
        "never": never,
    }, never_rejections


def persist_never_rejections(transact_state, rejections):
    if not rejections:
        return

    def mutate(state):
        for game_id, database_id in rejections:
            game = next(item for item in state["games"] if str(item.get("game_id")) == game_id)
            existing = [str(item) for item in (game.get("rejected_launchbox_ids") or [])]
            if database_id not in existing:
                existing.append(database_id)
            game["rejected_launchbox_ids"] = existing[-MAX_REJECTED_IDS:]

    transact_state(mutate)


def _apply_fields(game, record, database_id, field_allow_list, media_allow_list, replace_existing):
    if field_allow_list is None:
        field_allow_list = ["title", "platform", "year", "developer", "publisher", "genre", "esrb", "description"]
    mapping = {
        "title": ("name", "name"),
        "platform": ("platform", "platform"),
        "year": ("year", "release_date"),
        "developer": ("developer", "developer"),
        "publisher": ("publisher", "publisher"),
        "genre": ("genre", "genre"),
        "esrb": ("esrb", "esrb"),
        "description": ("description", "overview"),
    }
    for field in field_allow_list:
        target, source = mapping[field]
        value = record.get(source)
        if field == "year":
            value = _year_from_record(record)
        if value and (replace_existing or not game.get(target)):
            game[target] = value
    if replace_existing or not game.get("launchbox_db_id"):
        game["launchbox_db_id"] = str(database_id)
    if media_allow_list:
        raise BadRequest("media_allow_list is not supported in metadata apply; use /api/media/bulk.")


def apply_match_preview(
    preview_id,
    *,
    revision,
    game_ids=None,
    field_allow_list=None,
    media_allow_list=None,
    replace_existing=False,
    database_path,
    data_dir,
    transact_state,
    create_backup,
    data_parent,
    running_map,
    cancel_event=None,
):
    preview = load_match_preview(preview_id, data_dir=data_dir)
    if int(preview.get("revision") or 0) != int(revision):
        raise PreviewStale("Preview revision is stale.")
    if replace_existing and not field_allow_list and not media_allow_list:
        raise BadRequest("replace_existing requires a non-empty allow-list.")
    if media_allow_list:
        raise BadRequest("media_allow_list is not supported in metadata apply; use /api/media/bulk.")
    if replace_existing:
        from openbox import load_state

        create_backup(data_parent, load_state(), ["library", "settings"], keep=0, running_map=running_map)
    decisions = preview.get("decisions") or {}
    wanted = None if game_ids is None else {str(game_id) for game_id in game_ids}
    targets = []
    for game_id, decision in decisions.items():
        if wanted is not None and game_id not in wanted:
            continue
        if decision.get("action") in {"accept", "choose"} and decision.get("database_id"):
            targets.append((game_id, str(decision["database_id"])))
    for game_id, database_id in targets:
        if cancel_event is not None and cancel_event.is_set():
            break
        _, record = _load_metadata_record(database_path, database_id)

        def mutate(state, game_id=game_id, database_id=database_id, record=record):
            game = next(item for item in state["games"] if str(item.get("game_id")) == game_id)
            _apply_fields(
                game,
                record,
                database_id,
                field_allow_list,
                media_allow_list,
                replace_existing,
            )

        transact_state(mutate)
    return preview


def download_image(filename, destination, opener=urlopen):
    filename = Path(filename).name
    return str(download_file(
        IMAGE_URL + filename,
        destination,
        expected_types=("image/",),
        max_bytes=32 * 1024 * 1024,
        timeout=30,
        opener=opener,
    ))


def find_archive_manual(game, media_root, opener=urlopen):
    """Copy a manual (PDF or text) out of the game's own archive, if any.

    Reuses the extraction cache; returns the destination path string or None.
    """
    source = str(game.get("path") or "")
    if not source:
        return None
    archive = Path(source)
    if not archive.is_file() or archive.suffix.casefold() not in {".zip", ".7z", ".rar"}:
        return None
    try:
        from archives import extract_game
        extracted = extract_game(archive, Path(media_root).parent.parent / "cache" / "archives")
        # extract_game returns the chosen launch file; its parent holds the other extracted members.
        extraction_dir = Path(extracted).parent
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile):
        return None
    candidates = [
        path for path in extraction_dir.rglob("*")
        if not path.is_symlink() and path.is_file()
        and path.name != ".complete"
        and path.suffix.casefold() in MANUAL_SUFFIXES
    ]
    if not candidates:
        return None

    def rank(path):
        name = path.name.casefold()
        if name == "manual.pdf":
            return (0, 0, -path.stat().st_mtime)
        if name == "manual.txt":
            return (1, 0, -path.stat().st_mtime)
        return (2, len(path.name), -path.stat().st_mtime)

    chosen = min(candidates[:8], key=rank)
    destination = Path(media_root) / f"manual{chosen.suffix.casefold()}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(chosen, destination)
    return str(destination)


def _load_metadata_record(database_path, database_id):
    """Open the LBDB and fetch the game row; raises if the game is missing.

    Returns (database, record dict).
    """
    database = get_db_connection(database_path) if not isinstance(database_path, sqlite3.Connection) else database_path
    record = database.execute("SELECT * FROM games WHERE database_id = ?", (int(database_id),)).fetchone()
    if not record:
        raise ValueError("Metadata game not found.")
    return database, dict(record)


def _apply_metadata_fields(game, record, database_id, overwrite):
    """Copy metadata fields, ESRB rating, max players, and the LBDB id onto game."""
    fields = {
        "name": "name", "platform": "platform", "year": "release_date", "developer": "developer",
        "publisher": "publisher", "genre": "genre", "description": "overview", "series": "series",
    }
    for target, source in fields.items():
        if record[source] and (overwrite or not game.get(target)):
            game[target] = record[source]
    from parity_premium import apply_esrb_from_record
    if overwrite or not game.get("esrb"):
        apply_esrb_from_record(game, record)
    if record.get("max_players") and (overwrite or not game.get("max_players")):
        game["max_players"] = record["max_players"]
    game["launchbox_db_id"] = str(database_id)


def _load_media_images(database, database_id, region_priority):
    """Load the game's images, region-sorted."""
    from parity_media import REGION_PRIORITY_DEFAULT, sort_images_by_region

    return sort_images_by_region(
        [dict(row) for row in database.execute("SELECT * FROM images WHERE database_id = ?", (int(database_id),))],
        region_priority or REGION_PRIORITY_DEFAULT,
    )


def _group_images_by_type(images):
    """Bucket images by their LBDB type string."""
    images_by_type = {}
    for item in images:
        images_by_type.setdefault(item["type"], []).append(item)
    return images_by_type


def _download_media_for_type(game, media_type, images_by_type, root, overwrite, opener):
    """Download one media field from the LBDB image pool (or the game archive)."""
    if media_type == "screenshots":
        if overwrite or not game.get("screenshots"):
            candidates = images_by_type.get("Screenshot - Gameplay", [])
            downloaded = [
                download_image(item["filename"], root / f"screenshot-{index}{Path(item['filename']).suffix}", opener)
                for index, item in enumerate(candidates[:12], 1)
            ]
            if downloaded:
                game["screenshots"] = downloaded
        return
    if media_type == "manual":
        # LBDB zips ship no manuals; fall back to one inside the game's own archive.
        if overwrite or not game.get("manual"):
            candidate = find_archive_manual(game, root, opener)
            if candidate:
                game["manual"] = candidate
            else:
                game["_media_notes"] = list(game.get("_media_notes") or []) + ["manual: no manual in this archive"]
        return
    if not (overwrite or not game.get(media_type)):
        return
    for lbdb_type in MEDIA_TYPE_MAP.get(media_type, ()):
        candidates = images_by_type.get(lbdb_type, [])
        if not candidates:
            continue
        image = candidates[0]
        game[media_type] = download_image(
            image["filename"], root / f"{media_type}{Path(image['filename']).suffix}", opener
        )
        break


def apply_game_metadata(game, database_path, database_id, media_types, media_root, overwrite=False, opener=urlopen, region_priority=None):
    database, record = _load_metadata_record(database_path, database_id)
    _apply_metadata_fields(game, record, database_id, overwrite)
    images = _load_media_images(database, database_id, region_priority)
    images_by_type = _group_images_by_type(images)
    root = Path(media_root) / str(database_id)

    download_tasks = []
    for media_type in media_types:
        if media_type == "screenshots":
            if overwrite or not game.get("screenshots"):
                candidates = images_by_type.get("Screenshot - Gameplay", [])
                for index, item in enumerate(candidates[:12], 1):
                    target = root / f"screenshot-{index}{Path(item['filename']).suffix}"
                    download_tasks.append(("screenshots", target, item["filename"], index))
        elif media_type == "manual":
            if overwrite or not game.get("manual"):
                candidate = find_archive_manual(game, root, opener)
                if candidate:
                    game["manual"] = candidate
                else:
                    game["_media_notes"] = list(game.get("_media_notes") or []) + ["manual: no manual in this archive"]
        else:
            if overwrite or not game.get(media_type):
                for lbdb_type in MEDIA_TYPE_MAP.get(media_type, ()):
                    candidates = images_by_type.get(lbdb_type, [])
                    if candidates:
                        image = candidates[0]
                        target = root / f"{media_type}{Path(image['filename']).suffix}"
                        download_tasks.append((media_type, target, image["filename"], None))
                        break

    if not download_tasks:
        return game

    def _perform_download(task):
        m_type, dest_path, fn, idx = task
        try:
            res_path = download_image(fn, dest_path, opener)
            return (m_type, res_path, idx, None)
        except Exception as exc:
            return (m_type, None, idx, exc)

    if len(download_tasks) == 1:
        results = [_perform_download(download_tasks[0])]
    else:
        max_workers = min(8, len(download_tasks))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            results = list(executor.map(_perform_download, download_tasks))

    failures = [(m_type, exc) for m_type, res_path, _idx, exc in results if res_path is None and exc is not None]
    if failures:
        details = ", ".join(f"{m_type}: {exc}" for m_type, exc in failures[:3])
        raise ValueError(f"failed to download selected media: {details}")

    screenshots_results = []
    for m_type, res_path, idx, _exc in results:
        if m_type == "screenshots":
            screenshots_results.append((idx, res_path))
        else:
            game[m_type] = res_path

    if screenshots_results:
        screenshots_results.sort(key=lambda t: t[0])
        game["screenshots"] = [p for _, p in screenshots_results]

    return game
