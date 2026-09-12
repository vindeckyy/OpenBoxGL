"""Memory roots auto-import (S5): fold external screenshot folders into the library.

Steam (``userdata/<uid>/760/remote/<appid>/screenshots``), RetroArch
(``screenshots/`` incl. the ``screenshot_directory`` cfg override), Dolphin
(``ScreenShots/<TitleID>/``) and user-configured roots are scanned for image
files. Every candidate is deduplicated by SHA-256 against existing media and
previously imported memories, matched to at most one library game (ambiguous
candidates land in an ``memories_unassigned`` bucket rather than guessing),
and copied under ``<data_root>/media/memories/``.

Privacy posture: the whole feature is opt-in via ``memories_import_enabled``.
Callers gate on the flag before collecting — ``run_import`` and the
``/api/v2/memories/import`` job both return before any filesystem call is
made when the setting is off.
"""

import dataclasses
import hashlib
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

LOGGER = logging.getLogger("openbox")

MEMORY_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"})
IMPORT_JOB_NAME = "memories-import"
MEMORIES_FIELD = "memories"
UNASSIGNED_FIELD = "memories_unassigned"
MEDIA_SUBDIR = "memories"
UNASSIGNED_DIR = "_unassigned"

MAX_MEMORIES_PER_GAME = 200
MAX_IMPORT_FILES = 2000
MAX_IMPORT_BYTES = 2 * 1024**3
MAX_FILE_BYTES = 64 * 1024**2
MAX_UNASSIGNED = 500
MAX_IMPORT_ROOTS = 32
MAX_ERRORS = 20

_SAFE_COMPONENT = re.compile(r"[^a-zA-Z0-9._-]+")
_STEAM_STEM = re.compile(r"^(\d{14})_\d+$")
_RETROARCH_STAMP = re.compile(r"-(\d{6})-(\d{6})$")
_DOLPHIN_STAMP = re.compile(r"-\d+$")
_TAG_NOISE = re.compile(r"[\(\[][^\)\]]*[\)\]]")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


@dataclasses.dataclass
class MemoryCandidate:
    """One discovered screenshot file plus its matching hints."""

    path: Path
    source: str
    appid: str = ""
    title_id: str = ""
    names: tuple = ()


def memories_import_enabled(settings):
    """True only when the opt-in flag is set; falsy settings never scan."""
    return bool((settings or {}).get("memories_import_enabled"))


def configured_roots(settings):
    """Return sanitized user-configured memory roots (bounded)."""
    raw = (settings or {}).get("memories_import_roots") or []
    roots = []
    for value in raw:
        text = str(value or "").strip()
        if text:
            roots.append(text)
    return roots[:MAX_IMPORT_ROOTS]


def normalize_memory_key(value):
    """Normalize a title-ish string for comparison: tags and noise removed."""
    text = str(value or "").casefold()
    text = _TAG_NOISE.sub(" ", text)
    text = _RETROARCH_STAMP.sub("", text)
    text = _DOLPHIN_STAMP.sub("", text)
    return _NON_ALNUM.sub("", text)


def _safe_component(value):
    return _SAFE_COMPONENT.sub("_", str(value or "")) or "unknown"


def _retroarch_cfg_screenshot_dir(home):
    """Parse screenshot_directory from retroarch.cfg when present."""
    cfg = home / ".config" / "retroarch" / "retroarch.cfg"
    try:
        for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("screenshot_directory") and "=" in stripped:
                value = stripped.split("=", 1)[1].strip().strip('"')
                if value and value != "default":
                    return Path(os.path.expanduser(value))
                return None
    except OSError:
        return None
    return None


def _steam_remote_dirs(home):
    """Return every userdata/<uid>/760/remote directory under Steam roots."""
    remote_dirs = []
    for steam_root in (
        home / ".steam" / "steam",
        home / ".local" / "share" / "Steam",
    ):
        userdata = steam_root / "userdata"
        if not userdata.is_dir():
            continue
        try:
            accounts = [entry for entry in userdata.iterdir() if entry.is_dir()]
        except OSError:
            continue
        for account in accounts:
            remote = account / "760" / "remote"
            if remote.is_dir():
                remote_dirs.append(remote)
    return remote_dirs


