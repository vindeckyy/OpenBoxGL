"""Per-game save backup and restore."""

import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import zipfile
from datetime import datetime
from pathlib import Path

from backend_io import atomic_copy_stream, fsync_directory
from pkg.platform_compat import IS_MACOS, IS_WINDOWS, steam_candidate_roots, steam_install_roots, user_symlink_component


MAX_SAVE_ARCHIVE_MEMBER_BYTES = 4 * 1024 * 1024 * 1024
MAX_SAVE_ARCHIVE_TOTAL_BYTES = 32 * 1024 * 1024 * 1024
MAX_SAVE_ARCHIVE_MEMBERS = 50_000
PREVIEW_BUCKET_LIMIT = 200

# Serializes save-archive creation/extraction against cloud-sync uploads so a
# sync never observes a half-restored save tree and concurrent restores cannot
# interleave.  Reentrant because ``restore_saves`` nests ``backup_saves``.
SAVE_SYNC_LOCK = threading.RLock()


def _reject_symlink_components(path):
    component = user_symlink_component(path)
    if component is not None:
        raise ValueError(f"Save path may not contain symlinks: {component}")


def _manifest_entries(roots):
    """Describe every regular file before archiving: size, mode, mtime, hash.

    Symlinks are rejected by the caller; recording the target instead would let
    a later restore create a link the user never approved.
    """
    entries = []
    for index, source in enumerate(roots):
        if source.is_file():
            candidates = [(source, source.name)]
        else:
            _reject_symlink_components(source)
            candidates = []
            for file in source.rglob("*"):
                if file.is_symlink():
                    raise ValueError(f"Save backup source contains a symlink: {file}")
                if file.is_file():
                    candidates.append((file, str(file.relative_to(source))))
        for file, relative in candidates:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(file, flags)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError(f"Save backup source is not a regular file: {file}")
                digest = hashlib.sha256()
                with os.fdopen(os.dup(descriptor), "rb") as input_file:
                    for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
                        digest.update(chunk)
            finally:
                os.close(descriptor)
            entries.append({
                "root": index,
                "relative": relative,
                "archive": f"roots/{index}/{relative}" if not source.is_file() else f"roots/{index}/{source.name}",
                "size": info.st_size,
                "mode": stat.S_IMODE(info.st_mode),
                "mtime": int(info.st_mtime),
                "sha256": digest.hexdigest(),
            })
    return entries


