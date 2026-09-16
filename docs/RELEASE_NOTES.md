# OpenBox 1.13.0 — Solid Ground

The finishing pass for the 1.12 wave: the surfaces added in 1.11/1.12 become
faster, safer, and more reliable, and the biggest deferred features land —
household presence, party decks, save history, and artwork tooling.

---

## Added

- **Now Playing presence:** opt-in signed heartbeats with a 10-minute TTL, a
  live "who's playing what" strip in Household, elapsed time, and a
  rate-limited per-member toast. Presence projects unexpired events only and
  keeps no history (ADR 0050).
- **Game Night deck builder:** named queues, theme presets, deterministic
  seeded shuffle, and content-addressed deck sharing over the household
  folder (ADR 0051).
- **Time Machine compare:** added/removed/re-edited diffs between two dates
  with journal honesty flags, plus revert apply constrained to a metadata
  whitelist (paths and launch config are rejected, ADR 0052).
- **Save history:** per-game save versions with source, age, size, read-back
  verification, restore, and retention pruning (ADR 0053).
- **Artwork Doctor:** bulk hygiene report (missing, low-res, odd aspect,
  duplicate art) with a cancelable "fix all with SteamGridDB" job, per-item
  progress, undo, and provider attribution (ADR 0055).
- **Plugin API v1:** frozen SemVer surface — bounded library read, palette
  commands, and notifications — plus malformed/unsandboxed plugin surfacing
  in the manager and `docs/plugin-api.md` (ADR 0056).
- **High-contrast stock theme** (sixth theme) with WCAG AA contrast checks
  for text tokens in the theme test suite (ADR 0057).
- **Per-platform setup checklists**, first-run "try these" cards, auto-Moment
  suggestions, kiosk PIN lockout, constellation viewpoints/path/PNG export,
  signed emulator-definition update channel (ADR 0058), background update
  download with apply on restart (ADR 0059), household weekly challenges,
  the missing-file repair wizard, duplicate detection and merge, the save
  "test restore" drill, session journal export, collection export/import,
  the `?` shortcut cheat sheet, and undo action toasts.

## Changed

- **Performance at scale:** structurally shared library snapshots (ADR 0054),
  touched-id sync journaling, idle-fingerprinted auto-import, one-pass save
  indexing, single-transaction bulk media, SQL-level SQLite FTS/facets,
  media probe batch caching, emulator status TTL with background installs,
  worker-side facets, and a connection cap with a per-request deadline. At
  20k games: `/api/library` p95 85→75 ms, facets 774→46 ms, `/api/media`
  709→2 ms, picker 887→222 ms.
- Error taxonomy (`400` only for validation, `500` with request id for
  faults), a stacked toast queue, virtual-grid ARIA semantics, universal
  `openDialog()` focus handling, conditional dev-venv setup, per-file test
  timeouts with retry reporting, the token gate extended to `index.html`
  and `static/*.js`, and release workflows gated on version sync plus the
  full test gate with Flatpak build attestation.

## Fixed

- Reloading or closing the window no longer stops running games, and Escape
  in the game editor runs the unsaved-changes guard instead of discarding
  edits.
- A corrupt `library.json` no longer blocks startup: OpenBox boots into
  recovery mode and offers last-known-good or snapshot restore.
- LaunchBox/ES-DE apply no longer uploads the full preview plan (which
  exceeded the 64 KB body cap and always failed), and bulk edits chunk large
  selections instead of exceeding the cap.
- Error toasts render as errors again, the locale selector populates after
  settings load, reads never rewrite `library.json`, and concurrent writes
  can no longer expose half-applied state (ADR 0049).
- The full correctness sweep from the 1.13 plan: shared read locks,
  content-keyed projection caches, archive/operations safety, save-path
  containment, lazy BIOS hints, hot-reloadable emulator definitions,
  streaming export downloads, and the media/health/registry fixes.
- The standalone changed-line/touched-module checker now honors ADR 0025:
  test and script edits no longer count as coverage misses, and a touched
  module fails only at 0% instead of an unenforceable whole-file 95%.

## Documentation & gates

- `docs/api-v2.md` is generated from the live route tables with a freshness
  gate, `scripts/check_docs_links.py` verifies relative links,
  `scripts/bump_version.py` automates version bumps, `make check-ci` covers
  the CI-only checks, the changed-line/new-module coverage gates resolve
  their diff base correctly on PRs (ADR 0048), and gate scripts gained
  table-driven self-tests.

---

## Download

