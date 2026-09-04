# ADR 0040: Changed-line coverage floor 95%

**Date:** 2026-09-04
**Status:** Accepted

## Context

`scripts/check_tests.py` enforces a changed-line coverage floor over executable
Python lines changed since the diff base (`@{upstream}` merge-base, fallback
`HEAD`), measured by `scripts/check_changed_coverage.py::measure_changed_lines()`.
Unmeasured test/script files are skipped (ADR 0025). The floor was 80%.

v1.9.0 landed ~7 user-visible features plus 4 architecture changes in one
release, raising regression risk on exactly the lines new work touches. Total
(72%) and `web_app.py` (54%) floors guard the aggregate; only the changed-line
floor guards the new work itself. The v1.10.0 plan (M0.4) hard-requires 95%.

## Decision

Ratchet `CHANGED_LINE_FLOOR = 80.0 -> 95.0` in `scripts/check_tests.py`.

1. Every feature lane must land >=95% on its own diff, not just the release
   average. Same-PR tests; no test-deferred follow-ups.
2. `total == 0` (no executable Python changes) still passes, so docs/CSS-only
   PRs are unaffected.
3. `pragma: no cover` is allowed only for proven-unreachable defensive branches
   with a written PR justification. Error paths (`needs_confirm`, lock
   contention, parity-mismatch fallback) must be tested, not excluded.
4. Frontend JS is not counted here (Python changed lines only). JS is covered
   by `scripts/check_frontend.py` (eslint/tsc) plus `scripts/ui_smoke.sh`.
5. No gate waivers. A lane that would dip below 95% adds tests before merge.

## Consequences

- `make check` fails with `changed-line coverage floor 95%` when changed-line
  coverage regresses. Floors go up, never down: this ADR is the gate-change
  record required by AGENTS.md.
- Small, Python-focused PRs stay green more easily than blended
  refactor+feature diffs; decomposition work (M6 moves) must carry finder-shim
  and contract coverage for moved lines.
- Pinned in `tests/test_ci_gates.py::test_check_tests_floor_constants`.