def _write_archive_file(package, source, archive_name):
    """Copy a file opened without following a symlink into a ZIP member."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as error:
        raise ValueError(f"Save backup source could not be opened safely: {source}") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"Save backup source is not a regular file: {source}")
        with os.fdopen(descriptor, "rb") as input_file:
            descriptor = -1
            with package.open(archive_name, "w") as output:
                while True:
                    chunk = input_file.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _restrict_archive_permissions(folder):
    for existing in Path(folder).glob("*.zip"):
        if existing.is_symlink():
            continue
        os.chmod(existing, 0o600)


def _write_private_archive(archive, populate):
    archive = Path(archive)
    archive.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if archive.parent.is_symlink():
        raise ValueError("Save backup directory may not be a symlink.")
    os.chmod(archive.parent, 0o700)
    _restrict_archive_permissions(archive.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{archive.name}.", suffix=".tmp", dir=archive.parent
    )
    temporary = Path(temporary_name)
    os.chmod(temporary, 0o600)
    try:
        with os.fdopen(descriptor, "w+b") as output:
            descriptor = -1
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as package:
                populate(package)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, archive)
        os.chmod(archive, 0o600)
        fsync_directory(archive.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def game_backup_dir(game, root):
    key = hashlib.sha256(f"{game.get('name','')}:{game.get('path','')}".encode()).hexdigest()[:16]
    return Path(root) / key


def save_roots(game):
    configured = game.get("save_paths", [])
    if not isinstance(configured, list):
        return []
    roots = []
    for path in configured:
        if not str(path).strip():
            continue
        raw_path = Path(path).expanduser()
        _reject_symlink_components(raw_path)
        roots.append(raw_path.resolve())
    return roots


def _source_entry(path, label, shared, *, emulator_id="", core="", slot=""):
    return {
        "path": str(path),
        "label": label,
        "shared": bool(shared),
        "emulator_id": str(emulator_id),
        "core": str(core),
        "slot": str(slot),
    }


def discover_save_paths(game, home=None):
    if home is None and IS_WINDOWS:
        return _discover_save_paths_windows(game)
    if home is None and IS_MACOS:
        return _discover_save_paths_darwin(game, Path.home())
    home = Path(home or Path.home())
    candidates = []
    app_id = str(game.get("steam_app_id", ""))
    if app_id.isdigit():
        for steam in (
            home / ".local/share/Steam",
            home / ".steam/steam",
            home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
        ):
            candidates.extend(
                _source_entry(path, "Steam Cloud", False, emulator_id="steam")
                for path in (steam / "userdata").glob(f"*/{app_id}/remote")
                if path.is_dir()
            )
    shared = {
        "PlayStation 2": [
            home / ".config/PCSX2/memcards",
            home / ".var/app/net.pcsx2.PCSX2/config/PCSX2/memcards",
        ],
        "PSP": [
            home / ".config/ppsspp/PSP/SAVEDATA",
            home / ".var/app/org.ppsspp.PPSSPP/config/ppsspp/PSP/SAVEDATA",
        ],
        "PlayStation 3": [
            home / ".config/rpcs3/dev_hdd0/home/00000001/savedata",
            home / ".var/app/net.rpcs3.RPCS3/config/rpcs3/dev_hdd0/home/00000001/savedata",
        ],
        "GameCube": [
            home / ".local/share/dolphin-emu/GC",
            home / ".var/app/org.DolphinEmu.dolphin-emu/data/dolphin-emu/GC",
        ],
        "Wii": [
            home / ".local/share/dolphin-emu/Wii/title",
            home / ".var/app/org.DolphinEmu.dolphin-emu/data/dolphin-emu/Wii/title",
        ],
        "WiiWare": [
            home / ".local/share/dolphin-emu/Wii/title",
            home / ".var/app/org.DolphinEmu.dolphin-emu/data/dolphin-emu/Wii/title",
        ],
        "Sega Saturn": [
            home / ".config/retroarch/saves",
            home / ".var/app/org.libretro.RetroArch/config/retroarch/saves",
            home / ".mednafen",
        ],
        "Wii U": [home / ".local/share/Cemu/mlc01/usr/save"],
    }
    shared_emulators = {
        "PlayStation 2": "pcsx2",
        "PSP": "ppsspp",
        "PlayStation 3": "rpcs3",
        "GameCube": "dolphin",
        "Wii": "dolphin",
        "WiiWare": "dolphin",
        "Sega Saturn": "retroarch",
        "Wii U": "cemu",
    }
    retro_roots = (
        home / ".config/retroarch/saves",
        home / ".config/retroarch/states",
        home / ".var/app/org.libretro.RetroArch/config/retroarch/saves",
        home / ".var/app/org.libretro.RetroArch/config/retroarch/states",
    )
    candidates.extend(_shared_and_retro_candidates(game, shared, shared_emulators, retro_roots))
    return _unique_candidates(candidates)


def _shared_and_retro_candidates(game, shared, shared_emulators, retro_roots):
    """Candidates from a platform's shared save folders and the RetroArch saves that match the title."""
    platform = str(game.get("platform", ""))
    candidates = []
    for path in shared.get(platform, []):
        if not path.exists():
            continue
        emulator_id = shared_emulators.get(platform, "")
        if path.name == ".mednafen":
            emulator_id = "mednafen"
        candidates.append(_source_entry(path, f"{platform} shared saves", True, emulator_id=emulator_id))
    title = re.sub(r"[^a-z0-9]+", "", str(game.get("name") or Path(game.get("path", "")).stem).casefold())
    for root in retro_roots:
        if not root.is_dir() or not title:
            continue
        for path in root.rglob("*"):
            if path.is_file() and re.sub(r"[^a-z0-9]+", "", path.stem.casefold()) == title:
                core = path.parent.name if path.parent != root else ""
                candidates.append(_source_entry(path, "RetroArch save", False, emulator_id="retroarch", core=core))
    return candidates


