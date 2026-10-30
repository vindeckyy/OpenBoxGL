# OpenBox release notes

OpenBox v2.0.0 turns the 1.x compatibility scaffolding into stable contracts: the library can move, the integrations stop moving, and every supported platform gets a clean install. 1.19.1 was a platform-parity bug sweep; 1.19.0 made macOS a first-class channel, which exposed a class of defects where a Linux-only feature was offered on Windows or macOS and failed at click time. This release closes that class on all three platforms: one server-computed capability contract now decides which platform-specific controls exist, and the controls Windows and macOS keep use their own tools instead of Linux ones. Linux behavior is the reference and is unchanged. The complete list is in [CHANGELOG.md](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/CHANGELOG.md), and the code changes are in [v1.19.0...v1.19.1](https://github.com/vindeckyy/OpenBoxGL/compare/v1.19.0...v1.19.1).

## OpenBox v2.0.0 (unreleased)

OpenBox 2.0 turns the 1.x compatibility scaffolding into stable contracts. The three promises:
your library can move, your integrations stop moving, and every supported platform gets a clean
install.

### Your library can move

- **Portable library.** Game paths under one library folder are stored as `@library/...` tokens.
  Move the folder to another drive or machine, point OpenBox at it, and the games resolve
  without rewriting each game. **Settings > Library > Move library** previews the move and
  refuses a stale preview. Extra apps, versions, and documents under the folder move with it.
- **Library format 7.** Libraries upgrade on first start. The old file is kept as
  `library.v6.backup.json`, and `openbox --rollback-library` restores it byte-for-byte.
- **Integrity check and media manifest.** **Settings > Library > Check integrity** reports
  missing, unreadable, and outside-folder game paths, and media files that were changed,
  deleted, or added behind the app's back. Nothing is changed by a check.
- **Compatibility report.** **Settings > About > Show compatibility report** lists the library
  format, each theme's token fallbacks, which backups restore on which version, unknown settings
  keys, the public API and host channel, and the sync folder.

### Your integrations stop moving

- **The public API is `/api/v2`.** The retired `/api/v1` and unversioned routes are off by
  default; `OPENBOX_ENABLE_V1_API=1` restores them through 2.0.x, and they are removed in 2.1.
  `GET /api/v2/version` reports every contract version, and the OpenAPI document lists every
  public route.
- **The native host has its own channel.** Windows, Linux, and macOS windows talk to
  `/api/host/*` and send `X-OpenBox-API-Version: 2`. A window from another major version is
  refused before the route runs.
- **Backup format 2.** Every backup records a SHA-256 for each file; a restore checks them all
  before writing anything and reports what it wrote. A **1.19-compatible** export is available
  when an older install must read a backup.

### Platform installs

- **macOS stays ad-hoc signed.** The 2.0 bundle is not notarized: the Privacy & Security
  confirmation on first launch and after each update still applies, and the install docs
  keep the caveat. The notarization machinery (entitlements, `notarytool`, stapling) stays
  in the tree for a later release.
- **Windows ARM64.** The release publishes `OpenBox-arm64-windows.zip` and `OpenBox-arm64-windows-native-host.exe` from the `windows-11-arm` lane: the host is built for ARM64, attested, signed, and verified like the x86_64 assets, and the installer and updater select it on ARM64 machines by architecture.
- **Flatpak** stays submission-ready; the submission itself remains a maintainer decision.

### Breaking changes

| Change | What it means | How to keep working |
|---|---|---|
| Public API is `/api/v2` only | Scripts on `/api/v1` or unversioned paths stop working | Move to `/api/v2`, or set `OPENBOX_ENABLE_V1_API=1` through 2.0.x |
| Library format 7 | A 1.x build cannot read the library after the upgrade | Automatic on first start; `openbox --rollback-library` restores the v6 copy |
| Backup format 2 | A 1.x build cannot read a new backup | Use the 1.19-compatible export for archives a 1.x install must read |
| Host channel | New hosts use `/api/host/*` | Update the host; the old paths stay as internal aliases through 2.0.x |
| Retired `/api/v1` routes | Off by default | `OPENBOX_ENABLE_V1_API=1` |

### Not in 2.0

- JSON stays the canonical store. SQLite remains a read model; the storage decision is recorded
  in ADR 0088 with its measurements.
- The formal library scale stays at 20,000 games. 50,000 was measured and reported as
  exploratory.
- Python 3.10 is still supported.
- Plugin API v2 is additive; v1 plugins keep working.

## 1.19.1 (previous release)

### What 1.19.1 fixes

- **A stored gamescope preset no longer breaks Windows and macOS launches.** The preset is applied only on Linux, so it is never prepended to a Windows or macOS command line. The gamescope preset, MangoHud, and handheld-performance controls are hidden on those hosts, and `--game-mode` exits with the Linux-only message instead of starting a broken guest mode.
- **Windows reports its real battery.** The battery indicator reads `GetSystemPowerStatus`: a desktop reports no battery, a laptop reports its percentage and charging state, and the reading is cached for 30 seconds. Windows no longer gets a hard-coded "no battery" answer.
- **Wine, Faugus, and Flatpak management answer only on Linux.** The Wine/Proton manager reports `available: false` and never scans the disk off Linux, the Faugus scanner returns nothing, the Flatpak folder grant refuses the request before running anything, and the Launch Doctor stops offering a Flatpak install where Flatpak does not exist.
- **Storefront browsing uses the platform's own URL opener.** The Steam and Heroic catalogs no longer assume `xdg-open`, say clearly when a host has no opener, and a Git/MSYS `xdg-open` on `PATH` no longer wins over Explorer on Windows.
- **The storefront list matches the host.** `/api/v2/storefronts` lists Heroic on Linux and Windows, Lutris only on Linux, and GOG/EA/Ubisoft only on Windows; a stale client calling a native Windows import on another host receives a clear 400 instead of a failing scan.

## One capability contract

`GET /api/native/capabilities` now carries a `features` object: `gamescope`, `mangohud`, `handheld_perf`, `flatpak`, `wine`, `faugus`, `lutris`, `heroic`, `epic`, `native_storefronts`, and `desktop_entry`. The web UI gates every platform-specific control from that object through one `data-feature` marker, replacing the per-OS markup markers that made every new platform edit the page. A missing object means every feature is false, so a fetch that fails hides controls instead of exposing broken ones. The contract and its failure semantics are documented in [ADR 0086](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/adr/0086-platform-capability-contract.md).

## Upgrading

Upgrade from 1.19.0; there is no data migration, no route was removed, and no setting was deleted. Linux behavior is unchanged: gamescope presets, MangoHud, Wine/Proton management, Flatpak grants, and the Linux import sources work exactly as before.

The installers from 1.19.0 are unchanged:

```bash
VERSION=1.19.1
curl --proto '=https' --tlsv1.2 --fail --location \
  --output install-macos.sh \
  "https://github.com/vindeckyy/OpenBoxGL/releases/download/v${VERSION}/install-macos.sh"
less install-macos.sh
OPENBOX_RELEASE_TAG="v${VERSION}" bash install-macos.sh
```

On Windows, extract the release archive and run `OpenBox\scripts\install.ps1`; on macOS, download `install-macos.sh` and run it with the tag pinned; on Linux, use the AppImage, the Flatpak, or `scripts/install.sh` as before. Each binary artifact (the AppImages, the Windows archive and native host, and the macOS bundle) ships with its SHA-256 sidecar and Ed25519 signature. The installer scripts are not signed; they pin the release key and verify the artifacts against it.

## Known limits

- The macOS bundle is still ad-hoc signed, not notarized, so the first launch and each new build need the Privacy & Security confirmation.
- Linux-only features remain unavailable on Windows and macOS by design; they are now hidden rather than broken.
- ARM64 Windows builds remain out of scope; the Windows artifact is x86_64.

The [changelog](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/CHANGELOG.md) lists the main areas, not every change.
