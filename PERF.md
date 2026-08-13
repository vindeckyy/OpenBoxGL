# Performance

Measured by `scripts/perf_bench.py` against a synthetic library served by the real server (loopback, gzip enabled). Reference machine: this workstation.

## Baseline (2026-08-13)

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

## Gate

CI runs the 10k benchmark as a non-blocking job; it becomes blocking once two weeks of stable numbers exist.
