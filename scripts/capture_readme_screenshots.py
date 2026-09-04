#!/usr/bin/env python3
"""Populate a fixture library with real LaunchBox art and capture README screenshots.

Usage:
  cd scripts && npm ci    # once; requires Node.js 22.12+ and installs Puppeteer
  python3 scripts/capture_readme_screenshots.py

Writes assets/openbox-screenshot.png, assets/openbox-game-detail.png,
assets/openbox-bigbox.png, and assets/openbox-constellation.png (1920x1080).
Requires LaunchBox metadata under ~/.local/share/openbox-game-launcher/ (or network sync).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import quote
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURE_ROOT = Path("/tmp/openbox-readme-fixtures")
DATA_DIR = FIXTURE_ROOT / "data"
ROMS_DIR = FIXTURE_ROOT / "roms"
ASSETS_DIR = ROOT / "assets"
SOURCE_DB = Path.home() / ".local/share/openbox-game-launcher/metadata/launchbox.db"
EXISTING_MEDIA = Path.home() / ".local/share/openbox-game-launcher/media"
LAUNCHBOX_CDN_URL = "https://images.launchbox-app.com/"

GAMES = [
    ("Hollow Knight", "Windows", 123770, "HollowKnight.exe", "covers/hollow_knight_cover.jpg", "steam/hollow_knight_bg.jpg"),
    ("Elden Ring", "Windows", 208690, "EldenRing.exe", "covers/elden_ring_cover.jpg", "steam/elden_ring_bg.jpg"),
    ("Chrono Trigger", "Super Nintendo Entertainment System", 1255, "ChronoTrigger.smc", "covers/chrono_trigger_real.jpg", None),
    ("Super Metroid", "Super Nintendo Entertainment System", 299, "SuperMetroid.smc", "covers/super_metroid_real.jpg", None),
    ("The Legend of Zelda: Breath of the Wild", "Nintendo Switch", 129189, "BreathOfTheWild.nsz", "covers/zelda_botw_cover.jpg", None),
    ("Half-Life 2", "Windows", 146, "HalfLife2.exe", "covers/half_life_2_cover.jpg", "steam/half_life_2_bg.jpg"),
    ("Super Mario 64", "Nintendo 64", 216, "SuperMario64.z64", "covers/super_mario_64_real.jpg", None),
    ("Metroid Prime", "Nintendo GameCube", 172, "MetroidPrime.iso", "covers/metroid_prime_real.jpg", None),
    ("Red Dead Redemption 2", "Windows", 171258, "RedDeadRedemption2.exe", "covers/rdr2_cover.jpg", "steam/rdr2_bg.jpg"),
    ("Sonic the Hedgehog 2", "Sega Genesis", 108429, "Sonic2.bin", "covers/sonic_2_real.jpg", None),
    ("Mega Man X", "Super Nintendo Entertainment System", 143, "MegaManX.smc", "covers/mega_man_x_real.jpg", None),
    ("Final Fantasy VII", "Sony Playstation", 525, "FinalFantasyVII.bin", "covers/final_fantasy_vii_cover.jpg", None),
]

DISC_PLATFORMS = {
    "Sony Playstation",
    "Sony Playstation 2",
    "Sony Playstation 3",
    "Sony Playstation 4",
    "Sony Playstation 5",
    "Sega CD",
    "Sega Saturn",
    "Panasonic 3DO",
    "Xbox",
    "Xbox 360",
    "Xbox One",
    "PC Engine CD",
}


def ensure_fixture_tree() -> None:
    ROMS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    metadata_dir = DATA_DIR / "metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    destination = metadata_dir / "launchbox.db"
    if not destination.exists():
        if not SOURCE_DB.is_file():
            from metadata import sync_database

            sync_database(destination)
        else:
            shutil.copy2(SOURCE_DB, destination)


def apply_record_metadata(entry: dict, database_id: int, database: Path) -> None:
    import sqlite3

    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    record = conn.execute("SELECT * FROM games WHERE database_id = ?", (database_id,)).fetchone()
    conn.close()
    if not record:
        raise SystemExit(f"Metadata record missing for database_id={database_id}")
    record = dict(record)
    entry["developer"] = record.get("developer") or entry.get("developer", "")
    entry["publisher"] = record.get("publisher") or entry.get("publisher", "")
    entry["description"] = record.get("overview") or entry.get("description", "")
    entry["year"] = (record.get("release_date") or "")[:4]
    entry["launchbox_db_id"] = str(database_id)
    # LaunchBox stores ESRB like "E10+ - Everyone 10+ and up"; trim to the
    # rating code so the detail panel reads "E10+", not the long label.
    entry["esrb"] = (record.get("esrb") or "").split(" - ", 1)[0].strip()
    entry["max_players"] = str(record.get("max_players") or "")
    entry["genre"] = record.get("genre") or entry.get("genre", "")


def file_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def preferred_box_front(database_id: int, database: Path) -> dict | None:
    import sqlite3

    from parity_media import REGION_PRIORITY_DEFAULT, sort_images_by_region

    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    rows = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM images WHERE database_id = ? AND type = 'Box - Front'",
            (database_id,),
        )
    ]
    conn.close()
    images = sort_images_by_region(rows, REGION_PRIORITY_DEFAULT)
    return images[0] if images else None


def download_cdn_image(filename: str, destination: Path) -> Path:
    name = Path(filename).name
    request = Request(LAUNCHBOX_CDN_URL + name, headers={"User-Agent": "OpenBox/1"})
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    with urlopen(request, timeout=60) as response:
        if not response.headers.get_content_type().startswith("image/"):
            raise ValueError(f"LaunchBox CDN did not return an image for {name}")
        with temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
    temporary.replace(destination)
    return destination


def resolve_cover(database_id: int, cover_rel: str | None, destination: Path, database: Path) -> str:
    image = preferred_box_front(database_id, database)
    local = EXISTING_MEDIA / cover_rel if cover_rel else None

    if image:
        cdn_temp = destination.with_name(f"{destination.stem}.cdn{destination.suffix}")
        try:
            download_cdn_image(image["filename"], cdn_temp)
            if local and local.is_file() and file_fingerprint(local) == file_fingerprint(cdn_temp):
                shutil.copy2(local, destination)
            else:
                shutil.copy2(cdn_temp, destination)
        finally:
            cdn_temp.unlink(missing_ok=True)
        return str(destination)

    if local and local.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local, destination)
        return str(destination)

    raise SystemExit(f"No cover art available for database_id={database_id}")


def copy_media(source_relative: str | None, destination: Path) -> str | None:
    if not source_relative:
        return None
    source = EXISTING_MEDIA / source_relative
    if not source.is_file():
        raise SystemExit(f"Missing real media file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return str(destination)


def build_library() -> None:
    database = DATA_DIR / "metadata" / "launchbox.db"
    media_root = DATA_DIR / "media"
    games = []
    for index, (name, platform, database_id, rom_name, cover_rel, background_rel) in enumerate(GAMES):
        rom_path = ROMS_DIR / rom_name
        rom_path.touch(exist_ok=True)
        media_dir = media_root / str(database_id)
        cover = resolve_cover(database_id, cover_rel, media_dir / Path(cover_rel).name, database)
        background = copy_media(background_rel, media_dir / Path(background_rel).name) if background_rel else None
        play_count = (index + 1) * 3
        entry = {
            "name": name,
            "platform": platform,
            "path": str(rom_path),
            "launch": str(rom_path),
            "source": "Screenshot fixture",
            "cover": cover,
            "background": background or "",
            "progress": "Playing" if index % 3 == 0 else "Completed" if index % 3 == 1 else "On Hold",
            "favorite": index in {0, 4, 7},
            "play_count": play_count,
            "rating": 4 + (index % 2),
            "genre": {
                "Windows": "Action",
                "Super Nintendo Entertainment System": "Role-Playing",
                "Nintendo Switch": "Adventure",
                "Nintendo 64": "Platform",
                "Nintendo GameCube": "Action",
                "Sega Genesis": "Platform",
                "Sony Playstation": "Role-Playing",
            }.get(platform, "Action"),
            "region": "Worldwide",
            "play_mode": "Single player",
            "controller_support": "Keyboard + Mouse" if platform == "Windows" else "Yes",
            "disc_count": "1" if platform in DISC_PLATFORMS else "",
            "playtime_seconds": (play_count + 1) * 3600,
            "last_played": (datetime(2026, 8, 12, 18, 0) - timedelta(days=index)).isoformat(),
            "wikipedia_url": f"https://en.wikipedia.org/wiki/{quote(name.replace(' ', '_'))}",
            "video_url": "",
        }
        apply_record_metadata(entry, database_id, database)
        if not entry.get("cover") or not Path(entry["cover"]).is_file():
            raise SystemExit(f"No cover art available for {name}")
        games.append(entry)

    state = {
        "games": games,
        "profiles": {},
        "history": [],
        "playlists": [],
        "settings": {
            "welcome_completed": True,
            "theme": "",
            "library_view": "grid",
            "image_group": "cover",
            "bigbox_mode": "stage",
            "screensaver_seconds": 90,
            "controller_map": {
                "play": 0,
                "back": 1,
                "favorite": 2,
                "random": 3,
                "page_left": 4,
                "page_right": 5,
                "pause": 8,
                "menu": 9,
            },
        },
    }
    (DATA_DIR / "library.json").write_text(json.dumps(state, indent=2))


def start_server() -> tuple[subprocess.Popen[str], str, str]:
    env = os.environ.copy()
    env["OPENBOX_DATA_DIR"] = str(DATA_DIR)
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "web_app.py"), "--no-browser"],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    url = ""
    deadline = time.time() + 20
    while time.time() < deadline:
        line = process.stdout.readline() if process.stdout else ""
        if "http://127.0.0.1:" in line:
            url = line.strip().split()[-1]
            break
        if process.poll() is not None:
            raise SystemExit("OpenBox server exited before publishing a URL.")
    if not url:
        process.kill()
        raise SystemExit("Timed out waiting for OpenBox server URL.")
    # web_app.py no longer prints the token-bearing URL; the token lives in
    # <data_dir>/server.token (0600), which the server writes before printing
    # the port URL on stdout.
    token_file = DATA_DIR / "server.token"
    if not token_file.is_file():
        process.kill()
        raise SystemExit("Timed out waiting for OpenBox server.token.")
    token = token_file.read_text(encoding="utf-8").strip()
    return process, url, token


def capture_with_puppeteer(app_url: str, output: Path, mode: str = "", detail_game_id: int | None = None) -> None:
    script = ROOT / "scripts" / "capture_screenshot_puppeteer.mjs"
    command = ["node", str(script), app_url, str(output), mode]
    if detail_game_id is not None:
        command.append(str(detail_game_id))
    subprocess.run(command, check=True, cwd=str(ROOT))


def assert_dimensions(path: Path) -> None:
    result = subprocess.run(
        ["file", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    if "1920 x 1080" not in result.stdout:
        raise SystemExit(f"Expected 1920x1080 screenshot, got: {result.stdout.strip()}")


def main() -> None:
    ensure_fixture_tree()
    build_library()
    server, app_url, _token = start_server()
    library_out = FIXTURE_ROOT / "openbox-screenshot.png"
    detail_out = FIXTURE_ROOT / "openbox-game-detail.png"
    bigbox_out = FIXTURE_ROOT / "openbox-bigbox.png"
    constellation_out = FIXTURE_ROOT / "openbox-constellation.png"
    try:
        capture_with_puppeteer(app_url, library_out)
        capture_with_puppeteer(app_url, detail_out, mode="detail", detail_game_id=1)
        capture_with_puppeteer(app_url, bigbox_out, mode="bigbox")
        capture_with_puppeteer(app_url, constellation_out, mode="constellation")
        assert_dimensions(library_out)
        assert_dimensions(detail_out)
        assert_dimensions(bigbox_out)
        assert_dimensions(constellation_out)
        ASSETS_DIR.mkdir(exist_ok=True)
        shutil.copy2(library_out, ASSETS_DIR / "openbox-screenshot.png")
        shutil.copy2(detail_out, ASSETS_DIR / "openbox-game-detail.png")
        shutil.copy2(bigbox_out, ASSETS_DIR / "openbox-bigbox.png")
        shutil.copy2(constellation_out, ASSETS_DIR / "openbox-constellation.png")
        print(f"Wrote {ASSETS_DIR / 'openbox-screenshot.png'}")
        print(f"Wrote {ASSETS_DIR / 'openbox-game-detail.png'}")
        print(f"Wrote {ASSETS_DIR / 'openbox-bigbox.png'}")
        print(f"Wrote {ASSETS_DIR / 'openbox-constellation.png'}")
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    main()
