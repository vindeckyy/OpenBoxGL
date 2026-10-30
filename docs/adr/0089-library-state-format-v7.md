# ADR 0089: Library state format v7

Status: Accepted and implemented (2026-10-29)

## Context

`library.json` was a bare object with `schema_version` 6, no identity, no envelope, and runtime sessions stored beside durable data. Nothing could tell a future build to refuse a file without guessing, and a v6 library could not be rolled back once upgraded.

## Decision

- v7 adds an envelope: `format` (`openbox-library`), `schema_version` 7, `library_id` (ULID-style, minted once), `created_with`, `path_mode`, and `library_root`.
- `path_mode` and `library_root` mirror the settings of the same name. Settings stay authoritative and the envelope copy is refreshed on every write.
- `active_sessions` is process state. It is mirrored to `sessions.json` for crash recovery and never written into `library.json`.
- Migration from v6 runs under the store's exclusive lock. It writes a byte-identical copy (`library.v6.backup.json`), then a marker, a snapshot of the v6 primary, the v7 primary (atomic), and `migration-report.json`. A crash before the primary write leaves the v6 file in place. A crash after it leaves the marker, and the next start writes the report.
- A schema above 7 raises `StateFutureVersionError` (`STATE_FUTURE_VERSION`) and is never rewritten. Startup never auto-recovers from it or from a failed migration.
- Rollback restores the v6 bytes, keeps the replaced v7 file as `library.v7.kept-<stamp>.json`, lists the discarded game edits, and refuses while OpenBox is running or sessions are recorded.

## Consequences

- A v7 library cannot be read by 1.19.x. The 1.19 line must refuse it with a clear message (planned for 1.19.2, `docs/NEXT_UPDATE_PLAN-1.19.2.md`).
- Tests pin the schema through `scripts/contracts/state.json` (ratchet, ADR 0060).

## Verification

`tests/test_state_v7.py`, `tests/test_state_migration_rollback.py`, `tests/test_cli_library.py`, `scripts/check_state_schema.py`. Fixtures: `tests/fixtures/state/v6/`. The 1/100/10k matrix runs in `tests/test_state_v7.py`.
