"""LaunchBox library XML migration import (1.9.0, hardened 1.10.0 in ADR 0039).

Parses a LaunchBox ``<LaunchBox>`` export (typically from ``Data/Platforms/*.xml``)
into OpenBox game entries. Two-phase: preview (dry-run report) and apply
(persist via transact_state). Resolve (1.10.0) validates emulator mappings
against the emulator-defs registry, remaps Windows paths, and recounts
without mutating.

The parser uses only stdlib ``xml.etree.ElementTree``. Streaming previews use
``iterparse`` so 20k-game exports never materialize a DOM. Unknown fields are
ignored, malformed entries are skipped with a count, and unmapped Windows
paths become ``needs_path`` shelf rows (never synthesized shell commands).
"""

from __future__ import annotations

import hashlib
import json
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from api_errors import BadRequest, PreviewNotFound, PreviewStale

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

PREVIEW_PAGE_SIZE = 5000


def is_windows_path(path: str) -> bool:
    text = str(path or "")
    return len(text) >= 3 and text[1] == ":" and text[2] in ("\\", "/")


def remap_windows_path(path: str, path_remap: dict[str, Any] | None) -> str:
    original = str(path or "")
    if not path_remap:
        return original
    from_prefix = str(path_remap.get("from_prefix") or "")
    to_dir = str(path_remap.get("to_dir") or "")
    if not from_prefix or not to_dir:
        return original
    norm_path = original.replace("\\", "/")
    norm_prefix = from_prefix.replace("\\", "/").rstrip("/")
    if norm_path.lower().startswith(norm_prefix.lower() + "/"):
        remainder = norm_path[len(norm_prefix) + 1 :]
        return to_dir.rstrip("/").replace("\\", "/") + "/" + remainder
    return original


def _find_adapter_entry(want: str, adapters: list[dict] | None, defs_dir=None) -> dict | None:
    target = str(want or "").strip()
    if adapters is not None:
        for item in adapters:
            if item.get("adapter_id") == target:
                return item
        for item in adapters:
            if item.get("emulator_id") == target:
                return item
        return None
    from pkg.parity.parity_emulator_defs import find_adapter

    return find_adapter(target, target)


def validate_emulator_mappings(
    mappings: dict, *, adapters: list[dict] | None = None, defs_dir=None
) -> dict[str, Any]:
    if not isinstance(mappings, dict):
        raise BadRequest("mappings must be an object.")
    resolved: dict[str, str] = {}
    unresolved: list[str] = []
    for lb_id, target in mappings.items():
        found = _find_adapter_entry(target, adapters, defs_dir)
        if found is None:
            unresolved.append(str(lb_id))
            continue
        resolved[str(lb_id)] = str(found.get("adapter_id"))
    return {"resolved": resolved, "unresolved": sorted(unresolved)}


def build_shelf_row(
    game: dict,
    *,
    remapped_path: str = "",
    adapter_id: str | None = None,
    adapter_emulator_id: str | None = None,
) -> dict[str, Any]:
    original = str(game.get("path") or "") if isinstance(game, dict) else ""
    row = dict(game) if isinstance(game, dict) else {}
    row["launch"] = ""
    if remapped_path and not is_windows_path(remapped_path):
        row["path"] = remapped_path
        row["needs_path"] = False
        row.pop("launchbox_path", None)
    else:
        row["path"] = ""
        row["needs_path"] = True
        if original:
            row["launchbox_path"] = original
    if adapter_id:
        row["emulator_adapter_id"] = adapter_id
        if adapter_emulator_id:
            original_emu = str(game.get("emulator_id") or "") if isinstance(game, dict) else ""
            if original_emu and original_emu != adapter_emulator_id:
                row["launchbox_emulator_id"] = original_emu
            row["emulator_id"] = adapter_emulator_id
    return row


