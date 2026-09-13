# Performance

Measured by `scripts/perf_bench.py` against a synthetic library served by the real server (loopback, gzip enabled). Reference machine: this workstation.

## Final 1.11.0 measurements (2026-09-12)

Five-run strict local sampling on the final 1.11.0 worktree passed the 10k and
20k performance gates. Values below are p95 milliseconds from
`python3 -B scripts/perf_bench.py --sizes 10000,20000 --runs 5`; the generated
evidence is in `build/perf.json`.

| Library size | Full library | Gzip | Favorite write | Write path | Picker | Constellation |
|---|---:|---:|---:|---:|---:|---:|
| 10,000 games | 63.4 ms | 4.4 ms | 311.0 ms | 262.3 ms | 67.5 ms | 778.8 ms |
| 20,000 games | 120.8 ms | 5.4 ms | 553.2 ms | 537.6 ms | 135.2 ms | 611.1 ms |

The constellation endpoint uses the cached read-only state view so graph
requests do not deep-copy the complete catalog. This reduced the 20k
constellation p95 from 2,138.5 ms in the pre-fix sample to 611.1 ms here.

## Release-candidate measurements (2026-09-08, v1.10.0)

Seven-run strict local sampling on the release-candidate tree recorded the
following p95 values. The sample is evidence for this workstation, not a claim
about every user's hardware; the blocking CI job applies its documented runner
multiplier.

| Library size | Full library | Gzip | Favorite write | Write path | Picker | Constellation |
|---|---:|---:|---:|---:|---:|---:|
| 10,000 games | 36.6 ms | 2.9 ms | 164.0 ms | 143.4 ms | 112.8 ms | 579.7 ms |
| 20,000 games | 68.9 ms | 5.3 ms | 344.7 ms | 291.9 ms | 76.1 ms | 936.4 ms |

The full JSON evidence was captured in `/tmp/openbox-perf-final.json` during the
release execution and is intentionally treated as a disposable measurement.
The formal release gates remain the thresholds below; performance above 20,000
games is exploratory even when the SQLite read model is enabled.

## Historical baseline (2026-08-20, v1.5.1 - dirty-field writes + cached projections)

| Library size | /api/library plain | /api/library gzip | /api/media | Favorite mutation (full save) |
|---|---|---|---|---|
| 1,000 games | 3.6ms (2.9MB) | 1.4ms (136KB) | 1.9ms | 32.1ms |
| 5,000 games | 14.3ms (14.5MB) | 1.9ms (667KB) | 1.5ms | 156.7ms |
| 10,000 games | ~28ms (29MB est) | ~3.8ms (1.3MB) | ~2.8ms | ~310ms |

- Native host cold start (launch to server ready): 242 ms; server files published 182 ms after spawn. The WebKitGTK window then loads the token-bearing URL, so the full handshake stays under the 2s target.
- Coverage gates enforced in `scripts/check_tests.py`: `COVERAGE_FLOOR=72.0` total, `WEB_APP_FLOOR=54.0`, changed-line `95%`, new runtime modules `85%`.
- JSON remains canonical; the optional SQLite projection is an indexed read path for larger libraries, but only 10k/20k scenarios are release-gated.

## 20,000-game gates (blocking CI job `perf-20k`)

| key (20k) | p95 max ms |
|---|---|
| `library_ms_p95` | 4000 |
| `library_gzip_ms_p95` | 2000 |
| `favorite_mutation_ms_p95` | 4000 |
| `filtered_query_ms_p95` | 2000 |
| `facet_ms_p95` | 2000 |
| `20k_write_ms_p95` | 1000 |
| `picker_score_ms_p95` | 200 |
| `constellation_build_ms_p95` | 1500 |

CI command:

```bash
python3 -B scripts/perf_bench.py --sizes 10000,20000 --runs 5
```

Default local `--sizes` is `1000,5000,10000,20000`. Write-path benchmarks always include 10k and 20k.

## 10,000-game gates

