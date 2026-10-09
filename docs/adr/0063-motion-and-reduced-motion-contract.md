# ADR 0063: Motion, top-layer and reduced-motion contract

**Date:** 2026-09-29
**Status:** Accepted (1.15)

## Context

The UI had no motion vocabulary. Seven different durations between 140 and 240 ms, three
easings, no tokens, and three separate `prefers-reduced-motion` blocks plus one per theme that
set a token nothing read. That is why reduced motion was incomplete: `.library-head`, the
party wheel's wait, the constellation layout and the busy spinner all ignored it, and each
new animation had to remember to opt in. There were also no exit animations, the grid replayed
its entrance on every search keystroke, and toasts sat at `z-index: 20`, dimmed under any modal
and invisible under Big Box.

## Decision

- **Structural motion tokens** live in `static/app.css` `:root`: `--dur-fast` 150 ms,
  `--dur-base` 200 ms, `--dur-slow` 240 ms, `--dur-out` 140 ms (every exit), `--dur-spin`
  2400 ms, `--dur-loop`, `--stagger`, and `--ease-out`, `--ease-in`, `--ease-move`,
  `--ease-linear`. They are structural, not themable: a theme must not redeclare them
  (`tests/test_stock_themes.py`), or its stylesheet, which loads after `app.css`, would defeat
  the override below. The largest shift from the old values is 20 ms.
- **Reduced motion is correct by construction.** One `@media (prefers-reduced-motion: reduce)`
  block sets every duration token to about zero using `:root:root` (specificity 0,2,0, so it
  beats a theme's `:root` regardless of load order). Every animation and transition takes its
  duration from a token, so nothing needs to opt in. Infinite loops cannot be zeroed and are
  switched off; the busy state then shows an ellipsis instead of a frozen spinner.
- **JS-owned motion reads the same tokens** through `motionMs()` in `static/util.js`, at call
  time, so an operating-system toggle applies live. The party wheel, the constellation layout,
  dialog exits and the toast timers use it. `test_frontend_contract.py` requires a script-set
  transition to name a token and the animating modules to consult `motionMs()`.
- **Every dialog close plays an exit.** `HTMLDialogElement.prototype.close` is wrapped once in
  `static/dialogs.js`: it adds `.closing`, waits for `animationend` (with a timer fallback), then
  closes. This covers all raw call sites without a sweep and also wires the shared focus
  handling for lazily built dialogs. Callers that need "closed" listen for the `close` event.
- **One toast surface in the top layer.** `#toast` is a `popover="manual"` element, so no modal
  backdrop or Big Box overlay can cover it. Screen readers hear a separate always-rendered live
  region. A message sent while an action toast (Undo, View) is showing is held and shown after,
  never allowed to overwrite the action.
- **Entrance animation is for view changes only.** `renderGrid` animates on first paint and
  when the platform, playlist, preset or view changes, never on a search keystroke, favourite,
  bulk toggle, resize or cover-ratio regroup.
- **Gates.** `scripts/check_tokens.py` counts raw durations, easings, z-index, border-radius,
  box-shadow and `.showModal()` calls outside `dialogs.js`, each with a baseline that may only
  fall. `tests/test_stock_themes.py` runs a contrast matrix over every theme. `scripts/ui_smoke.cjs`
  asserts the exit plays, reduced motion closes synchronously with no running animation, the
  toast survives a concurrent message, and a search keystroke does not replay the entrance.
- **Not decided here:** pixel-golden screenshots (fonts differ per OS and a diff library would
  join the `npm audit` gate) and a computed-style contract file. The token gate plus the
  reduced-motion probe already cover what they would.

## Consequences

- Adding an animation means choosing a token; a raw duration fails the gate.
- Reduced motion cannot regress by omission.
- `popover` and `:has()` need a WebKitGTK from the last few years; both are feature-detected or
  degrade to the previous layout, and the supported Flatpak and Windows hosts ship them.
- Known limit: the native hosts paint `#11100e` before the page loads, so a light theme can
  flash dark for a few milliseconds. Fixing that needs a native-host rebuild and is out of scope.

## Addendum (1.16.1): the native first paint

The native windows no longer paint `#11100e` before the page loads when the active theme has another background. The app records the theme's background for them (ADR 0071).
