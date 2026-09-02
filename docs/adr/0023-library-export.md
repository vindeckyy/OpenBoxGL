# ADR 0023: Library Export

**Date:** 2026-09-02
**Status:** Accepted

## Context

OpenBox had no general library export: only highscores export (`POST /api/highscores/export`) and the redacted portable backup JSON (`GET /api/backup`) existed. Users migrating between machines, auditing their collection in a spreadsheet, or sharing a view of their library had no supported path. The 1.7.2 backup-diff API covered internal restore scenarios, not data-out scenarios.

## Decision

Add a shareable-by-construction game export in `pkg/parity/parity_export.py` + `handlers/export.py`:

1. **Routes (additive v2):** `POST /api/v2/library/export` queues a durable job (Activity Center, cancellable); `GET /api/v2/library/export/download?file=` streams the file with `Content-Disposition: attachment`; `GET /api/v2/library/export/exports` lists existing exports (name, size, mtime).
2. **Formats:** JSON (`{application, kind, exported_at, count, include_media_paths, games[]}`) and CSV (one row per game, `DictWriter`).
3. **Scope (server-side, deterministic):** `all`, `platform:<name>`, or `playlist:<name>` (playlist membership by stable `game_id`). The client-side query grammar is intentionally not re-implemented server-side.
4. **Shareable by construction:** exports contain only the game-field projection (`EXPORT_GAME_FIELDS`); settings, credentials, webhooks, and history are never included, so `parity_redact` is not needed on this path. Media path fields are opt-in via `include_media_paths`.
5. **Storage:** `exports/openbox-library-<UTCstamp>.<fmt>` in the data dir, name pattern enforced (`EXPORT_NAME_RE`) plus a same-second collision counter; keep the newest 10 files (`prune_exports`). Download validation = name regex + directory containment + existence.
6. **UI:** Settings → Advanced "Export library" card (format/scope/media-paths pickers) with job polling via `/api/jobs` and an automatic download link on completion.

## Consequences

- Data-out is a first-class workflow alongside backup/restore and backup-diff.
- CSV flattens list fields with `;`; consumers should split on that separator.
- Export filenames are second-granularity; concurrent exports within one second get `-N` suffixes rather than silently overwriting.
- Future scopes (e.g., "export current filtered view") can extend `EXPORT_SCOPES` without breaking the contract.
