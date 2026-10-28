# OpenBox release notes

OpenBox 1.19.1 is a platform-parity bug sweep. 1.19.0 made macOS a first-class channel, which exposed a class of defects where a Linux-only feature was offered on Windows or macOS and failed at click time. This release closes that class on all three platforms: one server-computed capability contract now decides which platform-specific controls exist, and the controls Windows and macOS keep use their own tools instead of Linux ones. Linux behavior is the reference and is unchanged. The complete list is in [CHANGELOG.md](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/CHANGELOG.md), and the code changes are in [v1.19.0...v1.19.1](https://github.com/vindeckyy/OpenBoxGL/compare/v1.19.0...v1.19.1).

## What 1.19.1 fixes

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
