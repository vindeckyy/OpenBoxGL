#!/usr/bin/env python3
"""Generate the API reference from the live route tables and decorator registry.

Outputs one markdown page with two sections:

  - **API v2** — the additive surface for 1.13+ features, generated from
    ``routes.GET_TABLE`` / ``routes.POST_TABLE`` plus every ``@route`` entry
    registered by the handlers package.
  - **API v1** — the frozen legacy contract, generated from
    ``V1_ALIASED_PREFIXES`` and documented response shapes in ``contracts.py``.

The output is deterministic (sorted by path, then method), so it can be
checked for freshness instead of reviewed by hand:

  python3 scripts/gen_api_docs.py                 # write docs/api-v2.md
  python3 scripts/gen_api_docs.py --check         # fail if the file drifted
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from routes import GET_TABLE, POST_TABLE, V1_ALIASED_PREFIXES  # noqa: E402
from contracts import V1_SCHEMA  # noqa: E402

DEFAULT_OUT = ROOT / "docs" / "api-v2.md"


def _registry_routes() -> dict[tuple[str, str], str]:
    """Return (method, path) -> spec for decorator-registered routes."""
    from routes.registry import _REGISTRY, _ensure_handlers_loaded

    _ensure_handlers_loaded()
    return {(entry.method, entry.path): entry.spec for entry in _REGISTRY.values()}


def v2_routes() -> list[tuple[str, str, str]]:
    """Return sorted (method, path, spec) rows for the /api/v2 surface."""
    rows: dict[tuple[str, str], str] = {}
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


def v1_routes() -> list[tuple[str, list[tuple[str, str]]]]:
    """Return sorted (v1_path, [(method, handler), ...]) rows for v1 aliases."""
    routes: dict[str, list[tuple[str, str]]] = {}
    for path in sorted(V1_ALIASED_PREFIXES):
        v1_path = f"/api/v1{path[len('/api'):]}"
        entries = []
        if v1_path in GET_TABLE:
            entries.append(("GET", GET_TABLE[v1_path]))
        if v1_path in POST_TABLE:
            entries.append(("POST", POST_TABLE[v1_path]))
        routes[v1_path] = entries
    return [(path, routes[path]) for path in sorted(routes)]


def _describe(v1_path: str, handler: str) -> str:
    schema = V1_SCHEMA.get(v1_path)
    if schema and schema.get("response"):
        return f"`{schema['response']}`"
    return f"`{handler}`"


def render() -> str:
    lines = [
        "# API reference",
        "",
        "Generated from `routes.py` and `contracts.py`; do not edit by hand. "
        "v2 rows also include `@route` decorator registrations from "
        "`routes/registry.py`. Regenerate with `python3 scripts/gen_api_docs.py`.",
        "",
        "Authentication: requests use the `X-OpenBox-Token` header with the "
        "per-process token from `server.token`; the local UI may also use a "
        "query token during startup. Public assets and byte/stream endpoints "
        "follow their route-specific behavior.",
        "",
        "## API v2",
        "",
        "The additive surface for 1.13+ features. New work targets `/api/v2/*`; "
        "routes listed here may evolve until they are explicitly frozen.",
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
            "The v1 surface is the stable contract. Legacy `/api/*` paths stay "
            "available for older clients; additive feature work targets "
            "`/api/v2/*` so the frozen v1 contract does not drift. Public "
            "assets and byte/stream endpoints follow their route-specific "
            "behavior, and not every legacy error has the full `error`, `code`, "
            "and `request_id` envelope.",
            "",
            "| Method | Path | Handler | Response |",
            "|---|---|---|---|",
        ]
    )
    v1 = v1_routes()
    for path, entries in v1:
        methods = " / ".join(method for method, _ in entries) or "-"
        handlers = ", ".join(f"`{handler}`" for _, handler in entries) or "`-`"
        primary = entries[0][1] if entries else "-"
        lines.append(f"| {methods} | `{path}` | {handlers} | {_describe(path, primary)} |")
    lines.extend(["", f"_{len(v1)} routes._", ""])
    return "\n".join(lines)


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _summarize_drift(expected: str, actual: str) -> str:
    expected_lines = expected.splitlines()
    actual_lines = actual.splitlines()
    expected_set = set(expected_lines)
    actual_set = set(actual_lines)
    added = [line for line in expected_lines if line not in actual_set]
    removed = [line for line in actual_lines if line not in expected_set]
    parts = []
    if added:
        parts.append(f"{len(added)} line(s) missing from the checked-in file (first: {added[0][:100]})")
    if removed:
        parts.append(f"{len(removed)} stale line(s) in the checked-in file (first: {removed[0][:100]})")
    return "; ".join(parts) or "files differ"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output markdown path")
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the output file matches the generated reference instead of writing it",
    )
    args = parser.parse_args(argv)

    target = Path(args.out)
    if not target.is_absolute():
        target = ROOT / target
    reference = render()

    if args.check:
        if not target.is_file():
            print(f"api docs missing: {_display(target)}; run python3 scripts/gen_api_docs.py", file=sys.stderr)
            return 1
        if target.read_text(encoding="utf-8") != reference:
            print(
                f"api docs stale: {_display(target)}; "
                f"{_summarize_drift(reference, target.read_text(encoding='utf-8'))}. "
                "Run python3 scripts/gen_api_docs.py",
                file=sys.stderr,
            )
            return 1
        print(f"api docs fresh: {_display(target)}")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(reference, encoding="utf-8")
    print(f"wrote {_display(target)} ({len(v2_routes())} v2 + {len(v1_routes())} v1 routes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
