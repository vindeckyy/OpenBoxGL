"""Verified GitHub release updates for the OpenBox AppImage."""

import hashlib
import json
import os
import re
from pathlib import Path
from urllib.request import Request, urlopen

VERSION = "0.3.0"
RELEASE_API = "https://api.github.com/repos/vindeckyy/OpenBox/releases/latest"
ASSET = "OpenBox-x86_64.AppImage"


def version_tuple(value):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(value).strip())
    if not match:
        raise ValueError("The release has an invalid version.")
    return tuple(map(int, match.groups()))


def github_request(url, opener=urlopen):
    from env_config import github_token_from_env

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"OpenBox/{VERSION}",
    }
    token = github_token_from_env()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return opener(Request(url, headers=headers), timeout=30)


def check_update(opener=urlopen):
    with github_request(RELEASE_API, opener=opener) as response:
        release = json.load(response)
    version = str(release.get("tag_name", ""))
    assets = {asset.get("name"):asset.get("browser_download_url") for asset in release.get("assets", []) if isinstance(asset, dict)}
    expected = f"https://github.com/vindeckyy/OpenBox/releases/download/"
    appimage, checksum = assets.get(ASSET, ""), assets.get(f"{ASSET}.sha256", "")
    if version_tuple(version) > version_tuple(VERSION) and (not appimage.startswith(expected) or not checksum.startswith(expected)):
        raise ValueError("The release is missing verified OpenBox update assets.")
    return {
        "current": VERSION,
        "latest": version.lstrip("v"),
        "available": version_tuple(version) > version_tuple(VERSION),
        "notes": str(release.get("body", ""))[:4000],
        "appimage": appimage,
        "checksum": checksum,
        "page": str(release.get("html_url", "")),
    }


def install_update(update, destination=None, opener=urlopen):
    destination = Path(destination or os.environ.get("APPIMAGE", "")).expanduser()
    if not destination.is_file():
        raise ValueError("Automatic updates require the OpenBox AppImage.")
    if not update.get("available"):
        raise ValueError("OpenBox is already up to date.")
    expected_url = "https://github.com/vindeckyy/OpenBox/releases/download/"
    if not str(update.get("appimage", "")).startswith(expected_url) or not str(update.get("checksum", "")).startswith(expected_url):
        raise ValueError("The update URLs are not trusted OpenBox release assets.")
    with github_request(update["checksum"], opener=opener) as response:
        expected = response.read(4096).decode().split()[0].lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("The release checksum is invalid.")
    temporary = destination.with_name(f".{destination.name}.update")
    digest = hashlib.sha256()
    try:
        with github_request(update["appimage"], opener=opener) as response, temporary.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != expected:
            raise ValueError("The downloaded update failed SHA-256 verification.")
        temporary.chmod(destination.stat().st_mode)
        backup = destination.with_name(f"{destination.stem}.previous{destination.suffix}")
        if backup.exists():
            backup.unlink()
        destination.replace(backup)
        try:
            temporary.replace(destination)
        except OSError:
            backup.replace(destination)
            raise
    finally:
        temporary.unlink(missing_ok=True)
    return {"installed": update["latest"], "backup": str(backup)}


def install_desktop_entry(appimage=None):
    appimage = Path(appimage or os.environ.get("APPIMAGE", "")).expanduser()
    if not appimage.is_file():
        raise ValueError("Desktop integration requires the OpenBox AppImage.")
    executable = str(appimage)
    if "\n" in executable:
        raise ValueError("The AppImage path is not valid for a desktop entry.")
    executable = executable.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    applications = Path.home() / ".local/share/applications"
    icons = Path.home() / ".local/share/icons/hicolor/scalable/apps"
    applications.mkdir(parents=True, exist_ok=True)
    icons.mkdir(parents=True, exist_ok=True)
    icon = icons / "io.openbox.GameLauncher.svg"
    icon.write_bytes((Path(__file__).parent / "openbox.svg").read_bytes())
    desktop = applications / "io.openbox.GameLauncher.desktop"
    desktop.write_text(
        "[Desktop Entry]\n"
        "Name=OpenBox\n"
        "Comment=Local-first Linux game library and launcher\n"
        f'Exec="{executable}"\n'
        "Icon=io.openbox.GameLauncher\n"
        "Terminal=false\n"
        "Type=Application\n"
        "Categories=Game;Emulator;\n"
        "Keywords=games;launcher;emulator;rom;\n"
    )
    desktop.chmod(0o755)
    return str(desktop)
