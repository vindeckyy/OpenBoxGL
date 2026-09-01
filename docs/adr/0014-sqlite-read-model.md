# ADR 0014: SQLite Read Model

**Date:** 2026-09-01
**Status:** Accepted

## Context

OpenBox stores its canonical library state in `library.json` (schema v6). The JSON read path in `pkg/state/cache.py` with `load_state_view()` and `CacheEpoch` handles current library sizes well, passing 20k performance gates. However, as libraries grow toward 50k+ entries, the JSON read path's O(n) filtering and linear search become a scalability ceiling.

The project's runtime dependency-free policy prohibits adding external database dependencies. Python's stdlib `sqlite3` module is available without any additional package.

## Decision

Add an **optional** SQLite read model (`pkg/state/sqlite_readmodel.py`) that:

1. Lives behind the `OPENBOX_ENABLE_SQLITE_READ=1` environment flag — disabled by default.
2. Rebuilds from the canonical JSON state on demand via `ensure_fresh(state, signature)`.
3. Provides FTS5 full-text search with LIKE fallback for environments without FTS5.
4. Offers indexed filtered queries (platform, genre, favorite, hidden, installed) with limit/offset.
5. Computes facets via SQL `GROUP BY`.
6. Is a **read-only projection** — all writes go through the canonical JSON state store, then `invalidate()` triggers a rebuild on the next read.
7. Includes `query_parity_check()` to verify SQLite results match the JSON path.

The database file lives alongside `library.json` in the data directory. It uses WAL journal mode for concurrent read access.

## Consequences

- **Positive**: Provides a forward path for 50k+ libraries without adding runtime dependencies.
- **Positive**: Flag-gated means zero behavior change for existing users.
- **Positive**: FTS5 fallback ensures compatibility with all SQLite builds.
- **Negative**: Rebuild cost is O(n) on first access or after invalidation.
- **Negative**: Additional complexity in the read path; users must opt in explicitly.

## Alternatives Considered

1. **Migrate to SQLite as canonical store**: Rejected — would break the JSON state store compatibility contract and require a schema migration.
2. **Add an external DB dependency**: Rejected — violates the dependency-free runtime policy.
3. **Optimize the JSON path further**: Already done in 1.7.1 with facet caching and write coalescing; diminishing returns for 50k+ scale.
