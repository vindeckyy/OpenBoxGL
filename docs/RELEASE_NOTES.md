# OpenBox 1.11 — Every Second Counts

Your collection keeps growing, and every session leaves a little more context
behind. OpenBox 1.11 keeps that context close: resume where you stopped,
capture the moment, find the right next game, and turn your library into
something you can browse together.

---

## What's New

### Never Lose Your Place

Quick Resume records progress-aware session state where the adapter can expose
it, while session recaps and the Moments timeline keep captures, play context,
and recent activity attached to the game. Record That can ask OBS for a replay
buffer, fall back to a safe local screenshot when necessary, and build bounded
highlight reels without sending media to a server.

### A Library You Can Ask

Backlog Radio recommends games from local habits and explains each pick. The
search bar understands a small deterministic grammar such as `short unplayed
rpg` and shows the interpretation as removable chips. Ctrl/Cmd-K opens the
command palette for games, actions, settings, and What's New discovery.

### Time Machine

The journal-backed Time Machine provides a bounded event timeline, as-of views,
and reviewable revert previews. It is designed to make experimentation safe:
the canonical state remains transactional, and malformed or stale requests
stop before mutation.

### Arcade Room and Household

Arcade Room is a controller-friendly canvas showroom organized by platform.
Museum mode turns it into a self-guided exhibit with real library facts, while
reduced-motion users get a static presentation. An optional salted PIN is a
local convenience boundary for kiosk browsing; it is not advertised as a
security boundary.

Household adds opt-in members, challenges, shares, results, and leaderboards
over the existing sync folder. There are no accounts, hosted services, or
surprise telemetry, and statistics remain off unless a device opts in.

### Deck Migration and Polish

Steam Bridge previews, applies, and removes OpenBox entries in Steam's
`shortcuts.vdf` while preserving unrelated records. `openbox --play <id>` makes
the same launch path available from Steam Game Mode. ES-DE `gamelist.xml`
imports use the same review-first discipline as LaunchBox migration: bounded
parsing, explicit identity, stale-source detection, and transactional apply.

What's New tips, localized UI strings, per-game Moments and Clips tabs, quick
query chips, and the 1.10.1 corrective hardening sweep round out the update.

---

## Previous release: OpenBox 1.10.0 — Bring Your Library Across Safely

Your collection keeps growing, and your devices should keep up. OpenBox 1.10.0 made the everyday path calmer: bring in a LaunchBox collection, review changes before they land, keep catalog data in sync without silent loss, and get into your next game without duplicate launches.

---

### What's New in 1.10.0

### Sync You Can Trust

Opt-in catalog sync now keeps a clear, reviewable history of changes between devices. Each change is validated and tied to a device, with tombstones, recovery snapshots, and delivery acknowledgement built in.

When two devices change the same game, OpenBox shows the alternatives with stable review IDs. Keep the title from one device, the rating from another, and decide on a deletion separately. Nothing unresolved is quietly accepted, and launch paths, commands, credentials, and media stay local to each device.

The older full-library routes now stop before mutation with a clear unavailable response. Statistics sync remains available, while the safer catalog transport is enabled explicitly.

### LaunchBox Import Without Surprises

Bring over a LaunchBox XML export through a bounded, review-first migration flow. OpenBox shows the games it found, path and emulator mappings, exclusions, and a deterministic preview before changing your library.

Stale previews and modified payloads are rejected before they can change your library. Accepted plans apply transactionally, and LaunchBox provider IDs stay separate from numeric metadata IDs so unrelated games cannot merge by accident.

### Search, Shelf & Large Libraries

Search and facets now use one consistent library model with sensible limits, hidden-item handling, and stable ordering. For larger collections, opt into the SQLite read model with `OPENBOX_ENABLE_SQLITE_READ=1` for indexed search and facets while keeping JSON as the source of truth.

Manual and shelf entries let you track cartridges, discs, board games, and console-only titles without inventing a local file path. Create, edit, filter, and export them, then convert one to a playable entry when you have a local file.

### Launch Once, Then Play

Atomic launch reservations prevent repeated clicks, key presses, and controller events from starting the same game twice. Reservations remain active through wrapper and child-process tracking, so the guard holds until the launch really finishes.

---

## Also New

### Faster Writes for Big Libraries

Large-library saves reuse validated state after a successful commit, reducing write latency while keeping backup and recovery guarantees.

### A Picker That Keeps Surprising You

The weighted picker recomputes its suggestion for every request, so **Again** can actually show something new. Older history entries with mixed timestamp formats are handled safely too.

### Ready for Desktops and Handhelds

AppImage and Flatpak packaging now carry the complete locale and metadata set, with relocatable Python, scoped loader paths, SBOMs, and update metadata. Release validation covers x86_64, ARM64, native WebKitGTK, and installed-tree behavior.

---

## Under the Hood

- **Review-first changes** — sync and LaunchBox migration validate plans before mutating the library
- **Stable identity** — provider IDs, local game IDs, tombstones, and launch reservations stay distinct and traceable
- **One library model** — search, facets, shelf records, export, and health checks share the same canonical state behavior
- **Release-ready packaging** — signed multi-architecture AppImages plus an x86_64 Flatpak bundle, with checksums, zsync metadata, SBOMs, and install tooling

---

## Download

| Asset | Architecture | Type |
|-------|-------------|------|
| `OpenBox-x86_64.AppImage` | x86_64 | AppImage |
| `OpenBox-aarch64.AppImage` | ARM64 | AppImage |
| `OpenBox-x86_64.flatpak` | x86_64 | Flatpak |

Choose the AppImage that matches your CPU, or install the Flatpak for a sandboxed desktop setup. AppImages are signed and include SHA-256 checksums, zsync metadata for delta updates, and SBOMs. Verify with `openbox-release.pub` and `install.sh`.

Already running OpenBox? Use the built-in updater or download the matching artifact from the release page.

---

**Full Changelog**: https://github.com/vindeckyy/OpenBoxGL/compare/v1.10.0...v1.11.0
