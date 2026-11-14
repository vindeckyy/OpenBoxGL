# OpenBox 3.0.0

**OpenBox 3.0 keeps your library in one JSON file and removes the three bridges 2.1 scheduled for this release. The formal scale stays 20,000 games.**

A library that is already schema 7 opens unchanged. Nothing in the file format moved: schema 7, backup format 2, sync format 3, and `/api/v2` are the same contracts 2.1 shipped. A 2.1 build can open a 3.0 library. A 3.0 build does not upgrade an older library just because you opened it.

The complete list is in [CHANGELOG.md](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/CHANGELOG.md), and the code changes are in [v2.1.0...v3.0.0](https://github.com/vindeckyy/OpenBoxGL/compare/v2.1.0...v3.0.0). The upgrade steps are in [upgrade-3.0.md](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/upgrade-3.0.md).

## Three things that change

### 1. Opening a library no longer migrates it

- **Schema 7 opens as it is.** There is no new library format in this release.
- **Schema 6 waits for `openbox --migrate-library`.** Startup, including crash recovery, leaves the file byte for byte. The command still writes `library.v6.backup.json` before it replaces the primary file.
- **Schema 1 through 5 needs OpenBox 2.1 first.** Open that library once on 2.1, then install 3.0, or stay on 2.1. 3.0 will not rewrite those files.
- **The error is `STATE_SCHEMA_TOO_OLD`.** The UI explains it. Nothing was changed.

### 2. Rollback and the v1 plugin runner are gone

- **`openbox --rollback-library` exits 4 and changes no file.** It names OpenBox 2.1.0 as the build that restores `library.v6.backup.json`. Copies already on disk are left where they are.
- **The rollback button is gone.** `POST /api/v2/setup/migration/rollback` stays registered so old clients get a `409` with `ROLLBACK_REMOVED` instead of a missing route. It does not restore anything and it does not shut the app down.
- **Plugins must say `"api_version": 2`.** A missing version and an explicit `1` both fail with `PLUGIN_API_RETIRED`. The message is `Plugin api_version is required and must be 2. OpenBox 3.0 removed the v1 runner.` The v2 JSON Lines protocol is unchanged.

### 3. Python 3.11 is the floor

- **Source checkouts on Python 3.10 stop before the server binds.** The message names the interpreter you actually ran.
- **3.12 is what the AppImage and the macOS bundle ship.** CI still tests 3.11 and 3.12. Do not use 3.12-only syntax.
- **The AppImage is still built on Ubuntu 22.04** so the highest glibc symbol stays `GLIBC_2.35`. The build pins CPython `3.12.16-1+jammy1`.

## What stays the same

| Contract | 2.1.0 | 3.0.0 |
|---|---|---|
| Public API | `/api/v2` | `/api/v2` |
| Native host channel | `/api/host` | `/api/host` |
| Library schema | 7 | 7 (no migration on open) |
| Backup format | Write 2, read 1 and 2 | Write 2, read 1 and 2 |
| Plugin API | 2, with the v1 adapter | 2 only |
| Sync format | 3 | 3 |
| Canonical store | `library.json` | `library.json` |

Deleting `sqlite_readmodel.db` still loses nothing. The projection rebuilds from the JSON file.

## Also in this release

- **Lifecycles are declared in one table (ADR 0107).** Operations check that table. The jobs panel, metadata sync, and match job share one set of state names. `/api/metadata/status` reports a running sync as `job` and a running match as `match_job`, so one no longer clears the other's progress. Launch Audit stays on its running button while a run is `cancelling`.
- **Five UI machines stay declared only.** Setup, the party wheel, the picker, Big Box overlays, and the repair wizard are listed and not enforced.

## Known limits

- The macOS bundle is ad-hoc signed. Notarization is unchanged.
- Some features are Linux-only and hidden on Windows and macOS.
- OpenBox 1.19 cannot read a schema 7 library.
- Flathub is not submitted.

## Not tagged

The 50,000-game run on 2026-11-13 (Apple M4, Python 3.15.2) met the write budget and missed the RSS budget. Favorite mutation p95 was 1174.2 ms and bulk mutation p95 was 1319.5 ms, both under 1500 ms. Server RSS after the benchmark was 2163.3 MB, over the 1500 MB budget. The formal scale stays 20,000. Do not tag 3.0.0 on this measurement. No soak was run. The numbers are in `docs/development/PERF.md` and ADR 0108.
