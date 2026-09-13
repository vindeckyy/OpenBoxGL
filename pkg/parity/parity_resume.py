"""Quick Resume & Moments core plumbing (T1).

Captures and replays emulator save-states for adapters that declare a
``state:`` block in ``emulator_defs/*.yaml`` (the M0a capability matrix).
States live under ``<data_parent>/resume_states/<stable-key>/`` with a
``state.json`` meta file stamped with the adapter id and an emulator version
fingerprint so upgrades flag stale states instead of silently loading them.

Conventions:
- RetroArch-family defs inject ``--appendconfig <state_dir>/openbox-session.cfg``
  (auto-save into ``state_dir``, command net enabled, user config untouched).
- MAME injects ``-autosave -state_directory {state_dir} -state {state_name}``
  and keeps the canonical file named ``openbox-resume`` per machine dir.
- Partial adapters (``adapter-cli`` without ``capture``) resume from a file
  handed into the state dir; ``{state_path}`` templates canonicalize the
  newest drop to ``resume<ext>`` while slot-managed defs (ScummVM) keep
  emulator-chosen names.
- Moment snapshots from T1-ui land in ``<state_dir>/moments/`` and are never
  adopted as the resume state by this module.
- A clean Start never deletes the stored state; a fresh capture only
  overwrites the canonical file after archiving the old one (bounded by the
  ``state_retention`` setting).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pkg.parity.launch_tokens import apply_tokens  # noqa: E402
from pkg.parity.parity_emulator_defs import (  # noqa: E402
    STATE_KINDS,
    detect_adapter_for_platform,
    detect_adapter_prefix,
    find_adapter,
)

LOGGER = logging.getLogger("openbox")

RESUME_DIRNAME = "resume_states"
STATE_META = "state.json"
RA_CONFIG_NAME = "openbox-session.cfg"
RESUME_BASENAME = "resume"
RESUME_SLOT = "openbox-resume"
CAPTURE_SLOT = "openbox-session"
ARCHIVE_DIRNAME = "archive"
MOMENTS_DIRNAME = "moments"
ARCHIVE_LIMIT = 20
_STATE_SUFFIXES = (".state.auto", ".state")


def resume_root(data_parent):
    """Return ``<data_parent>/resume_states`` (sibling of the library JSON)."""
    return Path(data_parent) / RESUME_DIRNAME


def state_key(game):
    """Stable per-game directory key: game_id sanitized, else a content hash."""
    stable = str(game.get("game_id") or "").strip()
    if stable:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", stable)
        if safe in {"", ".", ".."}:
            safe = "game"
        # Keep readable keys for ordinary IDs, but append a digest whenever
        # sanitization or truncation could make two IDs share a directory.
        if safe != stable or len(safe) > 80:
            digest = hashlib.sha1(stable.encode("utf-8")).hexdigest()[:12]
            safe = f"{safe[:80 - len(digest) - 1]}-{digest}"
        return safe
    digest = hashlib.sha1(
        f"{game.get('name', '')}|{game.get('path', '')}".encode("utf-8")
    ).hexdigest()
    return digest[:16]


def state_dir_for(game, data_parent):
    root = resume_root(data_parent)
    candidate = root / state_key(game)
    try:
        if candidate.is_symlink():
            raise ValueError("Resume state directory must not be a symlink.")
        resolved_root = root.resolve()
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ValueError("Could not resolve resume state directory.") from error
    if resolved == resolved_root or resolved_root not in resolved.parents:
        raise ValueError("Resume state directory escapes the OpenBox data root.")
    return candidate


def adapter_state(adapter):
    """Tolerant accessor for a normalized adapter's ``state`` block."""
    empty = {"kind": "none", "template": [], "capture": [], "glob": []}
    if not isinstance(adapter, dict):
        return empty
    state = adapter.get("state")
    if not isinstance(state, dict):
        return empty
    kind = state.get("kind")
    return {
        "kind": kind if kind in STATE_KINDS else "none",
        "template": [str(item) for item in (state.get("template") or [])],
        "capture": [str(item) for item in (state.get("capture") or [])],
        "glob": [str(item) for item in (state.get("glob") or [])],
    }


