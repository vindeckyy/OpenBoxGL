"""Setup preview scan, classification, retention, and commit helpers."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from api_errors import (
    BadRequest,
    PreviewEntryLimitExceeded,
    PreviewExpired,
    PreviewLibraryChanged,
    PreviewLimitExceeded,
    PreviewNotFound,
    PreviewStale,
    UnresolvedCandidates,
)
from backend_io import atomic_write_bytes, atomic_write_text
from openbox import EXTENSIONS, PLATFORM_BY_EXTENSION, load_state
from pkg.parity.parity_emulator_defs import _registry, detect_adapter_prefix, find_adapter
from pkg.parity.parity_identity import cross_source_identity, normalize_path_identity, source_identities
from pkg.parity.parity_import import generated_m3u_dir, import_multi_platform
from pkg.parity.parity_import_policy import exclusion_set, filter_imported
from pkg.state.imports import _index_existing_games, _merge_imported_game
from pkg.state.operations import ACTIVE_STATES, get_operation_service
from state_store import JsonStateStore

MAX_PREVIEWS = 10
PREVIEW_TTL_HOURS = 24
MAX_ENTRIES = 100_000
MAX_DECISION_BATCH = 200
DEFAULT_ITEMS_LIMIT = 50
MAX_ITEMS_LIMIT = 200

SETUP_JOB_TYPES = frozenset({"setup.scan", "setup.revalidate", "setup.commit"})


def _data_path(data_dir: Path | None = None) -> Path:
    if data_dir is not None:
        return data_dir / "library.json" if data_dir.is_dir() else data_dir
    from openbox import DATA

    return DATA


def previews_dir(data_dir: Path | None = None) -> Path:
    return _data_path(data_dir).parent / "previews"


def preview_path(preview_id: str, data_dir: Path | None = None) -> Path:
    return previews_dir(data_dir) / f"{preview_id}.json"


def library_signature(data_path: Path | None = None) -> str:
    store = JsonStateStore(data_path or _data_path())
    signature = store.signature()
    if signature is None:
        return "missing"
    return ":".join(str(part) for part in signature)


def file_fingerprint(path: str | Path) -> str:
    target = Path(path)
    try:
        stat = target.stat()
        resolved = str(target.resolve())
    except OSError:
        return f"missing:{path}"
    return f"{resolved}:{stat.st_size}:{stat.st_mtime_ns}"


def folder_fingerprint(path: str | Path) -> str:
    target = Path(path).expanduser()
    if not target.is_dir():
        return f"missing:{path}"
    digest = hashlib.sha256()
    digest.update(str(target.resolve()).encode())
    try:
        stat = target.stat()
        digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
    except OSError:
        pass
    return digest.hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expires_at(hours: int = PREVIEW_TTL_HOURS) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _is_expired(expires_at: str) -> bool:
    if not expires_at:
        return False
    try:
        return datetime.fromisoformat(expires_at) < datetime.now(timezone.utc)
    except ValueError:
        return False


def _candidate_id(source_type: str, source_id: str, identity: str, path: str | None = None) -> str:
    payload = f"{source_type}|{source_id}|{identity}|{path or ''}"
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def _identity_for_game(game: dict) -> str:
    identities = source_identities(game)
    if identities:
        return identities[0]
    if game.get("scummvm_id"):
        return f"scummvm:{game['scummvm_id']}"
    if game.get("path"):
        return f"path:{normalize_path_identity(game['path'])}"
    return f"name:{game.get('name', '')}:{game.get('platform', '')}"


def _adapter_choice(adapter: dict) -> dict:
    return {
        "adapter_id": adapter["adapter_id"],
        "emulator_id": adapter["emulator_id"],
        "label": adapter["label"],
        "recommended": bool(adapter.get("recommended")),
        "flatpak_app_id": adapter.get("flatpak_app_id"),
    }


def emulator_choices_for_platform(platform: str | None) -> list[dict]:
    if not platform:
        return []
    choices = []
    for adapter in _registry()["by_platform"].get(platform, []):
        choices.append(_adapter_choice(adapter))
    return choices


def _flatpak_installed(app_id: str, which=None) -> bool:
    which = which or shutil.which
    flatpak = which("flatpak")
    if not flatpak or not app_id:
        return False
    try:
        completed = subprocess.run(
            [flatpak, "info", app_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return completed.returncode == 0
    except OSError:
        return False


def _adapter_installed(adapter: dict | None, which=None) -> bool:
    if not adapter:
        return False
    which = which or shutil.which
    if detect_adapter_prefix(adapter, which=which):
        native = adapter.get("native_exe") or ""
        if native and which(native):
            return True
        for pattern in adapter.get("executable_patterns", []):
            if which(pattern):
                return True
        flatpak_id = adapter.get("flatpak_app_id") or ""
        if flatpak_id and _flatpak_installed(flatpak_id, which=which):
            return True
    return False


def classify_emulator_readiness(game: dict, *, which=None) -> str:
    which = which or shutil.which
    platform = str(game.get("platform") or "")
    custom_launch = str(game.get("launch") or "").strip()
    adapter_id = str(game.get("emulator_adapter_id") or "").strip()
    emulator_id = str(game.get("emulator_id") or "").strip()
    path = str(game.get("path") or "")
    adapter = find_adapter(adapter_id, emulator_id) if (adapter_id or emulator_id) else None
    if custom_launch and not adapter_id:
        return "unknown"
    if not adapter and not custom_launch:
        suffix = Path(path).suffix.lower() if path else ""
        if suffix in {".exe", ".sh", ".appimage"} and Path(path).is_file():
            return "ready"
        platform_adapters = _registry()["by_platform"].get(platform, [])
        if not platform_adapters:
            return "unknown"
        return "blocked"
    if adapter:
        if _adapter_installed(adapter, which=which):
            return "ready"
        return "warning"
    return "unknown"


def compute_summary(*, state: dict | None = None, which=None) -> dict:
    state = state or load_state()
    games = state.get("games", [])
    readiness = {"ready": 0, "warning": 0, "blocked": 0, "unknown": 0}
    missing_paths = 0
    duplicate_count = 0
    seen_paths: set[str] = set()
    for game in games:
        bucket = classify_emulator_readiness(game, which=which)
        readiness[bucket] = readiness.get(bucket, 0) + 1
        path = str(game.get("path") or "")
        if path and not Path(path).exists():
            missing_paths += 1
        norm = normalize_path_identity(path) if path else ""
        if norm:
            if norm in seen_paths:
                duplicate_count += 1
            seen_paths.add(norm)
    matched = sum(bool(game.get("launchbox_db_id")) for game in games)
    media_gaps = sum(
        1
        for game in games
        if not any(Path(str(game.get(field) or "")).is_file() for field in ("box_front", "screenshot"))
    )
    active_operations = sum(
        1
        for doc in get_operation_service().list_jobs(limit=MAX_PREVIEWS * 10).get("jobs", [])
        if doc.get("state") in ACTIVE_STATES
    )
    library_count = len(games)
    metadata_match_percent = round((matched / library_count) * 100, 2) if library_count else 0.0
    source_coverage = [
        {"source_id": "library", "label": "Library", "game_count": library_count, "coverage_percent": 100.0 if library_count else 0.0},
    ]
    next_action = _next_action(
        library_count=library_count,
        readiness=readiness,
        metadata_match_percent=metadata_match_percent,
        media_gaps=media_gaps,
        active_operations=active_operations,
    )
    return {
        "library_count": library_count,
        "source_coverage": source_coverage,
        "metadata_match_percent": metadata_match_percent,
        "media_gaps": media_gaps,
        "duplicate_count": duplicate_count,
        "missing_paths": missing_paths,
        "emulator_readiness": readiness,
        "active_operations": active_operations,
        "next_action": next_action,
    }


def _next_action(*, library_count, readiness, metadata_match_percent, media_gaps, active_operations):
    if library_count == 0:
        return {"id": "add_sources", "label": "Add game sources", "step": 2}
    if active_operations:
        return {"id": "health", "label": "Review active operations", "step": 1}
    if readiness.get("blocked", 0) or readiness.get("warning", 0):
        return {"id": "fix_launch", "label": "Fix launch readiness", "step": 5}
    if metadata_match_percent < 100:
        return {"id": "review_metadata", "label": "Review metadata matches", "step": 8}
    if media_gaps:
        return {"id": "download_media", "label": "Download missing media", "step": 8}
    return {"id": "none", "label": "Setup complete", "step": 8}


def _preview_job_pinned(preview: dict) -> bool:
    job_id = str(preview.get("job_id") or "").strip()
    if not job_id:
        return False
    operation = get_operation_service().get(job_id)
    if not operation:
        return False
    if operation.get("type") not in SETUP_JOB_TYPES:
        return False
    return operation.get("state") in ACTIVE_STATES


def _list_preview_records(data_dir: Path | None = None) -> list[dict]:
    directory = previews_dir(data_dir)
    if not directory.is_dir():
        return []
    records = []
    for path in directory.glob("*.json"):
        if path.name == "index.json":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        payload["_path"] = path
        records.append(payload)
    return records


def _enforce_preview_cap(*, exclude_preview_id: str | None = None, data_dir: Path | None = None) -> None:
    records = []
    for preview in _list_preview_records(data_dir):
        preview_id = str(preview.get("preview_id") or preview["_path"].stem)
        if preview_id == exclude_preview_id:
            continue
        if _preview_job_pinned(preview):
            continue
        if _is_expired(str(preview.get("expires_at") or "")):
            continue
        records.append(preview)
    if len(records) >= MAX_PREVIEWS:
        raise PreviewLimitExceeded(
            f"At most {MAX_PREVIEWS} active previews are allowed.",
            code="PREVIEW_LIMIT_EXCEEDED",
        )


def load_preview(preview_id: str, *, data_dir: Path | None = None, allow_expired: bool = False) -> dict:
    preview_id = str(preview_id or "").strip()
    if not preview_id:
        raise PreviewNotFound("preview_id is required.")
    path = preview_path(preview_id, data_dir)
    if not path.is_file():
        raise PreviewNotFound("Preview not found.")
    try:
        preview = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise PreviewNotFound("Preview not found.") from None
    if not allow_expired and _is_expired(str(preview.get("expires_at") or "")) and not _preview_job_pinned(preview):
        raise PreviewExpired("Preview has expired.")
    return preview


def save_preview(preview: dict, *, data_dir: Path | None = None) -> None:
    preview_id = str(preview["preview_id"])
    path = preview_path(preview_id, data_dir)
    atomic_write_text(path, json.dumps(preview, ensure_ascii=False))


def _human_preview_message(preview: dict) -> str:
    counts = preview.get("counts") or {}
    scanned = int(preview.get("scanned_entries") or 0)
    total = scanned or sum(int(v or 0) for v in counts.values() if isinstance(v, int))
    # Pending picks: ambiguities and unsupported need explicit user decision
    pending = int(counts.get("ambiguities") or 0) + int(counts.get("unsupported") or 0)
    # Fallback to legacy keys for backwards compat
    if not pending:
        pending = int(counts.get("pending") or counts.get("needs_review") or 0)
    if pending:
        return f"Found {total} games — {pending} need your pick"
    if total:
        return f"Found {total} games — review and commit"
    return "Preview ready — review items and commit"


def preview_document(preview: dict) -> dict:
    doc = {
        "preview_id": preview["preview_id"],
        "revision": int(preview.get("revision") or 1),
        "expires_at": preview.get("expires_at"),
        "state": preview.get("state", "ready"),
        "scanned_entries": int(preview.get("scanned_entries") or 0),
        "counts": dict(preview.get("counts") or {}),
        "library_signature": preview.get("library_signature"),
        "job_id": preview.get("job_id"),
        "sources": list(preview.get("sources") or []),
        "options": dict(preview.get("options") or {}),
        "message": _human_preview_message(preview),
    }
    if preview.get("revalidated"):
        doc["revalidated"] = True
    return doc


def _encode_cursor(preview_id: str, revision: int, offset: int) -> str:
    raw = json.dumps({"preview_id": preview_id, "revision": revision, "offset": offset}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str, *, preview_id: str, revision: int) -> int:
    if not cursor:
        return 0
    padding = "=" * (-len(cursor) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding).decode())
    except (ValueError, json.JSONDecodeError) as error:
        raise PreviewStale("Cursor is invalid or stale.") from error
    if payload.get("preview_id") != preview_id or int(payload.get("revision") or 0) != revision:
        raise PreviewStale("Cursor is bound to a different preview revision.")
    return max(0, int(payload.get("offset") or 0))


def list_preview_items(
    preview_id: str,
    *,
    cursor: str | None = None,
    limit: int = DEFAULT_ITEMS_LIMIT,
    data_dir: Path | None = None,
) -> dict:
    preview = load_preview(preview_id, data_dir=data_dir)
    revision = int(preview.get("revision") or 1)
    limit = max(1, min(int(limit or DEFAULT_ITEMS_LIMIT), MAX_ITEMS_LIMIT))
    offset = _decode_cursor(cursor or "", preview_id=preview_id, revision=revision)
    items = list(preview.get("items") or [])
    page = items[offset : offset + limit]
    next_offset = offset + len(page)
    next_cursor = _encode_cursor(preview_id, revision, next_offset) if next_offset < len(items) else None
    return {
        "preview_id": preview_id,
        "revision": revision,
        "cursor": cursor,
        "next_cursor": next_cursor,
        "items": [_public_item(item) for item in page],
    }


def _public_item(item: dict) -> dict:
    return {
        "candidate_id": item["candidate_id"],
        "group": item["group"],
        "source": dict(item.get("source") or {}),
        "detected_title": item.get("detected_title", ""),
        "detected_platform": item.get("detected_platform"),
        "intended_action": item.get("intended_action", "review"),
        "existing_game_target": item.get("existing_game_target"),
        "warnings": list(item.get("warnings") or []),
        "emulator_choices": list(item.get("emulator_choices") or []),
        "selected_emulator_id": item.get("selected_emulator_id"),
        "selected_adapter_id": item.get("selected_adapter_id"),
        "launch_setup": item.get("launch_setup"),
        "merge_diff": item.get("merge_diff"),
    }


def _scan_folder_source(source: dict) -> tuple[list[dict], str]:
    folder = str(source.get("path") or "").strip()
    if not folder:
        raise BadRequest("Folder path is required for folder sources.")
    games = import_multi_platform(folder, EXTENSIONS, PLATFORM_BY_EXTENSION, write_m3u=False)
    fingerprint = folder_fingerprint(folder)
    label = source.get("id") or Path(folder).name
    wrapped = []
    for game in games:
        game = dict(game)
        game["_source"] = {
            "type": "folder",
            "id": str(source.get("id") or label),
            "label": str(label),
            "path": folder,
        }
        wrapped.append(game)
    return wrapped, fingerprint


def _scan_files_source(source: dict) -> tuple[list[dict], str]:
    paths = source.get("paths") or []
    if not isinstance(paths, list) or not paths:
        raise BadRequest("paths are required for files sources.")
    games = []
    digest = hashlib.sha256()
    for raw in paths:
        path = Path(str(raw)).expanduser()
        digest.update(file_fingerprint(path).encode())
        if not path.is_file():
            continue
        platform = PLATFORM_BY_EXTENSION.get(path.suffix.lower(), "Imported")
        games.append(
            {
                "name": path.stem,
                "platform": platform,
                "genre": "",
                "path": str(path),
                "discs": [],
                "_source": {
                    "type": "files",
                    "id": str(source.get("id") or path.name),
                    "label": path.name,
                    "path": str(path),
                },
            }
        )
    return games, digest.hexdigest()


def _scan_list_source(source: dict, importer, *, source_type: str, label: str) -> tuple[list[dict], str]:
    games = importer()
    digest = hashlib.sha256()
    wrapped = []
    for game in games:
        game = dict(game)
        digest.update(_identity_for_game(game).encode())
        game["_source"] = {
            "type": source_type,
            "id": str(source.get("id") or _identity_for_game(game)),
            "label": label,
            "path": game.get("path"),
        }
        wrapped.append(game)
    return wrapped, digest.hexdigest()


def scan_sources(sources: list[dict], options: dict | None = None) -> tuple[list[dict], dict[str, str]]:
    options = options or {}
    scanned: list[dict] = []
    fingerprints: dict[str, str] = {}
    for index, source in enumerate(sources):
        source_type = str(source.get("type") or "").strip()
        key = f"{index}:{source_type}:{source.get('id') or source.get('path') or ''}"
        if source_type == "folder":
            games, fingerprint = _scan_folder_source(source)
        elif source_type == "files":
            games, fingerprint = _scan_files_source(source)
        elif source_type == "steam":
            from importers import import_steam

            games, fingerprint = _scan_list_source(source, import_steam, source_type="steam", label="Steam")
        elif source_type == "heroic":
            from importers import import_heroic

            games, fingerprint = _scan_list_source(source, import_heroic, source_type="heroic", label="Heroic")
        elif source_type == "lutris":
            from importers import import_lutris

            games, fingerprint = _scan_list_source(source, import_lutris, source_type="lutris", label="Lutris")
        elif source_type == "faugus":
            from parity_faugus import scan_faugus_games

            games, fingerprint = _scan_list_source(source, scan_faugus_games, source_type="faugus", label="Faugus")
        elif source_type == "scummvm":
            from parity_import import import_scummvm

            games, fingerprint = _scan_list_source(source, import_scummvm, source_type="scummvm", label="ScummVM")
        elif source_type == "rpcs3":
            from parity_import import import_rpcs3_hdd

            games, fingerprint = _scan_list_source(source, import_rpcs3_hdd, source_type="rpcs3", label="RPCS3")
        elif source_type == "vita3k":
            from parity_import import import_vita3k

            games, fingerprint = _scan_list_source(source, import_vita3k, source_type="vita3k", label="Vita3K")
        elif source_type == "xbox360":
            from parity_premium import import_xbox360_folder

            folder = str(source.get("path") or "").strip()
            if not folder:
                raise BadRequest("path is required for xbox360 sources.")
            games = import_xbox360_folder(folder)
            fingerprint = folder_fingerprint(folder)
            for game in games:
                game = dict(game)
                game["_source"] = {
                    "type": "xbox360",
                    "id": str(source.get("id") or folder),
                    "label": "Xbox 360",
                    "path": folder,
                }
                scanned.append(game)
            fingerprints[key] = fingerprint
            continue
        elif source_type == "arcade":
            from arcade import import_arcade

            folder = str(source.get("path") or "").strip()
            dat_path = str(source.get("dat_path") or "").strip()
            set_type = str(source.get("set_type") or "split")
            adapter_id = str(source.get("adapter_id") or source.get("emulator_id") or "mame")
            if not folder:
                raise BadRequest("path is required for arcade sources.")
            games = import_arcade(folder, dat_path or None, adapter_id, set_type)
            fingerprint = folder_fingerprint(folder)
            for game in games:
                game = dict(game)
                game["_source"] = {
                    "type": "arcade",
                    "id": str(source.get("id") or folder),
                    "label": "Arcade",
                    "path": folder,
                }
                scanned.append(game)
            fingerprints[key] = fingerprint
            continue
        elif source_type in {"steam", "heroic", "lutris", "gameyfin"}:
            from parity_storefront import catalog_entries_to_games, storefront_catalog

            settings = load_state().get("settings", {})
            catalog = storefront_catalog(source_type, settings=settings)
            include_uninstalled = bool(
                source.get("include_uninstalled") or options.get("include_owned_uninstalled")
            )
            if include_uninstalled:
                games = catalog_entries_to_games(catalog)
            else:
                games = catalog_entries_to_games(catalog, installed_only=True)
            digest = hashlib.sha256(json.dumps(catalog, sort_keys=True, default=str).encode()).hexdigest()
            for game in games:
                game = dict(game)
                game["_source"] = {
                    "type": source_type,
                    "id": str(
                        game.get("steam_app_id")
                        or game.get("heroic_app_id")
                        or game.get("lutris_id")
                        or game.get("gameyfin_id")
                        or game.get("name")
                        or ""
                    ),
                    "label": source_type.title(),
                    "path": game.get("path"),
                }
                scanned.append(game)
            fingerprints[key] = digest
            continue
        else:
            raise BadRequest(f"Unsupported source type: {source_type}")
        scanned.extend(games)
        fingerprints[key] = fingerprint
    return scanned, fingerprints


def classify_candidates(raw_games: list[dict], *, state: dict | None = None) -> list[dict]:
    state = state or load_state()
    filtered = filter_imported(raw_games, state)
    exact, cross = _index_existing_games(state.get("games", []))
    blocked = exclusion_set(state)
    items = []
    for game in filtered:
        if len(items) >= MAX_ENTRIES:
            raise PreviewEntryLimitExceeded(
                f"Preview cannot exceed {MAX_ENTRIES} entries.",
                code="PREVIEW_ENTRY_LIMIT_EXCEEDED",
            )
        source = dict(game.pop("_source", {}))
        identity = _identity_for_game(game)
        candidate_id = _candidate_id(source.get("type", ""), str(source.get("id") or ""), identity, game.get("path"))
        platform = game.get("platform")
        choices = emulator_choices_for_platform(platform)
        warnings = []
        group = "additions"
        intended_action = "import"
        existing_target = None
        merge_diff = None
        source_keys = source_identities(game)
        target = next((exact[key] for key in source_keys if key in exact), None)
        title_identity = cross_source_identity(game)
        if target is None and title_identity and title_identity in cross:
            target = cross[title_identity]
        if any(
            isinstance(item, tuple) and item in blocked
            for item in (
                ("steam", str(game.get("steam_app_id") or "")),
                ("heroic", str(game.get("source") or ""), str(game.get("heroic_app_id") or "")),
                ("lutris", str(game.get("lutris_id") or "")),
                ("gameyfin", str(game.get("gameyfin_id") or "")),
            )
        ):
            group = "exclusions"
            intended_action = "exclude"
        elif target is not None:
            if normalize_path_identity(str(target.get("path") or "")) == normalize_path_identity(str(game.get("path") or "")):
                group = "duplicates"
                intended_action = "skip"
            else:
                group = "merges"
                intended_action = "merge"
                existing_target = {
                    "game_id": str(target.get("game_id") or ""),
                    "title": str(target.get("name") or ""),
                    "platform": target.get("platform"),
                }
                merge_diff = _merge_diff(target, game)
        elif not platform:
            group = "ambiguities"
            intended_action = "review"
            warnings.append({"code": "AMBIGUOUS_PLATFORM", "message": "Platform could not be determined."})
        elif not choices and not str(game.get("launch") or "").strip():
            group = "unsupported"
            intended_action = "review"
            warnings.append({"code": "UNSUPPORTED", "message": "No registry adapters for platform."})
        items.append(
            {
                "candidate_id": candidate_id,
                "group": group,
                "source": source,
                "detected_title": str(game.get("name") or ""),
                "detected_platform": platform,
                "intended_action": intended_action,
                "existing_game_target": existing_target,
                "warnings": warnings,
                "emulator_choices": choices,
                "selected_emulator_id": None,
                "selected_adapter_id": None,
                "launch_setup": None,
                "merge_diff": merge_diff,
                "_game": game,
                "_identity": identity,
            }
        )
    return items


def _merge_diff(target: dict, proposed: dict) -> list[dict]:
    fields = ("name", "platform", "path", "genre", "launch")
    diff = []
    for field in fields:
        current = target.get("name" if field == "name" else field)
        value = proposed.get("name" if field == "name" else field)
        if field == "name":
            current = target.get("name")
            value = proposed.get("name")
        if current == value or value in (None, ""):
            continue
        effect = "fill" if not current else "replace"
        if current and value and field != "launch":
            effect = "fill"
        diff.append({"field": field, "current": current, "proposed": value, "effect": effect})
    return diff or [{"field": "path", "current": target.get("path"), "proposed": proposed.get("path"), "effect": "fill"}]


def _count_groups(items: list[dict]) -> dict[str, int]:
    counts = {
        "additions": 0,
        "merges": 0,
        "duplicates": 0,
        "ambiguities": 0,
        "exclusions": 0,
        "unsupported": 0,
        "errors": 0,
    }
    for item in items:
        group = str(item.get("group") or "errors")
        counts[group] = counts.get(group, 0) + 1
    return counts


def create_preview_record(
    *,
    sources: list[dict],
    options: dict,
    source_fingerprints: dict[str, str] | None = None,
    items: list[dict] | None = None,
    data_dir: Path | None = None,
) -> dict:
    preview_id = uuid.uuid4().hex
    _enforce_preview_cap(data_dir=data_dir)
    preview = {
        "preview_id": preview_id,
        "revision": 1,
        "expires_at": _expires_at(),
        "state": "ready",
        "scanned_entries": len(items or []),
        "counts": _count_groups(items or []),
        "library_signature": library_signature(),
        "job_id": None,
        "sources": [
            {
                "type": str(source.get("type") or ""),
                "id": str(source.get("id") or source.get("path") or ""),
                "label": str(source.get("label") or source.get("type") or ""),
                "path": source.get("path"),
            }
            for source in sources
        ],
        "options": dict(options or {}),
        "source_fingerprints": dict(source_fingerprints or {}),
        "items": items or [],
        "decisions": {},
        "commit_tokens": {},
        "revalidated": False,
    }
    save_preview(preview, data_dir=data_dir)
    return preview


def run_scan_job(preview_id: str, *, data_dir: Path | None = None, cancel_event=None) -> dict:
    preview = load_preview(preview_id, data_dir=data_dir, allow_expired=True)
    preview["state"] = "running"
    save_preview(preview, data_dir=data_dir)
    sources = preview.get("_request_sources") or preview.get("sources") or []
    options = preview.get("options") or {}
    try:
        raw_games, fingerprints = scan_sources(sources, options)
        items = classify_candidates(raw_games)
        preview["source_fingerprints"] = fingerprints
        preview["items"] = items
        preview["scanned_entries"] = len(items)
        preview["counts"] = _count_groups(items)
        preview["library_signature"] = library_signature()
        preview["state"] = "ready"
        preview.pop("_request_sources", None)
        save_preview(preview, data_dir=data_dir)
        return {"scanned_entries": len(items), "counts": preview["counts"]}
    except Exception as error:
        preview["state"] = "error"
        save_preview(preview, data_dir=data_dir)
        raise error


def apply_decisions(preview_id: str, decisions: list[dict], *, data_dir: Path | None = None) -> dict:
    if len(decisions) > MAX_DECISION_BATCH:
        raise BadRequest(f"At most {MAX_DECISION_BATCH} decisions are allowed per request.")
    preview = load_preview(preview_id, data_dir=data_dir)
    items_by_id = {item["candidate_id"]: item for item in preview.get("items") or []}
    accepted = 0
    for decision in decisions:
        candidate_id = str(decision.get("candidate_id") or "").strip()
        item = items_by_id.get(candidate_id)
        if item is None:
            raise BadRequest(f"Unknown candidate_id: {candidate_id}")
        action = str(decision.get("action") or "").strip()
        if action not in {"import", "merge", "skip", "exclude"}:
            raise BadRequest("Invalid decision action.")
        item["intended_action"] = action
        if action == "merge":
            merge_target = str(decision.get("merge_target") or "").strip()
            if not merge_target:
                raise BadRequest("merge_target is required for merge decisions.")
            item["existing_game_target"] = item.get("existing_game_target") or {
                "game_id": merge_target,
                "title": "",
                "platform": item.get("detected_platform"),
            }
            item["existing_game_target"]["game_id"] = merge_target
        emulator_id = decision.get("emulator_id")
        adapter_id = decision.get("adapter_id")
        launch_setup = decision.get("launch_setup")
        if launch_setup == "adapter" and (not emulator_id or not adapter_id):
            raise BadRequest("adapter launch_setup requires emulator_id and adapter_id.")
        item["selected_emulator_id"] = emulator_id
        item["selected_adapter_id"] = adapter_id
        item["launch_setup"] = launch_setup
        preview.setdefault("decisions", {})[candidate_id] = dict(decision)
        accepted += 1
    save_preview(preview, data_dir=data_dir)
    return {"accepted": accepted}


def revalidate_preview_record(preview_id: str, *, data_dir: Path | None = None) -> dict:
    preview = load_preview(preview_id, data_dir=data_dir)
    current_signature = library_signature()
    if current_signature != preview.get("library_signature"):
        raise PreviewLibraryChanged("Library changed since preview was created.")
    sources = preview.get("sources") or []
    options = preview.get("options") or {}
    _, fingerprints = scan_sources(sources, options)
    if fingerprints != preview.get("source_fingerprints"):
        raise PreviewStale("Source fingerprints changed since preview was created.")
    preview["revalidated"] = True
    save_preview(preview, data_dir=data_dir)
    return preview_document(preview)


def _unresolved_items(preview: dict) -> list[dict]:
    unresolved = []
    for item in preview.get("items") or []:
        if item.get("group") == "ambiguities" and item.get("intended_action") == "review":
            unresolved.append(item)
    return unresolved


def _apply_emulator_choice(item: dict, choice: dict | None) -> None:
    if not choice:
        return
    launch_setup = choice.get("launch_setup")
    item["selected_emulator_id"] = choice.get("emulator_id")
    item["selected_adapter_id"] = choice.get("adapter_id")
    item["launch_setup"] = launch_setup


def commit_preview(
    preview_id: str,
    *,
    revision: int,
    options: dict | None = None,
    emulator_choices: list[dict] | None = None,
    import_batch_id: str | None = None,
    data_dir: Path | None = None,
) -> dict:
    preview = load_preview(preview_id, data_dir=data_dir)
    if int(preview.get("revision") or 0) != int(revision):
        raise PreviewStale("Preview revision is stale.")
    revalidate_preview_record(preview_id, data_dir=data_dir)
    preview = load_preview(preview_id, data_dir=data_dir)
    if _unresolved_items(preview):
        raise UnresolvedCandidates("Unresolved candidates remain in preview.")
    token = f"{preview_id}:{revision}"
    if token in preview.get("commit_tokens", {}):
        return preview["commit_tokens"][token]
    choices_by_id = {entry["candidate_id"]: entry for entry in (emulator_choices or []) if entry.get("candidate_id")}
    for item in preview.get("items") or []:
        _apply_emulator_choice(item, choices_by_id.get(item["candidate_id"]) or preview.get("decisions", {}).get(item["candidate_id"]))
    import_batch_id = str(import_batch_id or uuid.uuid4().hex)
    generated_dir = generated_m3u_dir()
    staging_dir = generated_dir / ".staging" / preview_id
    staging_dir.mkdir(parents=True, exist_ok=True)
    counts = {"added": 0, "merged": 0, "skipped": 0, "excluded": 0, "failed": 0}
    from openbox import update_state

    def mutate(state):
        exact, cross = _index_existing_games(state.get("games", []))
        timestamp = datetime.now().isoformat(timespec="seconds")
        for item in preview.get("items") or []:
            action = item.get("intended_action")
            game = dict(item.get("_game") or {})
            if action in {"skip", "exclude"}:
                counts["skipped" if action == "skip" else "excluded"] += 1
                continue
            if action not in {"import", "merge"}:
                counts["failed"] += 1
                continue
            launch_setup = item.get("launch_setup")
            if launch_setup == "adapter":
                if item.get("selected_emulator_id"):
                    game["emulator_id"] = item.get("selected_emulator_id")
                if item.get("selected_adapter_id"):
                    game["emulator_adapter_id"] = item.get("selected_adapter_id")
            elif launch_setup in {"keep_custom", "incomplete", None}:
                game.pop("emulator_id", None)
                game.pop("emulator_adapter_id", None)
            elif launch_setup == "install_flatpak":
                if item.get("selected_emulator_id"):
                    game["emulator_id"] = item.get("selected_emulator_id")
                if item.get("selected_adapter_id"):
                    game["emulator_adapter_id"] = item.get("selected_adapter_id")
            discs = list(game.get("discs") or [])
            if discs:
                base = Path(discs[0]).stem
                m3u = staging_dir / f"{base}.m3u"
                from pkg.parity.parity_import import generate_m3u

                generate_m3u([Path(d) for d in discs], m3u)
                game["path"] = str(m3u)
            game["import_batch_id"] = import_batch_id
            game["added_at"] = timestamp
            target = None
            if action == "merge":
                game_id = str((item.get("existing_game_target") or {}).get("game_id") or "")
                target = next((g for g in state["games"] if str(g.get("game_id")) == game_id), None)
            if target is not None:
                _merge_imported_game(target, game, add_launcher=True)
                counts["merged"] += 1
                continue
            source_keys = source_identities(game)
            if any(key in exact for key in source_keys):
                counts["skipped"] += 1
                continue
            state["games"].append(game)
            for key in source_keys:
                exact.setdefault(key, game)
            title_identity = cross_source_identity(game)
            if title_identity:
                cross.setdefault(title_identity, game)
            counts["added"] += 1

    update_state(mutate)
    if staging_dir.exists():
        generated_dir.mkdir(parents=True, exist_ok=True)
        for path in staging_dir.glob("*.m3u"):
            target = generated_dir / path.name
            atomic_write_bytes(target, path.read_bytes())
        shutil.rmtree(staging_dir, ignore_errors=True)
    result = {"import_batch_id": import_batch_id, **counts}
    preview.setdefault("commit_tokens", {})[token] = result
    save_preview(preview, data_dir=data_dir)
    return result