| Asset | Architecture | Type |
|-------|-------------|------|
| `OpenBox-x86_64.AppImage` | x86_64 | AppImage |
| `OpenBox-aarch64.AppImage` | ARM64 | AppImage |
| `OpenBox-x86_64.flatpak` | x86_64 | Flatpak |

Already running OpenBox? The built-in updater handles the delta.

---

**Full Changelog**: https://github.com/vindeckyy/OpenBoxGL/compare/v1.12.1...v1.13.0

---

# OpenBox 1.12.1 — Hardening

A reliability pass: no new features, just a sturdier launcher.

---

## Fixed

- **Update verification:** the Ed25519 check now rejects the full
  small-order point blacklist (orders 1, 2, 4 and 8, matching the
  libsodium/ZIP-215 set), a malformed GitHub releases payload fails closed
  instead of crashing, and a symlinked AppImage path updates the real file.
- **Import honesty:** a Steam library on a read-only mount is reported in
  the import result's `errors` list with the actual path instead of being
  skipped silently.
- **Clearer failure surfaces:** RetroAchievements 401/403 responses read
  "RetroAchievements rejected those credentials", and an unreachable
  metadata database reports a connection error in the job panel instead of
  a raw socket message.
- **Every reliability scenario is now gated:** the last manual rows in
  `docs/reliability.md` (non-UTF8 paths, offline sync, read-only mounts,
  bad credentials, long names, delete-while-open, rapid filtering) are
  covered by automated tests, and coverage floors were ratcheted to
  83% / 58% (web_app).
- **Honest empty states and errors:** deleting a playlist that doesn't exist
  returns 404 instead of a false success; scoped library exports without a
  name are rejected synchronously with 400; and an empty Game Night queue
  explains itself with an `empty_reason` plus an exclusion breakdown
  (e.g. no games support N players) instead of a bare empty state.
- **Typed background jobs:** export, SteamGridDB, ScreenScraper,
  auto-import, and reel jobs now run as `library.export`,
  `screenscraper.*`, `steamgrid.*`, `storefront.auto_import`, and
  `clips.reel` with the correct retry policy instead of masquerading as
  `setup.scan`.
- **Backlog Radio estimates that survive reloads:** picks hydrated from the
  stored playlist now carry `estimated_minutes` (with a frontend fallback
  for older entries), fixing "undefinedm" rows.
- **Test isolation:** the suite no longer risks the real library — test runs
  export an isolated `OPENBOX_DATA_DIR` (and `test_sse.py` guards itself at
  import time), so synthetic games can't land in
  `~/.local/share/openbox-game-launcher/library.json`.

---

## Download

| Asset | Architecture | Type |
|-------|-------------|------|
| `OpenBox-x86_64.AppImage` | x86_64 | AppImage |
| `OpenBox-aarch64.AppImage` | ARM64 | AppImage |
| `OpenBox-x86_64.flatpak` | x86_64 | Flatpak |

Already running OpenBox? The built-in updater handles the delta.

---

**Full Changelog**: https://github.com/vindeckyy/OpenBoxGL/compare/v1.12.0...v1.12.1

---

# OpenBox 1.12 — Living Library

OpenBox 1.12 makes the 1.11 feature wave feel finished: searches you pinned
become shelves that stay correct on their own, every game gets a story worth
scrolling, and the launch-options sheet is complete down to environment
variables.

---

## What's New

### Smart Collections

The search bar's deterministic grammar (`short unplayed rpg`, `beaten co-op
before 2000`) can now be pinned. "Save as collection" turns the active query
into a named sidebar shelf that re-evaluates live — a collection stores the
question, not the answer, so "under 5 hours, never played" stays honest as
your library and habits change.

### Game Story

A new Story tab in the detail pane narrates each game's journey: added to the
library, first played, longest session, playtime milestones, progress states,
and the Moments you captured — one deterministic timeline built from the
session journal, no separate tracking required.

### Per-game Environment Overrides

Edit game → Launch gains `KEY=value` environment overrides per title, merged
over the launch environment at spawn. Combined with the existing per-game
launch command, profile, and Gamescope preset, every launch knob is now
per-game.

### Fixed

- The per-game **Gamescope preset override** saved correctly but was never
  projected to the client, so the Edit game select always rendered blank.
  It now round-trips.

Plus the full 1.11.1 hardening sweep already on master: complete large-media
responses, document-reader framing, scroll-stable grid virtualization, and
focus restoration fixes.

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

**Full Changelog**: https://github.com/vindeckyy/OpenBoxGL/compare/v1.11.0...v1.12.0