def _discover_candidates(home):
    """Yield (kind, path) for every plausible auto-detected memory root."""
    for remote in _steam_remote_dirs(home):
        yield "steam", remote
    configured = _retroarch_cfg_screenshot_dir(home)
    retro_dirs = [configured] if configured else [
        home / ".config" / "retroarch" / "screenshots",
        home / "RetroArch" / "screenshots",
    ]
    for retro in retro_dirs:
        yield "retroarch", retro
    for dolphin in (
        home / ".local" / "share" / "dolphin-emu" / "ScreenShots",
        home / "Dolphin Emulator" / "ScreenShots",
    ):
        yield "dolphin", dolphin


def discover_memory_roots(home=None, extra_roots=None):
    """Locate memory roots. Returns (roots, skipped).

    roots: [{"kind": <source>, "path": Path}] for existing readable dirs.
    skipped: [{"path": str, "reason": str}] for missing/unreadable entries.
    """
    home = Path(home).expanduser() if home else Path.home()
    candidates = list(_discover_candidates(home))
    candidates.extend(("custom", Path(root).expanduser()) for root in (extra_roots or []))
    roots, skipped, seen = [], [], set()
    for kind, path in candidates:
        resolved = Path(path)
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        if not resolved.exists():
            # Auto-detected dirs that simply aren't there are quiet; a
            # user-configured root that vanished is worth surfacing.
            if kind == "custom":
                LOGGER.warning("Memories: skipping missing root %s", path)
                skipped.append({"path": str(path), "reason": "missing"})
            continue
        try:
            if not resolved.is_dir():
                raise NotADirectoryError(str(resolved))
            next(resolved.iterdir(), None)  # force scandir to probe readability
        except OSError as error:
            LOGGER.warning("Memories: skipping unreadable root %s: %s", path, error)
            skipped.append({"path": str(path), "reason": "unreadable"})
            continue
        roots.append({"kind": kind, "path": resolved})
    return roots, skipped


