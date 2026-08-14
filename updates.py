"""Verified GitHub release updates for the OpenBox AppImage."""

import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from backend_io import atomic_write_bytes, atomic_write_text, download_file, read_limited

VERSION = "1.0.0"
RELEASE_API = "https://api.github.com/repos/vindeckyy/OpenBoxGL/releases/latest"
ASSET = "OpenBox-x86_64.AppImage"
TRUSTED_RELEASE_PREFIX = "https://github.com/vindeckyy/OpenBoxGL/releases/download/"


def version_tuple(value):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(value).strip())
    if not match:
        raise ValueError("The release has an invalid version.")
    return tuple(map(int, match.groups()))


def _version_key(value):
    """Compare with pre-release/build suffix awareness (suffix sorts lower)."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)([-+].*)?", str(value).strip())
    if not match:
        raise ValueError("The release has an invalid version.")
    return tuple(map(int, match.groups()[:3])) + (1 if not match.group(4) else 0,)

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


def asset_digest(asset):
    digest = str(asset.get("digest", "")).strip()
    if digest.startswith("sha256:"):
        value = digest.split(":", 1)[1].lower()
        if re.fullmatch(r"[0-9a-f]{64}", value):
            return value
    return ""


def parse_release_assets(release):
    urls = {}
    digests = {}
    for asset in release.get("assets", []):
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name", "")).strip()
        if not name:
            continue
        url = str(asset.get("browser_download_url", "")).strip()
        if url:
            urls[name] = url
        digest = asset_digest(asset)
        if digest:
            digests[name] = digest
    return urls, digests


def load_checksum_file(url, opener=urlopen):
    with github_request(url, opener=opener) as response:
        parts = read_limited(response, 4096).decode().split()
    if not parts:
        raise ValueError("The release checksum is invalid.")
    expected = parts[0].lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("The release checksum is invalid.")
    return expected


def resolve_update_checksum(update, opener=urlopen):
    checksum = str(update.get("checksum", "")).strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", checksum):
        return checksum
    checksum_url = str(update.get("checksum_url", "")).strip()
    if checksum_url.startswith(TRUSTED_RELEASE_PREFIX):
        return load_checksum_file(checksum_url, opener=opener)
    raise ValueError("The release checksum is unavailable.")


def check_update(opener=urlopen):
    try:
        with github_request(RELEASE_API, opener=opener) as response:
            release = json.loads(read_limited(response, 8 * 1024 * 1024))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:200]
        raise ValueError(f"GitHub releases request failed ({error.code}): {detail or error.reason}") from error
    except URLError as error:
        raise ValueError(f"Could not reach GitHub releases: {error.reason}") from error

    version = str(release.get("tag_name", ""))
    urls, digests = parse_release_assets(release)
    appimage = urls.get(ASSET, "")
    checksum = digests.get(ASSET, "")
    checksum_url = urls.get(f"{ASSET}.sha256", "")
    try:
        release_available = _version_key(version) > _version_key(VERSION)
    except ValueError:
        release_available = False
    if release_available and re.search(r"[-+]", version):
        # Never auto-update to a pre-release or build-suffixed tag.
        release_available = False
    if release_available and not appimage.startswith(TRUSTED_RELEASE_PREFIX):
        raise ValueError("The release is missing verified OpenBox update assets.")
    if release_available and not checksum and not checksum_url:
        raise ValueError("The release is missing a SHA-256 checksum for the AppImage.")
    return {
        "current": VERSION,
        "latest": version.lstrip("v"),
        "available": release_available,
        "notes": str(release.get("body", ""))[:4000],
        "appimage": appimage,
        "checksum": checksum,
        "checksum_url": checksum_url,
        "page": str(release.get("html_url", "")),
    }


def install_update(update, destination=None, opener=urlopen):
    destination = Path(destination or os.environ.get("APPIMAGE", "")).expanduser()
    if not destination.is_file():
        raise ValueError("Automatic updates require the OpenBox AppImage.")
    if not update.get("available"):
        raise ValueError("OpenBox is already up to date.")
    appimage = str(update.get("appimage", "")).strip()
    if not appimage.startswith(TRUSTED_RELEASE_PREFIX):
        raise ValueError("The update URLs are not trusted OpenBox release assets.")
    expected = resolve_update_checksum(update, opener=opener)
    temporary = destination.with_name(f".{destination.name}.update")
    try:
        download_file(
            update["appimage"], temporary, max_bytes=2 * 1024 * 1024 * 1024,
            timeout=60, opener=opener, sha256=expected,
        )
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
    atomic_write_bytes(icon, (Path(__file__).parent / "openbox.svg").read_bytes(), mode=0o644)
    desktop = applications / "io.openbox.GameLauncher.desktop"
    atomic_write_text(desktop, (
        "[Desktop Entry]\n"
        "Name=OpenBox\n"
        "Comment=Local-first Linux game library and launcher\n"
        f'Exec="{executable}"\n'
        "Icon=io.openbox.GameLauncher\n"
        "Terminal=false\n"
        "Type=Application\n"
        "Categories=Game;Emulator;\n"
        "Keywords=games;launcher;emulator;rom;\n"
    ), mode=0o755)
    desktop.chmod(0o755)
    return str(desktop)
