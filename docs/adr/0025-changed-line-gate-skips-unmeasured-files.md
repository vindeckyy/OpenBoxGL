# ADR 0025: Changed-Line Coverage Skips Unmeasured Files

**Date:** 2026-09-02
**Status:** Accepted

## Context

The changed-line coverage floor (`CHANGED_LINE_FLOOR = 80` in `scripts/check_tests.py`, measured by `scripts/check_changed_coverage.py`) intersected the diff against the upstream base with coverage data. However, the coverage configuration omits `test_*` and `scripts/*` from measurement (`[tool.coverage.run] omit`), so those files never appear in the combined data. Every changed line in a test or script file was therefore counted as a miss, making the floor unpassable whenever a release edits an existing test file and the upstream base is behind (the normal state while developing a release). Releases 1.7.x never hit this because checks ran after the upstream push, when the diff base equals HEAD.

## Decision

In `measure_changed_lines()`, skip any changed file that is not present in the coverage data's `measured_files()`. Concretely: files the measurement config excludes (tests, scripts, build output) are neutral to the changed-line floor instead of counting as misses. The gate's own unit tests gain a `get_data()` fake and a regression case asserting unmeasured files are skipped.

## Consequences

- The floor measures what it can actually influence: runtime code. Test edits no longer mechanically fail the gate.
- Coverage of test files themselves is still enforced structurally: the gate py_compiles them and runs them to completion; failures fail the gate.
- No change to the floors themselves (`COVERAGE_FLOOR`, `WEB_APP_FLOOR`, `CHANGED_LINE_FLOOR`, `NEW_MODULE_FLOOR`).
