"""Stats and achievement provider capability plugins (P10.3).

A plugin returns playtime/last-played/achievement values for library games;
the host validates and caches them per plugin under ``<data>/plugin-stats``.
Merging is monotonic: a plugin value never lowers local playtime or replaces
a newer local ``last_played``. Achievement sources are namespaced
``plugin:<plugin_id>:<source>`` and may not impersonate a built-in source.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re

from backend_io import atomic_write_text
from pkg.parity.parity_plugin_storefront import plugin_manifest, require_network_grant, require_plugin_capability
from pkg.platform_compat import parse_timestamp as parse_iso_timestamp
from plugins import run_plugin_hook

LOGGER = logging.getLogger("openbox.plugins.stats")

STATS_DIRNAME = "plugin-stats"
MAX_CACHE_BYTES = 10 * 1024 * 1024
MAX_GAMES = 500
MAX_ITEMS = 500
MAX_ITEMS_TOTAL = 2000
MAX_TOTAL = 10_000
MAX_PLAYTIME = 1_000_000_000
MAX_TEXT = 2000
RESERVED_SOURCES = frozenset({"retroachievements", "steam"})
_SOURCE_RE = re.compile(r"[^a-z0-9._-]+")


def default_data_dir():
    """Return the data directory that owns ``plugins/`` and ``plugin-stats/``."""
    from openbox import DATA

    return Path(DATA).parent


def stats_dir(data_dir=None):
    return Path(data_dir or default_data_dir()) / STATS_DIRNAME


def _enabled_plugin_ids(plugins_dir):
    """Enabled, valid plugin ids; an absent plugin directory enables none."""
    root = Path(plugins_dir)
    if not root.is_dir():
        return set()
    try:
        from plugins import list_plugins

        return {
            str(manifest.get("id") or "")
            for manifest in list_plugins(root)
            if manifest.get("valid") and manifest.get("enabled")
        }
    except (OSError, ValueError):
        return set()


def cache_path(plugin_id, data_dir=None):
    return stats_dir(data_dir) / f"{plugin_id}.json"


# ---------------------------------------------------------------------------
# Value validation
# ---------------------------------------------------------------------------

def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_timestamp(value):
    """Parse an ISO-8601 timestamp (with optional ``Z``); return ``None`` when invalid."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 64:
        return None
    parsed = parse_iso_timestamp(text)
    if parsed is None:
        return None
    return parsed.astimezone(timezone.utc)


def _bounded_int(value, low, high, label):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer.")
    if not low <= value <= high:
        raise ValueError(f"{label} is out of range.")
    return value


def _source_slug(value):
    cleaned = _SOURCE_RE.sub("-", str(value or "").strip().casefold()).strip("-")
    return cleaned[:64]


def _clean_item(raw):
    if not isinstance(raw, dict):
        return None
    item_id = raw.get("id")
    title = raw.get("title")
    if isinstance(item_id, (int, float)) and not isinstance(item_id, bool):
        item_id = str(int(item_id))
    if not isinstance(item_id, str) or not item_id.strip():
        return None
    if not isinstance(title, str) or not title.strip():
        return None
    item = {
        "id": item_id.strip()[:160],
        "title": title.strip()[:300],
        "earned": bool(raw.get("earned")),
    }
    description = raw.get("description")
    if isinstance(description, str) and description.strip():
        item["description"] = description.strip()[:MAX_TEXT]
    points = raw.get("points")
    if isinstance(points, (int, float)) and not isinstance(points, bool):
        item["points"] = max(0, min(100_000, int(points)))
    icon = raw.get("icon_url")
    if isinstance(icon, str) and icon.strip().startswith("https://") and len(icon.strip()) <= 2000:
        item["icon_url"] = icon.strip()
    return item


def _clean_source(raw, plugin_id):
    """Validate one achievement source; return ``(key, summary)`` or ``None``."""
    if not isinstance(raw, dict):
        return None
    slug = _source_slug(raw.get("source") or raw.get("id"))
    if not slug:
        return None
    if slug in RESERVED_SOURCES:
        raise ValueError(f"Achievement source {slug!r} is reserved for the built-in provider.")
    earned = _bounded_int(raw.get("earned", 0), 0, MAX_TOTAL, "Achievement earned count")
    total = _bounded_int(raw.get("total", 0), 0, MAX_TOTAL, "Achievement total count")
    if earned > total:
        raise ValueError("Achievement earned count cannot exceed the total.")
    if total == 0:
        return None
    items = []
    raw_items = raw.get("items")
    if isinstance(raw_items, list):
        for entry in raw_items[:MAX_ITEMS]:
            item = _clean_item(entry)
            if item is not None:
                items.append(item)
    summary = {
        "name": str(raw.get("name") or slug).strip()[:80] or slug,
        "earned": earned,
        "total": total,
        "progress_pct": round(earned / total * 100.0, 1),
        "items": items,
    }
    key = f"plugin:{plugin_id}:{slug}"[:160]
    return key, summary


