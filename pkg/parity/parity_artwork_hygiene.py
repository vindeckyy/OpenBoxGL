"""Artwork Doctor bulk hygiene report and safe fix/undo helpers (F5).

The provider side (search, assets, download) lives in ``parity_steamgrid``;
this module owns the local analysis and the file-level undo journal:

* ``build_report`` scans the library for missing covers, low-resolution art,
  wrong-aspect art, and duplicate artwork, all read-only.
* ``snapshot_for_replacement`` / ``undo_batch`` implement the "undo of
  replaced files" half of the fix-all job. The handler owns state mutation;
  this module owns bytes on disk and the manifest that can restore them.

The report never touches the network and never writes state, so it is safe to
run against large libraries. Provider artwork is attributed to SteamGridDB.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import struct
from datetime import datetime, timezone
from pathlib import Path

PROVIDER_ATTRIBUTION = "SteamGridDB"
UNDO_DIRECTORY = "artwork-hygiene"

# kind -> (expected width/height ratio, tolerance, label)
ASPECT_TARGETS = {
    "cover": (3 / 4, 0.16),
    "hero": (16 / 9, 0.35),
    "background": (16 / 9, 0.35),
    "banner": (460 / 215, 0.35),
}
ARTWORK_FIELDS = ("cover", "hero", "background", "clear_logo", "icon", "banner")
ISSUE_LABELS = {
    "missing_cover": "Missing cover",
    "missing_file": "Artwork file is missing",
    "low_res": "Low resolution",
    "wrong_aspect": "Wrong aspect ratio",
    "duplicate": "Duplicate artwork",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _png_size(blob):
    if len(blob) < 24 or blob[:8] != b"\x89PNG\r\n\x1a\n" or blob[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", blob[16:24])


def _gif_size(blob):
    if len(blob) < 10 or blob[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    return struct.unpack("<HH", blob[6:10])


def _bmp_size(blob):
    if len(blob) < 26 or blob[:2] != b"BM":
        return None
    width, height = struct.unpack("<ii", blob[18:26])
    return abs(width), abs(height)


def _jpeg_size(blob):
    if len(blob) < 4 or blob[:2] != b"\xff\xd8":
        return None
    index = 2
    while index + 9 < len(blob):
        if blob[index] != 0xFF:
            index += 1
            continue
        marker = blob[index + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        length = struct.unpack(">H", blob[index + 2:index + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height, width = struct.unpack(">HH", blob[index + 5:index + 9])
            return width, height
        index += 2 + max(length, 2)
    return None


def _webp_size(blob):
    if len(blob) < 30 or blob[:4] != b"RIFF" or blob[8:12] != b"WEBP":
        return None
    chunk = blob[12:16]
    if chunk == b"VP8X":
        width = int.from_bytes(blob[24:27], "little") + 1
        height = int.from_bytes(blob[27:30], "little") + 1
        return width, height
    if chunk == b"VP8 ":
        start = blob.find(b"\x9d\x01\x2a", 20)
        if start != -1 and start + 7 <= len(blob):
            width = int.from_bytes(blob[start + 3:start + 5], "little") & 0x3FFF
            height = int.from_bytes(blob[start + 5:start + 7], "little") & 0x3FFF
            return width, height
    if chunk == b"VP8L" and len(blob) >= 25:
        bits = int.from_bytes(blob[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    return None


def image_dimensions(path):
    """Return (width, height) for common raster formats, or None."""
    try:
        with Path(path).open("rb") as handle:
            header = handle.read(64)
            if header[:2] == b"\xff\xd8":
                return _jpeg_size(handle.read())
            handle.seek(0)
            blob = handle.read(65536)
    except OSError:
        return None
    for parser in (_png_size, _gif_size, _bmp_size, _webp_size):
        size = parser(blob)
        if size:
            return size
    return None


def _file_digest(path):
    digest = hashlib.sha1()
    try:
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def resolve_artwork_path(value, media_root=None):
    """Resolve a stored artwork value (absolute or library-relative)."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("http://") or text.startswith("https://"):
        return None
    candidate = Path(text).expanduser()
    if not candidate.is_absolute() and media_root:
        candidate = Path(media_root) / candidate
    return candidate


