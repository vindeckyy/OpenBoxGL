# Portable library

A portable library keeps its game paths relative to a library folder. Move the folder to another drive, machine, or NAS mount, point OpenBox at it, and the games resolve without rewriting each game. Use **Settings > Library > Move library**. Changing the folder or the path mode outside the move flow is refused, because it would change stored paths without a preview.

## What moves and what does not

| Moves with the library folder | Stays where it is |
|---|---|
| Game ROMs and disc images under the library folder | Saves, screenshots, plugin data, and the data directory |
| Extra-app paths (`applications`, `versions`, `documents`) under the folder | Media under the data directory (it moves with the data directory) |

The data directory itself is not moved by this feature. To move it, set `OPENBOX_DATA_DIR` and copy the folder while OpenBox is closed.

## How the move works

OpenBox never copies or deletes files. The flow is:

1. Move the folder with your file manager.
2. Open **Move library**, choose the new folder, and run **Preview move**. The preview counts the paths that become portable, the paths that stay absolute inside the old folder, the paths that are missing, the paths outside the folder, and the symlinks that point outside it.
3. Apply. OpenBox rebases the paths that followed the folder, sets the library folder, and switches to portable mode. The apply recomputes the plan and refuses with `MOVE_PLAN_STALE` if the library changed after the preview.

## Stored form

Paths under the library folder are stored as `@library/<relative path>`. Anything else stays absolute. A drive path is stored with forward slashes, and UNC paths are never tokenized. A token cannot escape the folder: `..`, a drive prefix, a leading separator, a NUL byte, or a Windows alternate data stream name is refused.

## Limits

- A library folder on a removable drive must be mounted before launch. A missing folder is reported as `LIBRARY_ROOT_MISSING`.
- A symlink that resolves outside the library folder is not tokenized.
- Matching is lexical. Case is folded on Windows and macOS only. Unicode spellings are compared in NFC, and the stored spelling is kept.
- The Windows and Linux path rules are tested as pure functions on every platform. Running the library on each operating system is a manual step (see `docs/archive/2.0-PLATFORM-EVIDENCE.md`).

## Checking the library

**Settings > Library > Check integrity**, `openbox --check-library`, or `GET /api/v2/library/integrity` reports:

- game problems: `game_path_missing`, `game_path_unreadable`, `game_path_outside_root`, `path_token_escape`, `library_root_missing`, `game_duplicate_id`;
- media problems, from the media manifest in the data directory: `media_missing`, `media_changed` (size or mtime moved and the hash differs), `media_untracked` (a file the app did not record).

Each category carries a count and the first examples. Nothing is repaired by a check. `openbox --repair-library` (or the repair action) rebuilds the media manifest and prunes expired trash after you confirm.
