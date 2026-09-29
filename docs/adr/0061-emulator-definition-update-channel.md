# ADR 0061: Emulator-definition update channel

**Date:** 2026-09-29
**Status:** Accepted (1.15)

## Context

Twenty-four emulator definitions ship in `emulator_defs/`, but a user on a newer
emulator build had no way to receive a newer definition short of editing YAML by
hand. 1.14 shipped the backend for a signed community pack
(`pkg/parity/parity_emulator_defs_update.py`, `handlers/defs.py`, four `/api/v2`
routes) and credited it to ADR 0060, which is about the feature-regression
ratchets. The channel therefore had no decision record of its own, no UI, and no
way to produce the pack it downloads: `PACK_BASE` pointed at a pinned commit that
never contained an `index.json`, so the first real check would have 404ed.

## Decision

- **Routes** (additive `/api/v2`, the v1 surface is untouched):
  `GET /api/v2/emulators/defs/status`, `GET` and `POST /api/v2/emulators/defs/update`,
  `POST /api/v2/emulators/defs/rollback`.
- **Verification is never re-implemented.** The archive is verified with
  `updates.verify_artifact` against `openbox-release.pub`, which already rejects
  small-order keys, fails closed on a malformed signature payload and normalizes the
  digest. A bad signature raises a persisted `security` notification, not a generic
  failure.
- **All-or-nothing.** The pack is fetched, verified, parsed and validated in full
  before a single file is written.
- **Local wins.** The pack installs into the per-user data directory and shadows the
  bundled set; it never overwrites it, and a definition the user already has is kept.
  Rollback removes exactly what the channel installed.
- **Publishing.** `scripts/build_defs_pack.py` builds a byte-deterministic
  `community-defs.tar.gz` and `index.json`. It never touches a key. Signing needs the
  private half of the release key, so it is a maintainer step (`scripts/sign_release.py`),
  followed by uploading the archive, its `.sig` and `index.json` to the rolling
  `emulator-defs` release that `PACK_BASE` names.
- **Mutable location, immutable trust.** `PACK_BASE` is no longer a pinned commit. The
  location can change; the trust cannot, because nothing is installed unless the
  signature verifies. The index is only a cheap "is there something newer" hint.
- **No pack published is a normal state.** A 404 from the index is shown as "No community
  definition pack is published yet", not as an error.
- **UI.** Settings > Emulators shows the installed pack, the definitions the user edited
  (never overwritten), and the check / install / roll back actions.

## Consequences

- The channel is reachable from the UI and can be turned on by publishing one signed pack.
- A downgrade (serving an older, validly signed pack) is possible from a mutable location.
  The blast radius is a definition set that was once valid, and rollback restores the
  bundled set. If that ever matters, put the pack version inside the signed archive and
  refuse a lower one.
- Publishing depends on a maintainer holding the key; the code cannot do it.
