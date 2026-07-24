"""Storefront catalog browsing, the best Linux equivalent to LaunchBox Storefront Manager."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from importers import heroic_bases, import_heroic, import_lutris, import_steam, json_records, steam_command, steam_roots, vdf_values


def steam_owned_app_ids(home=Path.home()):
    app_ids = set()
    for root in steam_roots(home):
        userdata = root / "userdata"
        if not userdata.is_dir():
            continue
        for account in userdata.iterdir():
            localconfig = account / "config/localconfig.vdf"
            if not localconfig.is_file():
                continue
            text = localconfig.read_text(errors="replace")
            for app_id in re.findall(r'"(\d{1,8})"\s*\{', text):
                if app_id.isdigit() and int(app_id) > 0:
                    app_ids.add(app_id)
    return app_ids


def heroic_library_records(base):
    records = []
    candidates = (
        base / "store_cache/legendaryLibrary.json",
        base / "store_cache/gog_library.json",
        base / "store_cache/nileLibrary.json",
        base / "store_cache/gameDB.json",
        base / "GamesConfig/legendary.json",
    )
    for path in candidates:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            for key, value in payload.items():
                if isinstance(value, dict):
                    records.append((key, value))
        elif isinstance(payload, list):
            for index, value in enumerate(payload):
                if isinstance(value, dict):
                    records.append((str(index), value))
    return records


def catalog_steam(home=Path.home()):
    installed = {game["steam_app_id"]: game for game in import_steam(home)}
    owned = steam_owned_app_ids(home) or set(installed)
    try:
        executable, launch = steam_command()
    except FileNotFoundError:
        executable, launch = shutil.which("xdg-open") or "xdg-open", "xdg-open steam://rungameid/{app_id}"
    opener = shutil.which("xdg-open") or executable
    catalog = []
    for app_id in sorted(owned, key=lambda value: int(value)):
        game = installed.get(app_id)
        catalog.append({
            "id": app_id,
            "name": game["name"] if game else f"Steam App {app_id}",
            "source": "Steam",
            "installed": app_id in installed,
            "install_uri": f"steam://store/{app_id}",
            "path": game["path"] if game else opener,
            "launch": game["launch"] if game else f"xdg-open steam://store/{app_id}",
            "steam_app_id": app_id,
            "install_dir": game.get("install_dir", "") if game else "",
        })
    return catalog


def catalog_heroic(home=Path.home()):
    installed = {
        (game.get("source"), game.get("heroic_app_id")): game
        for game in import_heroic(home)
    }
    opener = shutil.which("xdg-open")
    if not opener:
        raise FileNotFoundError("xdg-open is required to browse Heroic storefront entries.")
    catalog = []
    seen = set()
    for base in heroic_bases(home):
        for key, record in heroic_library_records(base):
            source = str(record.get("store") or record.get("store_name") or record.get("platform") or "Epic")
            if source.casefold() in {"epic", "legendary"}:
                source = "Epic"
            elif source.casefold() == "gog":
                source = "GOG"
            elif source.casefold() in {"amazon", "nile"}:
                source = "Amazon"
            elif "xbox" in source.casefold() or "game pass" in source.casefold():
                source = "Xbox"
            app_id = str(
                record.get("app_name") or record.get("appName") or record.get("product_id")
                or record.get("id") or key
            )
            title = record.get("title") or record.get("app_title") or record.get("name")
            if not title or (source, app_id) in seen:
                continue
            seen.add((source, app_id))
            runner = {"Epic": "legendary", "GOG": "gog", "Amazon": "nile", "Xbox": "xbox"}.get(source, "legendary")
            installed_game = installed.get((source, app_id))
            catalog.append({
                "id": app_id,
                "name": str(title),
                "source": source,
                "installed": installed_game is not None,
                "install_uri": f"heroic://install/{runner}/{app_id}",
                "path": installed_game["path"] if installed_game else opener,
                "launch": installed_game["launch"] if installed_game else f"xdg-open heroic://install/{runner}/{app_id}",
                "heroic_app_id": app_id,
                "install_dir": installed_game.get("install_dir", "") if installed_game else "",
            })
    if not catalog:
        for game in import_heroic(home):
            catalog.append({
                "id": game["heroic_app_id"],
                "name": game["name"],
                "source": game["source"],
                "installed": True,
                "install_uri": game["launch"].replace("{heroic_app_id}", game["heroic_app_id"]),
                "path": game["path"],
                "launch": game["launch"],
                "heroic_app_id": game["heroic_app_id"],
                "install_dir": game.get("install_dir", ""),
            })
    return catalog


def catalog_lutris(home=Path.home(), run=None, which=shutil.which):
    run = run or __import__("subprocess").run
    if binary := which("lutris"):
        command = [binary]
    elif binary := which("flatpak"):
        command = [binary, "run", "net.lutris.Lutris"]
    else:
        raise FileNotFoundError("Lutris or Flatpak is required to browse the Lutris catalog.")
    result = run(
        command + ["--list-games", "--json"],
        capture_output=True, text=True, check=True, timeout=30,
    )
    output = result.stdout.strip()
    start, end = output.find("["), output.rfind("]")
    records = json.loads(output[start:end + 1] if start >= 0 and end > start else output)
    if isinstance(records, dict):
        records = records.get("games", [])
    if not isinstance(records, list):
        raise ValueError("Lutris returned an invalid game list.")
    installed_games = {game["lutris_id"]: game for game in import_lutris(home, run=run, which=which)}
    catalog = []
    for record in records:
        if not isinstance(record, dict):
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
        installed = record.get("installed") is True or game_id in installed_games
        installed_game = installed_games.get(game_id)
        catalog.append({
            "id": game_id,
            "name": name,
            "source": source,
            "installed": installed,
            "install_uri": f"lutris:rungameid/{game_id}",
            "path": installed_game["path"] if installed_game else command[0],
            "launch": installed_game["launch"] if installed_game else " ".join(command + ["lutris:rungameid/{lutris_id}"]),
            "lutris_id": game_id,
            "install_dir": installed_game.get("install_dir", "") if installed_game else "",
        })
    return catalog


def storefront_catalog(source, home=Path.home(), run=None, which=shutil.which):
    source = str(source or "").strip().casefold()
    if source == "steam":
        return catalog_steam(home)
    if source == "heroic":
        return catalog_heroic(home)
    if source == "lutris":
        return catalog_lutris(home, run=run, which=which)
    raise ValueError("Storefront source must be steam, heroic, or lutris.")


def catalog_entries_to_games(entries, *, uninstalled_only=False, installed_only=False):
    games = []
    for entry in entries:
        installed = bool(entry.get("installed"))
        if uninstalled_only and installed:
            continue
        if installed_only and not installed:
            continue
        game = {
            "name": entry["name"],
            "platform": "PC" if entry.get("source") in {"Steam", "Epic", "GOG", "Amazon", "EA", "Ubisoft", "Xbox", "Lutris"} else "Windows",
            "source": entry["source"],
            "collection": entry["source"],
            "path": entry["path"],
            "launch": entry["launch"],
            "install_dir": entry.get("install_dir", ""),
            "store_catalog": True,
            "store_installed": installed,
        }
        if entry.get("steam_app_id"):
            game["steam_app_id"] = entry["steam_app_id"]
        if entry.get("heroic_app_id"):
            game["heroic_app_id"] = entry["heroic_app_id"]
        if entry.get("lutris_id"):
            game["lutris_id"] = entry["lutris_id"]
        if not installed:
            game["notes"] = f"Storefront entry opens the {entry['source']} install page."
        games.append(game)
    return games
