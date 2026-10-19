# ADR 0079: Newcomer GUI clarity and the default color system

**Date:** 2026-10-19
**Status:** Accepted (1.18 unreleased)

## Context

OpenBox is feature-rich but the first run is hostile to a newcomer: five overlapping top-bar add actions, a flat
38-item Tools menu, an eight-step setup wizard that opens on a zeroed dashboard with developer copy, an empty library
with six competing buttons, no help or glossary, and a details pane that says only "Select a game". The visual layer
is coherent but flat: one shadow value, 2 px cover lift, and keyboard focus in the same orange as selection.

## Decision

### Color roles and contrast

The default theme becomes a warm-neutral ramp with a split-complementary accent system. Neutrals and cream text carry
the screen; brand orange `#FF7A1A` marks selection and action; focus teal `#5FD3C4` is deliberately distinct from
selection; danger is `#B8473F` with a light `--badge-danger-fg` for text. Body text targets 7:1, muted text and control
boundaries 4.5:1 and 3:1 respectively. The exact token table lives in `static/app.css :root` and
`docs/development/DESIGN.md`; all six stock themes mirror the same elevation tokens and accent-consistency rules.

### Quick setup path

`STEPS` keeps all eight ids for programmatic callers. The two advanced steps (Welcome overview and Artwork & details)
are hidden until "Show advanced options" is pressed, so the default first run is six steps: sources, scan, review,
emulators, import, finish. The stepper exposes a progress bar and `Step N of M`; the raw preview revision is removed
from Confirm. Dismissal is persisted in `localStorage` under `openbox.setup.dismissed` and suppresses auto-open until
onboarding restarts.

### Guidance surfaces

The top bar collapses Add one game, Scan a folder, Add without files, and Guided setup plus store imports into one Add
games menu. Tools is grouped Start here / Library / Sources / More and every ambiguous label carries a hint. A new
Help & shortcuts dialog (`F1`, `?`, palette) provides a start-here tab, a glossary, and the real shortcut table. An
empty library shows one illustrated card with three actions and a setup link; the details pane shows a dismissible
three-step onboarding checklist. A Jump back in hero above the drop zone surfaces the most recently played visible
game with Play and View details. Play insights now follows the grid, so a populated library leads with its games
rather than a metrics dashboard. The Themes dialog gains a real swatch gallery.

No route, setting key, backend shape, or database schema changes. The only new persistence is two namespaced UI flags:
`openbox.setup.dismissed` and `openbox.onboarding.dismissed`.

## Evidence

`tests/test_frontend_contract.py` pins the tool groups, Add menu, Help surface, translated empty state, home hero,
setup step keys, dismissal write sites, and auto-open gates. `tests/test_stock_themes.py` extends the contrast matrix
to danger text, ratio counts, progress badges, achievement text, Big Box copy/header, and lifecycle kickers.
`scripts/check_frontend_modules.py` registers `static/help.js`. `scripts/ui_smoke.cjs` exercises the Add menu focus
path, theme z-order, setup reset flags, and group contract. `python3 -B scripts/run_ui_smoke.py` passes the main and
Big Box passes.

## Consequences

- Newcomers get one obvious way to add games, one quick setup path, and help that explains the vocabulary.
- Advanced setup remains reachable without a second wizard implementation.
- Colors, elevation, and focus are now tokenized; a new theme must supply the same token set and pass the contrast
  matrix.
- The two localStorage flags are UI-only and intentionally per-browser; they do not sync through the library.
- In `es`, `de`, `fr`, and `pt`, the renamed and newly added Tools entries (including the four group headings and all
  17 hints), the Add games menu, the new top-bar labels, and the new setup, help, onboarding, and Jump back in copy
  ship in English (about 40 Tools/nav keys per locale; untouched entries keep their existing translations). Key and
  placeholder parity is enforced, but translated copy is deferred to a follow-up pass.
