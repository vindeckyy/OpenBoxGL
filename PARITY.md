# OpenBox Parity Matrix

> **Legal Disclaimer:** OpenBox is an independent open-source project and is NOT affiliated, associated, authorized, endorsed by, or in any way officially connected with LaunchBox or Unbroken Software, LLC. Reference to LaunchBox features is solely for software compatibility tracking and open-source parity comparison.

Acceptance source: [LaunchBox product overview](https://www.launchbox-app.com/about), [Windows changelog](https://www.launchbox-app.com/about/changelog), and [plugin overview](https://feedback.launchbox-app.com/help/articles/1605395-plugins-overview).

`done` means the workflow is usable end to end. `partial` means real functionality exists but the LaunchBox surface is broader.

| Capability | Status | Acceptance check |
|---|---|---|
| Local library, metadata editing, search, filters, favorites | done | Changes persist and remain searchable |
| Platform and collection navigation | done | Counts and filters update from real games |
| Local executable and ROM import | done | Duplicate paths are skipped |
| Steam installed-game import | done | Manifests are discovered across Steam libraries |
| Steam metadata and artwork | done | Selected game downloads real store data and media |
| Steam launching | done | Imported App ID launches through Steam or its URI |
| Epic, GOG, and Amazon imports through Heroic | done | Installed manifests import and launch through Heroic |
| EA, Ubisoft, and Xbox imports | partial | Installed EA and Ubisoft games import through Lutris; Xbox PC packages are unavailable on Linux |
| MAME and FinalBurn full-set imports | done | DAT/XML metadata classifies and imports merged, split, and non-merged sets |
| LaunchBox Games Database matching | done | Official daily database sync, local matching, and selected metadata/media downloads work |
| Media manager and image groups | done | Grid image groups, per-game paths, media audits, and bulk matched-game downloads work |
| Emulator profiles and per-game commands | done | Commands launch without a shell |
| Emulator installation and automatic configuration | done | Supported emulators install from Flathub and add launch profiles per platform |
| Archive extraction before launch | done | ZIP uses safe built-in extraction; 7z/RAR use installed 7z |
| Startup and shutdown screens | done | Screens follow the launched process from start through its actual exit |
| Play counts, time, and session history | done | Sessions persist with duration and exit status |
| Additional apps, versions, and documents | done | Extras launch from each game's detail pane |
| Manuals and reader | done | Manuals render inline through the browser reader and can open in the Linux default application |
| Save management | done | Automatic location discovery, versioned backups, file/directory support, and guarded restore work |
| RetroAchievements | done | Account, common-ROM auto-matching, manual IDs, progress, hardcore status, and badges work |
| Video, music, and screenshot playback | done | Per-game video and music play, and screenshots open in a full-size gallery |
| Playlists, auto-filters, and saved filters | done | Platform, view, and search rules save, apply, update, and delete |
| Big Box controller-first navigation | done | Filter-aware fullscreen browsing, paging, favorites, and launch work with keyboard or standard gamepads |
| Themes and per-platform themes | done | CSS themes import, persist, apply live, and map globally or per platform |
| Plugin manager and extension API | done | Local packages install, update, disable, remove recoverably, and run library, launch, and session hooks |
| Backups and restore | done | Restore creates a pre-restore safety copy |
| Library audit and missing-file checks | done | Files, provider-aware duplicates, media, extras, saves, and emulator configuration are audited |
| Linux packaging and updates | done | AppImage build, Flatpak manifest, Makefile install/uninstall, desktop entry, and verified update mechanism |
