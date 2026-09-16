# ADR 0053: Save history

**Date:** 2026-09-16
**Status:** Accepted

## Context

Per-game save archives already exist: `saves.backup_saves` writes
`<stamp>-<label>.zip` with a `manifest.json`, and `saves.restore_saves`
verifies the archive against the game's configured roots before writing.
`GET /api/saves` returned only name and size, so users could not tell a manual
backup from an automatic or pre-restore safety copy, could not verify an
archive without restoring it, and had no visible retention story.

## Decision

- `pkg/parity/parity_save_history.py` is a read/verify/retention layer over
  the existing archives:
  - `save_history()` lists versions newest first with `name`, `size`,
    `created_at`, `age_seconds`, the raw `label`, and a normalized `source`.
    Source vocabulary: `manual`, `auto` (`on-close`/`auto` labels),
    `pre-launch`, `before-restore` (`safety`), and `other` for foreign names.
  - `verify_version()` reads every member back (ZIP CRC plus a streamed
    SHA-256), re-validates entry safety and the manifest, and reports members,
    bytes, digest, game, and root count. A corrupt archive raises `ValueError`.
  - `retention_plan()` / `apply_retention()` keep the newest N archives and
    remove the rest; `keep <= 0` keeps everything, matching
    `saves.enforce_backup_limit`. Retention never invents versions: only files
    listed by the history are candidates.
  - `restore_version()` delegates to `saves.restore_saves`, preserving the
    manifest/root identity checks and the pre-restore safety copy.
- Routes under `/api/v2`: `GET /saves/history` (versions + counts + retention
  preview), `POST /saves/history/verify`, `POST /saves/history/restore`
  (queued through the existing job manager), and `POST /saves/history/prune`.
  The frozen v1 `/api/saves` routes are unchanged.
- `static/sessions.js` renders each version with its source badge, age, and
  size, plus Verify and Restore actions; failures surface through the shared
  error toast.

## Consequences

- Users can see what each archive is and prove it reads back before restoring.
- Verification is synchronous and reads archive bytes; it is honest about cost
  and bounded by the existing archive size limits.
- Restores still create a `before-restore` archive first, which then appears in
  the history like any other version.
