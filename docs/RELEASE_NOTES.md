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