def _iter_image_files(root):
    """Yield image files under root without following symlinks."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]
        for name in filenames:
            candidate = Path(dirpath) / name
            if candidate.is_symlink():
                continue
            if candidate.suffix.casefold() in MEMORY_EXTENSIONS:
                yield candidate


def _steam_candidates(root, cancel=None):
    """Yield candidates from a steam remote dir: <appid>/screenshots/<file>."""
    appid_dirs = sorted(
        entry for entry in root.iterdir()
        if entry.is_dir() and not entry.is_symlink() and entry.name.isdigit()
    )
    for appid_dir in appid_dirs:
        shots = appid_dir / "screenshots"
        if not shots.is_dir():
            continue
        for path in _iter_image_files(shots):
            if cancel is not None and cancel.is_set():
                return
            if path.parent.name == "thumbnails":
                continue
            yield MemoryCandidate(path=path, source="steam", appid=appid_dir.name)


def _walk_candidates(root, kind, cancel=None):
    """Yield candidates from a walked directory tree (retroarch/dolphin/custom)."""
    for path in _iter_image_files(root):
        if cancel is not None and cancel.is_set():
            return
        if kind == "dolphin":
            yield MemoryCandidate(
                path=path, source=kind, title_id=path.parent.name, names=(path.stem,)
            )
        else:
            yield MemoryCandidate(path=path, source=kind, names=(path.stem,))


def scan_candidates(roots, cancel=None, errors=None):
    """Iterate MemoryCandidate objects for the discovered root list.

    Unreadable mid-scan roots are logged and appended to ``errors`` (when a
    list is provided) instead of aborting the whole scan.
    """
    for root in roots:
        if cancel is not None and cancel.is_set():
            return
        kind = root["kind"]
        try:
            if kind == "steam":
                yield from _steam_candidates(root["path"], cancel=cancel)
            else:
                yield from _walk_candidates(root["path"], kind, cancel=cancel)
        except OSError as error:
            LOGGER.warning("Memories: failed scanning %s: %s", root["path"], error)
            if errors is not None:
                errors.append({"path": str(root["path"]), "reason": "scan_failed"})


def _game_name_keys(game):
    """All normalized name keys a game answers to."""
    keys = set()
    for field in ("name", "sort_title", "rom_name"):
        key = normalize_memory_key(game.get(field))
        if key:
            keys.add(key)
    for alias in game.get("alternate_names") or []:
        key = normalize_memory_key(alias)
        if key:
            keys.add(key)
    stem_key = normalize_memory_key(Path(str(game.get("path") or "")).stem)
    if stem_key:
        keys.add(stem_key)
    return keys


def match_memory_game(games, candidate):
    """Match a candidate to a unique game. Returns (game_id or None, match_count).

    match_count is the number of library games the candidate resolved to;
    ``None, 0`` means unmatched, ``None, n>1`` means ambiguous.
    """
    matched = {}
    appid = str(candidate.appid or "").strip()
    if appid:
        for game in games:
            if str(game.get("steam_app_id") or "").strip() == appid:
                matched[str(game.get("game_id") or "")] = game
    title_id = normalize_memory_key(candidate.title_id)
    if title_id:
        for game in games:
            if normalize_memory_key(game.get("rom_name")) == title_id:
                matched[str(game.get("game_id") or "")] = game
            elif normalize_memory_key(Path(str(game.get("path") or "")).stem) == title_id:
                matched[str(game.get("game_id") or "")] = game
    name_keys = {normalize_memory_key(name) for name in candidate.names}
    name_keys.discard("")
    if name_keys:
        for game in games:
            if _game_name_keys(game) & name_keys:
                matched[str(game.get("game_id") or "")] = game
    matched.pop("", None)
    if len(matched) == 1:
        return next(iter(matched)), 1
    return None, len(matched)


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _DedupeIndex:
    """Content dedupe over existing media + already-imported memories."""

    def __init__(self):
        self.digests = set()
        self.sizes = {}

    @classmethod
    def from_state(cls, state):
        index = cls()
        for game in state.get("games") or []:
            for entry in game.get(MEMORIES_FIELD) or []:
                index._record_entry(entry)
            for raw in game.get("screenshots") or []:
                index._record_path(raw)
        for entry in state.get(UNASSIGNED_FIELD) or []:
            index._record_entry(entry)
        return index

    def _record_entry(self, entry):
        if not isinstance(entry, dict):
            return
        digest = str(entry.get("sha256") or "")
        path = str(entry.get("path") or "")
        size = 0
        try:
            size = int(entry.get("bytes") or 0)
        except (TypeError, ValueError):
            size = 0
        if not size and path:
            try:
                size = Path(path).stat().st_size
            except OSError:
                size = 0
        if digest:
            self.digests.add(digest)
        if size:
            self.sizes.setdefault(size, []).append([Path(path) if path else None, digest or None])

    def _record_path(self, raw):
        path = Path(str(raw))
        try:
            size = path.stat().st_size
        except OSError:
            return
        self.sizes.setdefault(size, []).append([path, None])

    def is_duplicate(self, size, digest):
        if digest in self.digests:
            return True
        for stored in self.sizes.get(size, []):
            stored_path, stored_digest = stored
            if stored_digest is None and stored_path is not None:
                try:
                    stored_digest = _sha256_file(stored_path)
                except OSError:
                    stored_digest = ""
                stored[1] = stored_digest  # memoize for later same-size probes
            if stored_digest and stored_digest == digest:
                return True
        return False

    def record(self, size, digest, path=None):
        self.digests.add(digest)
        self.sizes.setdefault(size, []).append([path, digest])


def _parse_taken_at(candidate, mtime):
    """Best-effort capture timestamp from source filename conventions."""
    stem = candidate.path.stem
    steam = _STEAM_STEM.match(stem)
    if steam:
        try:
            return datetime.strptime(steam.group(1), "%Y%m%d%H%M%S").isoformat()
        except ValueError:
            pass
    retro = _RETROARCH_STAMP.search(stem)
    if retro:
        try:
            stamp = datetime.strptime(
                f"{retro.group(1)}{retro.group(2)}", "%y%m%d%H%M%S"
            )
            return stamp.isoformat()
        except ValueError:
            pass
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()


def _copy_into_media(src, dest_dir, digest):
    """Copy src to content-addressed dest; returns Path or raises OSError."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{digest}{src.suffix.casefold()}"
    if dest.is_file():
        try:
            if dest.stat().st_size == src.stat().st_size:
                return dest
        except OSError:
            pass
    shutil.copyfile(src, dest)
    return dest


