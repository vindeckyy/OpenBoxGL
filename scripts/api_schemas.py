"""Typed response schemas for the public API (plan W2-08).

Forty schemas for the objects the local API returns. An operation is typed only when its
response is pinned here; an operation without an entry stays untyped in the OpenAPI document,
because a guessed schema is worse than an honest gap.

The validator is a small subset of JSON Schema (type, required, properties, items, enum,
additionalProperties) so the runtime stays dependency-free.
"""

from __future__ import annotations

from typing import Any

SCHEMA_NAMES = (
    "Version", "Problem", "Library", "Game", "Settings", "HealthSnapshot", "IntegrityReport",
    "CompatReport", "ThemeReport", "BackupList", "BackupManifest", "BackupArchive",
    "RestoreReport", "MovePlan", "MoveResult", "PluginList", "PluginEntry", "PluginManifest",
    "PluginCommand", "NotificationList", "Notification", "Queue", "QueueItem", "ProfileList",
    "Profile", "PlaylistList", "Playlist", "JobList", "Job", "EmulatorList", "Emulator",
    "MediaList", "MediaItem", "SearchResult", "Constellation", "SetupSummary", "TrashList",
    "Diagnostic", "CollectionList", "MediaManifest",
)


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required or [])}


def _str() -> dict[str, Any]:
    return {"type": "string"}


def _int() -> dict[str, Any]:
    return {"type": "integer"}


def _num() -> dict[str, Any]:
    return {"type": "number"}


def _bool() -> dict[str, Any]:
    return {"type": "boolean"}


