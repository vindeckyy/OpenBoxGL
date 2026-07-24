<p align="center">
  <img src="openbox.svg" width="128" alt="OpenBox logo">
</p>

<h1 align="center">OpenBox</h1>

<p align="center">
  Local-first game library and launcher for Linux
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL--3.0-blue.svg" alt="License: AGPL-3.0"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white" alt="Python 3.10+"></a>
  <a href="https://github.com/vindeckyy/OpenBox/releases/tag/v0.2.0"><img src="https://img.shields.io/badge/Release-v0.2.0-0052CC" alt="Release v0.2.0"></a>
  <a href="PARITY.md"><img src="https://img.shields.io/badge/LaunchBox-Parity%20Matrix-555" alt="LaunchBox parity matrix"></a>
</p>

<p align="center">
  <a href="#overview">Overview</a> |
  <a href="#why-openbox-on-linux">Why OpenBox on Linux</a> |
  <a href="#comparison-with-launchbox">Comparison</a> |
  <a href="#features">Features</a> |
  <a href="#installation">Installation</a> |
  <a href="#documentation">Documentation</a> |
  <a href="#development">Development</a> |
  <a href="#legal">Legal</a>
</p>

---

## Overview

OpenBox is an open-source game library manager and launcher built for Linux. It brings PC games, storefront libraries, ROM collections, arcade sets, and emulator workflows into one searchable catalog with artwork, metadata, session tracking, save management, and launch orchestration.

If you already know LaunchBox, OpenBox targets the same core job on Linux: organize a large library, enrich it with metadata and media, and launch games reliably from one front end. The difference is that OpenBox is designed around how Linux gamers actually install and run software today, through Steam, Heroic, Lutris, Flatpak emulators, RetroArch, and local ROM folders, without requiring Windows or a paid premium tier to unlock basic library workflows.

OpenBox provides two interfaces:

| Interface | Entry point | Best for |
| --- | --- | --- |
| Web UI | `python3 web_app.py` or `openbox` | Full feature set, REST API, Big Box mode |
| Native UI | `python3 openbox.py` or `openbox-native` | Lightweight desktop use |

Library data is stored locally at `~/.local/share/openbox-game-launcher/library.json`.

> **Independence notice:** OpenBox is an independent open-source project. It is not affiliated with LaunchBox or Unbroken Software, LLC. LaunchBox and Big Box are trademarks of Unbroken Software, LLC. See [DISCLAIMER.md](DISCLAIMER.md).

---

## Why OpenBox on Linux

LaunchBox is a mature Windows launcher with a large feature set and a strong community. On Linux, most people still want that experience: one polished library, good artwork, emulator profiles, Big Box browsing, and session tracking. The problem is that running a Windows-first launcher on Linux usually means extra friction, weaker integration with native Linux tools, and premium paywalls for workflows that Linux users often solve with local files and open tooling anyway.

OpenBox exists to give Linux users a front end that fits the platform.

### What that means in practice

| Topic | OpenBox on Linux | Typical LaunchBox experience on Linux |
| --- | --- | --- |
| Platform support | Native Linux application | Windows-first; Linux use often depends on compatibility layers |
| License and cost | Open source under AGPL-3.0 | Free tier plus paid Premium for several advanced workflows |
| Account requirement | No OpenBox account required | LaunchBox account and Premium features for parts of the ecosystem |
| Steam integration | Reads installed Steam libraries and launches through Steam | Supported, but not centered on Linux-native install layouts |
| Heroic / Epic / GOG / Amazon | First-class import through Heroic manifests | Possible, but not the primary Linux workflow |
| Lutris / EA / Ubisoft / Game Pass tagging | Built around Lutris and Heroic catalog import | Less direct on Linux |
| Emulator setup | Detects local binaries and installs from Flathub with Update All | Strong on Windows; Linux emulator install paths vary more |
| Updates | GitHub releases, AppImage, zsync, Flatpak, Makefile install | Windows installer/updater focused |
| Cloud sync | Mounted-folder sync with Syncthing, Dropbox, Drive, or any path | LaunchBox Premium cloud library |
| Automation | Local REST API with token auth | Limited external automation surface |
| Handheld / couch use | Big Box mode with controller navigation and AppImage portability | Big Box exists, but Linux handheld workflows are secondary |
| Source availability | Full source code in this repository | Proprietary application |

### Who OpenBox is for

OpenBox is a strong fit if you:

- Run Linux on a desktop, laptop, Steam Deck, or handheld PC
- Want one library for Steam, Heroic, Lutris, ROMs, and standalone emulators
- Prefer local JSON library state over vendor cloud lock-in
- Need Flathub-aware emulator install/update flows
- Want RetroAchievements, save backups, session history, and Big Box in one app
- Care about open source licensing and self-hosted backups

LaunchBox remains the better choice if you:

- Use Windows as your primary launcher platform
- Need Windows-only integrations such as shell replacement, LEDBlinky, or Teknoparrot-native workflows
- Already rely on LaunchBox Premium cloud library hosting and want that exact service model

