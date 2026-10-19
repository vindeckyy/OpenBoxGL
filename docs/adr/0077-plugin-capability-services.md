# ADR 0077: Plugin capabilities and the bounded service channel

**Date:** 2026-10-18
**Status:** Accepted (1.18 unreleased)

## Context

The v1 plugin API exposed one-way hooks and command output. LaunchBox plugins can add storefronts, metadata and media
providers, achievement/playtime sources, emulator settings, and discovery lists, and need read access to a bounded slice
of the host library to do it. Handing a subprocess unrestricted data access would break the existing sandbox model;
adding a new RPC protocol would create a second plugin API to maintain.

## Decision

- Capabilities are optional manifest declarations paired with a hook. `storefront`, `metadata`, `media`, `stats`,
  `emulator_settings`, and `discovery` each require their corresponding hook; a capability without a hook is a
  manifest error. Existing manifests remain valid and keep the legacy 5 s hook budget.
- Capability hooks receive `plugin_data`, non-secret `settings`, and `credentials` in their payload. Credentials never
  enter `before_launch`, `after_session`, `library`, `command`, or `events`, and hook results cannot write credentials.
- A capability hook may return up to eight `service_requests`. The host validates the action against a read-only
  allowlist (`library.query`, `achievements`, `profile`, `activity`, `leaderboards`, `catalog.search`,
  `metadata.search`, `media.search`), executes it with the documented caps, and re-invokes the same hook once with
  `service_results`. A second request block is rejected, so a plugin cannot recurse.
- Services are host-internal. No HTTP route exposes the service dispatcher to the browser.
- Plugin notifications resolve through the optional bounded `messages` table, using the active locale, then English,
  then the raw key. Data bags are 1 MiB objects under `plugins/data`; credentials are string-only files under
  `plugins/credentials`. Both are owner-only. Backups archive plugin data bags and blank password-format settings,
  but exclude the capability credential store.
- There is no standalone `services` hook. Only capability hooks may request host services.
- Capability providers are merged through per-domain parity modules. Storefront imports tag games with
  `plugin_source`/`plugin_source_name`; disabling or removing the plugin deletes those persisted games and emits
  `game_removed`.

## Evidence

`tests/test_plugin_capabilities.py`, `tests/test_plugin_storage.py`, `tests/test_plugin_notifications.py`,
`tests/test_plugin_services.py`, `tests/test_plugin_storefront.py`, `tests/test_plugin_providers.py`,
`tests/test_plugin_stats.py`, `tests/test_plugin_emulator.py`, `tests/test_plugin_discovery.py`,
`tests/test_parity_admin_http.py`, and `tests/test_backup_redaction.py` cover manifest validation, credential
redaction, localized messages, service bounds, storefront pagination, provider URL safety, monotonic stats, emulator
argument merging, discovery list caps, disable/removal cleanup, and backup redaction. `docs/plugin-api.md` is the
published contract.

## Consequences

- The plugin API stays v1 and additive. A plugin that declares no capability behaves exactly as before.
- A capability provider failure is recorded in its result or job error list and never rolls back already-applied
  metadata, media, or imported games.
- The service channel is deliberately read-only and bounded. A future write-capable service needs a separate design,
  not a widened action string.
