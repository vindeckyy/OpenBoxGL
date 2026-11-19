#!/usr/bin/env python3
"""Export deterministic golden fixtures from the Python reference implementation.

Writes JSON fixtures under ``tests/golden/`` so Rust unit and integration tests
assert byte-level and algorithmic equivalence against Python across:
  - ``stable_game_id`` (ASCII, Unicode, CJK, emoji surrogate pairs, storefront IDs, path normalization)
  - ``KNOWN_SETTINGS`` & ``api_errors`` problem payloads
  - ``parity_query`` ASTs & chips
  - ``parity_emulator_defs`` resolved launch plans
  - ``parity_steam_bridge`` binary VDF round-trip bytes
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "tests" / "golden"


def export_game_ids() -> dict:
    from state_store import _identity_payload, _stable_game_id

    samples = [
        {"name": "Quake", "path": "/games/quake/quake.sh", "platform": "PC"},
        {"name": "Chrono Trigger — クロノ・トリガー", "path": "/roms/SNES/クロノ・トリガー (JP).sfc", "platform": "SNES"},
        {"name": "Rocket 🚀 League", "path": "/games/🚀/rl.exe", "platform": "PC"},
        {"name": "Accented Café Éclair", "path": "/roms/GBA/Café//../Éclair/./rom.gba", "platform": "Game Boy Advance"},
        {"name": "Manual Shelf Entry", "platform": "Switch"},
        {"name": "Steam Only", "platform": "PC", "steam_app_id": "367520"},
        {"name": "GOG Native", "platform": "PC", "gog_id": "1207658924"},
        {"name": "EA Native", "platform": "PC", "ea_id": "Origin.OFR.50.0001234"},
        {"name": "Ubisoft Native", "platform": "PC", "ubisoft_id": "520"},
        {"name": "All Core Storefronts", "path": "/a/b/c", "platform": "PC", "steam_app_id": "1", "heroic_app_id": "h2", "lutris_id": "l3", "gameyfin_id": "g4", "launchbox_db_id": "lb5"},
        {"name": "Quotes & Escapes \"\\\n\t\x08\x0c", "platform": "PC"},
        {"name": "Double Leading Slash", "path": "//server/share/rom.nes", "platform": "NES"},
        {"name": "Triple Leading Slash", "path": "///var/roms/rom.nes", "platform": "NES"},
    ]
    rows = []
    for sample in samples:
        rows.append({
            "input": sample,
            "identity_payload": _identity_payload(sample),
            "game_id": _stable_game_id(sample),
        })
    return {"version": 1, "cases": rows}


def export_settings_and_errors() -> dict:
    from api_errors import (
        ApiVersionRequired,
        BadRequest,
        GameNotFound,
        HostApiUnsupported,
        HostChannelRetired,
        RouteNotFound,
        V1ApiRemoved,
        problem_payload,
    )
    from settings_schema import KNOWN_SETTINGS
    from web_app import _sanitize_error_message

    error_samples = [
        ("BadRequest", BadRequest("Invalid field")),
        ("GameNotFound", GameNotFound("No such game")),
        ("RouteNotFound", RouteNotFound("Not found")),
        ("V1ApiRemoved", V1ApiRemoved("Removed", detail={"retired": "/api/v1/x", "use": "/api/v2/x"})),
        ("ApiVersionRequired", ApiVersionRequired("Use v2")),
        ("HostChannelRetired", HostChannelRetired("Use /api/host")),
        ("HostApiUnsupported", HostApiUnsupported("Bad host version")),
    ]
    sanitize_inputs = [
        "Normal error message",
        "Permission denied: /home/alice/secret/library.json",
        "Failed reading ~/games/rom.iso",
        "Cannot open C:\\Users\\Alice\\Games\\foo.exe",
        "UNC failure \\\\server\\share\\file.bin",
    ]
    return {
        "known_settings": sorted(KNOWN_SETTINGS),
        "problems": [
            {
                "name": name,
                "status": err.status,
                "payload": problem_payload(err.status, err.to_payload("deadbeef"), request_id="deadbeef", instance="/api/v2/test"),
            }
            for name, err in error_samples
        ],
        "sanitized": [{"raw": raw, "clean": _sanitize_error_message(raw)} for raw in sanitize_inputs],
    }


def export_query_grammar() -> dict:
    from pkg.parity.parity_query import parse_query

    queries = [
        "unplayed retro rpg",
        "short co-op games under 5 hours",
        "highly rated snes games from the 90s",
        "haven't played in 6 months",
        'genre:rpg -platform:pc #favorite "chrono trigger"',
        "top rated 4+ players E10+",
        "long beaten games 2015..2020",
        "just plain title search",
    ]
    return {
        "cases": [{"query": q, "parsed": parse_query(q)} for q in queries],
    }


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    artifacts = {
        "game_ids.json": export_game_ids(),
        "settings_and_errors.json": export_settings_and_errors(),
        "query_grammar.json": export_query_grammar(),
    }
    for name, payload in artifacts.items():
        path = OUT_DIR / name
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
