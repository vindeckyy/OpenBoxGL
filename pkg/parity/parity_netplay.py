"""RetroArch netplay lobby browser (P12.1 / LaunchBox G3.40).

Read-only convenience browser over RetroArch's public lobby listing. The
service is public and HTTPS-only; no credentials are sent and nothing is
launched from here — RetroArch itself handles the connection once the user
copies the address.

Every failure (timeout, HTTP error, malformed JSON, no rooms) is returned as
``{"available": False, "error": "Netplay lobby is unavailable."}``; a UI
request must never raise. Successful, non-empty results are cached in-process
for ``LOBBY_CACHE_SECONDS``; ``reset_cache()`` exists for tests.
"""

from __future__ import annotations

import ipaddress
import json
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.request import urlopen

LOBBY_URL = "https://lobby.retroarch.com/api/lobby"
LOBBY_TIMEOUT = 10
LOBBY_CACHE_SECONDS = 60
LOBBY_MAX_ROWS = 500
LOBBY_MAX_BYTES = 8 * 1024 * 1024
LOBBY_ERROR = "Netplay lobby is unavailable."
MAX_HOST_LENGTH = 253
MAX_PLAYERS = 10_000
TEXT_LIMITS = {"nick": 80, "game": 200, "core": 100}

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_HOSTNAME_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")

# (expires_at_monotonic, rows). Only successful, non-empty listings are cached.
_CACHE: tuple[float, tuple[dict[str, Any], ...]] | None = None


def reset_cache() -> None:
    """Drop the in-process lobby cache (used by tests and manual refresh)."""
    global _CACHE
    _CACHE = None


def _clock(now: Callable[[], float] | float | None = None) -> float:
    if now is None:
        return time.monotonic()
    if callable(now):
        return float(now())
    return float(now)


def _text(value: Any, limit: int) -> str:
    if value is None or isinstance(value, bool):
        return ""
    text = _CONTROL_CHARS.sub("", str(value)).strip()
    return text[:limit]


def _is_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    if isinstance(value, float):
        return value == 1.0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true"}
    return False


def _players(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    elif isinstance(value, str) and value.strip().lstrip("+").isdigit():
        number = int(value.strip())
    else:
        return 0
    return number if 0 <= number <= MAX_PLAYERS else 0


def _valid_host(value: Any) -> str:
    host = _text(value, MAX_HOST_LENGTH)
    if not host or len(host) > MAX_HOST_LENGTH:
        return ""
    if any(char.isspace() for char in host):
        return ""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    labels = host.split(".")
    if not labels or any(not _HOSTNAME_LABEL.match(label) for label in labels):
        return ""
    return host


def _valid_port(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        port = value
    elif isinstance(value, float) and value.is_integer():
        port = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        port = int(value.strip())
    else:
        return 0
    return port if 1 <= port <= 65535 else 0


def _row(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    fields = entry.get("fields")
    if not isinstance(fields, dict):
        return None
    if not _is_true(fields.get("connectable")):
        return None
    host = _valid_host(fields.get("mitm_ip") or fields.get("ip"))
    if not host:
        return None
    port = _valid_port(fields.get("mitm_port") or fields.get("port"))
    if not port:
        return None
    return {
        "host": host,
        "port": port,
        "nick": _text(fields.get("username"), TEXT_LIMITS["nick"]),
        "game": _text(fields.get("game_name"), TEXT_LIMITS["game"]),
        "core": _text(fields.get("core_name"), TEXT_LIMITS["core"]),
        "players": _players(fields.get("player_count")),
        "private": _is_true(fields.get("has_password")),
    }


def _rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    rows: list[dict[str, Any]] = []
    for entry in payload:
        row = _row(entry)
        if row is None:
            continue
        rows.append(row)
        if len(rows) >= LOBBY_MAX_ROWS:
            break
    return rows


def _decode(response: Any) -> Any:
    try:
        raw = response.read(LOBBY_MAX_BYTES + 1)
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    if isinstance(raw, str):
        text = raw
    else:
        text = bytes(raw or b"").decode("utf-8", "replace")
    if len(text) > LOBBY_MAX_BYTES:
        raise ValueError("lobby listing too large")
    return json.loads(text)


def lobby(opener: Callable[..., Any] = urlopen, now: Callable[[], float] | float | None = None) -> dict[str, Any]:
    """Return ``{available, rows}`` for the public RetroArch lobby list."""
    global _CACHE
    current = _clock(now)
    if _CACHE is not None and _CACHE[0] > current:
        return {"available": True, "rows": [dict(row) for row in _CACHE[1]]}
    try:
        response = opener(LOBBY_URL, timeout=LOBBY_TIMEOUT)
        payload = _decode(response)
        rows = _rows(payload)
    except Exception:  # noqa: BLE001 - a UI request must never raise
        return {"available": False, "error": LOBBY_ERROR}
    if not rows:
        return {"available": False, "error": LOBBY_ERROR}
    _CACHE = (current + LOBBY_CACHE_SECONDS, tuple(rows))
    return {"available": True, "rows": [dict(row) for row in rows]}
