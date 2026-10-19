# ADR 0078: Managed emulator settings and launch-time application

**Date:** 2026-10-18
**Status:** Accepted (1.18 unreleased)

## Context

OpenBox installs and launches MAME and RetroArch but left their display, effects, run-ahead, rewind, and upscaling
options to each emulator's own UI. LaunchBox exposes those options in its emulator editor. Writing emulator config
files unconditionally can destroy a user's hand-tuned setup, and a bad path must not prevent a game from launching.

## Decision

- Emulator settings are stored outside `settings.json` under `state["emulator_settings"]`. Each emulator has `global`
  values, a `platforms` map, and a `managed` flag. MAME and RetroArch schemas are built in; plugins may contribute
  additional sections for an emulator id.
- Global values are overlaid by platform values, then defaults. Unknown emulators, fields, and enum values are
  rejected at the API boundary.
- OpenBox applies settings only when the emulator is marked managed. Installing an emulator marks it managed; startup
  detection leaves an existing choice alone. A user can turn management off without deleting the stored values.
- MAME arguments are inserted before the final ROM token: `-screen`, `-borderless`, `-effect`, and explicit
  lightgun/trackball flags. The scanline effect is passed only when the bundled PNG exists; otherwise the flag is
  omitted with a warning.
- RetroArch settings are written to an owner-only generated file under `<data>/emulator-configs/retroarch/` and
  appended with `--appendconfig`. Existing `--appendconfig` values are merged rather than duplicated. Retry-ahead,
  rewind, shader, and scaling keys use RetroArch's own names.
- Every filesystem failure returns the original argv plus a warning. A settings problem never blocks a launch.
- Plugin emulator sections may add argv strings and a bounded environment, after which forbidden loader variables
  (`LD_PRELOAD`, `LD_LIBRARY_PATH`, `PYTHONPATH`, `PYTHONHOME`) are rejected.

## Evidence

`tests/test_emulator_settings.py` covers schema validation, global/platform overlay, MAME argv order, RetroArch
`--appendconfig` contents, existing-config merging, IO fallback, and unknown fields. `tests/test_plugin_emulator.py`
covers plugin argument and environment merging. `tests/test_emulator_versions.py` covers version probes and update
opt-outs. Install-time managed-state marking is implemented in the install workers and startup import path but is not
exercised by a dedicated test. `docs/PARITY.md` records the completed parity row.

## Consequences

- Settings are reproducible from state and do not mutate the user's emulator configuration except through the
  generated `--appendconfig` file.
- Emulator upgrades and custom builds remain supported: OpenBox never downloads an arbitrary emulator build and only
  writes the keys its schema names.
- A future emulator needs a schema and an argv/config adapter; it does not need a new settings store.
