"""EmuMovies media provider: search and bounded download.

The provider is deliberately small: it resolves one media kind through the
public EmuMovies v1 endpoint, rate-limits to one request per second, and
downloads through :func:`backend_io.download_file` (the single downloader used
by the rest of OpenBox). A 4xx is permanent, a 5xx/timeout is retried twice.

The endpoint shape follows the historical ``/v1/media/<kind>`` call. Search
performs the authenticated request and returns a candidate carrying the
response content type; download reuses that candidate to choose the file
extension while the shared downloader performs the streaming transfer.
"""

from __future__ import annotations

import base64
import re
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from backend_io import contained_path, download_file

EMUMOVIES_ENDPOINT = "https://api.emumovies.com/v1/media"
EMUMOVIES_MAX_RETRIES = 2
EMUMOVIES_RETRY_DELAYS = (2, 8)
EMUMOVIES_RATE_LIMIT_SECONDS = 1.0
EMUMOVIES_TIMEOUT = 30

# OpenBox media kind -> EmuMovies endpoint kind.
KIND_MAP = {
    "cover": "box",
    "box_front": "box",
    "box_back": "boxback",
    "box_spine": "boxspine",
    "box_3d": "box3d",
    "screenshots": "snap",
    "title_screen": "title",
    "banner": "marquee",
    "cart_front": "cart",
    "cart_back": "cartback",
    "disc": "disc",
    "clear_logo": "logo",
    "video_snap": "video",
    "video_theme": "video",
    "manual": "manual",
    "music": "music",
}
EMUMOVIES_KINDS = frozenset(KIND_MAP)
ENDPOINT_KINDS = frozenset(KIND_MAP.values())

# Aliases accepted by the existing manual route, which historically used
# endpoint spellings (``snap``, ``boxback``) and library field names.
_COMPAT_ALIASES = {
    **KIND_MAP,
    "snap": "screenshots",
    "screenshot": "screenshots",
    "titlescreen": "title_screen",
    "title": "title_screen",
    "marquee": "banner",
    "video": "video_snap",
    "boxback": "box_back",
    "boxspine": "box_spine",
    "box3d": "box_3d",
    "boxfront": "cover",
    "cart": "cart_front",
    "cartback": "cart_back",
    "logo": "clear_logo",
}

_IMAGE_TYPES = (
    "cover", "box_front", "box_back", "box_spine", "box_3d", "screenshots",
    "title_screen", "banner", "cart_front", "cart_back", "disc", "clear_logo",
)
_VIDEO_TYPES = ("video_snap", "video_theme")
_MUSIC_TYPES = ("music",)
_MANUAL_TYPES = ("manual",)

MAX_BYTES_BY_KIND = {
    **{kind: 64 * 1024 * 1024 for kind in _IMAGE_TYPES},
    **{kind: 512 * 1024 * 1024 for kind in _VIDEO_TYPES},
    **{kind: 32 * 1024 * 1024 for kind in _MUSIC_TYPES},
    **{kind: 64 * 1024 * 1024 for kind in _MANUAL_TYPES},
}

EXPECTED_TYPES_BY_KIND = {
    **{kind: ("image/",) for kind in _IMAGE_TYPES},
    **{kind: ("video/",) for kind in _VIDEO_TYPES},
    **{kind: ("audio/", "application/ogg") for kind in _MUSIC_TYPES},
    **{kind: ("application/pdf",) for kind in _MANUAL_TYPES},
}

EXTENSION_BY_CONTENT_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/ogg": ".ogg",
    "application/ogg": ".ogg",
    "application/pdf": ".pdf",
}

_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST = [0.0]


def reset_rate_limit():
    """Reset the module-level throttle. Tests use this; production never does."""
    with _REQUEST_LOCK:
        _LAST_REQUEST[0] = 0.0


