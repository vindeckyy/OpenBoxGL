"""RetroAchievements account, matching, and progress."""

import hashlib
import json
import os
import re
import zipfile
from pathlib import Path
from time import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from state_store import secure_text_write


SYSTEM_NAMES = {
    "nes": ("Nintendo Entertainment System", "NES/Famicom"),
    "nintendo entertainment system": ("Nintendo Entertainment System", "NES/Famicom"),
    "snes": ("Super Nintendo Entertainment System", "SNES/Super Famicom"),
    "super nintendo entertainment system": ("Super Nintendo Entertainment System", "SNES/Super Famicom"),
    "game boy": ("Game Boy",),
    "game boy color": ("Game Boy Color",),
    "game boy advance": ("Game Boy Advance",),
    "nintendo 64": ("Nintendo 64",),
    "arcade": ("Arcade",),
    "sega genesis": ("Mega Drive",),
    "mega drive": ("Mega Drive",),
    "master system": ("Master System",),
    "game gear": ("Game Gear",),
    "atari 2600": ("Atari 2600",),
    "atari 7800": ("Atari 7800",),
    "atari lynx": ("Atari Lynx",),
    "pc engine": ("PC Engine",),
    "turbografx-16": ("PC Engine",),
}
RAW_MD5 = {
    "game boy", "game boy color", "game boy advance", "sega genesis",
    "mega drive", "master system", "game gear", "atari 2600",
}


def api_get(endpoint, params, credentials, opener=urlopen):
    query = urlencode({**params, "y": credentials["api_key"]})
    request = Request(
        f"https://retroachievements.org/API/{endpoint}?{query}",
        headers={"User-Agent": "OpenBox/1"},
    )
    with opener(request, timeout=20) as response:
        return json.load(response)


def load_credentials(directory):
    try:
        data = json.loads((Path(directory) / "retroachievements.json").read_text())
        if data.get("username") and data.get("api_key"):
            return data
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    from env_config import retroachievements_from_env
    return retroachievements_from_env()


def save_credentials(directory, username, api_key, fetch=api_get):
    credentials = {"username": username.strip(), "api_key": api_key.strip()}
    if not all(credentials.values()):
        raise ValueError("RetroAchievements username and web API key are required.")
    profile = fetch("API_GetUserProfile.php", {"u": credentials["username"]}, credentials)
    if not profile.get("User"):
        raise ValueError("RetroAchievements rejected those credentials.")
    path = Path(directory) / "retroachievements.json"
    secure_text_write(path, json.dumps(credentials))
    return profile


def rom_data(path):
    from parity_premium import archive_rom_bytes

    path = Path(path)
    if path.suffix.casefold() in {".zip", ".7z"}:
        return archive_rom_bytes(path)
    return path.read_bytes()


def game_hash(game):
    path = Path(game.get("path", ""))
    if not path.is_file():
        raise FileNotFoundError("The game path must be a local ROM file for automatic achievement matching.")
    platform = str(game.get("platform", "")).casefold()
    if platform == "arcade":
        return hashlib.md5(path.stem.encode()).hexdigest()
    if platform not in SYSTEM_NAMES:
        raise ValueError("Automatic matching is unavailable for this platform. Enter a RetroAchievements Game ID in Edit metadata.")
    data = rom_data(path)
    if platform in {"nes", "nintendo entertainment system"} and data.startswith(b"NES\x1a"):
        data = data[16:]
    elif platform in {"snes", "super nintendo entertainment system"} and len(data) % 8192 == 512:
        data = data[512:]
    elif platform == "atari 7800" and data.startswith(b"\x01ATARI7800"):
        data = data[128:]
    elif platform == "atari lynx" and data.startswith(b"LYNX\x00"):
        data = data[64:]
    elif platform in {"pc engine", "turbografx-16"} and len(data) % (128 * 1024) == 512:
        data = data[512:]
    elif platform == "nintendo 64" and path.suffix.casefold() == ".v64":
        data = b"".join(data[index:index + 2][::-1] for index in range(0, len(data), 2))
    elif platform == "nintendo 64" and path.suffix.casefold() == ".n64":
        data = b"".join(data[index:index + 4][::-1] for index in range(0, len(data), 4))
    elif platform not in RAW_MD5 and platform not in {"arcade", "nintendo 64"}:
        raise ValueError("Automatic matching is unavailable for this platform. Enter a RetroAchievements Game ID in Edit metadata.")
    return hashlib.md5(data).hexdigest()


def cached(path, getter, max_age=604800):
    path = Path(path)
    try:
        if time() - path.stat().st_mtime < max_age:
            return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        pass
    data = getter()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data))
    temporary.replace(path)
    return data


def match_game(game, credentials, cache_directory, fetch=api_get):
    if str(game.get("ra_game_id", "")).isdigit():
        return int(game["ra_game_id"]), game.get("ra_hash", "")
    platform = str(game.get("platform", "")).casefold()
    targets = SYSTEM_NAMES.get(platform)
    if not targets:
        raise ValueError("This platform has no RetroAchievements system mapping. Enter a Game ID in Edit metadata.")
    systems = cached(
        Path(cache_directory) / "systems.json",
        lambda: fetch("API_GetConsoleIDs.php", {"a":1, "g":1}, credentials),
    )
    console = next((
        item for item in systems
        if any(str(item.get("Name", "")).casefold() == name.casefold() for name in targets)
    ), None)
    if not console:
        raise ValueError("RetroAchievements did not return a matching system.")
    digest = game_hash(game)
    games = cached(
        Path(cache_directory) / f"system-{console['ID']}.json",
        lambda: fetch("API_GetGameList.php", {"i":console["ID"], "h":1, "f":1}, credentials),
    )
    match = next((item for item in games if digest in item.get("Hashes", [])), None)
    if not match:
        raise ValueError("This ROM hash is not linked to a RetroAchievements game.")
    return int(match["ID"]), digest


def game_progress(game_id, credentials, fetch=api_get):
    data = fetch(
        "API_GetGameInfoAndUserProgress.php",
        {"g":game_id, "u":credentials["username"], "a":1},
        credentials,
    )
    achievements = data.get("Achievements", {})
    if isinstance(achievements, dict):
        achievements = list(achievements.values())
    return {
        "title": data.get("Title", ""),
        "earned": int(data.get("NumAwardedToUser", 0)),
        "earned_hardcore": int(data.get("NumAwardedToUserHardcore", 0)),
        "total": int(data.get("NumAchievements", len(achievements))),
        "completion": data.get("UserCompletion", "0%"),
        "achievements": [{
            "title": item.get("Title", ""),
            "description": item.get("Description", ""),
            "points": item.get("Points", 0),
            "badge": re.sub(r"[^A-Za-z0-9_-]", "", str(item.get("BadgeName", ""))),
            "earned": bool(item.get("DateEarned") or item.get("DateEarnedHardcore")),
            "hardcore": bool(item.get("DateEarnedHardcore")),
        } for item in achievements],
    }
