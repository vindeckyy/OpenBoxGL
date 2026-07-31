"""External integrations: RA injection, bezels, EmuMovies, screenshots, OBS, high scores."""

from __future__ import annotations

import configparser
import json
import os
import re
import shutil
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen

from archives import safe_zip_extract
from backend_io import download_file


PLATFORM_BEZEL_REPO = {
    "NES": "thebezelproject/bezelproject-NintendoEntertainmentSystem",
    "SNES": "thebezelproject/bezelproject-SuperNintendoEntertainmentSystem",
    "Nintendo 64": "thebezelproject/bezelproject-Nintendo64",
    "Game Boy Advance": "thebezelproject/bezelproject-GameBoyAdvance",
    "Sega Genesis": "thebezelproject/bezelproject-SegaMegadrive",
    "PlayStation": "thebezelproject/bezelproject-SonyPlayStation",
    "Arcade": "thebezelproject/bezelproject-MAME",
}


def inject_retroachievements(credentials, home=None):
    home = Path(home or Path.home())
    username = str(credentials.get("username") or "").strip()
    api_key = str(credentials.get("api_key") or "").strip()
    if not username or not api_key:
        raise ValueError("RetroAchievements credentials are required.")
    updated, skipped = [], []

    def write_kv(path: Path, mapping: dict, separator=" = "):
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text() if path.is_file() else ""
        lines = existing.splitlines()
        keys = set(mapping)
        out = []
        seen = set()
        for line in lines:
            matched = False
            for key in keys:
                if re.match(rf"^\s*{re.escape(key)}\s*=", line):
                    out.append(f"{key}{separator}{mapping[key]}")
                    seen.add(key)
                    matched = True
                    break
            if not matched:
                out.append(line)
        for key, value in mapping.items():
            if key not in seen:
                out.append(f"{key}{separator}{value}")
        path.write_text("\n".join(out) + ("\n" if out else ""))
        updated.append(str(path))

    retro_cfgs = [
        home / ".config/retroarch/retroarch.cfg",
        home / ".var/app/org.libretro.RetroArch/config/retroarch/retroarch.cfg",
    ]
    for cfg in retro_cfgs:
        try:
            write_kv(cfg, {
                "cheevos_enable": "true",
                "cheevos_username": f'"{username}"',
                "cheevos_password": f'"{api_key}"',
                "cheevos_token": f'"{api_key}"',
            }, separator=" = ")
        except OSError as error:
            skipped.append(f"{cfg}: {error}")

    dolphin_inis = [
        home / ".config/dolphin-emu/Dolphin.ini",
        home / ".var/app/org.DolphinEmu.dolphin-emu/config/dolphin-emu/Dolphin.ini",
    ]
    for ini in dolphin_inis:
        try:
            write_kv(ini, {
                "Enabled": "True",
                "Username": username,
                "Password": api_key,
            }, separator=" = ")
        except OSError as error:
            skipped.append(f"{ini}: {error}")

    pcsx2_inis = [
        home / ".config/PCSX2/inis/PCSX2.ini",
        home / ".var/app/net.pcsx2.PCSX2/config/PCSX2/inis/PCSX2.ini",
    ]
    for ini in pcsx2_inis:
        try:
            write_kv(ini, {
                "Enabled": "true",
                "Username": username,
                "Token": api_key,
            }, separator=" = ")
        except OSError as error:
            skipped.append(f"{ini}: {error}")

    return {"updated": updated, "skipped": skipped}


def bezel_project_urls():
    return {
        platform: f"https://codeload.github.com/{repo}/zip/refs/heads/master"
        for platform, repo in PLATFORM_BEZEL_REPO.items()
    }


def download_bezel(platform, dest_dir, opener=urlopen):
    urls = bezel_project_urls()
    if platform not in urls:
        raise ValueError(f"No Bezel Project mapping for {platform}.")
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    archive = dest / f"{platform.replace(' ', '_')}-bezels.zip"
    request = Request(urls[platform], headers={"User-Agent": "OpenBox/1"})
    download_file(
        urls[platform], archive,
        max_bytes=512 * 1024 * 1024,
        timeout=120,
        opener=opener,
    )
    extract_to = dest / platform.replace(" ", "_")
    if extract_to.exists():
        shutil.rmtree(extract_to)
    extract_to.mkdir(parents=True, exist_ok=True)
    safe_zip_extract(archive, extract_to)
    return str(extract_to)


def load_emumovies_credentials(data_dir):
    path = Path(data_dir) / "emumovies.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict) and data.get("username"):
            return data
    from env_config import emumovies_from_env
    return emumovies_from_env()


def save_emumovies_credentials(data_dir, username, password):
    path = Path(data_dir) / "emumovies.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"username": username, "password": password}, indent=2))
    os.chmod(path, 0o600)
    return {"configured": True}


def download_emumovies_media(game, credentials, media_root, media_type="box"):
    username = str((credentials or {}).get("username") or "").strip()
    password = str((credentials or {}).get("password") or "").strip()
    if not username or not password:
        raise ValueError("Configure EmuMovies credentials in Settings first.")
    # EmuMovies requires a licensed account; attempt their search endpoint shape.
    platform = str(game.get("platform") or "Arcade")
    name = str(game.get("name") or "")
    url = f"https://api.emumovies.com/v1/media/{media_type}?system={platform}&search={name}"
    import base64
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    root = Path(media_root) / "emumovies" / re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    root.mkdir(parents=True, exist_ok=True)
    destination = root / f"{media_type}.jpg"
    try:
        download_file(
            url,
            destination,
            expected_types=("image/",),
            max_bytes=32 * 1024 * 1024,
            timeout=30,
            opener=urlopen,
            headers={"Authorization": f"Basic {token}"},
        )
    except Exception as error:  # noqa: BLE001 - surface remote/API failures cleanly
        raise ValueError(f"EmuMovies request failed: {error}") from error
    return str(destination)