def _throttle(clock, sleep):
    with _REQUEST_LOCK:
        now = clock()
        if _LAST_REQUEST[0]:
            wait = EMUMOVIES_RATE_LIMIT_SECONDS - (now - _LAST_REQUEST[0])
            if wait > 0:
                sleep(wait)
                now = clock()
        _LAST_REQUEST[0] = now


def _basic_auth(credentials):
    username = str((credentials or {}).get("username") or "").strip()
    password = str((credentials or {}).get("password") or "").strip()
    if not username or not password:
        raise ValueError("Configure EmuMovies credentials in Settings first.")
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _status_of(response, error=None):
    if error is not None:
        return int(getattr(error, "code", 0) or 0)
    try:
        return int(getattr(response, "status", 200) or 200)
    except (TypeError, ValueError):
        return 200


def _content_type(response, error=None):
    headers = getattr(error, "headers", None) if error is not None else getattr(response, "headers", None)
    if headers is None:
        return ""
    try:
        value = headers.get("Content-Type", "")
    except AttributeError:
        return ""
    return str(value or "").split(";", 1)[0].strip().casefold()


def _body_excerpt(response, error=None):
    source = error if error is not None else response
    read = getattr(source, "read", None)
    if not callable(read):
        return ""
    try:
        payload = read(300)
    except Exception:  # noqa: BLE001 - excerpts are diagnostic only
        return ""
    if isinstance(payload, bytes):
        return payload.decode("utf-8", "replace").strip()
    return str(payload or "").strip()


def _close(response):
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - closing a failed response must not mask the error
            pass


def _request_once(opener, request, timeout):
    """Return (response, http_error). A raised transport error propagates."""
    try:
        return opener(request, timeout=timeout), None
    except HTTPError as error:
        return None, error


def search(game, kind, credentials, *, opener=urlopen, sleep=time.sleep, clock=time.monotonic, timeout=EMUMOVIES_TIMEOUT):
    """Resolve one media kind to a download candidate.

    Raises ``ValueError`` for a permanent 4xx, a bounded retry for 5xx/timeout,
    and after two retries for a persistent server failure.
    """
    kind = str(kind or "").strip()
    if kind not in KIND_MAP:
        raise ValueError(f"Unsupported EmuMovies media kind: {kind}")
    auth = _basic_auth(credentials)
    platform = str(game.get("platform") or "Arcade").strip() or "Arcade"
    name = str(game.get("name") or "").strip()
    metadata = {
        "publisher": str(game.get("publisher") or "").strip(),
        "developer": str(game.get("developer") or "").strip(),
        "genre": str(game.get("genre") or "").strip(),
        "players": str(game.get("players") or game.get("max_players") or "").strip(),
        "release_year": str(game.get("release_year") or game.get("year") or "").strip(),
        "region": str(game.get("region") or "").strip(),
    }
    query = urlencode({"system": platform, "search": name})
    url = f"{EMUMOVIES_ENDPOINT}/{KIND_MAP[kind]}?{query}"
    last_error = ""
    for attempt in range(EMUMOVIES_MAX_RETRIES + 1):
        _throttle(clock, sleep)
        request = Request(url, headers={"Authorization": auth, "User-Agent": "OpenBox/1"})
        try:
            response, error = _request_once(opener, request, timeout)
        except (URLError, TimeoutError, OSError) as transport_error:
            last_error = str(transport_error)
            if attempt < EMUMOVIES_MAX_RETRIES:
                sleep(EMUMOVIES_RETRY_DELAYS[min(attempt, len(EMUMOVIES_RETRY_DELAYS) - 1)])
                continue
            raise ValueError(f"EmuMovies request failed: {last_error}") from transport_error
        try:
            status = _status_of(response, error)
            if 400 <= status < 500:
                detail = _body_excerpt(response, error)
                raise ValueError(f"EmuMovies request failed (HTTP {status}): {detail or 'permanent error'}")
            if status >= 500:
                last_error = f"HTTP {status}"
                if attempt < EMUMOVIES_MAX_RETRIES:
                    sleep(EMUMOVIES_RETRY_DELAYS[min(attempt, len(EMUMOVIES_RETRY_DELAYS) - 1)])
                    continue
                raise ValueError(f"EmuMovies request failed: {last_error}")
            content_type = _content_type(response, error)
            return {
                "kind": kind,
                "endpoint_kind": KIND_MAP[kind],
                "url": url,
                "platform": platform,
                "name": name,
                "publisher": metadata["publisher"],
                "developer": metadata["developer"],
                "genre": metadata["genre"],
                "players": metadata["players"],
                "release_year": metadata["release_year"],
                "region": metadata["region"],
                "content_type": content_type,
                "headers": {"Authorization": auth},
            }
        finally:
            _close(response or error)
    raise ValueError(f"EmuMovies request failed: {last_error or 'unknown error'}")


