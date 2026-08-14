"""Bundled stock CSS themes for the OpenBox Web UI."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

STOCK_MARKER = "/* OpenBox Stock Theme"
_VERSION_PATTERN = re.compile(r"^/\* OpenBox Stock Theme v(\d+):")


def stock_theme_sources(root=None):
    root = Path(root or Path(__file__).resolve().parent)
    themes_dir = root / "themes"
    if not themes_dir.is_dir():
        return []
    return sorted(path for path in themes_dir.glob("*.css") if path.is_file())


def is_stock_theme(path):
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            head = handle.read(120)
    except OSError:
        return False
    return head.lstrip().startswith(STOCK_MARKER)


def _stock_version(path):
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            head = handle.read(120)
    except OSError:
        return None
    match = _VERSION_PATTERN.match(head.lstrip())
    return int(match.group(1)) if match else 0


def ensure_stock_themes(destination, root=None):
    """Install or refresh bundled stock themes; user imports (no stock marker) are left untouched."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    installed = []
    for source in stock_theme_sources(root):
        target = destination / source.name
        if target.exists() and not is_stock_theme(target):
            continue
        if target.exists() and _stock_version(target) >= _stock_version(source):
            continue
        shutil.copy2(source, target)
        installed.append(target.stem)
    return installed
