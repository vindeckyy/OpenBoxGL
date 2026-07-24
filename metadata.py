"""LaunchBox Games Database sync, search, and media download. Independent open-source implementation not affiliated with LaunchBox or Unbroken Software, LLC."""

import re
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen
from xml.etree import ElementTree


DATABASE_URL = "https://gamesdb.launchbox-app.com/Metadata.zip"
IMAGE_URL = "https://gamesdb.launchbox-app.com/games/images/"


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
    request = Request(DATABASE_URL, headers={"User-Agent":"OpenBox/1"})
    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".zip", delete=False) as temporary:
        archive = Path(temporary.name)
        with opener(request, timeout=120) as response:
            shutil.copyfileobj(response, temporary)
    try:
        build_database(archive, destination)
    finally:
        archive.unlink(missing_ok=True)


def search_games(database_path, title, platform="", limit=20):
    database = sqlite3.connect(database_path)
    database.row_factory = sqlite3.Row
    query = normalized(title)
    rows = database.execute(
        """SELECT * FROM games
           WHERE normalized = ? OR normalized LIKE ?
           ORDER BY (normalized = ?) DESC, (lower(platform) = lower(?)) DESC, length(name)
           LIMIT ?""",
        (query, f"%{query}%", query, platform, limit),
    ).fetchall()
    database.close()
    return [dict(row) for row in rows]


def download_image(filename, destination, opener=urlopen):
    filename = Path(filename).name
    request = Request(IMAGE_URL + filename, headers={"User-Agent":"OpenBox/1"})
    with opener(request, timeout=30) as response:
        if not response.headers.get_content_type().startswith("image/"):
            raise ValueError("The metadata server did not return an image.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        with temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        temporary.replace(destination)
    return str(destination)


def apply_game_metadata(game, database_path, database_id, media_types, media_root, overwrite=False, opener=urlopen):
    database = sqlite3.connect(database_path)
    database.row_factory = sqlite3.Row
    record = database.execute("SELECT * FROM games WHERE database_id = ?", (int(database_id),)).fetchone()
    if not record:
        database.close()
        raise ValueError("Metadata game not found.")
    record = dict(record)
    fields = {
        "name":"name", "platform":"platform", "year":"release_date", "developer":"developer",
        "publisher":"publisher", "genre":"genre", "description":"overview", "series":"series",
    }
    for target, source in fields.items():
        if record[source] and (overwrite or not game.get(target)):
            game[target] = record[source]
    game["launchbox_db_id"] = str(database_id)
    images = [dict(row) for row in database.execute(
        """SELECT * FROM images WHERE database_id = ?
           ORDER BY CASE region WHEN 'North America' THEN 0 WHEN 'World' THEN 1 ELSE 2 END, filename""",
        (int(database_id),),
    )]
    database.close()
    root = Path(media_root) / str(database_id)
    if "cover" in media_types and (overwrite or not game.get("cover")):
        image = next((item for item in images if item["type"] == "Box - Front"), None)
        if image:
            game["cover"] = download_image(image["filename"], root / f"cover{Path(image['filename']).suffix}", opener)
    if "background" in media_types and (overwrite or not game.get("background")):
        image = next((item for item in images if item["type"] == "Fanart - Background"), None)
        if image:
            game["background"] = download_image(image["filename"], root / f"background{Path(image['filename']).suffix}", opener)
    if "screenshots" in media_types and (overwrite or not game.get("screenshots")):
        screenshots = [item for item in images if item["type"] == "Screenshot - Gameplay"][:12]
        game["screenshots"] = [
            download_image(item["filename"], root / f"screenshot-{index}{Path(item['filename']).suffix}", opener)
            for index, item in enumerate(screenshots, 1)
        ]
    return game
