# Performance

Measured by `scripts/perf_bench.py` against a synthetic library served by the real server (loopback, gzip enabled). Reference machine: this workstation.

## 1.15.0 measurements (2026-09-29, release tree)

Five-run strict local sampling on the 1.15.0 tree passed all performance gates
(`python3 -B scripts/perf_bench.py --sizes 10000,20000 --runs 5`, p95 milliseconds). The
1.15 polish is client-side (CSS, dialogs, toasts, grid attributes), so the server numbers
are expected to sit within run-to-run noise of the pre-polish run below, and they do.

| Library size | Full library | Gzip | Favorite write | Write path | Picker | Constellation | Facet |
|---|---:|---:|---:|---:|---:|---:|---:|
| 10,000 games | 37.6 ms | 16.8 ms | 211.2 ms | 166.6 ms | 44.4 ms | 279.7 ms | 485.2 ms |
| 20,000 games | 73.9 ms | 4.2 ms | 388.6 ms | 440.0 ms | 98.2 ms | 303.5 ms | 1219.1 ms |

`facet` is still the binding constraint: 1219 ms against a 2000 ms budget at 20k (1.6x
headroom), and it is still counted on the UI thread. The search-worker facet move stays
deferred; it is only worth building if a long-task probe shows animation jank during a facet
recompute. Frame time is still not measured. Browser timings (first render, long tasks and the search
shift) are in the 1.16.1 section below. The JSON artifact keeps `"cold_start_ms": null` with
`"cold_start_measured": false`: an earlier revision emitted a hardcoded 242.0 there, which read
as a measurement in the artifact and in any run-over-run comparison. The 242 ms figure below is
therefore historical context, not a current result.

## 1.16.1 browser and layout measurements (2026-10-08)

Headless Chrome 155 (chrome-headless-shell) on one Linux machine, through `scripts/perf_browser.cjs`. The
search measure types a query that matches nothing, 800 ms after the first cover appears, and records the layout
shift from that search. Reproduce with `PUPPETEER_EXECUTABLE_PATH=<chrome> python3 -B scripts/perf_bench.py
--sizes 10000 --browser --runs 3 --no-gate` (puppeteer under `scripts/node_modules`). `perf_bench.py --browser`
measures the largest library the run generated for the read benchmark.

Search layout shift, per run (the first run is cold):

| Library | Filter chip row | Runs |
|---|---|---|
| 1,000 games | `flex: 1 1 auto` (1.16.0) | 0.0994, 0.0994, 0.0994, 0.0994 |
| 1,000 games | one line, fixed height (1.16.1, ADR 0070) | 0, 0, 0, 0 |
| 10,000 games | `flex: 1 1 auto` (1.16.0) | 0.0994, 0, 0, 0 |
| 10,000 games | one line, fixed height (1.16.1, ADR 0070) | 0, 0, 0, 0 |

The shift came from the chip row, not from the grid emptying. Three chips wrapped to a 192 px row and pushed the
drop zone and Play Insights down by 145 px; the grid itself did not move. With the row one line tall, the drop zone
stays at 342 px and Play Insights at 423 px while the search adds chips. The earlier attribution to the chips' flex
basis was a partial reading and is superseded. The cold 10,000-game run shifted for the same reason, while the
panels were still settling.

First render and long tasks at 10,000 games (cold run, then warm runs): first render 16.3 s cold, then 0.7 to
2.2 s; 3 to 4 long tasks per load, about 490 to 640 ms, unchanged by this release.

Search typing at 20,000 games, CPU profile (`Profiler` domain, 200 µs sampling), queries `a`, `e`, `o`: the
top self-time functions were `loadInsights` (Play Insights re-rendering on refresh, about 55 ms in the `a` query),
the rendering "(program)" bucket (about 120 to 530 ms), and idle. Facet counting did not appear among the top
functions for any query. The worker move for facets was therefore not built: its condition in the plan was a long
task during a facet recompute, and the profile does not show one. The next candidate, if long tasks during search
become a problem, is Play Insights' refresh, not facets.

Gate: `search_cls_max < 0.1` (`BROWSER_CLS_GATE`). The filter-chip fix brings the measured value to 0 on every run,
so the gate now has room.

## 1.15 pre-polish measurements (2026-09-26)

Five-run strict local sampling on the 1.15 development worktree passed all sixteen 10k and
20k gates. Values below are p95 milliseconds from
`python3 -B scripts/perf_bench.py --sizes 10000,20000 --runs 5`; the generated
evidence is written by `--out` and the run is reproducible with the command
above. **These replace the 1.11.0 tables below, which are kept only as
history.**

| Library size | Full library | Gzip | Favorite write | Write path | Picker | Constellation |
|---|---:|---:|---:|---:|---:|---:|
| 10,000 games | 22.4 ms | 2.5 ms | 187.4 ms | 193.8 ms | 41.3 ms | 385.6 ms |
| 20,000 games | 78.8 ms | 3.5 ms | 504.2 ms | 434.3 ms | 85.7 ms | 404.7 ms |

Read paths are now an order of magnitude inside their budgets. The two
operations that matter are the ones still close to a line:

| Operation | 10k p95 | 20k p95 | 20k budget | Headroom |
|---|---:|---:|---:|---:|
| `facet_ms_p95` | 612.1 ms | 1395.5 ms | 2000 ms | **1.4x** |
| `20k_write_ms_p95` | — | 434.3 ms | 1000 ms | 2.3x |
| `picker_score_ms_p95` | 41.3 ms | 85.7 ms | 200 ms | 2.3x |

**`facet` is the binding constraint.** It is the only operation inside 2x of its
budget at 20k, and it is exactly the one still computed on the UI thread: 1.12
deferred "search-worker facet move" and `static/worker.search.js` still contains
no facet logic. Facet counting at 20k nearly doubles between 10k (612 ms) and 20k
(1395.5 ms), which is the superlinear shape you would expect from a single-threaded
pass. Moving that pass into the worker is the highest-value performance change
available and is why this row is called out rather than buried in the table.

Native host cold start (launch to server ready) measured **242 ms** on this run,
unchanged from 1.11.0.

## 1.16.0: Launch Audit

The Launch Audit (F1) runs the Launch Doctor over every game in a background job.
Its cost was dominated by subprocesses, not Python: the Doctor spawns
`flatpak info` and `flatpak info --show-permissions` per game, so an uncached
20,000-game audit is up to 40,000 spawns, over an hour at ~100 ms each.
`ProbeCache` in `pkg/parity/parity_launch_audit.py` caches the subprocess result
per argv (the permission *text*, never the per-ROM grant decision), and `which`
the same way.

| Operation | 10k p95 | 20k p95 | 20k budget | Flatpak spawns | `which` probes |
|---|---:|---:|---:|---:|---:|
| `launch_audit_ms_p95` | 1648 ms | 3321 ms | 10000 ms | 28 | 29 |

Measured in-process with the shipped 24 definitions (14 distinct Flatpak app
ids), five runs per size, deep mode off. The spawn and probe counts are the same
at 10k and 20k; that is the property that matters.

**The enforced gate is the spawn count, not this wall clock.**
`tests/test_launch_audit.py` asserts at most two spawns per distinct app id
(<= 48 for 24 definitions), one `which` per binary name, and that the count does
not grow from a 14-game library to a 2,000-game one. That is deterministic, so it
holds on a loaded shared runner where a millisecond budget would flake. With the
cache removed the same test sees 4,000 spawns and fails. The millisecond budget
above is documented, not gated.

Deep mode (archive listing and BIOS hashing) is off by default because it is the
only part whose cost grows with file size rather than game count.

## 1.12.0 measurements

No dedicated measurement run was recorded for the 1.12.0 release. The
SQLite read model self-enables at 5,000+ games unless opted out (ADR 0047), so
large-library search defaults to the FTS path rather than the JSON path those
older tables measured. The 1.15 run above supersedes this section.

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
- Coverage gates enforced in `scripts/check_tests.py`: `COVERAGE_FLOOR=83.0` total, `WEB_APP_FLOOR=58.0`, changed-line `95%`, new runtime modules `85%`.
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

- **Virtual Spacer-Window Grid**: `#grid` uses spacer-window virtualization (`IntersectionObserver`, `contain-intrinsic-size`, rAF coalescing) rendering only visible cards + overscan buffer, with full DOM a11y focus restoration. Scrolling cost is bounded by the visible window rather than the library size, which is what keeps the 20,000-game budget viable. Can be bypassed via `localStorage['openbox-virtual-grid'] = 'false'`. *(Frame time at that scale is not measured — see §1.7 above — so no FPS figure is claimed here.)*
- **Search Offloading**: Search indexing and query evaluation run off the main thread in `static/worker.search.js` using trigram index + acronym matching with synchronous fallback.
- **FacetCache**: LRU facet cache (capacity 64) with epoch bumping on state changes, preventing repeated facet recalculations on large libraries.
- **Write Coalescing**: `state_store.py` micro-batches writes within a 50ms window with single fsync, minimizing disk write amplification during bulk mutations.

## Gate

CI job `perf-20k` is blocking on pull requests and pushes to master.

## 1.7.2 SQLite Read Model

> Historical note: this section describes the original 1.7.2 opt-in behavior.
> Since 1.12.0 (ADR 0047) the read model self-enables at 5,000+ games unless
> opted out with `OPENBOX_ENABLE_SQLITE_READ=0`.

- **Optional Acceleration**: `pkg/state/sqlite_readmodel.py` provides an alternative read path using stdlib `sqlite3`, enabled via `OPENBOX_ENABLE_SQLITE_READ=1` env flag.
- **FTS5 Search**: Full-text search via SQLite FTS5 virtual tables with automatic LIKE fallback for builds without FTS5 support.
- **Indexed Queries**: Filtered lookups on platform, genre, favorite, hidden, installed with limit/offset pagination via SQL WHERE clauses.
- **GROUP BY Facets**: Facet computation via SQL `GROUP BY` instead of in-memory iteration.
- **Signature-Based Rebuild**: `ensure_fresh(state, signature)` rebuilds only when the state signature changes, avoiding unnecessary rebuilds.
- **Zero Impact When Off**: Disabled by default; all methods are no-ops returning empty results.
- **Parity Verification**: `query_parity_check()` verifies SQLite results match the JSON read path.
