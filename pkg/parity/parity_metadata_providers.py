"""Metadata provider registry for OpenBox.

The registry normalizes candidate records from the built-in LaunchBox database
and the optional IGDB/ScreenScraper/SteamGridDB providers into one shape,
ranks them by normalized title/platform similarity, and applies only known
library fields. Media candidates stay URLs until the caller downloads them
through :func:`download_candidate_media` (the shared ``download_file`` path);
a remote URL is never written into a local media field.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

from backend_io import contained_path, download_file

MAX_CANDIDATES = 20
MAX_MEDIA_URLS_PER_KIND = 10
MAX_URL_LENGTH = 2000
MAX_APPLY_URL_CHECKS = 40

BUILTIN_PROVIDER_ORDER = ("launchbox", "screenscraper", "igdb", "steamgriddb")

# Fields a provider candidate may write. Deliberately excludes path/launch and
# identity fields so a scrape cannot repoint a game at a different executable.
APPLY_FIELDS = (
    "name", "platform", "genre", "year", "developer", "publisher", "series",
    "description", "esrb", "max_players", "rating", "region", "sort_title",
)

IMAGE_MAX_BYTES = 64 * 1024 * 1024
VIDEO_MAX_BYTES = 512 * 1024 * 1024
MUSIC_MAX_BYTES = 32 * 1024 * 1024
MANUAL_MAX_BYTES = 64 * 1024 * 1024

VIDEO_MEDIA_KINDS = {"video", "video_snap", "video_theme", "video_trailer", "video_recording"}
MUSIC_MEDIA_KINDS = {"music"}
MANUAL_MEDIA_KINDS = {"manual"}

MEDIA_TYPES_BY_SOURCE = {
    "launchbox": {
        "poster": ("Box - Front",),
        "square": ("Icon", "Box - Front"),
    },
    "steamgriddb": {
        "poster": ("600x900",),
        "square": ("512x512",),
    },
}

_ARTICLE_RE = re.compile(r"^(the|a|an)\s+")
_TRAILING_ARTICLE_RE = re.compile(r",\s*(the|a|an)\s*$", re.IGNORECASE)
_PUNCTUATION_RE = re.compile(r"[^a-z0-9]+")


def normalize_title(value):
    """Normalize a title for matching (case, punctuation, leading articles)."""
    text = _TRAILING_ARTICLE_RE.sub("", str(value or "").strip())
    text = text.casefold().replace("&", " and ")
    words = " ".join(_PUNCTUATION_RE.sub(" ", text).split())
    return _ARTICLE_RE.sub("", words).strip()


def title_query_variants(value):
    """Queries worth sending to a provider for an article-shifted title."""
    text = str(value or "").strip()
    variants = []

    def add(candidate):
        candidate = str(candidate or "").strip()
        if candidate and candidate.casefold() not in {item.casefold() for item in variants}:
            variants.append(candidate)

    add(text)
    leading = re.match(r"^(the|a|an)\s+(.+)$", text, re.IGNORECASE)
    if leading:
        add(leading.group(2))
        add(f"{leading.group(2)}, {leading.group(1).title()}")
    trailing = _TRAILING_ARTICLE_RE.search(text)
    if trailing:
        base = text[: trailing.start()].strip()
        add(base)
        add(f"{trailing.group(1).title()} {base}")
    return variants


def normalize_platform(value):
    text = _PUNCTUATION_RE.sub(" ", str(value or "").casefold()).strip()
    return " ".join(text.split())


def platform_equivalent(left, right):
    if not left or not right:
        return False
    if normalize_platform(left) == normalize_platform(right):
        return True
    try:
        from metadata import PLATFORM_ALIASES
    except ImportError:
        return False
    alias = PLATFORM_ALIASES.get(str(left), str(left))
    return normalize_platform(alias) == normalize_platform(right)


def title_similarity(left, right):
    a = normalize_title(left)
    b = normalize_title(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def match_rank(title, candidate_name):
    """0 exact, 1 prefix, 2 substring, 3 fuzzy."""
    query = normalize_title(title)
    candidate = normalize_title(candidate_name)
    if not query or not candidate:
        return 3
    if query == candidate:
        return 0
    if candidate.startswith(query) or query.startswith(candidate):
        return 1
    if query in candidate or candidate in query:
        return 2
    return 3


def rank_candidates(candidates, title, platform="", *, limit=MAX_CANDIDATES):
    """Sort candidates by exactness, platform match, similarity, source order."""

    def key(item):
        return (
            match_rank(title, item.get("name")),
            0 if platform and platform_equivalent(platform, item.get("platform")) else 1,
            -title_similarity(title, item.get("name")),
            len(str(item.get("name") or "")),
        )

    ranked = sorted(candidates, key=key)
    return ranked[: max(0, int(limit))]


def clean_media_url(url, *, resolver=socket.getaddrinfo):
    """Return the URL when it is a public HTTPS URL, otherwise ``""``.

    ``resolver=False`` skips DNS (used for result lists); pass a callable such
    as :func:`socket.getaddrinfo` to reject hostnames resolving to private
    addresses before a download.
    """
    candidate = str(url or "").strip()
    if not candidate or len(candidate) > MAX_URL_LENGTH:
        return ""
    parsed = urlparse(candidate)
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        return ""
    if parsed.username or parsed.password:
        return ""
    host = parsed.hostname.strip().casefold().rstrip(".")
    if not host or host == "localhost" or host.endswith(".localhost"):
        return ""
    literal = None
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return candidate if literal.is_global else ""
    if resolver is False:
        return candidate
    try:
        addresses = {info[4][0] for info in resolver(host, None) if info and info[4]}
    except (OSError, TypeError, ValueError):
        return ""
    for address in addresses:
        try:
            if not ipaddress.ip_address(str(address).split("%", 1)[0]).is_global:
                return ""
        except ValueError:
            return ""
    return candidate if addresses else ""


# Historical name used by the plugin provider layer.
safe_media_url = clean_media_url


def _candidate(source, identifier, name, platform="", year="", fields=None, media=None):
    clean_fields = {}
    for key, value in (fields or {}).items():
        if key in APPLY_FIELDS and value not in (None, "", []):
            clean_fields[key] = value
    clean_media = {}
    for kind, urls in (media or {}).items():
        if not isinstance(urls, (list, tuple)):
            urls = [urls]
        kept = []
        for url in urls:
            value = clean_media_url(url, resolver=False)
            if value and value not in kept:
                kept.append(value)
            if len(kept) >= MAX_MEDIA_URLS_PER_KIND:
                break
        if kept:
            clean_media[str(kind)] = kept
    return {
        "id": str(identifier) if identifier not in (None, "") else "",
        "source": source,
        "name": str(name or ""),
        "platform": str(platform or ""),
        "year": str(year or ""),
        "fields": clean_fields,
        "media": clean_media,
    }


def normalize_candidate(raw, source):
    if not isinstance(raw, dict):
        return None
    fields = dict(raw.get("fields") or {})
    for key in APPLY_FIELDS:
        if key in raw and raw.get(key) not in (None, "") and key not in fields:
            fields[key] = raw[key]
    return _candidate(
        source,
        raw.get("id"),
        raw.get("name"),
        raw.get("platform", ""),
        raw.get("year", ""),
        fields,
        raw.get("media") or {},
    )


class _Provider:
    id = ""
    name = ""

    def search(self, game, limit, state=None):
        return []

    def apply(self, game, candidate):
        return apply_candidate(game, candidate)


class LaunchBoxProvider(_Provider):
    id = "launchbox"
    name = "LaunchBox Games Database"

    @staticmethod
    def database_path(state):
        try:
            import webapp_state

            configured = getattr(webapp_state, "METADATA_DATABASE", None)
        except Exception:  # noqa: BLE001 - optional runtime binding
            configured = None
        if configured:
            return Path(configured)
        settings = state.get("settings") if isinstance(state, dict) else {}
        value = str((settings or {}).get("metadata_database") or "").strip()
        return Path(value) if value else None

    def search(self, game, limit, state=None):
        import metadata as metadata_module

        database_path = self.database_path(state or {})
        if database_path is None or not Path(database_path).is_file():
            return []
        rows = []
        seen_ids = set()
        for query in title_query_variants(game.get("name")):
            for row in metadata_module.search_games(
                database_path,
                query,
                str(game.get("platform") or ""),
                limit=max(1, min(MAX_CANDIDATES, int(limit) * 2)),
            ):
                identifier = row.get("database_id")
                if identifier in seen_ids:
                    continue
                seen_ids.add(identifier)
                rows.append(row)
                if len(rows) >= max(1, min(MAX_CANDIDATES, int(limit) * 2)):
                    break
            if len(rows) >= max(1, min(MAX_CANDIDATES, int(limit) * 2)):
                break
        candidates = []
        try:
            database = metadata_module.get_db_connection(database_path)
        except Exception:  # noqa: BLE001 - a corrupt DB yields no candidates
            database = None
        for row in rows:
            media = {}
            if database is not None:
                try:
                    images = metadata_module._load_media_images(database, row["database_id"], None)
                except Exception:  # noqa: BLE001 - optional media
                    images = []
                reverse = {}
                for kind, lbdb_types in metadata_module.MEDIA_TYPE_MAP.items():
                    for lbdb_type in lbdb_types:
                        reverse.setdefault(lbdb_type, kind)
                for kind, types in MEDIA_TYPES_BY_SOURCE["launchbox"].items():
                    for lbdb_type in types:
                        reverse.setdefault(lbdb_type, kind)
                for image in images:
                    kind = reverse.get(str(image.get("type") or ""))
                    if not kind:
                        continue
                    url = metadata_module.IMAGE_URL + str(image.get("filename") or "")
                    media.setdefault(kind, []).append(url)
            fields = {
                "name": row.get("name"),
                "platform": row.get("platform"),
                "developer": row.get("developer"),
                "publisher": row.get("publisher"),
                "genre": row.get("genre"),
                "description": row.get("overview"),
                "series": row.get("series"),
                "esrb": row.get("esrb"),
                "max_players": row.get("max_players"),
                "year": metadata_module._year_from_record(row),
            }
            candidates.append(_candidate(
                self.id,
                row.get("database_id"),
                row.get("name"),
                row.get("platform"),
                metadata_module._year_from_record(row),
                fields,
                media,
            ))
        return candidates


class ScreenScraperProvider(_Provider):
    id = "screenscraper"
    name = "ScreenScraper"

    def search(self, game, limit, state=None):
        from pkg.parity import parity_screenscraper

        platform = str(game.get("platform") or "")
        system_id = parity_screenscraper.system_id_for_platform(platform)
        rows = parity_screenscraper.search_games(
            str(game.get("name") or ""), system_id, limit=max(1, min(MAX_CANDIDATES, int(limit)))
        )
        return [
            _candidate(
                self.id,
                row.get("id"),
                row.get("name"),
                row.get("system_name") or platform,
                row.get("year"),
                {
                    "name": row.get("name"),
                    "platform": row.get("system_name") or platform,
                    "year": row.get("year"),
                },
            )
            for row in rows
        ]


class IgdbProvider(_Provider):
    id = "igdb"
    name = "IGDB"

    def search(self, game, limit, state=None):
        from pkg.parity import parity_igdb

        rows = parity_igdb.search_games(
            str(game.get("name") or ""),
            str(game.get("platform") or ""),
            limit=max(1, min(MAX_CANDIDATES, int(limit))),
        )
        return [
            _candidate(
                self.id,
                row.get("id"),
                row.get("name"),
                str(game.get("platform") or ""),
                row.get("year"),
                {
                    "name": row.get("name"),
                    "description": row.get("summary"),
                    "year": row.get("year"),
                    "genre": row.get("genres"),
                },
            )
            for row in rows
        ]


class SteamGridDbProvider(_Provider):
    id = "steamgriddb"
    name = "SteamGridDB"

    def search(self, game, limit, state=None):
        from pkg.parity import parity_steamgrid

        rows = parity_steamgrid.search_games(
            str(game.get("name") or ""), limit=max(1, min(MAX_CANDIDATES, int(limit)))
        )
        return [
            _candidate(
                self.id,
                row.get("id"),
                row.get("name"),
                str(game.get("platform") or ""),
                row.get("year"),
                {"name": row.get("name"), "year": row.get("year")},
            )
            for row in rows
        ]


BUILTIN_PROVIDERS = {
    "launchbox": LaunchBoxProvider(),
    "screenscraper": ScreenScraperProvider(),
    "igdb": IgdbProvider(),
    "steamgriddb": SteamGridDbProvider(),
}


def _enabled_plugin_provider_ids():
    """Enabled plugins that declare the metadata capability."""
    from plugins import list_plugins
    from pkg.parity.parity_plugin_storefront import default_plugins_dir

    try:
        manifests = list_plugins(default_plugins_dir())
    except (OSError, ValueError):
        return []
    provider_ids = []
    for manifest in manifests:
        if not manifest.get("valid") or not manifest.get("enabled"):
            continue
        capabilities = manifest.get("capabilities")
        if isinstance(capabilities, dict) and isinstance(capabilities.get("metadata"), dict):
            plugin_id = str(manifest.get("id") or "").strip()
            if plugin_id:
                provider_ids.append(f"plugin:{plugin_id}")
    return provider_ids


def provider_priority(state):
    settings = state.get("settings") if isinstance(state, dict) else {}
    configured = (settings or {}).get("metadata_provider_priority")
    priority = []
    if isinstance(configured, list):
        for item in configured[:20]:
            key = str(item or "").strip()
            if not key or key in priority:
                continue
            if key in BUILTIN_PROVIDERS or key.startswith("plugin:"):
                priority.append(key)
    for key in BUILTIN_PROVIDER_ORDER:
        if key not in priority:
            priority.append(key)
    for key in _enabled_plugin_provider_ids():
        if key not in priority:
            priority.append(key)
    if (settings or {}).get("plugin_metadata_priority") is True:
        priority = [key for key in priority if key.startswith("plugin:")] + [
            key for key in priority if not key.startswith("plugin:")
        ]
    return priority


def _provider_for(key):
    if isinstance(key, _Provider):
        return key
    return BUILTIN_PROVIDERS.get(str(key or "").strip())


def search_all(state, game, providers=None, limit=MAX_CANDIDATES):
    """Search providers in priority order and return normalized candidates."""
    priority = provider_priority(state)
    requested = priority if providers is None else [str(item) for item in providers]
    try:
        limit = max(1, min(MAX_CANDIDATES, int(limit)))
    except (TypeError, ValueError):
        limit = MAX_CANDIDATES
    collected = []
    for key in requested:
        provider = _provider_for(key)
        if provider is not None:
            try:
                rows = provider.search(game if isinstance(game, dict) else {}, limit, state=state) or []
            except Exception:  # noqa: BLE001 - one optional provider must not break search
                continue
            for row in rows:
                normalized = normalize_candidate(row, provider.id)
                if normalized is not None:
                    collected.append(normalized)
            continue
        if not key.startswith("plugin:"):
            continue
        plugin_id = key.split(":", 2)[1]
        try:
            from pkg.parity.parity_plugin_providers import plugin_metadata_search

            rows = plugin_metadata_search(plugin_id, game if isinstance(game, dict) else {}, limit=limit)
        except Exception:  # noqa: BLE001 - one optional provider must not break search
            continue
        for row in rows:
            normalized = normalize_candidate(row, key)
            if normalized is None:
                continue
            normalized["plugin_id"] = plugin_id
            normalized["source_name"] = row.get("source") or plugin_id
            collected.append(normalized)
    title = str((game or {}).get("name") or "")
    platform = str((game or {}).get("platform") or "")
    return rank_candidates(collected, title, platform, limit=limit)


def apply_candidate(game, candidate, *, overwrite=False, local_media=None):
    """Apply known metadata fields and already-downloaded media paths."""
    if not isinstance(game, dict) or not isinstance(candidate, dict):
        raise ValueError("Invalid metadata candidate.")
    source = str(candidate.get("source") or "").strip()
    if source in BUILTIN_PROVIDER_ORDER or source.startswith("plugin:"):
        # Provenance for the bulk media queue: this game has a usable provider
        # even when every applied field already had a local value.
        game["metadata_provider"] = source
    fields = candidate.get("fields")
    if not isinstance(fields, dict):
        fields = {key: candidate.get(key) for key in APPLY_FIELDS}
    for key in APPLY_FIELDS:
        value = fields.get(key)
        if value in (None, "", []):
            continue
        if not overwrite and game.get(key) not in (None, "", []):
            continue
        game[key] = value
    if isinstance(local_media, dict):
        for kind, path in local_media.items():
            kind = str(kind or "")
            values = path if isinstance(path, (list, tuple)) else [path]
            values = [str(value or "").strip() for value in values]
            values = [value for value in values if value]
            if not values or kind == "video_recording":
                continue
            if kind == "screenshots":
                screenshots = game.get("screenshots")
                if not isinstance(screenshots, list):
                    screenshots = []
                    game["screenshots"] = screenshots
                for value in values:
                    if value not in screenshots:
                        screenshots.append(value)
                continue
            value = values[0]
            try:
                from pkg.state.media_probe import MEDIA_PATH_FIELDS

                allowed = MEDIA_PATH_FIELDS
            except ImportError:
                allowed = set()
            if kind not in allowed:
                continue
            if overwrite or not game.get(kind):
                game[kind] = value
    return game


def provider_kind_hint(source, kind):
    """Return the provider-native media selector rows for a new media kind."""
    return tuple(MEDIA_TYPES_BY_SOURCE.get(str(source or ""), {}).get(str(kind or ""), ()))


def _download_limits(kind):
    if kind in VIDEO_MEDIA_KINDS:
        return VIDEO_MAX_BYTES, ("video/",)
    if kind in MUSIC_MEDIA_KINDS:
        return MUSIC_MAX_BYTES, ("audio/", "application/ogg")
    if kind in MANUAL_MEDIA_KINDS:
        return MANUAL_MAX_BYTES, ("application/pdf",)
    return IMAGE_MAX_BYTES, ("image/",)


def download_candidate_media(
    candidate,
    kind,
    destination_root,
    *,
    filename=None,
    opener=urlopen,
    resolver=socket.getaddrinfo,
    timeout=30,
):
    """Download a candidate URL into ``destination_root`` through download_file."""
    if isinstance(candidate, dict):
        url = str(candidate.get("url") or candidate.get("source_url") or "").strip()
        kind = str(candidate.get("kind") or kind or "")
    else:
        url = str(candidate or "").strip()
    kind = str(kind or "").strip()
    safe = clean_media_url(url, resolver=resolver)
    if not safe:
        raise ValueError("Only public HTTPS media URLs are supported.")
    root = Path(destination_root).expanduser()
    name = filename or f"{re.sub(r'[^a-z0-9_-]+', '-', kind.casefold()).strip('-') or 'media'}.bin"
    destination = root / Path(name).name
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
