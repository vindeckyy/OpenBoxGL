# ADR 0035: Library sync via mounted folder

**Date:** 2026-09-04
**Status:** Accepted

## Context

`cloud_sync.py` synchronizes game statistics (play_count, playtime, last_played, progress, rating, favorite) via a mounted folder. Users with multiple devices want to sync their full library — not just stats — so a game added on device A appears on device B without manual re-import. This must stay local-first: no accounts, no OAuth, no cloud service.

## Decision

Extend `cloud_sync.py` with full library publish/pull via the same mounted folder pattern:

1. **`publish_library(state, folder, device_id)`**: writes `openbox-library.json` to the mounted folder. The file contains all games keyed by `game_key()`, each with `updated_at` timestamp and `device_id`. Tombstones from other devices are preserved (carried forward) unless the game was re-added locally.

2. **`pull_library(state, folder, device_id)`**: reads `openbox-library.json` and merges into the local library. Conflict resolution is last-writer-wins by `updated_at` timestamp. Tombstones delete local games that match the tombstone key. Returns added/updated/deleted/skipped counts and the merged games list.

3. **Routes**: `POST /api/v2/library/sync/publish` and `POST /api/v2/library/sync/pull`. Both use the existing `cloud_folder` setting. Pull persists via `transact_state`.

4. **Tombstones**: when a game is deleted on device A and published, other devices that pull will delete their local copy. Tombstones are keyed by `game_key()` and carry `deleted_at` + `device_id`. Re-adding a game locally clears its tombstone on next publish.

5. **Conflict resolution**: last-writer-wins by `updated_at`. This is deliberately simple — it handles the two-device workflow correctly. Multi-device conflicts with concurrent edits may lose data; this is a known ceiling documented in a `ponytail:` comment. Upgrade path: version vectors.

6. **File locking**: reuses the existing `_sync_lock()` (fcntl flock) to prevent concurrent read/write corruption.

7. **Safe writes**: uses `atomic_write_text()` (via `backend_io`) to prevent partial writes.

## Consequences

- Two-device library sync works via any mounted folder (Syncthing, USB, network share).
- No accounts or external services — fully local-first.
- Tombstones prevent deleted games from reappearing on pull.
- Stats sync (`sync_statistics`) remains separate and continues to work as before.
- Multi-device concurrent edits may lose data (last-writer-wins); documented ceiling.
