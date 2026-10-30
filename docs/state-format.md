# Library state format (v7)

`library.json` is the canonical library. It is JSON, human-readable, and written atomically. ADR 0089 defines the format; `scripts/check_state_schema.py` pins it through `scripts/contracts/state.json`.

## Envelope

| Field | Type | Rule |
|---|---|---|
| `format` | string | `openbox-library`. A file without it is not v7. |
| `schema_version` | integer | `7`. A higher value is refused with `STATE_FUTURE_VERSION` and is never rewritten. |
| `library_id` | string | 26 Crockford base32 characters, minted once. Never derived from a path. |
| `created_with` | string | The app version that created or migrated the file. Informational. |
| `path_mode` | string | `absolute` or `portable`. Mirrors `settings.path_mode`; settings are authoritative. |
| `library_root` | string | Empty or an absolute local folder. Mirrors `settings.library_root`. |

Every other top-level field (games, profiles, playlists, and any field this build does not know) is kept as written.

## Runtime sessions

`active_sessions` is process state. It is mirrored to `sessions.json` (format 1) for crash recovery and is never written into `library.json`. When no session is active, `sessions.json` is removed.

## Portable paths

In `path_mode: portable`, game paths under `library_root` are stored as `@library/<relative>`. Loading expands them to absolute paths, so launch and every other consumer see the same paths as in absolute mode. Saving encodes them again. Rules are in `docs/portable-library.md` and ADR 0090.

## Migration from v6

1. The primary is read and validated in memory. An invalid file is reported and not changed.
2. The original bytes are written to `library.v6.backup.json` (byte-identical). A different older copy is renamed with a timestamp.
3. A marker, `migration-in-progress.json`, is written.
4. A snapshot of the v6 primary is taken through the normal snapshot rotation.
5. The v7 primary replaces the old file atomically.
6. `migration-report.json` is written and the marker is removed.

A crash before step 5 leaves the v6 file in place and the next start retries. A crash after step 5 leaves the marker, and the next start writes the report.

## Migration report

`migration-report.json` records `from_schema`, `to_schema`, `library_id`, the backup name and SHA-256, the pre-migration snapshot name, `counts` (games, profiles, playlists, sessions moved, paths tokenized, paths skipped), `warnings`, and `reconstructed` (true when a later start wrote it).

## Rollback

`openbox --rollback-library --yes` restores the v6 bytes. It keeps the replaced v7 file as `library.v7.kept-<stamp>.json`, writes `rollback-report.json` that lists game edits made after the migration, and removes the migration report. It refuses while OpenBox is answering on its port or while `sessions.json` holds sessions.

## Integrity and repair

- `openbox --check-library` reads the library without changing it. Exit codes: 0 healthy, 1 warnings, 2 errors. The same report is served at `GET /api/v2/library/integrity`.
- `openbox --repair-library [--refresh-backup]` prunes expired trash and removes the derived read model. It never deletes games or media.

## Exit codes for library commands

| Code | Meaning |
|---|---|
| 0 | Success, or a healthy library |
| 1 | Check found warnings |
| 2 | Check found errors |
| 3 | Refused: OpenBox running, sessions active, invalid library, or the copy does not validate |
| 4 | Usage error |
