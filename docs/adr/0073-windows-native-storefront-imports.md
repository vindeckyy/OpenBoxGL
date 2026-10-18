# ADR 0073: Windows native storefront imports (GOG, EA app, Ubisoft Connect)

**Date:** 2026-10-18
**Status:** Accepted (1.17.0)

## Context

OpenBox on Windows discovered installed games only from Steam and Epic, although GOG Galaxy, the EA app, and
Ubisoft Connect are common launchers. 1.15's deferred list named these importers as the largest parity gap.
Reading their local state is read-only and needs no API account or network access.

## Decision

- **Discovery is local and best-effort.** GOG is read from `HKCU\Software\GOG.com\Games` and the HKLM views plus each
  game's `goggame-*.info`; EA from `%ProgramFiles%\EA Games` and the `installerdata.xml` inside each game; Ubisoft
  from `HKCU\SOFTWARE\Ubisoft\Launcher\Installs` and `HKLM\SOFTWARE\WOW6432Node\Ubisoft\Launcher\Installs`.
- **Registry values and manifests are untrusted.** Provider ids must be digits. Install dirs must be absolute and
  exist. A GOG executable taken from a play task or the registry must stay inside the install dir. Commands are argv
  lists, never a shell.
- **Launch paths are the storefront's own.** GOG games run their executable directly. EA and Ubisoft games launch
  through the `origin2://` and `uplay://` handlers with digit-validated ids. An uninstalled launcher breaks its
  launches, which is reported by the session error.
- **Re-import matching uses provider ids.** `gog_id`, `ea_id`, and `ubisoft_id` are optional game fields, used by
  merge, identity, exclusion, export, and projection code. `_identity_payload` appends an id only when it is non-empty,
  so existing stable game ids do not change.
- **The new provider ids stay local.** They are not added to `CATALOG_FIELDS`; doing so would make a mixed-version
  library-sync peer reject the whole event. The stable `game_id` already crosses that boundary.
- **Manual import only.** There is no startup auto-import for these three sources in 1.17.0.

## Evidence

`tests/test_parity_storefront_windows.py` covers each source with fixtures, path escape, malformed manifests, missing
install dirs, and non-digit ids. `tests/test_windows_storefront_http.py` covers re-import dedupe and error reporting.
`docs/reliability.md` rows 87-92 record the per-launcher manual procedure.

## Consequences

- The importer module is Windows-facing but platform-neutral: registry readers and the URI opener are injected in
  tests, so the Linux gate exercises the logic.
- Xbox PC package scanning and owned-but-uninstalled catalogs remain out of scope.
- If a launcher changes its registry or manifest format, the adapter is skipped with an error; it never mutates the
  launcher or the library.
