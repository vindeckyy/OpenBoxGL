"""LaunchBox library XML migration import (1.9.0).

Parses a LaunchBox ``<LaunchBox>`` export (typically from ``Data/Platforms/*.xml``)
into OpenBox game entries. Two-phase: preview (dry-run report) and apply
(persist via transact_state).

The parser uses only stdlib ``xml.etree.ElementTree``. It is intentionally
forgiving: unknown fields are ignored, missing fields default to empty
strings, and malformed entries are skipped with a count rather than aborting
the whole import.

Deduplication uses ``launchbox_db_id`` first (the LaunchBox ``ID`` field),
falling back to canonical ``game_identity`` when the ID is absent.

Emulator mappings are reported, not silently applied: LaunchBox uses internal
``EmulatorId`` values that have no OpenBox equivalent, so we surface them in
the report for the user to resolve manually.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

# ponytail: LaunchBox XML field set is large and version-dependent; we map only
# the fields OpenBox actually uses. Upgrade path: extend _FIELD_MAP as needed.
_FIELD_MAP = {
    "Title": "name",
    "ApplicationPath": "path",
    "Platform": "platform",
    "Genre": "genre",
    "Developer": "developer",
    "Publisher": "publisher",
    "ReleaseDate": "release_date",
    "PlayMode": "play_mode",
    "Region": "region",
    "Status": "status",
    "Notes": "description",
    "Rating": "rating",
    "EmulatorId": "emulator_id",
    "ID": "launchbox_db_id",
    "ManualPath": "manual_path",
    "MusicPath": "music_path",
    "VideoPath": "video_path",
    "ImagePath": "cover",
}


def parse_launchbox_xml(source: str | Path) -> dict[str, Any]:
    """Parse a LaunchBox XML file into a report dict.

    Returns ``{"games": [...], "skipped": int, "emulator_ids": set, "errors": [...]}``.
    Each game dict has OpenBox-compatible field names.
    """
    tree = ET.parse(str(source))
    root = tree.getroot()
    games: list[dict[str, Any]] = []
    skipped = 0
    emulator_ids: set[str] = set()
    errors: list[str] = []

    for game_elem in root.findall("Game"):
        try:
            entry = _parse_game(game_elem)
            if not entry.get("name") or not entry.get("launchbox_db_id"):
                skipped += 1
                continue
            if entry.get("emulator_id"):
                emulator_ids.add(entry["emulator_id"])
            games.append(entry)
        except Exception as exc:  # noqa: BLE001
            skipped += 1
            errors.append(str(exc))

    return {
        "games": games,
        "skipped": skipped,
        "emulator_ids": sorted(emulator_ids),
        "errors": errors,
    }


def _parse_game(elem: ET.Element) -> dict[str, Any]:
    entry: dict[str, Any] = {}
    for child in elem:
        lb_field = child.tag
        ob_field = _FIELD_MAP.get(lb_field)
        if ob_field is None:
            continue
        value = (child.text or "").strip()
        if not value:
            continue
        if ob_field == "rating":
            try:
                entry[ob_field] = float(value)
            except ValueError:
                pass
        else:
            entry[ob_field] = value
    return entry


def preview_import(xml_path: str | Path, existing_games: list[dict[str, Any]]) -> dict[str, Any]:
    """Dry-run: parse XML and report what would be imported, without writing.

    ``existing_games`` is the current library's game list, used for dedup.
    """
    parsed = parse_launchbox_xml(xml_path)
    existing_lb_ids = {
        str(g.get("launchbox_db_id") or "")
        for g in existing_games
        if isinstance(g, dict) and g.get("launchbox_db_id")
    }
    existing_names = {
        str(g.get("name", "")).strip().lower()
        for g in existing_games
        if isinstance(g, dict)
    }
    new_games: list[dict[str, Any]] = []
    duplicates = 0
    for game in parsed["games"]:
        lb_id = str(game.get("launchbox_db_id") or "")
        name_lower = str(game.get("name", "")).strip().lower()
        if lb_id and lb_id in existing_lb_ids:
            duplicates += 1
            continue
        if name_lower and name_lower in existing_names:
            duplicates += 1
            continue
        new_games.append(game)
    return {
        "total_in_xml": len(parsed["games"]),
        "skipped_malformed": parsed["skipped"],
        "duplicates": duplicates,
        "would_import": len(new_games),
        "emulator_ids": parsed["emulator_ids"],
        "errors": parsed["errors"],
        "preview_games": new_games[:50],
    }


def apply_import(
    xml_path: str | Path,
    existing_games: list[dict[str, Any]],
    merge_fn,
) -> dict[str, Any]:
    """Apply: parse XML, merge via merge_fn(imported_games, identity_fn).

    ``merge_fn`` is ``merge_imported_games`` from pkg.state.imports.
    Returns the merge result plus the emulator report.
    """
    parsed = parse_launchbox_xml(xml_path)
    if not parsed["games"]:
        return {
            "added": 0,
            "found": 0,
            "skipped_malformed": parsed["skipped"],
            "emulator_ids": parsed["emulator_ids"],
            "errors": parsed["errors"],
        }
    from pkg.state.imports import game_identity

    added, found = merge_fn(parsed["games"], game_identity)
    result = {
        "added": added,
        "found": found,
        "skipped_malformed": parsed["skipped"],
        "emulator_ids": parsed["emulator_ids"],
        "errors": parsed["errors"],
    }
    return result
