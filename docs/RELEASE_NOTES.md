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
imports add bounded parsing, explicit identity, stale-source detection, and
transactional apply for another popular handheld workflow.

### Artwork and achievements

The optional SteamGridDB artwork provider searches, previews, applies, and
bulk-matches community covers, backgrounds, clear logos, icons, and banners.
Set `STEAMGRIDDB_API_KEY` in `~/.env`; results use a local cache and the
provider can be disabled from Settings.

OpenBox now includes a local launcher trophy case with deterministic awards
evaluated from library and play-history data. These launcher trophies are
separate from the optional RetroAchievements account integration.

What's New tips, localized UI strings, per-game Moments and Clips tabs, quick
query chips, kiosk settings, and the final polish sweep round out the update.

---
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
