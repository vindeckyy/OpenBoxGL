"""Record That routes: replay-buffer clips, gallery listing, and reels (T3)."""

from __future__ import annotations

import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

import openbox
from api_errors import BadRequest
from parity_integrations import capture_screenshot, obs_recording_directory
from pkg.parity.parity_obs_bridge import replay_enabled, save_replay_buffer
from pkg.parity.parity_reels import build_reel_manifest, create_reel
from pkg.state.media_probe import approved_media_path
from routes.registry import route
from webapp_state import JOB_MANAGER, bump_media_epoch, game_from_payload, load_state_view, transact_state


MAX_CLIPS_PER_GAME = 100
MAX_CLIP_BYTES = 2 * 1024 * 1024 * 1024
CLIP_EXTENSIONS = frozenset({".avi", ".flv", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".webm", ".png", ".jpg", ".jpeg", ".webp"})


def _game(state, payload):
    try:
        return game_from_payload(state, payload if isinstance(payload, dict) else {})
    except (IndexError, ValueError, TypeError) as error:
        raise BadRequest("Game not found.", code="GAME_NOT_FOUND") from error


def _stable_id(game):
    value = str(game.get("game_id") or "").strip()
    if not value:
        raise BadRequest("Game has no stable ID.", code="GAME_NOT_FOUND")
    return value


