# ADR 0062: Windows reliability contract

**Date:** 2026-09-29
**Status:** Accepted (1.15)

## Context

1.13 shipped the Windows platform (ADR 0048). `docs/reliability.md`, the catalog of failure
modes a real user can hit, did not mention Windows, WebView2 or the native host. By 1.14 it
carried Windows rows 33-44 and the changelog credited them to ADR 0060, which is about the
feature-regression ratchets. The contract that governs those rows was never written down.

## Decision

- **Every Windows failure mode a user can hit has a row** in `docs/reliability.md`, with
  a status that says what stops it recurring: `Tested` (names a real `tests/test_*.py`,
  enforced by `check_reliability_catalog.py`), `Manual` (the procedure is written in the
  row), `Documented` (a known limit) or `Gated`.
- **The native host being absent is a supported configuration, not a failure.** With no
  WebView2 runtime, a blocked `native_host.exe`, or a headless session, OpenBox runs in a
  browser tab and must say so instead of silently opening a tab. Settings > About states
  which window is in use, the platform, and the data folder.
- **Install and removal are symmetric.** `scripts/install.ps1` and `scripts/uninstall.ps1`
  create and remove exactly the same things: the install tree and its `openbox.previous`
  rollback copy, the user PATH entry, the Start Menu shortcut and the `openbox://`
  registration. Uninstalling never touches the data folder; the library and settings
  survive so a reinstall picks them up. `tests/test_uninstall_script.py` pins the two
  scripts to each other and to the code that registers the shortcut and protocol.
- **Windows-only code is covered by the `windows-latest` job**, not by coverage exclusions
  alone. A `# pragma: no cover` on a Windows branch is only legitimate because that job
  exercises it (ADR 0048 section 9); the reliability rows are the human-readable
  counterpart.
- **Row numbers are identity.** The catalog ratchet keys a row on its number and scenario, so
  rows are only ever appended (row 49 sits in its own section for that reason).

## Consequences

- A reader can see, per scenario, what CI proves on Windows and what is a manual pass.
- Adding a Windows behaviour without a row is a review failure; dropping a row fails the gate.