def adapter_for_launch(game, profiles=None, *, which=None):
    """Return ``(adapter, precedence)`` mirroring ``resolve_launch``.

    Only the ``game_adapter`` and ``registry_adapter`` precedence levels carry
    a ``state:`` schema; per-game commands, platform profiles and direct
    executables bypass it entirely and return ``(None, <level>)``.
    """
    which = which or shutil.which
    profiles = profiles or {}
    if str(game.get("launch", "") or "").strip():
        return None, "game_launch"
    adapter = find_adapter(game.get("emulator_adapter_id", ""), game.get("emulator_id", ""))
    if adapter and detect_adapter_prefix(adapter, which=which):
        return adapter, "game_adapter"
    platform = str(game.get("platform", "") or "")
    if str(profiles.get(platform, "") or "").strip():
        return None, "platform_profile"
    detected = detect_adapter_for_platform(platform, which=which)
    if detected:
        return detected, "registry_adapter"
    return None, "direct_exe"


def _flatpak_version(app_id):
    """Best-effort ``flatpak info --show-version`` for fingerprinting."""
    try:
        result = subprocess.run(
            ["flatpak", "info", "--show-version", str(app_id)],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    version = result.stdout.strip()
    return version or None


def emulator_fingerprint(adapter, *, which=None):
    """Version stamp for stale detection, including the selected libretro core."""
    which = which or shutil.which
    prefix = detect_adapter_prefix(adapter, which=which) if adapter else []
    if not prefix:
        return "uninstalled"
    core = str((adapter or {}).get("core_path") or "").strip()
    if not core:
        startup_args = (adapter or {}).get("startup_args") or []
        try:
            index = list(startup_args).index("-L")
            candidate = str(startup_args[index + 1] or "")
            if candidate.startswith("/"):
                core = candidate
        except (ValueError, IndexError, TypeError):
            core = ""

    def file_stamp(path):
        if not path:
            return ""
        try:
            stat = os.stat(path)
            return f"{path}:{stat.st_mtime_ns}:{stat.st_size}"
        except OSError:
            return f"{path}:unknown"

    core_stamp = f"|core:{file_stamp(core)}" if core else ""
    if prefix[0] == "flatpak":
        app_id = prefix[-1] if len(prefix) > 1 else ""
        return f"flatpak:{app_id}:{_flatpak_version(app_id) or 'unknown'}{core_stamp}"
    exe = prefix[0]
    return f"{file_stamp(exe)}{core_stamp}"


def load_meta(game, data_parent):
    """Read ``state.json`` for a game; missing/corrupt files read as absent."""
    state_dir = state_dir_for(game, data_parent)
    path = _safe_state_path(state_dir, state_dir / STATE_META)
    if path is None:
        return None
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return meta if isinstance(meta, dict) else None


def is_stale(meta, adapter, *, which=None):
    """True when the stored adapter id or emulator fingerprint drifted."""
    if not meta:
        return False
    if str(meta.get("adapter_id") or "") != str(adapter.get("adapter_id") or ""):
        return True
    stored = str(meta.get("emulator_version") or "")
    return stored != emulator_fingerprint(adapter, which=which)


def _write_ra_session_config(state_dir):
    """Write the per-session RetroArch appendconfig inside the state dir.

    Never touches the user's real RetroArch config: the file is injected via
    ``--appendconfig`` so auto-save on exit writes into ``state_dir``.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Auto-generated by OpenBox Quick Resume. Do not edit; rewritten per launch.",
        'savestate_auto_save = "true"',
        'savestate_auto_load = "false"',
        f'savestate_directory = "{state_dir}"',
        'savestate_thumbnail_enable = "true"',
        'sort_savestates_enable = "false"',
        'state_slot = "0"',
        'network_cmd_enable = "true"',
        "",
    ]
    path = _safe_state_path(state_dir, state_dir / RA_CONFIG_NAME)
    if path is None:
        raise OSError("Refusing to write a resume config outside the state directory.")
    _write_state_text(path, "\n".join(lines))
    return path


def _render_state_args(game, raw_args, state_dir, *, state_path="", state_name="", state_config=""):
    rendered = []
    for raw in raw_args:
        rendered.append(apply_tokens(
            str(raw), game,
            path=str(game.get("path", "") or ""),
            state_path=str(state_path),
            state_dir=str(state_dir),
            state_config=str(state_config),
            state_name=str(state_name),
        ))
    return rendered


def _needs_config(raw_args):
    return any("{state_config}" in str(raw) for raw in raw_args)


def _safe_state_file(state_dir, relative):
    """Resolve a state filename without following links or escaping its dir."""
    if not isinstance(relative, str) or not relative.strip():
        return None
    raw = Path(relative)
    if raw.is_absolute() or any(part in {"", ".", ".."} for part in raw.parts):
        return None
    try:
        current = state_dir
        for part in raw.parts:
            current = current / part
            if current.is_symlink():
                return None
        resolved_dir = state_dir.resolve()
        candidate = current.resolve(strict=False)
        candidate.relative_to(resolved_dir)
        if not candidate.is_file():
            return None
        return candidate
    except (OSError, RuntimeError, ValueError):
        return None


def _resolve_resume_file(game, adapter, *, data_parent, resume, allow_stale, which):
    meta = load_meta(game, data_parent)
    if not meta:
        raise FileNotFoundError("No captured state exists for this game.")
    if is_stale(meta, adapter, which=which) and not allow_stale:
        raise ValueError(
            "The captured state was written by a different adapter or emulator build."
        )
    sdir = state_dir_for(game, data_parent)
    rel = resume if isinstance(resume, str) and resume not in ("", "latest") else str(meta.get("file") or "")
    candidate = _safe_state_file(sdir, rel)
    if candidate is None:
        raise FileNotFoundError("The captured state file is missing.")
    return candidate


def inject_resume_args(game, args, *, profiles=None, settings=None, resume=False, data_parent, allow_stale=False, which=None):
    """Append capture args on clean starts or state args on resume.

    ``resume`` is ``False`` (normal start, arm capture only), ``True`` (load
    the newest captured state), or a file name inside the state dir.
    Unsupported launches and ``state: {kind: none}`` adapters pass argv
    through untouched so Resume affordances never appear for them.
    """
    args = [str(part) for part in args]
    settings = settings or {}
    if not settings.get("quick_resume_enabled", True):
        if resume:
            raise ValueError("Quick Resume is disabled in Settings.")
        return args
    adapter, _precedence = adapter_for_launch(game, profiles, which=which)
    state = adapter_state(adapter)
    if adapter is None or state["kind"] == "none":
        if resume:
            raise ValueError("No state-capable adapter is configured for this game.")
        return args
    sdir = state_dir_for(game, data_parent)
    if resume:
        target = _resolve_resume_file(
            game, adapter, data_parent=data_parent, resume=resume,
            allow_stale=allow_stale, which=which,
        )
        config = _write_ra_session_config(sdir) if _needs_config(state["template"]) else ""
        return args + _render_state_args(
            game, state["template"], sdir,
            state_path=target, state_name=RESUME_SLOT, state_config=config,
        )
    try:
        config = _write_ra_session_config(sdir) if _needs_config(state["capture"]) else ""
        return args + _render_state_args(
            game, state["capture"], sdir,
            state_name=CAPTURE_SLOT, state_config=config,
        )
    except OSError:
        LOGGER.warning("Could not arm resume-state capture for %s", game.get("name", "game"))
        return args


def _state_suffix(picked):
    name = picked.name
    for compound in _STATE_SUFFIXES:
        if name.endswith(compound):
            # "<rom>.state.auto" is the emulator's auto-save marker; the
            # canonical resume file drops it down to plain ".state".
            return ".state" if compound == ".state.auto" else compound
    return picked.suffix or ".state"


def _state_files(state_dir, state):
    """Candidate save files, newest anywhere except archive/moments/meta."""
    if not state_dir.is_dir():
        return []
    patterns = state["glob"] or ["*"]
    seen = set()
    files = []
    for pattern in patterns:
        try:
            matches = state_dir.glob(pattern)
        except (OSError, ValueError):
            continue
        for path in matches:
            if path in seen or path.is_symlink() or not path.is_file():
                continue
            if path.name in {STATE_META, RA_CONFIG_NAME} or path.name.startswith("thumb."):
                continue
            if path.parent.name in {ARCHIVE_DIRNAME, MOMENTS_DIRNAME}:
                continue
            seen.add(path)
            files.append(path)
    return files


def _safe_state_path(state_dir, path):
    """Return a state path only when it has no links and stays in ``state_dir``."""
    state_dir = Path(state_dir)
    path = Path(path)
    try:
        for candidate in (state_dir, path):
            current = Path(os.path.abspath(os.fspath(candidate)))
            while True:
                if current.is_symlink():
                    return None
                if current.parent == current:
                    break
                current = current.parent
        resolved_dir = state_dir.resolve(strict=False)
        resolved = path.resolve(strict=False)
        resolved.relative_to(resolved_dir)
        if resolved == resolved_dir:
            return None
        return resolved
    except (OSError, RuntimeError, ValueError):
        return None


def _write_state_text(path, text):
    """Write one state-local text file without following its final symlink."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
    except OSError:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _archive_file(path, state_dir):
    try:
        archive_path = Path(state_dir) / ARCHIVE_DIRNAME
        source = _safe_state_path(state_dir, path)
        archive = _safe_state_path(state_dir, archive_path)
        if source is None or archive is None or not source.is_file():
            return False
        archive.mkdir(exist_ok=True)
        archive = _safe_state_path(state_dir, archive_path)
        if archive is None:
            return False
        stamp = int(source.stat().st_mtime_ns)
        target = _safe_state_path(state_dir, archive / f"{stamp}-{source.name}")
        if target is None:
            return False
        source.replace(target)
        return True
    except OSError:
        return False


def _canonicalize(picked, state_dir, state):
    """Move the newest capture to the canonical resume name, or leave in place."""
    template = " ".join(state["template"])
    if "{state_name}" in template:
        canonical_name = f"{RESUME_SLOT}{_state_suffix(picked)}"
    elif "{state_path}" in template:
        canonical_name = f"{RESUME_BASENAME}{_state_suffix(picked)}"
    else:
        # Slot-managed saves (ScummVM) keep their emulator-chosen names.
        return str(picked.relative_to(state_dir))
    canonical = picked.parent / canonical_name
    if picked != canonical:
        try:
            if canonical.exists():
                if not _archive_file(canonical, state_dir):
                    return str(picked.relative_to(state_dir))
            picked.replace(canonical)
        except OSError:
            return str(picked.relative_to(state_dir))
    return str(canonical.relative_to(state_dir))


def _select_thumbnail(state_dir, game, data_parent, picked):
    """Prefer the emulator's own state thumbnail, else copy game media."""
    try:
        picked_stem = picked.name.split(".state")[0].rsplit(".", 1)[0]
        named = state_dir / f"{picked_stem}.png"
        if named.is_file():
            return named.name
        pngs = sorted(
            (p for p in state_dir.glob("*.png") if p.is_file() and not p.name.startswith("thumb.")),
            key=lambda p: p.stat().st_mtime_ns,
            reverse=True,
        )
        if pngs:
            return pngs[0].name
    except OSError:
        pass
    for key in ("screenshot", "cover", "background", "thumbnail"):
        media = str(game.get(key) or "").strip()
        if not media:
            continue
        source = Path(media)
        if not source.is_absolute():
            source = Path(data_parent) / media
        if not source.is_file():
            continue
        target = state_dir / f"thumb{source.suffix or '.png'}"
        try:
            shutil.copyfile(source, target)
        except OSError:
            return ""
        return target.name
    return ""


def _enforce_retention(state_dir, settings):
    try:
        retention = int(settings.get("state_retention", 1) or 1)
    except (TypeError, ValueError):
        retention = 1
    retention = max(1, min(ARCHIVE_LIMIT, retention))
    archive = _safe_state_path(state_dir, Path(state_dir) / ARCHIVE_DIRNAME)
    if archive is None or not archive.is_dir():
        return
    try:
        files = []
        for path in archive.iterdir():
            safe_path = _safe_state_path(state_dir, path)
            if safe_path is not None and safe_path.is_file():
                files.append(safe_path)
        files.sort(key=lambda p: p.stat().st_mtime_ns, reverse=True)
    except OSError:
        return
    for stale_path in files[max(0, retention - 1):]:
        try:
            safe_path = _safe_state_path(state_dir, stale_path)
            if safe_path is not None:
                safe_path.unlink()
        except OSError:
            pass


def collect_resume_state(
    game,
    settings=None,
    *,
    profiles=None,
    data_parent,
    launch_id="",
    started_at=None,
    which=None,
):
    """Adopt the newest state file in the game's dir after a session ends.

    Returns the written meta dict, or ``None`` when the feature is disabled,
    the adapter cannot hold state, or no file was captured.
    """
    settings = settings or {}
    if not settings.get("quick_resume_enabled", True):
        return None
    adapter, _precedence = adapter_for_launch(game, profiles, which=which)
    state = adapter_state(adapter)
    if adapter is None or state["kind"] == "none":
        return None
    sdir = state_dir_for(game, data_parent)
    candidates = _state_files(sdir, state)
    if started_at is not None:
        try:
            started_timestamp = (
                started_at.timestamp() if hasattr(started_at, "timestamp")
                else float(started_at)
            )
        except (TypeError, ValueError, OverflowError, OSError):
            started_timestamp = None
        if started_timestamp is not None:
            fresh = []
            for path in candidates:
                try:
                    if path.stat().st_mtime > started_timestamp:
                        fresh.append(path)
                except OSError:
                    continue
            candidates = fresh
    if not candidates:
        return None
    try:
        picked = max(candidates, key=lambda p: p.stat().st_mtime_ns)
    except OSError:
        return None
    canonical_rel = _canonicalize(picked, sdir, state)
    canonical = sdir / canonical_rel
    try:
        captured_at = datetime.fromtimestamp(canonical.stat().st_mtime).isoformat(timespec="seconds")
        size = canonical.stat().st_size
    except OSError:
        captured_at = datetime.now().isoformat(timespec="seconds")
        size = 0
    meta = {
        "adapter_id": str(adapter.get("adapter_id") or ""),
        "emulator_version": emulator_fingerprint(adapter, which=which),
        "kind": state["kind"],
        "capture_id": f"capture-{secrets.token_hex(12)}",
        "file": canonical_rel,
        "captured_at": captured_at,
        "size": size,
        "thumbnail": _select_thumbnail(sdir, game, data_parent, canonical),
        "launch_id": str(launch_id or ""),
    }
    try:
        sdir.mkdir(parents=True, exist_ok=True)
        meta_path = _safe_state_path(sdir, sdir / STATE_META)
        if meta_path is None:
            return None
        _write_state_text(meta_path, json.dumps(meta, indent=2))
    except OSError:
        return None
    _enforce_retention(sdir, settings)
    return meta


def resume_status(game, settings=None, *, profiles=None, data_parent, which=None):
    """Snapshot of resume capability and the stored state for one game."""
    settings = settings or {}
    adapter, precedence = adapter_for_launch(game, profiles, which=which)
    state = adapter_state(adapter)
    status = {
        "game_id": str(game.get("game_id") or ""),
        "enabled": bool(settings.get("quick_resume_enabled", True)),
        "capable": False,
        "kind": state["kind"],
        "capture": bool(state["capture"]) or state["kind"] == "retroarch",
        "adapter_id": str(adapter.get("adapter_id") or "") if adapter else "",
        "precedence": precedence,
        "available": False,
        "stale": False,
        "adapter_changed": False,
        "state": None,
        "reason": "ok",
    }
    path = str(game.get("path") or "").strip()
    if game.get("shelf") or game.get("manual_entry") or not path or not Path(path).exists():
        status["reason"] = "missing-path"
        return status
    if adapter is None or state["kind"] == "none":
        status["reason"] = "unsupported-adapter"
        return status
    status["capable"] = True
    meta = load_meta(game, data_parent)
    if not meta:
        return status
    file_rel = str(meta.get("file") or "")
    target = _safe_state_file(state_dir_for(game, data_parent), file_rel)
    if target is None:
        return status
    status["available"] = True
    status["adapter_changed"] = str(meta.get("adapter_id") or "") != str(adapter.get("adapter_id") or "")
    status["stale"] = is_stale(meta, adapter, which=which)
    status["state"] = {
        "file": file_rel,
        "capture_id": str(meta.get("capture_id") or ""),
        "captured_at": str(meta.get("captured_at") or ""),
        "emulator_version": str(meta.get("emulator_version") or ""),
        "adapter_id": str(meta.get("adapter_id") or ""),
        "thumbnail": str(meta.get("thumbnail") or ""),
        "size": meta.get("size") if meta.get("size") is not None else target.stat().st_size,
        "launch_id": str(meta.get("launch_id") or ""),
    }
    return status


def discard_resume_state(game, data_parent):
    """Delete the stored resume state + meta; returns True when something was removed."""
    sdir = state_dir_for(game, data_parent)
    removed = False
    meta = load_meta(game, data_parent)
    if meta:
        rel = str(meta.get("file") or "")
        if rel:
            candidate = _safe_state_file(sdir, rel)
            if candidate is not None:
                try:
                    candidate.unlink()
                    removed = True
                except OSError:
                    pass
    meta_path = sdir / STATE_META
    try:
        if meta_path.exists():
            meta_path.unlink()
            removed = True
    except OSError:
        pass
    return removed
