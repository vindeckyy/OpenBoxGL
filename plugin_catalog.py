"""Bundled community plugin catalog."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from backend_io import download_file, read_limited

CATALOG_PATH = Path(__file__).resolve().parent / "plugins" / "catalog.json"
REMOTE_CATALOG = "https://raw.githubusercontent.com/vindeckyy/OpenBoxGL/566f57e276cd5fffb587675c970bc86f50dfbccb/plugins/catalog.json"
REMOTE_CATALOG_SHA256 = "8569fa78415cdd5b0042cfef36ed8ef40fe9df5cea3f536f1c6e5d79f5d95a78"
PLUGIN_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")


def _validate_download_url(url, plugin_id):
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"Catalog entry {plugin_id} has an invalid download URL.") from error
    if (
        parsed.scheme.casefold() != "https"
        or not host
        or parsed.username
        or parsed.password
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError(f"Catalog entry {plugin_id} must use an HTTPS download URL.")
    return parsed


def _validate_entry(entry):
    if not isinstance(entry, dict):
        raise ValueError("The plugin catalog contains a malformed entry.")
    plugin_id = str(entry.get("id") or "").strip()
    if not PLUGIN_ID.fullmatch(plugin_id):
        raise ValueError("The plugin catalog contains an invalid plugin id.")
    if not str(entry.get("name") or "").strip() or not str(entry.get("version") or "").strip():
        raise ValueError(f"Catalog entry {plugin_id} is missing a name or version.")
    url = str(entry.get("url") or "").strip()
    if url:
        _validate_download_url(url, plugin_id)
        sha256 = str(entry.get("sha256") or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
            raise ValueError(f"Catalog entry {plugin_id} is missing a valid sha256 checksum.")
    elif not entry.get("local_only"):
        raise ValueError(f"Catalog entry {plugin_id} has no download URL.")
    return dict(entry, id=plugin_id, url=url)


def _validate_catalog(payload):
    if not isinstance(payload, list):
        raise ValueError("The plugin catalog must be a list.")
    return [_validate_entry(entry) for entry in payload]


def load_local_catalog():
    if not CATALOG_PATH.is_file():
        return []
    try:
        payload = json.loads(CATALOG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    try:
        return _validate_catalog(payload)
    except ValueError:
        return []


def fetch_plugin_catalog(opener=urlopen):
    try:
        request = Request(REMOTE_CATALOG, headers={"User-Agent": "OpenBox/1"})
        with opener(request, timeout=20) as response:
            raw = read_limited(response, 4 * 1024 * 1024)
        if hashlib.sha256(raw).hexdigest() != REMOTE_CATALOG_SHA256:
            raise ValueError("The remote plugin catalog failed its pinned checksum.")
        payload = _validate_catalog(json.loads(raw))
        if payload:
            return payload
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return load_local_catalog()


def download_plugin_package(entry, dest_dir, opener=urlopen):
    if not isinstance(entry, dict):
        raise ValueError("The catalog entry is invalid.")
    plugin_id = str(entry.get("id") or "").strip()
    if not PLUGIN_ID.fullmatch(plugin_id):
        raise ValueError("The catalog entry has an invalid plugin id.")
    url = str(entry.get("url") or "").strip()
    if not url:
        raise ValueError("This catalog entry has no download URL.")
    _validate_download_url(url, plugin_id)
    sha256 = str(entry.get("sha256") or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
        raise ValueError(
            f"Catalog entry {entry.get('id', '?')} is missing a valid sha256 checksum; refusing to download."
        )
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    archive = dest / f"{plugin_id}.zip"
    download_file(
        url,
        archive,
        max_bytes=128 * 1024 * 1024,
        timeout=120,
        opener=opener,
        sha256=sha256,
    )
    return archive
