# ADR 0038: Library sync v2 — conflicts, mass-delete gate, tombstone GC

**Date:** 2026-09-04
**Status:** Accepted
**Amends:** ADR 0035 (library sync via mounted folder)

## Context

ADR 0035 shipped full-library publish/pull with last-writer-wins (LWW) conflict
resolution, tombstones, and fcntl locking. Multi-device use exposed four gaps:

1. Concurrent edits on two devices resolved silently — the loser never learned
   a conflict happened.
2. A stale remote could wipe a library: pulling tombstones for more than a
   tenth of local games applied without warning.
3. Tombstones accumulated forever; deleted games from retired devices left
   permanent residue in `openbox-library.json`.
4. Manual/shelf entries (`manual_entry: true`, ADR 0036) pulled onto a second
   device looked like missing-file rows instead of shelf rows, and the manifest
   said nothing about media (which was never synced, but nothing stated it).

## Decision

Extend `cloud_sync.py` (additive result/manifest fields only; LWW winners
unchanged; `sync_statistics` untouched):

1. **`conflicts[]` on pull**: when a remote record and the local copy differ in
   any field, pull appends `{game_key, local_updated_at, remote_updated_at,
   winner, fields_differ}`. The winner is still pure LWW by `updated_at`; the
   entry exists so the dialog can list it for review. No silent-merge claim.

2. **Mass-delete gate**: a pull whose tombstones would delete more than 10% of
   the local library returns `needs_confirm: true` with `deleted`/`local_count`
   counts and leaves the library unmutated. Resending with `confirm: true`
   applies it. The pull handler answers the unconfirmed case with HTTP 409
   `SYNC_NEEDS_CONFIRM` (new `api_errors.py` code, additive) and skips
   `transact_state`, so no mutation happens.

3. **90-day tombstone GC**: publish drops tombstones older than 90 days and
   reports the pruned count as `tombstones_gc`. Re-added games still clear
   their tombstone on publish (ADR 0035 behavior kept).

4. **Shelf rows**: pulled `manual_entry` games merge with `path_usable: true`
   so device B renders a shelf badge, not a missing-file error.

5. **Media explicit non-goal**: the publish manifest carries
   `media_synced: false`, and the sync dialog shows a "media stays per-device"
   notice next to the conflicts review list.

6. **Lock contention**: `_sync_lock()` uses a non-blocking `flock` and raises
   `CloudSyncError` with a readable "busy (another sync is holding the lock)"
   message naming the target, instead of blocking indefinitely. Reuses the same
   lock file and `atomic_write_text` path as before.

## Consequences

- Concurrent multi-device edits are visible (conflicts list) but resolution
  stays LWW — documented ceiling from ADR 0035, now surfaced, not silent.
- Destructive pulls gate on confirm; single deletes still flow through
  tombstones once confirmed.
- Tombstone residue expires after 90 days; manifests stay small.
- No accounts, services, or new dependencies; v1 route surface untouched
  (v2 responses only gain fields).