def _unique_candidates(candidates):
    unique = {}
    for candidate in candidates:
        unique[candidate["path"]] = candidate
    return list(unique.values())


def _discover_save_paths_darwin(game, home):
    """Discover Steam Cloud and the macOS save folders that upstream documents.

    A folder is listed only when an upstream source names it for macOS. Emulators
    without such a source (PCSX2, Cemu) are left out rather than guessed.
    """
    candidates = []
    app_id = str(game.get("steam_app_id", ""))
    if app_id.isdigit():
        for steam in steam_candidate_roots(home):
            if not steam.is_dir():
                continue
            candidates.extend(
                _source_entry(path, "Steam Cloud", False, emulator_id="steam")
                for path in (steam / "userdata").glob(f"*/{app_id}/remote")
                if path.is_dir()
            )
    library = home / "Library" / "Application Support"
    retroarch = home / "Documents" / "RetroArch"
    shared = {
        # ppsspp.org/docs/reference/mac: the Mac build keeps its memory stick in ~/.config/ppsspp.
        "PSP": [home / ".config" / "ppsspp" / "PSP" / "SAVEDATA"],
        # rpcs3.net/faq: macOS save data lives under ~/Library/Application Support/rpcs3.
        "PlayStation 3": [library / "rpcs3" / "dev_hdd0" / "home" / "00000001" / "savedata"],
        # dolphin-emu.org/docs/guides/controlling-global-user-directory: ~/Library/Application Support/Dolphin.
        "GameCube": [library / "Dolphin" / "GC"],
        "Wii": [library / "Dolphin" / "Wii" / "title"],
        "WiiWare": [library / "Dolphin" / "Wii" / "title"],
        # github.com/libretro/RetroArch/issues/14561: RetroArch saves default to ~/Documents/RetroArch.
        "Sega Saturn": [retroarch / "saves"],
    }
    shared_emulators = {
        "PSP": "ppsspp",
        "PlayStation 3": "rpcs3",
        "GameCube": "dolphin",
        "Wii": "dolphin",
        "WiiWare": "dolphin",
        "Sega Saturn": "retroarch",
    }
    candidates.extend(
        _shared_and_retro_candidates(game, shared, shared_emulators, (retroarch / "saves", retroarch / "states"))
    )
    return _unique_candidates(candidates)


def _windows_documents() -> Path:
    documents = os.environ.get("USERPROFILE", "")
    base = Path(documents) if documents else Path.home()
    return base / "Documents"


