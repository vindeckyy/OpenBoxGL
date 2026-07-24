<p align="center">
  <img src="openbox.svg" width="160" alt="OpenBox Logo">
</p>

<h1 align="center">OpenBox</h1>

<p align="center">
  <strong>The Ultimate Local-First Game Library & Launcher for Linux</strong>
</p>

<p align="center">
  <em>An open-source, modern LaunchBox alternative built for privacy, performance, and complete library control.</em>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL%20v3-blue.svg?style=for-the-badge&logo=gnu" alt="License: AGPL v3"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.10+-3776AB.svg?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.10+"></a>
  <a href="https://github.com/vindeckyy/OpenBox"><img src="https://img.shields.io/badge/Platform-Linux-FCC624.svg?style=for-the-badge&logo=linux&logoColor=black" alt="Platform: Linux"></a>
  <a href="PARITY.md"><img src="https://img.shields.io/badge/Parity-LaunchBox-0052CC.svg?style=for-the-badge&logo=gamepad" alt="LaunchBox Parity"></a>
  <a href="DISCLAIMER.md"><img src="https://img.shields.io/badge/Disclaimer-Independent-green.svg?style=for-the-badge&logo=shield" alt="Legal Disclaimer"></a>
</p>

<p align="center">
  <a href="#-overview">Overview</a> •
  <a href="#-key-features">Key Features</a> •
  <a href="#-quick-start">Quick Start</a> •
  <a href="#-screenshots">Screenshots</a> •
  <a href="#-architecture-overview">Architecture</a> •
  <a href="#-emulator-profiles--command-tokens">Emulator Profiles</a> •
  <a href="#-plugin-system-api">Plugins</a> •
  <a href="#-launchbox-parity-matrix">Parity Matrix</a> •
  <a href="#-legal-disclaimer">Disclaimer</a> •
  <a href="#-contributing">Contributing</a>
</p>

---

## 💡 Overview

**OpenBox** brings together your entire gaming universe on Linux into a unified, privacy-focused, and highly customizable interface. Whether you want to launch modern AAA PC titles from Steam, Heroic (Epic, GOG, Amazon), or Lutris, manage classic ROM collections with RetroArch and standalone emulators, or track RetroAchievements — **OpenBox handles it all locally without cloud lock-in.**

Featuring both a feature-packed **Modern Browser Web UI** (with a full REST API for automation and integrations) and a lightning-fast **Native Tk Interface**, OpenBox adapts to desktop PCs, handheld devices (Steam Deck, ROG Ally), and couch gaming setups via its controller-first **Big Box Mode**.

---

## ⚡ Key Features

### 🎮 Unified Library & Smart Discovery
- **Multi-Source Aggregation**: Seamlessly merges native Linux binaries, retro ROMs, Steam titles, Heroic Games Launcher (Epic, GOG, Amazon), Lutris (EA, Ubisoft, Xbox/Game Pass), ScummVM, RPCS3, and Vita3K libraries into one catalog.
- **Storefront Manager**: Browse owned and installed titles across Steam, Heroic, and Lutris from a unified storefront dialog — import uninstalled games and optionally auto-import on startup.
- **Automated Game Import**: Scans installed manifests, Steam libraries, and recursive ROM folders with intelligent file-extension detection, multi-platform folder import, multi-disc M3U generation, and emulator recommendations on import.
- **Game Discovery Center**: Curated local lists — recently added, never played, continue playing, highly rated, random picks, and short sessions — launch straight from the Discovery menu.
- **MAME & FinalBurn DAT Awareness**: Parses official XML/DAT catalogs to classify merged, split, and non-merged arcade ROM sets while automatically identifying BIOS files.
- **Smart Filtering & Collections**: Create custom collections, save dynamic search presets, filter by platform/genre/developer/series/missing media, hide sidebar sections, and use an arrange-by jump bar on large sorted views.
- **Platform Documents**: Attach manuals, guides, and reference files per platform with a dedicated platform detail pane.
- **"Surprise Me" Picker**: Instant randomized game selection from your active filter view.

### 🖼️ LaunchBox Database & Media Engine
- **Daily LaunchBox Sync**: Synchronizes with the official LaunchBox Games Database for accurate local metadata matching.
- **Rich Media Scraper**: Auto-downloads high-resolution cover art, background wallpapers, logos, and gameplay screenshots.
- **Media Audit & Image Groups**: Browse image groups, manage per-game artwork, execute bulk media downloads, clean duplicate artwork, apply region priority, and enforce download limits across matched libraries.
- **Manuals & Document Reader**: Open PDFs and documents in-app with page navigation, spread layout, and light/dark reader themes.
- **Video, Music & Screenshots**: Multi-category video playback, library background music in Big Box, video/BGM mix controls, screenshot capture, and fullscreen gallery lightbox.

