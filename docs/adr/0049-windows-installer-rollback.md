# ADR 0049: Windows installer rollback contract

**Date:** 2026-09-22
**Status:** Accepted

## Context

The Windows installer (`scripts/install.ps1`) replaces the installed tree at
`%LOCALAPPDATA%\OpenBox\share\openbox` and then registers desktop integration
(a Start Menu shortcut and the `openbox://` protocol handler). If registration
failed after the tree was replaced, the user was left with a half-registered
installation: new files on disk but no shortcut and no protocol handler, and no
way back to the previous working install.

## Decision

1. **The previous tree is kept until desktop integration succeeds.** Before the
   new tree replaces the old one, the existing install is moved aside to
   `share\openbox.previous`.
2. **Registration failure restores the previous tree.** A `$RestorePreviousInstall`
   hook is registered immediately after the new tree lands (`scripts/install.ps1`).
   If Start Menu or `openbox://` protocol registration fails, the hook removes
   the partially installed tree and moves `openbox.previous` back into place,
   then the installer exits with an error. The install is never left without
   its shortcut and protocol handler.
3. **The previous tree is only discarded on success.** A successful install may
   keep `openbox.previous` for the next upgrade cycle; it is never deleted
   before the new install is fully registered.

## Consequences

- Positive: a failed upgrade always leaves a working previous install instead
  of a broken half-state.
- Negative: failed installs leave the previous tree on disk by design; the
  installer reports this explicitly.
- Neutral: the rollback covers desktop integration only; download/verification
  failures already abort before anything is replaced.
