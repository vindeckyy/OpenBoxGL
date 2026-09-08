# ADR 0041: Docs archive layout and root-module manifest gate

**Date:** 2026-09-08
**Status:** Accepted

## Context

A stale-information sweep after the 1.10.0 release found three drift problems:

1. Superseded planning documents (`NEXT_UPDATE_PLAN.md`, `NEXT_UPDATE_EXECUTION.md`)
   and historical release notes for older versions sat in the active `docs/` root,
   and `docs/README.md` did not index most of the files that actually exist.
2. `scripts/check_runtime_modules.py` enforced manifest coverage for `handlers/`,
   `pkg/state/`, `pkg/parity/`, and `routes/` but not for root `*.py` modules, even
   though AGENTS.md requires every runtime module to be listed.
3. `routes/registry._ensure_handlers_loaded()` omitted six handler modules
   (`insights`, `picker`, `constellation`, `party`, `export`, `screenscraper`).
   Their routes still registered because `web_app` is in the load list and imports
   every handler transitively, so the gate passed — but the explicit list was
   misleading and silently dependent on that transitive import.

## Decision

- Move superseded plans and old release notes to `docs/archive/` (never delete;
  they provide traceability). Rewrite `docs/README.md` as a complete index of the
  docs tree.
- Add `*.py` (repo root) to the required globs in `check_runtime_modules.py`.
- List every handler module explicitly in `_ensure_handlers_loaded()` so route
  registration does not depend on `web_app`'s import graph.
- Add the missing `import pkg.parity` to `web_app.py` so both entry points match
  the `_ParityFlatFinder` contract documented in `pkg/parity/__init__.py` and
  ADR 0003 (previously satisfied only transitively via `from openbox import`).

## Consequences

- The docs index reflects the real tree and `docs/` separates current documents
  from historical records.
- A new root-level runtime module can no longer be added without a
  `runtime_modules.txt` entry.
- Route registration keeps working if `web_app` ever stops importing a handler.