| key (10k) | p95 max ms |
|---|---|
| `library_ms_p95` | 2000 |
| `library_gzip_ms_p95` | 1000 |
| `favorite_mutation_ms_p95` | 2000 |
| `filtered_query_ms_p95` | 1000 |
| `facet_ms_p95` | 1000 |
| `10k_write_ms_p95` | 500 |
| `picker_score_ms_p95` | 200 |
| `constellation_build_ms_p95` | 1500 |

## Historical baseline (2026-08-13, v1.0.0)

| Library size | /api/library plain | /api/library gzip | /api/media | Favorite mutation (full save) |
|---|---|---|---|---|
| 1,000 games | 3.6ms (2.9MB) | 1.4ms (136KB) | 1.9ms | 32.1ms |
| 5,000 games | 14.3ms (14.5MB) | 1.9ms (667KB) | 1.5ms | 156.7ms |

Previous 242 ms native host cold start and 182 ms server-files figures retained for reference.

## Historical baseline (2026-08-12)

| Library size | /api/library plain | /api/library gzip | /api/media | Favorite mutation (full save) |
|---|---|---|---|---|
| 1,000 games | 3.2ms (2.8MB) | - | 2.2ms | 29.8ms |
| 5,000 games | 13.7ms (13.8MB) | 1.9ms (638KB) | 1.3ms | 150.5ms |

Notes:

- Gzip is produced once per state change and cached, so the polled endpoint serves compressed bytes at plain-server speed with a 96% payload cut.
- The favorite mutation is the worst-case write: full JSON serialize + fsync + backup copy + snapshot rotation.

## Targets

- Product aspiration: `/api/library` at 10k games under 200ms plain and gzip under 50ms. The blocking CI thresholds are intentionally looser (`2000` ms and `1000` ms) to account for hosted-runner variance; passing CI is not the same as meeting this aspiration.
- State write at 10k games: under 500ms (currently ~150ms at 5k, superlinear).
- Cold start to UI ready: under 2s on the reference machine.

## Write-Path Benchmarks

| Operation | Target | Status |
|---|---|---|
| 10k favorite write | <500ms | measured |
| 20k favorite write | <1000ms | gated in CI |

## 1.7.1 Performance Architecture

- **Virtual Spacer-Window Grid**: `#grid` uses spacer-window virtualization (`IntersectionObserver`, `contain-intrinsic-size`, rAF coalescing) rendering only visible cards + overscan buffer. Enables 60 FPS scrolling at 20,000 games while maintaining full DOM a11y focus restoration. Can be bypassed via `localStorage['openbox-virtual-grid'] = 'false'`.
- **Search Offloading**: Search indexing and query evaluation run off the main thread in `static/worker.search.js` using trigram index + acronym matching with synchronous fallback.
- **FacetCache**: LRU facet cache (capacity 64) with epoch bumping on state changes, preventing repeated facet recalculations on large libraries.
- **Write Coalescing**: `state_store.py` micro-batches writes within a 50ms window with single fsync, minimizing disk write amplification during bulk mutations.

## Gate

CI job `perf-20k` is blocking on pull requests and pushes to master.

## 1.7.2 SQLite Read Model

- **Optional Acceleration**: `pkg/state/sqlite_readmodel.py` provides an alternative read path using stdlib `sqlite3`, enabled via `OPENBOX_ENABLE_SQLITE_READ=1` env flag.
- **FTS5 Search**: Full-text search via SQLite FTS5 virtual tables with automatic LIKE fallback for builds without FTS5 support.
- **Indexed Queries**: Filtered lookups on platform, genre, favorite, hidden, installed with limit/offset pagination via SQL WHERE clauses.
- **GROUP BY Facets**: Facet computation via SQL `GROUP BY` instead of in-memory iteration.
- **Signature-Based Rebuild**: `ensure_fresh(state, signature)` rebuilds only when the state signature changes, avoiding unnecessary rebuilds.
- **Zero Impact When Off**: Disabled by default; all methods are no-ops returning empty results.
- **Parity Verification**: `query_parity_check()` verifies SQLite results match the JSON read path.
