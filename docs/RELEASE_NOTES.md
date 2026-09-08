# OpenBox 1.10.0 — Bring Your Library Across Safely

Your collection keeps growing, and your devices should keep up. OpenBox 1.10.0 makes the everyday path calmer: bring in a LaunchBox collection, review changes before they land, keep catalog data in sync without silent loss, and get into your next game without duplicate launches.

---

## What's New

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

**Full Changelog**: https://github.com/vindeckyy/OpenBoxGL/compare/v1.9.0...v1.10.0
