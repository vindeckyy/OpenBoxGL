"""Pure, bounded ES-DE ``gamelist.xml`` import planning.

ES-DE exports are deliberately treated as a source snapshot.  Parsing and
planning are side-effect free; a handler can show the plan and apply it inside
the normal OpenBox state transaction after the source and library digests are
rechecked.
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


MAX_XML_BYTES = 32 * 1024 * 1024


class ESDEImportError(ValueError):
    """An ES-DE source cannot be safely interpreted."""


class StaleESDEPlan(ValueError):
    """The source or library changed after a preview was produced."""


def _tag(value: Any) -> str:
    return str(value or "").rsplit("}", 1)[-1].casefold()


def _source_bytes(source: str | bytes | Path, max_bytes: int) -> tuple[bytes, str, Path | None]:
    if max_bytes <= 0:
        raise ValueError("max_xml_bytes must be positive.")
    if isinstance(source, (bytes, bytearray)):
        data, label, source_path = bytes(source), "<memory>", None
    elif isinstance(source, Path) or (isinstance(source, str) and not source.lstrip().startswith("<")):
        source_path = Path(source).expanduser()
        label = str(source_path)
        try:
            size = source_path.stat().st_size
            if size > max_bytes:
                raise ESDEImportError(f"ES-DE XML is too large ({size} bytes; limit is {max_bytes} bytes).")
            data = source_path.read_bytes()
        except ESDEImportError:
            raise
        except OSError as error:
            raise ESDEImportError(f"Could not read ES-DE XML {source_path}: {error}") from error
    else:
        data, label, source_path = str(source).encode("utf-8"), "<memory>", None
    if len(data) > max_bytes:
        raise ESDEImportError(f"ES-DE XML is too large ({len(data)} bytes; limit is {max_bytes} bytes).")
    return data, label, source_path


def _text(parent: ET.Element, name: str) -> str:
    for child in parent:
        if _tag(child.tag) == name.casefold():
            return (child.text or "").strip()
    return ""


def _year(value: str) -> str:
    text = str(value or "")
    compact = re.sub(r"[^0-9]", "", text)
    if len(compact) >= 8:
        candidate = int(compact[:4])
        return str(candidate) if 1000 <= candidate <= 9999 else ""
    match = re.search(r"(?<!\d)(\d{4})(?!\d)", text)
    if not match:
        return ""
    candidate = int(match.group(1))
    return str(candidate) if 1000 <= candidate <= 9999 else ""


def _bool(value: str) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _source_path(value: str, parent: Path | None) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return ""
    if raw.startswith("./") and parent is not None:
        return str((parent / raw[2:]).resolve())
    return os.path.expanduser(raw)


def parse_gamelist(
    source: str | bytes | Path, *, max_xml_bytes: int = MAX_XML_BYTES
) -> dict[str, Any]:
    """Parse one ES-DE gamelist into normalized game rows."""
    data, label, source_path = _source_bytes(source, max_xml_bytes)
    digest = hashlib.sha256(data).hexdigest()
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise ESDEImportError(f"Could not parse ES-DE XML {label}: {error}") from error
    if _tag(root.tag) not in {"gamelist", "game_list"}:
        raise ESDEImportError(f"Could not parse ES-DE XML {label}: root element must be gameList.")

    parent = source_path.parent if source_path is not None else None
    games: list[dict[str, Any]] = []
    errors: list[str] = []
    skipped = 0
    for row_number, element in enumerate(root, 1):
        if _tag(element.tag) != "game":
            continue
        name = _text(element, "name")
        path = _source_path(_text(element, "path"), parent)
        if not name or not path:
            skipped += 1
            errors.append(f"Game row {row_number}: name and path are required")
            continue
        entry: dict[str, Any] = {
            "name": name,
            "path": path,
            "platform": _text(element, "system"),
            "genre": _text(element, "genre"),
            "developer": _text(element, "developer"),
            "publisher": _text(element, "publisher"),
            "description": _text(element, "desc") or _text(element, "description"),
            "year": _year(_text(element, "releasedate")),
            "region": _text(element, "region"),
            "max_players": _text(element, "players"),
            "favorite": _bool(_text(element, "favorite")),
            "hidden": _bool(_text(element, "hidden")),
            "esde_source_id": _text(element, "id"),
            "source": "ES-DE",
        }
        media = {
            "image": "cover", "thumbnail": "cover", "marquee": "clear_logo",
            "video": "video_snap", "manual": "manual", "manualpath": "manual",
            "music": "music",
        }
        for child in element:
            child_name = _tag(child.tag)
            field = media.get(child_name)
            value = _source_path((child.text or "").strip(), parent)
            if field and value:
                if field == "cover" and entry.get(field):
                    continue
                entry[field] = value
        try:
            rating = float(_text(element, "rating") or 0)
        except ValueError:
            rating = 0.0
        if rating:
            entry["rating"] = rating * 5 if rating <= 1 else rating
        source_id = str(entry.get("esde_source_id") or "").strip()
        identity = f"esde:{source_id}" if source_id else f"esde:path:{path}"
        entry["source_identity"] = identity
        entry["source_identities"] = [identity]
        games.append({key: value for key, value in entry.items() if value not in ("", [], False, 0.0)})
    return {
        "games": games,
        "skipped": skipped,
        "errors": errors,
        "source_digest": digest,
        "source_bytes": len(data),
        "source_label": label,
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _digest(value: Any) -> str:
    encoded = json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _games_and_settings(value: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if isinstance(value, dict):
        rows, settings = value.get("games", []), value.get("settings", {})
    else:
        rows, settings = value or [], {}
    if not isinstance(rows, list):
        raise ValueError("existing_games must contain a games list.")
    return [row for row in rows if isinstance(row, dict)], settings if isinstance(settings, dict) else {}


def _path_key(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return ""
    normalized = os.path.normpath(raw).replace("\\", "/")
    return normalized.casefold() if re.match(r"^(?:[A-Za-z]:/|//)", normalized) else normalized


def _map_path(value: Any, options: dict[str, Any]) -> str:
    candidate = str(value or "").strip()
    mappings = options.get("path_mappings", [])
    if isinstance(mappings, dict):
        mappings = [{"source": source, "destination": destination} for source, destination in mappings.items()]
    if not isinstance(mappings, list):
        mappings = []
    candidate_key = _path_key(candidate)
    for mapping in sorted((item for item in mappings if isinstance(item, dict)), key=lambda item: len(str(item.get("source") or "")), reverse=True):
        source = str(mapping.get("source") or "").strip().replace("\\", "/").rstrip("/")
        destination = os.path.expanduser(str(mapping.get("destination") or "").strip()).replace("\\", "/").rstrip("/")
        if not source or not destination:
            continue
        source_key = _path_key(source)
        if candidate_key == source_key:
            return destination
        if candidate_key.startswith(source_key + "/"):
            return f"{destination}/{candidate[len(source):].lstrip('/')}"
    return candidate


def _normalized_game(row: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    game = copy.deepcopy(row)
    for field in ("path", "cover", "clear_logo", "video_snap", "manual", "music"):
        if field in game:
            game[field] = _map_path(game[field], options)
    game["source"] = "ES-DE"
    identity = str(game.get("esde_source_id") or "").strip()
    game["source_identity"] = f"esde:{identity}" if identity else f"esde:path:{game.get('path', '')}"
    game["source_identities"] = [game["source_identity"]]
    return game


def _overwrite(options: dict[str, Any]) -> set[str]:
    value = options.get("overwrite_fields", [])
    if value is True:
        return {"name", "path", "platform", "genre", "year", "developer", "publisher", "description", "cover", "clear_logo", "video_snap", "manual", "music", "rating", "favorite", "hidden"}
    if isinstance(value, str):
        return {value}
    return {str(item) for item in value} if isinstance(value, (list, tuple, set)) else set()


def build_import_plan(
    source: dict[str, Any] | str | bytes | Path,
    existing_games: Any,
    *,
    options: dict[str, Any] | None = None,
    max_xml_bytes: int = MAX_XML_BYTES,
) -> dict[str, Any]:
    """Build a deterministic ES-DE preview plan without mutating state."""
    parsed = parse_gamelist(source, max_xml_bytes=max_xml_bytes) if not isinstance(source, dict) else dict(source)
    parsed.setdefault("games", [])
    parsed.setdefault("source_digest", _digest(parsed["games"]))
    parsed.setdefault("source_bytes", 0)
    parsed.setdefault("source_label", "<parsed>")
    parsed.setdefault("skipped", 0)
    parsed.setdefault("errors", [])
    games, settings = _games_and_settings(existing_games)
    opts = dict(options or {})
    existing_by_identity = {
        str(game.get("source_identity")): game
        for game in games if str(game.get("source_identity") or "").startswith("esde:")
    }
    existing_by_path = {_path_key(game.get("path")): game for game in games if _path_key(game.get("path"))}
    overwrite = _overwrite(opts)
    normalized = []
    operations = []
    for row in parsed["games"]:
        imported = _normalized_game(row, opts)
        normalized.append(imported)
        target = existing_by_identity.get(imported["source_identity"]) or existing_by_path.get(_path_key(imported.get("path")))
        if target is None:
            operations.append({"action": "add", "game": imported, "changes": copy.deepcopy(imported)})
            continue
        changes = {}
        review = []
        for field, value in imported.items():
            if field in {"source", "source_identity", "source_identities", "esde_source_id"} or value in (None, "", [], False):
                continue
            if target.get(field) == value:
                continue
            if target.get(field) not in (None, "", [], False) and field not in overwrite:
                review.append(field)
                continue
            changes[field] = copy.deepcopy(value)
        operations.append({
            "action": "merge", "target_game_id": str(target.get("game_id") or target.get("id") or ""),
            "game": imported, "changes": changes, "review_fields": sorted(set(review)),
        })
    base_digest = _digest(games)
    options_digest = _digest(opts)
    source_games_digest = _digest(parsed["games"])
    token = _digest({
        "source_digest": parsed["source_digest"], "source_games_digest": source_games_digest,
        "base_digest": base_digest, "options_digest": options_digest,
    })
    counts = {
        "total_in_xml": len(parsed["games"]), "skipped_malformed": int(parsed.get("skipped", 0) or 0),
        "added": sum(item["action"] == "add" for item in operations),
        "merged": sum(item["action"] == "merge" for item in operations),
    }
    counts["found"] = counts["total_in_xml"]
    return {
        "schema_version": 1, "source": {"digest": parsed["source_digest"], "bytes": parsed["source_bytes"], "label": parsed["source_label"]},
        "source_digest": parsed["source_digest"], "source_games_digest": source_games_digest,
        "base_digest": base_digest, "options": _jsonable(opts),
        "options_digest": options_digest, "preview_token": token, "counts": counts, "operations": operations,
        "errors": list(parsed.get("errors") or []), "normalized_games": normalized,
        "settings": copy.deepcopy(settings),
    }


def apply_import_plan(
    plan: dict[str, Any], current: Any, *, preview_token: str | None = None,
    source_digest: str | None = None, source: Any = None, options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply a preview only when its source and library digests still match."""
    if not isinstance(plan, dict) or not plan.get("preview_token"):
        raise ValueError("A valid ES-DE import plan is required.")
    games, _settings = _games_and_settings(current)
    canonical = build_import_plan(source, current, options=options) if source is not None else plan
    expected = str(plan["preview_token"])
    if str(canonical.get("preview_token")) != expected:
        raise StaleESDEPlan("ES-DE preview decisions changed; review them again.")
    if preview_token is not None and str(preview_token) != expected:
        raise StaleESDEPlan("ES-DE preview token is stale.")
    if source_digest is not None and str(source_digest) != str(canonical.get("source_digest")):
        raise StaleESDEPlan("ES-DE gamelist changed since the preview.")
    if _digest(games) != str(canonical.get("base_digest")):
        raise StaleESDEPlan("OpenBox library changed since the ES-DE preview.")
    result_games = copy.deepcopy(games)
    by_id = {str(game.get("game_id") or game.get("id") or ""): game for game in result_games}
    added = merged = 0
    for operation in canonical.get("operations", []):
        if operation.get("action") == "add":
            result_games.append(copy.deepcopy(operation.get("game") or {}))
            added += 1
        elif operation.get("action") == "merge":
            target = by_id.get(str(operation.get("target_game_id") or ""))
            if target is None:
                raise StaleESDEPlan("An ES-DE merge target changed since the preview.")
            target.update(copy.deepcopy(operation.get("changes") or {}))
            merged += 1
    result = {"games": result_games, "counts": {"added": added, "merged": merged, "found": int(canonical.get("counts", {}).get("found", 0))}}
    if isinstance(current, dict):
        result_state = copy.deepcopy(current)
        result_state["games"] = result_games
        result_state["import_counts"] = result["counts"]
        return result_state
    return result


parse_esde_gamelist = parse_gamelist
plan_import = build_import_plan
apply_plan = apply_import_plan


__all__ = [
    "ESDEImportError", "StaleESDEPlan", "MAX_XML_BYTES", "parse_gamelist",
    "parse_esde_gamelist", "build_import_plan", "plan_import", "apply_import_plan", "apply_plan",
]
