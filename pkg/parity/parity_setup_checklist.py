"""Per-platform setup checklist projection (F6).

Replaces "why doesn't PS2 work" with a green/red card per platform. The four
checks come from data the app already owns:

* ``bios`` — adapter-declared BIOS path + SHA1 (ADR 0018) with a fallback to
  the discovery hints in ``parity_import.bios_hints``.
* ``emulator`` — an adapter in the emulator def registry for the platform.
* ``launch`` — at least one game on the platform has an existing path and a
  resolved adapter or a custom launch command.
* ``artwork`` — at least one game on the platform has a cover.

The module is a pure projection: it never writes state, never launches, and
never touches the network.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from pkg.parity import parity_emulator_defs as defs

CHECK_ORDER = ("bios", "emulator", "launch", "artwork")

# Platforms that need a system BIOS or firmware before any game can boot.
# Most specific names first: "playstation" is a substring of "playstation 2".
_BIOS_PLATFORM_HINTS = (
    (("playstation 3", "ps3"), "RPCS3"),
    (("playstation 2", "ps2"), "PCSX2"),
    (("psx", "ps1", "playstation 1", "playstation"), "DuckStation"),
)

_ARTWORK_FIELDS = ("cover", "hero", "background", "clear_logo", "logo", "icon", "banner")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _file_sha1(path: Path) -> str | None:
    try:
        digest = hashlib.sha1()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(8192), b""):
                digest.update(chunk)
        return digest.hexdigest().lower()
    except OSError:
        return None


def _bios_emulator(platform: str) -> str:
    name = str(platform or "").strip().casefold()
    for aliases, emulator in _BIOS_PLATFORM_HINTS:
        if any(alias in name for alias in aliases):
            return emulator
    return ""


def _bios_check(adapters: list[dict], platform: str, hint_loader=None) -> dict:
    """Return the BIOS check for a platform.

    Adapter defs own the strict path: a declared ``bios_sha1`` that no longer
    matches the file is reported as drift, not as a generic failure. When no
    def declares a BIOS, fall back to the emulator discovery hints; an
    unknown-hash presence is reported honestly as a warning.
    """
    declared = [adapter for adapter in adapters if adapter.get("bios_path")]
    for adapter in declared:
        candidate = Path(str(adapter.get("bios_path"))).expanduser()
        expected = str(adapter.get("bios_sha1") or "").strip().lower()
        if candidate.is_file():
            if expected:
                actual = _file_sha1(candidate)
                if actual != expected:
                    return {
                        "id": "bios",
                        "state": "fail",
                        "detail": f"BIOS SHA1 drift for {adapter.get('label') or adapter.get('adapter_id')}.",
                        "path": str(candidate),
                        "sha1_drift": True,
                    }
            return {
                "id": "bios",
                "state": "ok",
                "detail": f"BIOS present for {adapter.get('label') or adapter.get('adapter_id')}.",
                "path": str(candidate),
                "sha1_drift": False,
            }
        if candidate.is_dir():
            try:
                present = next((child for child in candidate.iterdir() if child.is_file()), None)
            except OSError:
                present = None
            if present is not None:
                return {
                    "id": "bios",
                    "state": "warn" if not expected else "fail",
                    "detail": f"BIOS folder has files but no SHA1 to verify: {candidate}",
                    "path": str(candidate),
                    "sha1_drift": None,
                }
        return {
            "id": "bios",
            "state": "fail",
            "detail": f"BIOS is missing for {adapter.get('label') or adapter.get('adapter_id')}.",
            "path": str(candidate),
            "sha1_drift": None,
        }

    emulator = _bios_emulator(platform)
    if not emulator:
        return {"id": "bios", "state": "skip", "detail": "No BIOS requirement declared.", "sha1_drift": None}
    hints = hint_loader() if hint_loader else {}
    for _label, directory in hints.get(emulator, []):
        candidate = Path(str(directory)).expanduser()
        try:
            found = candidate.is_file() or any(child.is_file() for child in candidate.iterdir())
        except OSError:
            found = False
        if found:
            return {
                "id": "bios",
                "state": "warn",
                "detail": f"{emulator} BIOS found at {candidate}; no SHA1 baseline to verify.",
                "path": str(candidate),
                "sha1_drift": None,
            }
    return {
        "id": "bios",
        "state": "fail",
        "detail": f"No {emulator} BIOS found in the known locations.",
        "sha1_drift": None,
    }


def _emulator_check(adapters: list[dict], games: list[dict]) -> dict:
    custom = [game for game in games if str(game.get("launch") or "").strip()]
    if adapters:
        labels = ", ".join(str(adapter.get("label") or adapter.get("adapter_id")) for adapter in adapters[:3])
        return {"id": "emulator", "state": "ok", "detail": f"Adapter resolved: {labels}", "adapters": [adapter.get("adapter_id") for adapter in adapters]}
    if custom:
        return {"id": "emulator", "state": "ok", "detail": f"Custom launch command on {len(custom)} game(s)."}
    return {"id": "emulator", "state": "fail", "detail": "No emulator adapter or launch command resolved for this platform."}


def _launch_check(games: list[dict], adapters: list[dict]) -> dict:
    adapter_ids = {str(adapter.get("adapter_id") or "") for adapter in adapters}
    launchable = 0
    for game in games:
        path = str(game.get("path") or "").strip()
        if path and not Path(path).expanduser().exists():
            continue
        if str(game.get("emulator_adapter_id") or "") in adapter_ids or str(game.get("launch") or "").strip():
            launchable += 1
    if launchable:
        return {"id": "launch", "state": "ok", "detail": f"{launchable} of {len(games)} game(s) are launchable.", "launchable": launchable, "games": len(games)}
    return {"id": "launch", "state": "fail", "detail": f"No launchable game among {len(games)} game(s).", "launchable": 0, "games": len(games)}


def _artwork_check(games: list[dict]) -> dict:
    with_cover = sum(1 for game in games if str(game.get("cover") or "").strip())
    with_any = sum(1 for game in games if any(str(game.get(field) or "").strip() for field in _ARTWORK_FIELDS))
    if with_cover == len(games) and games:
        return {"id": "artwork", "state": "ok", "detail": "Artwork fetched for every game.", "covers": with_cover, "games": len(games)}
    if with_cover or with_any:
        return {"id": "artwork", "state": "warn", "detail": f"Artwork for {with_any} of {len(games)} game(s); {with_cover} have covers.", "covers": with_cover, "games": len(games)}
    return {"id": "artwork", "state": "fail", "detail": "No artwork fetched for this platform.", "covers": 0, "games": len(games)}


def _aggregate(checks: list[dict]) -> str:
    states = {check["state"] for check in checks}
    if "fail" in states:
        return "red"
    if "warn" in states:
        return "yellow"
    return "green"


def build_checklists(state, *, which=None, registry=None, hint_loader=None) -> dict:
    """Build the per-platform checklist payload from a library state dict."""
    games = [game for game in (state or {}).get("games", []) if isinstance(game, dict)]
    if registry is None:
        registry = defs.load_registry(defs.DEFS_DIR, health=False, which=which)
    adapters = [adapter for adapter in registry.get("adapters", []) if isinstance(adapter, dict)]
    by_platform: dict[str, list[dict]] = {}
    for game in games:
        platform = str(game.get("platform") or "Unassigned").strip() or "Unassigned"
        by_platform.setdefault(platform, []).append(game)

    platforms = []
    for platform in sorted(by_platform, key=str.casefold):
        platform_games = by_platform[platform]
        platform_adapters = [adapter for adapter in adapters if str(adapter.get("platform") or "") == platform]
        checks = [
            _bios_check(platform_adapters, platform, hint_loader=hint_loader),
            _emulator_check(platform_adapters, platform_games),
            _launch_check(platform_games, platform_adapters),
            _artwork_check(platform_games),
        ]
        ready = sum(1 for check in checks if check["state"] in {"ok", "skip"})
        platforms.append({
            "platform": platform,
            "games": len(platform_games),
            "status": _aggregate(checks),
            "ready": ready,
            "total": len(checks),
            "checks": {check["id"]: check for check in checks},
            "adapter_ids": [adapter.get("adapter_id") for adapter in platform_adapters],
        })
    summary = {
        "green": sum(1 for item in platforms if item["status"] == "green"),
        "yellow": sum(1 for item in platforms if item["status"] == "yellow"),
        "red": sum(1 for item in platforms if item["status"] == "red"),
    }
    return {"generated_at": _utc_now(), "platforms": platforms, "summary": summary}


def checklist_for_platform(state, platform, **kwargs):
    """Return one platform card (or None) for tests and deep links."""
    for item in build_checklists(state, **kwargs)["platforms"]:
        if item["platform"] == platform:
            return item
    return None
