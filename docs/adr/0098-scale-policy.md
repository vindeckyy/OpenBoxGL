# ADR 0098: Scale policy: 20k formal, 50k exploratory

Status: Accepted. The 50k measurement is pending.

## Context

The formal scale promise has been 20,000 games. The JSON store and its write path do not have a measured answer for 50,000 games, and a blocking gate at that size would fail on the current design without a storage rewrite.

## Decision

- The formal scale stays 20,000 games. Blocking performance gates cover 10,000 and 20,000 games, with no regression above 10% against the 1.19.1 baseline.
- 50,000 games is exploratory: measured, reported in `docs/development/PERF.md` with command, machine, commit, and date, and never a blocker.
- The decision rule for 3.0: if a JSON-canonical store meets a p95 write of 1.5 s or less at 50,000 games with memory at or below 1.5 GB, the formal scale can rise without a storage rewrite. Otherwise 3.0 selects a storage design under a new ADR.

## Consequences

- No number is copied forward from an earlier run. A missing measurement is reported as missing.

## Verification

`docs/reliability.md` row 139. The 1.19.1 baseline recorded in the release/1.19 worktree is the reference for the no-regression gate.
