#!/usr/bin/env python3
"""Generate the API v1 contract page from the live route tables.

Outputs a markdown reference (one row per route, method, handler) for the
docs site. Run from the repo root:

  python3 scripts/gen_api_docs.py --out /tmp/api-v1.md
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from routes import GET_TABLE, POST_TABLE, V1_ALIASED_PREFIXES  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="api-v1.md")
    args = parser.parse_args()

    lines = [
        "# API v1 contract",
        "",
        "The v1 surface is the stable contract. Legacy `/api/*` paths stay available for older clients; new work targets v1.",
        "",
        "Authentication: every request needs the `X-OpenBox-Token` header with the per-process token from `server.token`. Responses are JSON; errors carry `error`, `code`, and `request_id`.",
        "",
        "| Method | Path | Handler |",
        "|---|---|---|",
    ]
    seen = set()
    for path in sorted(V1_ALIASED_PREFIXES):
        if path in seen:
            continue
        seen.add(path)
        v1_path = f"/api/v1{path[len('/api'):]}"
        handler = GET_TABLE.get(v1_path) or POST_TABLE.get(v1_path) or "-"
        methods = []
        if v1_path in GET_TABLE:
            methods.append("GET")
        if v1_path in POST_TABLE:
            methods.append("POST")
        lines.append(f"| {' / '.join(methods)} | `{v1_path}` | `{handler}` |")
    lines.append("")
    lines.append("Generated from `routes.py`; do not edit by hand.")
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {args.out} with {len(seen)} routes")


if __name__ == "__main__":
    main()