def _clean_achievements(raw, plugin_id):
    """Accept one source object or a list of source objects."""
    values = raw if isinstance(raw, list) else [raw]
    sources = {}
    for value in values[:8]:
        cleaned = _clean_source(value, plugin_id)
        if cleaned is not None:
            key, summary = cleaned
            sources[key] = summary
    return sources


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------

def _game_rows(state):
    games = state.get("games") if isinstance(state, dict) else None
    if not isinstance(games, list):
        return [], {}
    rows, known = [], {}
    for game in games:
        if not isinstance(game, dict):
            continue
        game_id = str(game.get("game_id") or "").strip()[:160]
        if not game_id:
            continue
        known.setdefault(game_id, True)
        row = {
            "game_id": game_id,
            "name": str(game.get("name") or "")[:300],
            "platform": str(game.get("platform") or "")[:80],
        }
        for key in ("steam_app_id", "gog_id", "ra_game_id", "source"):
            value = game.get(key)
            if value not in (None, ""):
                row[key] = str(value)[:120]
        playtime = game.get("playtime_seconds")
        if isinstance(playtime, (int, float)) and not isinstance(playtime, bool):
            row["playtime_seconds"] = int(playtime)
        rows.append(row)
        if len(rows) >= MAX_GAMES:
            break
    return rows, known


def _merge_stats(collection, game_id, playtime, last_played, sources):
    entry = collection.setdefault(game_id, {})
    if playtime is not None:
        entry["playtime_seconds"] = max(int(entry.get("playtime_seconds", 0) or 0), playtime)
    if last_played:
        existing = parse_timestamp(entry.get("last_played"))
        candidate = parse_timestamp(last_played)
        if existing is None or (candidate is not None and candidate > existing):
            entry["last_played"] = last_played
    if sources:
        entry.setdefault("sources", {}).update(sources)


def refresh_plugin_stats(plugin_id, state, *, data_dir=None, plugins_dir=None, timeout=None):
    """Fetch and cache one stats provider; return its source summaries."""
    data_root = Path(data_dir or default_data_dir())
    plugin_root = Path(plugins_dir or (data_root / "plugins"))
    manifest = plugin_manifest(plugin_root, plugin_id)
    require_plugin_capability(manifest, "stats")
    require_network_grant(manifest, plugin_root)
    rows, known = _game_rows(state)
    payload = {"action": "fetch", "games": rows}
    try:
        result, error = run_plugin_hook(plugin_root, manifest["id"], "stats", payload, timeout=timeout)
    except Exception as hook_error:
        raise ValueError(f"Plugin stats failed: {hook_error}") from hook_error
    if error:
        raise ValueError(f"Plugin stats failed: {error}")
    if not isinstance(result, dict):
        raise ValueError("Plugin stats must return a JSON object.")
    game_rows = result.get("games")
    if not isinstance(game_rows, list):
        raise ValueError("Plugin stats must return {games: [...]}.")

    collection = {}
    source_games = {}
    source_names = {}
    for raw in game_rows[:MAX_GAMES]:
        if not isinstance(raw, dict):
            continue
        game_id = str(raw.get("game_id") or "").strip()
        if not game_id or game_id not in known:
            continue
        playtime = None
        if "playtime_seconds" in raw and raw.get("playtime_seconds") is not None:
            playtime = _bounded_int(raw.get("playtime_seconds"), 0, MAX_PLAYTIME, "Playtime")
        last_played = ""
        if raw.get("last_played") not in (None, ""):
            candidate = raw.get("last_played")
            if not isinstance(candidate, str) or parse_timestamp(candidate) is None:
                raise ValueError("last_played must be an ISO-8601 timestamp.")
            last_played = candidate.strip()[:64]
        sources = {}
        if raw.get("achievements") is not None:
            sources = _clean_achievements(raw.get("achievements"), manifest["id"])
        for key, summary in sources.items():
            source_games.setdefault(key, set()).add(game_id)
            source_names[key] = summary["name"]
        _merge_stats(collection, game_id, playtime, last_played, sources)

    updated_at = _now()
    source_counts = {
        key: {"name": source_names.get(key, key), "games": len(ids)}
        for key, ids in source_games.items()
    }
    _trim_items(collection)
    payload = {
        "plugin_id": manifest["id"],
        "updated_at": updated_at,
        "sources": source_counts,
        "games": collection,
    }
    encoded = _bounded_payload(payload)
    atomic_write_text(cache_path(manifest["id"], data_root), encoded, mode=0o600)
    return [
        {
            "source": key,
            "name": value.get("name", key),
            "games": int(value.get("games", 0)),
            "updated_at": updated_at,
        }
        for key, value in sorted(source_counts.items())
    ]