### 🕹️ Emulator & Session Orchestration
- **Auto-Discovery & One-Click Installs**: Automatically detects system emulators on your `$PATH` or installs them via Flathub (Dolphin, PCSX2, RPCS3, PPSSPP, Cemu, MAME, xemu, ScummVM, Vita3K, etc.) with **Update All**, dependency checks, and direct **Open Emulator** actions.
- **Safe Command Tokenization**: Robust argument parser with `{path}`, `{rom_name}`, `{app_id}`, and custom command overrides — launches without a shell.
- **Archive Extraction**: Safe built-in ZIP extraction and 7z/RAR support via installed `7z` before launch when needed.
- **Session Tracking**: Tracks launch timestamps, playtime duration, and total play counts across up to 500 session histories with optional session-history disable and a History viewer.
- **Process Monitoring**: Startup and shutdown overlay screens track launched processes through actual exit, with shutdown progress and force-close support.
- **Game Progress Automation**: Optionally auto-update Playing/Paused/Completed progress from play time and idle days.
- **Additional Apps & Versions**: Launch alternate executables, version-specific ROMs, and bundled extras from each game's detail pane.

### 🏆 RetroAchievements & Save Management
- **RetroAchievements Integration**: Full user authentication, ROM hash calculation, automatic platform matching (NES, SNES, N64, Genesis, GBA, etc.), badge rendering, Big Box achievement filters, pause-menu access, and emulator launch injection.
- **Hardcore Mode Support**: Live achievement progress tracking with hardcore status validation.
- **Automated Save Location Discovery**: Locates save directories for Steam Cloud, RetroArch, PCSX2, PPSSPP, RPCS3, Dolphin, and Cemu.
- **Versioned Backups & Guarded Restore**: Create timestamped save backups with retention limits, pre-restore safety fallbacks, and optional **backup-on-close** when a session ends.

### 🎨 Themes & Controller-First Big Box Mode
- **Big Box Fullscreen GUI**: Controller-first navigation for gamepads and handhelds (Steam Deck, ROG Ally) with **Stage**, **Hybrid**, and **CoverFlow** layouts, jewel-case styling, filter/sort/RetroAchievements menus, configurable gamepad button mapping, dynamic paging, pause overlay for running games, and screensaver launch.
- **Live CSS Themes**: Hot-reloadable custom CSS themes mapped globally or per-platform, with local import and open-folder workflow.
- **First-Run Welcome Wizard**: Staged setup on first launch with media limits and persistent import queues.
- **Searchable Settings**: Filter every settings field by keyword; open Settings instantly with `Ctrl+,`.

### 🔌 Extensibility, Integrations & Sync
- **Sandboxed Plugin System**: Extends functionality via isolated Python plugins hooked into `library`, `before_launch`, and `after_session` events, plus a bundled curated community plugin catalog.
- **EmuMovies & Bezel Project**: Download bezels and fetch licensed EmuMovies media when credentials are configured.
- **MAME High Scores**: Discover local high scores and export/import community score bundles.
- **OBS Recording Attach**: Automatically attach the latest OBS recording when a session closes; manual attach remains available.
- **Cloud & Directory Sync**: Synchronize game library metadata, saves, and assets across devices using any mounted directory (Syncthing, Dropbox, Google Drive) — the Linux equivalent to LaunchBox Premium cloud stats sync.
- **Safe JSON Backup/Restore**: Export and import full library configurations with automatic pre-restore safety copies.
- **Library Health Audit**: Check missing files, provider-aware duplicates, media gaps, extras, saves, and emulator configuration from the Health menu.

---

## 🖼️ Screenshots

<p align="center">
  <img src="assets/openbox-screenshot.png" alt="OpenBox Library View" width="92%">
  <br>
  <em>OpenBox Web Interface — unified library grid with multi-source game aggregation and platform filtering.</em>
</p>

<p align="center">
  <img src="assets/openbox-game-detail.png" alt="OpenBox Game Detail" width="92%">
  <br>
  <em>Game detail panel with metadata, launch controls, related games, and save management.</em>
</p>

---

## 🚀 Quick Start

### Option 1: AppImage (Recommended)

The standalone AppImage bundles Python and all core dependencies.

```bash
# Make executable and launch
chmod +x OpenBox-x86_64.AppImage
./OpenBox-x86_64.AppImage
```

> [!TIP]
> To launch the lightweight Tk native interface instead of the browser Web UI, append `--native`:
> ```bash
> ./OpenBox-x86_64.AppImage --native
> ```

