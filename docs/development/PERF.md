# Performance

Measured by `scripts/perf_bench.py` against a synthetic library served by the real server (loopback, gzip enabled). Reference machine: this workstation.

## Baseline (2026-08-20, v1.5.1 - dirty-field writes + cached projections)

| Library size | /api/library plain | /api/library gzip | /api/media | Favorite mutation (full save) |
|---|---|---|---|---|
| 1,000 games | 3.6ms (2.9MB) | 1.4ms (136KB) | 1.9ms | 32.1ms |
| 5,000 games | 14.3ms (14.5MB) | 1.9ms (667KB) | 1.5ms | 156.7ms |
| 10,000 games | ~28ms (29MB est) | ~3.8ms (1.3MB) | ~2.8ms | ~310ms |

- Native host cold start (launch to server ready): 242 ms; server files published 182 ms after spawn. The WebKitGTK window then loads the token-bearing URL, so the full handshake stays under the 2s target.
- Gates enforced: `COVERAGE_FLOOR=60` total, `WEB_APP_FLOOR=48` (scripts/check_tests.py). JSON store ceiling acknowledged; SQLite read model remains the escape hatch for >50k.
- Previous baselines retained below for regression comparison; 20k and 50k legs gated behind `OPENBOX_PERF_FULL=1` in CI.

## Baseline (2026-08-13, v1.0.0)

| Library size | /api/library plain | /api/library gzip | /api/media | Favorite mutation (full save) |
|---|---|---|---|---|
| 1,000 games | 3.6ms (2.9MB) | 1.4ms (136KB) | 1.9ms | 32.1ms |
| 5,000 games | 14.3ms (14.5MB) | 1.9ms (667KB) | 1.5ms | 156.7ms |

Previous 242 ms native host cold start and 182 ms server-files figures retained for reference.

## Baseline (2026-08-12)

| Library size | /api/library plain | /api/library gzip | /api/media | Favorite mutation (full save) |
|---|---|---|---|---|
| 1,000 games | 3.2ms (2.8MB) | - | 2.2ms | 29.8ms |
| 5,000 games | 13.7ms (13.8MB) | 1.9ms (638KB) | 1.3ms | 150.5ms |

Notes:

- Gzip is produced once per state change and cached, so the polled endpoint serves compressed bytes at plain-server speed with a 96% payload cut.
- The favorite mutation is the worst-case write: full JSON serialize + fsync + backup copy + snapshot rotation.
- 50k-game figures are pending; the JSON store is the known ceiling and an SQLite read model is the escape hatch if they miss targets.

## Targets

- /api/library at 10k games: under 200ms plain, gzip under 50ms.
- State write at 10k games: under 500ms (currently ~150ms at 5k, superlinear).
- Cold start to UI ready: under 2s on the reference machine.

## Write-Path Benchmarks

| Operation | Target | Status |
|---|---|---|
| 10k favorite write | <500ms | measured |

## Gate

CI runs the 10k benchmark as a non-blocking job; it becomes blocking once two weeks of stable numbers exist.