OpenBox does not try to clone every Windows-only LaunchBox feature. It implements the Linux-usable parts of the LaunchBox workflow and documents the rest in [PARITY.md](PARITY.md).

---

## Comparison with LaunchBox

OpenBox was built as a clean-room open-source project for Linux parity, not as a fork or derivative of LaunchBox. The comparison below focuses on practical outcomes for Linux users.

### Library management

Both projects handle large local libraries, metadata editing, favorites, collections, filters, playlists, and bulk operations. OpenBox adds Linux-native quality-of-life pieces such as searchable settings, sidebar section hiding, arrange-by jump bars, provider-aware duplicate detection, and a library health audit for missing files, media, saves, and emulator configuration.

### Imports and storefronts

OpenBox imports from:

- Installed Steam libraries across standard Steam root layouts
- Heroic for Epic, GOG, and Amazon titles
- Lutris for EA, Ubisoft, Xbox, and Game Pass tagged entries
- Local ROM folders with extension-aware scanning
- Multi-platform folder import and multi-disc M3U generation
- MAME and FinalBurn DAT/XML full-set classification
- ScummVM, RPCS3, and Vita3K library scans
- Storefront Manager for owned vs. installed catalog browsing and optional startup auto-import

LaunchBox covers many of the same sources on Windows. On Linux, OpenBox's advantage is that these importers are written against the paths, manifests, and launchers Linux users already have installed.

### Metadata and artwork

OpenBox syncs with the official LaunchBox Games Database, matches games locally, downloads artwork and metadata, supports image groups, duplicate cleanup, region priority, download limits, and bulk media jobs. Licensed EmuMovies and Bezel Project hooks are available when you provide your own credentials.

### Launching, sessions, and saves

OpenBox launches through safe tokenized emulator commands without shell interpolation, extracts archives before launch when needed, tracks sessions with play counts and play time, shows startup/shutdown overlays, supports force-close on exit, and can back up saves on session close with retention limits and guarded restore.

RetroAchievements support includes account login, ROM hash matching, badge display, hardcore status, Big Box filters, pause-menu access, and emulator launch injection.

### Big Box and themes

OpenBox includes fullscreen Big Box mode with Stage, Hybrid, and CoverFlow layouts, jewel-case styling, filter/sort/RetroAchievements menus, configurable gamepad button mapping, pause overlay for running games, screensaver support, and library background music.

Themes are plain CSS files with live reload, global or per-platform assignment, and a local import workflow instead of a proprietary online theme store.

### Extensibility

OpenBox provides sandboxed Python plugins with `library`, `before_launch`, and `after_session` hooks, plus a bundled community plugin catalog. LaunchBox has its own plugin ecosystem on Windows; OpenBox's model is smaller but fully local and inspectable.

### Parity status

The full capability matrix with acceptance checks lives in [PARITY.md](PARITY.md). At a high level, the major Linux-usable LaunchBox workflows are implemented. Windows-only arcade, shell, LED, and native Xbox package features are intentionally replaced with documented Linux equivalents or external tools.

---

## Features

### Unified library and discovery

- One catalog for Steam, Heroic, Lutris, ROM folders, ScummVM, RPCS3, Vita3K, and local executables
- Storefront Manager for catalog browse, uninstalled import, and startup auto-import
- Game Discovery Center with recently added, never played, continue playing, highly rated, random picks, and short-session lists
- Collections, saved filters, favorites, bulk edits, and Surprise Me random selection
- Platform documents pane for manuals and reference files per platform
- MAME and FinalBurn merged/split/non-merged set classification with BIOS awareness

### Metadata, media, and playback

- LaunchBox Games Database daily sync, local matching, and artwork download
- Media manager with image groups, audits, duplicate cleanup, region priority, and download limits
- In-app PDF/document reader with page navigation, spread layout, and light/dark themes
- Multi-category video playback, screenshot capture, gallery lightbox, and library BGM in Big Box

### Emulators and launch orchestration

- Auto-detection of emulators on `$PATH`
- Flathub install, Update All, Open Emulator, dependency checks, and recommend-on-import
- Per-platform command profiles with `{path}`, `{name}`, `{rom_name}`, `{app_id}`, `{heroic_app_id}`, and `{lutris_id}` tokens
- Archive extraction for ZIP plus 7z/RAR through installed `7z`
- Additional apps, alternate versions, and bundled extras per game
- Welcome wizard on first run with media limits and persistent import queues

### Sessions, progress, and saves

- Session history with timestamps, duration, exit status, and optional history disable
- Startup and shutdown overlays with force-close support
- Game progress automation from play time and idle days
- Save discovery for Steam Cloud, RetroArch, PCSX2, PPSSPP, RPCS3, Dolphin, and Cemu
- Versioned backups, retention limits, pre-restore safety copies, and backup-on-close

### Big Box, controller use, and couch play

- Fullscreen browsing optimized for gamepads and handhelds
- Stage, Hybrid, and CoverFlow layouts
- Filter, sort, and RetroAchievements menus
- Configurable gamepad button mapping
- Pause overlay for running games
- Screensaver with controller wake and launch

### Integrations and sync

