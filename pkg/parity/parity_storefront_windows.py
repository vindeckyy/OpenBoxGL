"""Native GOG, EA app, and Ubisoft Connect discovery on Windows.

Read-only and offline. Registry values and installer manifests are treated as
untrusted input: ids must be digits, install dirs must exist, and any
executable taken from a GOG manifest must stay inside its install dir.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from xml.etree import ElementTree

from pkg.platform_compat import join_command, split_command, windows_registry_tree

ID_RE = re.compile(r"^[0-9]{1,32}$")
GOG_REGISTRY = (
    ("HKCU", r"Software\GOG.com\Games"),
    ("HKLM", r"SOFTWARE\WOW6432Node\GOG.com\Games"),
    ("HKLM", r"SOFTWARE\GOG.com\Games"),
)
EA_REGISTRY = (
    ("HKLM", r"SOFTWARE\EA Games"),
    ("HKLM", r"SOFTWARE\WOW6432Node\EA Games"),
)
UBISOFT_REGISTRY = (
    ("HKCU", r"SOFTWARE\Ubisoft\Launcher\Installs"),
    ("HKLM", r"SOFTWARE\WOW6432Node\Ubisoft\Launcher\Installs"),
)
MAX_INFO_BYTES = 1_048_576
MAX_XML_BYTES = 2_097_152
MAX_INFO_DIRS = 100


def _error(errors, message):
    if errors is not None:
        errors.append(message)


def _id(value):
    text = str(value or "").strip()
    return text if ID_RE.fullmatch(text) else ""


def _dir(value):
    text = str(value or "").strip().strip('"')
    if not text:
        return None
    path = Path(os.path.expandvars(text)).expanduser()
    return path if path.is_absolute() and path.is_dir() else None


def _inside(base: Path, target: Path) -> bool:
    try:
        root = os.path.normcase(os.path.abspath(str(base)))
        candidate = os.path.normcase(os.path.abspath(str(target)))
        return os.path.commonpath([root, candidate]) == root
    except ValueError:  # pragma: no cover - different drives on Windows
        return False


def _gog_install_dir(values: dict):
    path = _dir(values.get("path"))
    if path is not None:
        return path
    raw_exe = str(values.get("exe") or "").strip().strip('"')
    if not raw_exe:
        return None
    exe = Path(os.path.expandvars(raw_exe)).expanduser()
    return exe.parent if exe.is_absolute() and exe.is_file() else None


def _relative(install_dir: Path, value) -> Path:
    return install_dir / str(value or "").replace("\\", os.sep)


def _read_json(path: Path):
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    if len(text) > MAX_INFO_BYTES:
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _find_gog_info(install_dir: Path, game_id: str):
    direct = install_dir / f"goggame-{game_id}.info"
    if direct.is_file():
        return direct
    top_level = []
    nested = []
    try:
        entries = sorted(install_dir.iterdir())
    except OSError:
        return None
    for entry in entries[:MAX_INFO_DIRS]:
        try:
            if entry.is_file() and entry.name.startswith("goggame-") and entry.suffix == ".info":
                top_level.append(entry)
            elif entry.is_dir():
                nested.extend(
                    child for child in sorted(entry.glob("goggame-*.info")) if child.is_file()
                )
        except OSError:  # pragma: no cover - defensive filesystem error
            continue
    candidates = top_level + nested
    match = next((item for item in candidates if str(item.stem).endswith(str(game_id))), None)
    return match or (candidates[0] if candidates else None)


def _gog_task(install_dir: Path, info):
    tasks = info.get("playTasks") if isinstance(info, dict) else None
    if not isinstance(tasks, list):
        return "", []
    chosen = next(
        (task for task in tasks if isinstance(task, dict) and task.get("isPrimary")),
        next((task for task in tasks if isinstance(task, dict) and task.get("path")), None),
    )
    if not chosen:
        return "", []
    exe = _relative(install_dir, chosen.get("path"))
    if not exe.is_file() or not _inside(install_dir, exe):
        return "", []
    arguments = str(chosen.get("arguments") or "").strip()
    return str(exe), split_command(arguments) if arguments else []


def _gog_game(install_dir: Path, game_id: str, values: dict, errors=None):
    info_path = _find_gog_info(install_dir, game_id)
    info = _read_json(info_path) if info_path else None
    if info is not None:
        info_id = _id(info.get("gameId"))
        if info_id and info_id != game_id:
            info = None
    if isinstance(info, dict):
        name = str(info.get("name") or "").strip()
    else:
        name = ""
    name = name or str(values.get("gameName") or "").strip() or install_dir.name
    exe, arguments = _gog_task(install_dir, info) if info else ("", [])
    if not exe:
        raw_exe = str(values.get("exe") or "").strip().strip('"')
        if raw_exe:
            candidate = _relative(install_dir, raw_exe)
            if candidate.is_file() and _inside(install_dir, candidate):
                exe = str(candidate)
                raw_args = str(values.get("launchParam") or "").strip()
                arguments = split_command(raw_args) if raw_args else []
    if not exe:
        _error(errors, f"GOG {name}: no launchable executable in {install_dir}")
        return None
    return {
        "name": name,
        "platform": "PC",
        "source": "GOG",
        "collection": "GOG",
        "path": exe,
        "launch": join_command([exe, *arguments]) if arguments else "",
        "gog_id": game_id,
        "install_dir": str(install_dir),
    }


def import_gog(*, registry=None, errors=None) -> list[dict]:
    """Import installed GOG games from the registry and their .info files."""
    read = registry or windows_registry_tree
    games = []
    seen = set()
    for root, path in GOG_REGISTRY:
        tree = read(root, path) or {}
        for key, values in tree.items():
            game_id = _id(key)
            if not game_id or game_id in seen or not isinstance(values, dict):
                continue
            install_dir = _gog_install_dir(values)
            if install_dir is None:
                _error(errors, f"GOG {game_id}: install directory is missing")
                continue
            game = _gog_game(install_dir, game_id, values, errors=errors)
            if game is None:
                continue
            seen.add(game_id)
            games.append(game)
    return games


def _explorer(environ, opener=None) -> str:
    if opener:
        return str(opener)
    root = environ.get("SYSTEMROOT") or r"C:\Windows"
    return str(Path(root) / "explorer.exe")


def _ea_roots(environ, registry) -> list[Path]:
    roots = []
    for name in ("ProgramFiles", "ProgramFiles(x86)"):
        value = environ.get(name)
        if value:
            roots.append(Path(value) / "EA Games")
    read = registry or windows_registry_tree
    for root, path in EA_REGISTRY:
        for values in (read(root, path) or {}).values():
            if not isinstance(values, dict):
                continue
            value = values.get("Install Dir") or values.get("InstallDir") or values.get("Path")
            if value:
                roots.append(Path(os.path.expandvars(str(value))))
    seen = set()
    unique = []
    for path in roots:
        key = os.path.normcase(str(path))
        if key not in seen and path.is_dir():
            seen.add(key)
            unique.append(path)
    return unique


def _ea_content_ids(path: Path) -> list[str]:
    try:
        if path.stat().st_size > MAX_XML_BYTES:
            return []
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError):
        return []
    ids = []
    for node in root.iter("contentID"):
        value = _id(node.text)
        if value and value not in ids:
            ids.append(value)
    return sorted(ids)


def import_ea(*, environ=None, registry=None, opener=None, errors=None) -> list[dict]:
    """Import games installed by the EA app from its installer manifests."""
    environ = os.environ if environ is None else environ
    explorer = _explorer(environ, opener)
    games = []
    seen = set()
    for root in _ea_roots(environ, registry):
        try:
            children = [root, *sorted(root.iterdir())] if root.is_dir() else []
        except OSError:  # pragma: no cover - defensive filesystem error
            continue
        for game_dir in children:
            try:
                if not game_dir.is_dir():
                    continue
            except OSError:  # pragma: no cover - defensive filesystem error
                continue
            installer_dir = game_dir / "__Installer"
            if not installer_dir.is_dir():
                continue
            content_ids = _ea_content_ids(installer_dir / "installerdata.xml")
            if not content_ids:
                _error(errors, f"EA {game_dir.name}: no installerdata.xml content id")
                continue
            game_id = content_ids[0]
            if game_id in seen:
                continue
            seen.add(game_id)
            offer_ids = ",".join(content_ids)
            uri = f"origin2://game/launch?offerIds={offer_ids}&autoDownload=1"
            games.append({
                "name": game_dir.name,
                "platform": "PC",
                "source": "EA",
                "collection": "EA",
                "path": explorer,
                "launch": join_command([explorer, uri]),
                "ea_id": game_id,
                "install_dir": str(game_dir),
            })
    return games


def import_ubisoft(*, registry=None, opener=None, environ=None, errors=None) -> list[dict]:
    """Import games installed by Ubisoft Connect from its Installs registry keys."""
    environ = os.environ if environ is None else environ
    explorer = _explorer(environ, opener)
    read = registry or windows_registry_tree
    games = []
    seen = set()
    for root, path in UBISOFT_REGISTRY:
        tree = read(root, path) or {}
        for key, values in tree.items():
            game_id = _id(key)
            if not game_id or game_id in seen or not isinstance(values, dict):
                continue
            install_dir = _dir(values.get("InstallDir"))
            if install_dir is None:
                _error(errors, f"Ubisoft {game_id}: install directory is missing")
                continue
            seen.add(game_id)
            games.append({
                "name": install_dir.name,
                "platform": "PC",
                "source": "Ubisoft",
                "collection": "Ubisoft",
                "path": explorer,
                "launch": join_command([explorer, f"uplay://launch/{game_id}/0"]),
                "ubisoft_id": game_id,
                "install_dir": str(install_dir),
            })
    return games


__all__ = ["import_gog", "import_ea", "import_ubisoft"]
