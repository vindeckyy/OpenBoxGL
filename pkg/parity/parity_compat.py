"""Compatibility report (plan W6-02, W6-03): what an upgrade keeps, what falls back, what is refused.

The report is read-only and deterministic (no timestamps), so two runs over the same data
compare equal. Each section names its items with a disposition code, which the UI turns into
localized text.

Theme checks compare each theme file with the token contract: every ``--token`` declared in a
``:root`` block of ``static/app.css``. A token a theme does not set falls back to the built-in
value and the UI still renders; a token the contract does not know is reported and ignored.
"""

from __future__ import annotations

import json
import os
import re
import zipfile
from pathlib import Path
from typing import Any

from settings_schema import KNOWN_SETTINGS

TOKEN_DECLARATION = re.compile(r"(--[A-Za-z0-9-]+)\s*:")
ROOT_BLOCK = re.compile(r":root\s*\{(.*?)\}", re.S)
MAX_ARCHIVES = 50
REPORT_FORMAT = "openbox-compat-report"
REPORT_VERSION = 1


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _root_tokens(css_text: str) -> set[str]:
    tokens: set[str] = set()
    for block in ROOT_BLOCK.findall(css_text):
        tokens.update(TOKEN_DECLARATION.findall(block))
    return tokens


def contract_tokens(app_css: Path) -> set[str]:
    """The token contract: every custom property declared in ``:root`` of the stylesheet."""
    return _root_tokens(app_css.read_text(encoding="utf-8"))


def theme_report(name: str, css_text: str, contract: set[str]) -> dict[str, Any]:
    declared = _root_tokens(css_text)
    missing = sorted(contract - declared)
    unknown = sorted(declared - contract)
    disposition = "complete" if not missing else "fallbacks"
    return {"name": name, "missing": missing, "unknown": unknown, "disposition": disposition}


