"""Import installed game libraries from Windows and Linux storefronts."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from pkg.platform_compat import IS_WINDOWS, epic_manifest_paths, join_command, steam_install_roots


def vdf_values(text):
    return dict(re.findall(r'"([^"]+)"\s+"([^"]*)"', text))


def steam_roots(home=None):
    if home is None:
        return [root for root in steam_install_roots() if (root / "steamapps").is_dir()]
    home = Path(home)
    candidates = (
        home / ".local/share/Steam",
        home / ".steam/steam",
        home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
    )
    return [path for path in candidates if (path / "steamapps").is_dir()]


def _flatpak_installed(app_id, run=subprocess.run):
    """True only when the named Flatpak app is actually installed."""
    flatpak = shutil.which("flatpak")
    if not flatpak:
        return False
    try:
        result = run([flatpak, "info", app_id], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _url_opener() -> str | None:
    """Return a command that opens a URL with the platform default handler."""
    if opener := shutil.which("xdg-open"):
        return opener
    if IS_WINDOWS:
        return os.environ.get("SYSTEMROOT", r"C:\Windows") + r"\explorer.exe"
    return None


def url_open_command(url) -> list[str]:
    """Return argv that opens *url* with the platform default handler."""
    opener = _url_opener()
    if opener:
        return [opener, str(url)]
    return []


def steam_command(home=None):
    if IS_WINDOWS:
        roots = steam_roots(home) if home is not None else steam_install_roots()
        for root in roots:
            executable = root / "steam.exe"
            if executable.is_file():
                return str(executable), join_command([str(executable), "-applaunch", "{app_id}"])
        if binary := shutil.which("steam"):
            return binary, "steam -applaunch {app_id}"
        if _flatpak_installed("com.valvesoftware.Steam"):
            return shutil.which("flatpak"), "flatpak run com.valvesoftware.Steam -applaunch {app_id}"
        if binary := shutil.which("xdg-open"):
            return binary, "xdg-open steam://rungameid/{app_id}"
        opener = _url_opener()
        if opener:
            return opener, join_command(url_open_command("steam://rungameid/{app_id}"))
        raise FileNotFoundError("The Steam installation could not be found.")
    if binary := shutil.which("steam"):
        return binary, "steam -applaunch {app_id}"
    if _flatpak_installed("com.valvesoftware.Steam"):
        return shutil.which("flatpak"), "flatpak run com.valvesoftware.Steam -applaunch {app_id}"
    if binary := shutil.which("xdg-open"):
        return binary, "xdg-open steam://rungameid/{app_id}"
    raise FileNotFoundError("Steam, the Steam Flatpak, or xdg-open is required to launch imported Steam games.")


def steam_libraries(root):
    libraries = {root}
    file = root / "steamapps/libraryfolders.vdf"
    if file.is_file():
        for path in re.findall(r'"path"\s+"([^"]+)"', file.read_text(encoding="utf-8", errors="replace")):
            library = Path(path.replace("\\\\", "\\"))
            if (library / "steamapps").is_dir():
                libraries.add(library)
    return sorted(libraries)


def import_steam(home=None, errors=None):
    home = home or Path.home()
    executable, command = steam_command()
    games = []
    seen = set()
    for root in steam_roots(home):
        try:
            libraries = steam_libraries(root)
        except OSError as error:
            if errors is not None:
                errors.append(f"{root}: {error}")
            continue
        for library in libraries:
            steamapps = library / "steamapps"
            if not steamapps.is_dir():
                continue
            try:
                with os.scandir(steamapps) as it:
                    for entry in it:
                        if entry.name.startswith("appmanifest_") and entry.name.endswith(".acf"):
                            try:
                                content = Path(entry.path).read_text(encoding="utf-8", errors="replace")
                            except OSError:
                                continue
                            values = vdf_values(content)
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
            except OSError as error:
                if errors is not None:
                    errors.append(f"{steamapps}: {error}")
                continue
    return games


def json_records(path):
    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace")
        data = json.loads(content)
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    if isinstance(data, list):
        return [(str(index), value) for index, value in enumerate(data) if isinstance(value, dict)]
    if isinstance(data, dict):
        return [(str(key), value) for key, value in data.items() if isinstance(value, dict)]
    return []



def heroic_bases(home=None):
    if home is not None:
        home = Path(home)
        candidates = (
            home / ".config/heroic",
            home / ".var/app/com.heroicgameslauncher.hgl/config/heroic",
        )
        return [path for path in candidates if path.is_dir()]
    candidates = []
    if IS_WINDOWS:
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "heroic")
    else:
        home = Path.home()
        candidates.extend((
            home / ".config/heroic",
            home / ".var/app/com.heroicgameslauncher.hgl/config/heroic",
        ))
    return [path for path in candidates if path.is_dir()]


def _heroic_windows_executable(record, install_dir):
    """Return a launchable Windows executable for a Heroic record, if any."""
    candidate = str(record.get("executable") or record.get("executablePath") or "").strip()
    if not candidate:
        return ""
    path = Path(candidate)
    if not path.is_absolute() and install_dir:
        path = Path(install_dir) / candidate
    return str(path) if path.is_file() else ""


def import_heroic(home=None):
    if IS_WINDOWS and home is None:
        return _import_heroic_windows(home)
    home = home or Path.home()
    opener = _url_opener()
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


def _import_heroic_windows(home=None):
    """Import Heroic records on Windows, launching executables directly."""
    base_candidates = heroic_bases(home)
    if home is not None:
        legacy = Path(home) / "AppData" / "Roaming" / "heroic"
        if legacy.is_dir():
            base_candidates.append(legacy)
    manifests = []
    for base in base_candidates:
        manifests.extend((
            ("Epic", base / "legendaryConfig/legendary/installed.json"),
            ("GOG", base / "gog_store/installed.json"),
            ("Amazon", base / "nile_config/installed.json"),
        ))
    games, seen = [], set()
    for source, manifest in manifests:
        for key, record in json_records(manifest):
            app_id = str(record.get("app_name") or record.get("appName") or record.get("product_id") or record.get("id") or key)
            title = record.get("title") or record.get("app_title") or record.get("name")
            if not title or record.get("is_dlc") or (source, app_id) in seen:
                continue
            install_dir = str(record.get("install_path") or record.get("installPath") or record.get("path") or "")
            executable = _heroic_windows_executable(record, install_dir)
            if not executable:
                continue
            seen.add((source, app_id))
            games.append({
                "name": str(title),
                "platform": "PC",
                "source": source,
                "collection": source,
                "path": executable,
                "launch": "",
                "heroic_app_id": app_id,
                "install_dir": install_dir,
            })
    return games


def import_epic(home=None):
    """Import Epic Games Launcher titles from its Windows manifests."""
    games, seen = [], set()
    for manifest in epic_manifest_paths():
        try:
            record = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(record, dict) or record.get("bIsIncompleteInstall"):
            continue
        app_id = str(record.get("AppName") or record.get("CatalogItemId") or "").strip()
        title = str(record.get("DisplayName") or record.get("AppName") or "").strip()
        install_dir = str(record.get("InstallLocation") or "").strip()
        executable = str(record.get("LaunchExecutable") or "").strip()
        if not app_id or not title or not install_dir or not executable or app_id in seen:
            continue
        launch_path = Path(install_dir) / executable
        if not launch_path.is_file():
            continue
        seen.add(app_id)
        games.append({
            "name": title,
            "platform": "PC",
            "source": "Epic",
            "collection": "Epic",
            "path": str(launch_path),
            "launch": "",
            "heroic_app_id": app_id,
            "install_dir": install_dir,
        })
    return games


def _lutris_command(home, run, which):
    """Resolve the Lutris binary, or the Lutris Flatpak run command.

    Returns (command, binary); binary is the lutris binary when found, else
    None (the flatpak branch leaves it unset, exactly like the original).
    """
    if binary := which("lutris"):
        return [binary], binary
    if which("flatpak") and _flatpak_installed("net.lutris.Lutris", run=run):
        return [which("flatpak"), "run", "net.lutris.Lutris"], None
    raise FileNotFoundError("Lutris or the Lutris Flatpak is required to import Lutris games.")


def _load_lutris_records(command, run):
    """Query Lutris and normalize the JSON game list."""
    result = run(
        command + ["--list-games", "--installed", "--json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True, timeout=30,
    )
    output = result.stdout.strip()
    start, end = output.find("["), output.rfind("]")
    records = json.loads(output[start:end + 1] if start >= 0 and end > start else output)
    if isinstance(records, dict):
        records = records.get("games", [])
    if not isinstance(records, list):
        raise ValueError("Lutris returned an invalid game list.")
    return records


def _lutris_record_source(record):
    origin = " ".join(str(record.get(key, "")) for key in ("service", "source")).lower()
    if "xbox" in origin or "game pass" in origin:
        return "Xbox"
    if "origin" in origin or "ea app" in origin:
        return "EA"
    if "ubisoft" in origin or "uplay" in origin:
        return "Ubisoft"
    return "Lutris"


def _lutris_cover_path(home, slug):
    return next((
        path for base in (
            home / ".local/share/lutris/coverart",
            home / ".var/app/net.lutris.Lutris/data/lutris/coverart",
        ) for suffix in (".jpg", ".png", ".webp")
        if slug and (path := base / f"{slug}{suffix}").is_file()
    ), "")


def _lutris_game_entry(record, home, command, binary):
    if not isinstance(record, dict) or record.get("installed") is False:
        return None
    game_id = str(record.get("id", "")).strip()
    name = str(record.get("name", "")).strip()
    if not game_id.isdigit() or not name:
        return None
    source = _lutris_record_source(record)
    runner = str(record.get("runner", "")).strip()
    slug = str(record.get("slug") or record.get("game_slug") or "").strip()
    cover = _lutris_cover_path(home, slug)
    game = {
        "name": name,
        "platform": str(record.get("platform") or ("Windows" if runner in {"wine", "winesteam"} else "PC")),
        "source": source,
        "collection": source,
        "path": binary or str(record.get("directory") or record.get("path") or command[0]),
        "launch": join_command(command + ["lutris:rungameid/{lutris_id}"]),
        "lutris_id": game_id,
        "install_dir": str(record.get("directory") or record.get("path") or ""),
    }
    if cover:
        game["cover"] = str(cover)
    return game


def import_lutris(home=None, run=subprocess.run, which=shutil.which):
    home = home or Path.home()
    command, binary = _lutris_command(home, run, which)
    records = _load_lutris_records(command, run)
    games = []
    for record in records:
        game = _lutris_game_entry(record, home, command, binary)
        if game:
            games.append(game)
    return games