def _discover_save_paths_windows(game):
    """Discover common Windows emulator and Steam save locations."""
    home = Path.home()
    documents = _windows_documents()
    roaming = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    candidates = []
    app_id = str(game.get("steam_app_id", ""))
    if app_id.isdigit():
        for steam in steam_install_roots():
            userdata = steam / "userdata"
            if not userdata.is_dir():
                continue
            candidates.extend(
                _source_entry(path, "Steam Cloud", False, emulator_id="steam")
                for path in userdata.glob(f"*/{app_id}/remote")
                if path.is_dir()
            )
    platform = str(game.get("platform", ""))
    shared = {
        "PlayStation 2": [documents / "PCSX2" / "memcards", roaming / "PCSX2" / "memcards"],
        "PSP": [documents / "PPSSPP" / "PSP" / "SAVEDATA"],
        "PlayStation 3": [roaming / "rpcs3" / "dev_hdd0" / "home" / "00000001" / "savedata"],
        "GameCube": [documents / "Dolphin Emulator" / "GC"],
        "Wii": [documents / "Dolphin Emulator" / "Wii" / "title"],
        "WiiWare": [documents / "Dolphin Emulator" / "Wii" / "title"],
        "Sega Saturn": [roaming / "retroarch" / "saves", local / "retroarch" / "saves"],
        "Wii U": [roaming / "Cemu" / "mlc01" / "usr" / "save"],
    }
    shared_emulators = {
        "PlayStation 2": "pcsx2",
        "PSP": "ppsspp",
        "PlayStation 3": "rpcs3",
        "GameCube": "dolphin",
        "Wii": "dolphin",
        "WiiWare": "dolphin",
        "Sega Saturn": "retroarch",
        "Wii U": "cemu",
    }
    for path in shared.get(platform, []):
        if not path.exists():
            continue
        candidates.append(
            _source_entry(path, f"{platform} shared saves", True, emulator_id=shared_emulators.get(platform, ""))
        )
    title = re.sub(r"[^a-z0-9]+", "", str(game.get("name") or Path(game.get("path", "")).stem).casefold())
    retro_roots = (
        roaming / "retroarch" / "saves",
        roaming / "retroarch" / "states",
        local / "retroarch" / "saves",
        local / "retroarch" / "states",
    )
    for root in retro_roots:
        if not root.is_dir() or not title:
            continue
        for path in root.rglob("*"):
            if path.is_file() and re.sub(r"[^a-z0-9]+", "", path.stem.casefold()) == title:
                core = path.parent.name if path.parent != root else ""
                candidates.append(
                    _source_entry(path, "RetroArch save", False, emulator_id="retroarch", core=core)
                )
    unique = {}
    for candidate in candidates:
        unique[candidate["path"]] = candidate
    return list(unique.values())


def file_sha256(path):
    """Streaming SHA-256 of an existing regular file; symlinks are refused."""
    raw = Path(path)
    if raw.is_symlink():
        raise ValueError(f"Save path may not be a symlink: {raw}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(raw, flags)
    except OSError as error:
        raise ValueError(f"Save file could not be opened safely: {raw}") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"Save path is not a regular file: {raw}")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _normalized_source_path(path):
    try:
        return Path(str(path)).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def save_source_for(game, path):
    """Per-path save provenance recorded by the save scan.

    Returns ``{"emulator_id", "core", "slot"}`` with empty strings when the
    path has no recorded source; restore/delete callers use it only as a hint
    and keep their existing behaviour when it is empty.
    """
    empty = {"emulator_id": "", "core": "", "slot": ""}
    sources = game.get("save_sources") if isinstance(game, dict) else None
    if not isinstance(sources, dict) or not sources:
        return dict(empty)
    wanted = str(path)

    def _normalized(entry):
        if not isinstance(entry, dict):
            return dict(empty)
        return {
            key: str(entry.get(key) or "")
            for key in ("emulator_id", "core", "slot")
        }

    if wanted in sources:
        return _normalized(sources[wanted])
    target = _normalized_source_path(wanted)
    if target is not None:
        for key, entry in sources.items():
            if str(key) == wanted:
                continue
            if _normalized_source_path(key) == target:
                return _normalized(entry)
    return dict(empty)


