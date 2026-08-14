"""LaunchBox Premium parity: custom fields, ESRB, media packs, and related settings."""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import zipfile
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from backend_io import read_limited

from backend_io import download_file

try:
    import py7zr
except ImportError:
    py7zr = None


ESRB_VALUES = {"", "E", "E10+", "T", "M", "AO", "RP", "EC", "K-A", "Unrated"}

DEFAULT_PLATFORM_CATEGORIES = {
    "Arcade": "Arcade",
    "MAME": "Arcade",
    "FinalBurn Neo": "Arcade",
    "NES": "Nintendo",
    "SNES": "Nintendo",
    "Nintendo 64": "Nintendo",
    "Game Boy": "Nintendo",
    "Game Boy Color": "Nintendo",
    "Game Boy Advance": "Nintendo",
    "Nintendo DS": "Nintendo",
    "Nintendo 3DS": "Nintendo",
    "GameCube": "Nintendo",
    "Wii": "Nintendo",
    "WiiWare": "Nintendo",
    "Wii U": "Nintendo",
    "Nintendo Switch": "Nintendo",
    "PlayStation": "Sony",
    "PlayStation 2": "Sony",
    "PlayStation 3": "Sony",
    "PSP": "Sony",
    "PlayStation Vita": "Sony",
    "Xbox": "Microsoft",
    "Xbox 360": "Microsoft",
    "PC": "Computer",
    "Windows": "Computer",
    "Linux": "Computer",
    "ScummVM": "Adventure",
    "DOS": "Computer",
}

REGION_RANK = {
    "world": 0,
    "usa": 1,
    "us": 1,
    "english": 1,
    "europe": 2,
    "eur": 2,
    "japan": 3,
    "jpn": 3,
    "asia": 4,
}

BAD_TAGS = re.compile(r"\((?:beta|proto|demo|sample|unl|pirate|hack|translation)\)", re.I)

LOCALES = ("en", "es", "de", "fr", "pt")

STRINGS = {
    "en": {
        "library": "Library",
        "all_games": "All games",
        "import": "Import",
        "settings": "Settings",
        "big_box": "Big Box",
        "list_view": "List view",
        "grid_view": "Grid view",
        "drop_import": "Drop ROM folders or game files here to import",
    },
    "es": {
        "library": "Biblioteca",
        "all_games": "Todos los juegos",
        "import": "Importar",
        "settings": "Ajustes",
        "big_box": "Big Box",
        "list_view": "Vista de lista",
        "grid_view": "Vista de cuadrícula",
        "drop_import": "Suelta carpetas ROM o archivos aquí para importar",
    },
    "de": {
        "library": "Bibliothek",
        "all_games": "Alle Spiele",
        "import": "Importieren",
        "settings": "Einstellungen",
        "big_box": "Big Box",
        "list_view": "Listenansicht",
        "grid_view": "Rasteransicht",
        "drop_import": "ROM-Ordner oder Spieldateien hier ablegen",
    },
    "fr": {
        "library": "Bibliothèque",
        "all_games": "Tous les jeux",
        "import": "Importer",
        "settings": "Paramètres",
        "big_box": "Big Box",
        "list_view": "Vue liste",
        "grid_view": "Vue grille",
        "drop_import": "Déposez des dossiers ROM ou des fichiers ici",
    },
    "pt": {
        "library": "Biblioteca",
        "all_games": "Todos os jogos",
        "import": "Importar",
        "settings": "Configurações",
        "big_box": "Big Box",
        "list_view": "Vista em lista",
        "grid_view": "Vista em grade",
        "drop_import": "Solte pastas ROM ou arquivos aqui",
    },
}

