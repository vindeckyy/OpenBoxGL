# Support

## Scale and architecture

- **Formal library scale:** 20,000 games (blocking performance gates cover 10k and 20k scenarios). The optional SQLite read model (`OPENBOX_ENABLE_SQLITE_READ=1`) provides indexed search and facets for larger libraries while JSON remains canonical.
- **CPU architecture:** OpenBox 1.11.0 publishes signed **x86_64 and aarch64 AppImages**. The Flatpak bundle is **x86_64 only**. The in-app updater and the release installer select the artifact that matches the running architecture and refuse a mismatched or unsigned artifact.
- **Interface language:** English, Spanish, German, French, and Portuguese (v1.7.2+).

## Supported platforms

OpenBox targets Linux on **x86_64 and aarch64**. Release CI validates the packaged paths and native host build; hardware and distro coverage below describes the strength of the evidence rather than a promise that every combination is physically tested:

| Environment | Status |
|---|---|
| Ubuntu LTS (release runners) | Release-gated build and test coverage |
| Fedora | Host and writable emulated-aarch64 build validation; runtime otherwise best effort |
| Arch Linux | Best effort |
| SteamOS / Steam Deck | Gamescope guest harness and controller-path coverage; physical maintainer pass unavailable |
| aarch64 desktops / handhelds | Release-gated AppImage/native build and emulated smoke; physical hardware unverified |
| Other glibc distributions | Best effort |

The aarch64 runner and emulated tests prove the build and packaged paths, not battery, display, controller, or gamescope behavior on a particular handheld. Report hardware-specific results with the model, distro image, and desktop/session details.

## Supported runtimes

- Python 3.10 or newer (CI runs 3.10 and 3.12)
- Chromium-family browsers get the chrome-less app window; Firefox opens a separate window; no compatible browser falls back to the default browser

## Reporting problems

Open **Settings -> Library Audit** and use the diagnostic log, or paste the error banner's "Copy details" output (it includes a request id maintainers can correlate with the log).

Bug reports should include: OpenBox version, distro and desktop, Python version when running from source, the diagnostic report from `/api/diagnostic` (Settings), and steps to reproduce.

## Known behavior

See `docs/reliability.md` for the full edge case catalog. Highlights:

- Games you launch keep running after OpenBox exits unless you close them from the Running panel.
- OpenBox data lives in `~/.local/share/openbox-game-launcher/`; deleting `library.json` resets the library while media files stay.
- The web UI is local-only. Sharing the token in the URL with another machine is equivalent to handing over control of the instance.
