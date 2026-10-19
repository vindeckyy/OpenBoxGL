"""Game discovery capability plugins (P10.5).

A plugin may publish up to ten named lists of library game ids. The host
resolves them to the same index rows ``/api/discovery`` already returns, so
the frontend needs no second payload shape, and drops unknown ids rather
than inventing games.
"""

from __future__ import annotations

import logging
from pathlib import Path
import re

from pkg.parity.parity_plugin_storefront import (
    default_plugins_dir,
    plugin_manifest,
    require_plugin_capability,
)
from plugins import run_plugin_hook

LOGGER = logging.getLogger("openbox.plugins.discovery")

MAX_LISTS = 10
MAX_IDS = 50
MAX_ID_LENGTH = 160
MAX_NAME_LENGTH = 80
_ID_RE = re.compile(r"[^a-z0-9._-]+")


def _slug(value):
    cleaned = _ID_RE.sub("-", str(value or "").strip().casefold()).strip("-")
    return cleaned[:80]


def _empty(error=""):
    payload = {"lists": {}, "names": {}, "game_ids": {}, "truncated": False}
    if error:
        payload["error"] = error[:500]
    return payload


def merge_plugin_discovery(payload, games, limit=MAX_IDS, *, plugins_dir=None):
    """Merge enabled discovery plugins into an existing ``/api/discovery`` payload.

    Built-in list keys are preserved; plugin lists are added under
    ``plugin:<plugin_id>:<list_id>`` with matching ``discovery_names`` entries.
    A failing optional plugin is skipped rather than breaking the Discovery
    Center, and the existing index-based list shape is unchanged.
    """
    from plugins import list_plugins

    if not isinstance(payload, dict):
        payload = {}
    root = Path(plugins_dir or default_plugins_dir())
    try:
        manifests = list_plugins(root)
    except (OSError, ValueError):
        return payload
    names = payload.setdefault("discovery_names", {})
    if not isinstance(names, dict):
        names = payload["discovery_names"] = {}
    for manifest in manifests:
        if not manifest.get("valid") or not manifest.get("enabled"):
            continue
        capabilities = manifest.get("capabilities")
        if not isinstance(capabilities, dict) or not isinstance(capabilities.get("discovery"), dict):
            continue
        plugin_id = str(manifest.get("id") or "")
        if not plugin_id:
            continue
        result = fetch_plugin_discovery(plugin_id, games, limit=limit, plugins_dir=root)
        if result.get("error"):
            continue
        for key, indices in (result.get("lists") or {}).items():
            if isinstance(key, str) and isinstance(indices, list):
                payload[key] = indices
        for key, name in (result.get("names") or {}).items():
            if isinstance(key, str) and isinstance(name, str):
                names[key] = name
    return payload


def fetch_plugin_discovery(plugin_id, games, limit=MAX_IDS, *, plugins_dir=None, timeout=None):
    """Return a plugin's discovery lists resolved to library indices.

    Shape: ``{"lists": {key: [indices]}, "names": {key: name},
    "game_ids": {key: [ids]}}`` where ``key`` is
    ``plugin:<plugin_id>:<list_id>``. A hook or result failure returns the
    empty shape (one optional provider never breaks the Discovery Center).
    """
    root = Path(plugins_dir or default_plugins_dir())
    manifest = plugin_manifest(root, plugin_id)
    require_plugin_capability(manifest, "discovery")
    try:
        limit = max(1, min(MAX_IDS, int(limit)))
    except (TypeError, ValueError):
        limit = MAX_IDS

    index = {}
    for position, game in enumerate(games if isinstance(games, list) else []):
        if not isinstance(game, dict):
            continue
        game_id = str(game.get("game_id") or game.get("id") or "").strip()
        if game_id and game_id not in index:
            index[game_id] = position

    try:
        result, error = run_plugin_hook(root, manifest["id"], "discovery", {"limit": limit}, timeout=timeout)
    except Exception as hook_error:
        LOGGER.warning("Plugin %s discovery crashed: %s", manifest["id"], hook_error)
        return _empty(str(hook_error))
    if error:
        LOGGER.warning("Plugin %s discovery failed: %s", manifest["id"], error)
        return _empty(error)
    if not isinstance(result, dict):
        return _empty("Plugin discovery must return a JSON object.")
    rows = result.get("lists")
    if not isinstance(rows, list):
        return _empty("Plugin discovery must return {lists: [...]}.")

    payload = _empty()
    truncated = len(rows) > MAX_LISTS
    for raw in rows[:MAX_LISTS]:
        if not isinstance(raw, dict):
            continue
        list_id = _slug(raw.get("id"))
        name = raw.get("name")
        if not list_id or not isinstance(name, str) or not name.strip():
            continue
        raw_ids = raw.get("game_ids")
        if not isinstance(raw_ids, list):
            continue
        if len(raw_ids) > limit:
            truncated = True
        ids = []
        indices = []
        for value in raw_ids[:limit]:
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                value = str(int(value))
            if not isinstance(value, str):
                continue
            game_id = value.strip()[:MAX_ID_LENGTH]
            if game_id in index and game_id not in ids:
                ids.append(game_id)
                indices.append(index[game_id])
        if not indices:
            continue
        key = f"plugin:{manifest['id']}:{list_id}"[:MAX_ID_LENGTH]
        payload["lists"][key] = indices
        payload["names"][key] = name.strip()[:MAX_NAME_LENGTH]
        payload["game_ids"][key] = ids
    payload["truncated"] = truncated
    return payload
