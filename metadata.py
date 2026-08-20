"""LaunchBox Games Database sync, search, and media download. Independent open-source implementation not affiliated with LaunchBox or Unbroken Software, LLC."""

import concurrent.futures
import re
import shutil
import sqlite3
import tempfile
import threading
import zipfile
from pathlib import Path
from urllib.request import urlopen
from xml.etree import ElementTree

from backend_io import download_file


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
        matched[title] = best
    return matched


def best_match(database_path, title, platform=""):
    """Return the single most confident LBDB match for a title, or None."""
    return batch_match(database_path, [(title, platform)]).get(title)


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
