#!/usr/bin/env python3
"""Generate docs/openapi.json from the live public route surface (ADR 0091).

Every public operation (``/api/v2/*`` in the route tables and registry) becomes an
OpenAPI path. Operations with a hand-written response schema in ``TYPED`` carry it;
every other operation is marked ``x-openbox-schema: untyped`` so readers know its
shape is not pinned. Host-channel and unversioned legacy paths are never listed.

Usage: ``python3 scripts/gen_openapi.py [--update]``. Without ``--update`` it prints
a diff summary and exits 1 when the committed document is stale.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "openapi.json"
sys.path.insert(0, str(ROOT))

import api_schemas  # noqa: E402

PROBLEM_SCHEMA = api_schemas.SCHEMAS["Problem"]

# Hand-authored response schemas for the operations most clients depend on. Keep this
# small and accurate; an operation not listed here is untyped, never guessed.
TYPED = {
    (method, path): {"description": description, "schema": {"$ref": f"#/components/schemas/{name}"}}
    for (method, path), (name, description) in api_schemas.OPERATIONS.items()
}


def live_operations() -> list[tuple[str, str]]:
    from routes import GET_TABLE, POST_TABLE
    from routes.registry import all_routes

    operations: set[tuple[str, str]] = set()
    for table, method in ((GET_TABLE, "GET"), (POST_TABLE, "POST")):
        operations.update((method, path) for path in table if path.startswith("/api/v2/"))
    for route in all_routes():
        if route.visibility == "public":
            operations.add((route.method, route.path))
    return sorted(operations, key=lambda item: (item[1], item[0]))


def build() -> dict:
    from updates import VERSION

    paths: dict[str, dict] = {}
    for method, path in live_operations():
        typed = TYPED.get((method, path))
        success = {
            "description": typed["description"] if typed else "Response. The schema is not pinned for this operation.",
            "content": {
                "application/json": {
                    "schema": typed["schema"] if typed else {"x-openbox-schema": "untyped"},
                }
            },
        }
        operation = {
            "operationId": f"{method.lower()}{path.replace('/', '_').replace('-', '_')}",
            "summary": f"{method} {path}",
            "tags": [path.split("/")[3] if path.count("/") >= 3 else "root"],
            "security": [{"openboxToken": []}],
            "responses": {
                "200": success,
                "default": {"$ref": "#/components/responses/Problem"},
            },
        }
        if not typed:
            operation["x-openbox-schema"] = "untyped"
        paths.setdefault(path, {})[method.lower()] = operation
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "OpenBox local API",
            "version": VERSION,
            "description": (
                "Public HTTP surface of the local OpenBox server (/api/v2). Native-host calls live under "
                "/api/host and are not documented here. Errors are application/problem+json (RFC 9457 shape)."
            ),
            "license": {"name": "See LICENSE in the repository"},
        },
        "servers": [{"url": "http://127.0.0.1:{port}", "variables": {"port": {"default": "8765"}}}],
        "components": {
            "securitySchemes": {
                "openboxToken": {"type": "apiKey", "in": "header", "name": "X-OpenBox-Token"},
            },
            "schemas": {"Problem": PROBLEM_SCHEMA, **api_schemas.SCHEMAS},
            "responses": {
                "Problem": {
                    "description": "An error. Body is an RFC 9457-shaped problem document.",
                    "content": {"application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}},
                }
            },
        },
        "paths": dict(sorted(paths.items())),
    }


def main(argv: list[str]) -> int:
    document = build()
    rendered = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    if "--update" in argv:
        OUTPUT.write_text(rendered, encoding="utf-8")
        print(f"Wrote {OUTPUT.relative_to(ROOT)}: {len(document['paths'])} paths")
        return 0
    committed = OUTPUT.read_text(encoding="utf-8") if OUTPUT.is_file() else ""
    if committed != rendered:
        print("FAIL: docs/openapi.json is stale; regenerate with scripts/gen_openapi.py --update")
        return 1
    print(f"openapi OK: {len(document['paths'])} public paths")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