MEDIA_PACKS = [
    {
        "id": "clear-logos-default",
        "name": "Platform Clear Logos",
        "description": "High-contrast platform logos for sidebar and Big Box hybrid views.",
        "kinds": ["platform_logo"],
    },
    {
        "id": "controller-xbox",
        "name": "Xbox Controller Prompts",
        "description": "Xbox-style button prompts for Big Box status hints.",
        "kinds": ["controller_art"],
        "mapping_hint": "A Play · B Back · X Favorite · Y Menu",
    },
    {
        "id": "controller-playstation",
        "name": "PlayStation Controller Prompts",
        "description": "PlayStation-style button prompts for Big Box status hints.",
        "kinds": ["controller_art"],
        "mapping_hint": "Cross Play · Circle Back · Square Favorite · Triangle Menu",
    },
    {
        "id": "badges-core",
        "name": "Core Status Badges",
        "description": "Favorite, save, document, and progress badge styling pack.",
        "kinds": ["badges"],
    },
]

BULK_WIZARD_FIELDS = (
    "platform", "genre", "progress", "rating", "favorite", "hidden", "esrb", "custom_fields",
)

LIST_COLUMNS_DEFAULT = ("name", "platform", "genre", "esrb", "progress", "last_played", "play_count")


def strings_for(locale: str):
    locale = str(locale or "en").casefold().split("-", 1)[0]
    return STRINGS.get(locale, STRINGS["en"])


def custom_field_defs(settings):
    defs = settings.get("custom_field_defs", [])
    if not isinstance(defs, list):
        return []
    clean = []
    for item in defs[:20]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        options = item.get("options", [])
        if isinstance(options, str):
            options = [part.strip() for part in options.split(",") if part.strip()]
        elif not isinstance(options, list):
            options = []
        clean.append({
            "name": name,
            "options": [str(option).strip() for option in options if str(option).strip()][:50],
        })
    return clean


def normalize_custom_fields(game, defs):
    current = game.get("custom_fields", {})
    if not isinstance(current, dict):
        current = {}
    allowed = {item["name"] for item in defs}
    game["custom_fields"] = {key: str(current.get(key, "")).strip() for key in allowed if str(current.get(key, "")).strip()}
    return game


def platform_categories(settings):
    mapping = settings.get("platform_categories", {})
    if not isinstance(mapping, dict):
        mapping = {}
    merged = dict(DEFAULT_PLATFORM_CATEGORIES)
    merged.update({str(key): str(value) for key, value in mapping.items() if str(key).strip() and str(value).strip()})
    return merged


def category_for_platform(platform, settings):
    return platform_categories(settings).get(platform or "Unspecified", "Other")


def region_score(name: str):
    lowered = name.casefold()
    for token, score in REGION_RANK.items():
        if token in lowered:
            return score
    return 5