def resolve_games(
    games: list[dict],
    *,
    mappings: dict | None = None,
    path_remap: dict | None = None,
    adapters: list[dict] | None = None,
    defs_dir=None,
) -> dict[str, Any]:
    mappings = mappings or {}
    validation = validate_emulator_mappings(mappings, adapters=adapters, defs_dir=defs_dir)
    resolved_map = validation["resolved"]
    rows: list[dict[str, Any]] = []
    remapped = 0
    needs = 0
    resolved_count = 0
    unresolved_emu = 0
    for game in games or []:
        original_path = str(game.get("path") or "") if isinstance(game, dict) else ""
        new_path = remap_windows_path(original_path, path_remap) if path_remap else original_path
        if new_path != original_path:
            remapped += 1
        lb_emu = str(game.get("emulator_id") or "") if isinstance(game, dict) else ""
        adapter_id = resolved_map.get(lb_emu)
        adapter_emu = None
        if adapter_id:
            entry = _find_adapter_entry(adapter_id, adapters, defs_dir) or {}
            adapter_emu = entry.get("emulator_id") or adapter_id
            resolved_count += 1
        elif lb_emu:
            unresolved_emu += 1
        row = build_shelf_row(game, remapped_path=new_path, adapter_id=adapter_id, adapter_emulator_id=adapter_emu)
        if row.get("needs_path"):
            needs += 1
        rows.append(row)
    counts = {
        "total": len(games or []),
        "resolved": resolved_count,
        "unresolved_emulator": unresolved_emu,
        "remapped": remapped,
        "needs_path": needs,
    }
    return {"rows": rows, "counts": counts, "resolved": validation["resolved"], "unresolved": validation["unresolved"]}


def iter_parsed_games(source: str | Path):
    context = ET.iterparse(str(source), events=("end",))
    for _event, elem in context:
        if elem.tag != "Game":
            continue
        entry = _parse_game(elem)
        elem.clear()
        yield entry


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


def _stream_new_games(xml_path: str | Path, existing_games: list[dict[str, Any]]) -> dict[str, Any]:
    existing_lb_ids = {
        str(g.get("launchbox_db_id") or "")
        for g in (existing_games or [])
        if isinstance(g, dict) and g.get("launchbox_db_id")
    }
    existing_names = {
        str(g.get("name", "")).strip().lower() for g in (existing_games or []) if isinstance(g, dict)
    }
    total = 0
    skipped = 0
    duplicates = 0
    emulator_ids: set[str] = set()
    new_games: list[dict[str, Any]] = []
    for entry in iter_parsed_games(xml_path):
        if not entry.get("name") or not entry.get("launchbox_db_id"):
            skipped += 1
            continue
        total += 1
        if entry.get("emulator_id"):
            emulator_ids.add(entry["emulator_id"])
        lb_id = str(entry.get("launchbox_db_id") or "")
        name_lower = str(entry.get("name", "")).strip().lower()
        if lb_id and lb_id in existing_lb_ids:
            duplicates += 1
            continue
        if name_lower and name_lower in existing_names:
            duplicates += 1
            continue
        new_games.append(entry)
    return {
        "total": total,
        "skipped": skipped,
        "duplicates": duplicates,
        "new_games": new_games,
        "emulator_ids": sorted(emulator_ids),
        "errors": [],
    }


def preview_import(
    xml_path: str | Path, existing_games: list[dict[str, Any]], limit=5000, offset=0
) -> dict[str, Any]:
    """Dry-run: stream XML and report what would be imported, without writing.

    ``existing_games`` is the current library's game list, used for dedup.
    ``limit``/``offset`` paginate ``preview_games`` over the would-import set
    (capped at ``PREVIEW_PAGE_SIZE`` so 20k exports stay bounded).
    """
    limit = max(1, min(int(limit or PREVIEW_PAGE_SIZE), PREVIEW_PAGE_SIZE))
    offset = max(0, int(offset or 0))
    streamed = _stream_new_games(xml_path, existing_games)
    new_games = streamed["new_games"]
    page = new_games[offset : offset + limit]
    return {
        "total_in_xml": streamed["total"],
        "skipped_malformed": streamed["skipped"],
        "duplicates": streamed["duplicates"],
        "would_import": len(new_games),
        "emulator_ids": streamed["emulator_ids"],
        "errors": streamed["errors"],
        "preview_games": page,
        "preview_limit": limit,
        "preview_offset": offset,
    }


def _data_path(data_dir=None) -> Path:
    if data_dir is not None:
        candidate = Path(data_dir)
        return candidate / "library.json" if candidate.is_dir() else candidate
    from openbox import DATA

    return DATA


