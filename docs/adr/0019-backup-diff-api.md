# ADR 0019: Backup Diff API

**Date:** 2026-09-01
**Status:** Accepted

## Context

OpenBox supports whole-library backups as JSON archives. Users can create, rotate, and restore archives, but there was no way to compare the current library state against a backup without restoring it first. For libraries with thousands of games, a manual diff is impractical.

## Decision

Add a **backup diff endpoint** `GET /api/v2/backup/diff?archive=<name>` that:

1. Loads the named backup archive's manifest.
2. Compares it against the current library state.
3. Returns `added` (games in current library but not in backup), `removed` (games in backup but not in current library), and `changed` (games present in both but with differing fields).
4. Returns a `settings_changed` boolean and summary counts.
5. Handles invalid or missing archives with appropriate error responses.

Implementation lives in `pkg/parity/parity_backup.py` as `diff_manifests()`, registered as a v2 route without modifying frozen v1 routes.

## Consequences

- Users can inspect what changed between a backup and current state without restoring.
- The diff is additive (new v2 route); v1 routes are unchanged.
- `diff_manifests()` operates on in-memory manifests; no disk writes.
- Large libraries may take longer to diff; the endpoint is synchronous.
