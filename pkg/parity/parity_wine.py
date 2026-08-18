"""Wine prefix and Proton version discovery for Linux.

Local-first, dependency-free. Scans common prefix locations and Proton
installations without network access.
"""

from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path


# Common Wine prefix roots users actually use (including Lutris, Bottles, Faugus, Steam).
DEFAULT_PREFIX_ROOTS = [
    "~/.wine",
    "~/.local/share/wineprefixes",
    "~/.local/share/lutris/runners/wine",
    "~/.config/faugus-launcher/prefixes",
    "~/.local/share/bottles/bottles",
    "~/Games",
    "~/Faugus",
]


# Known Proton/ Wine runner locations.
DEFAULT_PROTON_ROOTS = [
    "~/.steam/root/compatibilitytools.d",
    "~/.steam/steam/compatibilitytools.d",
    "~/.local/share/Steam/compatibilitytools.d",
    "~/.local/share/lutris/runners/wine",
    "~/.config/faugus-launcher/runners",
    "~/.local/share/faugus-launcher/runners",
]


def _expand_roots(roots):
    out = []
    for raw in roots:
        p = Path(raw).expanduser()
        if p.is_dir():
            out.append(p)
    return out


def list_wine_prefixes(search_roots=None):
    """Scan for Wine prefixes. Returns list of {path, has_drive_c, size_mb}."""
    roots = search_roots if search_roots is not None else DEFAULT_PREFIX_ROOTS
    prefixes = []
    seen = set()
    # Expand and also scan one level deep for container dirs (Bottles, Lutris, Faugus)
    base_dirs = _expand_roots(roots)
    candidates = []
    for base in base_dirs:
        # If base itself looks like a prefix (has drive_c), add it
        if (base / "drive_c").is_dir():
            candidates.append(base)
        # Scan children one level (e.g., Bottles/<bottle>/, Faugus/<game>/)
        try:
            for child in base.iterdir():
                if not child.is_dir() or child.is_symlink():
                    continue
                if (child / "drive_c").is_dir():
                    candidates.append(child)
                # Bottles nests one deeper: Bottles/<bottle>/drive_c?
                # Also Lutris runners are not prefixes, skip them here
                try:
                    for grand in child.iterdir():
                        if (grand / "drive_c").is_dir():
                            candidates.append(grand)
                except OSError:
                    pass
        except OSError:
            pass
    # Also scan via WINEPREFIX env if set
    env_prefix = os.environ.get("WINEPREFIX", "").strip()
    if env_prefix:
        p = Path(env_prefix).expanduser()
        if p.is_dir() and (p / "drive_c").is_dir():
            candidates.append(p)
    for p in candidates:
        try:
            real = p.resolve()
        except OSError:
            real = p
        key = str(real)
        if key in seen:
            continue
        seen.add(key)
        # Size: sum of a few top-level entries to avoid walking entire prefix (can be large)
        has_drive_c = (p / "drive_c").is_dir()
        prefixes.append({
            "path": str(p),
            "has_drive_c": has_drive_c,
            "name": p.name,
        })
    return sorted(prefixes, key=lambda x: x["path"])


def list_proton_versions(search_roots=None):
    """List available Proton/Wine runners. Returns list of {name, path, source}."""
    versions = []
    seen_paths = set()
    # Check executables on PATH
    for exe in ("wine", "wine64", "proton", "umu-run", "umu-launcher"):
        which = shutil.which(exe)
        if which:
            key = which
            if key not in seen_paths:
                seen_paths.add(key)
                versions.append({"name": exe, "path": which, "source": "PATH"})
    # Check flatpak runners
    if shutil.which("flatpak"):
        try:
            import subprocess
            result = subprocess.run(
                ["flatpak", "list", "--app", "--columns=application"],
                capture_output=True, text=True, timeout=5, check=False
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    app = line.strip()
                    if "wine" in app.lower() or "proton" in app.lower() or "faugus" in app.lower():
                        versions.append({"name": app, "path": f"flatpak:{app}", "source": "flatpak"})
        except Exception:
            pass
    # Scan known Proton roots for version directories containing proton or wine binary
    roots = search_roots if search_roots is not None else DEFAULT_PROTON_ROOTS
    for base in _expand_roots(roots):
        try:
            for child in base.iterdir():
                if not child.is_dir() or child.is_symlink():
                    continue
                # Proton dirs typically contain 'proton' file or 'files/bin/wine' or just version name
                has_proton = (
                    (child / "proton").is_file()
                    or (child / "files" / "bin" / "wine").is_file()
                    or (child / "dist" / "bin" / "wine").is_file()
                    or child.name.lower().startswith("proton")
                    or "proton" in child.name.lower()
                    or "wine" in child.name.lower()
                )
                if has_proton:
                    key = str(child.resolve()) if child.exists() else str(child)
                    if key not in seen_paths:
                        seen_paths.add(key)
                        versions.append({"name": child.name, "path": str(child), "source": str(base)})
        except OSError:
            pass
    return sorted(versions, key=lambda x: x["name"])


def get_prefix_for_game(game):
    """Return the Wine prefix configured for a game, if any."""
    # Games can store prefix in launch_profile, wine_prefix, or WINEPREFIX in launch command
    for key in ("wine_prefix", "prefix", "WINEPREFIX"):
        val = game.get(key, "")
        if val and isinstance(val, str):
            p = Path(val).expanduser()
            if p.is_dir():
                return str(p)
    launch = str(game.get("launch", ""))
    if "WINEPREFIX=" in launch:
        try:
            parts = shlex.split(launch)
        except ValueError:
            parts = []
        for part in parts:
            if part.startswith("WINEPREFIX="):
                val = part.split("=", 1)[1]
                val = os.path.expandvars(os.path.expanduser(val)).strip()
                if val:
                    return val
    return ""
