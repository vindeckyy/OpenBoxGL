# ADR-0008: Decomposition of webapp_state.py into focused state modules

Status: accepted (implementation diverged — see note)
Date: 2026-08-22
Updated: 2026-09-04 — implemented differently than specified below: `pkg/state/game_identity.py` and `pkg/state/cloud.py` were never created. Identity/merge helpers (`game_identity`, `consolidate_existing_games`, …) and `sync_cloud` live in `pkg/state/imports.py`; `clean_commands`/`run_configured_commands` live in `pkg/state/commands.py`. `pkg/state/` now holds 11 modules (cache, commands, _deps, imports, launch, media_probe, operations, registry, sqlite_readmodel, sse, __init__) and `webapp_state.py` is a 288-line thin re-export shim (not ~80 lines). The module table, file diagram, and "573 → ~80 lines" claims below describe the proposal, not the outcome.

## Context

`webapp_state.py` is a 573-line hybrid shim that owns the mutable module state
(TOKEN, locks, caches) and the service helpers that `web_app.Handler` mixin
methods and `web_app.main()` call as bare names. `web_app.py` and `handlers/*.py`
import from it so every reference resolves statically.

The file currently mixes three concerns:

1. **Module-level state** -- `TOKEN`, `ROOT`, `LOGGER`, `INSTALLS`, `METADATA_JOB`,
   `MEDIA_JOB`, `JOB_MANAGER`, `WATCH_STOP` and ~120 re-exported names from
   `pkg.state.cache`, `pkg.state.launch`, `pkg.state.media_probe`, and
   `pkg.state.sse`.

2. **Game identity and consolidation** -- `game_identity`, `_launcher_label`,
   `_filled_launch_command`, `_application_for_game`, `_append_unique_application`,
   `_merge_source_fields`, `_merge_imported_game`, `_index_existing_games`, and
   `consolidate_existing_games`. These functions encode how games from different
   sources (Steam, Heroic, Lutris, Gameyfin, Faugus, arcade ROMs) are identified,
   deduplicated, and merged.

3. **Import orchestration** -- `import_folder_path`, `merge_imported_games`, and
   `auto_import_worker`. These coordinate filesystem scanning, storefront imports
   (Steam/Heroic/Lutris/Gameyfin), emulator scan configs, and the auto-import
   watch loop.

4. **Cloud sync and commands** -- `sync_cloud`, `clean_commands`,
   `run_configured_commands`. These are standalone service functions with minimal
   coupling to the identity/import logic.

ADR 0005 identified the need for this decomposition but deferred it to avoid
import-graph churn. This ADR specifies the concrete decomposition strategy.

## Decision

Decompose `webapp_state.py` into four focused modules under `pkg/state/` plus a
thin re-export shim at the original location.

### Target modules

| Module | Responsibility | Functions extracted |
|---|---|---|
| `pkg/state/game_identity.py` | Game identity resolution, deduplication, and merge logic | `game_identity`, `_launcher_label`, `_filled_launch_command`, `_application_for_game`, `_append_unique_application`, `_merge_source_fields`, `_merge_imported_game`, `_index_existing_games`, `consolidate_existing_games` |
| `pkg/state/imports.py` | Import orchestration: folder scanning, storefront imports, auto-import watch loop | `import_folder_path`, `merge_imported_games`, `auto_import_worker` |
| `pkg/state/commands.py` | Launch command validation and execution | `clean_commands`, `run_configured_commands` |
| `pkg/state/cloud.py` | Cloud save synchronization | `sync_cloud` |

### Re-export shim

`webapp_state.py` becomes a thin re-export shim that:

1. Retains `TOKEN`, `ROOT`, `LOGGER`, `INSTALLS`, `METADATA_JOB`, `MEDIA_JOB`,
   `JOB_MANAGER`, `WATCH_STOP` as module-level state (these are the process-global
   singletons that other modules reference by name).
2. Re-imports every currently-exported name from its new canonical location so
   that `from webapp_state import game_identity` continues to resolve.
3. Retains all `pkg.state.cache`, `pkg.state.launch`, `pkg.state.media_probe`,
   and `pkg.state.sse` re-exports unchanged -- these are facade aliases already
   owned by `pkg/state/`, not decomposition targets.

### Dependency direction

```
webapp_state.py  (thin shim, re-exports only)
    |-- pkg/state/game_identity.py  (imports from parity_identity)
    |-- pkg/state/imports.py        (imports game_identity, importers, parity_import, ...)
    |-- pkg/state/commands.py       (imports openbox.load_state)
    +-- pkg/state/cloud.py          (imports cloud_sync, openbox.load_state, openbox.update_state)
```

No circular dependencies: `game_identity` depends only on `parity_identity` and
standard library. `imports` depends on `game_identity` and external parity modules.
`commands` and `cloud` are leaf modules with no intra-package deps.

## Phased migration

### Phase 1: Extract (no behavioral change)

1. Create `pkg/state/game_identity.py` with the 9 identity/merge functions.
   Move their imports (`parity_identity.cross_source_identity`,
   `parity_identity.source_family`, `parity_identity.source_identities`,
   `parity_storefront.catalog_entries_to_games`) into the new module.
2. Create `pkg/state/imports.py` with `import_folder_path`,
   `merge_imported_games`, `auto_import_worker`. Move their imports
   (`importers.*`, `parity_import.*`, `parity_import_policy.*`,
   `parity_emulator_defs.*`, `parity_gameyfin.*`, `parity_storefront.*`,
   `parity_media.normalize_video_fields`, `parity_media.enqueue_media_job`,
   `parity_media.media_types_from_settings`, `catalog.apply_progress_automation`)
   into the new module.
3. Create `pkg/state/commands.py` with `clean_commands`, `run_configured_commands`.
4. Create `pkg/state/cloud.py` with `sync_cloud`. Move `cloud_sync.sync_statistics`
   import here.
5. Update `webapp_state.py` to import everything from the new modules and
   re-export under the same names.

### Phase 2: Update callers

1. Update `handlers/*.py` and `web_app.py` to import directly from `pkg.state.*`
   modules where appropriate, bypassing the shim.
2. Update `tests/` imports to exercise the new canonical paths.

### Phase 3: Thin the shim

1. Remove dead imports from `webapp_state.py` that are no longer needed by any
   remaining caller.
2. Add a deprecation comment to the shim indicating new code should import from
   `pkg.state.*`.

## Rollback strategy

Each phase is independently revertible:

- **Phase 1 rollback**: Delete the four new modules, restore `webapp_state.py`
  from git. No caller changes, no behavioral difference.
- **Phase 2 rollback**: Revert caller import changes. The shim still re-exports
  everything, so all callers continue to work.
- **Phase 3 rollback**: Re-add removed imports to the shim. Callers importing
  from the shim still work.

The shim is never deleted in this ADR -- it remains as a compatibility layer
for one release, consistent with the parity shim approach in ADR 0003.

## Consequences

- **Positive**: `webapp_state.py` shrinks from ~573 lines to ~80 lines (module
  state + re-exports). Each new module has a single responsibility and can be
  understood, tested, and modified in isolation.
- **Positive**: Import graph becomes acyclic and explicit. The identity logic
  no longer pulls in `cloud_sync`, `importers`, and `parity_import` at module
  load time.
- **Positive**: `auto_import_worker` can be tested without importing the full
  state shim, reducing test isolation problems.
- **Negative**: Four new files to maintain. The re-export shim adds a layer of
  indirection that can confuse grep-based navigation.
- **Neutral**: No runtime behavior change. All existing callers continue to
  resolve names through the shim until Phase 2 updates them.
