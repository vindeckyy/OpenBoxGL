# OpenBox Parity Matrix

> **Legal Disclaimer:** OpenBox is an independent open-source project and is NOT affiliated, associated, authorized, endorsed by, or in any way officially connected with LaunchBox or Unbroken Software, LLC. Reference to LaunchBox features is solely for software compatibility tracking and open-source parity comparison.

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
| Media manager and image groups | done | Grid image groups, audits, bulk downloads, duplicate cleanup, region priority, and download limits work |
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
| Playlists, auto-filters, and saved filters | done | Platform, view, and search rules save, apply, update, and delete |
| Big Box controller-first navigation | done | Stage, hybrid, and CoverFlow layouts; filter/sort/RA filters; pause overlay; screensaver launch |
| Themes and per-platform themes | done | CSS themes import, persist, apply live, and open-folder access works |
| Plugin manager and extension API | done | Local packages install and run hooks; curated community catalog is bundled |
| Backups and restore | done | Restore creates a pre-restore safety copy |
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
| ScummVM / RPCS3 / Vita3K library import | done | Dedicated import endpoints scan common emulator libraries |
| MAME community high scores | done | Local high-score discovery plus export/import bundles for sharing |
| OBS recording attach | done | Latest OBS recording auto-attaches on session close; manual attach remains available |
| Premium cloud sync | done | Mounted-folder statistics sync is the Linux equivalent to LaunchBox Premium cloud stats |

## Intentionally not replicated on Linux

These LaunchBox features have no practical Linux equivalent and are documented rather than emulated:

| LaunchBox feature | OpenBox decision |
|---|---|
| Windows shell replacement | Not applicable on Linux desktop environments |
| LEDBlinky / cabinet LED control | Use external Linux arcade I/O tools instead |
| Teknoparrot arcade launcher | Use Lutris/Wine launch profiles for supported titles |
| Native Xbox PC package scanning | Use Heroic/Lutris/Xbox Cloud entries instead |
| Bundled proprietary media packs | Use LaunchBox Games Database, EmuMovies, and local media imports |
| LaunchBox Premium account cloud library | Replaced by mounted-folder sync plus local backups |
| LaunchBox online theme storefront | Replaced by local CSS theme import and open-folder workflow |
