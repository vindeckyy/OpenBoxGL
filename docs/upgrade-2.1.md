# Upgrading to OpenBox 2.1

OpenBox 2.1 removes the compatibility layers 2.0 announced. It does not migrate your library, change backup format 2, or change the plugin API. A 2.0 build can open a 2.1 library; a 2.1 build opens a 2.0 library without a migration step.

## Before you upgrade

- **If a script calls `/api/v1`:** move it to `/api/v2` before or after the upgrade. `OPENBOX_ENABLE_V1_API` no longer restores v1.
- **If a script calls an unversioned path such as `/api/library` or `/api/jobs`:** use the `/api/v2` replacement. The retired response names it in `context.use`.
- **If you use a native window from before 2.0:** update it. 2.0+ hosts already call `/api/host/*`; pre-2.0 hosts are refused with `410 HOST_CHANNEL_RETIRED`.
- **If you need a backup that a 1.19 install can read:** create it with a 2.0.x build before upgrading. 2.1 reads format 1 but writes format 2 only.
- **If you run from source on Python 3.10:** it still works in 2.1, but it is deprecated. Python 3.12 is recommended; removal is planned for 3.0.

## Upgrade

1. Quit OpenBox.
2. Install 2.1 over 2.0, or over 1.19 if you are skipping 2.0.
3. Start OpenBox. A 2.0 library opens as format 7 with no migration. A 1.19 library migrates from format 6 to 7 on first start and keeps `library.v6.backup.json`.

## Roll back

`openbox --rollback-library` is still available in 2.1:

```
openbox --rollback-library          # shows what would be discarded
openbox --rollback-library --yes    # restores the 1.19 library
```

It restores the v6 copy byte-for-byte, keeps the replaced v7 file as `library.v7.kept-<stamp>.json`, and lists edits made after the migration. The command is deprecated in 2.1 and is planned for removal in 3.0.

## API migration cheat sheet

| 2.0 call | 2.1 call |
|---|---|
| `/api/v1/library` | `/api/v2/library` |
| `/api/library` | `/api/v2/library` |
| `/api/jobs` (job map) | `/api/v2/jobs` (list and cursor) |
| `/api/launch` (host) | `/api/host/launch` |
| `/api/shutdown` (host) | `/api/host/shutdown` |
| `/api/native/capabilities` (host) | `/api/host/capabilities` |
| `POST /api/v2/backup/create` with `format: 1` | Use a 2.0.x build to write format 1; 2.1 returns `BACKUP_FORMAT_RETIRED` |

The local UI already uses `/api/v2`. `GET /api/v2/version` reports the app, API, state schema, plugin API, backup format, and sync format versions.

## What did not change

- Library schema 7 and `library.v6.backup.json`.
- Backup format 2 and format 1 read support.
- Plugin API 2, including the v1 adapter.
- Sync format 3.
- The 20,000-game formal scale.
