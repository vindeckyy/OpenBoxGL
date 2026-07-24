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
  <a href="#features">Features</a> |
  <a href="#installation">Installation</a> |
  <a href="#documentation">Documentation</a> |
  <a href="#development">Development</a> |
  <a href="#legal">Legal</a>
</p>

---

## Overview

OpenBox is an open-source game library manager and launcher for Linux. It unifies PC games, storefront imports, ROM collections, and emulator workflows in one local catalog with metadata, artwork, session tracking, and launch orchestration.

The project provides two interfaces:

| Interface | Entry point | Best for |
| --- | --- | --- |
| Web UI | `python3 web_app.py` or `openbox` | Full feature set, REST API, Big Box mode |
| Native UI | `python3 openbox.py` or `openbox-native` | Lightweight desktop use |

Library data is stored locally at `~/.local/share/openbox-game-launcher/library.json`.

OpenBox is an independent project. It is not affiliated with LaunchBox or Unbroken Software, LLC. See [DISCLAIMER.md](DISCLAIMER.md).

---

## Features

### Library and discovery

- Unified catalog for Steam, Heroic, Lutris, ROM folders, ScummVM, RPCS3, Vita3K, and local executables
- Storefront Manager for owned vs. installed titles with optional startup auto-import
- Game Discovery Center with curated local lists (continue playing, never played, random picks, and more)
- Collections, saved filters, platform documents, and bulk library operations
- MAME and FinalBurn DAT-aware arcade set classification

### Metadata and media

- LaunchBox Games Database sync, matching, and artwork download
- Media manager with image groups, duplicate cleanup, region priority, and download limits
- In-app document reader, video playback, screenshot gallery, and library background music in Big Box

### Launching and sessions

- Emulator auto-detection, Flathub install/update, dependency checks, and per-platform command profiles
- Safe command tokenization and archive extraction before launch
- Session history, playtime tracking, startup/shutdown overlays, and optional progress automation
- Save discovery, retention policies, versioned backups, and backup-on-close

### Big Box and integrations

- Fullscreen controller-first browsing with Stage, Hybrid, and CoverFlow layouts
- RetroAchievements matching, badges, filters, pause access, and emulator launch injection
- Plugin hooks, bundled plugin catalog, EmuMovies/Bezel Project support, OBS attach, and MAME high scores
- CSS themes, searchable settings, JSON backup/restore, and mounted-folder sync

See [PARITY.md](PARITY.md) for the complete capability matrix and acceptance criteria.

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

---

## Development

### Project layout

```
OpenBox/
├── web_app.py              Web UI server and REST API
├── openbox.py              Native Tk UI
├── importers.py            Steam, Heroic, and Lutris import
├── parity_*.py             Parity modules (import, media, saves, integrations, storefront, discovery)
├── metadata.py             LaunchBox database sync and media scraping
├── emulators.py            Emulator profiles and Flathub management
├── retroachievements.py    RetroAchievements integration
├── saves.py                Save discovery and backup engine
├── plugins.py              Plugin lifecycle and hooks
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
