"""Faugus Launcher integration via UMU-Launcher.

Scans Faugus launcher config and UMU prefixes to import games that Faugus manages.
Faugus stores per-game JSON configs and prefixes in:
  ~/.config/faugus-launcher/prefixes/<gameid>/
  ~/.config/faugus-launcher/games/*.json  (or similar)
  ~/Faugus/<game>/
  ~/.local/share/faugus-launcher/

This module is read-only and never launches games directly; it returns candidates
for the source adapter flow.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


FAUGUS_CONFIG_DIRS = [
    "~/.config/faugus-launcher",
    "~/.local/share/faugus-launcher",
]

FAUGUS_GAMES_DIRS = [
    "~/Faugus",
    "~/.config/faugus-launcher/prefixes",
    "~/.local/share/faugus-launcher/prefixes",
]


def find_faugus_data_dirs():
    """Return existing Faugus data directories."""
    found = []
    for raw in FAUGUS_CONFIG_DIRS + FAUGUS_GAMES_DIRS:
        p = Path(raw).expanduser()
        if p.is_dir():
            found.append(str(p))
    # Also check XDG locations
    xdg_config = os.environ.get("XDG_CONFIG_HOME", "")
    xdg_data = os.environ.get("XDG_DATA_HOME", "")
    for base in (xdg_config, xdg_data):
        if base:
            for sub in ("faugus-launcher", "faugus"):
                p = Path(base) / sub
                if p.is_dir() and str(p) not in found:
                    found.append(str(p))
    return sorted(set(found))


def _load_faugus_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def scan_faugus_games(data_dirs=None):
    """Return candidate game dicts found in Faugus launcher data.

    Each candidate contains: source, source_identity, name, path, prefix, runner.
    Returns empty list when Faugus is not installed.
    """
    dirs = data_dirs
    if dirs is None:
        dirs = find_faugus_data_dirs()
    candidates = []
    seen = set()
    for raw in dirs:
        base = Path(raw)
        if not base.is_dir():
            continue
        # Look for per-game JSON configs
        for pattern in ("*.json", "games/*.json", "configs/*.json"):
            try:
                for cfg in base.glob(pattern):
                    if not cfg.is_file() or cfg.is_symlink():
                        continue
                    data = _load_faugus_json(cfg)
                    if not isinstance(data, dict):
                        continue
                    # Faugus configs vary: try common keys
                    name = str(data.get("name") or data.get("title") or data.get("game_name") or cfg.stem).strip()
                    game_id = str(data.get("game_id") or data.get("id") or data.get("GAMEID") or cfg.stem).strip()
                    if not name or not game_id:
                        continue
                    prefix = str(data.get("prefix") or data.get("WINEPREFIX") or data.get("wine_prefix") or "").strip()
                    exe = str(data.get("exe") or data.get("path") or data.get("executable") or "").strip()
                    runner = str(data.get("runner") or data.get("PROTONPATH") or data.get("proton") or "").strip()
                    identity = f"faugus:{game_id}"
                    if identity in seen:
                        continue
                    seen.add(identity)
                    candidates.append({
                        "source": "Faugus",
                        "source_identity": identity,
                        "faugus_id": game_id,
                        "name": name,
                        "path": exe,
                        "prefix": prefix,
                        "runner": runner,
                        "config_path": str(cfg),
                    })
            except OSError:
                pass
        # Also look for prefix directories as fallback (derive game from prefix name)
        prefixes_dir = base / "prefixes" if (base / "prefixes").is_dir() else base
        if base.name == "prefixes" or (base / "drive_c").is_dir():
            try:
                for child in prefixes_dir.iterdir():
                    if not child.is_dir() or child.is_symlink():
                        continue
                    if not (child / "drive_c").is_dir() and not child.name:
                        continue
                    game_id = child.name
                    identity = f"faugus:{game_id}"
                    if identity in seen:
                        continue
                    # Only add if no JSON already covered it
                    seen.add(identity)
                    candidates.append({
                        "source": "Faugus",
                        "source_identity": identity,
                        "faugus_id": game_id,
                        "name": game_id.replace("-", " ").title(),
                        "path": "",
                        "prefix": str(child),
                        "runner": "",
                        "config_path": "",
                    })
            except OSError:
                pass
    return sorted(candidates, key=lambda x: x["name"].lower())
