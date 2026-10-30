# Upgrading to OpenBox 2.0

OpenBox 2.0 changes three things you may notice: the library file format, the public API, and the backup format. Your library, settings, and plugins keep working.

## What happens on first start

1. The library is upgraded from format 6 to format 7. The upgrade keeps every field it does not know.
2. A copy of the old file is kept as `library.v6.backup.json` next to your library.
3. `migration-report.json` records what changed: game count, profiles, playlists, and any runtime sessions moved to `sessions.json`.
4. A notification confirms the upgrade.

Nothing is deleted. Keep `library.v6.backup.json` until you are sure you do not need to roll back.

## Roll back

Quit OpenBox, then run:

```
openbox --rollback-library          # shows what would be discarded
openbox --rollback-library --yes    # restores the 1.19 library
```

Rollback restores the exact old file. Changes made after the upgrade (games added or edited) are not in the restored library. They are listed in `rollback-report.json`, and the replaced 2.0 file is kept as `library.v7.kept-<stamp>.json`. Rollback refuses while OpenBox is running.

## Versions that cannot read a 2.0 library

1.19.x cannot read a format 7 library. Install 2.0, or restore `library.v6.backup.json` and install 1.19.1.

## Scripts that call the local API

The public API is `/api/v2`. `/api/v1` is off by default. Set `OPENBOX_ENABLE_V1_API=1` to re-enable it through 2.0.x. It is removed in 2.1. `GET /api/v2/version` reports the contract versions. The native windows call `/api/host/*`.

## Backups

2.0 writes backup format 2, which records a SHA-256 for every file. Older backups still restore. To create a backup that 1.19 can read, choose the 1.19-compatible export. See `docs/backups.md`.

## Command reference

| Command | Purpose |
|---|---|
| `openbox --check-library` | Read-only integrity report; exit 0, 1, or 2 |
| `openbox --repair-library [--refresh-backup]` | Prune expired trash and drop the derived read model |
| `openbox --migrate-library` | Run the format 7 upgrade now |
| `openbox --rollback-library [--yes]` | Restore the format 6 copy |
