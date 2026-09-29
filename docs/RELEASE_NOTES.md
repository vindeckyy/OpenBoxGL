# OpenBox 1.15.0 — Finish the surface

The UI grows a proper motion system and the features that shipped without a screen finally get one.
No account, no cloud, no telemetry — everything runs locally.

## What's New

- **Motion that respects you.** Every duration and easing is a token and one switch turns the whole
  interface static for reduced-motion users. Dialogs animate out as well as in, live theme
  switches cross-fade, covers reserve their space and fade in, and typing in search no longer
  replays the grid's entrance animation.
- **A readable Harbor Light.** The light theme had white text on white inputs and cards. Text now
  uses semantic tokens, and a contrast check runs against every theme.
- **Toasts that stay visible.** One toast surface in the top layer: never dimmed by a modal, never
  hidden under Big Box, and an Undo can no longer be overwritten by another message.
- **Accessibility.** The grid is a list with the real total, Big Box announces as a dialog, job rows
  expose progress, the result count is a live region, touch targets are larger, and forced-colors
  mode is supported.
- **Emulator definition updates.** Settings > Emulators can check for, install and roll back the
  signed community pack and never overwrites definitions you edited.
- **Time Machine Compare.** See what was added, removed and changed between two dates, with true
  totals.
- **Settings > About.** Version, platform, data folder, and whether you are in the native window
  or a browser tab.
- **Windows uninstaller.** `scripts/uninstall.ps1` removes the install, PATH entry, Start Menu
  shortcut and `openbox://` registration, and leaves your library alone.

## Not in 1.15

Deck Builder for Game Night (deferred a third time, leads 1.16), Windows storefront importers,
Presence and saves sync, the per-platform setup checklist, Constellation viewpoints, the kiosk PIN
backoff, and the Wayland and Steam Deck manual passes. The definitions pack UI ships in 1.15; the
first signed pack is published separately.

## Fixed

See the changelog for the full list. Highlights: the grid entrance replay, toasts hiding behind
modals, the party wheel and constellation ignoring reduced motion, lightbox arrows jumping on
press, and a light theme opening dark.

---

Full changelog: https://github.com/vindeckyy/OpenBoxGL/compare/v1.14.0...v1.15.0

# OpenBox 1.14.0 — The library grows up

Six flagships that make the library smarter, more personal, and couch-ready.
No account, no cloud, no telemetry — everything runs locally.

## What's New

- **Plugins 2.0.** Per-plugin trust bound to the package checksum — updates
  re-prompt, no global trust toggle. Android-style permission prompts at
  install and enable time, per-plugin settings forms generated from the
  manifest, a `library_source` importer hook that merges plugin games into
  the library with a source badge, a single `events` lifecycle hook, a
  catalog browser tab with per-entry Install/Update, and palette integration
  for plugin commands.
- **Effortless metadata.** One auto-scrape pass after import: offline match
  preview plus opt-in ScreenScraper dual-hash and IGDB passes, then media
  fill from LaunchBox and SteamGridDB. All providers stay off by default
  with per-run budgets, ROM-hash confidence gates anything that
  auto-applies, and a thumbnail chooser on LaunchBox, SteamGridDB, and
  ScreenScraper search results lets you pick the exact image. Plus a
  "Time to beat" fact card.
- **Backlog management.** Every game gets a personal backlog layer: Unplayed
  status rendering, personal 0–5 star ratings with picker weighting, manual
  playtime logging, dated notes, and an optional one-time "Mark as Playing?"
  prompt on first launch.
- **Couch-ready Big Box.** Boot straight into Big Box (`--bigbox` flag and a
  Settings checkbox), a d-pad-navigable on-screen keyboard for search, Steam
  grid artwork copied for bridged games, and a unified single gamepad poll
  loop across all surfaces.
- **Library health score.** A 0–100 score across file integrity, duplicates,
  artwork, metadata, and launch readiness — every deduction names its games,
  with a "Fix all" queue (dry-run preview first, every fix undoable), a Big
  Box health tile, and a scheduled rescan. Pairs with the new Artwork Doctor,
  missing-file repair wizard, and duplicate merge.
- **Game DNA search.** Fully offline smart search: a Title | Smart toggle on
  the search box, BM25 plus a curated 151-concept lexicon in five languages,
  "why" explanation chips, and "More like this" in the details pane, card
  menu, and Big Box. No AI cloud, no downloads — standard library only.

## Fixed

- Big Box pause overlay trapped gamepad users: the pad kept driving the grid
  behind it, A launched the highlighted game, and B killed Big Box mode. The
  overlay now owns the pad while open.
- Attract mode could start over the Big Box pause panel and the Game Night
  party overlay; the overlay guard is restored.
- Opening the Big Box pause overlay for a game without attached documents no
  longer crashes.

---

Full changelog: https://github.com/vindeckyy/OpenBoxGL/compare/v1.13.1...v1.14.0
