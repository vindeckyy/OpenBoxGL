"""Deterministic highlight-reel manifests and optional ffmpeg rendering.

Reels are deliberately local and best-effort.  A manifest is made only from
existing local files, has no wall-clock fields, and is sorted independently of
filesystem traversal order.  If ffmpeg is absent or fails, the same manifest
is rendered as a small scrollable HTML page; callers never need to install a
video tool to keep their clips and moments usable.
"""

from __future__ import annotations

import html
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote


REEL_FORMAT = "openbox-reel-v1"
DEFAULT_IMAGE_SECONDS = 3.0
DEFAULT_FFMPEG_TIMEOUT = 300.0
MAX_FFMPEG_TIMEOUT = 600.0
MAX_INPUTS = 64
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
IMAGE_EXTENSIONS = frozenset({".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"})
VIDEO_EXTENSIONS = frozenset({".avi", ".flv", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".webm"})
_SAFE_NAME = re.compile(r"[^a-zA-Z0-9._-]+")


def _safe_timeout(value) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("ffmpeg timeout must be a positive number.") from error
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("ffmpeg timeout must be a positive number.")
    return min(timeout, MAX_FFMPEG_TIMEOUT)


def _slug(value) -> str:
    text = _SAFE_NAME.sub("-", str(value or "").strip()).strip("-.")
    return text or "reel"


def _subject(game=None, *, game_id=None, year=None, subject=None):
    if year is not None:
        return str(year)
    if subject not in (None, ""):
        return str(subject)
    if game_id not in (None, ""):
        return str(game_id)
    if isinstance(game, dict):
        for key in ("game_id", "id", "name", "title"):
            if game.get(key) not in (None, ""):
                return str(game[key])
    if game not in (None, "") and not isinstance(game, dict):
        return str(game)
    return "reel"


def _title(game=None, *, title=None, year=None, subject=None):
    if title not in (None, ""):
        return str(title)
    if year is not None:
        return f"OpenBox {year}"
    if isinstance(game, dict):
        for key in ("name", "title"):
            if game.get(key) not in (None, ""):
                return str(game[key])
    if subject not in (None, ""):
        return str(subject)
    return "OpenBox Reel"


def _raw_path(value):
    if isinstance(value, (str, Path)):
        return value
    if not isinstance(value, dict):
        return None
    for key in (
        "path", "file", "filename", "source", "clip", "clip_path", "video", "video_path",
        "image", "image_path", "screenshot", "screenshot_path", "media", "media_path",
    ):
        candidate = value.get(key)
        if isinstance(candidate, (str, Path)):
            return candidate
    return None


def _local_path(value):
    raw = _raw_path(value)
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    if not text or "\x00" in text:
        return None
    # Reel rendering must not turn a URL into an implicit network fetch.
    if text.casefold().startswith(("http://", "https://", "rtmp://", "file://")):
        return None
    path = Path(text).expanduser()
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return path.resolve()
    except OSError:
        return None


def _metadata(value, *keys):
    if not isinstance(value, dict):
        return None
    for key in keys:
        candidate = value.get(key)
        if candidate not in (None, ""):
            return candidate
    return None


def _duration(value, default=DEFAULT_IMAGE_SECONDS):
    try:
        duration = float(value)
    except (TypeError, ValueError):
        duration = default
    if not math.isfinite(duration) or duration <= 0:
        duration = default
    return round(min(duration, 60.0), 3)


def _entry(value, kind, default_duration=None):
    path = _local_path(value)
    if path is None:
        return None
    item = {"kind": kind, "path": str(path)}
    created = _metadata(value, "created_at", "captured_at", "taken_at", "timestamp", "date")
    if created is not None:
        item["created_at"] = str(created)
    label = _metadata(value, "title", "name", "label")
    if label is not None:
        item["title"] = str(label)
    note = _metadata(value, "note", "caption", "description")
    if note is not None:
        item["note"] = str(note)
    if default_duration is not None:
        item["duration"] = _duration(_metadata(value, "duration", "duration_seconds", "seconds"), default_duration)
    return item


