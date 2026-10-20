# ADR 0084: Per-source storefront auto-import and scan status

**Date:** 2026-10-19
**Status:** Accepted (1.18.0)

## Context

`storefront_auto_import` was a map of booleans, one per store. That could not express where an imported game belongs,
whether owned-but-uninstalled entries are wanted, or which platforms a user never wants imported. A storefront scan
also ran every source blindly: an absent launcher, a slow catalog, or a provider error could delay the rest of the
scan, and the page had no per-source result to show.

Persisted settings are a compatibility surface. A new shape must not erase existing choices when an older client or a
partially migrated library is read.

## Decision

### Settings shape

- `storefront_auto_import` maps a source id to `{enabled, platform, include_uninstalled}`. Sources are `steam`,
  `heroic`, `lutris`, `gameyfin`, `gog`, `ea`, `ubisoft`, and `plugin:<id>`. Epic is not an auto-import source (see
  Known gaps).
- The map defaults to empty, so every source is off. Missing object fields default to `enabled: false`,
  `platform: ""`, and `include_uninstalled: false`.
- `_clean_storefront` (`handlers/settings.py`) casefolds keys and drops unknown keys and keys over 80 characters. It
  rejects more than 20 entries and a `platform` over 80 characters.
- A boolean value is accepted and normalized to the object shape. Settings saves always rewrite
  `storefront_auto_import`, so the object shape is stored on the next save, even a partial one. Readers accept both
  shapes until then.
- `auto_import_excluded_platforms` is a list of up to 100 platforms, each truncated to 80 characters. The auto-import
  worker applies it to every source, compared case-insensitively.

### Auto-import worker

- `auto_import_worker` (`pkg/state/imports.py`) runs as the `auto-import` job from boot. It ticks every 10 seconds and
  backs off to 300 seconds after a tick that imports nothing. Each tick imports watched folders, then each enabled
  storefront source, then emulator scan configs with `auto_update`.
- A disabled source is never invoked. Steam, Heroic, and Lutris run on every platform. GOG, EA, and Ubisoft run only on
  Windows (`pkg/parity/parity_storefront_windows.py`). Gameyfin uses its catalog. Each `plugin:<id>` uses
  `plugin_storefront_import`.
- For each enabled source the worker runs the importer, applies `platform` when set, drops entries with
  `store_installed` false unless `include_uninstalled` is true, drops excluded platforms, and calls
  `merge_imported_games` with the source identity. The manual import routes use the same identity keys, so a re-import
  updates rather than duplicates.
- Games the merge adds or tracks are queued for provider media through `enqueue_import_media` (ADR 0080).

### Known gaps at 1.18.0

- On every page load, `runStartupStorefrontImports` (`static/imports.js`, run from `static/app.js`) calls the manual
  import routes for enabled Steam, Heroic, Lutris, and Gameyfin sources (`/api/import/steam`, `/api/import/heroic`,
  `/api/import/lutris`, `/api/storefront/import`). Those routes do not apply `platform`, `include_uninstalled`, or
  `auto_import_excluded_platforms`; only the worker does. Identity merging prevents duplicate games.
- The Storefront Manager's Epic checkbox (`storefrontAutoImportEpic`, `static/storefront.js`) builds an `epic` entry,
  and `runStartupStorefrontImports` reads `settings.epic`. `_clean_storefront` drops `epic`, so Epic auto-import does
  not run.

### Scan worker

- `POST /api/v2/storefront/scan` (`handlers/imports.py`) takes an optional `sources` list and `include_uninstalled`
  (default `true`). It returns `202` with `job_id` and the resolved `sources`. A list over 20 entries is rejected;
  duplicates are removed. Each entry must be a scan source: `steam`, `heroic`, `lutris`, `gameyfin`, or an enabled
  `plugin:<id>`. GOG, EA, and Ubisoft are not scan sources.
- The scan runs as the `storefront-scan` job. Each source runs in a spawned child process with a 30-second wall-clock
  limit (`STOREFRONT_SCAN_TIMEOUT`). A timeout records `timeout`, and a provider error records `error`. The scan
  continues, and earlier sources stay applied.
- A source that is neither present on the machine nor represented in the library is recorded as `skipped` without
  being scanned. A scanned source is `ok` when at least one game matched and `skipped` otherwise. The worker updates
  `store_installed`, `install_dir`, `path`, `launch`, `install_uri`, and `repair_uri` on matching games. It never
  deletes games or rolls back. `games` counts the matched games.
- Progress is written to `<data>/storefront-status.json` (owner-only, atomic) as
  `{sources: [{id, name, status, installed, last_scan, error, games}]}`. Statuses are `pending`, `running`, `ok`,
  `skipped`, `timeout`, and `error`. Pending and remaining sources appear before the scan finishes.
  `GET /api/v2/storefront/status` returns it.
- The scan runs on request only: the Storefront Manager's "Refresh storefront status" button, or the API. There is no
  startup scan.
- `GET /api/v2/storefronts` lists the built-in ids (`steam`, `heroic`, `lutris`, `gameyfin`, `gog`, `ea`, `ubisoft`)
  and enabled plugin storefronts for the UI.

## Alternatives considered

- **Keep one boolean per store and add separate keys for platform and uninstalled.** Rejected: the options describe
  one source and must migrate together; separate keys would multiply the shape and leave partial states.
- **Reject the old boolean shape.** Rejected: a library written by an earlier release would fail its first settings
  save, and readers load settings before any migration can run.
- **Scan every source even when it is absent and unused.** Rejected: a missing launcher only produces an error row and
  delays the sources the user actually has.
- **Run all sources in one process and abort on the first failure.** Rejected: a slow catalog would consume the whole
  scan and one provider error would hide the rest.
- **Delete or mark games missing when a scan fails.** Rejected: a scan is a refresh; absence of evidence is not a
  deletion.

## Evidence

`tests/test_settings_schema.py` covers the native storefront keys, boolean normalization, and unknown-key dropping.
`tests/test_state_imports.py` covers auto-import for Windows storefronts, plugin storefronts, Gameyfin with excluded
platforms, and worker error handling. `tests/test_storefront_scan.py` covers plugin source discovery, installed-state
updates, `installed_only` propagation to the plugin catalog, unknown-source rejection, and the skipped state. No named
test covers the scan timeout or provider-error branches.

## Consequences

- Existing boolean choices keep working and are rewritten to objects on the next settings save.
- A source can be scanned without importing owned-but-uninstalled games, and imports can be pinned to a platform.
- A failed or timed-out source is visible in the status file and does not block or roll back the rest of the scan.
- Scan status is derived state; deleting the file only empties the status view until the next scan.
- The browser startup path ignores the per-source options until it is changed (see Known gaps).
