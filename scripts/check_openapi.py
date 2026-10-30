#!/usr/bin/env python3
"""OpenAPI gate (plan W2-08).

Checks the generated document against the live registry and the typed schemas:

  - the document is fresh: regenerating it changes nothing,
  - every public route is listed, and no operation is listed twice,
  - every typed operation names a schema the document publishes,
  - every published schema comes from scripts/api_schemas.py.

An operation without a schema stays untyped on purpose: a guessed schema is worse than an
honest gap.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import api_schemas  # noqa: E402
import gen_openapi  # noqa: E402


def main() -> int:
    problems: list[str] = []
    document = gen_openapi.build()
    on_disk = json.loads(gen_openapi.OUTPUT.read_text(encoding="utf-8"))
    if on_disk != document:
        problems.append("docs/openapi.json is stale; run scripts/gen_openapi.py")

    listed: set[tuple[str, str]] = set()
    for path, operations in document["paths"].items():
        for method in operations:
            key = (method.upper(), path)
            if key in listed:
                problems.append(f"{method.upper()} {path} is listed twice")
            listed.add(key)

    live = set(gen_openapi.live_operations())
    for method, path in sorted(live - listed):
        problems.append(f"{method} {path} is a live route but is not in the document")
    for method, path in sorted(listed - live):
        problems.append(f"{method} {path} is in the document but is not a live route")

    published = document["components"]["schemas"]
    for name in api_schemas.SCHEMAS:
        if name not in published:
            problems.append(f"schema {name} is not published in the document")
    for name in published:
        if name != "Problem" and name not in api_schemas.SCHEMAS:
            problems.append(f"schema {name} is published but is not in scripts/api_schemas.py")

    for (method, path), (name, _description) in api_schemas.OPERATIONS.items():
        if (method, path) not in live:
            problems.append(f"{method} {path} is typed but is not a live route")
        if name not in published:
            problems.append(f"{method} {path} is typed with {name}, which is not published")

    if problems:
        print("FAIL: OpenAPI contract")
        for problem in problems[:40]:
            print("  " + problem)
        return 1
    print(
        f"OpenAPI contract OK: {len(document['paths'])} paths, "
        f"{len(api_schemas.SCHEMAS)} schemas, {len(api_schemas.OPERATIONS)} typed operations"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