def _default_limits(overrides=None):
    limits = {
        "max_per_game": MAX_MEMORIES_PER_GAME,
        "max_files": MAX_IMPORT_FILES,
        "max_bytes": MAX_IMPORT_BYTES,
        "max_file_bytes": MAX_FILE_BYTES,
    }
    for key, value in (overrides or {}).items():
        if key in limits:
            try:
                limits[key] = max(0, int(value))
            except (TypeError, ValueError):
                continue
    return limits


def collect_imports(state, data_root, *, home=None, extra_roots=None,
                    progress=None, cancel=None, limits=None):
    """Scan roots, dedupe, match, copy. Returns an apply-ready plan dict."""
    plan = {
        "attach": {},
        "unassigned": [],
        "max_per_game": 0,
        "counts": {
            "enabled": True, "roots": 0, "scanned": 0, "added": 0,
            "unassigned": 0, "skipped": 0, "failed": 0,
            "errors": [], "skipped_roots": [], "cancelled": False,
        },
    }
    roots, skipped = discover_memory_roots(home=home, extra_roots=extra_roots)
    counts = plan["counts"]
    counts["roots"] = len(roots)
    counts["skipped_roots"] = skipped
    caps = _default_limits(limits)
    plan["max_per_game"] = caps["max_per_game"]
    dedupe = _DedupeIndex.from_state(state)
    games = state.get("games") or []
    media_root = Path(data_root) / "media" / MEDIA_SUBDIR
    pending_unassigned = len(state.get(UNASSIGNED_FIELD) or [])
    existing_counts = {
        str(game.get("game_id") or ""): len(game.get(MEMORIES_FIELD) or [])
        for game in games
        if isinstance(game, dict)
    }
    bytes_used = 0
    for candidate in scan_candidates(roots, cancel=cancel, errors=counts["skipped_roots"]):
        if cancel is not None and cancel.is_set():
            counts["cancelled"] = True
            break
        counts["scanned"] += 1
        if progress is not None:
            progress(phase="scan", current=counts["scanned"], total=0)
        staged = _stage_candidate(
            candidate, games, dedupe, media_root, caps, bytes_used,
            pending_unassigned, existing_counts, plan, counts,
        )
        if staged is not None:
            bytes_used += staged["bytes"]
    return plan


def _stage_candidate(candidate, games, dedupe, media_root, caps,
                     bytes_used, pending_unassigned, existing_counts, plan, counts):
    """Dedupe/match/copy one candidate into the plan. Returns entry or None."""
    if counts["added"] + counts["unassigned"] >= caps["max_files"]:
        counts["skipped"] += 1
        return None
    try:
        size = candidate.path.stat().st_size
    except OSError as error:
        counts["failed"] += 1
        _record_error(counts, f"{candidate.path}: {error}")
        return None
    if size > caps["max_file_bytes"] or bytes_used + size > caps["max_bytes"]:
        counts["skipped"] += 1
        return None
    try:
        digest = _sha256_file(candidate.path)
    except OSError as error:
        counts["failed"] += 1
        _record_error(counts, f"{candidate.path}: {error}")
        return None
    if dedupe.is_duplicate(size, digest):
        counts["skipped"] += 1
        return None
    game_id, match_count = match_memory_game(games, candidate)
    if game_id is None and match_count == 0:
        counts["skipped"] += 1
        return None
    if game_id is not None:
        held = existing_counts.get(game_id, 0) + len(plan["attach"].get(game_id, []))
        if held >= caps["max_per_game"]:
            counts["skipped"] += 1
            return None
    entry = _import_candidate_file(candidate, game_id, digest, size, media_root, counts)
    if entry is None:
        return None
    dedupe.record(size, digest, Path(entry["path"]))
    if game_id is None:
        if pending_unassigned + len(plan["unassigned"]) >= MAX_UNASSIGNED:
            counts["skipped"] += 1
            return entry
        entry["hint"] = candidate.title_id or next(iter(candidate.names), "")
        plan["unassigned"].append(entry)
        counts["unassigned"] += 1
        return entry
    plan["attach"].setdefault(game_id, []).append(entry)
    counts["added"] += 1
    return entry