def _saved_filename(value, depth=0):
    if depth > 4:
        return ""
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    for key in ("savedFilename", "saved_filename", "filename", "path", "file"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    for candidate in value.values():
        found = _saved_filename(candidate, depth + 1)
        if found:
            return found
    return ""


def _path_has_symlink_component(path):
    current = Path(path)
    while True:
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
        if current.parent == current:
            return False
        current = current.parent


def _recording_roots(settings):
    values = []
    configured = settings.get("obs_recording_path") if isinstance(settings, dict) else None
    if configured:
        values.append(configured)
    try:
        values.append(obs_recording_directory())
    except (OSError, RuntimeError, ValueError):
        pass
    roots = []
    for value in values:
        try:
            root = Path(value).expanduser()
            if not root.is_absolute() or _path_has_symlink_component(root):
                continue
            root = root.resolve(strict=False)
            if root.is_dir() and root not in roots:
                roots.append(root)
        except (OSError, RuntimeError, ValueError):
            continue
    return roots


def _source_clip_path(value, allowed_roots=None):
    text = str(value or "").strip()
    if not text or "\x00" in text:
        return None
    source = Path(text).expanduser()
    try:
        if not source.is_absolute() or _path_has_symlink_component(source) or not source.is_file():
            return None
        if source.suffix.casefold() not in CLIP_EXTENSIONS:
            return None
        if source.stat().st_size > MAX_CLIP_BYTES:
            return None
        resolved = source.resolve()
        if allowed_roots is not None:
            if not any(resolved != root and root in resolved.parents for root in allowed_roots):
                return None
        return resolved
    except (OSError, RuntimeError, ValueError):
        return None


def _destination(stable_id, suffix):
    safe_id = "".join(character if character.isalnum() or character in "-_" else "_" for character in stable_id)[:160] or "game"
    media_root = openbox.DATA.parent / "media"
    if _path_has_symlink_component(media_root):
        raise OSError("The OpenBox media directory contains a symlink.")
    media_root.mkdir(parents=True, exist_ok=True)
    media_root = media_root.resolve()
    clips_root = media_root / "clips"
    if _path_has_symlink_component(clips_root):
        raise OSError("The OpenBox clips directory contains a symlink.")
    clips_root.mkdir(exist_ok=True)
    if _path_has_symlink_component(clips_root):
        raise OSError("The OpenBox clips directory contains a symlink.")
    root = clips_root / safe_id
    if _path_has_symlink_component(root):
        raise OSError("The OpenBox clip directory contains a symlink.")
    root.mkdir(exist_ok=True)
    if _path_has_symlink_component(root):
        raise OSError("The OpenBox clip directory contains a symlink.")
    root.resolve().relative_to(media_root)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return root / f"{stamp}-{uuid.uuid4().hex[:12]}{suffix}"


def _copy_clip(source, stable_id, *, settings=None):
    allowed_roots = _recording_roots(settings) if settings is not None else None
    source_path = _source_clip_path(source, allowed_roots=allowed_roots)
    if source_path is None:
        return None
    destination = None
    try:
        destination = _destination(stable_id, source_path.suffix.casefold() or ".mp4")
        shutil.copy2(source_path, destination)
        return str(destination.resolve())
    except OSError:
        try:
            if destination is not None:
                destination.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def _capture_screenshot_clip(stable_id):
    try:
        destination = _destination(stable_id, ".png")
        return str(capture_screenshot(destination))
    except (OSError, RuntimeError, ValueError):
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def _public_clip(value):
    if not isinstance(value, dict):
        return None
    path = str(value.get("path") or "").strip()
    if not path:
        return None
    try:
        approved = approved_media_path(Path(path).expanduser())
    except (OSError, RuntimeError, ValueError):
        return None
    if approved is None:
        return None
    result = {"path": str(approved)}
    for key in ("clip_id", "created_at", "launch_id", "title", "source", "fallback"):
        if value.get(key) not in (None, ""):
            result[key] = str(value[key]) if key != "fallback" else bool(value[key])
    return result


def _clips_for_game(game):
    values = game.get("clips", []) if isinstance(game, dict) else []
    if not isinstance(values, list):
        return []
    return [item for value in values[-MAX_CLIPS_PER_GAME:] if (item := _public_clip(value)) is not None]


@route("GET", "/api/v2/clips", spec="handlers.clips.clips_list")
def clips_list(handler, parsed):
    query = parse_qs(parsed.query or "")
    state = load_state_view()
    game = _game(state, {"game_id": (query.get("game_id", [""])[0] or query.get("id", [""])[0])})
    clips = _clips_for_game(game)
    handler.send_json(200, {"game_id": _stable_id(game), "clips": clips, "total": len(clips)})


@route("POST", "/api/v2/clips/capture", spec="handlers.clips.capture_clip")
def capture_clip(handler, payload):
    state = load_state_view()
    game = _game(state, payload)
    stable_id = _stable_id(game)
    settings = state.get("settings", {}) if isinstance(state, dict) else {}
    obs_result = save_replay_buffer(settings) if replay_enabled(settings) else None
    source = _saved_filename(obs_result)
    clip_path = _copy_clip(source, stable_id, settings=settings)
    fallback = False
    if not clip_path:
        clip_path = _capture_screenshot_clip(stable_id)
        fallback = True
    if not clip_path:
        raise BadRequest(
            "OBS replay is unavailable and a screenshot could not be captured.",
            code="CLIP_UNAVAILABLE",
        )
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    clip = {
        "clip_id": f"clip-{uuid.uuid4().hex}",
        "path": clip_path,
        "created_at": now,
        "launch_id": str((payload or {}).get("launch_id") or (payload or {}).get("launchId") or ""),
        "title": "Screenshot fallback" if fallback else "Replay clip",
        "source": "screenshot" if fallback else "obs_replay",
        "fallback": fallback,
    }

    def mutate(current):
        target = _game(current, {"game_id": stable_id})
        clips = target.get("clips")
        if not isinstance(clips, list):
            clips = []
            target["clips"] = clips
        clips.append(clip)
        del clips[:-MAX_CLIPS_PER_GAME]
        # Keep the existing active-video path useful to the media manager while
        # the gallery remains the canonical multi-clip collection.
        if not fallback:
            target["video_recording"] = clip_path
        return clip

    try:
        committed = transact_state(mutate)[1]
    except (IndexError, ValueError, TypeError) as error:
        raise BadRequest("Game disappeared while saving the clip.", code="GAME_NOT_FOUND") from error
    bump_media_epoch()
    handler.send_json(200, {
        "game_id": stable_id,
        "clip": _public_clip(committed),
        "fallback": fallback,
        "obs": not fallback,
    })


@route("GET", "/api/v2/reels", spec="handlers.clips.reel_manifest")
def reel_manifest(handler, parsed):
    query = parse_qs(parsed.query or "")
    state = load_state_view()
    game = _game(state, {"game_id": (query.get("game_id", [""])[0] or query.get("id", [""])[0])})
    manifest = build_reel_manifest(game=game)
    handler.send_json(200, manifest)


@route("POST", "/api/v2/reels/create", spec="handlers.clips.create_reel_job")
def create_reel_job(handler, payload):
    state = load_state_view()
    game = _game(state, payload)
    stable_id = _stable_id(game)
    snapshot = dict(game)
    year = payload.get("year") if isinstance(payload, dict) else None

    def worker(_cancel_event=None):
        result = create_reel(game=snapshot, output_root=openbox.DATA.parent, year=year)
        return {"game_id": stable_id, **result}

    job = JOB_MANAGER.submit(f"reel:{stable_id}", worker)
    handler.send_json(202, {"state": job.get("state", "queued"), "job_id": job.get("job_id", ""), "game_id": stable_id})


__all__ = ["clips_list", "capture_clip", "reel_manifest", "create_reel_job"]