def _aspect_issue(kind, width, height):
    target = ASPECT_TARGETS.get(kind)
    if not target or not width or not height:
        return None
    expected, tolerance = target
    actual = width / height
    if abs(actual - expected) > tolerance:
        return round(actual, 3), round(expected, 3)
    return None


def build_report(games, *, media_root=None, low_res_min=200, include_duplicates=True):
    """Build the read-only hygiene report for a game list."""
    issues = []
    covers = []
    scanned = 0
    for game in games or []:
        if not isinstance(game, dict):
            continue
        scanned += 1
        game_id = str(game.get("game_id") or "")
        name = str(game.get("name") or "")
        missing_cover = not str(game.get("cover") or "").strip()
        if missing_cover:
            issues.append({
                "issue": "missing_cover",
                "label": ISSUE_LABELS["missing_cover"],
                "game_id": game_id,
                "name": name,
                "field": "cover",
                "provider": str(game.get("artwork_provider") or ""),
            })
        for field in ARTWORK_FIELDS:
            value = game.get(field)
            if not str(value or "").strip():
                continue
            path = resolve_artwork_path(value, media_root)
            if path is None:
                continue
            if not path.is_file():
                issues.append({
                    "issue": "missing_file",
                    "label": ISSUE_LABELS["missing_file"],
                    "game_id": game_id,
                    "name": name,
                    "field": field,
                    "path": str(value),
                })
                continue
            dimensions = image_dimensions(path)
            if dimensions is None:
                continue
            width, height = dimensions
            if min(width, height) < int(low_res_min):
                issues.append({
                    "issue": "low_res",
                    "label": ISSUE_LABELS["low_res"],
                    "game_id": game_id,
                    "name": name,
                    "field": field,
                    "width": width,
                    "height": height,
                })
            aspect = _aspect_issue(field, width, height)
            if aspect:
                issues.append({
                    "issue": "wrong_aspect",
                    "label": ISSUE_LABELS["wrong_aspect"],
                    "game_id": game_id,
                    "name": name,
                    "field": field,
                    "aspect": aspect[0],
                    "expected_aspect": aspect[1],
                })
            if field == "cover" and include_duplicates:
                digest = _file_digest(path)
                if digest:
                    covers.append((digest, game_id, name, str(path)))

    duplicates = []
    if include_duplicates:
        grouped = {}
        for digest, game_id, name, path in covers:
            grouped.setdefault(digest, []).append({"game_id": game_id, "name": name, "path": path})
        for digest, members in grouped.items():
            if len(members) > 1:
                duplicates.append({"digest": digest, "games": members, "count": len(members)})
    for group in duplicates:
        for member in group["games"]:
            issues.append({
                "issue": "duplicate",
                "label": ISSUE_LABELS["duplicate"],
                "game_id": member["game_id"],
                "name": member["name"],
                "field": "cover",
                "path": member["path"],
                "digest": group["digest"],
            })

    counts = {key: 0 for key in ISSUE_LABELS}
    for issue in issues:
        counts[issue["issue"]] = counts.get(issue["issue"], 0) + 1
    provider_counts = {}
    for issue in issues:
        provider = issue.get("provider")
        if provider:
            provider_counts[provider] = provider_counts.get(provider, 0) + 1
    return {
        "generated_at": _utc_now(),
        "scanned": scanned,
        "issues": issues,
        "counts": counts,
        "duplicates": duplicates,
        "provider_attribution": PROVIDER_ATTRIBUTION,
        "providers": provider_counts,
        "thresholds": {"low_res_min": int(low_res_min)},
    }


