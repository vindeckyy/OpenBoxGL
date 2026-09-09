# ADR 0042: Warm-up runs and trimmed p95 for performance gates

**Date:** 2026-09-09
**Status:** Accepted

## Context

The `perf-20k` CI job failed intermittently on `picker_score.p95_ms` (764 ms
and 541 ms against a 500 ms CI gate) while the median stayed at 59–89 ms, and
passed on rerun both times. Two mechanisms combine to cause this:

1. The first hit on each endpoint pays connection setup and lazy caches, and
   the benchmark only warmed up `/api/library` globally.
2. The p95 of a five-run sample interpolates between the top two runs, so a
   single scheduler stall or GC pause on a shared runner lands in the p95 and
   can exceed the gate even though the operation's real latency is far below
   it.

## Decision

- `_safe_request` performs one unmeasured warm-up request before the measured
  runs, so first-connection and lazy-cache costs never enter the sample.
- `_p95` drops the single worst run when the sample has five or more entries.
  The median continues to use every run.

## Consequences

- One-off runner noise no longer flakes the perf gate; a sustained regression
  (every run slower) still fails every gate unchanged.
- Gate thresholds are unchanged; the measured statistic is simply robust to a
  single outlier sample.
