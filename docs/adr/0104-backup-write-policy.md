# ADR 0104: Backup format 2 write only, format 1 read

Status: Accepted and implemented (2026-11-01)

## Context

2.0 wrote format 2 by default and offered a 1.19-compatible format 1 export. ADR 0100 schedules the v1 write path for removal in 2.1. Older archives must keep restoring.

## Decision

- 2.1 writes backup format 2 only.
- 2.1 reads formats 1 and 2. Format 1 includes archives written by 1.19.x and the 2.0.x compatibility export.
- A format 1 write request returns `410 BACKUP_FORMAT_RETIRED` with `context.writable: [2]`.
- The Settings backup-format selector is removed; the create request no longer sends a format.
- `library.v6.backup.json` and `openbox --rollback-library` remain the 1.19 recovery path.

## Consequences

- A user who needs a 1.19-readable archive must create it on 2.0.x before upgrading.
- Format 2 remains readable by 2.0.0 because its manifest fields did not change.

## Verification

`tests/test_backup_format_v2.py`, `tests/test_backup_fixtures.py`, `tests/test_retired_surfaces.py`, `scripts/check_backup_format.py`.