def extension_for(content_type="", url="", kind=""):
    """Pick a safe file extension from the response type, then the URL."""
    content_type = str(content_type or "").strip().casefold()
    candidate = EXTENSION_BY_CONTENT_TYPE.get(content_type)
    if candidate:
        return candidate
    suffix = Path(str(url or "")).suffix.casefold()
    if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix or ""):
        return suffix
    if kind in _VIDEO_TYPES:
        return ".mp4"
    if kind in _MUSIC_TYPES:
        return ".mp3"
    if kind in _MANUAL_TYPES:
        return ".pdf"
    return ".jpg"


def _slug(value, fallback):
    slug = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").casefold()).strip("-_")
    return (slug or fallback)[:80]


def destination_for(candidate, destination_root):
    """Compute and contain the destination for a candidate."""
    root = Path(destination_root).expanduser()
    kind = str(candidate.get("kind") or "cover")
    platform = _slug(candidate.get("platform"), "unknown-platform")
    name = _slug(candidate.get("name"), "unknown-game")
    extension = extension_for(candidate.get("content_type"), candidate.get("url"), kind)
    destination = root / "emumovies" / platform / name / f"{_slug(kind, 'media')}{extension}"
    contained_path(destination, [root])
    return destination


def download(candidate, destination_root, *, opener=urlopen, timeout=EMUMOVIES_TIMEOUT):
    """Download a search candidate under ``<destination_root>/emumovies``."""
    if not isinstance(candidate, dict):
        raise ValueError("Invalid EmuMovies candidate.")
    url = str(candidate.get("url") or "").strip()
    if not re.match(r"^https://[^/\s]+/", url, re.IGNORECASE):
        raise ValueError("EmuMovies media URLs must use HTTPS.")
    kind = str(candidate.get("kind") or "cover")
    if kind not in KIND_MAP:
        raise ValueError(f"Unsupported EmuMovies media kind: {kind}")
    destination = destination_for(candidate, destination_root)
    headers = {"User-Agent": "OpenBox/1"}
    candidate_headers = candidate.get("headers")
    if isinstance(candidate_headers, dict):
        auth = str(candidate_headers.get("Authorization") or "").strip()
        if auth.startswith("Basic "):
            headers["Authorization"] = auth
    try:
        download_file(
            url,
            destination,
            expected_types=EXPECTED_TYPES_BY_KIND[kind],
            max_bytes=MAX_BYTES_BY_KIND[kind],
            timeout=timeout,
            opener=opener,
            headers=headers,
        )
    except (OSError, ValueError) as error:
        raise ValueError(f"EmuMovies download failed: {error}") from error
    contained_path(destination, [Path(destination_root).expanduser()], must_exist=True)
    return str(destination)


def download_emumovies_media(game, credentials, media_root, media_type="box", *, opener=urlopen, sleep=time.sleep):
    """Compatibility entry point for the existing manual EmuMovies route."""
    canonical = _COMPAT_ALIASES.get(str(media_type or "").strip().casefold())
    if canonical is None:
        raise ValueError(f"Unsupported EmuMovies media type: {media_type}")
    candidate = search(game, canonical, credentials, opener=opener, sleep=sleep)
    return download(candidate, media_root, opener=opener)
