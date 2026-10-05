# ADR 0064: Changelog-truth contract

**Date:** 2026-10-04
**Status:** Accepted (1.16)

## Context

1.15.0 shipped four claims in `docs/CHANGELOG.md` that were true of the documentation and
not of the code:

- **"Every dialog now plays an exit."** True only for a JS `close()`. Escape and a
  backdrop click run the browser's close watcher, which removes `open` without calling
  `close()`, so 21 of 29 dialogs still snapped shut and dropped focus to `<body>`.
- **The toast queue.** There was one `#toast` element written by several modules. A second
  message destroyed the first one's Undo button.
- **The emulator-definition channel's atomicity.** A failed install could leave a partly
  written definition set and a ledger that disagreed with it.
- **The reduced-motion gate.** The test accepted a module that read motion settings in
  either of two ways (an OR), so a module using neither still passed.

None of these was a missing feature. Each was a feature the changelog described, with a
test that could not tell whether the behaviour was there. ADR 0060 pins *surfaces* (routes,
settings, definitions, modules, the reliability catalog); nothing pinned *behaviour*, and
nothing compared the changelog with the code. The 1.16 sweep found the same gap in passing
gates: a check whose only assertion sat inside `if errors:` (S25) passed on any input, and
several gates checked that something existed rather than that it was complete (S20, S21).

## Decision

Four rules, applied from 1.16.0 on.

1. **A claim is written after the gate that proves it.** A changelog or release-notes bullet
   that names a behaviour needs a test that fails when the behaviour is absent, or an
   explicit `Manual` or `Documented` row in `docs/reliability.md` that says how it was
   checked. A reliability row marked `Tested` must name a test that exists;
   `scripts/check_reliability_catalog.py` already rejects one that does not.
2. **Gates test behaviour, not presence.** "`renderPanelError` exists" is not a test. "A
   failing health scan renders a retry button" is. Where a browser is the only place the
   behaviour exists, the gate is a `ui_smoke` case that drives the real code.
3. **A gate that cannot fail is fixed or removed.** A conditional around the *only*
   assertion is the usual cause. Assertions inside a loop are fine; a guard that lets the
   whole test pass on empty input is not. A `ui_smoke` case that works over a list must also
   assert the list is non-empty, or it proves nothing on an empty fixture.
4. **A shipped claim is re-checked at the next release against the code, not the plan.** A
   claim that fails the re-check is corrected in public, in the next release's notes, under
   its own heading, rather than fixed silently.

## Consequences

- 1.16.0 applies rule 4 to 1.15.0: the release notes carry a "Corrections to 1.15.0"
  section naming all four claims, and each is now backed by a gate:
  `test_every_dialog_close_path_goes_through_the_exit_choke_point` and
  `test_escape_is_not_handled_twice` for dialog exits; `test_the_toast_surface_has_one_writer`,
  `test_toast_timers_pause_and_resume` and `test_toasts_are_raised_above_dialogs` for the toast
  queue; `tests/test_defs_install_atomicity.py` for the definitions channel; and the motion
  gate is now an AND over a reviewed allow-list (S22).
- S25's vacuous guard was removed, and the new 1.16 `ui_smoke` cases assert non-empty lists
  (`motion.restorePreview` checks that a backup was listed; `motion.launchAudit` checks its
  cause rows and member count).
- The 1.16.0 reliability rows 50–54 were added after the tests they name, and the version
  bump, `docs/api-v2.md` and the route baseline were regenerated from the code.
- Cost: writing the gate first makes some claims slower to land, and a few behaviours that
  need a physical device (Steam Deck, Wayland scaling) can only be `Manual`. That is the
  honest status for them, and the catalog already supports it.
