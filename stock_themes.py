"""Bundled stock CSS themes for the OpenBox Web UI."""

from __future__ import annotations

import shutil
from pathlib import Path

STOCK_MARKER = "/* OpenBox Stock Theme:"


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


def ensure_stock_themes(destination, root=None):
    """Install or refresh bundled stock themes into the user themes folder.

    User-imported themes without the stock marker are left untouched, and
    user edits to a stock theme are preserved (only missing or identical
    files are written).
    """
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    installed = []
    for source in stock_theme_sources(root):
        target = destination / source.name
        if target.exists() and not is_stock_theme(target):
            continue
        if target.exists() and target.read_bytes() != source.read_bytes():
            # Keep the user's customized copy.
            continue
        shutil.copy2(source, target)
        installed.append(target.stem)
    return installed
