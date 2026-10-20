# ADR 0081: Save archive manifests and selective restore

**Date:** 2026-10-19
**Status:** Accepted (1.18.0)

## Context

Per-game save backups are ZIP archives under `<data>/save-backups/<game-key>/`. Before 1.18 the archive carried the
save files but no inventory, so version history could show only a name and size, and restore extracted the whole
archive. A user could not see which local saves would be replaced, could not restore one file out of a backup, and got
no safety copy before a destructive write.

A save tree is attacker-adjacent input for restore: it can contain symlinks, and archive members can name absolute or
parent-relative paths. The restore path must not create a link or write outside the configured save roots.

## Decision

### Manifest

- Every new archive contains `manifest.json` next to the save members. It records `game`, `label`, `emulator_id`,
  `core`, `slot`, the captured `roots` (`{path, file}`, in manifest order), and `entries`, one per regular file:
  `{root, relative, archive, size, mode, mtime, sha256}`.
- The inventory is taken before archiving, streaming SHA-256 with a read-only, no-follow descriptor. Only regular
  files are captured; a symlink anywhere in a source root aborts the backup.
- Members are named `roots/<root-index>/<relative-path>`, where the index is the position in the manifest's `roots`. At
  restore time every manifest root must match one of the game's currently configured save roots by path. A recorded root
  that no longer exists is restored as its recorded file-or-directory type. A duplicate root, a root outside the configured
  set, or a live file/directory type swap is refused.
- Archives without `manifest.json` (all archives written before 1.18.0) still appear in the backup list with name, size,
  and label, but preview, inspect, and restore refuse them.

### Preview

- `POST /api/saves/restore/preview` classifies the members named in `selected` (every member when it is omitted) as `add`
  (absent locally), `overwrite` (present with a different SHA-256), or `missing` (in the manifest but absent from the
  archive), and counts `unchanged`.
- `remove` is always empty and present only for wire compatibility: a restore never deletes local files.
- Each bucket is capped at 200 rows; `counts` holds the untruncated totals and `truncated` names the cut buckets.
  `conflict` is true when anything would be overwritten.
- `selected` is an optional list of archive member names. It must be non-empty, within the 50,000-member cap, and a
  subset of the archive; it can never select something the archive does not contain.

### Restore safety

- `POST /api/saves/restore` accepts `backup`, `selected`, `dry_run`, and `confirm_conflict`. It runs as a job (HTTP 202 with
  `job_id`). A dry run returns the destinations without writing. A real restore with overwrites returns HTTP 409 and the
  preview unless `confirm_conflict` is true.
- Before writing, a real restore takes a safety backup labelled `before-restore`, recording the emulator/core/slot of the
  game's first configured save path. The safety backup is skipped only when no configured save root exists, because a
  missing tree is exactly when a restore is needed.
- Archive validation rejects absolute paths, `..` components, NUL, duplicate members, more than 50,000 members, any
  member over 4 GiB, and more than 32 GiB expanded in total.
- Restore destinations are resolved inside their matched root, symlink components are rejected before and after
  directory creation, and each file is written with `atomic_copy_stream` at mode `0600`.
- Backup, preview, and restore share one reentrant lock (`saves.SAVE_SYNC_LOCK`). Cloud sync's statistics upload
  (`cloud_sync.sync_statistics`) takes the same lock; sync does not read or upload save archives.

### Retention and toggle

- `save_backup_limit` (0–500, default 10) trims the oldest archives after a route backup or an on-close backup; `0` keeps
  all. Restore safety archives are counted in that list and can be trimmed the same way.
- `save_management_enabled` (default true) gates the save routes, including preview and manifest reads. When it is off,
  reads answer `{"enabled": false}` and writes are refused.

### HTTP surface

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/saves?id=` | Backup list with `name`, `size`, `mtime`, `label`, `emulator_id`, `core`, `slot`; newest first, capped at 200 |
| GET | `/api/saves/manifest?id=&backup=` | Manifest and the files the archive would restore |
| POST | `/api/saves/backup` | Queue a backup job (HTTP 202); trims to `save_backup_limit` |
| POST | `/api/saves/restore/preview` | Synchronous preview; body `id`, `backup`, optional `selected` |
| POST | `/api/saves/restore` | Queue a restore job (HTTP 202); body `id`, `backup`, optional `selected`, `dry_run`, `confirm_conflict` |

### UI

- Library details pane, "Save management" card (`static/library.js`): "Back up now", "Version history", and the backup list.
- Backup list entries open the restore dialog (`restoreSaves` in `static/sessions.js`): one checkbox per file, "Select all",
  "Select none", "Dry run", and "Restore selected". A restore that would overwrite files asks for confirmation and lists them.
- "Version history" (`openSaveHistory` in `static/sessions.js`): lists backups newest first. Selecting one shows its new,
  overwritten, unchanged, and missing counts, with "Restore this version" or "Restore anyway".
- Settings: "Enable save management and backups" and "Save backup retention limit" (Sessions panel).

## Alternatives considered

- **Compare only mtime and size.** Rejected: copies across filesystems and emulator restarts change mtime without
  changing content, so the preview would label unchanged files as conflicts and vice versa.
- **Restore the whole archive and delete files absent from it.** Rejected: a save backup is not authoritative over
  files the user created after it. `remove` stays empty by design.
- **Write a separate sidecar manifest.** Rejected: the archive would stop being a single portable file, and the
  manifest could be separated from the bytes it describes.
- **Trust archive member paths and root membership.** Rejected: a crafted ZIP or a replaced save tree could redirect
  a write outside the save roots or through a symlink.
- **Skip the pre-restore safety backup.** Rejected: selective restore still overwrites files, and the safety archive
  is the only local undo.

## Evidence

`tests/test_saves.py` covers manifest entries and member layout, archive and directory permissions, dry run, selected
restore, single-file and relative roots, a root missing at backup time, add/overwrite/missing preview, the 200-row bucket
cap, backup metadata round trip, and the `before-restore` safety archive. `tests/test_save_management_toggle.py` covers the
disabled read and write paths. `tests/test_sweep_residuals.py` (`TestSaveSyncLock`) covers the shared lock, and
`tests/test_parity_features.py` covers `enforce_backup_limit`. `tests/test_bug_sweep_api.py` covers unknown-game responses
on the save routes.

## Consequences

- New archives are self-describing. The version history shows counts for a selected backup, and the restore dialog shows
  each file before anything is written.
- Archives written before 1.18.0 have no manifest. They stay listed, but restoring them is not supported at 1.18.0.
- Preview compares the manifest's SHA-256 values with hashes of the local files it compares, reading each compared local
  file once. The restore does not re-hash the bytes it writes.
- A restore never removes local files, and a failed write can leave an earlier file restored; the safety backup is the
  recovery point.
- A backup of a save tree that does not exist is refused unless the caller passes `allow_empty` (the safety-backup path
  does). In that case nothing is written and the restore continues.