def backup_saves(game, root, label="manual", *, allow_empty=False, emulator_id=None, core=None, slot=None):
    with SAVE_SYNC_LOCK:
        raw_root = Path(root).expanduser()
        _reject_symlink_components(raw_root)
        root = raw_root.resolve()
        roots = [path for path in save_roots(game) if path.exists()]
        if not roots:
            if allow_empty:
                return None
            raise FileNotFoundError("No configured save paths currently exist.")
        if any(path.is_symlink() for path in roots):
            raise ValueError("Save backup paths may not be symlinks.")
        directory = game_backup_dir(game, root)
        _reject_symlink_components(directory)
        directory.parent.mkdir(parents=True, exist_ok=True)
        _reject_symlink_components(directory)
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory.parent, 0o700)
        os.chmod(directory, 0o700)
        _restrict_archive_permissions(directory)
        label = str(label or "manual").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", label):
            raise ValueError("Save backup label is invalid.")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        archive = directory / f"{stamp}-{label}.zip"
        configured_paths = game.get("save_paths") if isinstance(game.get("save_paths"), list) else []
        first_save_path = next(
            (str(path) for path in configured_paths if str(path).strip()), ""
        )
        recorded_source = save_source_for(game, first_save_path) if first_save_path else {}
        if emulator_id is None:
            emulator_id = (
                game.get("emulator_adapter_id") or game.get("emulator_id") or ""
            )
        if core is None:
            core = recorded_source.get("core", "")
        if slot is None:
            slot = recorded_source.get("slot", "")
        manifest = {
            "game": game.get("name", ""),
            "label": label,
            "emulator_id": str(emulator_id or "")[:64],
            "core": str(core or "")[:64],
            "slot": str(slot or "")[:64],
            "roots": [{"path":str(path), "file":path.is_file()} for path in roots],
        }
        manifest["entries"] = _manifest_entries(roots)

        def populate(package):
            package.writestr("manifest.json", json.dumps(manifest))
            for index, source in enumerate(roots):
                if source.is_file():
                    _write_archive_file(package, source, f"roots/{index}/{source.name}")
                else:
                    _reject_symlink_components(source)
                    for file in source.rglob("*"):
                        if file.is_symlink():
                            raise ValueError(f"Save backup source contains a symlink: {file}")
                        if file.is_file():
                            _write_archive_file(package, file, f"roots/{index}/{file.relative_to(source)}")

        _write_private_archive(archive, populate)
        return archive


def list_backups(game, root):
    raw_root = Path(root).expanduser()
    _reject_symlink_components(raw_root)
    directory = game_backup_dir(game, raw_root.resolve())
    _reject_symlink_components(directory)
    if not directory.is_dir() or directory.is_symlink():
        return []
    return sorted(
        (path for path in directory.glob("*.zip") if path.is_file() and not path.is_symlink()),
        reverse=True,
    )


def _label_from_archive_name(name):
    stem = Path(name).name
    if stem.endswith(".zip"):
        stem = stem[:-4]
    parts = stem.split("-", 3)
    return parts[3] if len(parts) == 4 and parts[3] else "manual"


def backup_metadata(game, root, backup_name):
    """Describe one backup: name, size, mtime and manifest provenance.

    The manifest is best-effort: an archive written by an older version (or
    with a corrupt manifest) still returns its file name, size and a label
    parsed from the archive name instead of failing the whole listing.
    """
    raw_root = Path(root).expanduser()
    _reject_symlink_components(raw_root)
    directory = game_backup_dir(game, raw_root.resolve())
    _reject_symlink_components(directory)
    archive = directory / Path(backup_name).name
    _reject_symlink_components(archive)
    if directory not in archive.parents or not archive.is_file():
        raise FileNotFoundError("Save backup not found.")
    info = archive.stat()
    metadata = {
        "name": archive.name,
        "size": info.st_size,
        "mtime": int(info.st_mtime),
        "label": _label_from_archive_name(archive.name),
        "emulator_id": "",
        "core": "",
        "slot": "",
    }
    try:
        with zipfile.ZipFile(archive) as package:
            manifest = _load_save_manifest(package)
    except (OSError, ValueError, zipfile.BadZipFile):
        return metadata
    if isinstance(manifest, dict):
        label = str(manifest.get("label") or "").strip()
        if label:
            metadata["label"] = label
        for key in ("emulator_id", "core", "slot"):
            value = manifest.get(key)
            if isinstance(value, str):
                metadata[key] = value
    return metadata