def _items(value):
    if value is None:
        return []
    if isinstance(value, (str, Path, dict)):
        return [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _sort_key(item):
    # Covers stay up front, followed by chronological clips and moments.  A
    # path tie-breaker makes entries deterministic even when timestamps match.
    kind_rank = {"cover": 0, "clip": 1, "moment": 2}.get(item["kind"], 3)
    path = str(item.get("path") or "")
    return (kind_rank, str(item.get("created_at") or ""), path.casefold(), path)


def build_reel_manifest(
    game=None,
    clips=None,
    moments=None,
    cover=None,
    *,
    cover_art=None,
    cover_path=None,
    clip_paths=None,
    moment_paths=None,
    year=None,
    game_id=None,
    subject=None,
    title=None,
):
    """Build a deterministic manifest from local cover, clip, and moment files.

    ``game`` may be a library record; when explicit collections are omitted,
    its ``clips``/``moments``/``cover`` fields are used.  Missing, remote, and
    symlinked media is ignored so an optional reel can never make a request to
    the network or follow an unexpected filesystem link.
    """

    game_record = game if isinstance(game, dict) else {}
    if clips is None:
        clips = clip_paths if clip_paths is not None else game_record.get("clips")
    if moments is None:
        moments = moment_paths if moment_paths is not None else game_record.get("moments")
    if cover is None:
        cover = cover_art
    if cover is None:
        cover = cover_path
    if cover is None:
        cover = game_record.get("cover") or game_record.get("cover_path")

    entries = []
    cover_entry = _entry(cover, "cover", DEFAULT_IMAGE_SECONDS)
    if cover_entry is not None:
        entries.append(cover_entry)
    for value in _items(clips):
        item = _entry(value, "clip")
        if item is not None:
            entries.append(item)
    for value in _items(moments):
        item = _entry(value, "moment", DEFAULT_IMAGE_SECONDS)
        if item is not None:
            entries.append(item)

    unique = []
    seen = set()
    for item in sorted(entries, key=_sort_key):
        identity = (item["kind"], item["path"])
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(item)
        if len(unique) >= MAX_INPUTS:
            break

    manifest = {
        "format": REEL_FORMAT,
        "subject": _subject(game, game_id=game_id, year=year, subject=subject),
        "title": _title(game, title=title, year=year, subject=subject),
        "inputs": unique,
    }
    if year is not None:
        manifest["year"] = str(year)
    return manifest


def build_manifest(*args, game=None, clips=None, moments=None, cover=None, year=None, **kwargs):
    """Short-form manifest builder for callers without a game record.

    The usual form is ``build_manifest(clips, moments, cover)``.  For callers
    that have a game identity, the compatible four-positional form
    ``build_manifest(game, clips, moments, cover)`` is accepted as well.
    """

    if len(args) > 4:
        raise TypeError("build_manifest accepts at most four positional arguments.")
    if len(args) == 4:
        game, clips, moments, cover = args
    elif len(args) == 3:
        first, second, third = args
        if isinstance(first, dict) or (not isinstance(first, (list, tuple)) and isinstance(second, (list, tuple))):
            game, clips, moments = first, second, third
        else:
            clips, moments, cover = args
    elif len(args) == 2:
        clips, moments = args
    elif len(args) == 1:
        clips = args[0]
    return build_reel_manifest(game, clips, moments, cover, year=year, **kwargs)


def manifest_json(manifest) -> str:
    """Serialize a manifest canonically for stable diffs and cache keys."""

    payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if len(payload.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise ValueError("Reel manifest is too large.")
    return payload


def write_manifest(manifest, path):
    """Atomically write ``manifest`` and return its path."""

    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = manifest_json(manifest)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=str(destination.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except (OSError, ValueError):
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return str(destination)


def reel_path(output_root, subject=None, *, game=None, game_id=None, year=None, extension=".mp4"):
    """Return the conventional ``<root>/reels/<game-or-year>.<ext>`` path."""

    label = _subject(game, game_id=game_id, year=year, subject=subject)
    suffix = str(extension or ".mp4")
    if not suffix.startswith("."):
        suffix = f".{suffix}"
    return Path(output_root).expanduser() / "reels" / f"{_slug(label)}{suffix}"


def _is_image(entry):
    if entry["kind"] in {"cover", "moment", "image"}:
        return True
    return Path(entry["path"]).suffix.casefold() in IMAGE_EXTENSIONS


def _ffmpeg_command(manifest, output, ffmpeg):
    entries = list(manifest.get("inputs") or []) if isinstance(manifest, dict) else []
    if not entries:
        return None
    command = [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error"]
    filters = []
    labels = []
    valid_entries = [entry for entry in entries if isinstance(entry, dict) and entry.get("path")]
    for index, entry in enumerate(valid_entries):
        if _is_image(entry):
            command.extend(["-loop", "1", "-t", str(_duration(entry.get("duration"))), "-i", str(entry["path"])])
        else:
            command.extend(["-i", str(entry["path"])])
        label = f"v{index}"
        labels.append(f"[{label}]")
        filters.append(
            f"[{index}:v]scale=1280:720:force_original_aspect_ratio=decrease,"
            f"pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[{label}]"
        )
    if not labels:
        return None
    filters.append("".join(labels) + f"concat=n={len(labels)}:v=1:a=0[outv]")
    command.extend([
        "-filter_complex", ";".join(filters),
        "-map", "[outv]",
        "-an",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output),
    ])
    return command


def ffmpeg_available(which=None):
    """Return the resolved ffmpeg executable, or ``None`` when absent."""

    try:
        return (which or shutil.which)("ffmpeg") or None
    except (OSError, TypeError):
        return None


def _href(path, base):
    relative = os.path.relpath(str(path), str(base.parent)).replace(os.sep, "/")
    return quote(relative, safe="/$:;,@-_.+!*'()~")


def render_html_reel(manifest, output_path):
    """Render a dependency-free HTML reel from a manifest."""

    destination = Path(output_path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    title = html.escape(str((manifest or {}).get("title") or "OpenBox Reel"), quote=True)
    cards = []
    for entry in (manifest or {}).get("inputs", []):
        if not isinstance(entry, dict) or not entry.get("path"):
            continue
        path = Path(entry["path"])
        href = html.escape(_href(path, destination), quote=True)
        label = html.escape(str(entry.get("title") or entry.get("kind") or "Media"), quote=True)
        note = html.escape(str(entry.get("note") or ""), quote=False)
        if _is_image(entry):
            content = f'<img loading="lazy" src="{href}" alt="{label}">'
        else:
            content = f'<video controls preload="metadata" src="{href}"></video>'
        caption = f"<h2>{label}</h2>"
        if note:
            caption += f"<p>{note}</p>"
        cards.append(f"<article>{content}{caption}</article>")
    if not cards:
        cards.append("<p class=empty>No local media was available for this reel.</p>")
    document = (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        f"<meta name=viewport content=\"width=device-width, initial-scale=1\"><title>{title}</title>"
        "<style>body{font:16px system-ui,sans-serif;max-width:72rem;margin:2rem auto;padding:0 1rem;"
        "background:#111;color:#eee}main{display:grid;gap:1rem}article{background:#222;padding:1rem;"
        "border-radius:.5rem}img,video{display:block;max-width:100%;max-height:70vh;margin:auto}"
        "h1,h2{margin:.5rem 0}p{color:#bbb}.empty{text-align:center}</style></head>"
        f"<body><header><h1>{title}</h1></header><main>{''.join(cards)}</main></body></html>\n"
    )
    destination.write_text(document, encoding="utf-8")
    return str(destination)


def render_reel(
    manifest,
    output_root,
    *,
    subject=None,
    year=None,
    ffmpeg=None,
    ffmpeg_path=None,
    timeout=DEFAULT_FFMPEG_TIMEOUT,
):
    """Render a manifest to MP4 when possible, otherwise to HTML.

    The returned mapping always contains ``path`` and ``manifest``.  A
    renderer error is intentionally treated like an absent optional tool and
    produces the HTML representation instead of surfacing a partial MP4.
    """

    if not isinstance(manifest, dict):
        raise ValueError("Reel manifest must be a mapping.")
    actual_subject = subject if subject not in (None, "") else manifest.get("subject", "reel")
    output = reel_path(output_root, actual_subject, year=year, extension=".mp4")
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest_file = output.with_suffix(".manifest.json")
    write_manifest(manifest, manifest_file)

    if ffmpeg is None:
        ffmpeg = ffmpeg_path
    ffmpeg_binary = str(ffmpeg or "").strip() or ffmpeg_available()
    temporary_output = None
    if ffmpeg_binary:
        fd, temporary_output = tempfile.mkstemp(
            prefix=f".{output.stem}.", suffix=output.suffix, dir=str(output.parent)
        )
        os.close(fd)
        command = _ffmpeg_command(manifest, temporary_output, ffmpeg_binary)
    else:
        command = None
    if command is None and temporary_output:
        try:
            Path(temporary_output).unlink(missing_ok=True)
        except OSError:
            pass
        temporary_output = None
    timeout_value = _safe_timeout(timeout) if command is not None else None
    error = ""
    if command is not None:
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=timeout_value)
            os.replace(temporary_output, output)
            return {
                "path": str(output),
                "output": str(output),
                "format": "mp4",
                "fallback": False,
                "ffmpeg": True,
                "manifest": str(manifest_file),
                "inputs": len(manifest.get("inputs") or []),
            }
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            error = type(exc).__name__
        finally:
            if temporary_output:
                try:
                    Path(temporary_output).unlink(missing_ok=True)
                except OSError:
                    pass

    html_output = output.with_suffix(".html")
    render_html_reel(manifest, html_output)
    result = {
        "path": str(html_output),
        "output": str(html_output),
        "format": "html",
        "fallback": True,
        "ffmpeg": bool(ffmpeg_binary),
        "manifest": str(manifest_file),
        "inputs": len(manifest.get("inputs") or []),
    }
    if error:
        result["error"] = error
    return result


def create_reel(
    game=None,
    clips=None,
    moments=None,
    cover=None,
    output_root=".",
    *,
    output_dir=None,
    cover_art=None,
    cover_path=None,
    clip_paths=None,
    moment_paths=None,
    year=None,
    game_id=None,
    subject=None,
    title=None,
    ffmpeg=None,
    ffmpeg_path=None,
    timeout=DEFAULT_FFMPEG_TIMEOUT,
):
    """Build and render a game or year reel in one call."""

    if output_dir is not None:
        output_root = output_dir
    manifest = build_reel_manifest(
        game,
        clips,
        moments,
        cover,
        cover_art=cover_art,
        cover_path=cover_path,
        clip_paths=clip_paths,
        moment_paths=moment_paths,
        year=year,
        game_id=game_id,
        subject=subject,
        title=title,
    )
    return render_reel(
        manifest,
        output_root,
        subject=manifest["subject"],
        ffmpeg=ffmpeg,
        ffmpeg_path=ffmpeg_path,
        timeout=timeout,
    )


def generate_reel(*args, **kwargs):
    """Alias for :func:`create_reel`."""

    return create_reel(*args, **kwargs)


def reel_manifest(*args, **kwargs):
    """Alias for :func:`build_reel_manifest`."""

    return build_reel_manifest(*args, **kwargs)


reel_output_path = reel_path
write_reel_manifest = write_manifest
render_html = render_html_reel
html_fallback = render_html_reel


__all__ = [
    "REEL_FORMAT",
    "DEFAULT_IMAGE_SECONDS",
    "DEFAULT_FFMPEG_TIMEOUT",
    "build_reel_manifest",
    "build_manifest",
    "reel_manifest",
    "manifest_json",
    "write_manifest",
    "reel_path",
    "reel_output_path",
    "ffmpeg_available",
    "render_html_reel",
    "render_html",
    "html_fallback",
    "render_reel",
    "create_reel",
    "generate_reel",
    "write_reel_manifest",
]
