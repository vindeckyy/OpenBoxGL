# OpenBox release notes

OpenBox 1.19.0 is the macOS release. OpenBox now runs as a real Mac app on Apple Silicon and Intel: one signed universal bundle with its own Python, a native WKWebView window, the menu-bar tray, native dialogs, `openbox://` deeplinks, and the same verified updater as every other platform. The library, emulator, save, and Big Box features are the ones Linux and Windows already have. Linux and Windows behavior is unchanged. The complete list is in [CHANGELOG.md](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/CHANGELOG.md), and the code changes are in [v1.18.0...v1.19.0](https://github.com/vindeckyy/OpenBoxGL/compare/v1.18.0...v1.19.0).

## OpenBox is a Mac app now

- **One download for both chips.** `OpenBox-macos.zip` carries arm64 and x86_64 builds of the window host and one merged universal Python 3.12, so the same file runs natively on an M-series Mac and on an Intel one. Nothing has to be installed first.
- **A real window, not a browser tab.** The window is WKWebView with remembered size and position, a menu-bar tray with recent games and minimize-to-tray, native file, message, and confirmation dialogs, fullscreen, the clipboard bridge, and the same `openbox://` links the other hosts use. If the host cannot start, the launcher falls back to the browser app window and then to a plain tab, exactly as on Linux and Windows.
- **Your data lives in the Mac place.** The library and settings are in `~/Library/Application Support/openbox-game-launcher`, and `OPENBOX_DATA_DIR` still overrides that. Stored paths keep POSIX separators, so a library folder can move between platforms.
- **Emulators tell the truth about macOS.** Every bundled emulator definition now records whether upstream ships a macOS build. RetroArch resolves as an app bundle, a bare program such as `mame` is found on `PATH`, and a definition with no verified macOS build is reported as not installed instead of failing at launch. Flathub install controls are hidden on macOS, where there is no Flatpak channel.
- **Saves are found where macOS keeps them.** Save discovery covers Steam Cloud plus the documented macOS folders for PPSSPP, RPCS3, Dolphin, and RetroArch, so backups and selective restore see the same files the emulators use.
- **Updates work the same way.** The in-app updater verifies the SHA-256 sidecar and the Ed25519 signature, replaces the bundle after the server exits, and keeps the previous one as `OpenBox.app.previous`. A failed update leaves the working bundle in place.
- **The Mac build is a first-class build.** CI runs the full suite and the portable gates on Apple Silicon and Intel runners, and the release job builds, smoke-tests, signs, and attaches the bundle beside the Linux and Windows artifacts.

## Install on macOS

Download the installer from this release, read it, and run it. It checks the pinned release key, the bundle's SHA-256 sidecar, and its Ed25519 signature before installing `OpenBox.app` into `~/Applications` and linking `openbox` into `~/.local/bin`:

```bash
VERSION=1.19.0
curl --proto '=https' --tlsv1.2 --fail --location \
  --output install-macos.sh \
  "https://github.com/vindeckyy/OpenBoxGL/releases/download/v${VERSION}/install-macos.sh"
less install-macos.sh
OPENBOX_RELEASE_TAG="v${VERSION}" bash install-macos.sh
```

Re-run with `--replace` to update an existing install, or set `OPENBOX_INSTALL_DIR` to install elsewhere.

**The first launch needs one action.** The bundle is ad-hoc signed, not notarized, so macOS asks you to allow it in **System Settings > Privacy & Security > Open Anyway**. Every new build resets the ad-hoc signature, so macOS may ask again for access to the folders, screen recording, and network volumes you granted before.

A source checkout works too: install Python 3.10 or newer (the system `python3` is older), then run `python3 web_app.py`. `scripts/build_native_host_macos.sh` builds the window host, and `scripts/build_macos_app.sh` builds the whole universal bundle.

## What is different on macOS

- **Linux-only features are hidden instead of broken.** Heroic, Lutris, Epic, Faugus, Proton scanning, Flatpak emulator installation, and the plugin sandbox have no macOS implementation, so their controls are not offered. Steam Game Mode exits with "Game Mode needs gamescope and is only available on Linux."
- **Small things use the Mac tools.** The Big Box battery indicator reads `pmset`, screenshots use `screencapture`, and process tracking uses libproc instead of `/proc`.
- **The supported floor is macOS 11 or newer** on both arm64 and x86_64.

## Also in 1.19.0

- **Backups no longer carry provider tokens or the OBS WebSocket password.** The per-provider integration tokens (IGDB, ScreenScraper, SteamGridDB, Gameyfin, RetroAchievements) and `obs_websocket_password` are now redacted like every other credential, and a restore still preserves the local copy.
- **The Windows installer explains itself when the release verifier is missing.** The standalone `install.ps1` now points at the supported flow (extract the release archive and run `OpenBox\scripts\install.ps1`) instead of "run this script from a checkout".
- **A native `openbox://resume/<id>` deeplink is no longer ignored.** The page selects the game and scrolls it into view, and `?deeplink=showgame&id=` scrolls as the contract describes.
- **The version gate is strict again.** It reads the release version the bug-report template advertises, so a stale "current release is ..." line fails `make version-check`.

## Upgrading

Upgrade from any 1.18.x release; there is no data migration. The macOS library starts in its own data folder, and a library copied from another platform keeps working because stored references use POSIX separators. No existing v1 API route was removed or changed.

## Known limits

- The macOS bundle is not notarized, so the first launch and each new build need the Privacy & Security confirmation described above.
- The macOS emulator set is the set the upstream projects actually ship; definitions without a verified macOS build are shown as not installed.
- The Linux-only features listed above remain unavailable on macOS.

The [changelog](https://github.com/vindeckyy/OpenBoxGL/blob/master/docs/CHANGELOG.md) lists the main areas, not every change.