def list_backup_metadata(game, root):
    """Newest-first metadata for every backup of ``game``."""
    return [backup_metadata(game, root, archive.name) for archive in list_backups(game, root)]


def _manifest_entry_destination(entry, roots):
    try:
        root_info = roots[int(entry.get("root"))]
        relative = str(entry.get("relative") or "")
    except (KeyError, IndexError, TypeError, ValueError):
        return ""
    if not relative:
        return ""
    base = root_info["path"].parent if root_info["file"] else root_info["path"]
    return str(base / relative)


def preview_restore(game, root, backup_name, selected=None):
    """Describe what ``restore_saves`` would change, without writing anything.

    ``add`` are backup files absent on disk, ``overwrite`` are files present
    on both sides whose SHA-256 differs, and ``missing`` are manifest entries
    the archive no longer contains.  ``remove`` is always empty: a restore
    never deletes local files, and the key exists for wire compatibility.
    Each bucket is capped at ``PREVIEW_BUCKET_LIMIT``; ``truncated`` records
    which buckets were cut and ``counts`` has the untruncated totals.
    """
    with SAVE_SYNC_LOCK:
        raw_root = Path(root).expanduser()
        _reject_symlink_components(raw_root)
        directory = game_backup_dir(game, raw_root.resolve())
        _reject_symlink_components(directory)
        archive = directory / Path(backup_name).name
        _reject_symlink_components(archive)
        if directory not in archive.parents or not archive.is_file():
            raise FileNotFoundError("Save backup not found.")
        with zipfile.ZipFile(archive) as package:
            infos = _validate_save_archive_entries(package)
            manifest = _load_save_manifest(package)
            roots = _match_save_roots(manifest, save_roots(game))
            destinations = _compute_save_destinations(infos, roots)
            archive_names = {info.filename for info, _destination, _base in destinations}
            manifest_entries = manifest.get("entries", []) if isinstance(manifest, dict) else []
            if not isinstance(manifest_entries, list):
                manifest_entries = []
            wanted = None
            if selected is not None:
                if not isinstance(selected, list) or not selected or len(selected) > MAX_SAVE_ARCHIVE_MEMBERS:
                    raise ValueError("Select at least one save file to restore.")
                wanted = {str(name) for name in selected}
                if not wanted or not wanted <= archive_names:
                    raise ValueError("Save selection does not match the backup.")
                destinations = [item for item in destinations if item[0].filename in wanted]
            entries_by_archive = {}
            for entry in manifest_entries:
                if isinstance(entry, dict) and isinstance(entry.get("archive"), str):
                    entries_by_archive.setdefault(entry["archive"], entry)
            add = []
            overwrite = []
            unchanged = 0
            for info, destination, _base in destinations:
                entry = entries_by_archive.get(info.filename, {})
                backup_sha = entry.get("sha256") if isinstance(entry.get("sha256"), str) else ""
                if not destination.exists() or not destination.is_file():
                    add.append({
                        "archive": info.filename,
                        "path": str(destination),
                        "size": info.file_size,
                        "sha256": backup_sha,
                    })
                    continue
                disk_sha = file_sha256(destination)
                if backup_sha and disk_sha == backup_sha:
                    unchanged += 1
                    continue
                overwrite.append({
                    "archive": info.filename,
                    "path": str(destination),
                    "backup_sha": backup_sha,
                    "disk_sha": disk_sha,
                    "size": info.file_size,
                })
            missing = []
            for entry in manifest_entries:
                member = entry.get("archive") if isinstance(entry, dict) else None
                if not isinstance(member, str) or member in archive_names:
                    continue
                if wanted is not None and member not in wanted:
                    continue
                missing.append({
                    "archive": member,
                    "path": _manifest_entry_destination(entry, roots),
                })
            counts = {
                "add": len(add),
                "overwrite": len(overwrite),
                "remove": 0,
                "missing": len(missing),
                "unchanged": unchanged,
            }
            return {
                "archive": archive.name,
                "game": manifest.get("game", "") if isinstance(manifest, dict) else "",
                "add": add[:PREVIEW_BUCKET_LIMIT],
                "overwrite": overwrite[:PREVIEW_BUCKET_LIMIT],
                "remove": [],
                "missing": missing[:PREVIEW_BUCKET_LIMIT],
                "conflict": bool(overwrite),
                "unchanged": unchanged,
                "counts": counts,
                "truncated": {
                    "add": counts["add"] > PREVIEW_BUCKET_LIMIT,
                    "overwrite": counts["overwrite"] > PREVIEW_BUCKET_LIMIT,
                    "remove": False,
                    "missing": counts["missing"] > PREVIEW_BUCKET_LIMIT,
                },
            }


