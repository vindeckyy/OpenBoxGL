"""Plugin-provided storefronts (P10.1).

A plugin that declares the ``storefront`` capability and hook can publish a
catalog of owned/installed games. The host validates the plugin, its network
grant, and every returned entry before anything reaches the library; this
module never writes state. Import callers merge the returned games through
the normal storefront path.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlsplit

from plugins import (
    list_plugins,
    plugin_permission_grants,
    plugin_settings_values,
    plugin_settings_values_public,
    run_plugin_hook,
)

LOGGER = logging.getLogger("openbox.plugins.storefront")

MAX_PAGES = 20
MAX_ENTRIES = 2000
MAX_ID_LENGTH = 160
MAX_TEXT_LENGTH = 300
MAX_URI_LENGTH = 2000
MAX_PLATFORM_LENGTH = 40
MAX_CURSOR_LENGTH = 200
MAX_SETTINGS_KEYS = 128

_STORE_TEXT_FIELDS = ("path", "launch", "install_dir")
_NETWORK_ERROR = "Grant network access to this plugin first."



# ---------------------------------------------------------------------------
# Plugin access
# ---------------------------------------------------------------------------

def default_plugins_dir():
    """Return ``<data>/plugins`` for the running installation."""
    from openbox import DATA

    return Path(DATA).parent / "plugins"


def plugin_manifest(plugins_dir, plugin_id):
    """Return the manifest of an enabled, valid plugin or raise ``ValueError``."""
    wanted = str(plugin_id or "").strip()
    if not wanted:
        raise ValueError("A plugin id is required.")
    root = Path(plugins_dir)
    for manifest in list_plugins(root):
        if str(manifest.get("id")) != wanted:
            continue
        if not manifest.get("valid"):
            raise ValueError(f"Plugin is invalid: {manifest.get('error') or 'unknown error'}.")
        if not manifest.get("enabled"):
            raise ValueError("Plugin is disabled.")
        return manifest
    raise ValueError(f"Unknown plugin: {wanted}.")


def require_plugin_capability(manifest, capability):
    """Return one declared capability object or raise a clear ``ValueError``."""
    capabilities = manifest.get("capabilities")
    if not isinstance(capabilities, dict) or not isinstance(capabilities.get(capability), dict):
        raise ValueError(f"Plugin does not declare the {capability} capability.")
    return capabilities[capability]


def require_network_grant(manifest, plugins_dir=None):
    """Refuse capability work when the plugin may reach the network but was not granted it."""
    declared = set(manifest.get("permissions") or [])
    if "network" not in declared:
        raise ValueError("Plugin does not declare network access.")
    granted = set(manifest.get("granted_permissions") or [])
    if not granted:
        try:
            root = Path(plugins_dir) if plugins_dir is not None else default_plugins_dir()
            granted = set(plugin_permission_grants(root, manifest["id"]))
        except (OSError, ValueError):  # a missing state file means "no grants"
            granted = set()
    if "network" not in granted:
        raise ValueError(_NETWORK_ERROR)


def _clean_settings(settings):
    if settings is None:
        return {}
    if not isinstance(settings, dict):
        raise ValueError("Plugin storefront settings must be an object.")
    return {str(key)[:80]: value for key, value in list(settings.items())[:MAX_SETTINGS_KEYS]}


# ---------------------------------------------------------------------------
# Catalog discovery
# ---------------------------------------------------------------------------

def list_storefront_plugins(plugins_dir):
    """Return enabled plugins that expose a storefront capability.

    Settings values are redacted for the UI; the hook call still receives the
    stored (non-secret for password fields via the plugin payload) values.
    """
    root = Path(plugins_dir)
    items = []
    for manifest in list_plugins(root):
        if not manifest.get("valid") or not manifest.get("enabled"):
            continue
        capabilities = manifest.get("capabilities")
        if not isinstance(capabilities, dict) or not isinstance(capabilities.get("storefront"), dict):
            continue
        capability = capabilities["storefront"]
        try:
            values = plugin_settings_values_public(root, manifest)
        except (OSError, ValueError):
            values = {}
        items.append({
            "plugin_id": manifest["id"],
            "id": capability.get("id", ""),
            "name": capability.get("name", "") or manifest.get("name", ""),
            "platform": capability.get("platform", ""),
            "settings_schema": manifest.get("settings") or {"type": "object", "properties": {}},
            "settings_values": values,
            "permission_granted": "network" in set(manifest.get("granted_permissions") or []),
            "enabled": True,
        })
    return items


def _https_uri(value):
    """Return an HTTPS URI without credentials, or ``""`` when invalid."""
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_URI_LENGTH:
        return ""
    parts = urlsplit(candidate)
    if parts.scheme != "https" or not parts.hostname:
        return ""
    if parts.username or parts.password:
        return ""
    return candidate


def _text(value, limit=MAX_TEXT_LENGTH):
    return str(value).strip()[:limit] if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""


def _installed_flag(raw):
    value = raw.get("installed")
    if isinstance(value, bool):
        return value, True
    if isinstance(value, int) and value in (0, 1):
        return bool(value), True
    return False, False


def _clean_entry(raw, manifest, capability):
    """Validate one plugin catalog entry into the host shape."""
    if not isinstance(raw, dict):
        return None
    their_id = raw.get("id", raw.get("game_id"))
    if isinstance(their_id, (int, float)) and not isinstance(their_id, bool):
        their_id = str(int(their_id))
    if not isinstance(their_id, str) or not their_id.strip():
        return None
    their_id = their_id.strip()[:MAX_ID_LENGTH]
    name = _text(raw.get("name"), 200)
    if not name:
        return None
    installed, installed_known = _installed_flag(raw)
    platform = _text(raw.get("platform"), MAX_PLATFORM_LENGTH) or capability.get("platform", "")
    entry = {
        "id": f"plugin:{manifest['id']}:{their_id}"[:MAX_ID_LENGTH],
        "plugin_id": manifest["id"],
        "plugin_store_id": their_id,
        "name": name,
        "source": capability.get("name") or manifest.get("name") or manifest["id"],
        "platform": platform[:MAX_PLATFORM_LENGTH],
        "installed": installed,
        "installed_known": installed_known,
        "install_uri": _https_uri(raw.get("install_uri")),
        "repair_uri": _https_uri(raw.get("repair_uri")),
        "store_catalog": True,
    }
    for key in _STORE_TEXT_FIELDS:
        value = raw.get(key)
        entry[key] = value.strip()[:MAX_URI_LENGTH] if isinstance(value, str) else ""
    return entry


def _storefront_rows(plugin_id, settings=None, *, action, collection,
                     cursor="", installed_only=False, uninstalled_only=False,
                     timeout=30, plugins_dir=None):
    """Page one storefront action and return raw validated row objects.

    Both catalog and import share the same manifest/grant/pagination rules;
    only the action and the result collection name differ.
    """
    root = Path(plugins_dir or default_plugins_dir())
    manifest = plugin_manifest(root, plugin_id)
    require_plugin_capability(manifest, "storefront")
    require_network_grant(manifest, root)
    if not isinstance(cursor, str):
        raise ValueError("Storefront cursor must be text.")
    values = _clean_settings(settings)
    if settings is None:
        try:
            values = plugin_settings_values(root, manifest)
        except (OSError, ValueError):
            values = {}
    allowed = bool(installed_only), bool(uninstalled_only)
    entries = []
    next_cursor = cursor.strip()[:MAX_CURSOR_LENGTH]
    seen = set()
    pages = 0
    while pages < MAX_PAGES:
        payload = {
            "action": action,
            "cursor": next_cursor,
            "settings": values,
            "installed_only": allowed[0],
            "uninstalled_only": allowed[1],
        }
        result, error = run_plugin_hook(root, manifest["id"], "storefront", payload, timeout=timeout)
        if error:
            raise ValueError(f"Plugin storefront failed: {error}")
        if not isinstance(result, dict):
            raise ValueError("Plugin storefront must return a JSON object.")
        rows = result.get(collection)
        if rows is None:
            # A plugin may use the other canonical collection name; accepting
            # it keeps catalog and import fixtures compatible.
            rows = result.get("entries" if collection == "games" else "games")
        if not isinstance(rows, list):
            raise ValueError(f"Plugin storefront must return {{{collection}: [...]}}.")
        for raw in rows[:MAX_ENTRIES]:
            if isinstance(raw, dict):
                entries.append(raw)
            if len(entries) >= MAX_ENTRIES:
                break
        pages += 1
        if len(entries) >= MAX_ENTRIES:
            break
        following = result.get("next_cursor", "")
        if following in (None, ""):
            break
        if not isinstance(following, str):
            raise ValueError("Storefront next_cursor must be text.")
        following = following.strip()[:MAX_CURSOR_LENGTH]
        if not following:
            break
        if following in seen:
            raise ValueError("Plugin storefront pagination repeated a cursor.")
        seen.add(following)
        next_cursor = following
    return entries


def plugin_storefront_catalog(plugin_id, settings=None, cursor="", installed_only=False,
                              uninstalled_only=False, timeout=30, *, plugins_dir=None):
    """Page through a plugin storefront catalog and return validated entries.

    Pagination stops on an empty/repeated cursor, after 20 pages, or at 2,000
    entries. A hook failure or malformed page raises ``ValueError``; an
    individual malformed entry is skipped.
    """
    root = Path(plugins_dir or default_plugins_dir())
    manifest = plugin_manifest(root, plugin_id)
    capability = require_plugin_capability(manifest, "storefront")
    rows = _storefront_rows(
        plugin_id,
        settings,
        action="catalog",
        collection="entries",
        cursor=cursor,
        installed_only=installed_only,
        uninstalled_only=uninstalled_only,
        timeout=timeout,
        plugins_dir=root,
    )
    entries = []
    for raw in rows:
        entry = _clean_entry(raw, manifest, capability)
        if entry is not None:
            entries.append(entry)
        if len(entries) >= MAX_ENTRIES:
            break
    return entries


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def _clean_import_fields(entry):
    """Validate through the library cleaner, falling back to a local shape."""
    try:
        from handlers.library import _clean_game_fields

        return _clean_game_fields({
            "name": entry["name"],
            "platform": entry["platform"],
            "path": entry.get("path", ""),
            "launch": entry.get("launch", ""),
            "install_dir": entry.get("install_dir", ""),
        })
    except ImportError:  # pragma: no cover - handlers are always importable at runtime
        game = {"name": entry["name"], "platform": entry["platform"] or "PC"}
        for key in ("path", "launch", "install_dir"):
            if entry.get(key):
                game[key] = entry[key]
        return game


def plugin_storefront_import(plugin_id, settings=None, installed_only=False, uninstalled_only=False,
                             *, plugins_dir=None, timeout=30):
    """Return validated games for a plugin storefront.

    Existing games are never deleted here and no state is written; callers
    merge the returned list through ``merge_imported_games``.
    """
    root = Path(plugins_dir or default_plugins_dir())
    manifest = plugin_manifest(root, plugin_id)
    capability = require_plugin_capability(manifest, "storefront")
    rows = _storefront_rows(
        plugin_id,
        settings,
        action="import",
        collection="games",
        installed_only=installed_only,
        uninstalled_only=uninstalled_only,
        timeout=timeout,
        plugins_dir=root,
    )
    games = []
    for raw in rows:
        entry = _clean_entry(raw, manifest, capability)
        if entry is None:
            continue
        if entry.get("installed_known"):
            if installed_only and not entry["installed"]:
                continue
            if uninstalled_only and entry["installed"]:
                continue
        try:
            game = _clean_import_fields(entry)
        except (ValueError, TypeError, AttributeError) as error:
            LOGGER.warning("Plugin %s storefront entry rejected: %s", manifest["id"], error)
            continue
        if not game.get("name"):
            continue
        game.update({
            "game_id": entry["id"],
            "source": entry["source"],
            "collection": entry["source"],
            "store_catalog": True,
            "store_installed": bool(entry["installed"]),
            "owned": True,
            "install_uri": entry.get("install_uri", ""),
            "repair_uri": entry.get("repair_uri", ""),
            "plugin_source": manifest["id"],
            "plugin_source_name": manifest.get("name") or manifest["id"],
            "plugin_store_id": entry["plugin_store_id"],
        })
        if not entry["installed"] and not game.get("notes"):
            game["notes"] = f"Owned via {entry['source']}. Install or open the storefront page to get files locally."
        games.append(game)
    return games
