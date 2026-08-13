#!/usr/bin/env python3
"""Generate a synthetic OpenBox library for performance benchmarking.

Creates an isolated data root populated with N realistic games and fixture
media files, written through the real state store so benchmarks exercise the
same load/normalize path as production.

Usage:
  python3 -B scripts/perf_gen_library.py --games 5000 --data-dir /tmp/openbox-perf/5000
"""

import argparse
import binascii
import random
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from state_store import JsonStateStore  # noqa: E402

PLATFORMS = [
    ("PC", "Steam"), ("Nintendo Entertainment System", "Folder"), ("Super Nintendo", "Folder"),
    ("Nintendo 64", "Folder"), ("Game Boy Advance", "Folder"), ("Nintendo DS", "Folder"),
    ("Sega Genesis", "Folder"), ("Sega Saturn", "Folder"), ("PlayStation", "Folder"),
    ("PlayStation 2", "Folder"), ("PlayStation 3", "Folder"), ("PlayStation Portable", "Folder"),
    ("Xbox 360", "Folder"), ("Sega Dreamcast", "Folder"), ("Arcade", "MAME"),
    ("Nintendo GameCube", "Folder"), ("Nintendo Wii", "Folder"), ("Atari 2600", "Folder"),
    ("Commodore 64", "Folder"), ("TurboGrafx-16", "Folder"),
]
GENRES = ["Action", "Adventure", "RPG", "Platformer", "Shooter", "Puzzle", "Racing",
          "Fighting", "Sports", "Strategy", "Simulation", "Horror", "Metroidvania"]
DEVELOPERS = ["Nintendo", "Sega", "Konami", "Capcom", "Square", "Namco", "Atari",
              "Hudson Soft", "Tecmo", "Sunsoft", "Data East", "SNK"]
PREFIXES = ["Super", "Mega", "Turbo", "Hyper", "Neo", "Ultra", "Cosmic", "Shadow",
            "Crimson", "Phantom", "Astro", "Cyber", "Pixel", "Retro", "Galactic"]
SUFFIXES = ["Quest", "Rally", "Force", "Strike", "Legend", "Chronicles", "Mania",
            "Blitz", "Rush", "Saga", "Wars", "Duel", "Storm", "Zero"]


def _png_bytes():
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", binascii.crc32(body) & 0xFFFFFFFF)

    raw = zlib.compress(b"\x00\x10\x20\x30")
    return signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


def _write_png(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_png_bytes())


def build_games(count, data_dir: Path, rng: random.Random):
    """Return a list of N realistic game dicts, writing fixture media files."""
    media_root = data_dir / "media"
    png = _png_bytes()
    games = []
    for index in range(count):
        platform, source = PLATFORMS[index % len(PLATFORMS)]
        name = f"{rng.choice(PREFIXES)} {rng.choice(SUFFIXES)} {index + 1}"
        slug = f"game-{index:05d}"
        roll = rng.random()

        def media_path(kind, ext="png", slug=slug):
            return str(media_root / slug / f"{kind}.{ext}")

        def maybe_media(kind, chance, ext="png", slug=slug):
            if rng.random() < chance:
                path = media_path(kind, ext)
                path_obj = media_root / slug / f"{kind}.{ext}"
                if not path_obj.is_file():
                    path_obj.parent.mkdir(parents=True, exist_ok=True)
                    path_obj.write_bytes(png)
                return path
            return ""

        path_exists = rng.random() < 0.85
        path = "/usr/bin/true" if path_exists else f"/nonexistent/openbox-perf/{slug}/game.bin"
        screenshots = []
        for shot in range(rng.choice([0, 0, 0, 1, 1, 2, 3, 4])):
            shot_path = media_root / slug / f"screenshot-{shot}.png"
            if not shot_path.is_file():
                shot_path.parent.mkdir(parents=True, exist_ok=True)
                shot_path.write_bytes(png)
            screenshots.append(str(shot_path))

        game = {
            "game_id": f"game-{index:05d}",
            "name": name,
            "sort_title": name.upper(),
            "platform": platform,
            "genre": rng.choice(GENRES),
            "year": str(1985 + rng.randrange(35)),
            "developer": rng.choice(DEVELOPERS),
            "publisher": rng.choice(DEVELOPERS),
            "series": rng.choice(SUFFIXES) if rng.random() < 0.3 else "",
            "description": (
                f"{name} is a classic {rng.choice(GENRES)} game released in {1985 + rng.randrange(35)}. "
                f"Players explore a vast world filled with {rng.choice(['puzzles', 'enemies', 'secrets', 'bosses'])} "
                f"while uncovering the story of {rng.choice(['a lost hero', 'an ancient war', 'a cursed land', 'a robot uprising'])}. "
                f"Featuring {rng.choice(['pixel art', 'hand-drawn sprites', 'pre-rendered graphics', '16-bit chiptunes'])}, "
                f"it remains a beloved entry in the genre."
            ),
            "path": path,
            "launch": f"/bin/true --openbox-perf {slug}",
            "cover": maybe_media("cover", 0.90),
            "background": maybe_media("background", 0.70),
            "clear_logo": maybe_media("clear_logo", 0.30),
            "fanart": maybe_media("fanart", 0.20),
            "banner": maybe_media("banner", 0.25),
            "icon": maybe_media("icon", 0.10),
            "box_back": maybe_media("box_back", 0.15),
            "source": source,
            "progress": rng.choice(["", "", "", "Beaten", "Completed", "In Progress", "Paused", "Mastered"]),
            "rating": round(rng.random() * 5, 1) if rng.random() < 0.7 else 0,
            "play_count": rng.randrange(0, 60),
            "playtime_seconds": rng.randrange(0, 30 * 3600),
            "added_at": f"2026-{1 + rng.randrange(7):02d}-{1 + rng.randrange(27):02d}",
            "last_played": f"2026-{1 + rng.randrange(7):02d}-{1 + rng.randrange(27):02d}" if rng.random() < 0.5 else "",
            "favorite": rng.random() < 0.1,
            "hidden": False,
            "notes": rng.choice(["", "", "", "Play with friends", "Speedrun category: any%", "Needs CRT shader"]),
            "alternate_names": [f"{name} Deluxe"] if rng.random() < 0.2 else [],
            "screenshots": screenshots,
            "applications": [{"name": "Manual", "path": "/bin/true", "command": "/bin/true"}],
            "versions": [],
            "documents": [],
            "save_paths": [] if rng.random() < 0.8 else [f"/nonexistent/openbox-perf/saves/{slug}"],
            "esrb": rng.choice(["", "E", "E10+", "T", "M"]) if rng.random() < 0.6 else "",
            "broken": False,
            "portable": rng.random() < 0.05,
            "custom_fields": {"Notes": "perf fixture"} if rng.random() < 0.2 else {},
            "hide_in_bigbox": False,
        }
        if source == "Steam":
            game["steam_app_id"] = str(1000 + index)
        if roll < 0.15:
            game["launchbox_db_id"] = f"LB{100000 + index}"
        games.append(game)
    return games


def generate(games_count, data_dir: Path, seed=42):
    data_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    games = build_games(games_count, data_dir, rng)
    store = JsonStateStore(data_dir / "library.json")
    state = {
        "schema_version": 3,
        "games": games,
        "profiles": {},
        "history": [],
        "settings": {"image_group": "cover", "language": "en"},
        "playlists": [],
    }
    store.save(state)
    return len(games)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games", type=int, default=1000)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    count = generate(args.games, Path(args.data_dir), args.seed)
    print(f"generated {count} games in {args.data_dir}")


if __name__ == "__main__":
    main()