def select_fixable(report, *, issues=None, fields=None, game_ids=None):
    """Return the deduplicated (game_id, field) pairs worth replacing."""
    wanted_issues = set(issues) if issues else {"missing_cover", "low_res", "wrong_aspect", "duplicate"}
    wanted_fields = set(fields) if fields else {"cover"}
    wanted_games = {str(item) for item in game_ids} if game_ids else None
    selected = {}
    for issue in (report or {}).get("issues", []):
        if issue.get("issue") not in wanted_issues:
            continue
        if issue.get("issue") == "missing_file":
            continue
        game_id = str(issue.get("game_id") or "")
        field = str(issue.get("field") or "cover")
        if field not in wanted_fields or not game_id:
            continue
        if wanted_games is not None and game_id not in wanted_games:
            continue
        selected[(game_id, field)] = {
            "game_id": game_id,
            "name": issue.get("name") or "",
            "field": field,
            "issues": sorted({*selected.get((game_id, field), {}).get("issues", []), issue["issue"]}),
        }
    return list(selected.values())


def undo_root(cache_dir):
    return Path(cache_dir) / UNDO_DIRECTORY


def snapshot_for_replacement(cache_dir, batch_id, game_id, field, current_path, backup=True):
    """Copy the current file into the undo journal before it is replaced.

    Returns a journal record. ``created`` marks a file that did not exist, so
    undo removes it instead of restoring bytes.
    """
    root = undo_root(cache_dir) / str(batch_id)
    root.mkdir(parents=True, exist_ok=True)
    current = Path(current_path) if current_path else None
    record = {
        "game_id": str(game_id),
        "field": str(field),
        "previous": str(current_path or ""),
        "created": not (current and current.is_file()),
        "backup": "",
    }
    if not record["created"] and backup:
        target = root / f"{str(game_id)[:40]}-{field}{current.suffix or '.bin'}"
        try:
            shutil.copy2(current, target)
            record["backup"] = str(target)
        except OSError:
            record["created"] = True
    return record


def write_undo_manifest(cache_dir, batch_id, records, *, provider=PROVIDER_ATTRIBUTION):
    root = undo_root(cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "batch_id": str(batch_id),
        "created_at": _utc_now(),
        "provider": provider,
        "records": records,
    }
    path = root / f"{batch_id}.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return str(path)


def load_undo_manifest(cache_dir, batch_id):
    path = undo_root(cache_dir) / f"{batch_id}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("No artwork undo journal was found for this batch.") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        raise ValueError("The artwork undo journal is invalid.")
    if str(payload.get("batch_id")) != str(batch_id):
        raise ValueError("The artwork undo journal does not match the requested batch.")
    return payload


def undo_batch(cache_dir, batch_id):
    """Restore replaced artwork files. Returns the list of restored records."""
    manifest = load_undo_manifest(cache_dir, batch_id)
    restored = []
    for record in manifest["records"]:
        if not isinstance(record, dict):
            continue
        previous = str(record.get("previous") or "")
        backup = str(record.get("backup") or "")
        entry = {"game_id": record.get("game_id"), "field": record.get("field"), "action": ""}
        if record.get("created"):
            entry["action"] = "removed"
        elif backup and Path(backup).is_file() and previous:
            destination = Path(previous)
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, destination)
                entry["action"] = "restored"
            except OSError:
                entry["action"] = "failed"
        else:
            entry["action"] = "skipped"
        restored.append(entry)
    return restored


def list_undo_batches(cache_dir, limit=20):
    root = undo_root(cache_dir)
    if not root.is_dir():
        return []
    batches = []
    for path in sorted(root.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        batches.append({
            "batch_id": payload.get("batch_id", path.stem),
            "created_at": payload.get("created_at", ""),
            "provider": payload.get("provider", ""),
            "count": len(payload.get("records", [])),
        })
        if len(batches) >= int(limit):
            break
    return batches
