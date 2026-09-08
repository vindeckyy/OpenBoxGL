# ADR 0037: Require 95% changed-line coverage locally and in CI

Date: 2026-09-08
Status: Accepted

## Context

The local full gate required 80% changed-line coverage while the additional CI
coverage command required 95%. This allowed a local success below the CI floor.
The maintainer requested a 95% changed-line test gate.

## Decision

Raise `scripts/check_tests.py`'s `CHANGED_LINE_FLOOR` to 95%. Keep the existing
95% standalone CI check. Add a local-gate regression that rejects 94% and accepts
95% and 96%, and update the floor contract tests and development documentation.

## Consequences

`make check` and CI require at least 95% changed-line coverage. Existing total,
web-app, and new-module floors remain unchanged. This ratchets the threshold
established in ADR 0002; the measurement policy from ADR 0025 is unchanged.
