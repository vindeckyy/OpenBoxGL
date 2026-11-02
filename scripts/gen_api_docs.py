#!/usr/bin/env python3
"""Generate the API reference from the live route tables.

Outputs a markdown reference (one row per route, method, handler) for the docs
site. Run from the repo root:

  python3 scripts/gen_api_docs.py              # write docs/api-v2.md
  python3 scripts/gen_api_docs.py --check      # fail if docs/api-v2.md drifted
  python3 scripts/gen_api_docs.py --out /tmp/api.md
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from routes import GET_TABLE, POST_TABLE  # noqa: E402


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


def host_routes():
    """Return sorted (method, path, spec) rows for the /api/host channel."""
    rows = {}
    for path, spec in GET_TABLE.items():
        if path.startswith("/api/host/"):
            rows[("GET", path)] = spec
    for path, spec in POST_TABLE.items():
        if path.startswith("/api/host/"):
            rows[("POST", path)] = spec
    for (method, path), spec in _registry_routes().items():
        if path.startswith("/api/host/"):
            rows.setdefault((method, path), spec)
    return [(method, path, rows[(method, path)]) for method, path in sorted(rows, key=lambda item: (item[1], item[0]))]


def render_v2():
    lines = [
        "# API reference",
        "",
        "Generated from `routes.py` and `routes/registry.py`; do not edit by hand. Regenerate with `python3 scripts/gen_api_docs.py`.",
        "",
        "Authentication: requests use the `X-OpenBox-Token` header with the per-process token from `server.token`; the local UI may also use a query token during startup. Public assets and byte/stream endpoints follow their route-specific behavior.",
        "",
        "Errors: every response with status 400 or above is an RFC 9457 problem document served as `application/problem+json`, carrying `type` (derived from `code`), `title`, `status`, `detail`, `code`, `retryable`, `request_id`, and `instance`; `error` duplicates `detail` for one major cycle.",
        "",
        "## API v2",
        "",
        "The public API since 2.0 (ADR 0091). New work targets `/api/v2/*`; the surface is pinned by `api_contracts.json` and `scripts/check_api_contract.py`.",
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
            "## API host channel",
            "",
            "The native windows (WebKitGTK, WebView2, WKWebView) call `/api/host/*` and send `X-OpenBox-API-Version: 2`. A different major version is refused with `409 HOST_API_UNSUPPORTED` before the route runs (ADR 0091).",
            "",
            "| Method | Path | Handler |",
            "|---|---|---|",
        ]
    )
    host = host_routes()
    for method, path, spec in host:
        lines.append(f"| {method} | `{path}` | `{spec}` |")
    lines.extend(
        [
            "",
            f"_{len(host)} routes._",
            "",
            "## Retired surfaces",
            "",
            "`/api/v1`, the unversioned public `/api/*` paths, and the legacy native-host names were removed in 2.1 (ADR 0103). The frozen v1 route list remains at `docs/archive/v1_contracts.json`; the retirement ledger is `docs/api-deprecations.md`.",
            "",
        ]
    )
    return "\n".join(lines), len(v2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=None)
    parser.add_argument("--check", action="store_true", help="fail if the output file drifted instead of writing it")
    args = parser.parse_args()

    out = Path(args.out) if args.out else ROOT / "docs" / "api-v2.md"
    reference, count = render_v2()

    if args.check:
        if not out.is_file():
            print(f"api docs missing: {out}; run python3 scripts/gen_api_docs.py", file=sys.stderr)
            return 1
        if out.read_text(encoding="utf-8") != reference:
            print(f"api docs drifted: {out}; run python3 scripts/gen_api_docs.py", file=sys.stderr)
            return 1
        print(f"api docs OK: {count} v2 routes")
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(reference, encoding="utf-8")
    print(f"Wrote {out.relative_to(ROOT)}: {count} v2 routes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
