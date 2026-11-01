# OpenBox release notes

OpenBox 2.0.0 turns the 1.x compatibility scaffolding into stable contracts: your library can move, your integrations stop moving, and every supported platform gets a clean install. Linux, Windows, and macOS share the same state format, API version, and release ladder. The complete list is in [CHANGELOG.md](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/CHANGELOG.md), and the code changes are in [v1.19.1...v2.0.0](https://github.com/vindeckyy/OpenBoxGL/compare/v1.19.1...v2.0.0).

## Your library can move

- **Portable library.** Game paths under one library folder are stored as `@library/...` tokens. Move the folder to another drive or machine, point OpenBox at it, and the games resolve without rewriting each game. **Settings > Library > Move library** previews the move and refuses a stale preview. Extra apps, versions, and documents under the folder move with it.
- **Library format 7.** Libraries upgrade on first start. The old file is kept as `library.v6.backup.json`, and `openbox --rollback-library` restores it. A rollback lists the changes it would discard first.
- **Library maintenance without a window.** `openbox --check-library`, `--repair-library`, `--migrate-library`, and `--rollback-library` run from the command line. `GET /api/v2/library/integrity` serves the same report.
- **Integrity check and media manifest.** **Settings > Library > Check integrity** reports missing, unreadable, and outside-folder game paths, plus media files that were changed, deleted, or added behind the app's back. Nothing is changed by a check; a repair rebuilds the media manifest.
- **Compatibility report.** **Settings > About > Show compatibility report** lists the library format, each theme's token fallbacks, which backups restore on which version, unknown settings keys, the public API and host channel, and the sync folder.

## Your integrations stop moving

- **The public API is `/api/v2`.** `/api/v1` and the unversioned routes are off by default; `OPENBOX_ENABLE_V1_API=1` restores them through 2.0.x, and they are removed in 2.1. `GET /api/v2/version` reports every contract version, and the OpenAPI document lists every public route. API errors use `application/problem+json` with a stable `code` and `request_id`.
- **The native host has its own channel.** Windows, Linux, and macOS windows talk to `/api/host/*` and send `X-OpenBox-API-Version: 2`. A window from another major version is refused before the route runs.
- **Backup format 2.** Every backup records a SHA-256 for each file; a restore checks them all before writing anything and reports every member it wrote. A **1.19-compatible** export is available when an older install must read a backup.
- **Plugin API v2.** A plugin may declare `api_version: 2`, the oldest OpenBox that serves it, and the events it subscribes to. A v2 plugin gets a JSON Lines runner with a 4 MiB output cap and read-only host services. A v1 plugin keeps the v1 runner and its payloads unchanged.
- **Search rebuilds off the request path.** A write queues a debounced rebuild of the SQLite read model. A search that arrives before the rebuild answers from the canonical library and reports `source: "json"`. The library filter chips count in the search worker instead of on the UI thread.

## Platform installs

- **Windows ARM64.** The release publishes `OpenBox-arm64-windows.zip` and `OpenBox-arm64-windows-native-host.exe` from the `windows-11-arm` lane. The host is built for ARM64, attested, signed, and verified like the x86_64 assets, and the installer and updater select it by architecture.
- **macOS stays ad-hoc signed.** The 2.0 bundle is not notarized: the Privacy & Security confirmation on first launch and after each update still applies, and the install docs keep the caveat. The notarization machinery stays in the tree for a later release.
- **Flatpak** stays submission-ready; the submission itself remains a maintainer decision.

## Breaking changes

| Change | What it means | How to keep working |
|---|---|---|
| Public API is `/api/v2` only | Scripts on `/api/v1` or unversioned paths stop working | Move to `/api/v2`, or set `OPENBOX_ENABLE_V1_API=1` through 2.0.x |
| Library format 7 | A 1.x build cannot read the library after the upgrade | Automatic on first start; `openbox --rollback-library` restores the v6 copy |
| Backup format 2 | A 1.x build cannot read a new backup | Use the 1.19-compatible export for archives a 1.x install must read |
| Host channel | New hosts use `/api/host/*` | Update the host; the old paths stay as internal aliases through 2.0.x |
| Retired `/api/v1` routes | Off by default | `OPENBOX_ENABLE_V1_API=1` |

## Upgrading

Upgrade from 1.19.1. The first start upgrades the library to format 7, keeps `library.v6.backup.json`, and shows the readiness report before the library opens. `openbox --rollback-library` restores the 1.19 file.

The macOS installer pins the release tag:

```bash
VERSION=2.0.0
curl --proto '=https' --tlsv1.2 --fail --location \
  --output install-macos.sh \
  "https://github.com/vindeckyy/OpenBoxGL/releases/download/v${VERSION}/install-macos.sh"
less install-macos.sh
OPENBOX_RELEASE_TAG="v${VERSION}" bash install-macos.sh
```

On Windows, extract the release archive and run `OpenBox\scripts\install.ps1`; on Linux, use the AppImage, the Flatpak, or `scripts/install.sh` as before. Each binary artifact (both AppImages, the Windows archives and native hosts, and the macOS bundle) ships with its SHA-256 sidecar and Ed25519 signature. The installer scripts are not signed; they pin the release key and verify the artifacts against it.

## Known limits

- The macOS bundle is ad-hoc signed, not notarized, so the first launch and each new build need the Privacy & Security confirmation.
- JSON remains the canonical store. SQLite is a read model and can be deleted and rebuilt at any time.
- The formal library scale is 20,000 games. 50,000 was measured and reported as exploratory.
- Python 3.10 is still supported.

The [changelog](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/CHANGELOG.md) lists the main areas, not every change.
