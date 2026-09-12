"""Pure LaunchBox XML migration policy.

The parser and planner do not read or write OpenBox state.  A handler can
parse a source, build a plan against a state snapshot, show it, then apply it
inside its own transaction after checking the plan token.  The older
``preview_import`` and ``apply_import`` functions remain compatibility
wrappers for the existing v2 routes.

LaunchBox XML ``ID`` is a source-record identity and is stored as
``launchbox_source_id``.  ``launchbox_db_id`` is reserved for numeric
``DatabaseID`` metadata.  An old record containing only ``launchbox_db_id``
is never treated as a match for an XML ``ID``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

MAX_XML_BYTES = 64 * 1024 * 1024


class LaunchBoxImportError(ValueError):
    """A source cannot be safely interpreted as a LaunchBox export."""


class StaleImportPlan(ValueError):
    """The source/options/library snapshot used for a plan has changed."""


_FIELD_MAP = {
    "Title": "name", "ApplicationPath": "path", "Platform": "platform",
    "Genre": "genre", "Developer": "developer", "Publisher": "publisher",
    "PlayMode": "play_mode", "Region": "region", "Status": "status",
    "Notes": "description", "Rating": "rating", "ManualPath": "manual",
    "MusicPath": "music", "VideoPath": "video_snap", "ImagePath": "cover",
    "BoxFrontImagePath": "cover", "ScreenshotPath": "screenshots",
    "ScreenshotsPath": "screenshots",
}
_MEDIA_FIELDS = {
    "cover", "background", "clear_logo", "fanart", "banner", "icon", "manual",
    "music", "video", "video_snap", "video_theme", "video_trailer",
    "video_recording", "title_screen", "cart_front", "cart_back", "disc",
    "advertisement", "box_back", "box_spine", "box_3d", "screenshots",
}
_USER_FIELDS = {
    "game_id", "favorite", "hidden", "broken", "portable", "installed",
    "progress", "play_count", "playtime_seconds", "last_played", "rating",
    "notes", "description", "tags", "playlists", "added_at", "launch",
    "emulator_id", "emulator_adapter_id", "emulator_profile_id",
}
_PATH_FIELDS = {"path", *_MEDIA_FIELDS}


def _local_tag(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _source_bytes(source: str | bytes | Path, max_bytes: int) -> tuple[bytes, str]:
    if max_bytes <= 0:
        raise ValueError("max_xml_bytes must be positive.")
    if isinstance(source, (bytes, bytearray)):
        data, label = bytes(source), "<memory>"
    elif isinstance(source, Path) or (isinstance(source, str) and not source.lstrip().startswith("<")):
        path = Path(source).expanduser()
        label = str(path)
        try:
            size = path.stat().st_size
            if size > max_bytes:
                raise LaunchBoxImportError(
                    f"LaunchBox XML is too large ({size} bytes; limit is {max_bytes} bytes)."
                )
            data = path.read_bytes()
        except LaunchBoxImportError:
            raise
        except OSError as exc:
            raise LaunchBoxImportError(f"Could not read LaunchBox XML {path}: {exc}") from exc
    else:
        data, label = str(source).encode("utf-8"), "<memory>"
    if len(data) > max_bytes:
        raise LaunchBoxImportError(
            f"LaunchBox XML is too large ({len(data)} bytes; limit is {max_bytes} bytes)."
        )
    return data, label


def _year(value: str) -> str:
    match = re.search(r"(?<!\d)(\d{4})(?!\d)", str(value or ""))
    if not match:
        return ""
    candidate = int(match.group(1))
    return str(candidate) if 1000 <= candidate <= 9999 else ""


def _numeric_database_id(value: str) -> str:
    value = str(value or "").strip()
    return value if re.fullmatch(r"\d+", value) else ""


def parse_launchbox_xml(
    source: str | bytes | Path, *, max_xml_bytes: int = MAX_XML_BYTES
) -> dict[str, Any]:
    """Parse a bounded XML source into normalized game rows.

    ``ID`` becomes ``launchbox_source_id``.  Only numeric ``DatabaseID`` is
    exposed as ``launchbox_db_id``.  Malformed XML raises before any mutation.
    """
    data, label = _source_bytes(source, max_xml_bytes)
    digest = hashlib.sha256(data).hexdigest()
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise LaunchBoxImportError(f"Could not parse LaunchBox XML {label}: {exc}") from exc
    if _local_tag(root.tag).casefold() != "launchbox":
        raise LaunchBoxImportError(
            f"Could not parse LaunchBox XML {label}: root element must be LaunchBox."
        )
    games: list[dict[str, Any]] = []
    skipped = 0
    emulator_ids: set[str] = set()
    unsupported_fields: set[str] = set()
    errors: list[str] = []
    for row_number, game_elem in enumerate(root.iter(), 1):
        if _local_tag(game_elem.tag).casefold() != "game":
            continue
        try:
            entry = _parse_game(game_elem)
            if not entry.get("name") or not entry.get("launchbox_source_id"):
                skipped += 1
                continue
            if entry.get("launchbox_emulator_id"):
                emulator_ids.add(entry["launchbox_emulator_id"])
            unsupported_fields.update(entry.pop("_unsupported_fields", []))
            games.append(entry)
        except (TypeError, ValueError, KeyError) as exc:
            skipped += 1
            errors.append(f"Game row {row_number}: {exc}")
    return {
        "games": games, "skipped": skipped, "emulator_ids": sorted(emulator_ids),
        "errors": errors, "unsupported_fields": sorted(unsupported_fields),
        "source_digest": digest, "source_bytes": len(data),
        "source_label": label,
    }


def _parse_game(elem: ET.Element) -> dict[str, Any]:
    entry: dict[str, Any] = {}
    screenshots: list[str] = []
    for child in elem:
        lb_field = _local_tag(child.tag)
        value = (child.text or "").strip()
        if not value:
            continue
        if lb_field == "ID":
            entry["launchbox_source_id"] = value
            continue
        if lb_field == "DatabaseID":
            database_id = _numeric_database_id(value)
            if database_id:
                entry["launchbox_db_id"] = database_id
            continue
        if lb_field in ("ReleaseDate", "Year"):
            parsed_year = _year(value)
            if parsed_year:
                entry["year"] = parsed_year
            continue
        if lb_field == "EmulatorId":
            entry["launchbox_emulator_id"] = value
            continue
        ob_field = _FIELD_MAP.get(lb_field)
        if ob_field is None:
            entry.setdefault("_unsupported_fields", []).append(lb_field)
            continue
        if ob_field == "screenshots":
            screenshots.append(value)
            continue
        if ob_field == "rating":
            try:
                entry[ob_field] = float(value)
            except ValueError:
                continue
        else:
            entry[ob_field] = value
    if screenshots:
        entry["screenshots"] = screenshots
    return entry


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, set):
        return sorted(_jsonable(item) for item in value)
    return value


def _digest(value: Any) -> str:
    encoded = json.dumps(
        _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _games_and_settings(existing_games: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if isinstance(existing_games, dict):
        rows, settings = existing_games.get("games", []), existing_games.get("settings", {})
    else:
        rows, settings = existing_games or [], {}
    if not isinstance(rows, list):
        raise ValueError("existing_games must be a list or state object with a games list.")
    return [row for row in rows if isinstance(row, dict)], settings if isinstance(settings, dict) else {}


def _path_key(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return ""
    windows = bool(re.match(r"^[A-Za-z]:/", raw) or raw.startswith("//"))
    normalized = os.path.normpath(raw).replace("\\", "/")
    return normalized.casefold() if windows else normalized


def _mapping_entries(options: dict[str, Any]) -> list[tuple[str, str]]:
    raw = None
    for key in ("path_mappings", "path_mapping", "path_map", "path_root_mappings", "root_map", "path_root_map", "windows_root_map"):
        if options.get(key) is not None:
            raw = options[key]
            break
    entries: list[tuple[str, str]] = []
    if isinstance(raw, dict):
        for source, destination in raw.items():
            if str(source).strip() and str(destination).strip():
                entries.append((str(source).strip(), str(destination).strip()))
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            source = item.get("source", item.get("source_root", item.get("windows_root", item.get("from", ""))))
            destination = item.get("destination", item.get("destination_root", item.get("local_root", item.get("to", ""))))
            if str(source).strip() and str(destination).strip():
                entries.append((str(source).strip(), str(destination).strip()))
    if options.get("source_root") is not None and options.get("destination_root") is not None:
        entries.append((str(options["source_root"]), str(options["destination_root"])))
    if options.get("relative_root"):
        entries.append((".", str(options["relative_root"])))
    return entries


def _map_path(value: Any, options: dict[str, Any]) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    candidate = raw.replace("\\", "/")
    candidate_key = _path_key(candidate)
    for source, destination in sorted(_mapping_entries(options), key=lambda pair: len(pair[0]), reverse=True):
        source_norm = source.replace("\\", "/").rstrip("/") or "."
        source_key = _path_key(source_norm)
        if source_norm == "." and not re.match(r"^(?:[A-Za-z]:/|/|//)", candidate):
            suffix = candidate.lstrip("./")
        elif candidate_key == source_key:
            suffix = ""
        elif candidate_key.startswith(source_key.rstrip("/") + "/"):
            suffix = candidate[len(source_norm):].lstrip("/")
        else:
            continue
        destination_norm = os.path.expanduser(str(destination).strip()).replace("\\", "/").rstrip("/")
        return f"{destination_norm}/{suffix}" if suffix else destination_norm
    return candidate


def _profile_mapping(options: dict[str, Any], source_id: str) -> dict[str, Any]:
    raw = None
    for key in ("emulator_profile_map", "emulator_profiles", "emulator_mappings", "emulator_mapping", "emulator_map", "emulators"):
        if options.get(key) is not None:
            raw = options[key]
            break
    if not isinstance(raw, dict):
        return {}
    value = raw.get(source_id)
    if isinstance(value, str) and value.strip():
        return {"emulator_adapter_id": value.strip()}
    if not isinstance(value, dict):
        return {}
    allowed = {"emulator_id", "emulator_adapter_id", "emulator_profile_id", "profile_id"}
    result = {key: str(item).strip() for key, item in value.items() if key in allowed and str(item).strip()}
    if "profile_id" in result and "emulator_profile_id" not in result:
        result["emulator_profile_id"] = result.pop("profile_id")
    return result


def _normalize_game(parsed: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    game = copy.deepcopy(parsed)
    for field in ("path", *_MEDIA_FIELDS):
        if field in game and field != "screenshots":
            game[field] = _map_path(game[field], options)
    if isinstance(game.get("screenshots"), list):
        game["screenshots"] = [_map_path(path, options) for path in game["screenshots"] if str(path).strip()]
    source_id = str(game.get("launchbox_source_id") or "").strip()
    game["launchbox_source_id"] = source_id
    game["source_identity"] = f"launchbox:{source_id}"
    game["source_identities"] = [f"launchbox:{source_id}"]
    profile = _profile_mapping(options, str(game.get("launchbox_emulator_id") or ""))
    if profile:
        game.update(profile)
    return game


def _launchbox_id(game: dict[str, Any]) -> str:
    return str(game.get("launchbox_source_id") or "").strip()


def _source_identity(game: dict[str, Any]) -> str:
    source_id = _launchbox_id(game)
    if source_id:
        return f"launchbox:{source_id}"
    source_identity = str(game.get("source_identity") or "").strip()
    return source_identity if source_identity.startswith("launchbox:") else ""


def _exclusions(options: dict[str, Any], settings: dict[str, Any]) -> list[Any]:
    for key in ("import_exclusions", "exclusions"):
        if isinstance(options.get(key), (list, tuple, set)):
            return list(options[key])
    value = settings.get("import_exclusions", [])
    return value if isinstance(value, list) else []


def _is_excluded(game: dict[str, Any], exclusions: list[Any]) -> bool:
    source_id = _launchbox_id(game)
    for item in exclusions:
        if isinstance(item, str) and item.strip() == source_id:
            return True
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            if str(item[0]).strip().casefold() in {"launchbox", "launchbox_xml", "launchbox_source"} and str(item[1]).strip() == source_id:
                return True
        if isinstance(item, dict):
            source = str(item.get("source", "")).strip().casefold()
            external_id = str(item.get("external_id", item.get("launchbox_source_id", ""))).strip()
            if source in {"launchbox", "launchbox_xml", "launchbox_source"} and external_id == source_id:
                return True
    return False


def _overwrite_fields(options: dict[str, Any]) -> set[str]:
    value = options.get(
        "overwrite_fields",
        options.get("overwrite_user_fields", options.get("replace_fields", options.get("overwrite", []))),
    )
    if value is True:
        return set(_FIELD_MAP.values()) | {"path", "year", "screenshots"}
    if isinstance(value, str):
        return {value}
    return {str(field) for field in value} if isinstance(value, (list, tuple, set)) else set()


def _changes(existing: dict[str, Any], imported: dict[str, Any], options: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    overwrite = _overwrite_fields(options)
    changes: dict[str, Any] = {}
    review: list[str] = []
    for field, value in imported.items():
        if field == "launchbox_emulator_id" or value in (None, "", [], {}):
            continue
        old = existing.get(field)
        if old == value:
            continue
        if field in _USER_FIELDS or field in _PATH_FIELDS:
            if old not in (None, "", [], {}) and field not in overwrite:
                review.append(field)
                continue
        changes[field] = copy.deepcopy(value)
    for field in ("launchbox_source_id", "source_identity", "source_identities"):
        if imported.get(field) and existing.get(field) != imported[field]:
            changes[field] = copy.deepcopy(imported[field])
    return changes, sorted(set(review))


def build_import_plan(
    source: str | bytes | Path | dict[str, Any] | list[dict[str, Any]],
    existing_games: Any,
    *,
    options: dict[str, Any] | None = None,
    max_xml_bytes: int = MAX_XML_BYTES,
    **option_overrides: Any,
) -> dict[str, Any]:
    """Build a deterministic, side-effect-free LaunchBox migration plan."""
    # Handlers may parse bytes outside their state transaction and pass this
    # report into the planner.  Accepting it here keeps parsing and policy
    # pure while avoiding a second read under the transaction lock.
    if isinstance(source, dict) and isinstance(source.get("games"), list):
        parsed = dict(source)
        parsed.setdefault("source_digest", _digest(parsed["games"]))
        parsed.setdefault("source_bytes", 0)
        parsed.setdefault("source_label", "<parsed>")
        parsed.setdefault("skipped", 0)
        parsed.setdefault("emulator_ids", [])
        parsed.setdefault("errors", [])
        parsed.setdefault("unsupported_fields", [])
    elif isinstance(source, list):
        parsed = {
            "games": source, "skipped": 0, "emulator_ids": [], "errors": [],
            "unsupported_fields": [],
            "source_digest": _digest(source), "source_bytes": 0, "source_label": "<parsed>",
        }
    else:
        parsed = parse_launchbox_xml(source, max_xml_bytes=max_xml_bytes)
    games, settings = _games_and_settings(existing_games)
    opts = dict(options or {})
    opts.update(option_overrides)
    normalized_options = _jsonable(opts)
    base_digest = _digest(games)
    source_digest = parsed["source_digest"]
    options_digest = _digest(normalized_options)
    source_by_id: dict[str, dict[str, Any]] = {}
    source_by_identity: dict[str, dict[str, Any]] = {}
    path_by_key: dict[str, dict[str, Any]] = {}
    for game in games:
        source_id = _launchbox_id(game)
        if source_id:
            source_by_id.setdefault(source_id, game)
        identity = _source_identity(game)
        if identity:
            source_by_identity.setdefault(identity, game)
        if not source_id:
            path = _path_key(game.get("path"))
            if path:
                path_by_key.setdefault(path, game)
    exclusions = _exclusions(opts, settings)
    operations: list[dict[str, Any]] = []
    normalized_rows: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    for row in parsed["games"]:
        imported = _normalize_game(row, opts)
        normalized_rows.append(imported)
        source_id = _launchbox_id(imported)
        if _is_excluded(imported, exclusions):
            operations.append({"action": "exclude", "reason": "import_exclusion", "source_id": source_id, "game": imported})
            continue
        if source_id in seen_sources:
            operations.append({"action": "skip", "reason": "duplicate_source", "source_id": source_id, "game": imported})
            continue
        seen_sources.add(source_id)
        target = source_by_id.get(source_id) or source_by_identity.get(_source_identity(imported))
        if target is None and imported.get("path"):
            target = path_by_key.get(_path_key(imported["path"]))
        if target is None:
            operations.append({
                "action": "add", "source_id": source_id, "game": imported,
                "changes": copy.deepcopy(imported),
                "affected_fields": sorted(imported),
            })
            continue
        changes, review = _changes(target, imported, opts)
        target_id = str(target.get("game_id") or target.get("id") or "")
        target_index = next((index for index, candidate in enumerate(games) if candidate is target), -1)
        operations.append({
            "action": "merge", "source_id": source_id, "target_game_id": target_id,
            "target_index": target_index, "game": imported, "changes": changes,
            "review_fields": review, "affected_fields": sorted(changes),
        })
    counts = {
        "total_in_xml": len(parsed["games"]),
        "skipped_malformed": parsed["skipped"],
        "excluded": sum(op["action"] == "exclude" for op in operations),
        "duplicates": sum(op.get("reason") == "duplicate_source" for op in operations),
        "added": sum(op["action"] == "add" for op in operations),
        "merged": sum(op["action"] == "merge" for op in operations),
        "skipped": sum(op["action"] == "skip" for op in operations),
    }
    counts.update({
        # ``found`` is the number of valid, non-excluded source rows.  Duplicate
        # rows remain visible in that count and are separately reported.
        "found": counts["total_in_xml"] - counts["excluded"],
        "would_import": counts["added"], "would_change": counts["merged"],
    })
    token_payload = {"source_digest": source_digest, "options_digest": options_digest, "base_digest": base_digest}
    preview_token = _digest(token_payload)
    return {
        "schema_version": 1,
        "source": {"digest": source_digest, "bytes": parsed["source_bytes"], "label": parsed["source_label"]},
        "source_digest": source_digest, "options": normalized_options,
        "options_digest": options_digest, "base_digest": base_digest,
        "preview_token": preview_token, "counts": counts, "operations": operations,
        "emulator_ids": parsed["emulator_ids"], "errors": parsed["errors"],
        "unsupported_fields": parsed.get("unsupported_fields", []),
        "preview_games": [op["game"] for op in operations if op["action"] in {"add", "merge"}][:50],
        "normalized_games": normalized_rows,
    }


def plan_import(*args, **kwargs):
    """Compatibility spelling for :func:`build_import_plan`."""
    return build_import_plan(*args, **kwargs)


def _find_target(games: list[dict[str, Any]], operation: dict[str, Any]) -> dict[str, Any] | None:
    # ``target_game_id`` is the stable identity and wins over the positional
    # ``target_index`` hint: a shifted library must never silently merge into
    # whatever game happens to sit at the old index.
    target_id = str(operation.get("target_game_id") or "")
    source_id = str(operation.get("source_id") or "")
    for game in games:
        if target_id and str(game.get("game_id") or game.get("id") or "") == target_id:
            return game
        if source_id and _launchbox_id(game) == source_id:
            return game
    # The positional hint applies only when the operation carries no stronger
    # identity — never merge into an index that contradicts them.
    if target_id or source_id:
        return None
    target_index = operation.get("target_index")
    if isinstance(target_index, int) and 0 <= target_index < len(games):
        return games[target_index]
    return None


def apply_import_plan(
    plan: dict[str, Any], current_games: Any, *, preview_token: str | None = None,
    source_digest: str | None = None, source: Any = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply a plan after rebuilding its decisions from the trusted source.

    The browser payload is review evidence only.  When ``source`` is supplied,
    the canonical planner recomputes operations against the transaction's
    current state and the current options, then the submitted token must match
    that plan before any operation is applied.
    """
    if not isinstance(plan, dict) or not plan.get("preview_token"):
        raise ValueError("A valid LaunchBox import plan is required.")
    expected_token = str(plan["preview_token"])
    if source is not None:
        canonical = build_import_plan(source, current_games, options=options)
        if canonical.get("preview_token") != expected_token:
            raise StaleImportPlan("LaunchBox preview decisions changed; review them again.")
        plan = canonical
    if preview_token is not None and str(preview_token) != expected_token:
        raise StaleImportPlan("LaunchBox import preview token is stale.")
    if source_digest is not None and str(source_digest) != str(plan.get("source_digest") or ""):
        raise StaleImportPlan("LaunchBox XML changed since the preview; review it again.")
    games, _settings = _games_and_settings(current_games)
    if _digest(games) != str(plan.get("base_digest") or ""):
        raise StaleImportPlan("OpenBox library changed since the LaunchBox preview; review it again.")
    result_games = copy.deepcopy(games)
    counts = {"added": 0, "merged": 0, "skipped": 0, "excluded": 0}
    for operation in plan.get("operations", []):
        action = operation.get("action")
        if action == "add":
            result_games.append(copy.deepcopy(operation.get("game") or {}))
            counts["added"] += 1
        elif action == "merge":
            target = _find_target(result_games, operation)
            if target is None:
                raise StaleImportPlan("A LaunchBox merge target changed since the preview.")
            target.update(copy.deepcopy(operation.get("changes") or {}))
            counts["merged"] += 1
        elif action == "exclude":
            counts["excluded"] += 1
        elif action == "skip":
            counts["skipped"] += 1
    counts["found"] = int(plan.get("counts", {}).get("found", 0))
    if isinstance(current_games, dict):
        state = copy.deepcopy(current_games)
        state["games"] = result_games
        state["import_counts"] = counts
        return state
    return {"games": result_games, "counts": counts, "import_counts": counts}


