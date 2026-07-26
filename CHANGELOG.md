# Changelog

All notable changes to OpenBox are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- GitHub update checks, plugin catalog downloads, AppImage zsync metadata, and repository links now use the canonical `vindeckyy/OpenBoxGL` location after the repository rename

## [0.4.6] - 2026-07-26

### Fixed

- Authenticated POST API requests now reject non-object JSON and request-shape type errors with JSON `400` responses instead of dropping the connection

### Changed

- Public identity, support, trademark, and notice documentation consistently distinguishes OpenBox Game Launcher from the Openbox window manager
- The repository default branch, CI triggers, issue links, and contributor documentation now use `master`

### Tests

- Added real-HTTP API boundary regressions for authorization, validation, exception mapping, partial settings updates, secret sanitization, and lifecycle errors

## [0.4.5] - 2026-07-24

### Added

- Filter presets, explorer facets, Big Box quick-switch presets, and import exclusions
- `openbox://` deep links, keyboard launcher support, granular library backups, and restore rotation
- Process tracking modes, optional IGDB metadata search, and YAML emulator definition packs

### Fixed

- Session restart handling, launcher menu formatting, ROM paths containing spaces, malformed tracking values, and unsafe backup archive paths
- Responsive and keyboard-accessible library controls, lazy media loading, and reduced-motion behavior

## [0.4.4] - 2026-07-24

### Fixed

- Settings persistence on partial saves, storefront-only POSTs, JSON error responses on catalog routes, Gameyfin install recovery, premium route auth, and session polling guards

## [0.4.3] - 2026-07-24

### Fixed

- Settings save merges omitted keys from existing state. Partial storefront POSTs no longer wipe watch folders or storefront auto-import flags.
- Storefront save posts only storefront fields instead of spreading empty Settings form values
- GET `/api/storefront/catalog` and `/api/gameyfin/providers` return JSON errors instead of dropping the connection
- Gameyfin install worker reports error state on invalid `library_id`; UI times out after ~60s instead of polling forever
- Premium GET routes require authorization; `/api/update` catches malformed release JSON
- Session poll no longer overlaps `refresh()`; `openReader` guards missing documents

## [0.4.2] - 2026-07-24

### Fixed

- Gameyfin installs run in a background worker; the UI polls `/api/gameyfin/install/status` instead of blocking the server
- Storefront Gameyfin settings use dedicated `storefront*` form fields; saving Settings no longer touches Gameyfin credentials

## [0.4.1] - 2026-07-24

### Fixed

- Update check no longer fails when a release omits a separate `.sha256` file; GitHub asset digests are used instead
- Settings update check returns readable errors instead of a browser network failure
- Top bar and library header scale cleanly at any browser zoom level

## [0.4.0] - 2026-07-24

### Added

- Gameyfin storefront integration: browse owned library, import, install/uninstall on demand in desktop and Big Box
- Library and Big Box filters for installed-only vs all owned games
- Ludusavi and Hoard save-tool hooks from the game detail pane (when installed on PATH)

## [0.3.0] - 2026-07-24

### Added

- LaunchBox Premium-equivalent features without a subscription: custom fields, ESRB metadata and filters, list view, platform categories, and bulk edit wizard
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

- Expanded LaunchBox parity matrix with Linux equivalents for premium workflows
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

[Unreleased]: https://github.com/vindeckyy/OpenBoxGL/compare/v0.4.6...HEAD
[0.4.6]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v0.4.6
[0.4.5]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v0.4.5
[0.4.4]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v0.4.4
[0.4.3]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v0.4.3
[0.4.2]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v0.4.2
[0.4.1]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v0.4.1
[0.4.0]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v0.4.0
[0.3.0]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v0.3.0
[0.2.0]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v0.2.0
[0.1.0]: https://github.com/vindeckyy/OpenBoxGL/releases/tag/v0.1.0
