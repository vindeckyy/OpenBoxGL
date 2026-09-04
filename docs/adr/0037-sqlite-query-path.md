# ADR 0037: SQLite query path phase 2 (observability + opt-in filtered queries)

**Amends:** ADR 0014 (SQLite read model), ADR 0032 (graduation)
**Date:** 2026-09-04
**Status:** Accepted

## Context

ADR 0032 wired the SQLite read model to `GET /api/explorer/facets` and
`GET /api/v2/library/search` behind `OPENBOX_ENABLE_SQLITE_READ=1`, with a
`query_parity_check()` fallback to JSON. Two gaps remained for the v1.10.0
scale lane (plan M1):

1. No observability: responses did not say which path served them, parity was
   a silent boolean, and there were no timings to judge the 20k gate
   (`facet_ms_p95`, `filtered_query_ms_p95`).
2. `SqliteReadModel.query()` (platform/genre/favorite/hidden/installed +
   limit/offset) had no production caller and no flag story; the explorer grid
   (`GET /api/library`) stays JSON by design.

## Decision

1. **Additive observability fields.** When the read flag is on, facets and
   search return `source` (`"sqlite"`/`"json"`), `parity_ok` (bool), and
   `timings_ms` (`{"sqlite": ms, "json": ms}`). `timings_ms.sqlite` covers
   `ensure_fresh` + sqlite fetch + parity check; `timings_ms.json` covers the
   JSON fallback (0.0 when the sqlite path wins, so the fast path pays no
   double-compute cost). Flag off returns the exact legacy shapes
   (byte-identical; verified by test).
2. **Counts-only mismatch warning.** On parity failure the handler logs one
   warning with state signature + `json_count`/`sqlite_count` and serves JSON.
   No per-game dump (log-spam guard at 20k). `OPENBOX_SQLITE_PARITY_LOG=1` is
   the verbose escape hatch: it appends the first 10 symmetric-difference game
   ids at info level.
3. **Second flag for filtered queries.** `OPENBOX_ENABLE_SQLITE_QUERY=1`
   (default off) gates the structured path: `GET /api/v2/library/search`
   accepts `platform/genre/favorite/hidden/installed + offset` (limit already
   existed, still clamped to 200). With READ+QUERY on, filters run as one
   indexed `filtered_query()` (single `ensure_fresh`, no invalidation on the
   read path); text `q` intersects FTS5/LIKE hits by `game_id`. With READ on
   but QUERY off, filters are honored via Python over sqlite results, so filter
   params are never silently ignored. With both flags off, behavior is
   byte-identical to before. `GET /api/library` stays JSON (ADR 0032 scope
   line preserved).
4. **Single freshness check per request.** `filtered_query()` bundles one
   `ensure_fresh` + one `query()`; handlers never call `ensure_fresh` twice.
   `transact_state()` still invalidates exactly once per write (tested).
5. **FTS5 honesty.** `source: "sqlite"` means "served from SQLite", whether the
   text match used FTS5 or the LIKE fallback (existing behavior, now covered
   for the filtered path too). No new `engine` field was added to keep the
   response surface minimal.

## Consequences

- New query surface is additive (`/api/v2/*` params only); v1 contract
  untouched (neither endpoint is in the frozen 60-route surface).
- Flag matrix is off/off by default = zero behavior change; facets-only and
  facets+query are independently opt-in.
- Parity scope stays at game-id sets (`query_parity_check`); facet *values*
  may differ from JSON (sqlite GROUP BY counts hidden games, JSON
  `explorer_facets` skips them) without tripping the fallback. This is
  intentional and documented here.
- No new runtime module; no locale strings (log/API only); no tokens/CSS.

## Write-path ceiling (M1.3)

Favorite-mutation p95 at 10k/20k was re-measured with
`scripts/perf_bench.py --sizes 10000,20000 --runs 5` on
AMD Ryzen 5 4600H, 2026-09-04, commit b4e2810 (machine under parallel-agent
load); see `docs/development/PERF.md` 1.10.0 section. Both write budgets pass
(462.2ms < 500 @10k; 954.3ms < 1000 @20k) but exceed the 80% heuristic, so the
dirty-field subset was evaluated and deliberately NOT implemented: the
single-file JSON store must re-serialize + fsync + backup-copy + rotate
snapshots on every write, and a favorite-only fast path cannot skip the
dominant costs without risking backup/snapshot consistency. The candidate
follow-up (skip `_rotate_snapshots()` for volatile-only mutations in
`state_store.py::_write_unlocked`) is recorded in PERF.md; revisit only if
write p95 breaches budget on quiet hardware.