def launchbox_previews_dir(data_dir=None) -> Path:
    return _data_path(data_dir).parent / "launchbox_previews"


def launchbox_preview_path(preview_id: str, data_dir=None) -> Path:
    return launchbox_previews_dir(data_dir) / f"{preview_id}.json"


def library_signature_for_games(existing_games: list[dict] | None) -> str:
    items = sorted(
        (str(g.get("launchbox_db_id") or ""), str(g.get("name", "")).strip().lower())
        for g in (existing_games or [])
        if isinstance(g, dict)
    )
    payload = json.dumps(items, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def xml_fingerprint(xml_path: str | Path) -> str:
    target = Path(xml_path)
    stat = target.stat()
    return f"{target.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"


def create_launchbox_preview(
    xml_path: str | Path, existing_games: list[dict[str, Any]], *, data_dir=None, limit=5000, offset=0
) -> dict[str, Any]:
    report = preview_import(xml_path, existing_games, limit=limit, offset=offset)
    preview_id = uuid.uuid4().hex
    preview = {
        "preview_id": preview_id,
        "revision": 1,
        "xml_path": str(xml_path),
        "xml_fingerprint": xml_fingerprint(xml_path),
        "library_signature": library_signature_for_games(existing_games),
        "total_in_xml": report["total_in_xml"],
        "skipped_malformed": report["skipped_malformed"],
        "duplicates": report["duplicates"],
        "would_import": report["would_import"],
        "emulator_ids": report["emulator_ids"],
        "errors": report["errors"],
        "preview_games": report["preview_games"],
        "preview_limit": report["preview_limit"],
        "preview_offset": report["preview_offset"],
    }
    path = launchbox_preview_path(preview_id, data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(preview, indent=2, sort_keys=True), encoding="utf-8")
    return preview


def load_launchbox_preview(preview_id: str, *, data_dir=None) -> dict[str, Any]:
    path = launchbox_preview_path(str(preview_id or "").strip(), data_dir)
    if not path.is_file():
        raise PreviewNotFound("LaunchBox preview not found.")
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_launchbox_preview(
    preview_id: str,
    mappings: dict | None = None,
    path_remap: dict | None = None,
    existing_games: list[dict] | None = None,
    *,
    data_dir=None,
    adapters: list[dict] | None = None,
    defs_dir=None,
) -> dict[str, Any]:
    mappings = mappings or {}
    existing_games = existing_games or []
    if not isinstance(mappings, dict):
        raise BadRequest("mappings must be an object.")
    if path_remap is not None and not isinstance(path_remap, dict):
        raise BadRequest("path_remap must be an object.")
    preview = load_launchbox_preview(preview_id, data_dir=data_dir)
    if library_signature_for_games(existing_games) != preview.get("library_signature"):
        raise PreviewStale("Library changed since preview was created.")
    if xml_fingerprint(preview.get("xml_path") or "") != preview.get("xml_fingerprint"):
        raise PreviewStale("Source XML changed since preview was created.")
    validation = validate_emulator_mappings(mappings, adapters=adapters, defs_dir=defs_dir)
    streamed = _stream_new_games(preview.get("xml_path") or "", existing_games)
    resolved = resolve_games(
        streamed["new_games"], mappings=mappings, path_remap=path_remap, adapters=adapters, defs_dir=defs_dir
    )
    counts = dict(resolved["counts"])
    counts["duplicates"] = streamed["duplicates"]
    counts["would_import"] = len(streamed["new_games"])
    counts["total_in_xml"] = streamed["total"]
    return {
        "preview_id": preview.get("preview_id"),
        "revision": int(preview.get("revision") or 1),
        "total_in_xml": streamed["total"],
        "would_import": len(streamed["new_games"]),
        "duplicates": streamed["duplicates"],
        "resolved": validation["resolved"],
        "unresolved": validation["unresolved"],
        "remapped": resolved["counts"]["remapped"],
        "needs_path": resolved["counts"]["needs_path"],
        "counts": counts,
        "rows": resolved["rows"][:PREVIEW_PAGE_SIZE],
        "preview_games": resolved["rows"][:PREVIEW_PAGE_SIZE],
        "emulator_ids": streamed["emulator_ids"],
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