def _import_candidate_file(candidate, game_id, digest, size, media_root, counts):
    """Copy the candidate under the media root and build its entry dict."""
    dest_dir = media_root / (UNASSIGNED_DIR if game_id is None else _safe_component(game_id))
    try:
        dest = _copy_into_media(candidate.path, dest_dir, digest)
        taken_at = _parse_taken_at(candidate, candidate.path.stat().st_mtime)
    except OSError as error:
        counts["failed"] += 1
        _record_error(counts, f"{candidate.path}: {error}")
        return None
    return {
        "path": str(dest),
        "sha256": digest,
        "bytes": size,
        "source": candidate.source,
        "taken_at": taken_at,
        "imported_at": datetime.now(timezone.utc).isoformat(),
    }


def _record_error(counts, message):
    if len(counts["errors"]) < MAX_ERRORS:
        counts["errors"].append(str(message))


def apply_import_plan(state, plan):
    """Apply a collected plan to state. Returns {'attached','unassigned','failed'}.

    Per-game caps are re-checked here so a plan collected against a snapshot
    cannot overflow a game that gained memories meanwhile.
    """
    counts = {"attached": 0, "unassigned": 0, "failed": 0}
    try:
        max_per_game = max(0, int(plan.get("max_per_game") or 0)) or MAX_MEMORIES_PER_GAME
    except (TypeError, ValueError):
        max_per_game = MAX_MEMORIES_PER_GAME
    games = state.get("games") or []
    by_id = {
        str(game.get("game_id") or ""): game for game in games if isinstance(game, dict)
    }
    for alias_owner in games:
        for alias in alias_owner.get("legacy_game_ids") or []:
            by_id.setdefault(str(alias), alias_owner)
    for game_id, entries in (plan.get("attach") or {}).items():
        game = by_id.get(str(game_id))
        if game is None:
            counts["failed"] += len(entries)
            continue
        existing = game.get(MEMORIES_FIELD)
        if not isinstance(existing, list):
            existing = []
            game[MEMORIES_FIELD] = existing
        known = {
            str(item.get("sha256") or "")
            for item in existing
            if isinstance(item, dict)
        }
        for entry in entries:
            if len(existing) >= max_per_game:
                break
            if entry.get("sha256") in known:
                continue
            existing.append(dict(entry))
            known.add(entry.get("sha256"))
            counts["attached"] += 1
    _apply_unassigned(state, plan, counts)
    return counts


def _apply_unassigned(state, plan, counts):
    """Append plan unassigned entries to the bounded state bucket."""
    unassigned = state.setdefault(UNASSIGNED_FIELD, [])
    if not isinstance(unassigned, list):
        unassigned = []
        state[UNASSIGNED_FIELD] = unassigned
    known_unassigned = {
        str(item.get("sha256") or "") for item in unassigned if isinstance(item, dict)
    }
    for entry in plan.get("unassigned") or []:
        if len(unassigned) >= MAX_UNASSIGNED:
            break
        if entry.get("sha256") in known_unassigned:
            continue
        unassigned.append(dict(entry))
        known_unassigned.add(entry.get("sha256"))
        counts["unassigned"] += 1
    if not unassigned:
        state.pop(UNASSIGNED_FIELD, None)


def run_import(state, data_root, *, home=None, extra_roots=None,
               progress=None, cancel=None, limits=None, settings=None):
    """End-to-end import: enabled check, collect, apply. Returns summary."""
    settings = settings if settings is not None else state.get("settings") or {}
    if not memories_import_enabled(settings):
        return {
            "enabled": False, "roots": 0, "scanned": 0, "added": 0,
            "unassigned": 0, "skipped": 0, "failed": 0,
            "errors": [], "skipped_roots": [], "cancelled": False,
        }
    roots = configured_roots(settings) + list(extra_roots or [])
    plan = collect_imports(
        state, data_root, home=home, extra_roots=roots,
        progress=progress, cancel=cancel, limits=limits,
    )
    applied = apply_import_plan(state, plan)
    summary = dict(plan["counts"])
    summary["added"] = applied["attached"]
    summary["unassigned"] = applied["unassigned"]
    summary["failed"] = summary["failed"] + applied["failed"]
    return summary
