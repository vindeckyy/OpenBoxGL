# ADR 0072: Launch readiness actions, a per-game RetroArch core, and disc-image platform choices

**Date:** 2026-10-08
**Status:** Accepted (1.16.1)

## Context

1.16 showed which games would not launch and offered only single-game fixes. The 1.17 plan asked for
group fixes, a repair scoped to one cause, a real core choice, a notice when the audit's totals change,
and the import wizard asking about files several systems share.

## Decision

- **Changes are announced.** When a Launch Audit finishes with different blocked or warning totals from
  the previous one, the notification feed gets one entry, keyed by the totals so a repeat does not add
  another. The first audit says nothing.
- **Group fixes act once.** A group of games that share one fix (one Flatpak grant, one emulator install)
  shows its fix once, for the whole group. After a grant, only that group's games are checked again
  (`POST /api/v2/launch/audit/refresh`). The refresh replaces those games' results in the cached report and moves
  the totals by each game's old and new status. Refreshing every game gives the same groups and totals as a full
  audit, which the tests pin. The refresh refuses a report that no longer describes the library.
- **Repair is scoped to a cause.** "Find moved files" on a missing-file group opens the repair wizard
  limited to that group's games. The server resolves the group from the cached report and refuses a group
  the report no longer has, so the wizard never silently widens.
- **A game can choose its RetroArch core.** The Doctor's missing-core check opens a picker of the cores
  installed in the system and Flatpak folders. The choice is stored on the game as `retroarch_core`, which
  the launch builder and the Doctor both honour. Only a plain `*_libretro.so` name is accepted.
- **Disc images are not guessed.** `.bin`, `.cue` and `.iso` files that several systems share wait for a
  platform choice in the import wizard. The choice must be one of the systems that declare the extension,
  and the file cannot import until it has one. A platform a source named is kept, not re-asked.
- **Badges can be turned off.** `show_launch_badges` (default on) hides the grid badges. The setting is
  saved and reaches the page; the badge text is translated.

## Evidence

- `tests/test_launch_audit.py` (`TotalsChangeNoticeTests`), `tests/test_parity_repair.py`
  (`RepairScopeTests`), `tests/test_emulators.py` (`CoreChoiceTests`, including the route and the
  Flatpak mapping), `tests/test_parity_setup_preview.py` (`AmbiguousDiscChooserTests`),
  `tests/test_settings_schema.py`.
- The group fix and the core picker are wired in `static/health.js` and `static/library.js`; the browser
  flows are manual checks (reliability rows 77 and 80).

## Consequences

- The Doctor's "Choose core" no longer opens the profiles dialog. The adapter choice ("Choose emulator")
  still does.
- Notification text from the server is English, as the cloud-sync notices already are. Translating server
  notices is a separate change.
