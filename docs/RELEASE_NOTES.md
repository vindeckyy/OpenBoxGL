# OpenBox 1.14.0 — The library grows up

Six flagships that make the library smarter, more personal, and couch-ready.
No account, no cloud, no telemetry — everything runs locally.

---

## Added

- **Plugins 2.0.** Per-plugin checksum-bound trust (updates re-prompt, no
  global trust toggle), Android-style permission prompts at install/enable
  time, per-plugin settings forms generated from the manifest, a
  `library_source` importer hook that merges plugin games into the library
  with a source badge, a single `events` lifecycle hook, a catalog browser
  tab with Install/Update, and palette integration for plugin commands.
- **Effortless metadata.** One auto-scrape pass after import: offline
  match preview plus opt-in ScreenScraper dual-hash and IGDB passes, then
  media fill from LaunchBox and SteamGridDB. All providers off by default
  with per-run budgets. New thumbnail chooser on every search result, ROM-hash
  confidence before anything auto-applies, and a "Time to beat" fact card.
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
  "why" explanation chips, "More like this" in the details pane, card menu,
  and Big Box. No AI cloud, no downloads — standard library only.

## Fixed

- Big Box pause overlay trapped gamepad users: the pad kept driving the grid
  behind it, A launched the highlighted game, and B killed Big Box mode. The
  overlay now owns the pad while open.
- Attract mode could start over the Big Box pause panel and the Game Night
  party overlay; the overlay guard is restored.
- Opening the Big Box pause overlay for a game without attached documents no
  longer crashes.

## Changed

- The three per-surface gamepad poll loops are unified into a single rAF loop
  in `static/gamepad.js`. No input behavior changes.
- Plugin API v1 is frozen (ADR 0050): manifests declare `api_version`, with a
  `command` hook and up to 32 palette commands.

---

Full changelog: https://github.com/vindeckyy/OpenBoxGL/compare/v1.13.1...v1.14.0
