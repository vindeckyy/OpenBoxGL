"""Metadata and media provider capability plugins (P10.2).

Plugins never return filesystem paths or downloadable URLs that get written
into a game record: metadata candidates are validated against the library
field allowlist, and media URLs are SSRF-gated and queued through the durable
media queue. Applying a candidate preserves existing non-empty fields unless
the caller asks to overwrite.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
import socket
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

from backend_io import contained_path
from pkg.parity.parity_metadata_providers import (
    APPLY_FIELDS,
    IMAGE_MAX_BYTES,
    MANUAL_MAX_BYTES,
    MUSIC_MAX_BYTES,
    VIDEO_MAX_BYTES,
    apply_candidate,
)
from pkg.parity.parity_media import default_media_queue_path, enqueue_media_job
from pkg.parity.parity_plugin_storefront import (
    default_plugins_dir,
    plugin_manifest,
    require_network_grant,
    require_plugin_capability,
)
from plugins import run_plugin_hook

try:  # the shared downloader; parity_media re-exports it when available
    from pkg.parity.parity_media import download_file  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - current parity_media has no re-export
    from backend_io import download_file

LOGGER = logging.getLogger("openbox.plugins.providers")

MAX_CANDIDATES = 20
MAX_MEDIA_URLS_PER_KIND = 10
MAX_URL_LENGTH = 2000
MAX_CANDIDATE_TEXT = 200
MAX_FIELD_TEXT = 4000
MAX_CANDIDATE_ID = 160
MAX_QUEUE_MEDIA = 20

VIDEO_KINDS = {"video", "video_snap", "video_theme", "video_trailer", "video_recording"}
MUSIC_KINDS = {"music"}
MANUAL_KINDS = {"manual"}
_DEFAULT_EXTENSIONS = {
    "music": ".mp3",
    "manual": ".pdf",
    "video": ".mp4",
    "video_snap": ".mp4",
    "video_theme": ".mp4",
    "video_trailer": ".mp4",
    "video_recording": ".mp4",
}
_SLUG_RE = re.compile(r"[^a-z0-9._-]+")

_PUBLIC_GAME_FIELDS = (
    "game_id", "name", "platform", "genre", "year", "developer", "publisher",
    "series", "description", "esrb", "region", "sort_title", "source",
    "steam_app_id", "gog_id", "igdb_id", "ra_game_id", "progress", "favorite",
    "playtime_seconds", "last_played",
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _media_kinds():
    try:
        from pkg.state.media_probe import MEDIA_PATH_FIELDS, MEDIA_TYPES_ALL

        return frozenset(MEDIA_PATH_FIELDS) | frozenset(MEDIA_TYPES_ALL) | {"screenshots"}
    except ImportError:  # pragma: no cover - runtime always has the package
        return frozenset({
            "cover", "background", "screenshots", "clear_logo", "fanart",
            "banner", "icon", "box_back", "box_spine", "box_3d", "title_screen",
            "cart_front", "cart_back", "disc", "advertisement", "manual",
            "video", "music", "video_snap", "video_theme", "video_trailer",
            "poster", "square",
        })


def _slug(value, limit=60):
    cleaned = _SLUG_RE.sub("-", str(value or "").strip().casefold()).strip("-")
    return cleaned[:limit] or "media"


def _text(value, limit):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    return str(value).strip()[:limit]


def _public_game(game):
    if not isinstance(game, dict):
        return {}
    projected = {}
    for key in _PUBLIC_GAME_FIELDS:
        value = game.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, (str, int, float, bool)):
            projected[key] = value if isinstance(value, bool) else _text(value, 500)
    if not projected.get("game_id"):
        game_id = game.get("id")
        if isinstance(game_id, (int, str)) and not isinstance(game_id, bool):
            projected["game_id"] = str(game_id)[:MAX_CANDIDATE_ID]
    return projected


def _clean_media_kind(kind):
    value = str(kind or "").strip()
    return value if value in _media_kinds() else ""


# ---------------------------------------------------------------------------
# URL gate and download
# ---------------------------------------------------------------------------

def safe_media_url(url, *, resolver=socket.getaddrinfo):
    """Return *url* when it is a public HTTPS URL; otherwise raise ``ValueError``.

    Rejects credentials in the URL, non-HTTPS schemes, loopback/localhost
    hosts, and hosts resolving to private/link-local/reserved addresses.
    An unresolvable host stays syntactically valid; the download then fails
    cleanly instead of leaking a request to an internal address.
    """
    if not isinstance(url, str):
        raise ValueError("Media URL must be text.")
    value = url.strip()
    if not value or len(value) > MAX_URL_LENGTH:
        raise ValueError("Media URL is empty or too long.")
    parts = urlsplit(value)
    if parts.scheme != "https":
        raise ValueError("Media URLs must use HTTPS.")
    if parts.username or parts.password:
        raise ValueError("Media URLs may not carry credentials.")
    host = parts.hostname
    if not host:
        raise ValueError("Media URL needs a host.")
    host = host.strip().casefold().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("Media hosts cannot be localhost.")
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise ValueError("Media hosts must be public addresses.")
        return value
    if resolver is False:
        return value
    try:
        addresses = {info[4][0] for info in resolver(host, None) if info and info[4]}
    except (OSError, TypeError, ValueError):
        # Unresolvable now; download_plugin_media resolves again before use.
        return value
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(str(address).split("%", 1)[0])
        except ValueError as error:
            raise ValueError("Media host resolved to an invalid address.") from error
        if not parsed.is_global:
            raise ValueError("Media hosts must not resolve to private addresses.")
    return value


def _download_limits(kind):
    if kind in VIDEO_KINDS:
        return VIDEO_MAX_BYTES, ("video/",)
    if kind in MUSIC_KINDS:
        return MUSIC_MAX_BYTES, ("audio/", "application/ogg")
    if kind in MANUAL_KINDS:
        return MANUAL_MAX_BYTES, ("application/pdf",)
    return IMAGE_MAX_BYTES, ("image/",)


def download_plugin_media(plugin_id, game, kind, candidate, destination_root, *,
                          opener=urlopen, timeout=30):
    """Download one plugin media candidate into ``destination_root``.

    The URL must pass :func:`safe_media_url`; the file is written through the
    shared ``download_file`` helper (temp file + atomic replace) and cannot
    escape ``destination_root``.
    """
    kind = _clean_media_kind(kind)
    if not kind:
        raise ValueError(f"Unsupported media kind: {kind!r}.")
    url = candidate.get("url") if isinstance(candidate, dict) else candidate
    safe = safe_media_url(url)
    root = Path(destination_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(safe.encode("utf-8")).hexdigest()[:12]
    name = _text((game or {}).get("name") if isinstance(game, dict) else "", 60)
    extension = _DEFAULT_EXTENSIONS.get(kind, ".bin")
    destination = root / f"{_slug(name or plugin_id)}-{kind}-{digest}{extension}"
    contained_path(destination, [root])
    max_bytes, expected_types = _download_limits(kind)
    download_file(
        safe,
        destination,
        expected_types=expected_types,
        max_bytes=max_bytes,
        timeout=timeout,
        opener=opener,
    )
    contained_path(destination, [root], must_exist=True)
    return str(destination)


# ---------------------------------------------------------------------------
# Candidate validation
# ---------------------------------------------------------------------------

def _clean_fields(raw):
    if not isinstance(raw, dict):
        return {}
    fields = {}
    for key in APPLY_FIELDS:
        value = raw.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            continue
        if isinstance(value, str):
            value = value.strip()[:MAX_FIELD_TEXT]
            if not value:
                continue
        fields[key] = value
    return fields


def _clean_candidate(raw, manifest, capability):
    if not isinstance(raw, dict):
        return None
    candidate_id = raw.get("id")
    if isinstance(candidate_id, (int, float)) and not isinstance(candidate_id, bool):
        candidate_id = str(int(candidate_id))
    name = _text(raw.get("name"), MAX_CANDIDATE_TEXT)
    if not isinstance(candidate_id, str) or not candidate_id.strip() or not name:
        return None
    candidate_id = candidate_id.strip()[:MAX_CANDIDATE_ID]
    fields = _clean_fields(raw.get("fields"))
    media = {}
    raw_media = raw.get("media")
    if isinstance(raw_media, dict):
        for kind, values in raw_media.items():
            kind = _clean_media_kind(kind)
            if not kind:
                continue
            items = values if isinstance(values, (list, tuple)) else [values]
            urls = []
            for value in items:
                try:
                    urls.append(safe_media_url(value, resolver=False))
                except ValueError:
                    continue
                if len(urls) >= MAX_MEDIA_URLS_PER_KIND:
                    break
            if urls:
                media[kind] = urls
    return {
        "id": candidate_id,
        "plugin_id": manifest["id"],
        "source": capability.get("name") or manifest.get("name") or manifest["id"],
        "provider": capability.get("name") or manifest.get("name") or manifest["id"],
        "name": name,
        "platform": _text(
            raw.get("platform") or fields.get("platform") or capability.get("platform"),
            40,
        ),
        "year": _text(raw.get("year") or fields.get("year"), 12),
        "fields": fields,
        "media": media,
    }


# ---------------------------------------------------------------------------
# Metadata provider
# ---------------------------------------------------------------------------

def _metadata_manifest(plugins_dir, plugin_id):
    root = Path(plugins_dir or default_plugins_dir())
    manifest = plugin_manifest(root, plugin_id)
    capability = require_plugin_capability(manifest, "metadata")
    require_network_grant(manifest, root)
    return root, manifest, capability


def plugin_metadata_search(plugin_id, game, *, limit=MAX_CANDIDATES, plugins_dir=None, timeout=None):
    """Return validated metadata candidates from one plugin.

    Hook failures are logged and produce an empty list so one optional
    provider cannot break a multi-provider search.
    """
    root, manifest, capability = _metadata_manifest(plugins_dir, plugin_id)
    try:
        limit = max(1, min(MAX_CANDIDATES, int(limit)))
    except (TypeError, ValueError):
        limit = MAX_CANDIDATES
    payload = {"action": "search", "game": _public_game(game), "limit": limit}
    try:
        result, error = run_plugin_hook(root, manifest["id"], "metadata", payload, timeout=timeout)
    except Exception as hook_error:  # an optional provider never breaks the search
        LOGGER.warning("Plugin %s metadata search crashed: %s", manifest["id"], hook_error)
        return []
    if error:
        LOGGER.warning("Plugin %s metadata search failed: %s", manifest["id"], error)
        return []
    if not isinstance(result, dict):
        return []
    rows = result.get("candidates")
    if not isinstance(rows, list):
        return []
    candidates = []
    for raw in rows[:MAX_CANDIDATES]:
        candidate = _clean_candidate(raw, manifest, capability)
        if candidate is not None:
            candidates.append(candidate)
        if len(candidates) >= limit:
            break
    return candidates


def _queue_media(plugin_id, game, media, *, overwrite, queue_path):
    if not isinstance(game, dict):
        return 0
    game_id = str(game.get("game_id") or game.get("id") or "").strip()[:MAX_CANDIDATE_ID]
    if not game_id or not isinstance(media, dict):
        return 0
    path = Path(queue_path) if queue_path is not None else default_media_queue_path()
    allowed = _media_kinds()
    queued = 0
    for kind, values in media.items():
        kind = _clean_media_kind(kind)
        if not kind or kind not in allowed:
            continue
        items = values if isinstance(values, (list, tuple)) else [values]
        for value in items:
            try:
                url = safe_media_url(value, resolver=False)
            except ValueError:
                continue
            try:
                enqueue_media_job(path, {
                    "game_id": game_id,
                    "source": f"plugin:{plugin_id}",
                    "kind": kind,
                    "url": url,
                    "overwrite": bool(overwrite),
                })
            except (OSError, ValueError) as error:
                LOGGER.warning("Plugin %s media could not be queued: %s", plugin_id, error)
                return queued
            queued += 1
            if queued >= MAX_QUEUE_MEDIA:
                return queued
    return queued


def plugin_metadata_apply(plugin_id, game, candidate_id, *, overwrite=False, plugins_dir=None,
                          queue_path=None, timeout=None):
    """Apply one plugin candidate to *game* in place and queue its media URLs.

    Only known library fields are written. Media stays a queue entry: a URL is
    never written into a media field.
    """
    if not isinstance(game, dict):
        raise ValueError("A game record is required.")
    candidate_id = _text(candidate_id, MAX_CANDIDATE_ID)
    if not candidate_id:
        raise ValueError("A candidate id is required.")
    root, manifest, capability = _metadata_manifest(plugins_dir, plugin_id)
    payload = {"action": "apply", "game": _public_game(game), "candidate_id": candidate_id}
    try:
        result, error = run_plugin_hook(root, manifest["id"], "metadata", payload, timeout=timeout)
    except Exception as hook_error:
        raise ValueError(f"Plugin metadata apply failed: {hook_error}") from hook_error
    if error:
        raise ValueError(f"Plugin metadata apply failed: {error}")
    if not isinstance(result, dict):
        raise ValueError("Plugin metadata apply must return a JSON object.")
    fields = _clean_fields(result.get("fields"))
    apply_candidate(game, {"source": f"plugin:{manifest['id']}", "fields": fields}, overwrite=bool(overwrite))
    _queue_media(plugin_id, game, result.get("media"), overwrite=overwrite, queue_path=queue_path)
    return game


# ---------------------------------------------------------------------------
# Media provider
# ---------------------------------------------------------------------------

def plugin_media_search(plugin_id, game, kind, *, limit=MAX_CANDIDATES, plugins_dir=None, timeout=None):
    """Return validated media candidates of one kind from a plugin."""
    root = Path(plugins_dir or default_plugins_dir())
    manifest = plugin_manifest(root, plugin_id)
    capability = require_plugin_capability(manifest, "media")
    require_network_grant(manifest, root)
    kind = _clean_media_kind(kind)
    if not kind:
        raise ValueError(f"Unsupported media kind: {kind!r}.")
    declared = capability.get("media_kinds")
    if isinstance(declared, list) and declared and kind not in declared:
        raise ValueError(f"Plugin does not provide the {kind} media kind.")
    try:
        limit = max(1, min(MAX_CANDIDATES, int(limit)))
    except (TypeError, ValueError):
        limit = MAX_CANDIDATES
    payload = {"action": "search", "game": _public_game(game), "kind": kind, "limit": limit}
    try:
        result, error = run_plugin_hook(root, manifest["id"], "media", payload, timeout=timeout)
    except Exception as hook_error:
        LOGGER.warning("Plugin %s media search crashed: %s", manifest["id"], hook_error)
        return []
    if error:
        LOGGER.warning("Plugin %s media search failed: %s", manifest["id"], error)
        return []
    if not isinstance(result, dict):
        return []
    rows = result.get("candidates")
    if not isinstance(rows, list):
        return []
    candidates = []
    for raw in rows[:limit]:
        if not isinstance(raw, dict):
            continue
        try:
            url = safe_media_url(raw.get("url"), resolver=False)
        except ValueError:
            continue
        item = {"url": url, "kind": _clean_media_kind(raw.get("kind")) or kind}
        for key in ("title", "width", "height"):
            value = raw.get(key)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                item[key] = value.strip()[:MAX_CANDIDATE_TEXT] if isinstance(value, str) else value
        candidates.append(item)
    return candidates
