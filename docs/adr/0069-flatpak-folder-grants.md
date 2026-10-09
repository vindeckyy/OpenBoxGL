# ADR 0069: Flatpak folder grants are read-only, confirmed, and removable

**Date:** 2026-10-08
**Status:** Accepted (1.16.1)

## Context

A Flatpak emulator's sandbox can read only the folders it has been granted. When a game's folder is
outside those grants, the Launch Doctor reports `FLATPAK_FS_DENIED`. Reinstalling the emulator does
not change its grants, so the earlier fix suggestion did not help. Flatpak's own answer is
`flatpak override --user --filesystem=<folder>:ro <app>`, which is persistent and needs no root.

## Decision

- OpenBox runs that override only when all of these hold: the Flatpak id is one a bundled emulator
  definition declares; the folder is absolute, exists, is a directory inside the user's home, and is
  not the home directory itself; the request came from the Launch Doctor after the user confirmed the
  exact command in a dialog. The grant is read-only (`:ro`).
- Remove access runs `flatpak override --user --nofilesystem=<folder> <app>`. It is offered in the
  panel that made the grant, for the rest of the session.
- Each `flatpak` call has a 30 s timeout. A refusal or a hang becomes a message; nothing is retried.
- The command is also shown with a Copy button, so a user can run or review it outside OpenBox.

## Consequences

- A grant outlives OpenBox. Remove access is offered only in the session that made the grant. After a
  restart, the grant is removed outside OpenBox with `flatpak override --user --nofilesystem=<folder> <app>`
  (the command the grant ran, with the flag changed).
- The grant never reinstalls the emulator and never widens access beyond one folder.
- Alternatives rejected: granting the whole home folder (too broad); a grant with no confirmation
  (a permission change must be visible to the user); a Flatpak portal file chooser (the portal does
  not change a sandbox's static grants).

## Evidence

- `pkg/parity/parity_flatpak_grant.py`, with its guards and the exact command; tests in
  `tests/test_parity_flatpak_grant.py` (validation, command shape, refusal and timeout paths).
- Reliability rows 58 (automated) and 74 (manual procedure for the real sandbox).
