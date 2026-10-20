# ADR 0082: Platform rename and delete with scoped media migration

**Date:** 2026-10-19
**Status:** Accepted (1.18.0)

## Context

A platform is not a single field. It is `game["platform"]` plus a key in ten settings maps
(`platform_categories`, `theme_by_platform`, `image_group_by_platform`, `cover_model_by_platform`,
`model_back_by_platform`, `model_case_by_platform`, `model_rotation_lock_by_platform`, `platform_documents`,
`platform_videos`, `emulator_platform_settings`), a `profiles` entry, filter-playlist rules, filter-preset rules, and
per-emulator platform overrides under `state["emulator_settings"]`. Renaming by editing games alone leaves every
override attached to the old name. Deleting a platform had no defined meaning for games, overrides, or media.

Platform media lives under `<data>/media/<platform>/`, but a game may also point at media elsewhere. A migration that
moved every referenced file could touch shared artwork or files outside the media root.

## Decision

### Shared rules

- A platform name is required, at most 80 characters, and may not be `.`, `..`, or contain `/`, `\`, NUL, CR, or LF.
- Rename and delete run inside `transact_state`, so the field/map changes commit together.

### Rename

- The source must currently exist as a game platform. The target is rejected when it collides with any existing game
  platform, a `profiles` key, a key in any of the ten platform maps, or an emulator platform override, even when the
  target has no games yet. Renaming to the same name is a no-op.
- One pass updates `game["platform"]`, all ten settings maps, `profiles`, `emulator_settings[*].platforms`, filter
  playlists whose `rules.platform` matches, and `filter_presets[*].rules.platform`.
- `migrate_media` is opt-in. When set, only files under `<data>/media/<old>/` are moved, and only when the game's
  stored path is an approved media path that still exists. Files outside that folder are skipped and their metadata is
  left unchanged.
- A destination that already holds an identical file is deduplicated (the source is removed); a different file at the
  destination is an error.
- The move is all-or-nothing for the call: every move is recorded, and on any failure the moved files are returned to
  their sources and all game media fields and screenshots are restored from snapshots before the error propagates.
- A cross-device move copies, flushes the destination through a writable handle (tolerating filesystems that cannot
  `fsync` it), and only then removes the source, so a crash cannot leave neither name.

### Bulk platform edits

- Bulk editing the `platform` field accepts `migrate_media` on `POST /api/games/bulk` and `POST /api/games/bulk-wizard`.
  The metadata update is authoritative: groups of selected games are migrated per source platform, and a failed group never
  rolls back the field change.
- The response reports `media_moved` and `media_errors` (`[{platform, error}]`) so the caller can retry the move.

### Delete

- `POST /api/v2/platform/delete` takes `name`, `confirm`, and optional `delete_media`. A `confirm` that does not equal `name`
  is rejected before any state change.
- Every game on the platform becomes a Trash entry with `trashed_at`, then is removed from `games`. The ten map keys, the
  `profiles` entry, and emulator platform overrides are removed with it. Filter playlists and presets that name the platform
  are not changed.
- Trash is bounded to the newest 200 entries kept for 30 days (`state_store.TRASH_CAP`, `TRASH_MAX_AGE_DAYS`), and state
  writes prune it. A delete larger than the room left evicts the oldest entries.
- `delete_media` is opt-in. Deletion is confined to `<data>/media/<name>/`, refuses a symlinked or escaping root, and
  removes only files and directories under it. Filesystem errors during media deletion are tolerated; the response reports
  the number of deleted files as `media_deleted`.

### API and UI

| Method | Path | Request | Response |
|---|---|---|---|
| POST | `/api/v2/platform/rename` | `old`, `new`, `migrate_media` | `games`, `media_moved`, `media_skipped` |
| POST | `/api/v2/platform/delete` | `name`, `confirm`, `delete_media` | `games`, `media_deleted` |
| POST | `/api/games/bulk`, `/api/games/bulk-wizard` | `ids`, `changes`, `migrate_media` | `media_moved`, `media_errors` |

- Platform view, "Platform" card (`static/library.js`): "Rename platform" asks for a new name, then "Move media files".
  "Delete platform" requires the typed name, then asks "Delete media".
- Bulk edit dialog (`index.html`): "Move media files for the selected games when the platform changes". `static/app.js` sends
  it to `/api/games/bulk-wizard`, only when the platform is among the changes.

## Alternatives considered

- **Rewrite only `game["platform"]`.** Rejected: categories, themes, covers, documents, videos, emulator overrides,
  profiles, and filter rules would silently keep the old name.
- **Allow renaming onto an existing platform.** Rejected: it would merge two platforms' games and override maps, which
  is destructive and has no undo.
- **Move every media path a game references.** Rejected: paths can point outside the platform folder and can be shared
  between games; the media root prefix is the only safe ownership boundary.
- **Best-effort moves without rollback.** Rejected: a partial move leaves the library pointing at a mix of old and new
  paths.
- **Delete games and their media immediately.** Rejected: deletion is reversible through Trash, and media deletion is
  a separate explicit choice.

## Evidence

`tests/test_parity_platform_admin.py` covers metadata migration with unrelated platforms preserved, target collision, invalid
names, the default media root, identical/conflicting/cross-device moves, skipping unapproved and outside paths, rollback of
prior moves, moving only the platform folder, Trash on delete, scoped media deletion, and the bulk media move
(`test_bulk_platform_change_moves_only_selected_games`). `tests/test_parity_admin_http.py` covers the rename and delete routes.

## Consequences

- Rename is safe for users who never opted into media migration: their overrides still move, their files do not.
- Media migration is a filesystem operation inside a state transaction. It runs before the transaction commits and
  can make a large rename slower on a slow disk.
- Games whose media lives outside the platform folder keep their existing paths and are not moved.
- Bulk media failures are visible and retryable without losing the platform field change.
- Delete keeps games recoverable through Trash only within Trash bounds; media deletion is irreversible and therefore separate.