def _validate_save_archive_entries(package):
    infos = package.infolist()
    if len(infos) > MAX_SAVE_ARCHIVE_MEMBERS:
        raise ValueError("Save backup contains too many files.")
    total_bytes = 0
    seen = set()
    for info in infos:
        name = info.filename.replace("\\", "/")
        if not name or "\x00" in name or Path(name).is_absolute() or ".." in Path(name).parts or name in seen:
            raise ValueError("Save backup contains an unsafe path.")
        seen.add(name)
        if info.file_size > MAX_SAVE_ARCHIVE_MEMBER_BYTES:
            raise ValueError("Save backup member is too large.")
        total_bytes += info.file_size
        if total_bytes > MAX_SAVE_ARCHIVE_TOTAL_BYTES:
            raise ValueError("Save backup expands beyond the allowed size.")
    return infos


def _load_save_manifest(package):
    try:
        with package.open("manifest.json") as source:
            manifest_bytes = source.read(1024 * 1024 + 1)
        if len(manifest_bytes) > 1024 * 1024:
            raise ValueError("Save backup manifest is too large.")
        manifest = json.loads(manifest_bytes)
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Save backup manifest is invalid.") from error
    return manifest


def _match_save_roots(manifest, configured):
    saved_roots = manifest.get("roots", []) if isinstance(manifest, dict) else []
    # backup_saves records only the configured roots that existed at backup
    # time, so the manifest is matched by path as a subset of the configured
    # roots, in manifest order (archive member indices follow that order).
    if not isinstance(saved_roots, list) or not saved_roots or len(saved_roots) > len(configured):
        raise ValueError("Save backup roots do not match this game.")
    roots = []
    seen = set()
    for item in saved_roots:
        if not isinstance(item, dict):
            raise ValueError("Save backup roots do not match this game.")
        saved_path = Path(str(item.get("path", ""))).expanduser()
        if saved_path not in configured or saved_path in seen:
            raise ValueError("Save backup roots do not match this game.")
        seen.add(saved_path)
        # The manifest records the root's type at backup time.  A missing root
        # is restored as that recorded type; a live type swap is still unsafe.
        expected_file = bool(item.get("file"))
        current = saved_path
        if current.exists() and current.is_file() != expected_file:
            raise ValueError("Save backup root type does not match this game.")
        _reject_symlink_components(current)
        roots.append({"path": current, "file": expected_file})
    return roots


def _compute_save_destinations(infos, roots):
    destinations = []
    for info in infos:
        name = info.filename.replace("\\", "/")
        if info.is_dir() or not name.startswith("roots/"):
            continue
        parts = Path(name).parts
        if len(parts) < 3:
            continue
        try:
            index = int(parts[1])
        except ValueError as error:
            raise ValueError("Invalid save backup manifest.") from error
        if index < 0 or index >= len(roots):
            raise ValueError("Invalid save backup manifest.")
        root_info = roots[index]
        base = (root_info["path"].parent if root_info["file"] else root_info["path"]).resolve()
        _reject_symlink_components(root_info["path"])
        _reject_symlink_components(base)
        destination = (base / Path(*parts[2:])).resolve()
        if destination != base and base not in destination.parents:
            raise ValueError("Save backup contains an unsafe path.")
        raw_destination = base / Path(*parts[2:])
        if raw_destination.is_symlink() or any(parent.is_symlink() for parent in raw_destination.parents if parent != base):
            raise ValueError("Save restore destination contains a symlink.")
        destinations.append((info, destination, base))
    return destinations


