# Support

## Scale and architecture

- **Formal library scale:** 20,000 games (blocking performance gates cover 10k and 20k scenarios). The optional SQLite read model (`OPENBOX_ENABLE_SQLITE_READ=1`) provides indexed search and facets for larger libraries while JSON remains canonical.
- **CPU architecture:** OpenBox 2.0.0 publishes signed **x86_64 and aarch64 AppImages**, signed **x86_64 and arm64 Windows portable zips**, and a signed **universal macOS app bundle** (arm64 and x86_64 in one archive). The Flatpak bundle is **x86_64 only**. The in-app updater and the release installers select the artifact that matches the running architecture and refuse a mismatched or unsigned artifact.

  The Windows native host is built per architecture against the WebView2 SDK pinned in `scripts/build_native_host_windows.ps1`: after downloading the SDK NuGet package the script verifies its SHA-256 against a committed hash (`$WEBVIEW2_SDK_SHA256`, overridable with the `WEBVIEW2_SDK_SHA256` environment variable when trying a new SDK version) and refuses to build when the hash does not match. To rotate the SDK, bump the script's default version, download the new package, and replace the pinned hash with the new package's SHA-256.

  The macOS bundle carries a pinned CPython build (python-build-standalone). `scripts/build_macos_app.sh` verifies the SHA-256 of every download, merges the arm64 and x86_64 interpreters into one universal tree, signs the bundle ad-hoc, and refuses to package it when the signature does not verify.
- **Interface language:** English, Spanish, German, French, and Portuguese (v1.7.2+).

## Supported platforms

OpenBox targets Linux on **x86_64 and aarch64**, Windows 10/11 on **x86_64 and arm64**, and macOS 11 or newer on **arm64 and x86_64**. Release CI validates the packaged paths and native host builds; hardware and distro coverage below describes the strength of the evidence rather than a promise that every combination is physically tested:

| Environment | Status |
|---|---|
| Ubuntu LTS (release runners) | Release-gated build and test coverage |
| Fedora | Host and writable emulated-aarch64 build validation; runtime otherwise best effort |
| Arch Linux | Best effort |
| SteamOS / Steam Deck | Gamescope guest harness and controller-path coverage; physical maintainer pass unavailable |
| aarch64 desktops / handhelds | Release-gated AppImage/native build and emulated smoke; physical hardware unverified |
| Windows 10/11 (x86_64) | Full test suite on `windows-latest` CI; WebView2 host build exercised there; physical maintainer pass unavailable |
| macOS 11+ (arm64 and x86_64) | Full test suite on `macos-15` and `macos-15-intel` CI; universal bundle built, signed, and smoke-tested there; physical maintainer pass on Apple Silicon only |
| Other glibc distributions | Best effort |

The aarch64 runner and emulated tests prove the build and packaged paths, not battery, display, controller, or gamescope behavior on a particular handheld. Report hardware-specific results with the model, distro image, and desktop/session details.

## Supported runtimes

- Python 3.10 or newer (CI runs 3.10 and 3.12)
- Linux: the native window needs WebKitGTK 4.1; `--web` and `python3 web_app.py` need no host library
- Windows: the native window needs the WebView2 runtime (shipped with Windows 11 and most Windows 10 systems); `openbox --web` and `python web_app.py` work without it
- macOS: the app bundle carries its own Python, so nothing has to be installed. A source checkout needs a Python 3.10 or newer; the system `python3` is older than that. The native window is WKWebView, which ships with macOS.
- Chromium-family browsers get the chrome-less app window; Firefox opens a separate window; no compatible browser falls back to the default browser

## Removing OpenBox

On Windows, `scripts/uninstall.ps1` removes a portable install: the application directory, its `PATH`
entry, the Start Menu shortcut, and the `openbox://` URL handler. It deliberately does **not** touch
your data — the library, artwork, and settings stay in `%LOCALAPPDATA%\openbox-game-launcher\` so a
reinstall finds them intact. Delete that directory yourself to remove your data as well. The script
prints what it removed and what it left behind; pass `-WhatIf` to preview without changing anything.

On macOS, remove `OpenBox.app` from `~/Applications` (or wherever you installed it) and the
`~/.local/bin/openbox` link. Your data stays in `~/Library/Application Support/openbox-game-launcher`;
delete that directory yourself to remove it as well. A release is notarized only when its release notes
say so. Any other macOS bundle is ad-hoc signed, so macOS asks you to allow it the first time you open
it, and each new ad-hoc build asks again.

## Reporting problems

Open **Settings -> Library Audit** and use the diagnostic log, or paste the error banner's "Copy details" output (it includes a request id maintainers can correlate with the log).

Bug reports should include: OpenBox version, distro and desktop, Python version when running from source, the diagnostic report from `/api/diagnostic` (Settings), and steps to reproduce.

## Portable library

If games show as missing after you moved your library folder, open **Settings > Library > Check integrity**. It names missing, unreadable, and outside-folder paths with counts. OpenBox never copies or deletes game files during a move. A move is a preview followed by an apply, and you can move the folder back the same way. Your data folder (saves, media, settings) does not move with the library folder.

## Known behavior

See `docs/reliability.md` for the full edge case catalog. Highlights:

- Graceful shutdown stops tracked sessions, persists cleanup, and drains shutdown work. If a tracked game remains alive after the two-second grace window, OpenBox force-kills its process tree (on Linux and macOS the process group, falling back to the PID; on Windows the tree via job object / `TerminateProcess`); the native hosts also force-kill their server process tree if the server does not exit in time.
- OpenBox data lives in `~/.local/share/openbox-game-launcher/` on Linux, `%LOCALAPPDATA%\openbox-game-launcher\` on Windows, and `~/Library/Application Support/openbox-game-launcher/` on macOS (override with `OPENBOX_DATA_DIR` in the process environment before launch, read at import time per `openbox.py:20-21`); deleting `library.json` resets the library while media files stay.
- The loopback server writes per-launch `server.token` and `server.port` into the data directory (`web_app.py:751-753`) and deletes both on exit; prefer the `X-OpenBox-Token` header over a `token` query parameter.
- Optional `.env` discovery checks an explicit `OPENBOX_ENV_FILE` first, then the data directory and its parent, `~/.env`, and `~/.config/openbox-game-launcher/.env` (`env_config.py:116-140`). The data-directory choice itself must be exported before launch — a discovered `.env` is read too late for it.
- The web UI is local-only. Sharing the token in the URL with another machine is equivalent to handing over control of the instance.
