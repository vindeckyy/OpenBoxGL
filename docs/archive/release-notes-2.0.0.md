# OpenBox release notes

**OpenBox 2.0.0 is the release where your library becomes portable, your integrations become contracts, and every supported platform gets its own install.**

This is the largest OpenBox release since the launcher became cross-platform. A library can now move to another drive or machine and keep working. The API, host channel, plugin API, backup format, and state format are versioned and gated, so the next release can change one without silently breaking another. Windows ARM64 joins the release, macOS keeps its universal bundle, and Linux keeps the AppImage and Flatpak channels.

The complete list is in [CHANGELOG.md](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/CHANGELOG.md), and the code changes are in [v1.19.1...v2.0.0](https://github.com/vindeckyy/OpenBoxGL/compare/v1.19.1...v2.0.0).

## Three promises for 2.0

### 1. Your library can move

- **Portable library.** Game paths under one library folder are stored as `@library/...` tokens. Move the folder to another drive, another computer, or a network share, point OpenBox at it, and the games resolve without rewriting every entry. Extra apps, versions, and documents under the folder move with it.
- **A move wizard that refuses stale plans.** **Settings > Library > Move library** previews the move, shows what will change, and applies it only when the preview still matches. Changing the folder or the path mode outside that flow is refused.
- **Integrity checks without changes.** **Settings > Library > Check integrity** reports missing, unreadable, and outside-folder game paths, plus media files that were changed, deleted, or added behind the app's back. Nothing is touched by a check.
- **A media manifest.** Downloads and captures are recorded in `media/manifest.json` with their size, mtime, and SHA-256, so the integrity check can tell a changed file from an untouched one.
- **Maintenance without a window.** `openbox --check-library`, `--repair-library`, `--migrate-library`, and `--rollback-library` run from the command line. `GET /api/v2/library/integrity` serves the same report to a script.
- **A compatibility report.** **Settings > About > Show compatibility report** lists the library format, each theme's token fallbacks, which backups restore on which version, unknown settings keys, the public API and host channel, and the sync folder.

### 2. Your integrations stop moving

- **The public API is `/api/v2`.** The retired `/api/v1` routes are off by default; `OPENBOX_ENABLE_V1_API=1` restores them through 2.0.x, and they are removed in 2.1. Unversioned `/api/*` paths stay routable as the internal layer through 2.0.x. `GET /api/v2/version` reports the app, API, state schema, plugin API, backup format, and sync format versions.
- **Errors have a stable shape.** API failures are `application/problem+json` with `type`, `title`, `status`, `detail`, `code`, `retryable`, and `request_id`. The old `error` field is still there, so existing clients keep working.
- **The native host has its own channel.** The Windows, Linux, and macOS windows talk to `/api/host/*` and send `X-OpenBox-API-Version: 2`. A host from another major API version is refused before the route runs, instead of failing halfway through a launch.
- **Backup format 2.** Every backup records a SHA-256 for each file. A restore checks every hash before writing anything and reports every member it wrote. A **1.19-compatible** export is available when an older install must read the archive.
- **Plugin API v2.** A plugin can declare `api_version: 2`, the oldest OpenBox that serves it, and the events it subscribes to. A v2 plugin runs through a JSON Lines runner with a 4 MiB output cap and read-only host services. A v1 plugin keeps its v1 runner and payloads unchanged.
- **OpenAPI and deprecation gates.** The generated OpenAPI document lists every public route, and a deprecation ledger pins sunset dates. The gates fail the build when the live surface drifts from either.

### 3. Big libraries spend less time waiting

- **Search rebuilds leave the request path.** A write queues a debounced rebuild of the SQLite read model. A search that arrives before the rebuild answers from the canonical library and reports `source: "json"`, and the next search is served from the projection. A corrupt projection is deleted and rebuilt; a projection that disagrees with the library drops back to JSON.
- **Filter chips count off the UI thread.** The platform and platform-category counts come from the search worker, so a large library no longer spends the UI thread counting chips on every render. Without a worker the counts fall back to the UI thread as before.

## Every supported platform

- **Windows ARM64 is a first-class download.** The release publishes `OpenBox-arm64-windows.zip` and `OpenBox-arm64-windows-native-host.exe` from the `windows-11-arm` lane. The host is built for ARM64, attested, signed, and verified like the x86_64 assets. The installer and updater select the right architecture on their own.
- **macOS keeps the universal bundle.** `OpenBox-macos.zip` runs natively on Apple Silicon and Intel. The 2.0 bundle is ad-hoc signed, not notarized, so the Privacy & Security confirmation on first launch and after each update still applies. The notarization machinery stays in the tree for a later release.
- **Linux keeps both artifact shapes.** The release publishes x86_64 and aarch64 AppImages with zsync metadata and a Flatpak. The AppImages keep the glibc 2.35 floor, so they run on the same distributions as 1.19.
- **The platform downloads are signed.** Both AppImages, both Windows archives and native hosts, and the macOS bundle ship with their SHA-256 sidecar and Ed25519 signature. SBOMs cover both AppImages, the Windows ARM64 build, and the macOS bundle. The installer scripts pin the release key and verify the artifacts against it.

## The first start after an upgrade

Upgrade from any 1.19.x release. On the first 2.0 start the library is upgraded from format 6 to format 7, a copy of the old file is kept as `library.v6.backup.json`, and `migration-report.json` records what changed. A readiness gate lists anything worth a look before the library is opened: a theme with an unknown token, a plugin with an API version this build does not serve, a missing sync folder, an archive with no format, a library over the formal scale, or a custom data directory. A backup can be written from the gate before the library opens.

Rolling back restores the 1.19 library byte-for-byte:

```
openbox --rollback-library          # shows what would be discarded
openbox --rollback-library --yes    # restores the 1.19 library
```

The full walkthrough is in [docs/upgrade-2.0.md](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/upgrade-2.0.md).

## Breaking changes

| Change | What it means | How to keep working |
|---|---|---|
| Public API is `/api/v2` | Scripts on `/api/v1` stop working; unversioned paths remain internal through 2.0.x | Move to `/api/v2`, or set `OPENBOX_ENABLE_V1_API=1` through 2.0.x |
| Library format 7 | A 1.x build cannot read the library after the upgrade | Automatic on first start; `openbox --rollback-library` restores the v6 copy |
| Backup format 2 | A 1.x build cannot read a new backup | Use the 1.19-compatible export for archives a 1.x install must read |
| Host channel | New hosts use `/api/host/*` | Update the host; the old paths stay as internal aliases through 2.0.x |
| Retired `/api/v1` routes | Off by default | `OPENBOX_ENABLE_V1_API=1` |

## Get OpenBox 2.0.0

| Platform | Download | Notes |
|---|---|---|
| Linux x86_64 | `OpenBox-x86_64.AppImage` | glibc 2.35 floor; zsync available |
| Linux aarch64 | `OpenBox-aarch64.AppImage` | glibc 2.35 floor; zsync available |
| Linux Flatpak | `OpenBox-x86_64.flatpak` | x86_64 |
| Windows x86_64 | `OpenBox-x86_64-windows.zip` | Includes the native host |
| Windows ARM64 | `OpenBox-arm64-windows.zip` | Includes the ARM64 native host |
| macOS | `OpenBox-macos.zip` | Universal; ad-hoc signed |

The macOS installer pins the release tag:

```bash
VERSION=2.0.0
curl --proto '=https' --tlsv1.2 --fail --location \
  --output install-macos.sh \
  "https://github.com/vindeckyy/OpenBoxGL/releases/download/v${VERSION}/install-macos.sh"
less install-macos.sh
OPENBOX_RELEASE_TAG="v${VERSION}" bash install-macos.sh
```

On Windows, extract the release archive and run `OpenBox\scripts\install.ps1`. On Linux, use the AppImage, the Flatpak, or `scripts/install.sh` as before.

## Also fixed in 2.0.0

- **The library commands work on Windows.** `openbox --rollback-library` refuses cleanly when the terminal cannot answer the confirmation, and a corrupt search projection is released before it is deleted, so it is rebuilt instead of being left broken.
- **A restored trash entry expires on every platform.** A `trashed_at` stamp written in UTC (`Z`) is read like any other, so an entry from a backup is pruned once it is old instead of being kept forever.

## Not in 2.0

- JSON stays the canonical store. SQLite remains a read model; the storage decision is recorded in ADR 0088 with its measurements.
- The formal library scale stays at 20,000 games. 50,000 was measured and reported as exploratory.
- Python 3.10 is still supported.
- Plugin API v2 is additive; v1 plugins keep working.

## Known limits

- The macOS bundle is ad-hoc signed, not notarized, so the first launch and each new build need the Privacy & Security confirmation.
- Linux-only features remain unavailable on Windows and macOS by design; they are hidden rather than broken.
- 1.19.x cannot read a format 7 library. Keep `library.v6.backup.json` until you are sure you do not need to roll back.
- `/api/v1` is off by default and is removed in 2.1. Use `OPENBOX_ENABLE_V1_API=1` during the 2.0.x bridge.

The [changelog](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/CHANGELOG.md) lists the main areas, not every change.
