# ADR 0099: Deprecation and removal ledger

Status: Accepted and implemented (2026-10-29)

## Context

Deprecations in 1.x were announced in release notes and nowhere else. Nothing checked that a removal had an owner or a version, or that a sunset header matched what the code sent.

## Decision

- `docs/api-deprecations.md` is the ledger. Each row names the surface, its owner, the version it was announced in, the default in 2.0, the removal version, the sunset header, and the status.
- `scripts/check_deprecations.py` fails when a row lacks an owner or a version, when a removal does not come after its announcement, or when the `/api/v1` sunset date differs from `routes.V1_SUNSET`.
- A surface is removed only in the release named in its row, with a migration note.

## Consequences

- Adding a deprecation means adding a row in the same change. The gate runs in `make check`.

## Verification

`scripts/check_deprecations.py`, run by `scripts/check_tests.py`.
