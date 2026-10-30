# Backups

OpenBox writes a ZIP archive with a manifest. ADR 0093 defines the format; `scripts/check_backup_format.py` pins it through `scripts/contracts/backup.json`. The fixture corpus in `tests/fixtures/backup/` holds one archive per format, including one written by the 1.19.1 writer.

## Formats

| Format | Written by | Read by | Manifest |
|---|---|---|---|
| 2 (default) | 2.0.0 | 2.0.0 and later | `format: 2`, `app_version`, `state_schema`, `library_id`, `created_by`, `encryption: "none"`, `items`, `created`, and `files` with each member's `name`, `size`, `mode`, and SHA-256 |
| 1 | 1.x, and the 1.19-compatible export in 2.0.0 | 1.x and 2.x | No `format` field and no hashes |

A format above 2 is refused with `BACKUP_FORMAT_UNSUPPORTED`. Nothing is written.

## Restore

1. The archive is checked for symlinks, path traversal, and member limits.
2. For format 2, every member is checked against its manifest entry (size and SHA-256). An unlisted or missing member refuses the archive.
3. Only then are files written. A failed check leaves every existing file as it was.
4. The restore returns a report with one row per archive member: `verified` says whether its hash was checked, and `status` says whether this restore wrote it (`restored`), left it alone (`skipped`), or it is the manifest (`manifest`). The restore job returns the rows as `files`.

Restore keeps the pre-restore safety copy and merges preserved local credentials, as in 1.19.

Choosing a restore scope (for example settings only) checks every member first but writes only the selected items. The rest of the library is not touched.

## 1.19-compatible export

Choose **1.19-compatible** in **Settings > Sessions > Backup format** when a 1.19 install must read the backup. The export writes format 1: no manifest hashes, and the library layout 1.19 reads. It is the only export that a 1.19 install can restore, and the 2.0 build restores it too.

## What restores where

| Archive | 2.0 | 1.19 |
|---|---|---|
| Format 2 | Restores, every member hash-checked | Refused (`format` above 1) |
| Format 1 (1.19 writer) | Restores, no hash check | Restores |
| Format 1 (1.19-compatible export from 2.0) | Restores, no hash check | Restores (checked on 1.19.1) |

## What is not in a backup

Capability credentials under `plugins/credentials` are never included. Secrets are redacted from settings and plugin state. Media that a download wrote is listed in the media manifest in the data directory, but the manifest records what the app wrote, not a copy of the files outside the archive's items.

## Retention

Automatic backups keep the number set in settings. Manual backups are kept until you delete them.
