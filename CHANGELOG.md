# Changelog

All notable changes to OpenBox are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Gameyfin storefront integration: browse owned library, import, install/uninstall on demand in desktop and Big Box
- Library and Big Box filters for installed-only vs all owned games
- Ludusavi and Hoard save-tool hooks from the game detail pane (when installed on PATH)

## [0.3.0] - 2026-07-24

### Added

- LaunchBox Premium-equivalent features with no paywall: custom fields, ESRB metadata and filters, list view, platform categories, and bulk edit wizard
- Drag-and-drop import zone with multi-emulator install chooser and ROM version ranking
- Steam trailer and GOG media auto-download, RetroAchievements 7z scanning, and richer achievement stats
- Big Box hybrid scoped search, attract mode, startup video, bundled media packs, and controller prompt packs
- Localization for English, Spanish, German, French, and Portuguese
- Xbox 360, loose arcade, and Vita3K title resolution import helpers
- `parity_premium.py` module and premium API routes

### Changed

- Expanded PARITY.md to mark premium workflows as done and free
- AppImage, Makefile, Flatpak manifest, and packaging tests include `parity_premium.py`

## [0.2.0] - 2026-07-24

### Added

- Storefront Manager with catalog browse, uninstalled import, and startup auto-import
- Game Discovery Center with curated local discovery lists
- Big Box Stage, Hybrid, and CoverFlow layouts with gamepad mapping, pause overlay, and screensaver
- Parity modules for import, media hygiene, saves, integrations, and storefront workflows
- ScummVM, RPCS3, and Vita3K dedicated import endpoints
- RetroAchievements Big Box filters, pause access, and emulator launch injection
- EmuMovies and Bezel Project integration hooks
- OBS recording auto-attach on session close
- MAME community high score export and import
- Platform documents pane and sidebar section management
- Searchable settings, welcome wizard, and library health audit
- Expanded API and parity integration test coverage

### Changed

- Expanded LaunchBox parity matrix with Linux-first equivalents for premium workflows
- AppImage and Makefile packaging updated for new modules

## [0.1.0] - 2026-07-23

### Added

- Initial public release
- Web UI and native Tk UI
- Steam, Heroic, and Lutris import
- LaunchBox Games Database sync and media scraping
- Emulator profiles with Flathub auto-install
- RetroAchievements integration
- Save discovery and versioned backups
- Session tracking and plugin hooks
- AppImage, Flatpak manifest, and Makefile install targets

[Unreleased]: https://github.com/vindeckyy/OpenBox/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/vindeckyy/OpenBox/releases/tag/v0.3.0
[0.2.0]: https://github.com/vindeckyy/OpenBox/releases/tag/v0.2.0
[0.1.0]: https://github.com/vindeckyy/OpenBox/releases/tag/v0.1.0
