# Upgrading to OpenBox 3.0

OpenBox 3.0 keeps library schema 7, backup format 2, sync format 3, and `/api/v2`. A 2.1 build can still open a 3.0 library. The breaks run the other way: 3.0 will not boot a schema 6 file, will not run a v1 plugin, will not roll a library back, and will not start on Python 3.10.

## Before you upgrade

- **If the library is still schema 6** (a 1.19 library that 2.1 has not opened yet): quit OpenBox, then either run `openbox --migrate-library` on 3.0 or open the library once on 2.1 before you install 3.0. Startup does not upgrade it. The command keeps `library.v6.backup.json`.
- **If the library is schema 1 through 5:** open it on OpenBox 2.1 before you install 3.0. 3.0 migrates schema 6 only and does not rewrite an older file.
- **If a plugin omits `api_version` or still says `1`:** set `"api_version": 2` and follow `docs/plugin-api-v2.md`. 3.0 does not run the v1 runner.
- **If you run from source on Python 3.10:** install Python 3.11 or newer before you start 3.0. Python 3.12 is what the AppImage and the macOS bundle ship. 3.0 exits before it binds a port when the interpreter is older than 3.11.
- **If you still need `openbox --rollback-library`:** that command is gone. OpenBox 2.1.0 is the last build that restores `library.v6.backup.json`. 3.0 does not delete a backup that is already on disk.

## Upgrade

1. Quit OpenBox.
2. If the library is older than schema 7, migrate it as described above. A schema 7 library needs no migration.
3. Install 3.0 over 2.1, or over an older build after the library is schema 7.
4. Start OpenBox. `GET /api/v2/version` reports `version` 3.0.0, `api_version` 2, `state_schema` 7, `plugin_api_version` 2, `backup_format` 2, and `sync_format` 3.

## What did not change

- Library schema 7, the required envelope, and backup format 2 (format 1 is still read).
- Sync format 3.
- The public API at `/api/v2` and the host channel at `/api/host`.
- The SQLite read model: it is still a projection. Deleting `sqlite_readmodel.db` loses nothing.
- The AppImage glibc floor (`GLIBC_2.35` on Ubuntu 22.04).

## What you cannot do in 3.0

- Roll the library back to the v6 copy. Use OpenBox 2.1.0 if you still have `library.v6.backup.json` and you need that restore.
- Start on Python 3.10.
- Load a plugin whose manifest is missing `api_version` or sets it to 1.