### Option 2: System Install (Makefile)

Install system-wide with standard desktop menu integration:

```bash
sudo make install

# Run Web UI launcher
openbox

# Run native Tk UI
openbox-native
```

### Option 3: Flatpak Build

Build and run using Flatpak for isolated sandbox execution:

```bash
flatpak-builder --user --install --force-clean build-dir io.openbox.GameLauncher.yml
flatpak run io.openbox.GameLauncher
```

### Option 4: Run from Source

```bash
# Clone the repository
git clone https://github.com/vindeckyy/OpenBox.git
cd OpenBox

# Launch Web Application (Default)
python3 web_app.py

# Launch Native Tk Interface
python3 openbox.py
```

---

## 🏗️ Architecture Overview

OpenBox follows a modular, local-first architecture designed for maximum stability, fast startup times, and minimal memory overhead.

```
OpenBox Root
 ├── web_app.py               # Web GUI server & REST API backend
 ├── openbox.py               # Native Tk GUI client
 ├── importers.py             # Steam, Heroic, Lutris manifest discovery
 ├── parity_import.py         # M3U, multi-platform import, emulator recommendations
 ├── parity_storefront.py     # Storefront Manager catalog & uninstalled import
 ├── parity_discovery.py      # Game Discovery Center curated lists
 ├── parity_media.py          # Media queues, duplicates, region priority, limits
 ├── parity_saves.py          # Save retention & backup-on-close helpers
 ├── parity_integrations.py   # RA inject, bezels, EmuMovies, OBS, MAME scores
 ├── arcade.py                # MAME/FinalBurn DAT parser & set classifier
 ├── metadata.py              # LaunchBox Games DB sync & media scraper
 ├── emulators.py             # Flathub auto-installer & profile manager
 ├── retroachievements.py     # Hash calculation, achievement tracking & badges
 ├── saves.py                 # Save location discovery & versioned backup engine
 ├── plugins.py               # Plugin lifecycle manager & hook dispatcher
 ├── plugin_runner.py         # Subprocess plugin runner with safety timeouts
 ├── plugin_catalog.py        # Curated bundled community plugin catalog
 ├── catalog.py               # Deep search, filtering, and bulk library edits
 ├── archives.py              # Safe archive extraction engine (ZIP, 7z, RAR)
 ├── cloud_sync.py            # Local/mounted folder stat sync (Syncthing/Drive)
 ├── env_config.py            # Optional ~/.env configuration loading
 └── updates.py               # GitHub release checker with zsync AppImage updating
```

> [!NOTE]
> **Data Location**: OpenBox stores library state, metadata, and custom configurations locally in:
> `~/.local/share/openbox-game-launcher/library.json`

---

## ⚙️ Emulator Profiles & Command Tokens

Configure per-platform launcher commands in your emulator preferences:

```ini
[SNES]
command = retroarch -L /usr/lib/libretro/snes9x_libretro.so "{path}"
```

### Supported Command Tokens

| Token | Description | Example Replacement |
| :--- | :--- | :--- |
| `{path}` | Absolute path to the target game file / ROM | `/roms/snes/ChronoTrigger.sfc` |
| `{name}` | Title of the game | `Chrono Trigger` |
| `{rom_name}` | Filename of the target ROM | `ChronoTrigger.sfc` |
| `{app_id}` | Steam Application ID | `1188930` |
| `{heroic_app_id}`| Heroic Launcher Application ID | `gog_1207664643` |
| `{lutris_id}` | Lutris Game Identifier | `cyberpunk-2077` |

---

## 🔌 Plugin System API

OpenBox features a secure, sandboxed plugin system. Create a directory inside the plugins folder containing a `plugin.json` manifest and an entry file:

`plugin.json`:
```json
{
  "id": "discord-rich-presence",
  "name": "Discord Rich Presence",
  "version": "1.0.0",
  "entry": "main.py",
  "hooks": ["library", "before_launch", "after_session"]
}
```

`main.py`:
```python
import sys
import json

def handle_hook(payload):
    # Process library event or game session data
    return payload

if __name__ == "__main__":
    data = json.load(sys.stdin)
    result = handle_hook(data)
    json.dump(result, sys.stdout)
```

---

## 🎯 LaunchBox Parity Matrix

OpenBox targets full feature parity with LaunchBox for Linux environments. See [PARITY.md](PARITY.md) for the complete capability matrix, acceptance checks, and intentionally skipped Windows-only items.