def theme_reports(themes_dir: Path, contract: set[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not themes_dir.is_dir():
        return rows
    for path in sorted(themes_dir.glob("*.css")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            rows.append({"name": path.stem, "missing": [], "unknown": [], "disposition": "unreadable"})
            continue
        rows.append(theme_report(path.stem, text, contract))
    return rows


def _archive_row(path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as package:
            manifest = json.loads(package.read("manifest.json"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return {"name": path.name, "format": None, "disposition": "unreadable"}
    declared = manifest.get("format", 1) if isinstance(manifest, dict) else None
    if declared == 1:
        disposition = "both"
    elif declared == 2:
        disposition = "2.0-only"
    else:
        disposition = "refused"
    return {"name": path.name, "format": declared, "disposition": disposition}


def backup_rows(backups_dir: Path) -> list[dict[str, Any]]:
    if not backups_dir.is_dir():
        return []
    return [_archive_row(path) for path in sorted(backups_dir.glob("*.zip"))[:MAX_ARCHIVES]]


def library_section(data_dir: Path, raw: dict[str, Any]) -> dict[str, Any]:
    migration = _read_json(data_dir / "migration-report.json")
    return {
        "schema_version": raw.get("schema_version"),
        "path_mode": raw.get("path_mode", "absolute"),
        "library_root": raw.get("library_root", ""),
        "upgraded_from": migration.get("from_schema"),
        "upgrade_backup": (data_dir / "library.v6.backup.json").is_file(),
    }


def settings_section(settings: dict[str, Any]) -> dict[str, Any]:
    return {"unknown_keys": sorted(key for key in settings if key not in KNOWN_SETTINGS)}


def api_section() -> dict[str, Any]:
    return {
        "public_prefix": "/api/v2",
        "host_channel": "/api/host",
        "v1_bridge": os.environ.get("OPENBOX_ENABLE_V1_API") == "1",
    }


def sync_section(settings: dict[str, Any]) -> dict[str, Any]:
    folder = str(settings.get("cloud_folder") or "").strip()
    return {"configured": bool(folder), "found": bool(folder) and Path(folder).is_dir()}


def plugins_section(directory: Path) -> list[dict[str, Any]]:
    """Each installed plugin with its API version, its disposition, and the sandbox status."""
    try:
        from plugins import list_plugins
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for entry in list_plugins(Path(directory)):
        api_version = entry.get("api_version", 1)
        if not entry.get("valid"):
            disposition = "refused"
        elif api_version in (1, 2):
            disposition = "compatible"
        else:
            disposition = "refused"
        rows.append({
            "id": str(entry.get("id") or ""),
            "name": str(entry.get("name") or ""),
            "api_version": api_version,
            "valid": bool(entry.get("valid")),
            "enabled": bool(entry.get("enabled")),
            "sandbox": str(entry.get("sandbox") or "unknown"),
            "disposition": disposition,
            "error": str(entry.get("error") or ""),
        })
    return rows


def build_report(data_dir: Path, *, root: Path) -> dict[str, Any]:
    """One deterministic report over a data directory; ``root`` is the repository or install root."""
    data_dir = Path(data_dir)
    raw = _read_json(data_dir / "library.json")
    settings = raw.get("settings") if isinstance(raw.get("settings"), dict) else {}
    contract = contract_tokens(Path(root) / "static" / "app.css")
    return {
        "format": REPORT_FORMAT,
        "version": REPORT_VERSION,
        "library": library_section(data_dir, raw),
        "themes": theme_reports(data_dir / "themes", contract),
        "backups": backup_rows(data_dir / "backups"),
        "settings": settings_section(settings),
        "plugins": plugins_section(data_dir / "plugins"),
        "api": api_section(),
        "sync": sync_section(settings),
    }


# The formal scale for 2.0 (plan section 13.1). A library above it is worth
# saying out loud before the migration runs.
READINESS_LIBRARY_LIMIT = 20000


def readiness_checks(data_dir: Path, *, root: Path, report: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The readiness checks, run on 2.0's first launch before the migration.

    The plan asked for these in the 1.19.2 preflight release. The maintainer
    declined that release, so the same checks run here instead (plan 14.1).
    Each entry names one thing to look at before the upgraded library is used.
    """
    from pkg.platform_compat import default_data_dir

    data_dir = Path(data_dir)
    report = report if report is not None else build_report(data_dir, root=root)
    raw = _read_json(data_dir / "library.json")
    games = raw.get("games") if isinstance(raw.get("games"), list) else []
    items: list[dict[str, Any]] = []
    for theme in report.get("themes", []):
        unknown = theme.get("unknown") or []
        if theme.get("disposition") == "unreadable":
            items.append({
                "id": f"theme:{theme.get('name')}",
                "kind": "theme",
                "label": str(theme.get("name") or ""),
                "detail": "the theme file could not be read",
                "action": "review",
            })
            continue
        if not unknown:
            # A theme that does not declare every token is normal: the app's
            # defaults are the fallback. Only a token the app does not know
            # needs the user's attention.
            continue
        items.append({
            "id": f"theme:{theme.get('name')}",
            "kind": "theme",
            "label": str(theme.get("name") or ""),
            "detail": f"{len(unknown)} token(s) the app does not know: {', '.join(unknown[:3])}",
            "action": "review",
        })
    for plugin in report.get("plugins", []):
        plugin_id = str(plugin.get("id") or "")
        if plugin_id in ("data", "credentials"):
            # The reserved store directories, not plugins.
            continue
        if str(plugin.get("disposition")) == "compatible":
            continue
        items.append({
            "id": f"plugin:{plugin.get('id')}",
            "kind": "plugin",
            "label": str(plugin.get("name") or plugin.get("id") or ""),
            "detail": str(plugin.get("error") or f"api version {plugin.get('api_version')} is not served"),
            "action": "update",
        })
    sync = report.get("sync") or {}
    if sync.get("configured") and not sync.get("found"):
        items.append({
            "id": "sync:folder",
            "kind": "sync",
            "label": "Sync folder",
            "detail": "the configured sync folder is not on disk",
            "action": "check",
        })
    for archive in report.get("backups", []):
        if archive.get("format"):
            continue
        items.append({
            "id": f"backup:{archive.get('name')}",
            "kind": "backup",
            "label": str(archive.get("name") or ""),
            "detail": "the archive carries no format and cannot be restored",
            "action": "recreate",
        })
    if len(games) > READINESS_LIBRARY_LIMIT:
        items.append({
            "id": "library:size",
            "kind": "library",
            "label": "Library size",
            "detail": f"{len(games)} games is above the {READINESS_LIBRARY_LIMIT} game scale",
            "action": "review",
        })
    try:
        default_dir = Path(default_data_dir())
        custom = data_dir.resolve() != default_dir.resolve()
    except OSError:
        custom = False
    if custom:
        items.append({
            "id": "library:data_dir",
            "kind": "library",
            "label": "Data directory",
            "detail": f"the library lives in {data_dir}",
            "action": "review",
        })
    return items