- RetroAchievements matching, badges, hardcore tracking, and launch injection
- EmuMovies and Bezel Project downloads with user-provided credentials
- OBS recording auto-attach on session close
- MAME community high score export and import
- JSON library backup and restore with automatic pre-restore safety copy
- Mounted-folder sync for Syncthing, Dropbox, Google Drive, or any local path
- Plugin hooks and bundled plugin catalog
- Local REST API for automation and third-party tooling

---

## Installation

### AppImage (recommended)

Download the latest release from [GitHub Releases](https://github.com/vindeckyy/OpenBox/releases).

```bash
chmod +x OpenBox-x86_64.AppImage
./OpenBox-x86_64.AppImage
```

Use `--native` to launch the Tk interface instead of the web UI.

### System install

```bash
sudo make install
openbox          # Web UI
openbox-native   # Native UI
```

### Flatpak

```bash
flatpak-builder --user --install --force-clean build-dir io.openbox.GameLauncher.yml
flatpak run io.openbox.GameLauncher
```

### From source

```bash
git clone https://github.com/vindeckyy/OpenBox.git
cd OpenBox
python3 web_app.py
```

Requirements: Python 3.10 or newer on a Linux system with standard desktop tooling.

Optional local configuration can be loaded from `~/.env` or a project `.env` file. See `.env.example`. Never commit secrets.

---

## Screenshots

<p align="center">
  <img src="assets/openbox-screenshot.png" alt="OpenBox library view" width="92%">
  <br>
  <sub>Library grid with multi-source aggregation and platform filtering</sub>
</p>

<p align="center">
  <img src="assets/openbox-game-detail.png" alt="OpenBox game detail view" width="92%">
  <br>
  <sub>Game detail panel with metadata, launch controls, and save management</sub>
</p>

---

## Documentation

| Document | Description |
| --- | --- |
| [PARITY.md](PARITY.md) | LaunchBox capability matrix and Linux parity decisions |
| [CHANGELOG.md](CHANGELOG.md) | Release history |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development workflow and contribution guidelines |
| [SECURITY.md](SECURITY.md) | Security reporting process |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | Community standards |
| [DISCLAIMER.md](DISCLAIMER.md) | Trademark, fair use, and DMCA policy |

### Emulator command tokens

Configure per-platform launch commands in emulator preferences:

```ini
[SNES]
command = retroarch -L /usr/lib/libretro/snes9x_libretro.so "{path}"
```

| Token | Description |
| --- | --- |
| `{path}` | Absolute path to the game file or ROM |
| `{name}` | Game title |
| `{rom_name}` | ROM filename |
| `{app_id}` | Steam application ID |
| `{heroic_app_id}` | Heroic application ID |
| `{lutris_id}` | Lutris game identifier |

### Plugin API

Plugins live under the user plugins directory with a `plugin.json` manifest and Python entry module. Supported hooks: `library`, `before_launch`, and `after_session`. See [CONTRIBUTING.md](CONTRIBUTING.md#plugins) for details.

### REST API

The web UI exposes a local REST API authenticated with a session token. This supports automation, integrations, and third-party tools without modifying the core application. API routes are covered by the parity and integration test suite in `test_parity_api.py`.

---

## Development

### Project layout

```
OpenBox/
├── web_app.py              Web UI server and REST API
├── openbox.py              Native Tk UI
├── importers.py            Steam, Heroic, and Lutris import
├── parity_import.py        M3U, multi-platform import, emulator recommendations
├── parity_storefront.py    Storefront Manager catalog and uninstalled import
├── parity_discovery.py     Game Discovery Center lists
├── parity_media.py         Media queues, duplicates, region priority, limits
├── parity_saves.py         Save retention and backup-on-close helpers
├── parity_integrations.py  RA inject, bezels, EmuMovies, OBS, MAME scores
├── metadata.py             LaunchBox database sync and media scraping
├── emulators.py            Emulator profiles and Flathub management
├── retroachievements.py    RetroAchievements integration
├── saves.py                Save discovery and backup engine
├── plugins.py              Plugin lifecycle and hooks
├── plugin_catalog.py       Bundled community plugin catalog
├── catalog.py              Search, filters, and bulk edits
└── test_*.py               Test suite
```

### Run tests

```bash
./run_all_tests.sh
```

Build the AppImage:

```bash
./build_appimage.sh
```

Pull requests should pass the full test suite. See [CONTRIBUTING.md](CONTRIBUTING.md).

---

## Legal

OpenBox is released under the [GNU Affero General Public License v3.0](LICENSE).

Trademark references to LaunchBox, Steam, Heroic, Lutris, RetroArch, and other third-party products are used for compatibility description only. OpenBox does not distribute ROMs, BIOS files, firmware, or DRM circumvention tools.

For the full legal policy, see [DISCLAIMER.md](DISCLAIMER.md).

---

## Support

- [Report a bug](https://github.com/vindeckyy/OpenBox/issues/new?template=bug_report.yml)
- [Request a feature](https://github.com/vindeckyy/OpenBox/issues/new?template=feature_request.yml)
- [Review open issues](https://github.com/vindeckyy/OpenBox/issues)

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) before opening a pull request.