def capture_screenshot(dest_path, window_hint=""):
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    commands = []
    if shutil.which("gnome-screenshot"):
        commands.append(["gnome-screenshot", "-f", str(dest)])
    if shutil.which("spectacle"):
        commands.append(["spectacle", "-b", "-n", "-o", str(dest)])
    if shutil.which("scrot"):
        commands.append(["scrot", str(dest)])
    if shutil.which("import"):
        commands.append(["import", "-window", "root", str(dest)])
    last_error = None
    for command in commands:
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=20)
            if dest.is_file():
                return str(dest)
        except (OSError, subprocess.SubprocessError) as error:
            last_error = error
    raise FileNotFoundError(last_error or "No screenshot tool found (gnome-screenshot, spectacle, scrot, or ImageMagick import).")


def mame_highscore_path(home=None):
    home = Path(home or Path.home())
    candidates = [
        home / ".mame/hi",
        home / ".config/mame/hi",
        home / ".var/app/org.mamedev.MAME/config/mame/hi",
    ]
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def read_local_highscores(game, home=None):
    rom = str(game.get("rom_name") or Path(game.get("path", "")).stem).strip()
    if not rom:
        return []
    root = mame_highscore_path(home)
    scores = []
    for path in root.glob(f"{rom}*"):
        try:
            size = path.stat().st_size
            scores.append({"file": path.name, "size": size, "label": path.stem})
        except OSError:
            pass
    return scores


def obs_recording_directory(home=None):
    home = Path(home or Path.home())
    profile_roots = (
        home / ".config/obs-studio/basic/profiles",
        home / ".var/app/com.obsproject.Studio/config/obs-studio/basic/profiles",
    )
    for profiles in profile_roots:
        if not profiles.is_dir():
            continue
        for profile in sorted(profiles.iterdir(), reverse=True):
            ini = profile / "basic.ini"
            if not ini.is_file():
                continue
            parser = configparser.ConfigParser()
            try:
                parser.read(ini)
            except configparser.Error:
                continue
            for section in ("SimpleOutput", "AdvOut"):
                if parser.has_option(section, "FilePath"):
                    path = parser.get(section, "FilePath").strip()
                    if path:
                        return Path(path).expanduser()
                if parser.has_option(section, "RecFilePath"):
                    path = parser.get(section, "RecFilePath").strip()
                    if path:
                        return Path(path).expanduser()
    return home / "Videos"


def find_latest_recording(directory, since=None):
    root = Path(directory).expanduser()
    if not root.is_dir():
        return None
    extensions = {".mp4", ".mkv", ".mov", ".flv", ".webm"}
    candidates = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.casefold() not in extensions:
            continue
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        if since and modified < since.timestamp() - 1:
            continue
        candidates.append((modified, path))
    if not candidates:
        return None
    return str(max(candidates)[1])


def obs_recording_status(home=None):
    directory = obs_recording_directory(home)
    latest = find_latest_recording(directory)
    return {
        "running": bool(shutil.which("obs") and _pgrep("obs")),
        "recording": bool(_pgrep("obs")),
        "directory": str(directory),
        "latest_recording": latest,
    }


def _pgrep(name):
    try:
        result = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def attach_recording(game, video_path):
    path = Path(video_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError("Recording file not found.")
    game["video_recording"] = str(path)
    if not game.get("video"):
        game["video"] = str(path)
    return str(path)


def auto_attach_obs_recording(game, started, settings=None, home=None):
    settings = settings or {}
    if settings.get("obs_auto_attach") is False:
        return None
    directory = settings.get("obs_recording_path") or obs_recording_directory(home)
    recording = find_latest_recording(directory, since=started)
    if recording:
        return attach_recording(game, recording)
    return None


def export_highscores(game, export_dir, home=None):
    root = mame_highscore_path(home)
    export = Path(export_dir)
    export.mkdir(parents=True, exist_ok=True)
    copied = []
    for score in read_local_highscores(game, home):
        source = root / score["file"]
        if not source.is_file():
            continue
        destination = export / score["file"]
        shutil.copy2(source, destination)
        copied.append(str(destination))
    manifest = export / "highscores.json"
    manifest.write_text(json.dumps({
        "format": 1,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "game": game.get("name"),
        "rom": game.get("rom_name") or Path(game.get("path", "")).stem,
        "files": copied,
    }, indent=2))
    return {"files": copied, "manifest": str(manifest)}


def import_highscores(game, import_dir, home=None):
    folder = Path(import_dir).expanduser()
    manifest = folder / "highscores.json"
    if manifest.is_file():
        try:
            payload = json.loads(manifest.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("High score bundle is invalid.") from error
        files = payload.get("files", []) if isinstance(payload, dict) else []
    else:
        files = [str(path) for path in folder.glob("*") if path.is_file()]
    target = mame_highscore_path(home)
    target.mkdir(parents=True, exist_ok=True)
    rom = str(game.get("rom_name") or Path(game.get("path", "")).stem).strip()
    restored = []
    for file_path in files:
        source = Path(file_path)
        if not source.is_file():
            source = folder / source.name
        if not source.is_file():
            continue
        destination = target / (source.name if rom in source.name else f"{rom}-{source.name}")
        shutil.copy2(source, destination)
        restored.append(str(destination))
    if not restored:
        raise FileNotFoundError("No high score files were found in the import bundle.")
    return restored