def _arr(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


def _any() -> dict[str, Any]:
    return {}


SCHEMAS: dict[str, dict[str, Any]] = {
    "Version": _obj({
        "version": _str(), "api_version": _int(), "state_schema": _int(),
        "plugin_api_version": _int(), "backup_format": _int(), "sync_format": _int(),
        "platform": {"type": "string", "enum": ["linux", "windows", "macos"]}, "python": _str(),
    }, ["version", "api_version", "state_schema", "plugin_api_version", "backup_format", "sync_format", "platform", "python"]),
    "Problem": {
        "type": "object",
        "required": ["type", "title", "status", "detail", "code", "error", "retryable"],
        "properties": {
            "type": {"type": "string", "format": "uri"},
            "title": {"type": "string"},
            "status": {"type": "integer"},
            "detail": {"type": "string"},
            "code": {"type": "string"},
            "error": {"type": "string", "description": "Duplicates detail for one major cycle."},
            "request_id": {"type": "string"},
            "instance": {"type": "string"},
            "retryable": {"type": "boolean"},
        },
        "additionalProperties": True,
    },
    "Game": _obj({
        "game_id": _str(), "name": _str(), "platform": _str(), "path": _str(), "genre": _str(),
        "year": _int(), "developer": _str(), "rating": _int(), "favorite": _bool(),
        "hidden": _bool(), "installed": _bool(), "progress": _str(), "notes": _str(),
        "playtime_seconds": _int(), "play_count": _int(), "last_played": _str(), "source": _str(),
        "tags": _arr(_str()), "screenshots": _arr(_str()), "cover": _str(), "background": _str(),
        "applications": _arr(_obj({"name": _str(), "path": _str()})),
        "versions": _arr(_obj({"name": _str(), "path": _str()})),
        "documents": _arr(_obj({"name": _str(), "path": _str()})),
    }, ["game_id", "name"]),
    "Library": _obj({
        "games": _arr({"$ref": "#/components/schemas/Game"}),
        "schema_version": _int(), "format": _str(), "library_id": _str(), "library_root": _str(),
        "path_mode": {"type": "string", "enum": ["absolute", "portable"]},
        "settings": {"$ref": "#/components/schemas/Settings"},
        "profiles": _arr({"$ref": "#/components/schemas/Profile"}),
        "playlists": _arr({"$ref": "#/components/schemas/Playlist"}),
        "queue": _arr({"$ref": "#/components/schemas/QueueItem"}),
        "notifications": _arr({"$ref": "#/components/schemas/Notification"}),
        "trash": _arr({"$ref": "#/components/schemas/Game"}),
        "history": _arr(_any()), "party_decks": _arr(_any()),
        "emulator_settings": _obj({}), "achievement_overrides": _obj({}),
        "smart_collections": _arr(_any()),
    }, ["games"]),
    "Settings": _obj({
        "theme": _str(), "path_mode": _str(), "library_root": _str(),
        "media_manifest_enabled": _bool(), "backup_on_close": _bool(), "backup_auto_enabled": _bool(),
        "backup_auto_keep": _int(), "cloud_folder": _str(), "watch_folders": _arr(_str()),
        "sidebar_order": _arr(_str()), "hidden_sidebar_sections": _arr(_str()),
        "grid_columns": _int(), "view_mode": _str(), "currency": _str(),
    }),
    "HealthSnapshot": _obj({
        "score": _num(), "generated_at": _str(), "dimensions": _arr(_any()),
        "issues": _arr(_any()), "games": _int(),
    }),
    "IntegrityReport": _obj({
        "format": _int(), "library_path": _str(), "checked_at": _str(), "schema_version": _int(),
        "library_id": _str(), "path_mode": _str(), "library_root": _str(), "games": _int(),
        "categories": _obj({}), "exit_code": _int(),
    }, ["categories", "exit_code"]),
    "CompatReport": _obj({
        "format": _str(), "version": _int(),
        "library": _obj({}), "themes": _arr({"$ref": "#/components/schemas/ThemeReport"}),
        "backups": _arr(_any()), "settings": _obj({}), "plugins": _arr(_any()),
        "api": _obj({}), "sync": _obj({}),
    }, ["library", "themes", "api"]),
    "ThemeReport": _obj({
        "name": _str(), "missing": _arr(_str()), "unknown": _arr(_str()), "disposition": _str(),
    }, ["name", "missing", "unknown"]),
    "BackupList": _obj({"backups": _arr({"$ref": "#/components/schemas/BackupArchive"})}),
    "BackupArchive": _obj({
        "name": _str(), "path": _str(), "size": _int(), "created": _str(),
        "sha256": _str(), "items": _arr(_str()),
    }),
    "BackupManifest": _obj({
        "format": _int(), "app_version": _str(), "state_schema": _int(), "library_id": _str(),
        "created_by": _str(), "created": _str(), "items": _arr(_str()),
        "files": _obj({}), "encryption": _str(),
    }),
    "RestoreReport": _obj({
        "items": _arr(_str()),
        "files": _arr(_obj({
            "name": _str(), "item": _str(), "size": _int(), "verified": _bool(), "status": _str(),
        })),
    }, ["items", "files"]),
    "MovePlan": _obj({
        "format": _str(), "plan_id": _str(), "old_root": _str(), "new_root": _str(),
        "counts": _obj({}), "examples": _obj({}), "rebased": _obj({}),
    }, ["plan_id", "counts"]),
    "MoveResult": _obj({
        "moved": _int(), "counts": _obj({}), "library_root": _str(), "path_mode": _str(),
    }, ["moved"]),
    "PluginList": _obj({"plugins": _arr({"$ref": "#/components/schemas/PluginEntry"})}),
    "PluginEntry": _obj({
        "id": _str(), "name": _str(), "version": _str(), "valid": _bool(), "enabled": _bool(),
        "error": _str(), "sandbox": _str(), "api_version": _int(), "min_openbox": _str(),
        "subscriptions": _arr(_str()), "hooks": _arr(_str()), "commands": _arr(_any()),
        "capabilities": _arr(_any()), "checksum": _str(), "trusted": _bool(),
        "granted_permissions": _arr(_str()),
    }, ["id", "valid"]),
    "PluginManifest": _obj({
        "id": _str(), "name": _str(), "version": _str(), "entry": _str(), "hooks": _arr(_str()),
        "api_version": _int(), "min_openbox": _str(), "subscriptions": _arr(_str()),
        "permissions": _arr(_str()), "commands": _arr({"$ref": "#/components/schemas/PluginCommand"}),
        "capabilities": _arr(_any()), "settings": _obj({}), "messages": _obj({}),
        "description": _str(), "author": _str(), "homepage": _str(),
    }, ["id", "name", "version"]),
    "PluginCommand": _obj({
        "id": _str(), "label": _str(), "description": _str(), "hooks": _arr(_str()),
    }, ["id", "label"]),
    "NotificationList": _obj({"notifications": _arr({"$ref": "#/components/schemas/Notification"})}),
    "Notification": _obj({
        "id": _str(), "level": _str(), "message": _str(), "created": _str(), "game_id": _str(),
        "title": _str(), "actions": _arr(_any()),
    }, ["message"]),
    "Queue": _obj({"queue": _arr({"$ref": "#/components/schemas/QueueItem"})}),
    "QueueItem": _obj({
        "game_id": _str(), "name": _str(), "position": _int(), "added": _str(),
        "platform": _str(),
    }, ["game_id"]),
    "ProfileList": _obj({"profiles": _arr({"$ref": "#/components/schemas/Profile"})}),
    "Profile": _obj({
        "id": _str(), "name": _str(), "created": _str(), "last_used": _str(),
        "settings": _obj({}), "games": _arr(_str()),
    }, ["name"]),
    "PlaylistList": _obj({"playlists": _arr({"$ref": "#/components/schemas/Playlist"})}),
    "Playlist": _obj({
        "name": _str(), "created": _str(), "game_ids": _arr(_str()), "count": _int(),
    }, ["name"]),
    "JobList": _obj({"jobs": _arr({"$ref": "#/components/schemas/Job"})}),
    "Job": _obj({
        "job_id": _str(), "name": _str(), "state": _str(), "progress": _num(),
        "result": _any(), "error": _str(), "created": _str(),
    }, ["job_id", "state"]),
    "EmulatorList": _obj({"emulators": _arr({"$ref": "#/components/schemas/Emulator"})}),
    "Emulator": _obj({
        "id": _str(), "name": _str(), "platform": _str(), "installed": _bool(),
        "version": _str(), "path": _str(), "available": _bool(), "reason": _str(),
    }, ["id", "name"]),
    "MediaList": _obj({"media": _arr({"$ref": "#/components/schemas/MediaItem"})}),
    "MediaItem": _obj({
        "game_id": _str(), "kind": _str(), "path": _str(), "url": _str(), "source": _str(),
        "width": _int(), "height": _int(),
    }),
    "SearchResult": _obj({
        "results": _arr({"$ref": "#/components/schemas/Game"}), "query": _str(),
        "count": _int(), "truncated": _bool(),
    }),
    "Constellation": _obj({
        "nodes": _arr(_any()), "edges": _arr(_any()), "games": _int(),
    }),
    "SetupSummary": _obj({
        "games": _int(), "issues": _arr(_any()), "sections": _obj({}), "ready": _bool(),
    }),
    "TrashList": _obj({"trash": _arr({"$ref": "#/components/schemas/Game"})}),
    "Diagnostic": _obj({
        "version": _str(), "platform": _str(), "python": _str(), "data_dir": _str(),
        "log": _str(), "paths": _obj({}),
    }),
    "CollectionList": _obj({"collections": _arr(_any()), "smart_collections": _arr(_any())}),
    "MediaManifest": _obj({
        "format": {"type": "string", "const": "openbox-media-manifest"},
        "version": _int(),
        "files": _obj({
            "size": _int(), "mtime_ns": _int(), "sha256": _str(), "source": _str(),
        }),
    }, ["format", "files"]),
}

# Operations whose response is pinned by a schema above. Everything else stays untyped.
OPERATIONS: dict[tuple[str, str], tuple[str, str]] = {
    ("GET", "/api/v2/version"): ("Version", "Contract versions of this build."),
    ("GET", "/api/v2/library"): ("Library", "The whole library, with the durable state envelope."),
    ("GET", "/api/v2/settings"): ("Settings", "The stored settings."),
    ("GET", "/api/v2/library/integrity"): ("IntegrityReport", "Read-only integrity report."),
    ("GET", "/api/v2/compat/report"): ("CompatReport", "What this version does with your data."),
    ("POST", "/api/v2/library/move/preview"): ("MovePlan", "The plan a library move would apply."),
    ("POST", "/api/v2/library/move/apply"): ("MoveResult", "The result of an applied library move."),
    ("GET", "/api/v2/backups"): ("BackupList", "The backup archives in the data folder."),
    ("GET", "/api/v2/plugins"): ("PluginList", "The installed plugins."),
}


def resolve(ref: str, schemas: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Resolve a local ``#/components/schemas/Name`` reference."""
    catalog = schemas if schemas is not None else SCHEMAS
    name = ref.rsplit("/", 1)[-1]
    if name not in catalog:
        raise KeyError(f"unknown schema reference: {ref}")
    return catalog[name]


def validate(instance: Any, schema: dict[str, Any], schemas: dict[str, dict[str, Any]] | None = None) -> list[str]:
    """Validate an instance against the supported subset of JSON Schema. Returns the problems."""
    catalog = schemas if schemas is not None else SCHEMAS
    problems: list[str] = []
    if "$ref" in schema:
        try:
            return validate(instance, resolve(schema["$ref"], catalog), catalog)
        except KeyError as error:
            return [str(error)]
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(instance, dict):
            return [f"expected an object, got {type(instance).__name__}"]
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in instance:
                problems.append(f"missing required property {name!r}")
        for name, value in instance.items():
            if name in properties:
                problems += [f"{name}: {issue}" for issue in validate(value, properties[name], catalog)]
    elif expected == "array":
        if not isinstance(instance, list):
            return [f"expected an array, got {type(instance).__name__}"]
        items = schema.get("items")
        if isinstance(items, dict):
            for index, value in enumerate(instance):
                problems += [f"[{index}]: {issue}" for issue in validate(value, items, catalog)]
    elif expected == "string":
        if not isinstance(instance, str):
            problems.append(f"expected a string, got {type(instance).__name__}")
    elif expected == "integer":
        if not isinstance(instance, int) or isinstance(instance, bool):
            problems.append(f"expected an integer, got {type(instance).__name__}")
    elif expected == "number":
        if not isinstance(instance, (int, float)) or isinstance(instance, bool):
            problems.append(f"expected a number, got {type(instance).__name__}")
    elif expected == "boolean":
        if not isinstance(instance, bool):
            problems.append(f"expected a boolean, got {type(instance).__name__}")
    if "enum" in schema and instance not in schema["enum"]:
        problems.append(f"{instance!r} is not one of {schema['enum']}")
    return problems