| Capability | OpenBox Status | Highlights |
| :--- | :---: | :--- |
| **Local Library Management** | ✅ `Done` | Bulk editing, search, dynamic filters, collections, arrange-by jump bar |
| **Steam / Heroic / Lutris** | ✅ `Done` | Manifest parsing, EA/Ubisoft/Xbox tagging, automated media & launch integration |
| **Storefront Manager** | ✅ `Done` | Catalog browse, uninstalled import, startup auto-import |
| **ScummVM / RPCS3 / Vita3K** | ✅ `Done` | Dedicated library import endpoints |
| **MAME & Arcade DAT Imports** | ✅ `Done` | Merged, split, and non-merged set classification; community high scores |
| **LaunchBox DB Sync** | ✅ `Done` | Official daily sync, local matching, artwork downloader |
| **Media Manager** | ✅ `Done` | Image groups, audits, duplicates, region priority, download limits |
| **Manuals & Reader** | ✅ `Done` | In-app PDF/document reader with toolbar and themes |
| **Emulator Flathub Installer** | ✅ `Done` | Auto-detect, Update All, dependency checks, recommend-on-import |
| **Save Backup & Discovery** | ✅ `Done` | Auto-detects 6+ platforms, retention limits, backup-on-close |
| **RetroAchievements** | ✅ `Done` | Matching, badges, hardcore, Big Box filters, pause access, RA inject |
| **Big Box Mode** | ✅ `Done` | Stage/Hybrid/CoverFlow, gamepad mapping, pause overlay, screensaver |
| **Game Discovery Center** | ✅ `Done` | Curated local discovery lists from the Discovery menu |
| **Theme Customization** | ✅ `Done` | Custom CSS injection with live preview and per-platform themes |
| **Integrations** | ✅ `Done` | EmuMovies, Bezel Project, OBS recording attach |
| **Plugin API** | ✅ `Done` | Sandboxed Python hooks plus bundled community catalog |
| **First-Run Setup** | ✅ `Done` | Welcome wizard with media limits and import queues |
| **Linux Packaging & Updates** | ✅ `Done` | AppImage, Flatpak, Makefile install, zsync updates |

---

## 🧪 Testing & Verification

OpenBox maintains test coverage across library, import, media, sessions, parity modules, API routes, and packaging.

```bash
# Run the full suite (recommended)
./run_all_tests.sh

# Or run individual modules
python3 test_arcade.py
python3 test_archives.py
python3 test_auto_import.py
python3 test_catalog.py
python3 test_changelog_features.py
python3 test_cloud_sync.py
python3 test_demo_purge.py
python3 test_emulators.py
python3 test_env_config.py
python3 test_importers.py
python3 test_metadata.py
python3 test_packaging.py
python3 test_parity_api.py
python3 test_parity_features.py
python3 test_parity_integrations.py
python3 test_parity_storefront.py
python3 test_plugins.py
python3 test_retroachievements.py
python3 test_saves.py
python3 test_secrets.py
python3 test_sessions.py
python3 test_updates.py
```

To build the release AppImage binary:
```bash
./build_appimage.sh
```

---

## 🤝 Contributing

Contributions from the community are warmly welcomed!

1. **Fork the Repository** on GitHub.
2. **Create a Feature Branch** (`git checkout -b feature/amazing-feature`).
3. **Commit your Changes** (`git commit -m 'Add amazing feature'`).
4. **Ensure All Tests Pass** (`python3 test_*.py`).
5. **Push to the Branch** (`git push origin feature/amazing-feature`).
6. **Open a Pull Request**.

---

## ⚖️ Legal & DMCA Disclaimer

**OpenBox is an independent open-source project developed from scratch and is NOT affiliated, associated, authorized, endorsed by, or in any way officially connected with LaunchBox, Unbroken Software, LLC, or any of their subsidiaries or affiliates.**

- **Clean-Room Open Source**: OpenBox is a ground-up open-source implementation developed independently without proprietary code or trade secrets.
- **Zero Proprietary Assets**: OpenBox contains **no copyrighted ROMs, no emulator firmware/BIOS images, no encryption keys, and no DRM-bypassing tools**.
- **Nominative Fair Use**: Product names, logos, and trademarks (LaunchBox, Steam, Heroic, Lutris, RetroArch, Nintendo, Sony, Microsoft, etc.) belong to their respective owners and are referenced strictly under 15 U.S.C. § 1125(c)(3)(A) for software compatibility and feature-parity comparison.
- **DMCA Compliance**: For complete trademark attributions, clean-room declarations, and DMCA takedown notice policy, see [DISCLAIMER.md](DISCLAIMER.md).

---

## 📄 License

OpenBox is free software released under the [GNU Affero General Public License v3.0](LICENSE).

<p align="center">
  <sub>Designed with care for Linux gamers who value privacy, control, and open source.</sub>
</p>
