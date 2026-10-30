# ADR 0088: Storage direction after 2.0

Status: Accepted for 2.0 (2026-10-29). JSON stays canonical. The 50k write measurement was run (`docs/development/PERF.md`, "2.0 exploratory: 50k"). The memory budget at 50k is not measured.

## Context

`transact_state(mutator)` has roughly 121 call sites in handlers and parity modules. A canonical SQLite store would need dirty tracking in every mutator, a full-state diff on each commit, or a new command API. ADR 0014 rejected canonical SQLite for compatibility reasons. The 2.0 plan asked for that rejection to be re-validated with a measurement before 3.0 considers it.

## Measurement

Single-process spike against the 1.19.1 production store (p95 milliseconds, Apple M4):

| Games | JSON full single-field update | SQLite full-state commit | JSON full-state digest |
|---|---:|---:|---:|
| 10,000 | 75.1 | 17.6 | 10.5 |
| 50,000 | 415.9 | 89.0 | 52.5 |

Server-level numbers at 50k (`scripts/perf_bench.py`, 3 runs): favorite mutation p95 927.8 ms and bulk mutation p95 1087.4 ms. Both are inside the 1500 ms write budget from ADR 0098. Memory at 50k was not measured.

Caveats: the spike's single-row SQLite commit writes a two-key stub body, so it is not a fair cost. The full-state commit is the comparison. Neither store uses `F_FULLFSYNC` on macOS (SQLite's `fullfsync` pragma is off by default), so both durability costs are understated by the same mechanism.

## Context on the budget

ADR 0098 sets the 50k write budget at p95 ≤ 1.5 s and memory ≤ 1.5 GB. The spike and the server run show the write budget is met on this machine. Memory is the open half of that budget.

## Decision

- JSON stays the canonical store in 2.0. `library.json` keeps its atomic writer, backup, snapshot rotation, and digest check.
- SQLite remains a derived read model (`pkg/state/sqlite_readmodel.py`). Deleting it loses nothing.
- The write measurements do not force a storage rewrite in 2.0. A 3.0 decision to raise the formal scale above 20,000 games must first measure memory at 50k.

## Consequences

- No storage rewrite lands in 2.0. Scale work stays inside JSON plus the read model.
- The formal library scale stays at 20,000 games (ADR 0098) until memory at 50k is measured and a 3.0 storage decision is recorded.
- Canonical SQLite would still need dirty tracking across the mutators. These measurements do not change that cost, so a 3.0 proposal starts there.

## Verification

- `docs/development/PERF.md`: 1.19.1 baseline, 2.0 exploratory 50k, SQLite spike.
- Spike harness: `/tmp/openbox-2.0/sqlite-spike/spike.py` (throwaway, not committed).
- `scripts/perf_bench.py --sizes 50000 --runs 3 --no-gate`.
