"""Import installed game libraries from Linux storefronts."""

import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path


def vdf_values(text):
    return dict(re.findall(r'"([^"]+)"\s+"([^"]*)"', text))


def steam_roots(home=None):
    home = home or Path.home()
    candidates = (
        home / ".local/share/Steam",
        home / ".steam/steam",
        home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
    )
    return [path for path in candidates if (path / "steamapps").is_dir()]


def steam_command():
    if binary := shutil.which("steam"):
        return binary, "steam -applaunch {app_id}"
    if binary := shutil.which("flatpak"):
        return binary, "flatpak run com.valvesoftware.Steam -applaunch {app_id}"
    if binary := shutil.which("xdg-open"):
        return binary, "xdg-open steam://rungameid/{app_id}"
    raise FileNotFoundError("Steam, Flatpak, or xdg-open is required to launch imported Steam games.")


def steam_libraries(root):
    libraries = {root}
    file = root / "steamapps/libraryfolders.vdf"
    if file.is_file():
        for path in re.findall(r'"path"\s+"([^"]+)"', file.read_text(errors="replace")):
            library = Path(path.replace("\\\\", "\\"))
            if (library / "steamapps").is_dir():
                libraries.add(library)
    return sorted(libraries)


def import_steam(home=None):
    home = home or Path.home()
    executable, command = steam_command()
    games = []
    seen = set()
    for root in steam_roots(home):
        for library in steam_libraries(root):
            for manifest in (library / "steamapps").glob("appmanifest_*.acf"):
                values = vdf_values(manifest.read_text(errors="replace"))
                app_id, name = values.get("appid"), values.get("name")
                if not app_id or not name or app_id in seen:
                    continue
                seen.add(app_id)
                games.append({
                    "name": name,
                    "platform": "PC",
                    "source": "Steam",
                    "collection": "Steam",
                    "path": executable,
                    "launch": command,
                    "steam_app_id": app_id,
                    "install_dir": str(library / "steamapps/common" / values.get("installdir", "")),
                })
    return games


def json_records(path):
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, list):
        return [(str(index), value) for index, value in enumerate(data) if isinstance(value, dict)]
    if isinstance(data, dict):
        return [(str(key), value) for key, value in data.items() if isinstance(value, dict)]
    return []


def heroic_bases(home=None):
    home = home or Path.home()
    candidates = (
        home / ".config/heroic",
        home / ".var/app/com.heroicgameslauncher.hgl/config/heroic",
    )
    return [path for path in candidates if path.is_dir()]


def import_heroic(home=None):
    home = home or Path.home()
    opener = shutil.which("xdg-open")
    if not opener:
        raise FileNotFoundError("xdg-open is required to launch imported Heroic games.")
    manifests = []
    for base in heroic_bases(home):
        manifests.extend((
            ("Epic", "legendary", base / "legendaryConfig/legendary/installed.json"),
            ("GOG", "gog", base / "gog_store/installed.json"),
            ("Amazon", "nile", base / "nile_config/installed.json"),
        ))
    manifests.extend((
        ("Epic", "legendary", home / ".config/legendary/installed.json"),
        ("Epic", "legendary", home / ".var/app/com.heroicgameslauncher.hgl/config/legendary/installed.json"),
    ))
    games, seen = [], set()
    for source, runner, manifest in manifests:
        for key, record in json_records(manifest):
            app_id = str(record.get("app_name") or record.get("appName") or record.get("product_id") or record.get("id") or key)
            title = record.get("title") or record.get("app_title") or record.get("name")
            if not title or record.get("is_dlc") or (source, app_id) in seen:
                continue
            seen.add((source, app_id))
            games.append({
                "name": str(title),
                "platform": "PC",
                "source": source,
                "collection": source,
                "path": opener,
                "launch": f"xdg-open heroic://launch/{runner}/{{heroic_app_id}}",
                "heroic_app_id": app_id,
                "install_dir": str(record.get("install_path") or record.get("installPath") or record.get("path") or ""),
            })
    return games


def import_lutris(home=None, run=subprocess.run, which=shutil.which):
    home = home or Path.home()
    if binary := which("lutris"):
        command = [binary]
    elif binary := which("flatpak"):
        command = [binary, "run", "net.lutris.Lutris"]
    else:
        raise FileNotFoundError("Lutris or Flatpak is required to import Lutris games.")
    result = run(
        command + ["--list-games", "--installed", "--json"],
        capture_output=True, text=True, check=True, timeout=30,
    )
    output = result.stdout.strip()
    start, end = output.find("["), output.rfind("]")
    records = json.loads(output[start:end + 1] if start >= 0 and end > start else output)
    if isinstance(records, dict):
        records = records.get("games", [])
    if not isinstance(records, list):
        raise ValueError("Lutris returned an invalid game list.")
    games = []
    for record in records:
        if not isinstance(record, dict) or record.get("installed") is False:
            continue
        game_id = str(record.get("id", "")).strip()
        name = str(record.get("name", "")).strip()
        if not game_id.isdigit() or not name:
            continue
        origin = " ".join(str(record.get(key, "")) for key in ("service", "source")).lower()
        if "xbox" in origin or "game pass" in origin:
            source = "Xbox"
        elif "origin" in origin or "ea app" in origin:
            source = "EA"
        elif "ubisoft" in origin or "uplay" in origin:
            source = "Ubisoft"
        else:
            source = "Lutris"
        runner = str(record.get("runner", "")).strip()
        slug = str(record.get("slug") or record.get("game_slug") or "").strip()
        cover = next((
            path for base in (
                home / ".local/share/lutris/coverart",
                home / ".var/app/net.lutris.Lutris/data/lutris/coverart",
            ) for suffix in (".jpg", ".png", ".webp")
            if slug and (path := base / f"{slug}{suffix}").is_file()
        ), "")
        game = {
            "name": name,
            "platform": str(record.get("platform") or ("Windows" if runner in {"wine", "winesteam"} else "PC")),
            "source": source,
            "collection": source,
            "path": binary,
            "launch": shlex.join(command + ["lutris:rungameid/{lutris_id}"]),
            "lutris_id": game_id,
            "install_dir": str(record.get("directory") or record.get("path") or ""),
        }
        if cover:
            game["cover"] = str(cover)
        games.append(game)
    return games
