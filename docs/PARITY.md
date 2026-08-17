# OpenBox Parity Matrix

OpenBox tracks LaunchBox feature parity for Linux environments. For contribution and release workflow, see [CONTRIBUTING.md](CONTRIBUTING.md) and [CHANGELOG.md](CHANGELOG.md). The latest release is **v1.4.0**, the packaging fix release described in the CHANGELOG.

> **Legal disclaimer:** OpenBox is an independent open-source project and is NOT affiliated, associated, authorized, endorsed by, or in any way officially connected with LaunchBox or Unbroken Software, LLC. Reference to LaunchBox features is solely for software compatibility tracking and open-source parity comparison.

Acceptance source: [LaunchBox product overview](https://www.launchbox-app.com/about), [Windows changelog](https://www.launchbox-app.com/about/changelog), and [plugin overview](https://feedback.launchbox-app.com/help/articles/1605395-plugins-overview).

`done` means the workflow is usable end to end on Linux. `partial` means a deliberate Linux equivalent exists but LaunchBox’s Windows/premium surface is broader or unavailable. `missing` means no usable equivalent yet.

## Capability matrix

| Capability | Status | Acceptance check |
|---|---|---|
| Local library, metadata editing, search, filters, favorites | done | Changes persist and remain searchable |
| Platform and collection navigation | done | Counts and filters update from real games |
| Local executable and ROM import | done | Multi-disc M3U generation, multi-platform folder import, and emulator recommendations on import work |
| Steam installed-game import | done | Manifests are discovered across Steam libraries |
| Steam metadata and artwork | done | Selected game downloads real store data and media |
| Steam launching | done | Imported App ID launches through Steam or its URI |
| Epic, GOG, and Amazon imports through Heroic | done | Installed manifests import and launch through Heroic |
| EA, Ubisoft, and Xbox imports | done | Lutris/Heroic catalog import with EA, Ubisoft, and Xbox/Game Pass tagging; Xbox native PC packages remain unavailable on Linux |
| MAME and FinalBurn full-set imports | done | DAT/XML metadata classifies and imports merged, split, and non-merged sets |
| LaunchBox Games Database matching | done | Official daily database sync, local matching, and selected metadata/media downloads work |
| Full LaunchBox media catalog | done | Box backs, spines, 3D boxes, clear logos, fanart, banners, title screens, carts, discs, and advertisement flyers download from the database alongside covers, backgrounds, and screenshots; manuals are user-supplied paths because the LaunchBox metadata feed ships no manual images |
| Media manager and image groups | done | Grid image groups, extended artwork groups, audits, bulk downloads, duplicate cleanup, region priority, and download limits work |
| Emulator profiles and per-game commands | done | Commands launch without a shell |
| Emulator installation and automatic configuration | done | Install, Update All, open emulator, recommend-on-import, and dependency checks work |
| Archive extraction before launch | done | ZIP uses safe built-in extraction; 7z/RAR use installed 7z |
| Startup and shutdown screens | done | Screens follow the launched process from start through its actual exit |
| Play counts, time, and session history | done | Sessions persist with duration and exit status |
| Additional apps, versions, and documents | done | Extras launch from each game's detail pane |
| Manuals and reader | done | Reader toolbar with page navigation, spread layout, and light/dark themes for PDFs and documents |
| Save management | done | Discover, scan, retention limits, versioned backups, backup-on-close, and restore work |
| RetroAchievements | done | Account, matching, progress, badges, Big Box filters, pause access, and emulator injection work |
| Video, music, and screenshot playback | done | Multi-category videos, library BGM, video/BGM mix, capture, and gallery work |
| Playlists, auto-filters, and saved filters | done | Platform, view, search rules, ordered manual members, parent playlists, and notes save, apply, update, and delete |
| Big Box controller-first navigation | done | Stage, hybrid, and CoverFlow layouts; filter/sort/RA filters; pause overlay; screensaver launch |
| Steam Game Mode / gamescope guest | done | `--game-mode` opens Big Box; guest detection; Steam launches keep Input; non-Steam windows get best-effort STEAM_GAME props |
| Themes and per-platform themes | done | Five stock CSS themes ship with the Web UI; import, persist, apply live, and open-folder access work |
| Plugin manager and extension API | done | Local packages install and run hooks; curated community catalog is bundled |
| Backups and restore | done | The web UI lists archives, shows manifests, restores selected archives, and creates a pre-restore safety copy |
| Library audit and missing-file checks | done | Files, provider-aware duplicates, media, extras, saves, and emulator configuration are audited |
| Linux packaging and updates | done | AppImage build, Flatpak manifest, Makefile install/uninstall, desktop entry, and verified update mechanism |
| Welcome wizard and first-run setup | done | Staged setup wizard with media limits and persistent import queues |
| Searchable settings pages | done | Settings dialog filters fields by name and related terms |
| Session history toggle and viewer | done | Play sessions can be disabled and are browsable from the History menu |
| Arrange-by scrollbar | done | Large sorted views expose a jump bar with group markers |
| Delete media on game removal | done | Removing a game can optionally delete its associated media files |
| Save backup on session close | done | Automatic save backups can run when a game session ends |
| Game progress automation | done | Playing and paused progress values can be updated from play time and idle days |
| Open emulator and install all | done | Installed emulators can be launched directly and bulk-installed from Flathub |
| Shutdown progress and force close | done | Exiting with running games shows shutdown status and supports force close |
| Ctrl+, settings shortcut | done | Opens Settings from the keyboard |
| Platform documents | done | Platform detail pane and document storage API work per platform |
| Sidebar filter section management | done | Hidden sidebar sections apply live from Settings |
| CoverFlow / 3D box models | done | CoverFlow layout with CSS jewel-case depth styling |
| Game Discovery Center | done | Curated local discovery lists launch from the Discovery menu |
| EmuMovies / Bezel Project | done | Bezel downloads and EmuMovies credential/download hooks work with licensed accounts |
| Storefront Manager / uninstalled auto-import | done | Unified storefront dialog, catalog browse, uninstalled import, and startup auto-import work |
| Gameyfin self-hosted library | done | Catalog import, background install with status polling, install/uninstall on demand, desktop + Big Box owned/installed filters |
| Ludusavi / Hoard save tools | done | Optional CLI hooks from game detail when binaries are on PATH |
| ScummVM / RPCS3 / Vita3K library import | done | Dedicated import endpoints scan common emulator libraries |
| MAME community high scores | done | Local high-score discovery plus export/import bundles for sharing |
| OBS recording attach | done | Latest OBS recording auto-attaches on session close; manual attach remains available |
| Premium cloud sync | done | Mounted-folder statistics sync is the Linux equivalent to LaunchBox Premium cloud stats |
| Custom fields and bulk metadata wizard | done | Define custom fields in Settings, edit per game, and bulk update selected games |
| ESRB ratings filter and metadata | done | ESRB from LaunchBox database imports, sidebar filter, list view column, and bulk edit |
| List view and library columns | done | Grid/list toggle with sortable list columns including ESRB and progress |
| Drag-and-drop import wizard | done | Drop zone prompts for folder path with multi-emulator install chooser |
| Platform categories | done | Sidebar category navigation groups platforms by family |
| ROM version ranking on import | done | Duplicate ROM groups rank USA/world releases and skip betas on import |
| Steam trailer and GOG media download | done | Detail pane downloads Steam trailers and Heroic GOG artwork automatically |
| RetroAchievements 7z scanning and rich profile | done | ZIP and 7z ROM hashing plus beaten/mastered stats in achievement pane |
| Big Box hybrid scoped search | done | Hybrid mode exposes platform-scoped search while browsing |
| Attract mode and startup video | done | Separate attract delay, optional Big Box startup video, and screensaver launch |
| Bundled media packs (free) | done | Platform logos, controller prompts, and badge packs apply without a subscription |
| Localization | partial | Interface is English-only; the five partial translations were removed until real localization lands |
| Big Box shutdown apps on mode switch | done | Configurable commands run when entering Big Box (not when leaving) |
| Xbox 360 and loose arcade import | done | default.xex folder scan and Hypseus/Singe loose file import |
| Vita3K title resolution | done | Title IDs resolve to readable game names on import |
| Advanced search syntax | done | Field terms such as `platform:PC`, quoted values, status terms, and negative terms filter the live grid |
| Context actions and multi-selection | done | Right-click actions plus Ctrl/Shift range selection expose launch, favorite, progress, playlist, edit, and remove actions |
| Configurable status badges | done | Settings controls favorite, install, media, save, progress, storefront, achievement, rating, and hardware badges |
| Collection and related details | done | Platform, category, and playlist detail panes show stats, quick actions, rich related-game reasons, and artwork galleries |
| Per-game launch profile overrides | done | A game can select an installed emulator profile without changing the platform default |
| Persistent play queue | done | Queue controls add, remove, advance, reorder, and resolve missing games |
| Game tags and tag filtering | done | Tags normalize, count, filter, and bulk-edit through the Web UI |
| Notification Center | done | Persistent deduplicated notifications expose unread state and read/clear controls |
| Signed webhook automation | done | Event subscriptions, HTTPS validation, bounded retries, test delivery, and secret redaction work locally |

All LaunchBox Premium-equivalent workflows above are included in OpenBox without a subscription. OpenBox sets `premium_features_free: true` in settings and ships bundled media packs without a license gate.

## Intentionally not replicated on Linux

These LaunchBox features have no practical Linux equivalent and are documented rather than emulated:

| LaunchBox feature | OpenBox decision |
|---|---|
| Windows shell replacement | Not applicable on Linux desktop environments |
| LEDBlinky / cabinet LED control | Use external Linux arcade I/O tools instead |
| Teknoparrot arcade launcher | Use Lutris/Wine launch profiles for supported titles |
| Native Xbox PC package scanning | Use Heroic/Lutris/Xbox Cloud entries instead |
| Bundled proprietary media packs | Replaced by free bundled media packs in OpenBox |
| LaunchBox Premium account cloud library | Replaced by mounted-folder sync plus local backups |
| LaunchBox online theme storefront | Replaced by local CSS theme import and open-folder workflow |
