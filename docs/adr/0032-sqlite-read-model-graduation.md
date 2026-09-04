# ADR 0032: SQLite read model graduation

**Amends:** ADR 0014 (SQLite read model)
**Date:** 2026-09-04
**Status:** Accepted

## Context

ADR 0014 introduced `pkg/state/sqlite_readmodel.py` behind the `OPENBOX_ENABLE_SQLITE_READ=1` environment flag. The module was complete (rebuild, ensure_fresh, query, search, facets, count, query_parity_check) with 90% test coverage, but was **never imported at runtime** — the flag was read only inside the module itself, and no query path consulted it. The README "Scale & Backups" section and PARITY row "Optional SQLite read model: done" advertised FTS5 search and GROUP BY facets that were unreachable.

## Decision

Graduate the read model from dead code to a wired, parity-checked query path:

1. **Singleton**: `SQLITE_READ_MODEL = SqliteReadModel(DATA.parent / "sqlite_readmodel.db")` in `pkg/state/cache.py`, created at import time. No-op when the flag is unset (`enabled` returns `False`, all methods return empty results).

2. **Invalidation**: `transact_state()` calls `SQLITE_READ_MODEL.invalidate()` after every write, alongside the existing `CACHE_EPOCH._invalidate_all()`. The next read triggers `ensure_fresh(state, signature)` which rebuilds only when the state signature changed.

3. **Facets wiring**: `GET /api/explorer/facets` consults the SQLite model when enabled. Before serving SQLite results, `query_parity_check()` verifies the SQLite game IDs match the JSON game IDs. On mismatch, the JSON path is used for that request and a warning is logged.

4. **Search endpoint**: New additive `GET /api/v2/library/search?q=&limit=` uses FTS5 (or LIKE fallback) when the SQLite model is enabled; falls back to a simple JSON title substring match when disabled. The `source` field in the response indicates which path was used (`"sqlite"` or `"json"`).

5. **Full library `GET /api/library` stays JSON**: the frontend filters client-side via `worker.search.js`; SQL adds nothing there. This is a deliberate scope line — the SQLite model serves facets and server-side search only.

## Consequences

- `OPENBOX_ENABLE_SQLITE_READ=1` now actually changes behavior: facets are served from SQLite GROUP BY, and `/api/v2/library/search` uses FTS5.
- Flag off (default): byte-identical behavior to before. The singleton is a no-op.
- Parity check on first serve per signature prevents serving stale or wrong data.
- JSON remains the source of truth; SQLite is a read-only projection rebuilt on demand.
- `runtime_modules.txt` already includes `sqlite_readmodel.py`; no new runtime module was added (the singleton lives in `cache.py` which is already registered).