def _write_save_restores(package, destinations):
    for info, destination, base in destinations:
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Re-validate after mkdir: a planted symlink must not redirect the restore write.
        resolved = destination.resolve()
        if resolved != base and base not in resolved.parents:
            raise ValueError("Save backup contains an unsafe path.")
        if resolved.is_symlink() or any(parent.is_symlink() for parent in resolved.parents if parent != base):
            raise ValueError("Save restore destination contains a symlink.")
        with package.open(info) as source:
            atomic_copy_stream(source, destination, mode=0o600, max_bytes=MAX_SAVE_ARCHIVE_MEMBER_BYTES)


def inspect_save_backup(game, root, backup_name):
    raw_root = Path(root).expanduser()
    _reject_symlink_components(raw_root)
    directory = game_backup_dir(game, raw_root.resolve())
    _reject_symlink_components(directory)
    archive = directory / Path(backup_name).name
    _reject_symlink_components(archive)
    if directory not in archive.parents or not archive.is_file():
        raise FileNotFoundError("Save backup not found.")
    with zipfile.ZipFile(archive) as package:
        infos = _validate_save_archive_entries(package)
        manifest = _load_save_manifest(package)
        configured = save_roots(game)
        roots = _match_save_roots(manifest, configured)
        destinations = _compute_save_destinations(infos, roots)
        entries = []
        for info, destination, _base in destinations:
            entries.append({"archive": info.filename, "path": str(destination), "size": info.file_size})
        return {"archive": archive.name, "game": manifest.get("game", ""), "files": entries, "manifest": manifest}


def restore_saves(game, root, backup_name, selected=None, dry_run=False):
    with SAVE_SYNC_LOCK:
        raw_root = Path(root).expanduser()
        _reject_symlink_components(raw_root)
        root = raw_root.resolve()
        directory = game_backup_dir(game, root)
        _reject_symlink_components(directory)
        archive = directory / Path(backup_name).name
        _reject_symlink_components(archive)
        if directory not in archive.parents or not archive.is_file():
            raise FileNotFoundError("Save backup not found.")
        with zipfile.ZipFile(archive) as package:
            infos = _validate_save_archive_entries(package)
            manifest = _load_save_manifest(package)
            configured = save_roots(game)
            roots = _match_save_roots(manifest, configured)
            destinations = _compute_save_destinations(infos, roots)
            if selected is not None:
                if not isinstance(selected, list) or not selected or len(selected) > MAX_SAVE_ARCHIVE_MEMBERS:
                    raise ValueError("Select at least one save file to restore.")
                available = {info.filename for info, _destination, _base in destinations}
                wanted = {str(name) for name in selected}
                if not wanted or not wanted <= available:
                    raise ValueError("Save selection does not match the backup.")
                destinations = [item for item in destinations if item[0].filename in wanted]
            if dry_run:
                return {"archive": archive.name, "dry_run": True, "files": [{"archive": info.filename, "path": str(destination)} for info, destination, _base in destinations]}
            # A missing save tree is exactly when a restore is needed; the
            # safety backup is skipped instead of aborting the restore.
            configured_paths = game.get("save_paths") if isinstance(game.get("save_paths"), list) else []
            first_path = next((str(path) for path in configured_paths if str(path).strip()), "")
            source = save_source_for(game, first_path) if first_path else {}
            backup_saves(game, root, "before-restore", allow_empty=True,
                         emulator_id=source.get("emulator_id") or None,
                         core=source.get("core") or None,
                         slot=source.get("slot") or None)
            _write_save_restores(package, destinations)
        return archive
