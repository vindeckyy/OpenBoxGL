"""Bundled community plugin catalog."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.request import Request, urlopen

from backend_io import download_file, read_limited

CATALOG_PATH = Path(__file__).resolve().parent / "plugins" / "catalog.json"
REMOTE_CATALOG = "https://raw.githubusercontent.com/vindeckyy/OpenBoxGL/master/plugins/catalog.json"


def load_local_catalog():
    if not CATALOG_PATH.is_file():
        return []
    try:
        payload = json.loads(CATALOG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return payload if isinstance(payload, list) else []


def fetch_plugin_catalog(opener=urlopen):
    try:
        request = Request(REMOTE_CATALOG, headers={"User-Agent": "OpenBox/1"})
        with opener(request, timeout=20) as response:
            payload = json.loads(read_limited(response, 4 * 1024 * 1024))
        if isinstance(payload, list) and payload:
            return payload
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return load_local_catalog()


def download_plugin_package(entry, dest_dir, opener=urlopen):
    url = str(entry.get("url") or "").strip()
    if not url:
        raise ValueError("This catalog entry has no download URL.")
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    archive = dest / f"{entry.get('id', 'plugin')}.zip"
    download_file(
        url,
        archive,
        max_bytes=128 * 1024 * 1024,
        timeout=120,
        opener=opener,
        sha256=str(entry.get("sha256") or "").strip(),
    )
    return archive