def _trim_items(collection, budget=MAX_ITEMS_TOTAL):
    """Cap total achievement items across the cache (per-source cap is 500)."""
    remaining = budget
    for entry in collection.values():
        if not isinstance(entry, dict):
            continue
        for summary in (entry.get("sources") or {}).values():
            if not isinstance(summary, dict):
                continue
            items = summary.get("items")
            if not isinstance(items, list):
                continue
            if remaining <= 0:
                summary["items"] = []
                continue
            summary["items"] = items[:remaining]
            remaining -= len(summary["items"])


def _bounded_payload(payload):
    """Serialize, dropping achievement items before refusing an oversized cache."""
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= MAX_CACHE_BYTES:
        return encoded
    for entry in payload["games"].values():
        for summary in (entry.get("sources") or {}).values():
            if isinstance(summary, dict):
                summary["items"] = []
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= MAX_CACHE_BYTES:
        return encoded
    raise ValueError("Plugin stats cache is too large.")


# ---------------------------------------------------------------------------
# Cache loading
# ---------------------------------------------------------------------------

def _cached_summary(key, raw):
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    earned = raw.get("earned")
    total = raw.get("total")
    if not isinstance(name, str) or not isinstance(earned, int) or not isinstance(total, int):
        return None
    if isinstance(earned, bool) or isinstance(total, bool):
        return None
    if not 0 <= earned <= total <= MAX_TOTAL or total == 0:
        return None
    items = raw.get("items") if isinstance(raw.get("items"), list) else []
    summary = {
        "name": name[:80],
        "earned": earned,
        "total": total,
        "progress_pct": round(earned / total * 100.0, 1),
        "items": [item for item in items[:MAX_ITEMS] if isinstance(item, dict)],
    }
    return summary


def load_plugin_stats(data_dir=None):
    """Return ``{game_id: {playtime_seconds, last_played, sources}}`` from caches."""
    merged = {}
    root = stats_dir(data_dir)
    if not root.is_dir():
        return merged
    enabled = _enabled_plugin_ids(Path(data_dir or default_data_dir()) / "plugins")
    for path in sorted(root.glob("*.json")):
        try:
            if path.stat().st_size > MAX_CACHE_BYTES:
                continue
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        plugin_id = raw.get("plugin_id")
        games = raw.get("games")
        if not isinstance(plugin_id, str) or not isinstance(games, dict):
            continue
        if plugin_id not in enabled:
            continue
        prefix = f"plugin:{plugin_id}:"
        for game_id, entry in games.items():
            if not isinstance(entry, dict) or not isinstance(game_id, str):
                continue
            projected = merged.setdefault(game_id, {
                "playtime_seconds": 0,
                "last_played": "",
                "sources": {},
            })
            playtime = entry.get("playtime_seconds")
            if isinstance(playtime, int) and not isinstance(playtime, bool) and playtime >= 0:
                projected["playtime_seconds"] = max(projected["playtime_seconds"], playtime)
            last_played = entry.get("last_played")
            if isinstance(last_played, str) and parse_timestamp(last_played):
                existing = parse_timestamp(projected["last_played"])
                candidate = parse_timestamp(last_played)
                if existing is None or candidate > existing:
                    projected["last_played"] = last_played[:64]
            sources = entry.get("sources")
            if not isinstance(sources, dict):
                continue
            for key, value in sources.items():
                if not isinstance(key, str) or not key.startswith(prefix):
                    continue
                summary = _cached_summary(key, value)
                if summary is None:
                    continue
                projected["sources"][key] = summary
    return merged


def plugin_stats_summaries(data_dir=None):
    """Return one bounded summary row per cached plugin source."""
    rows = []
    root = stats_dir(data_dir)
    if not root.is_dir():
        return rows
    enabled = _enabled_plugin_ids(Path(data_dir or default_data_dir()) / "plugins")
    for path in sorted(root.glob("*.json")):
        try:
            if path.stat().st_size > MAX_CACHE_BYTES:
                continue
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict) or not isinstance(raw.get("sources"), dict):
            continue
        plugin_id = raw.get("plugin_id")
        if plugin_id not in enabled:
            continue
        updated_at = raw.get("updated_at") if isinstance(raw.get("updated_at"), str) else ""
        for key, value in sorted(raw["sources"].items()):
            if not isinstance(key, str) or not key.startswith("plugin:") or not isinstance(value, dict):
                continue
            try:
                games = max(0, int(value.get("games") or 0))
            except (TypeError, ValueError):
                games = 0
            rows.append({
                "source": key,
                "name": str(value.get("name") or key)[:80],
                "games": games,
                "updated_at": updated_at,
            })
    return rows
