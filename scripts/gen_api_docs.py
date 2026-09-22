#!/usr/bin/env python3
"""Generate the API contract pages from the live route tables.

Outputs a markdown reference (one row per route, method, handler) for the
docs site. Run from the repo root:

  python3 scripts/gen_api_docs.py --out /tmp/api-v1.md
  python3 scripts/gen_api_docs.py --v2              # write docs/api-v2.md
  python3 scripts/gen_api_docs.py --v2 --check      # fail if docs/api-v2.md drifted

The v2 surface is the additive surface for 1.13+ features; routes listed there
may evolve until they are explicitly frozen.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from routes import GET_TABLE, POST_TABLE, V1_ALIASED_PREFIXES  # noqa: E402
from contracts import V1_SCHEMA  # noqa: E402


def handlers_for(v1_path):
    entries = []
    if v1_path in GET_TABLE:
        entries.append(("GET", GET_TABLE[v1_path]))
    if v1_path in POST_TABLE:
        entries.append(("POST", POST_TABLE[v1_path]))
    return entries


def _describe(v1_path, handler):
    """Return a documented response shape when the route is in V1_SCHEMA."""
    schema = V1_SCHEMA.get(v1_path)
    if schema and schema.get("response"):
        return f"`{schema['response']}`"
    return f"`{handler}`"


def _registry_routes():
    """Return (method, path) -> spec for decorator-registered routes."""
    from routes.registry import _REGISTRY, _ensure_handlers_loaded

    _ensure_handlers_loaded()
    return {(entry.method, entry.path): entry.spec for entry in _REGISTRY.values()}


def v2_routes():
    """Return sorted (method, path, spec) rows for the /api/v2 surface."""
    rows = {}
    for path, spec in GET_TABLE.items():
        if path.startswith("/api/v2/"):
            rows[("GET", path)] = spec
    for path, spec in POST_TABLE.items():
        if path.startswith("/api/v2/"):
            rows[("POST", path)] = spec
    for (method, path), spec in _registry_routes().items():
        if path.startswith("/api/v2/"):
            rows.setdefault((method, path), spec)
    return [(method, path, rows[(method, path)]) for method, path in sorted(rows, key=lambda item: (item[1], item[0]))]


def v1_rows():
    """Return sorted (v1_path, [(method, handler), ...]) rows for v1 aliases."""
    rows = {}
    for path in sorted(V1_ALIASED_PREFIXES):
        v1_path = f"/api/v1{path[len('/api'):]}"
        rows[v1_path] = handlers_for(v1_path)
    return [(path, rows[path]) for path in sorted(rows)]


def render_v1():
    lines = [
        "# API v1 contract",
        "",
        "The v1 surface is the stable contract. Legacy `/api/*` paths stay available for older clients; additive feature work targets `/api/v2/*` so the frozen v1 contract does not drift.",
        "",
        "Authentication: this generated page covers authenticated v1 JSON aliases. Most requests use the `X-OpenBox-Token` header with the per-process token from `server.token`; the local UI may also use a query token during startup. Public assets and byte/stream endpoints follow their route-specific behavior, and not every legacy error has the full `error`, `code`, and `request_id` envelope.",
        "",
        "| Method | Path | Handler | Response |",
        "|---|---|---|---|",
    ]
    seen = set()
    for v1_path, entries in v1_rows():
        if v1_path in seen:
            continue
        seen.add(v1_path)
        methods = [method for method, _ in entries] or ["-"]
        handlers_formatted = ", ".join(f"`{h}`" for _, h in entries) if entries else "`-`"
        primary_handler = entries[0][1] if entries else "-"
        response = _describe(v1_path, primary_handler)
        lines.append(f"| {' / '.join(methods)} | `{v1_path}` | {handlers_formatted} | {response} |")
    lines.append("")
    lines.append("Generated from `routes.py` and `contracts.py`; do not edit by hand.")
    return "\n".join(lines), len(seen)


def render_v2():
    lines = [
        "# API reference",
        "",
        "Generated from `routes.py`, `contracts.py`, and `routes/registry.py`; do not edit by hand. Regenerate with `python3 scripts/gen_api_docs.py --v2`.",
        "",
        "Authentication: requests use the `X-OpenBox-Token` header with the per-process token from `server.token`; the local UI may also use a query token during startup. Public assets and byte/stream endpoints follow their route-specific behavior.",
        "",
        "## API v2",
        "",
        "The additive surface for 1.13+ features. New work targets `/api/v2/*`; routes listed here may evolve until they are explicitly frozen.",
        "",
        "| Method | Path | Handler |",
        "|---|---|---|",
    ]
    v2 = v2_routes()
    for method, path, spec in v2:
        lines.append(f"| {method} | `{path}` | `{spec}` |")
    lines.extend(
        [
            "",
            f"_{len(v2)} routes._",
            "",
            "## API v1 (frozen)",
            "",
            "The v1 surface is the stable contract. Legacy `/api/*` paths stay available for older clients; additive feature work targets `/api/v2/*` so the frozen v1 contract does not drift.",
            "",
            "| Method | Path | Handler | Response |",
            "|---|---|---|---|",
        ]
    )
    for v1_path, entries in v1_rows():
        methods = " / ".join(method for method, _ in entries) or "-"
        handlers = ", ".join(f"`{handler}`" for _, handler in entries) or "`-`"
        primary = entries[0][1] if entries else "-"
        lines.append(f"| {methods} | `{v1_path}` | {handlers} | {_describe(v1_path, primary)} |")
    lines.extend(["", f"_{len(v1_rows())} routes._", ""])
    return "\n".join(lines), len(v2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=None)
    parser.add_argument("--v2", action="store_true", help="write the combined v2+v1 API reference instead of the v1 page")
    parser.add_argument("--check", action="store_true", help="fail if the output file drifted instead of writing it")
    args = parser.parse_args()

    if args.v2:
        out = Path(args.out) if args.out else ROOT / "docs" / "api-v2.md"
        reference, count = render_v2()
    else:
        out = Path(args.out) if args.out else Path("api-v1.md")
        reference, count = render_v1()

    if args.check:
        if not out.is_file():
            print(f"api docs missing: {out}; run python3 scripts/gen_api_docs.py{' --v2' if args.v2 else ''}", file=sys.stderr)
            return 1
        if out.read_text(encoding="utf-8") != reference + "\n":
            print(f"api docs stale: {out}; run python3 scripts/gen_api_docs.py{' --v2' if args.v2 else ''}", file=sys.stderr)
            return 1
        print(f"api docs fresh: {out}")
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(reference + "\n", encoding="utf-8")
    print(f"wrote {out} with {count} routes")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
