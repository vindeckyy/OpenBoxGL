# ADR 0093: Backup format v2

Status: Accepted and implemented (2026-10-29)

## Context

Backups had no format field. A v2 backup from a future release could not be told apart from a corrupt v1 backup, and a restore could write files before every archived member was checked.

## Decision

- Format 2 is the default. The manifest carries `format: 2`, `app_version`, `state_schema`, `library_id`, `created_by`, `encryption: "none"`, and `files`. Each entry lists the member's `name`, `size`, `mode`, and lowercase `sha256`.
- Each member's hash is computed from the bytes actually written to the archive. The manifest is written last.
- Restore reads formats 1 and 2. A manifest without `format` is format 1 and is accepted as written. A format above 2 is refused with `BACKUP_FORMAT_UNSUPPORTED` before anything is written.
- For format 2, restore checks every member's size and SHA-256 and refuses an unlisted or missing member before any write. A single mismatch leaves every existing file as it was.
- A 1.19-compatible export (`format: 1` in the create request) writes the v6 library layout: the v7 envelope is removed, `schema_version` is 6, and runtime sessions are kept under `active_sessions`.
- Archives stay ZIP files with symlinks refused, path traversal refused, and member limits as before.

## Consequences

- A restore that needs every file to be intact is stricter than in 1.19. That is the intent.
- Backups are not encrypted. `encryption` is always `none` in 2.0.

## Verification

`tests/test_backup_format_v2.py`, `tests/test_backup_diff.py`, `tests/test_backup_redaction.py`, `scripts/check_backup_format.py`, `scripts/contracts/backup.json`.