def apply_plan(*args, **kwargs):
    """Compatibility spelling for :func:`apply_import_plan`."""
    return apply_import_plan(*args, **kwargs)


def preview_import(xml_path: str | Path, existing_games: list[dict[str, Any]], options: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    """Legacy dry-run report backed by the pure planner."""
    plan = build_import_plan(xml_path, existing_games, options=options, **kwargs)
    report = dict(plan["counts"])
    report.update({
        "emulator_ids": plan["emulator_ids"], "errors": plan["errors"],
        "unsupported_fields": plan["unsupported_fields"],
        "preview_games": plan["preview_games"], "preview_token": plan["preview_token"],
        "source_digest": plan["source_digest"], "base_digest": plan["base_digest"],
        "options_digest": plan["options_digest"], "plan": plan,
    })
    return report


def apply_import(xml_path: str | Path, existing_games: list[dict[str, Any]], merge_fn, options: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    """Legacy route adapter; persistence remains owned by ``merge_fn``."""
    plan = build_import_plan(xml_path, existing_games, options=options, **kwargs)
    selected = [op["game"] for op in plan["operations"] if op["action"] in {"add", "merge"}]
    if selected:
        merge_fn(selected, lambda game: ("launchbox", _launchbox_id(game)))
    counts = plan["counts"]
    return {
        "added": counts["added"], "merged": counts["merged"], "found": counts["found"],
        "skipped_malformed": counts["skipped_malformed"], "duplicates": counts["duplicates"],
        "excluded": counts["excluded"], "emulator_ids": plan["emulator_ids"],
        "errors": plan["errors"], "unsupported_fields": plan["unsupported_fields"],
        "preview_token": plan["preview_token"],
        "source_digest": plan["source_digest"], "base_digest": plan["base_digest"],
    }
