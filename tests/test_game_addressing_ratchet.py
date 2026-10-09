#!/usr/bin/env python3
"""Ratchets for the two ways a stale client can address the wrong game.

1. Frontend: a game-addressed request sends only the array index ``id``. An
   index taken before a delete, trash or sync points at another game once the
   library shifts. Requests should also send the stable ``game_id``.
2. Backend: a bare ``datetime.fromisoformat`` outside ``pkg/platform_compat``
   fails on offset-stamped values. Use ``parse_timestamp``.

Both allowances are measured counts that must match exactly. A new occurrence
fails the gate, and fixing one fails it too until its allowance is lowered.
"""

import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Measured on 2026-10-08. Lower these as call sites migrate; never raise them.
ALLOWED_ID_ONLY_GAME_CALLS = 0
ALLOWED_BARE_FROMISOFORMAT = 29

GAME_ROUTE_RE = re.compile(
    r"/api/(favorite|game\b|v2/library/trash|screenshot|extra/launch|ra/game|saves/|metadata/|storefront/|library/)"
)
API_CALL_RE = re.compile(r"""api\(\s*(['"`])(/api/[^'"`]+)\1[^;]*?JSON\.stringify\(\{\s*id\s*[,:}]([^;]*)""", re.S)


def id_only_game_calls():
    rows = []
    for path in sorted((ROOT / "static").glob("*.js")):
        source = path.read_text(encoding="utf-8")
        for match in API_CALL_RE.finditer(source):
            route, body = match.group(2), match.group(3)[:200]
            if GAME_ROUTE_RE.search(route) and "game_id" not in body:
                rows.append((path.name, route))
    return rows


def bare_fromisoformat_calls():
    rows = []
    listed = [line.strip() for line in (ROOT / "runtime_modules.txt").read_text(encoding="utf-8").splitlines()]
    for entry in listed:
        if not entry or entry.startswith("#") or not entry.endswith(".py") or entry == "pkg/platform_compat.py":
            continue
        path = ROOT / entry
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=entry)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "fromisoformat"):
                rows.append((entry, node.lineno))
    return rows


class GameAddressingRatchetTests(unittest.TestCase):
    def test_id_only_game_requests_match_the_allowance_exactly(self):
        rows = id_only_game_calls()
        if len(rows) > ALLOWED_ID_ONLY_GAME_CALLS:
            self.fail(f"{len(rows)} id-only game requests, {ALLOWED_ID_ONLY_GAME_CALLS} allowed; "
                      f"send game_id with id: {rows}")
        if len(rows) < ALLOWED_ID_ONLY_GAME_CALLS:
            self.fail(f"now {len(rows)} id-only game requests; lower ALLOWED_ID_ONLY_GAME_CALLS to match")

    def test_bare_fromisoformat_matches_the_allowance_exactly(self):
        rows = bare_fromisoformat_calls()
        if len(rows) > ALLOWED_BARE_FROMISOFORMAT:
            self.fail(f"{len(rows)} bare fromisoformat calls, {ALLOWED_BARE_FROMISOFORMAT} allowed; "
                      f"use pkg.platform_compat.parse_timestamp: {rows[-5:]}")
        if len(rows) < ALLOWED_BARE_FROMISOFORMAT:
            self.fail(f"now {len(rows)} bare fromisoformat calls; lower ALLOWED_BARE_FROMISOFORMAT to match")


if __name__ == "__main__":
    sys.exit(unittest.main())