def rom_quality_score(path: Path):
    name = path.name
    score = 0
    if BAD_TAGS.search(name):
        score += 100
    score += region_score(name)
    if path.suffix.casefold() in {".chd", ".cue", ".m3u"}:
        score -= 2
    if path.suffix.casefold() in {".zip", ".7z"}:
        score += 1
    try:
        score -= min(path.stat().st_size // (64 * 1024 * 1024), 5)
    except OSError:
        # A missing/unreadable ROM scores worst so it sorts last.
        score += 1000
    return score


def rank_rom_group(paths):
    ranked = sorted((Path(p) for p in paths if Path(p).is_file()), key=lambda item: (rom_quality_score(item), item.name.casefold()))
    return [str(path) for path in ranked]


def pick_best_rom(paths):
    ranked = rank_rom_group(paths)
    return ranked[0] if ranked else ""


def apply_esrb_from_record(game, record):
    esrb = str(record.get("esrb", "") or record.get("ESRB", "")).strip()
    if esrb and esrb in ESRB_VALUES:
        game["esrb"] = esrb


def download_bytes(url, destination, opener=urlopen):
    return str(download_file(
        url,
        destination,
        max_bytes=512 * 1024 * 1024,
        timeout=20,
        opener=opener,
    ))


def download_steam_trailer(game, media_root, opener=urlopen):
    app_id = str(game.get("steam_app_id", ""))
    if not app_id.isdigit():
        raise ValueError("Steam App ID required for trailer download.")
    request = Request(
        f"https://store.steampowered.com/api/appdetails?appids={app_id}",
        headers={"User-Agent": "OpenBox/1"},
    )
    with opener(request, timeout=20) as response:
        payload = json.loads(read_limited(response, 4 * 1024 * 1024))
    record = payload.get(app_id, {})
    if not record.get("success"):
        raise ValueError("Steam did not return app details.")
    movies = record.get("data", {}).get("movies") or []
    if not movies:
        raise ValueError("Steam returned no trailers for this game.")
    movie = movies[0]
    mp4 = next((item for item in movie.get("mp4", {}).values() if item), "") or movie.get("webm", {}).get("max", "")
    if not mp4:
        raise ValueError("Steam trailer URL was missing.")
    destination = Path(media_root) / "steam" / app_id / "trailer.mp4"
    game["video_trailer"] = download_bytes(mp4, destination, opener=opener)
    return game["video_trailer"]


def download_gog_media(game, media_root, opener=urlopen):
    app_id = str(game.get("heroic_app_id", ""))
    if not app_id:
        raise ValueError("Heroic/GOG app id required.")
    slug = app_id.replace("gog_", "")
    request = Request(
        f"https://embed.gog.com/games/ajax/filtered?mediaType=game&search={quote(slug)}",
        headers={"User-Agent": "OpenBox/1"},
    )
    with opener(request, timeout=20) as response:
        payload = json.loads(read_limited(response, 4 * 1024 * 1024))
    products = payload.get("products", []) if isinstance(payload, dict) else []
    product = next((item for item in products if str(item.get("id", "")) == slug or slug in str(item.get("slug", ""))), None)
    if not product:
        raise ValueError("GOG metadata lookup returned no product.")
    root = Path(media_root) / "gog" / slug
    if product.get("img"):
        game["cover"] = download_bytes(product["img"], root / "cover.jpg", opener=opener)
    if product.get("background"):
        game["background"] = download_bytes(product["background"], root / "background.jpg", opener=opener)
    return game


def archive_rom_bytes(path: Path):
    suffix = path.suffix.casefold()
    if suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            files = [info for info in archive.infolist() if not info.is_dir()]
            if not files:
                raise ValueError("Archive contains no ROM files.")
            return archive.read(max(files, key=lambda info: info.file_size))
    if suffix == ".7z":
        if py7zr is None:
            process = subprocess.run(
                ["7z", "e", "-so", str(path)], capture_output=True, check=False, timeout=300,
            )
            if process.returncode != 0 or not process.stdout:
                raise ValueError("Install py7zr or 7z to hash ROMs inside .7z archives.")
            return process.stdout
        with py7zr.SevenZipFile(path, mode="r") as archive:
            names = [name for name in archive.getnames() if not name.endswith("/")]
            if not names:
                raise ValueError("7z archive contains no ROM files.")
            data = archive.read(names[0])
            if isinstance(data, dict):
                data = next(iter(data.values()))
            return data
    return path.read_bytes()


def import_loose_arcade(folder, command=""):
    root = Path(folder).expanduser()
    if not root.is_dir():
        raise FileNotFoundError("Arcade folder does not exist.")
    binary = shutil.which("hypseus") or shutil.which("singe")
    default = command or (shlex.join([binary, "{path}"]) if binary else "")
    games = []
    for path in sorted(root.iterdir()):
        if not path.is_file():
            continue
        if path.suffix.casefold() not in {".zip", ".7z", ".singe", ".rom"}:
            continue
        games.append({
            "name": path.stem.replace("_", " "),
            "platform": "Arcade",
            "source": "Hypseus Singe" if "singe" in path.name.casefold() else "Loose Arcade",
            "collection": "Arcade",
            "path": str(path),
            "launch": default or str(path),
            "rom_name": path.stem,
        })
    return games


def import_xbox360_folder(folder, command=""):
    root = Path(folder).expanduser()
    if not root.is_dir():
        raise FileNotFoundError("Xbox 360 folder does not exist.")
    binary = shutil.which("xenia") or shutil.which("xenia-canary")
    default = command or (shlex.join([binary, "--launch", "{path}"]) if binary else "")
    games = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.name.casefold() != "default.xex" and path.suffix.casefold() not in {".xex", ".xbe"}:
            continue
        title = path.parent.name if path.name.casefold() == "default.xex" else path.stem
        games.append({
            "name": title.replace("_", " "),
            "platform": "Xbox 360",
            "source": "Xbox 360",
            "collection": "Xbox 360",
            "path": str(path),
            "launch": default or str(path),
        })
    return games


def resolve_vita_title(entry):
    title = str(entry.get("title", "") or entry.get("name", "")).strip()
    title_id = str(entry.get("title_id", "") or entry.get("id", "")).strip()
    if title and title.casefold() != title_id.casefold() and not re.fullmatch(r"[A-Z0-9]{9}", title):
        return title
    meta = entry.get("metadata") or entry.get("param_sfo") or {}
    if isinstance(meta, dict):
        for key in ("Title", "title", "name"):
            value = str(meta.get(key, "")).strip()
            if value:
                return value
    return title or title_id or "PlayStation Vita Game"


def enhanced_ra_profile(progress, credentials):
    profile = {}
    try:
        from retroachievements import api_get
        profile = api_get("API_GetUserProfile.php", {"u": credentials["username"]}, credentials)
    except (OSError, ValueError, json.JSONDecodeError, KeyError):
        profile = {}
    # API_GetUserProfile has no Awarded field; derive the beaten count from the profile's totals, else earned.
    beaten = profile.get("Awarded")
    if beaten is None:
        beaten = progress.get("beaten", progress.get("earned", 0))
    return {
        **progress,
        "beaten": int(beaten or 0),
        "mastered": int(progress.get("earned_hardcore", 0)),
        "points_earned": int(profile.get("TotalPoints", 0) or 0),
        "points_total": int(profile.get("TotalTruePoints", 0) or 0),
        "motivation": profile.get("Motivation", ""),
        "rich_presence": profile.get("RichPresenceMsg", ""),
        "commitment_label": f"{progress.get('earned', 0)}/{progress.get('total', 0)} unlocked",
    }


def apply_media_pack(state, pack_id):
    pack = next((item for item in MEDIA_PACKS if item["id"] == pack_id), None)
    if not pack:
        raise ValueError("Unknown media pack.")
    settings = state.setdefault("settings", {})
    active = settings.setdefault("active_media_packs", [])
    if pack_id not in active:
        active.append(pack_id)
    if "controller_art" in pack["kinds"]:
        settings["controller_prompt_pack"] = pack_id
        settings["controller_prompt_hint"] = pack.get("mapping_hint", "")
    return pack


def list_media_packs(settings):
    active = set(settings.get("active_media_packs", []))
    return [{**pack, "active": pack["id"] in active} for pack in MEDIA_PACKS]


def import_with_emulator_choice(folder, extensions_set, platform_map, chosen_emulators=None):
    from datetime import datetime
    from parity_import import import_multi_platform

    candidates = import_multi_platform(folder, extensions_set, platform_map)
    chosen = chosen_emulators or {}
    additions = []
    recommendations = {}
    for item in candidates:
        platform = item.get("platform", "")
        recommendations.setdefault(platform, recommend_emulators(platform))
        if platform in chosen and chosen[platform]:
            item["recommended_emulator"] = chosen[platform]
        versions = item.pop("version_candidates", None)
        if versions:
            best = pick_best_rom(versions)
            if best:
                item["path"] = best
        item["added_at"] = datetime.now().isoformat(timespec="seconds")
        additions.append(item)
    return additions, recommendations


def recommend_emulators(platform):
    from parity_import import recommend_emulators as base
    return base(platform)


def bulk_wizard_changes(step_fields):
    if not isinstance(step_fields, dict):
        raise ValueError("Bulk wizard payload must be an object.")
    allowed = set(BULK_WIZARD_FIELDS)
    if not set(step_fields) <= allowed:
        raise ValueError("Bulk wizard contains unsupported fields.")
    return step_fields
